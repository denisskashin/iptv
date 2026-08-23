#!/usr/bin/env python3
"""
M3U Index Updater
==================
Обслуживает НЕСКОЛЬКО плейлистов сразу: index.m3u и дополнительные тематические
файлы (sport.m3u, music.m3u, foreign.m3u, children.m3u, tv_series.m3u,
discovery.m3u, hobby.m3u — см.
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
    url_blocklist.txt  — блоклист URL (подстроки; '*' — wildcard).
                         В конце файла — авто-секция (AUTO_DEAD_MARKER): туда
                         чекер сам дописывает безнадёжно мёртвые ссылки, чтобы
                         на следующем прогоне не тратить на них проверку.
                         Там точное совпадение по всей строке, а не подстрока.

Requirements: Python 3.8+  —  no third-party libraries.
Опционально: ffprobe (из ffmpeg) — нужен для проверки rtmp/rtsp-ссылок.

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
import errno
import ipaddress
import logging
import logging.handlers
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
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

# Маркер авто-секции в url_blocklist.txt. Всё, что ниже него, дописано
# чекером: полные URL (точное совпадение) и свёрнутые хосты (подстрока).
# Руками там править не нужно, можно смело удалить весь блок целиком.
#
# СТРОКУ НЕ МЕНЯТЬ: по ней распознаётся начало авто-секции в уже существующих
# файлах. Поменяешь текст — старая секция уедет в «ручные» паттерны, и каждый
# из тысяч полных URL начнёт работать как подстрока (и медленно, и неверно).
AUTO_DEAD_MARKER = "# === auto-dead: добавлено m3u_checker.py (точное совпадение URL) ==="
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
    url_block_exact: set[str] = field(default_factory=set)   # авто-секция: полные URL

    def url_blocked(self, url: str) -> bool:
        """True, если URL попадает под url_blocklist.txt (подстрока или wildcard)."""
        url_lc = url.lower()
        # Авто-секция — это тысячи полных URL; сравниваем через set (O(1)),
        # иначе подстрочный перебор превращает проверку в квадрат.
        if url_lc in self.url_block_exact:
            return True
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

    url_patterns, auto_dead = _read_url_blocklist(log)
    cfg.url_block_plain    = {p.lower() for p in url_patterns if "*" not in p}
    cfg.url_block_wildcard = [_wildcard_to_regex(p.lower()) for p in url_patterns if "*" in p]
    cfg.url_block_exact    = auto_dead

    log.info(f"⚙️  Config: {len(cfg.sources)} source(s), "
             f"{len(cfg.name_blocklist)} blocked name(s), {len(cfg.aliases)} alias(es), "
             f"{len(url_patterns)} url pattern(s), {len(auto_dead)} auto-dead URL(s)")
    return cfg


def _read_url_blocklist(log: logging.Logger) -> tuple[list[str], set[str]]:
    """
    Читает url_blocklist.txt, разделяя его на две части по AUTO_DEAD_MARKER.

    До маркера — ручные паттерны (подстрока / wildcard), семантика прежняя.
    После маркера — авто-добавленные полные URL: точное совпадение и set-lookup.
    Разделение принципиально: ручные записи вроде 'line.iptvhunt.com' должны
    ловить любой URL этого хоста, а авто-запись обязана бить ровно по себе,
    чтобы мёртвая ссылка не утащила с собой соседние живые.
    """
    path = URL_BLOCKLIST_FILE
    if not os.path.exists(path):
        log.warning(f"⚙️  url blocklist: файл {path!r} не найден — использую пустой список")
        return [], set()

    manual: list[str] = []
    auto: set[str] = set()
    in_auto = False
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line == AUTO_DEAD_MARKER:
                in_auto = True
                continue
            if not line or line.startswith("#"):
                continue
            # В авто-секции лежат два вида записей: полные URL (точное совпадение)
            # и свёрнутые хосты (подстрока, как ручные паттерны).
            if in_auto and line.startswith(("http://", "https://", "rtmp")):
                auto.add(line.lower())
            else:
                manual.append(line)
    return manual, auto


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
    ("Кино",           "cinema.m3u"),
    ("Спорт",          "sport.m3u"),
    ("Музыка",         "music.m3u"),
    ("Зарубежные",     "foreign.m3u"),
    ("Детские",        "children.m3u"),
    ("ТВ Сериалы",     "tv_series.m3u"),
    ("Познавательные", "discovery.m3u"),
    ("Хобби",          "hobby.m3u"),
])

LOG_FILE            = "m3u_checker.log"
DEFAULT_TIMEOUT_SEC = 8
DEFAULT_WORKERS     = 30
FETCH_TIMEOUT_MULT  = 3               # таймаут скачивания источника = timeout * MULT

DEFAULT_RETRIES       = 1     # доп. попыток при мягком отказе (таймаут/обрыв)
RETRY_TIMEOUT_MULT    = 2.0   # на повторе таймаут увеличивается во столько раз
RETRY_BACKOFF_SEC     = 1.0   # пауза перед повтором
DEFAULT_PER_HOST      = 4     # макс. одновременных запросов к одному хосту
DEFAULT_AUTOBLOCK_HOST_MIN = 5  # с этого числа мёртвых ссылок пишем хост целиком
ANOMALY_SHARE     = 0.30      # доля одинаковых отказов, после которой прогон подозрителен
ANOMALY_MIN_COUNT = 50        # ...и при этом их не меньше этого числа
ANOMALY_MIN_HOSTS = 20        # ...и разброс по хостам не меньше этого
FFPROBE_TIMEOUT_MULT  = 2     # таймаут ffprobe = timeout * MULT

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
    retryable: bool = False               # имеет ли смысл повторить попытку


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
    net_fail_reasons: Counter = field(default_factory=Counter)
    autoblocked:  int = 0   # мёртвых URL дописано в url_blocklist.txt
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

    # Атомарная запись (tmp-файл → os.replace),
    # чтобы краш посреди записи не оставил битый/пустой index.m3u.
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
#  Классификация сетевых отказов  (❌ мёртв  vs  ⚠️ не проверено)
# ──────────────────────────────────────────────────────────
#
# Принцип: ⚠️ — только те отказы, где повторная попытка реально может
# дать другой результат. Всё, что детерминировано (порт закрыт, домена
# не существует, URL синтаксически битый), — это ❌, а не «не проверено».

# errno, при которых стек сказал окончательное «нет».
_FATAL_ERRNOS = frozenset({
    errno.ECONNREFUSED,   # 61 — порт закрыт, сервер не слушает
    errno.EHOSTUNREACH,   # 65 — хост недостижим
    errno.ENETUNREACH,    # 51 — сети до хоста нет
    errno.EADDRNOTAVAIL,  # 49
})

# NXDOMAIN: домена не существует. Отличается от EAI_AGAIN («temporary
# failure in name resolution»), который как раз временный → ⚠️.
_DNS_DEAD_MARKERS = (
    "nodename nor servname provided",   # macOS EAI_NONAME
    "name or service not known",        # Linux EAI_NONAME
    "no address associated with hostname",
    "getaddrinfo failed",
)
_DNS_SOFT_MARKERS = (
    "temporary failure in name resolution",
    "try again",
)

# Мягкие отказы: имеет смысл повторить.
_SOFT_MARKERS = (
    "timed out", "timeout",
    "connection reset",
    "remotedisconnected", "remote end closed",
    "broken pipe",
    "incompleteread",
)


def classify_error(exc: BaseException) -> tuple[bool, bool, str]:
    """
    Разбирает сетевое исключение.

    Возвращает (net_error, retryable, reason):
      net_error=False → вердикт окончательный, ссылка мёртвая (❌);
      net_error=True  → проверить не удалось (⚠️);
      retryable=True  → есть смысл повторить попытку.
    """
    reason = getattr(exc, "reason", exc)
    text = str(reason).lower()

    # SSL: сервер жив и отвечает, проблема только в сертификате.
    # Повтор без верификации делает check_stream. Проверяем ДО ValueError:
    # SSLCertVerificationError — подкласс и SSLError, и ValueError.
    if isinstance(exc, ssl.SSLError) or isinstance(reason, ssl.SSLError) \
            or "ssl" in text or "certificate verify" in text:
        return True, True, f"SSL: {reason}"

    # Битый URL / неподдерживаемая схема — чинить нечего, это ❌.
    if "unknown url type" in text:
        return False, False, str(reason)
    if isinstance(exc, (UnicodeError, ValueError)) or \
            type(exc).__name__ == "InvalidURL":
        return False, False, f"invalid URL: {reason}"

    if any(m in text for m in _DNS_SOFT_MARKERS):
        return True, True, f"DNS temporary: {reason}"
    if any(m in text for m in _DNS_DEAD_MARKERS):
        return False, False, "DNS: host does not exist"

    err_no = getattr(reason, "errno", None)
    if err_no in _FATAL_ERRNOS:
        return False, False, f"{os.strerror(err_no)} (errno {err_no})"

    if isinstance(exc, (TimeoutError, socket.timeout)) or \
            any(m in text for m in _SOFT_MARKERS) or \
            type(exc).__name__ in ("RemoteDisconnected", "IncompleteRead"):
        return True, True, str(reason) or type(exc).__name__

    # Неизвестное — консервативно ⚠️ с одним повтором.
    return True, True, f"{type(exc).__name__}: {reason}"


# Ответы посредника (CDN/прокси), а не самого сервера потока. Делятся надвое,
# и разница принципиальная.
#
# 520–527, 530 — семейство Cloudflare «origin недостижим» (530 = origin DNS
# error). На практике это протухший хост: бэкенд провайдера снесли, а ссылки
# в плейлистах остались. Считаем мёртвым и блокируем — решение владельца
# плейлиста. Вернуть в «не проверено» — перенести коды в _TRANSIENT_HTTP.
_CDN_ORIGIN_DOWN = frozenset({520, 521, 522, 523, 524, 525, 526, 527, 530})

# 502/503/504 — классика временного: сервер перезагружается, шлюз не дождался.
# Мёртвым считаем (потока нет), но в блоклист не пишем: завтра оживёт.
_TRANSIENT_HTTP = frozenset({502, 503, 504})


def http_verdict(code: int) -> tuple[bool, str]:
    """(net_error, reason) для HTTP-кода ответа."""
    if code in _CDN_ORIGIN_DOWN:
        return False, f"HTTP {code} (CDN: origin недостижим)"
    if code in _TRANSIENT_HTTP:
        return False, f"HTTP {code} (шлюз: сервер не ответил, обычно временно)"
    return False, f"HTTP {code}"


_INSECURE_SSL_CTX = ssl.create_default_context()
_INSECURE_SSL_CTX.check_hostname = False
_INSECURE_SSL_CTX.verify_mode = ssl.CERT_NONE


def _host_of(url: str) -> str:
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except Exception:
        return ""


def _endpoint_of(url: str) -> str:
    """host:port — именно эта пара определяет «порт закрыт», а не хост целиком."""
    try:
        p = urllib.parse.urlsplit(url)
        host = (p.hostname or "").lower()
        port = p.port or (443 if p.scheme == "https" else 80)
        return f"{host}:{port}"
    except Exception:
        return _host_of(url)


class HostGate:
    """
    Ограничитель нагрузки на хост + кэш заведомо мёртвых адресов.

    Зачем: 30 воркеров, долбящих один сервер, сами провоцируют таймауты и
    отказы — часть ⚠️ раньше была самострелом. Плюс если домен не резолвится
    или порт закрыт, незачем ждать таймаут для каждой из его сотен ссылок.

    Два уровня кэша, и путать их нельзя:
      • host      — домен не резолвится / хост недостижим → мертво всё;
      • host:port — конкретный порт закрыт; другие порты того же хоста
                    при этом могут прекрасно работать.
    """

    def __init__(self, per_host: int):
        self.per_host = max(1, per_host)
        self._sems: dict[str, threading.Semaphore] = {}
        self._dead_hosts: dict[str, str] = {}       # host      → причина
        self._dead_endpoints: dict[str, str] = {}   # host:port → причина
        self._lock = threading.Lock()

    def sem(self, host: str) -> threading.Semaphore:
        with self._lock:
            s = self._sems.get(host)
            if s is None:
                s = self._sems[host] = threading.Semaphore(self.per_host)
            return s

    def dead_reason(self, url: str) -> Optional[str]:
        host, endpoint = _host_of(url), _endpoint_of(url)
        with self._lock:
            return self._dead_hosts.get(host) or self._dead_endpoints.get(endpoint)

    def mark_dead(self, url: str, reason: str, host_wide: bool) -> None:
        key = _host_of(url) if host_wide else _endpoint_of(url)
        if not key or key.startswith(":"):
            return
        with self._lock:
            target = self._dead_hosts if host_wide else self._dead_endpoints
            target.setdefault(key, reason)

    @property
    def dead_hosts(self) -> dict[str, str]:
        with self._lock:
            return {**self._dead_hosts, **self._dead_endpoints}


def _ffprobe_ok(url: str, timeout: int, log: logging.Logger) -> Optional[bool]:
    """
    Проверка потока через ffprobe. Нужна для схем, которые urllib не умеет
    вовсе (rtmp/rtsp) — раньше они падали в ⚠️ как «unknown url type».
    None — ffprobe недоступен или сам упал по таймауту.
    """
    if not _FFPROBE_BIN:
        return None
    cmd = [
        _FFPROBE_BIN, "-v", "error",
        "-rw_timeout", str(timeout * 1_000_000),
        "-show_entries", "stream=codec_type",
        "-of", "csv=p=0", url,
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True,
                             timeout=timeout * FFPROBE_TIMEOUT_MULT)
    except subprocess.TimeoutExpired:
        log.debug(f"   ⏱️  ffprobe timeout — {url}")
        return None
    except Exception as e:
        log.debug(f"   ffprobe failed to start: {e}")
        return None
    return res.returncode == 0 and bool(res.stdout.strip())


_FFPROBE_BIN = shutil.which("ffprobe")


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
                   log: logging.Logger, ssl_ctx=None) -> Optional[SourceChannel]:
    """HEAD-этап: возвращает ch, если вердикт окончательный, иначе None (→ GET)."""
    try:
        req = _make_request(ch.url, "HEAD")
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_ctx) as resp:
            code = resp.getcode()
            ch.http_status = code
            mime = _parse_mime(resp.headers.get("Content-Type", ""))

            if code >= 400:
                ch.check_ms = (time.time() - t0) * 1000
                ch.reachable = False
                ch.net_error, ch.check_error = http_verdict(code)
                icon = "⚠️ " if ch.net_error else "❌"
                log.debug(f"   {icon} [HEAD] HTTP {code}  {ch.check_ms:.0f}ms  {ch.name!r}")
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
        ch.net_error, ch.check_error = http_verdict(e.code)
        icon = "⚠️ " if ch.net_error else "❌"
        log.debug(f"   {icon} [HEAD] HTTP {e.code}  {ch.check_ms:.0f}ms  {ch.name!r}")
        return ch
    except Exception as e:
        # Фатальный отказ (порт закрыт, домена нет) — GET даст ровно то же,
        # только потратит ещё один таймаут. Отвечаем сразу.
        net_error, retryable, reason = classify_error(e)
        if not net_error and not retryable:
            ch.check_ms = (time.time() - t0) * 1000
            ch.reachable = False
            ch.check_error = reason
            log.debug(f"   ❌ [HEAD] {reason}  {ch.check_ms:.0f}ms  {ch.name!r}")
            return ch
        log.debug(f"   HEAD error ({type(e).__name__}), trying GET: {ch.url}")
        return None


def _check_stream_once(ch: SourceChannel, timeout: int, log: logging.Logger,
                       strict: bool = False, ssl_ctx=None) -> SourceChannel:
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
    ch.net_error = False
    ch.retryable = False
    ch.check_error = None
    is_hls = ch.url.lower().split("?")[0].endswith(".m3u8")

    # ── Этап 1: HEAD (кроме HLS) ──────────────────────────────────
    if not is_hls:
        done = _head_precheck(ch, timeout, t0, log, ssl_ctx)
        if done is not None:
            return done

    # ── Этап 2: GET + байтовая валидация ──────────────────────────
    try:
        req = _make_request(ch.url, "GET")
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_ctx) as resp:
            code = resp.getcode()
            ch.http_status = code
            mime = _parse_mime(resp.headers.get("Content-Type", ""))
            ch.content_type = mime
            ch.check_ms = (time.time() - t0) * 1000

            if code >= 400:
                ch.reachable = False
                ch.net_error, ch.check_error = http_verdict(code)
                icon = "⚠️ " if ch.net_error else "❌"
                log.debug(f"   {icon} [GET] HTTP {code}  {ch.check_ms:.0f}ms  {ch.name!r}")
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
        ch.net_error, ch.check_error = http_verdict(e.code)
        icon = "⚠️ " if ch.net_error else "❌"
        log.debug(f"   {icon} [GET] HTTP {e.code}  {ch.name!r}")
    except Exception as e:
        net_error, retryable, reason = classify_error(e)
        ch.reachable = False
        ch.net_error = net_error
        ch.retryable = retryable
        ch.check_error = reason
        icon = "⚠️ " if net_error else "❌"
        log.debug(f"   {icon} [GET] {reason}  {ch.name!r}")

    ch.check_ms = (time.time() - t0) * 1000
    return ch


def check_stream(ch: SourceChannel, timeout: int, log: logging.Logger,
                 strict: bool = False, gate: Optional["HostGate"] = None,
                 retries: int = DEFAULT_RETRIES) -> SourceChannel:
    """
    Обёртка над одной проверкой: кэш мёртвых хостов, лимит на хост,
    повтор при мягком отказе, фолбэк на ffprobe для rtmp/rtsp.
    """
    t0 = time.time()
    host = _host_of(ch.url)
    scheme = ch.url.split(":", 1)[0].lower()

    # rtmp/rtsp: urllib не поддерживает схему вовсе — раньше это давало
    # ложное ⚠️ «unknown url type». Пробуем ffprobe.
    if scheme in ("rtmp", "rtmps", "rtsp"):
        ok = _ffprobe_ok(ch.url, timeout, log)
        ch.check_ms = (time.time() - t0) * 1000
        if ok is None:
            ch.reachable, ch.net_error = False, True
            ch.check_error = f"{scheme}: ffprobe unavailable"
        else:
            ch.reachable, ch.net_error = ok, False
            ch.stream_verified = ok
            if not ok:
                ch.check_error = f"{scheme}: no stream"
        log.debug(f"   {'✅' if ch.reachable else '❌'} [ffprobe] "
                  f"{ch.check_ms:.0f}ms  {ch.name!r}")
        return ch

    # Адрес уже признан мёртвым другим потоком — не ждём таймаут заново.
    if gate is not None:
        reason = gate.dead_reason(ch.url)
        if reason:
            ch.reachable, ch.net_error = False, False
            ch.check_error = f"host dead: {reason}"
            ch.check_ms = 0.0
            log.debug(f"   ❌ [cached] {reason}  {ch.name!r}")
            return ch

    sem = gate.sem(host) if gate is not None else None
    attempt, cur_timeout, ssl_ctx = 0, timeout, None
    while True:
        if sem is not None:
            sem.acquire()
        try:
            ch = _check_stream_once(ch, cur_timeout, log, strict, ssl_ctx)
        finally:
            if sem is not None:
                sem.release()

        if ch.reachable:
            return ch

        err = (ch.check_error or "").lower()

        # Запоминаем недоступный адрес, чтобы не ждать таймаут для остальных
        # его ссылок. DNS/недостижимость — по хосту, отказ порта — по host:port.
        if gate is not None and not ch.net_error:
            if "dns: host does not exist" in err or "unreachable" in err \
                    or "no route to host" in err:
                gate.mark_dead(ch.url, ch.check_error, host_wide=True)
            elif "connection refused" in err:
                gate.mark_dead(ch.url, ch.check_error, host_wide=False)

        if not ch.net_error or not getattr(ch, "retryable", False):
            return ch
        if attempt >= retries:
            return ch

        # Плохой сертификат — не сетевая проблема: повторяем без верификации.
        if ssl_ctx is None and ("ssl" in err or "certificate" in err):
            ssl_ctx = _INSECURE_SSL_CTX          # бесплатно, не тратит попытку
            log.debug(f"   🔓 retry without cert verification  {ch.name!r}")
            continue

        attempt += 1
        cur_timeout = int(cur_timeout * RETRY_TIMEOUT_MULT) or cur_timeout
        time.sleep(RETRY_BACKOFF_SEC)
        log.debug(f"   🔁 retry {attempt}/{retries} "
                  f"(timeout={cur_timeout}s)  {ch.name!r}")


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
    per_host: int = DEFAULT_PER_HOST,
    retries: int = DEFAULT_RETRIES,
) -> dict[str, SourceChannel]:
    """Параллельная проверка потоков; возвращает url → проверенный канал."""
    done_count, total = 0, len(channels)
    checked_map: dict[str, SourceChannel] = {}
    gate = HostGate(per_host)
    net_reasons: Counter = Counter()

    # Ссылки одного хоста идут подряд → семафор упирается в лимит и воркеры
    # простаивают. Перемешиваем по хостам, чтобы нагрузка была равномерной.
    channels = _interleave_by_host(channels)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(check_stream, ch, timeout, log, strict,
                               gate, retries): ch
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
                net_reasons[_reason_bucket(ch.check_error)] += 1
            else:
                stats.dead += 1

            if done_count % 25 == 0 or done_count == total:
                pct = done_count / total * 100
                log.info(
                    f"   {done_count}/{total} ({pct:.0f}%)  "
                    f"✅ {stats.reachable}  ❌ {stats.dead}  ⚠️  {stats.net_fail}"
                )

    dead_hosts = gate.dead_hosts
    if dead_hosts:
        log.info(f"   🚫 Хостов признано мёртвыми целиком: {len(dead_hosts)}")
        for h, why in sorted(dead_hosts.items())[:15]:
            log.info(f"        {h} — {why}")
    if net_reasons:
        stats.net_fail_reasons = net_reasons
        log.info("   ⚠️  Причины «не проверено»:")
        for why, n in net_reasons.most_common():
            log.info(f"        {n:>5}  {why}")
    return checked_map


def _interleave_by_host(channels: list[SourceChannel]) -> list[SourceChannel]:
    """Round-robin по хостам: сначала по одной ссылке с каждого, потом по второй…"""
    buckets: "OrderedDict[str, list[SourceChannel]]" = OrderedDict()
    for ch in channels:
        buckets.setdefault(_host_of(ch.url), []).append(ch)
    out: list[SourceChannel] = []
    while buckets:
        for host in list(buckets):
            out.append(buckets[host].pop(0))
            if not buckets[host]:
                del buckets[host]
    return out


# HTTP-коды, после которых ссылку можно хоронить: сервер ответил и отказал
# окончательно. 403 здесь по решению владельца плейлиста — да, часть серверов
# отдаёт 403 на HEAD, а плееру поток отдаёт, но на практике таких ссылок
# всё равно не дождаться. Вернуть обратно в soft — убрать 403 из этого списка.
#
# 429, 500 и 502/503/504 НЕ здесь: перегрузка и падение сервера — временные.
# А вот 52x/530 здесь: у Cloudflare это «origin недостижим», и когда так
# отвечают тысячи ссылок сутками подряд — бэкенда провайдера больше нет.
_HARD_DEAD_HTTP = frozenset({
    400, 401, 402, 403, 404, 406, 410, 444, 451,
}) | _CDN_ORIGIN_DOWN

_HARD_DEAD_MARKERS = (
    "connection refused",
    "dns: host does not exist",
    "unreachable",
    "no route to host",
    "invalid url",
    "unknown url type",
    "html response",            # геоблок / страница авторизации вместо потока
    "missing #extm3u header",   # по ссылке лежит вообще не плейлист
    "failed to decode playlist",
)


def dead_kind(ch: SourceChannel) -> str:
    """
    Насколько уверенно ссылка мертва: 'hard' — навсегда, 'soft' — сервер
    ответил отказом, но это может измениться, '' — не мёртвая.

    В авто-блоклист по умолчанию идёт только 'hard': блоклист не просто
    пропускает ссылку при разборе источников, но и ВЫЧИЩАЕТ её из index.m3u
    на следующем прогоне. Ошибиться здесь = молча потерять рабочий канал.

    В soft остаётся то, что завтра может ожить само: 5xx (сервер лёг),
    429 (перегрузка), пустой HLS-плейлист (канал просто не в эфире сейчас),
    неопознанный формат, rtmp без потока.
    """
    if ch.reachable or ch.net_error:
        return ""
    err = (ch.check_error or "").lower()
    if ch.http_status in _HARD_DEAD_HTTP:
        return "hard"
    if any(m in err for m in _HARD_DEAD_MARKERS):
        return "hard"
    return "soft"


def _netloc_of(url: str) -> str:
    """
    netloc ровно в том виде, в каком он записан в URL: 'host', 'host:port'.
    Именно эта строка потом работает как подстрочный паттерн, поэтому
    выдумывать порт по умолчанию нельзя — в URL его физически нет.
    """
    try:
        netloc = urllib.parse.urlsplit(url).netloc.lower()
    except Exception:
        return ""
    return netloc.split("@", 1)[1] if "@" in netloc else netloc


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def collapse_to_hosts(
    to_block: list[str],
    checked_map: dict[str, SourceChannel],
    known_dead: set[str],
    min_urls: int,
    log: logging.Logger,
) -> tuple[list[str], list[str]]:
    """
    Сворачивает пачку мёртвых URL в паттерны по хосту.

    Одна строка '127.0.0.1:6878' вместо тысячи ace/getstream-ссылок — и читать
    проще, и удалить одним движением. Возвращает (паттерны, оставшиеся URL).

    Свернуть хост можно ТОЛЬКО если в этом прогоне у него не осталось ничего
    живого или непроверенного: паттерн бьёт по подстроке и заденет все будущие
    ссылки этого хоста. Один живой канал на хосте — и сворачивать нельзя.

    Два уровня: сначала пробуем весь хост целиком (zetvideo.net накрывает и
    :443, и без порта), не вышло — пробуем 'host:port'. Для голых IP уровень
    хоста пропускаем: на одном адресе часто висят разные сервисы по портам.
    """
    blocked = {u.lower() for u in to_block}

    # keep — то, что трогать нельзя: живое, ⚠️ и ❌, которые мы не блокируем.
    keep_by_host: Counter = Counter()
    keep_by_netloc: Counter = Counter()
    for url, ch in checked_map.items():
        if url.lower() in blocked:
            continue
        keep_by_host[_host_of(url)] += 1
        keep_by_netloc[_netloc_of(url)] += 1

    # Уже лежащие в блоклисте URL тоже считаем — иначе хост, размазанный
    # по нескольким прогонам, никогда не дорастёт до порога.
    dead_by_host: Counter = Counter()
    dead_by_netloc: Counter = Counter()
    for u in list(blocked) + list(known_dead):
        dead_by_host[_host_of(u)] += 1
        dead_by_netloc[_netloc_of(u)] += 1

    patterns: list[str] = []
    covered_hosts: set[str] = set()
    covered_netlocs: set[str] = set()

    for host in sorted({_host_of(u) for u in blocked}):
        if not host or keep_by_host[host] or dead_by_host[host] < min_urls:
            continue
        if _is_ip_literal(host):
            continue
        patterns.append(host)
        covered_hosts.add(host)

    for netloc in sorted({_netloc_of(u) for u in blocked}):
        host = netloc.split(":", 1)[0]
        if not netloc or host in covered_hosts:
            continue
        if keep_by_netloc[netloc] or dead_by_netloc[netloc] < min_urls:
            continue
        patterns.append(netloc)
        covered_netlocs.add(netloc)

    leftover = sorted(
        u for u in blocked
        if _host_of(u) not in covered_hosts and _netloc_of(u) not in covered_netlocs
    )
    if patterns:
        saved = len(blocked) - len(leftover)
        log.info(f"   📦 Свёрнуто в {len(patterns)} хост-паттерн(ов) "
                 f"вместо {saved} отдельных строк")
    return patterns, leftover


def append_auto_dead(patterns: list[str], urls: list[str],
                     log: logging.Logger, dry_run: bool) -> int:
    """Дописывает мёртвые хосты и URL в авто-секцию url_blocklist.txt."""
    total = len(patterns) + len(urls)
    if not total:
        return 0
    if dry_run:
        # Сами списки печатает report_blocklist ниже — здесь только итог.
        log.info(f"🧪 DRY-RUN: в {URL_BLOCKLIST_FILE} было бы добавлено "
                 f"{len(patterns)} хост(ов) и {len(urls)} URL")
        return 0

    need_marker = True
    if os.path.exists(URL_BLOCKLIST_FILE):
        with open(URL_BLOCKLIST_FILE, encoding="utf-8") as f:
            need_marker = AUTO_DEAD_MARKER not in f.read()

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(URL_BLOCKLIST_FILE, "a", encoding="utf-8") as f:
        if need_marker:
            f.write(f"\n\n{AUTO_DEAD_MARKER}\n"
                    f"# Добавлено проверкой. Строки без схемы — хосты целиком\n"
                    f"# (совпадение по подстроке), строки с http:// — точное\n"
                    f"# совпадение URL. Блок можно удалить целиком: чекер\n"
                    f"# просто проверит эти ссылки заново.\n")
        f.write(f"\n# --- {stamp} ---\n")
        if patterns:
            f.write(f"# хосты, где мертво всё ({len(patterns)}):\n")
            f.write("\n".join(patterns) + "\n")
        if urls:
            f.write(f"# отдельные ссылки ({len(urls)}):\n")
            f.write("\n".join(urls) + "\n")

    log.info(f"🚫 В {URL_BLOCKLIST_FILE} добавлено: "
             f"{len(patterns)} хост(ов), {len(urls)} URL")
    return total


REPORT_LIST_LIMIT = 25   # сколько строк списка печатать до «…и ещё N»
PROBLEM_HOST_TOP  = 12   # сколько проблемных хостов показывать
PROBLEM_HOST_MIN  = 20   # ...начиная со скольких неудачных ссылок


def report_problem_hosts(checked_map: dict[str, SourceChannel],
                         log: logging.Logger) -> None:
    """
    Кто именно съел проверку. Отвечает на вопрос «почему 65% не проверилось
    и почему прогон идёт 14 минут»: обычно это не тысяча дохлых серверов,
    а три-четыре хоста, отдающих по тысяче ссылок каждый.

    Хост, где НИ ОДНА ссылка не ожила, — прямой кандидат в ручной блоклист:
    одна строка убирает его из всех будущих прогонов.
    """
    stat: dict[str, list[int]] = {}       # host -> [живых, мёртвых, не проверено]
    for url, ch in checked_map.items():
        s = stat.setdefault(_host_of(url), [0, 0, 0])
        s[0 if ch.reachable else (2 if ch.net_error else 1)] += 1

    rows = [(h, s) for h, s in stat.items() if s[1] + s[2] >= PROBLEM_HOST_MIN]
    if not rows:
        return
    rows.sort(key=lambda r: -(r[1][1] + r[1][2]))

    log.info("")
    shown = min(PROBLEM_HOST_TOP, len(rows))
    log.info(f"   🐌 Хосты, на которые ушла проверка "
             + (f"(топ-{shown} из {len(rows)}):" if len(rows) > shown else f"({len(rows)}):"))
    for host, (live, dead, unver) in rows[:PROBLEM_HOST_TOP]:
        hint = "  ← ни одной живой, кандидат в блоклист" if live == 0 else ""
        log.info(f"        {dead + unver:>5}  {host:<34} "
                 f"✅{live:<5} ❌{dead:<5} ⚠️{unver:<5}{hint}")

    hopeless = [h for h, s in rows if s[0] == 0]
    if hopeless:
        total = sum(s[1] + s[2] for h, s in rows if s[0] == 0)
        log.info("")
        log.info(f"   💡 {len(hopeless)} хост(ов) без единой живой ссылки — это {total} "
                 f"проверок каждый прогон впустую.")
        log.info(f"      Если они не нужны — добавь их в {URL_BLOCKLIST_FILE} "
                 f"руками (по строке на хост), прогон заметно ускорится.")


def _log_list(log: logging.Logger, items: list[str], limit: int = REPORT_LIST_LIMIT,
              indent: str = "        ") -> None:
    for it in items[:limit]:
        log.info(f"{indent}{it}")
    if len(items) > limit:
        log.info(f"{indent}…и ещё {len(items) - limit} "
                 f"(полный список — в {URL_BLOCKLIST_FILE})")


def report_blocklist(cfg: Config, added_hosts: list[str], added_now: list[str],
                     offered_urls: set[str], log: logging.Logger,
                     dry_run: bool = False) -> None:
    """
    Что лежит в url_blocklist.txt — чтобы можно было глазами проверить и почистить.

    Три полезных среза:
      • что добавлено прямо сейчас — свежие кандидаты на перепроверку;
      • топ хостов — если с одного адреса набралось 20 мёртвых ссылок, дешевле
        завести один ручной паттерн на хост и удалить эти 20 строк;
      • записи, которых уже нет ни в одном источнике — их никто не предлагает,
        блоклист держит их вхолостую.
    """
    auto_before = cfg.url_block_exact
    auto_all = auto_before | {u.lower() for u in added_now}
    manual_n = len(cfg.url_block_plain) + len(cfg.url_block_wildcard)

    suffix = " (dry-run, в файл не записано)" if dry_run else ""
    log.info(f"   Ручных паттернов : {manual_n}  (подстрока/wildcard — не трогаю)")
    log.info(f"   Авто-секция      : {len(auto_all)} URL"
             + (f"  (+{len(added_now)} в этот прогон{suffix})" if added_now else ""))

    verb = "Было бы добавлено" if dry_run else "Добавлено сейчас"
    if added_hosts:
        log.info("")
        log.info(f"   📦 {verb}: хостов целиком ({len(added_hosts)}) — "
                 f"на них не осталось ни живых, ни непроверенных ссылок:")
        _log_list(log, added_hosts)

    if added_now:
        log.info("")
        log.info(f"   ➕ {verb}: отдельных ссылок ({len(added_now)}) — "
                 f"проверь и удали лишнее:")
        _log_list(log, added_now)

    if not auto_all:
        return

    by_netloc = Counter(_netloc_of(u) for u in auto_all)
    heavy = [(nl, n) for nl, n in by_netloc.most_common() if n >= 5]
    if heavy:
        log.info("")
        log.info(f"   🗜  Ещё сворачиваемо ({len(heavy)}): по этим хостам в секции "
                 f"лежит 5+ отдельных ссылок, но свернуть нельзя — на хосте "
                 f"есть живые или непроверенные:")
        _log_list(log, [f"{n:>5}  {nl}" for nl, n in heavy])

    # Никто из источников больше не отдаёт эту ссылку — держать её незачем.
    stale = sorted(u for u in auto_before if u not in offered_urls)
    if stale:
        log.info("")
        log.info(f"   🧹 Больше не встречаются в источниках ({len(stale)} из "
                 f"{len(auto_before)}) — можно удалить:")
        _log_list(log, stale)

    still = len(auto_before) - len(stale)
    if still:
        log.info("")
        log.info(f"   ℹ️  Ещё {still} записей источники продолжают предлагать — "
                 f"блоклист их отсекает, проверка на них не тратится.")


def _reason_bucket(err: Optional[str]) -> str:
    """Сводит текст ошибки к короткой категории для статистики."""
    e = (err or "unknown").lower()
    if "cdn: origin недостижим" in e:
        return "CDN: origin недостижим (52x/530)"
    if "шлюз: сервер не ответил" in e:
        return "шлюз 502/503/504 (обычно временно)"
    if "ssl" in e or "certificate" in e:
        return "SSL / сертификат"
    if "dns" in e:
        return "DNS (временный сбой)"
    if "timed out" in e or "timeout" in e:
        return "таймаут"
    if "reset" in e or "disconnected" in e or "closed" in e:
        return "сервер оборвал соединение"
    if "ffprobe" in e:
        return "rtmp/rtsp: нет ffprobe"
    return err or "unknown"


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
        "--retries", type=int, default=DEFAULT_RETRIES, metavar="N",
        help=f"Повторов при мягком отказе — таймаут, обрыв (default: {DEFAULT_RETRIES}). "
             f"0 = как раньше, без повторов.",
    )
    parser.add_argument(
        "--per-host", type=int, default=DEFAULT_PER_HOST, metavar="N",
        help=f"Максимум одновременных запросов к одному хосту "
             f"(default: {DEFAULT_PER_HOST}). Снижает ложные таймауты.",
    )
    parser.add_argument(
        "--autoblock", choices=("hard", "all", "off"), default="hard",
        help="Дописывать мёртвые URL в url_blocklist.txt. "
             "hard (default) — только безнадёжные (404/410, порт закрыт, нет домена); "
             "all — любой ❌, включая 403/5xx/пустой HLS (рискованно); off — не трогать.",
    )
    parser.add_argument(
        "--autoblock-host-min", type=int, default=DEFAULT_AUTOBLOCK_HOST_MIN,
        metavar="N",
        help=f"Сколько мёртвых ссылок на хосте нужно, чтобы записать хост целиком "
             f"одной строкой вместо каждой ссылки (default: "
             f"{DEFAULT_AUTOBLOCK_HOST_MIN}). Свернётся только если на хосте не "
             f"осталось ни живых, ни непроверенных ссылок.",
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
    offered_urls: set[str] = set()   # всё, что источники предложили в этот прогон

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
            # Копим ВСЕ предложенные источниками URL (включая заблокированные) —
            # по ним потом видно, какие записи блоклиста уже никому не нужны.
            offered_urls.add(ch.url.lower())
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
    # We need to check: all source channels (for test group) +
    # de-duplicate with update_candidates (already in the list)
    # Build a flat list: every unique source channel once
    seen_urls: set[str] = set()
    all_to_check: list[SourceChannel] = []
    for ch in all_source_channels:
        if ch.url not in seen_urls:
            seen_urls.add(ch.url)
            all_to_check.append(ch)

    log.info("")
    log.info(f"STEP 4 — Checking {len(all_to_check)} unique of {stats.parsed} source stream URLs")
    log.info(f"         (workers={args.workers}, timeout={args.timeout}s)")
    log.info("-" * 60)
    log.info("   ✅ живой   ❌ мёртвый (сервер отказал)   ⚠️ сеть/таймаут — не проверено")
    checked_map = check_all_streams(
        all_to_check, args.workers, args.timeout, args.strict, log, stats,
        per_host=args.per_host, retries=args.retries,
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

    # ── Step 6: Авто-блоклист мёртвых ссылок ────────────────────────────────
    to_block: list[str] = []
    host_patterns: list[str] = []
    if args.autoblock != "off":
        log.info("")
        log.info(f"STEP 6 — Авто-блоклист мёртвых URL (режим: {args.autoblock})")
        log.info("-" * 60)

        kinds: Counter = Counter()
        soft_reasons: Counter = Counter()
        for url, ch in checked_map.items():
            kind = dead_kind(ch)
            if not kind:
                continue
            kinds[kind] += 1
            if kind == "soft":
                soft_reasons[_reason_bucket(ch.check_error)] += 1
            if kind == "hard" or args.autoblock == "all":
                if url.lower() not in cfg.url_block_exact and not cfg.url_blocked(url):
                    to_block.append(url)

        to_block = sorted(set(to_block))
        log.info(f"   Мёртвых всего: {sum(kinds.values())}  "
                 f"(hard: {kinds['hard']}, soft: {kinds['soft']})")
        if kinds["soft"]:
            verb = "блокирую" if args.autoblock == "all" else "не блокирую"
            log.info(f"   soft ({kinds['soft']}) — {verb}, это часто временно:")
            for why, n in soft_reasons.most_common():
                log.info(f"        {n:>5}  {why}")

        # Защита от «плохого дня сети»: если один и тот же отказ прилетел от
        # большинства проверенных ссылок И сразу с десятков РАЗНЫХ хостов —
        # это не толпа умерших серверов, это VPN / DNS-фильтр / прокси на пути.
        # Блоклист вечный, прогон разовый: лучше пропустить запись, чем
        # похоронить рабочие каналы.
        #
        # Разброс по хостам тут ключевой. Тысяча отказов с одного адреса —
        # совершенно нормальный случай (не поднят локальный AceStream, лёг
        # один провайдер), и блокировать её как раз нужно.
        checked_n = len(checked_map) or 1
        by_reason: dict[str, set[str]] = {}
        reason_hits: Counter = Counter()
        for url, ch in checked_map.items():
            if ch.reachable:
                continue
            why = _reason_bucket(ch.check_error)
            reason_hits[why] += 1
            by_reason.setdefault(why, set()).add(_host_of(url))

        top_reason, top_n = (reason_hits.most_common(1) or [("", 0)])[0]
        top_hosts = len(by_reason.get(top_reason, ()))
        if top_n >= ANOMALY_MIN_COUNT and top_n / checked_n >= ANOMALY_SHARE:
            # Массовый одинаковый отказ. Разброс по хостам решает, что это:
            # сеть на нашей стороне или пачка ссылок с пары мёртвых хостов.
            log.info(f"   Главная причина отказов: «{top_reason}» — "
                     f"{top_n} шт. ({top_n / checked_n:.0%}) с {top_hosts} хост(ов)")
        if (top_n >= ANOMALY_MIN_COUNT and top_n / checked_n >= ANOMALY_SHARE
                and top_hosts >= ANOMALY_MIN_HOSTS):
            log.warning("")
            log.warning(f"   ⛔ АНОМАЛИЯ: {top_n} из {checked_n} проверенных "
                        f"({top_n / checked_n:.0%}) упали одинаково — "
                        f"«{top_reason}», и это {top_hosts} разных хостов.")
            log.warning(f"      Столько независимых серверов разом не умирает. "
                        f"Похоже на VPN / DNS-фильтр / прокси на твоей стороне.")
            log.warning(f"      Авто-блоклист в этот прогон НЕ трогаю. Проверь сеть "
                        f"и перезапусти; если так и задумано — --autoblock off "
                        f"выключит проверку совсем.")
            to_block = []
        else:
            # Порядок в файле стабильный — так diff остаётся читаемым.
            host_patterns, to_block = collapse_to_hosts(
                to_block, checked_map, cfg.url_block_exact,
                args.autoblock_host_min, log,
            )
            stats.autoblocked = append_auto_dead(
                host_patterns, to_block, log, args.dry_run)

    # ── Step 7: Ревизия блоклиста ────────────────────────────────────────────
    log.info("")
    log.info(f"STEP 7 — Ревизия {URL_BLOCKLIST_FILE}")
    log.info("-" * 60)
    report_blocklist(cfg, host_patterns, to_block, offered_urls, log, args.dry_run)
    report_problem_hosts(checked_map, log)

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
    for why, n in stats.net_fail_reasons.most_common():
        log.info(f"       ↳ {why:<28} {n}")
    log.info(f"  🔗 Matched existing     : {stats.candidates}  → inserted: {stats.inserted}")
    if stats.inserted:
        for pl in playlists:
            n = inserted_by_file.get(os.path.basename(pl.path), 0)
            if n:
                log.info(f"       ↳ {os.path.basename(pl.path):<16} +{n}")
    log.info(f"  🧪 Appended to test     : {stats.appended} URL(s)  (→ {os.path.basename(index_pl.path)})")
    log.info(f"  🚫 Auto-blocked         : {stats.autoblocked} URL(s)  (→ {URL_BLOCKLIST_FILE})")
    log.info(f"  📄 Files serviced       : {', '.join(os.path.basename(pl.path) for pl in playlists)}")
    log.info(f"  ⏱️  Total time           : {stats.elapsed}")
    log.info("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n⛔ Interrupted by user.", file=sys.stderr)
        sys.exit(1)
