#!/usr/bin/env python3
"""
M3U Index Updater
==================
Обслуживает НЕСКОЛЬКО плейлистов сразу: index.m3u и дополнительные тематические
файлы (sport.m3u, music.m3u, foreign.m3u, children.m3u, tv_series.m3u — см.
EXTRA_PLAYLISTS). Читает их, качает источники, находит каналы с совпадающим
именем/tvg-id и вставляет рабочие ссылки прямо в блок нужного канала — в том
файле, где этот канал лежит (первая активная ссылка без '#', последующие как
'#url'-альтернативы).

Найденные, но ещё не разобранные каналы сваливаются в группу '# test' В КОНЦЕ
index.m3u (единый «входящий» ящик). Дедуп при этом идёт против ссылок ВО ВСЕХ
обслуживаемых файлах, поэтому канал, уже лежащий в sport.m3u/music.m3u/…, в test
повторно не попадёт.

Configuration files (ищутся в текущей директории):
    sources.txt        — источники плейлистов (по URL в строке)
    name_blocklist.txt — блоклист имён каналов
    aliases.txt        — алиасы: '<имя в источнике> => <имя в плейлисте>'
    url_blocklist.txt  — блоклист URL (подстроки; '*' — wildcard)

Requirements: Python 3.8+  —  no third-party libraries.

Usage:
    python3 m3u_checker.py [options]

Examples:
    python3 m3u_checker.py                   # index.m3u + все EXTRA_PLAYLISTS
    python3 m3u_checker.py --no-extra        # только index.m3u
    python3 m3u_checker.py --index my_channels.m3u
    python3 m3u_checker.py --timeout 10 --workers 20
    python3 m3u_checker.py --sources https://example.com/list.m3u
    python3 m3u_checker.py --dry-run         # preview without writing
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.request
import urllib.error
from collections import Counter, OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


# ──────────────────────────────────────────────────────────
#  Configuration
# ──────────────────────────────────────────────────────────






# Паттерны для блокировки по имени канала (регулярные выражения).
# Канал блокируется, если его имя содержит хотя бы один совпадающий паттерн.
# Примеры попадающих имён: «Первый канал (+2)», «НТВ (+4)», «2x2 (+7)».
BLOCKLIST_PATTERNS: list[re.Pattern] = [
    re.compile(r'\([+-]\d+\)'),            # временной сдвиг в скобках: (+2), (-3), …
    re.compile(r'(?<=\s)[+-]\d+(?=\s|$)'), # временной сдвиг без скобок: «НТВ +2», «НТВ +2 HD»
    re.compile(r'XXX', re.IGNORECASE),
    re.compile(r'Erotic', re.IGNORECASE),
    re.compile(r'Adult', re.IGNORECASE),
    re.compile(r'\bPenthouse\b', re.IGNORECASE),
    re.compile(r'18+', re.IGNORECASE),
    re.compile(r'Private', re.IGNORECASE),
    re.compile(r'\bHustler\b', re.IGNORECASE),
]


def _wildcard_to_regex(pattern: str) -> re.Pattern:
    """Compile a blocklist pattern containing '*' into a regex.

    Every character except '*' is matched literally; '*' matches any run
    of characters (including none).  Used with re.search, so a pattern
    like '*.hh.ee' blocks any URL whose host ends in '.hh.ee', and
    'rt-*-htlive.cdn.ngenix.net' blocks every such regional host.
    """
    return re.compile(".*".join(re.escape(part) for part in pattern.split("*")))

URL_BLOCKLIST_FILE  = "url_blocklist.txt"  # блоклист URL: подстроки и wildcard '*'
SOURCES_FILE        = "sources.txt"        # источники плейлистов, по URL в строке
NAME_BLOCKLIST_FILE = "name_blocklist.txt" # блоклист имён каналов
ALIASES_FILE        = "aliases.txt"        # '<имя в источнике> => <имя в index.m3u>'


def _read_config_lines(path: str, log: logging.Logger, what: str) -> list[str]:
    """Непустые строки файла без #-комментариев (как есть, с дублями)."""
    if not os.path.exists(path):
        log.warning(f"⚙️  {what}: файл {path!r} не найден — использую пустой список")
        return []
    out: list[str] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                out.append(line)
    return out


@dataclass
class Config:
    """Настраиваемые списки, загруженные из файлов (см. *_FILE выше)."""
    sources: list[str] = field(default_factory=list)
    name_blocklist: set[str] = field(default_factory=set)      # lower-case
    aliases: dict[str, str] = field(default_factory=dict)      # lower(источник) -> канон
    url_block_plain: set[str] = field(default_factory=set)
    url_block_wildcard: list[re.Pattern] = field(default_factory=list)

    def url_blocked(self, url: str) -> bool:
        """True, если URL попадает под url_blocklist.txt (подстрока или wildcard)."""
        url_lc = url.lower()
        if any(pat in url_lc for pat in self.url_block_plain):
            return True
        return any(rx.search(url_lc) for rx in self.url_block_wildcard)

    def name_blocked(self, name: str) -> bool:
        return name.strip().lower() in self.name_blocklist \
            or any(p.search(name) for p in BLOCKLIST_PATTERNS)


def load_config(log: logging.Logger) -> Config:
    """Читает конфиг-файлы; формат-проблемы (дубли ключей и т.п.) — warning в лог."""
    cfg = Config()
    cfg.sources = _read_config_lines(SOURCES_FILE, log, "sources")

    raw_names = _read_config_lines(NAME_BLOCKLIST_FILE, log, "name blocklist")
    for name, cnt in Counter(n.lower() for n in raw_names).items():
        if cnt > 1:
            log.warning(f"⚙️  name_blocklist: дубль записи {name!r} ×{cnt}")
    cfg.name_blocklist = {n.lower() for n in raw_names}

    for line in _read_config_lines(ALIASES_FILE, log, "aliases"):
        if "=>" not in line:
            log.warning(f"⚙️  aliases: строка без '=>' пропущена: {line!r}")
            continue
        src_name, dst = (part.strip() for part in line.split("=>", 1))
        key = src_name.lower()
        if key in cfg.aliases and cfg.aliases[key] != dst:
            log.warning(f"⚙️  aliases: дубль ключа {src_name!r}: "
                        f"{cfg.aliases[key]!r} → {dst!r} (беру последний)")
        cfg.aliases[key] = dst

    url_patterns = _read_config_lines(URL_BLOCKLIST_FILE, log, "url blocklist")
    cfg.url_block_plain    = {p.lower() for p in url_patterns if "*" not in p}
    cfg.url_block_wildcard = [_wildcard_to_regex(p.lower()) for p in url_patterns if "*" in p]

    log.info(f"⚙️  Config: {len(cfg.sources)} source(s), "
             f"{len(cfg.name_blocklist)} blocked name(s), {len(cfg.aliases)} alias(es), "
             f"{len(url_patterns)} url pattern(s)")
    return cfg


def validate_config(cfg: Config, blocks: list[IndexBlock], log: logging.Logger) -> None:
    """Перекрёстные проверки конфига и индекса. Только предупреждения, ничего не меняет."""
    warn = 0

    for u, cnt in Counter(cfg.sources).items():
        if cnt > 1:
            warn += 1
            log.warning(f"⚙️  sources: дубль источника (×{cnt}): {u}")

    for src_name, dst in sorted(cfg.aliases.items()):
        dst_l = dst.strip().lower()
        if src_name == dst_l:
            warn += 1
            log.warning(f"⚙️  aliases: самоалиас (no-op): {dst!r}")
        elif dst_l in cfg.aliases:
            warn += 1
            log.warning(f"⚙️  aliases: цепочка {src_name!r} → {dst!r} → "
                        f"{cfg.aliases[dst_l]!r} — однопроходный lookup её не резолвит, "
                        f"укажи финальное имя сразу")
        if dst_l in cfg.name_blocklist:
            # Осознанный приём «канонизируй имя → блокируй канон» — не warning.
            log.debug(f"⚙️  aliases: цель алиаса {dst!r} заблокирована в name_blocklist")

    index_names = {b.name.strip().lower(): b.name for b in blocks}
    for n in sorted(set(index_names) & cfg.name_blocklist):
        warn += 1
        log.warning(f"⚙️  конфликт: {index_names[n]!r} есть в index.m3u, но заблокирован — "
                    f"чекер не принесёт ему свежих ссылок")

    log.info(f"⚙️  Config validation: {warn} warning(s)" if warn
             else "⚙️  Config validation: OK")


DEFAULT_INDEX_FILE  = "index.m3u"

# Дополнительные плейлисты, которые чекер обслуживает НАРАВНЕ с index.m3u:
# у их каналов тоже обновляются рабочие ссылки (Step 5a).
# Формат: '<человекочитаемая метка / основная group-title>': '<файл>'.
# Метка идёт только в лог; матчинг каналов — по имени и tvg-id, не по группе,
# поэтому файлы с несколькими группами (foreign.m3u, children.m3u, …) тоже ок.
# Открытие «мусорки» test (Step 5b) остаётся ТОЛЬКО в index.m3u, но дедуп
# новых ссылок идёт против URL'ов ВСЕХ перечисленных файлов.
EXTRA_PLAYLISTS: "OrderedDict[str, str]" = OrderedDict([
    ("Спорт",      "sport.m3u"),
    ("Музыка",     "music.m3u"),
    ("Зарубежные", "foreign.m3u"),
    ("Детские",    "children.m3u"),
    ("ТВ Сериалы", "tv_series.m3u"),
])

LOG_FILE            = "m3u_checker.log"
DEFAULT_TIMEOUT_SEC = 8
DEFAULT_WORKERS     = 30
FETCH_TIMEOUT_MULT  = 3               # таймаут скачивания источника = timeout * MULT
BACKUP_SUFFIX       = ".checker.bak"  # бэкап index.m3u перед перезаписью

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# ──────────────────────────────────────────────────────────
#  Data classes
# ──────────────────────────────────────────────────────────

@dataclass
class SourceChannel:
    """One channel entry parsed from a remote source playlist."""
    extinf_line: str
    url: str
    name: str
    source: str
    tvg_id: str = ""
    reachable: Optional[bool]  = None
    http_status: Optional[int] = None
    check_error: Optional[str] = None
    check_ms: Optional[float]  = None
    content_type: Optional[str] = None   # Content-Type из ответа сервера
    stream_verified: bool = False         # True если magic-байты подтвердили формат
    net_error: bool = False               # отказ по сети/таймауту (не проверить), а не «мёртв»


@dataclass
class IndexBlock:
    """
    One channel block in the local index.m3u.

    lines  — all raw lines that belong to this block
             (the #EXTINF line + all URL lines, active and commented)
    name   — display name extracted from the #EXTINF line
    tvg_id — tvg-id attribute from the #EXTINF line (may be empty)
    urls   — set of all known URLs (stripped of leading #) for dedup
    """
    lines: list[str]
    name: str
    tvg_id: str = ""
    urls: set[str] = field(default_factory=set)
    origin: str = ""   # имя файла-плейлиста, которому принадлежит блок (для статистики)


@dataclass
class Playlist:
    """Один обслуживаемый .m3u-файл: index.m3u или один из EXTRA_PLAYLISTS."""
    path: str                                  # путь к файлу
    label: str                                 # метка для лога ('index' или group-title)
    header: list[str] = field(default_factory=list)
    blocks: list[IndexBlock] = field(default_factory=list)
    is_index: bool = False                     # True → сюда идёт test-дамп (Step 5b)


@dataclass
class Stats:
    sources_ok:   int = 0
    sources_fail: int = 0
    parsed:       int = 0
    candidates:   int = 0
    reachable:    int = 0
    dead:         int = 0   # сервер ответил, но потока нет (HTTP>=400 / HTML / битый HLS)
    net_fail:     int = 0   # сеть/таймаут — проверить не удалось (возможно, жив)
    inserted:     int = 0
    appended:     int = 0
    _start: float = field(default_factory=time.time, repr=False)

    @property
    def elapsed(self) -> str:
        s = int(time.time() - self._start)
        return f"{s // 60}m {s % 60}s" if s >= 60 else f"{s}s"


# ──────────────────────────────────────────────────────────
#  Logging
# ──────────────────────────────────────────────────────────

def setup_logging(log_file: Optional[str]) -> logging.Logger:
    log = logging.getLogger("m3u_checker")
    log.setLevel(logging.DEBUG)
    fmt = logging.Formatter("[%(asctime)s] %(levelname)-8s %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    log.addHandler(ch)

    if log_file:
        fh = logging.handlers.RotatingFileHandler(
            log_file, encoding="utf-8", maxBytes=5_000_000, backupCount=2
        )
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        log.addHandler(fh)
        log.info(f"Full debug log → {log_file}")

    return log


# ──────────────────────────────────────────────────────────
#  Helpers: tvg-id extraction & matching
# ──────────────────────────────────────────────────────────

_TVG_ID_RE = re.compile(r'tvg-id="([^"]*)"', re.IGNORECASE)


def extract_tvg_id(extinf_line: str) -> str:
    """Return the tvg-id value from an #EXTINF line, or empty string."""
    m = _TVG_ID_RE.search(extinf_line)
    return m.group(1).strip() if m else ""




# ──────────────────────────────────────────────────────────
#  index.m3u parsing and writing
# ──────────────────────────────────────────────────────────

def parse_index_m3u(path: str, cfg: Config, log: logging.Logger,
                    origin: Optional[str] = None) -> tuple[list[str], list[IndexBlock]]:
    """
    Parse a local playlist file (index.m3u or one of the extra playlists).

    Args:
        origin — метка файла, проставляется каждому блоку (по умолчанию basename пути).

    Returns:
        header_lines  — lines before the first #EXTINF block (e.g. #EXTM3U)
        blocks        — list of IndexBlock, one per channel
    """
    origin = origin or os.path.basename(path)
    if not os.path.exists(path):
        log.warning(f"Index file not found: {path}")
        return ["#EXTM3U\n"], []

    with open(path, encoding="utf-8") as f:
        raw_lines = f.readlines()

    header_lines: list[str] = []
    blocks: list[IndexBlock] = []
    current_block_lines: list[str] = []
    in_block = False
    dropped_urls: list[tuple[str, str]] = []  # (имя канала, URL) — вычищено по блоклисту

    def _finish_block(blines: list[str]) -> Optional[IndexBlock]:
        """Turn accumulated lines into an IndexBlock."""
        extinf = next((l for l in blines if l.strip().upper().startswith("#EXTINF")), None)
        if not extinf:
            return None
        extinf_s = extinf.strip()
        name   = _clean_name(_parse_extinf_name(extinf_s))
        tvg_id = extract_tvg_id(extinf_s)

        # Remove any URL lines that match URL_BLOCKLIST (подстроки И wildcard-паттерны).
        # Удаление НЕ молчаливое: всё вычищенное копится в dropped_urls и логируется.
        if cfg.url_block_plain or cfg.url_block_wildcard:
            cleaned: list[str] = []
            for l in blines:
                stripped = l.strip()
                if stripped.startswith("#"):
                    candidate = stripped.lstrip("#").strip()
                else:
                    candidate = stripped
                if candidate.startswith(("http://", "https://", "rtmp")) \
                        and cfg.url_blocked(candidate):
                    dropped_urls.append((name, candidate))
                    continue  # drop this URL line
                cleaned.append(l)
            blines = cleaned

        # Collect all URLs (active and commented) for dedup
        urls: set[str] = set()
        for l in blines:
            stripped = l.strip()
            if stripped.startswith("#"):
                candidate = stripped.lstrip("#").strip()
            else:
                candidate = stripped
            if candidate.startswith(("http://", "https://", "rtmp")):
                urls.add(candidate)
        return IndexBlock(lines=blines, name=name, tvg_id=tvg_id, urls=urls, origin=origin)

    for line in raw_lines:
        stripped = line.strip()
        if stripped.upper().startswith("#EXTINF"):
            # Save previous block if any
            if in_block and current_block_lines:
                blk = _finish_block(current_block_lines)
                if blk:
                    blocks.append(blk)
            current_block_lines = [line]
            in_block = True
        elif in_block:
            current_block_lines.append(line)
        else:
            header_lines.append(line)

    # Last block
    if in_block and current_block_lines:
        blk = _finish_block(current_block_lines)
        if blk:
            blocks.append(blk)

    log.info(f"📂 Parsed {os.path.basename(path)}: {len(blocks)} channel blocks")
    if dropped_urls:
        log.info(f"🧹 Removed {len(dropped_urls)} blocklisted URL line(s) from index:")
        for nm, u in dropped_urls:
            log.info(f"   • {nm!r}: {u}")
    for b in blocks:
        log.debug(f"   Block: {b.name!r}  ({len(b.urls)} URLs)")
    return header_lines, blocks


def _parse_extinf_name(line: str) -> str:
    return line.rsplit(",", 1)[-1].strip() if "," in line else ""


def _clean_name(name: str) -> str:
    """Strip leading/trailing whitespace from a channel name."""
    return name.strip()


def write_index_m3u(
    path: str,
    header_lines: list[str],
    blocks: list[IndexBlock],
    log: logging.Logger,
    dry_run: bool = False,
) -> None:
    """Reassemble and write the index.m3u from header + blocks."""
    output = list(header_lines)
    for blk in blocks:
        output.extend(blk.lines)
        # Ensure blocks are separated by a blank line
        if output and output[-1].strip():
            output.append("\n")

    content = "".join(output)

    if dry_run:
        log.info("[DRY RUN] Would write:")
        for line in content.splitlines()[:40]:
            log.info(f"   {line}")
        if len(content.splitlines()) > 40:
            log.info("   ... (truncated)")
        return

    # Бэкап предыдущей версии + атомарная запись (tmp-файл → os.replace),
    # чтобы краш посреди записи не оставил битый/пустой index.m3u.
    if os.path.exists(path):
        backup = path + BACKUP_SUFFIX
        shutil.copy2(path, backup)
        log.info(f"🛟 Backup → {backup}")

    dst_dir = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp_path = tempfile.mkstemp(
        dir=dst_dir, prefix=os.path.basename(path) + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    log.info(f"💾 Written → {path}  ({len(content):,} bytes)")


# ──────────────────────────────────────────────────────────
#  HTTP helpers
# ──────────────────────────────────────────────────────────

def _make_request(url: str, method: str) -> urllib.request.Request:
    req = urllib.request.Request(url, method=method)
    req.add_header("User-Agent", USER_AGENT)
    req.add_header("Accept", "*/*")
    return req


# ──────────────────────────────────────────────────────────
#  Stream validation helpers
# ──────────────────────────────────────────────────────────

# MIME-типы, которые однозначно указывают на медиапоток.
_STREAM_CONTENT_TYPES: frozenset = frozenset({
    "audio/mpeg", "audio/mp3", "audio/aac", "audio/ogg", "audio/flac",
    "audio/x-mpegurl", "audio/mpegurl", "audio/x-ms-wma",
    "video/mp4", "video/mpeg", "video/x-flv", "video/webm",
    "video/quicktime", "video/x-msvideo", "video/mp2t",
    "application/vnd.apple.mpegurl", "application/x-mpegurl",
    "application/octet-stream",   # Часто используется стримами — нужна проверка байт
})

# MIME-типы, которые указывают на HTML-страницу (геоблок, авторизация и т.п.).
_ERROR_CONTENT_TYPES: frozenset = frozenset({
    "text/html",
    "application/xhtml+xml",
})


def _parse_mime(content_type: str) -> str:
    """Нормализовать Content-Type: убрать параметры (charset и т.п.)."""
    return content_type.split(";")[0].strip().lower()


def _is_stream_magic(data: bytes) -> bool:
    """
    Проверить magic-байты данных на соответствие известным форматам.

    Поддерживаемые форматы:
    - MP3: ID3-тег (0x49 0x44 0x33) или MPEG sync word (0xFF 0xEx/0xFx)
    - MPEG-TS: sync byte 0x47 каждые 188 байт
    - AAC ADTS: 0xFF 0xF1 (MPEG-4) или 0xFF 0xF9 (MPEG-2)
    - Ogg: OggS (0x4F 0x67 0x67 0x53)
    - FLAC: fLaC (0x66 0x4C 0x61 0x43)
    - HLS playlist: #EXTM3U
    - RIFF: WAV, AVI
    """
    if not data or len(data) < 3:
        return False

    # ID3-тег (MP3 с метаданными)
    if data[:3] == b"ID3":
        return True

    # MPEG audio sync word: 0xFF + старшие 3 бита = 0b111
    if data[0] == 0xFF and len(data) >= 2 and (data[1] & 0xE0) == 0xE0:
        return True

    # MPEG-TS: sync byte 0x47 ('G') — одного достаточно
    if data[0] == 0x47:
        return True

    # AAC ADTS
    if data[0] == 0xFF and len(data) >= 2 and data[1] in (0xF1, 0xF9):
        return True

    # Ogg: OggS
    if data[:4] == b"OggS":
        return True

    # FLAC
    if data[:4] == b"fLaC":
        return True

    # HLS playlist
    if data[:7] == b"#EXTM3U":
        return True

    # RIFF (WAV, AVI)
    if data[:4] == b"RIFF":
        return True

    return False


def _validate_hls_content(data: bytes, log: logging.Logger) -> tuple[bool, Optional[str]]:
    """
    Разобрать HLS-плейлист и проверить наличие сегментов или вариантов.

    Возвращает (is_valid, error_message).
    """
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:
        return False, "Failed to decode playlist"

    if not text.strip().startswith("#EXTM3U"):
        return False, "Missing #EXTM3U header"

    lines = text.splitlines()
    has_segments = any(
        line.strip() and not line.startswith("#")
        for line in lines
    )
    has_extinf   = any(line.startswith("#EXTINF") for line in lines)
    has_stream_inf = any(line.startswith("#EXT-X-STREAM-INF") for line in lines)

    log.debug(
        f"   HLS: has_segments={has_segments}, "
        f"has_extinf={has_extinf}, has_stream_inf={has_stream_inf}"
    )

    if not has_segments:
        return False, "Playlist has no segment URLs"
    if not (has_extinf or has_stream_inf):
        return False, "Playlist has no #EXTINF or #EXT-X-STREAM-INF tags"

    return True, None


def fetch_url_text(url: str, timeout: int, log: logging.Logger) -> Optional[str]:
    log.info(f"⬇️  Fetching: {url}")
    t0 = time.time()
    try:
        req = _make_request(url, "GET")
        with urllib.request.urlopen(req, timeout=timeout * FETCH_TIMEOUT_MULT) as resp:
            elapsed = (time.time() - t0) * 1000
            code = resp.getcode()
            raw = resp.read()
            text = raw.decode("utf-8", errors="replace")
            log.info(
                f"   ✅ HTTP {code}  {len(raw):,} bytes  "
                f"{text.count(chr(10))} lines  {elapsed:.0f}ms"
            )
            return text
    except urllib.error.HTTPError as e:
        log.warning(f"   ❌ HTTP {e.code} — {url}")
    except urllib.error.URLError as e:
        log.warning(f"   ❌ URLError: {e.reason} — {url}")
    except TimeoutError:
        log.warning(f"   ⏱️  Timeout — {url}")
    except Exception as e:
        log.warning(f"   ⚠️  {type(e).__name__}: {e} — {url}")
    return None


def _head_precheck(ch: SourceChannel, timeout: int, t0: float,
                   log: logging.Logger) -> Optional[SourceChannel]:
    """HEAD-этап: возвращает ch, если вердикт окончательный, иначе None (→ GET)."""
    try:
        req = _make_request(ch.url, "HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = resp.getcode()
            ch.http_status = code
            mime = _parse_mime(resp.headers.get("Content-Type", ""))

            if code >= 400:
                ch.check_ms = (time.time() - t0) * 1000
                ch.reachable = False
                ch.check_error = f"HTTP {code}"
                log.debug(f"   ❌ [HEAD] HTTP {code}  {ch.check_ms:.0f}ms  {ch.name!r}")
                return ch

            # HTML = страница ошибки (геоблок, redirect на авторизацию и т.п.)
            if mime in _ERROR_CONTENT_TYPES:
                ch.check_ms = (time.time() - t0) * 1000
                ch.content_type = mime
                ch.reachable = False
                ch.check_error = "HTML response (geo-block or auth wall)"
                log.debug(f"   ❌ [HEAD] HTML  {ch.check_ms:.0f}ms  {ch.name!r}")
                return ch

            # Чёткий медиатип (не octet-stream) — ok без GET
            if mime in _STREAM_CONTENT_TYPES and mime != "application/octet-stream":
                ch.check_ms = (time.time() - t0) * 1000
                ch.content_type = mime
                ch.reachable = True
                log.debug(
                    f"   ✅ [HEAD] HTTP {code}  mime={mime}  "
                    f"{ch.check_ms:.0f}ms  {ch.name!r}"
                )
                return ch

            # octet-stream / пустой CT — нужен GET с байтами
            return None

    except urllib.error.HTTPError as e:
        ch.http_status = e.code
        if e.code == 405:
            log.debug(f"   HEAD→405, retry GET: {ch.url}")
            return None
        ch.check_ms = (time.time() - t0) * 1000
        ch.reachable = False
        ch.check_error = f"HTTP {e.code}"
        log.debug(f"   ❌ [HEAD] HTTP {e.code}  {ch.check_ms:.0f}ms  {ch.name!r}")
        return ch
    except Exception as e:
        # Сеть упала на HEAD — окончательный вердикт даст GET
        log.debug(f"   HEAD error ({type(e).__name__}), trying GET: {ch.url}")
        return None


def check_stream(ch: SourceChannel, timeout: int, log: logging.Logger,
                 strict: bool = False) -> SourceChannel:
    """
    Двухэтапная проверка URL на наличие реального медиапотока.

    Этап 1 — HEAD (быстро, без тела). Для .m3u8 пропускается: плейлист
    всё равно придётся скачивать GET-ом, HEAD был бы лишним запросом.

    Этап 2 — GET с чтением первых байт:
      - Статус >= 400 или text/html → мёртв.
      - .m3u8 / *mpegurl → HLS-валидация: есть ли сегменты/варианты.
      - Иначе: первые 1024 байта + magic-сигнатура. Неопознанный формат:
        strict=False → консервативно ok, strict=True → мёртв.

    Классификация отказов (для статистики ✅/❌/⚠️):
      - net_error=False — сервер ответил, но потока нет: ссылка мертва (❌);
      - net_error=True  — сеть/таймаут: проверить не удалось, возможно жив (⚠️).
    """
    t0 = time.time()
    is_hls = ch.url.lower().split("?")[0].endswith(".m3u8")

    # ── Этап 1: HEAD (кроме HLS) ──────────────────────────────────
    if not is_hls:
        done = _head_precheck(ch, timeout, t0, log)
        if done is not None:
            return done

    # ── Этап 2: GET + байтовая валидация ──────────────────────────
    try:
        req = _make_request(ch.url, "GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = resp.getcode()
            ch.http_status = code
            mime = _parse_mime(resp.headers.get("Content-Type", ""))
            ch.content_type = mime
            ch.check_ms = (time.time() - t0) * 1000

            if code >= 400:
                ch.reachable = False
                ch.check_error = f"HTTP {code}"
                log.debug(f"   ❌ [GET] HTTP {code}  {ch.check_ms:.0f}ms  {ch.name!r}")
                return ch

            # HTML с кодом 200 = страница ошибки
            if mime in _ERROR_CONTENT_TYPES:
                ch.reachable = False
                ch.check_error = "HTML response (geo-block or auth wall)"
                log.debug(f"   ❌ [GET] HTML  {ch.check_ms:.0f}ms  {ch.name!r}")
                return ch

            # HLS: читаем плейлист целиком (до 16 KB) и проверяем структуру
            if is_hls or mime in ("application/vnd.apple.mpegurl", "application/x-mpegurl"):
                data = resp.read(16384)
                ch.check_ms = (time.time() - t0) * 1000
                valid, err = _validate_hls_content(data, log)
                ch.reachable = valid
                ch.stream_verified = valid
                if not valid:
                    ch.check_error = err
                status_icon = "✅" if valid else "❌"
                log.debug(
                    f"   {status_icon} [GET/HLS] HTTP {code}  "
                    f"{ch.check_ms:.0f}ms  {ch.name!r}"
                    + (f"  err={err}" if err else "")
                )
                return ch

            # Обычный поток: читаем первые 1024 байта и проверяем magic
            first_bytes = resp.read(1024)
            ch.check_ms = (time.time() - t0) * 1000
            ch.stream_verified = _is_stream_magic(first_bytes)

            if mime in _STREAM_CONTENT_TYPES and mime != "application/octet-stream":
                ch.reachable = True          # известный медиатип
            elif ch.stream_verified:
                ch.reachable = True          # magic подтвердил формат
            elif strict:
                ch.reachable = False
                ch.check_error = "Format unrecognized (strict)"
            else:
                ch.reachable = True          # консервативно ok
                ch.check_error = "Format unrecognized (conservative ok)"

            verified_tag = " 🎵" if ch.stream_verified else " ?"
            status_icon  = "✅" if ch.reachable else "❌"
            log.debug(
                f"   {status_icon}{verified_tag} [GET] HTTP {code}  "
                f"mime={mime or '?'}  {ch.check_ms:.0f}ms  {ch.name!r}"
            )
            return ch

    except urllib.error.HTTPError as e:
        ch.http_status = e.code
        ch.reachable = False
        ch.check_error = f"HTTP {e.code}"
        log.debug(f"   ❌ [GET] HTTP {e.code}  {ch.name!r}")
    except urllib.error.URLError as e:
        ch.reachable = False
        ch.net_error = True
        ch.check_error = str(e.reason)
        log.debug(f"   🔌 URLError: {e.reason}  {ch.name!r}")
    except TimeoutError:
        ch.reachable = False
        ch.net_error = True
        ch.check_error = "timeout"
        log.debug(f"   ⏱️  Timeout  {ch.name!r}")
    except Exception as e:
        ch.reachable = False
        ch.net_error = True
        ch.check_error = str(e)
        log.debug(f"   ⚠️  {type(e).__name__}: {e}  {ch.name!r}")

    ch.check_ms = (time.time() - t0) * 1000
    return ch


# ──────────────────────────────────────────────────────────
#  Source M3U parsing
# ──────────────────────────────────────────────────────────

def parse_source_m3u(content: str, source_url: str, log: logging.Logger) -> list[SourceChannel]:
    channels: list[SourceChannel] = []
    lines = content.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.upper().startswith("#EXTINF"):
            extinf = line
            name   = _clean_name(_parse_extinf_name(line))
            j = i + 1
            while j < len(lines):
                nxt = lines[j].strip()
                if nxt and not nxt.startswith("#"):
                    break
                j += 1
            if j < len(lines):
                url = lines[j].strip()
                if url.startswith(("http://", "https://", "rtmp")):
                    channels.append(SourceChannel(
                        extinf_line=extinf,
                        url=url,
                        name=name,
                        tvg_id=extract_tvg_id(extinf),
                        source=source_url,
                    ))
                    log.debug(f"   Parsed: {name!r}")
                    i = j + 1
                    continue
        i += 1
    return channels


# ──────────────────────────────────────────────────────────
#  Core matching logic
# ──────────────────────────────────────────────────────────

def build_block_index(
    blocks: list[IndexBlock],
    log: Optional[logging.Logger] = None,
) -> tuple[dict[str, IndexBlock], dict[str, IndexBlock]]:
    """Индексы для матчинга: lower(имя)→блок и lower(tvg-id)→блок.

    При дублях выигрывает ПЕРВЫЙ блок в порядке передачи. Вызывающий подаёт
    блоки index.m3u первыми, затем extra-плейлисты — то есть при коллизии имени
    между файлами приоритет у index.m3u, потом по порядку EXTRA_PLAYLISTS.
    Коллизии между разными файлами логируются (debug), чтобы был след, что
    свежая ссылка уедет в первый файл, а не во второй.
    """
    by_name: dict[str, IndexBlock] = {}
    by_id:   dict[str, IndexBlock] = {}
    collisions = 0
    for blk in blocks:
        key = blk.name.strip().lower()
        if key:
            owner = by_name.get(key)
            if owner is None:
                by_name[key] = blk
            elif owner.origin != blk.origin:
                collisions += 1
                if log:
                    log.debug(f"   ⚠️  name collision {blk.name!r}: "
                              f"{owner.origin} (kept) vs {blk.origin} (ignored)")
        if blk.tvg_id:
            by_id.setdefault(blk.tvg_id.strip().lower(), blk)
    if log and collisions:
        log.info(f"   ℹ️  {collisions} cross-file name collision(s) — "
                 f"свежая ссылка уедет в приоритетный файл (index → extra по порядку)")
    return by_name, by_id


def find_matching_block(
    src_ch: SourceChannel,
    by_name: dict[str, IndexBlock],
    by_id: dict[str, IndexBlock],
    log: logging.Logger,
) -> Optional[IndexBlock]:
    """Матч по имени (приоритет), затем по tvg-id. O(1) вместо перебора блоков."""
    blk = by_name.get(src_ch.name.strip().lower())
    if blk is not None:
        log.debug(f"   MATCH [name={src_ch.name!r}] ↔ idx={blk.name!r}")
        return blk
    if src_ch.tvg_id:
        blk = by_id.get(src_ch.tvg_id.strip().lower())
        if blk is not None:
            log.debug(f"   MATCH [tvg-id={src_ch.tvg_id!r}] ↔ idx={blk.name!r}")
            return blk
    return None

def _block_has_active_url(blk: IndexBlock) -> bool:
    """Return True if the block already has at least one active (non-commented) URL."""
    for line in blk.lines:
        stripped = line.strip()
        if not stripped.startswith("#") and stripped.startswith(("http://", "https://", "rtmp")):
            return True
    return False


def insert_url_into_block(blk: IndexBlock, url: str, log: logging.Logger) -> bool:
    """
    Insert a URL line into the block after the last existing URL line.
    - If the block has no active URL yet → insert as active (no #)
    - If the block already has an active URL → insert as commented alternative (#url)
    Returns True if inserted, False if already present.
    """
    if url in blk.urls:
        log.debug(f"   Already in block {blk.name!r}: {url}")
        return False

    has_active = _block_has_active_url(blk)
    new_line = f"{url}\n" if not has_active else f"#{url}\n"
    role = "primary" if not has_active else "alternative"

    last_url_idx = -1
    for i, line in enumerate(blk.lines):
        stripped = line.strip().lstrip("#").strip()
        if stripped.startswith(("http://", "https://", "rtmp")):
            last_url_idx = i

    if last_url_idx >= 0:
        blk.lines.insert(last_url_idx + 1, new_line)
    else:
        blk.lines.append(new_line)

    blk.urls.add(url)
    log.debug(f"   Inserted [{role}] into {blk.name!r}: {url}")
    return True


def collect_all_file_urls(path: str) -> set[str]:
    """Return a set of all URLs (active and commented) already present in a file."""
    urls: set[str] = set()
    if not os.path.exists(path):
        return urls
    with open(path, encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            candidate = stripped.lstrip("#").strip()
            if candidate.startswith(("http://", "https://", "rtmp")):
                urls.add(candidate)
    return urls


def _channel_group_key(ch: SourceChannel) -> str:
    """Key for grouping duplicate channels from different sources."""
    if ch.tvg_id:
        return f"id:{ch.tvg_id.lower()}"
    return f"name:{ch.name.strip().lower()}"


def append_test_group(
    path: str,
    pairs: list[tuple[SourceChannel, Optional[IndexBlock]]],
    log: logging.Logger,
    existing_urls: Optional[set[str]] = None,
    dry_run: bool = False,
) -> int:
    """
    Append all reachable source channels to the end of path as a 'test' group.
    Channels from multiple sources with the same name/tvg-id are grouped:
      - first URL  → active (no #)
      - the rest   → commented alternatives (#url)
    Skips URLs already present. `existing_urls` — заранее собранный union URL'ов
    по ВСЕМ обслуживаемым файлам (чтобы не свалить в test ссылку, уже лежащую в
    sport.m3u/music.m3u/…); он объединяется с URL'ами самого path на диске.
    Returns count of new URL lines written.
    """
    existing_urls = set(existing_urls or set()) | collect_all_file_urls(path)
    log.info(f"   URLs already known (all files): {len(existing_urls)}")

    # Filter out already-present URLs
    new_pairs = [(ch, blk) for ch, blk in pairs if ch.url not in existing_urls]
    skipped = len(pairs) - len(new_pairs)
    log.info(f"   New URLs to append  : {len(new_pairs)}  (skipped duplicates: {skipped})")

    if not new_pairs:
        log.info("   Nothing new to write.")
        return 0

    # Group by channel identity; preserve insertion order
    groups: OrderedDict[str, list[tuple[SourceChannel, Optional[IndexBlock]]]] = OrderedDict()
    for ch, blk in new_pairs:
        key = _channel_group_key(ch)
        groups.setdefault(key, []).append((ch, blk))

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total_urls = len(new_pairs)
    lines: list[str] = [
        f"\n\n# ── test ──────────────────────────────────────────\n",
        f"# Added: {ts}  ({total_urls} URLs in {len(groups)} channel group(s))\n",
        f"# ───────────────────────────────────────────────────\n",
    ]

    for key, group_pairs in groups.items():
        # Use the EXTINF from the first entry, set group-title="test"
        first_ch, first_blk = group_pairs[0]
        extinf = re.sub(r'group-title="[^"]*"', 'group-title="test"', first_ch.extinf_line)
        if 'group-title=' not in extinf:
            extinf = re.sub(r'(#EXTINF:[^,]+)', r'\1 group-title="test"', extinf)

        lines.append(f"{extinf}\n")

        for i, (ch, blk) in enumerate(group_pairs):
            is_primary = (i == 0)
            url_line = f"{ch.url}\n" if is_primary else f"#{ch.url}\n"
            lines.append(url_line)
            role = "primary" if is_primary else "alt"
            matched_info = f" [→ {blk.name!r}]" if blk else ""
            log.info(
                f"   [{role}] {ch.name!r}{matched_info}  "
                f"[{ch.http_status}, {ch.check_ms:.0f}ms]  {url_line.strip()}"
            )

    content = "".join(lines)

    if dry_run:
        log.info("[DRY RUN] Would append:")
        for line in content.splitlines():
            log.info(f"   {line}")
        return total_urls

    with open(path, "a", encoding="utf-8") as f:
        f.write(content)
    log.info(f"💾 Appended {total_urls} URL(s) in {len(groups)} group(s) → {path}")
    return total_urls


def check_all_streams(
    channels: list[SourceChannel],
    workers: int,
    timeout: int,
    strict: bool,
    log: logging.Logger,
    stats: Stats,
) -> dict[str, SourceChannel]:
    """Параллельная проверка потоков; возвращает url → проверенный канал."""
    done_count, total = 0, len(channels)
    checked_map: dict[str, SourceChannel] = {}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(check_stream, ch, timeout, log, strict): ch
                   for ch in channels}
        # as_completed отдаёт результаты в главном потоке — блокировки не нужны.
        for future in as_completed(futures):
            ch = future.result()
            checked_map[ch.url] = ch

            done_count += 1
            if ch.reachable:
                stats.reachable += 1
            elif ch.net_error:
                stats.net_fail += 1
            else:
                stats.dead += 1

            if done_count % 25 == 0 or done_count == total:
                pct = done_count / total * 100
                log.info(
                    f"   {done_count}/{total} ({pct:.0f}%)  "
                    f"✅ {stats.reachable}  ❌ {stats.dead}  ⚠️  {stats.net_fail}"
                )
    return checked_map


# ──────────────────────────────────────────────────────────
#  Main
# ──────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Update index.m3u: find matching channels in sources, check streams, insert working URLs."
    )
    parser.add_argument(
        "--index", default=DEFAULT_INDEX_FILE, metavar="FILE",
        help=f"Local index M3U file to update (default: {DEFAULT_INDEX_FILE})",
    )
    parser.add_argument(
        "--no-extra", action="store_true",
        help="Обслуживать только --index, не трогать дополнительные плейлисты "
             f"({', '.join(EXTRA_PLAYLISTS.values())}).",
    )
    parser.add_argument(
        "--timeout", type=int, default=DEFAULT_TIMEOUT_SEC, metavar="SEC",
        help=f"Per-stream HTTP timeout in seconds (default: {DEFAULT_TIMEOUT_SEC})",
    )
    parser.add_argument(
        "--workers", type=int, default=DEFAULT_WORKERS, metavar="N",
        help=f"Parallel stream-check workers (default: {DEFAULT_WORKERS})",
    )
    parser.add_argument(
        "--sources", nargs="*", default=None, metavar="URL",
        help="Override source playlist URLs",
    )
    parser.add_argument(
        "--log", default=LOG_FILE, metavar="FILE",
        help=f"Log file (default: {LOG_FILE}). Pass 'none' to disable.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Do everything but don't write the output file.",
    )
    parser.add_argument(
        "--strict", action="store_true",
        help="Считать потоки с неопознанным форматом мёртвыми (по умолчанию — живыми).",
    )
    args = parser.parse_args()

    log_file = None if str(args.log).lower() == "none" else args.log
    log = setup_logging(log_file)
    stats = Stats()

    cfg = load_config(log)
    sources = args.sources or cfg.sources
    if not sources:
        log.error("❌ Нет источников: заполни sources.txt или передай --sources. Exiting.")
        sys.exit(1)

    # Список обслуживаемых файлов: index.m3u первым (приоритет при матчинге,
    # хозяин test-дампа), затем EXTRA_PLAYLISTS в объявленном порядке.
    extra_specs = [] if args.no_extra else list(EXTRA_PLAYLISTS.items())

    log.info("=" * 60)
    log.info("🚀 M3U Index Updater started")
    log.info(f"   Python   : {sys.version.split()[0]}")
    log.info(f"   Index    : {args.index}")
    if extra_specs:
        log.info(f"   Extra    : {', '.join(f for _, f in extra_specs)}")
    else:
        log.info("   Extra    : (none — --no-extra)")
    log.info(f"   Sources  : {len(sources)}")
    log.info(f"   Timeout  : {args.timeout}s  |  Workers: {args.workers}")
    if args.dry_run:
        log.info("   DRY RUN  : files will NOT be modified")
    log.info("=" * 60)

    # ── Step 1: Parse index.m3u + extra playlists ───────────────────────────
    log.info("")
    log.info("STEP 1 — Reading playlists")
    log.info("-" * 60)

    playlists: list[Playlist] = []

    idx_header, idx_blocks = parse_index_m3u(args.index, cfg, log, origin=os.path.basename(args.index))
    playlists.append(Playlist(path=args.index, label="index",
                              header=idx_header, blocks=idx_blocks, is_index=True))

    for label, fname in extra_specs:
        # extra-файлы ищем рядом с --index (os.path.join("", x) == "x").
        fpath = os.path.join(os.path.dirname(args.index), fname)
        if not os.path.exists(fpath):
            log.warning(f"⚠️  Extra playlist {fname!r} не найден — пропускаю "
                        f"(будет обслуживаться, когда появится)")
            continue
        hdr, blks = parse_index_m3u(fpath, cfg, log, origin=fname)
        playlists.append(Playlist(path=fpath, label=label, header=hdr, blocks=blks))

    # Плоский список всех блоков; index первым → приоритет при коллизиях имён.
    all_blocks: list[IndexBlock] = [blk for pl in playlists for blk in pl.blocks]

    if not all_blocks:
        log.error("❌ Плейлисты не содержат каналов. Матчить не с чем. Exiting.")
        sys.exit(1)

    log.info(f"   Channels per file:")
    for pl in playlists:
        log.info(f"   • {os.path.basename(pl.path):<16} {len(pl.blocks):>5} channel(s)"
                 f"  [{pl.label}]")
    log.info(f"   Total channels across all files: {len(all_blocks)}")

    validate_config(cfg, all_blocks, log)

    # ── Step 2: Fetch source playlists ───────────────────────────────────────
    log.info("")
    log.info("STEP 2 — Fetching source playlists")
    log.info("-" * 60)

    all_source_channels: list[SourceChannel] = []

    for url in sources:
        content = fetch_url_text(url, args.timeout, log)
        if content is None:
            stats.sources_fail += 1
            continue
        stats.sources_ok += 1
        found = parse_source_m3u(content, url, log)

        # Порядок единый: сначала алиас (каноническое имя), затем блоклисты.
        # Иначе «Салям УФА» проходил фильтр по исходному имени, переименовывался
        # в заблокированный «Салям» и попадал в 5a, но отсекался в 5b.
        for ch in found:
            canonical = cfg.aliases.get(ch.name.strip().lower())
            if canonical:
                log.debug(f"   ALIAS: {ch.name!r} → {canonical!r}")
                ch.name = canonical

        filtered: list[SourceChannel] = []
        for ch in found:
            if ch.name.strip().lower() in cfg.name_blocklist:
                log.debug(f"   BLOCKED (name): {ch.name!r}")
            elif cfg.url_blocked(ch.url):
                log.debug(f"   BLOCKED (url): {ch.name!r}  {ch.url!r}")
            elif any(p.search(ch.name) for p in BLOCKLIST_PATTERNS):
                log.debug(f"   BLOCKED (pattern): {ch.name!r}")
            else:
                filtered.append(ch)
        log.info(f"   → Parsed {len(found)} channel(s)  (blocked: {len(found) - len(filtered)})")
        all_source_channels.extend(filtered)

    stats.parsed = len(all_source_channels)
    log.info("")
    log.info(f"📊 Sources: {stats.sources_ok} ok / {stats.sources_fail} failed")
    log.info(f"📊 Total source channels: {stats.parsed}")

    if not all_source_channels:
        log.error("❌ No channels found in any source. Exiting.")
        sys.exit(1)

    # ── Step 3: Match source channels to existing blocks (all files) ────────
    log.info("")
    log.info("STEP 3 — Matching source channels to existing blocks (index + extra)")
    log.info("-" * 60)

    by_name, by_id = build_block_index(all_blocks, log)

    # Pairs (source_channel, index_block) where URL is new and block matches
    update_candidates: list[tuple[SourceChannel, IndexBlock]] = []

    for src_ch in all_source_channels:
        blk = find_matching_block(src_ch, by_name, by_id, log)
        if blk is None:
            log.debug(f"   No match: {src_ch.name!r}")
            continue
        if src_ch.url in blk.urls:
            log.debug(f"   Already in block: {src_ch.url}  ({blk.name!r})")
            continue
        update_candidates.append((src_ch, blk))

    stats.candidates = len(update_candidates)
    log.info(f"   Matched (new URLs)  : {stats.candidates}")
    log.info(f"   Total source ch.    : {stats.parsed}")

    # ── Step 4: Check reachability of ALL source channels ───────────────────
    log.info("")
    log.info(f"STEP 4 — Checking ALL {stats.parsed} source stream URLs")
    log.info(f"         (workers={args.workers}, timeout={args.timeout}s)")
    log.info("-" * 60)

    # We need to check: all source channels (for test group) +
    # de-duplicate with update_candidates (already in the list)
    # Build a flat list: every unique source channel once
    seen_urls: set[str] = set()
    all_to_check: list[SourceChannel] = []
    for ch in all_source_channels:
        if ch.url not in seen_urls:
            seen_urls.add(ch.url)
            all_to_check.append(ch)

    log.info("   ✅ живой   ❌ мёртвый (сервер отказал)   ⚠️ сеть/таймаут — не проверено")
    checked_map = check_all_streams(
        all_to_check, args.workers, args.timeout, args.strict, log, stats
    )

    # ── Step 5a: Insert new URLs into matching existing blocks ───────────────
    log.info("")
    log.info("STEP 5a — Updating existing channel blocks with new URLs (all files)")
    log.info("-" * 60)

    inserted_by_file: Counter = Counter()
    for src_ch, blk in update_candidates:
        ch = checked_map.get(src_ch.url, src_ch)
        if not ch.reachable:
            log.debug(f"   Skip (unreachable): {ch.url}")
            continue
        inserted = insert_url_into_block(blk, ch.url, log)
        if inserted:
            stats.inserted += 1
            inserted_by_file[blk.origin] += 1
            log.info(
                f"   ✅ [{blk.origin}] {blk.name!r}  ←  #{ch.url}"
                f"  [{ch.http_status}, {ch.check_ms:.0f}ms]"
            )

    # Всегда переписываем КАЖДЫЙ файл: даже без вставок парсер мог вычистить
    # заблокированные URL — держим все плейлисты в каноничном виде.
    for pl in playlists:
        write_index_m3u(pl.path, pl.header, pl.blocks, log, dry_run=args.dry_run)

    # ── Step 5b: Append ALL reachable source channels to test group ──────────
    log.info("")
    log.info("STEP 5b — Appending ALL reachable source channels to 'test' group (index.m3u)")
    log.info("-" * 60)

    # Build list of all reachable channels (with their matched block or None)
    block_by_url: dict[str, IndexBlock] = {
        src_ch.url: blk for src_ch, blk in update_candidates
    }

    all_reachable_pairs: list[tuple[SourceChannel, Optional[IndexBlock]]] = [
        (ch, block_by_url.get(ch.url)) for ch in checked_map.values()
        if ch.reachable and ch.name.strip().lower() not in cfg.name_blocklist
   ]

    # Дедуп против URL'ов ВСЕХ обслуживаемых файлов (in-memory, уже с учётом
    # вставок 5a и хвоста test), чтобы не свалить в test ссылку, которая уже
    # лежит в sport.m3u/music.m3u/foreign.m3u/children.m3u/tv_series.m3u.
    known_urls: set[str] = set()
    for pl in playlists:
        for blk in pl.blocks:
            known_urls |= blk.urls

    index_pl = next(pl for pl in playlists if pl.is_index)
    stats.appended = append_test_group(
        index_pl.path, all_reachable_pairs, log,
        existing_urls=known_urls, dry_run=args.dry_run,
    )

    # ── Summary ──────────────────────────────────────────────────────────────
    log.info("")
    log.info("=" * 60)
    log.info("SUMMARY")
    log.info("=" * 60)
    log.info(f"  Sources fetched         : {stats.sources_ok}  (failed: {stats.sources_fail})")
    log.info(f"  Source channels (total) : {stats.parsed}  (unique: {len(all_to_check)})")
    log.info(f"  ✅ Reachable            : {stats.reachable}")
    log.info(f"  ❌ Dead (server said no): {stats.dead}")
    log.info(f"  ⚠️  Network fail         : {stats.net_fail}  (сеть/таймаут — не проверено)")
    log.info(f"  🔗 Matched existing     : {stats.candidates}  → inserted: {stats.inserted}")
    if stats.inserted:
        for pl in playlists:
            n = inserted_by_file.get(os.path.basename(pl.path), 0)
            if n:
                log.info(f"       ↳ {os.path.basename(pl.path):<16} +{n}")
    log.info(f"  🧪 Appended to test     : {stats.appended} URL(s)  (→ {os.path.basename(index_pl.path)})")
    log.info(f"  📄 Files serviced       : {', '.join(os.path.basename(pl.path) for pl in playlists)}")
    log.info(f"  ⏱️  Total time           : {stats.elapsed}")
    log.info("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n⛔ Interrupted by user.", file=sys.stderr)
        sys.exit(1)
