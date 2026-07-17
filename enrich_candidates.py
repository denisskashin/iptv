#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
enrich_candidates.py — заполняет tvg-logo и tvg-id (ссылка на расписание/EPG)
для каналов group-title="candidate" в index.m3u и cinema.m3u,
беря данные из локального EPG (epg.xml, формат XMLTV iptvx.one).

Что делает:
  * стрим-парсит epg.xml (останавливается на первом <programme>) и строит
    индекс: нормализованное display-name -> (id, icon);
  * для каждого кандидата подбирает EPG-канал по ТОЧНОМУ, затем по
    НОРМАЛИЗОВАННОМУ имени; неоднозначные совпадения (имя ведёт на >1 канал)
    отбрасываются, чтобы не мислинковать EPG;
  * вставляет tvg-logo="<icon>" tvg-id="<id>" сразу после group-title="candidate"
    (порядок как в остальном файле: logo -> id -> description);
  * ОПИСАНИЯ (tvg-description) НЕ трогает — их в EPG нет.

Безопасность:
  * по умолчанию DRY-RUN (ничего не пишет, только статистика);
  * с --apply: бэкап <файл>.enrich.bak, запись атомарная (tmp в той же папке
    + os.replace), md5-гард (если файл изменился с момента чтения — стоп);
  * трогает ТОЛЬКО строки #EXTINF кандидатов; URL и прочие строки — байт-в-байт;
  * пост-проверка: число строк, мультимножество URL и множество имён кандидатов
    до/после должны совпадать, а каждая изменённая строка обязана отличаться
    ровно на вставленные атрибуты.

Запуск:
  python3 enrich_candidates.py                 # dry-run, index.m3u + cinema.m3u
  python3 enrich_candidates.py --apply         # применить с бэкапом
  python3 enrich_candidates.py --files a.m3u   # другой набор файлов
  python3 enrich_candidates.py --epg epg.xml   # другой EPG
"""
from __future__ import annotations

import argparse
import hashlib
import html
import os
import re
import sys
import tempfile
from collections import defaultdict

# --- Токены, отбрасываемые с ХВОСТА имени при нормализации -------------------
# качество/служебные пометки чекера
QUALITY = {
    "hd", "sd", "fhd", "uhd", "qhd", "4k", "8k", "hq", "hevc", "h265",
    "h.265", "fullhd", "ultrahd", "1080p", "720p", "576p", "480p", "240p",
    "50fps", "60fps", "orig",
}
# 2-буквенные коды стран/языков — чекер лепит их как источник (" DE", " US"...)
COUNTRY = {
    "de", "ee", "lt", "lv", "us", "ua", "pl", "ru", "fr", "es", "it", "tr",
    "az", "kz", "by", "ge", "am", "uz", "md", "kg", "tj", "il", "gb", "uk",
    "ca", "at", "ch", "nl", "cz", "sk", "ro", "bg", "rs", "gr", "cn", "in",
    "ir", "tm", "fi", "se", "no", "dk", "hu", "pt", "ae", "sa", "eu",
}
STRIP_TAIL = QUALITY | COUNTRY

# --- Ручные алиасы: имя кандидата (casefold) -> EPG id -----------------------
# точечные соответствия, которые авто-матч не ловит (можно расширять файлом
# epg_aliases.txt рядом со скриптом, формат: "имя канала => epg-id")
BUILTIN_ALIASES = {
    "love nature": "love-nature-4k",
    "terra incognita": "terra-inkognita",
    "world fashion channel russia": "world-fashion-channel",
    "беларусь 4 витебске": "belarus4-vitebsk",
    "шансон тв": "shanson-tv",
}

CAND_TOKEN = 'group-title="candidate"'


# ---------------------------------------------------------------------------
# Нормализация имён
# ---------------------------------------------------------------------------
def normalize(name: str) -> str:
    """casefold + снятие хвостовых меток качества/страны + [source]-тегов."""
    s = html.unescape(name).strip().casefold()
    s = re.sub(r"\s*\[[^\]]*\]\s*$", "", s)      # хвостовой [PL], [source] ...
    s = s.replace(" ", " ")
    tokens = s.split()
    while tokens and tokens[-1] in STRIP_TAIL:
        tokens.pop()
    return " ".join(tokens)


def exact_key(name: str) -> str:
    return html.unescape(name).strip().casefold()


def tail_countries(name: str) -> set[str]:
    """Коды стран, снятые с ХВОСТА имени (напр. 'AXN HD ES' -> {'es'})."""
    s = html.unescape(name).strip().casefold()
    s = re.sub(r"\s*\[[^\]]*\]\s*$", "", s)
    tokens = s.split()
    out = set()
    while tokens and tokens[-1] in STRIP_TAIL:
        if tokens[-1] in COUNTRY:
            out.add(tokens[-1])
        tokens.pop()
    return out


_ID_CC = re.compile(r"-([a-z]{2})$")


def guard_ok(name: str, cid: str) -> bool:
    """Отсекаем кросс-страновые ложные матчи: если у кандидата явно указана
    страна (напр. ES), а id EPG заканчивается на ДРУГУЮ страну (напр. -pl),
    это разные каналы -> матч отклоняем. Если страны у кандидата нет — доверяем."""
    m = _ID_CC.search(cid)
    if not m or m.group(1) not in COUNTRY:
        return True
    tc = tail_countries(name)
    return (not tc) or (m.group(1) in tc)


# ---------------------------------------------------------------------------
# Разбор EPG
# ---------------------------------------------------------------------------
class Epg:
    def __init__(self):
        self.id_icon: dict[str, str] = {}
        self.by_exact: dict[str, set[str]] = defaultdict(set)
        self.by_norm: dict[str, set[str]] = defaultdict(set)

    def lookup(self, name: str, aliases: dict[str, str]):
        """-> (id, icon|None) или None. Неоднозначные имена отбрасываются.
        Порядок доверия: ручной алиас -> точное имя -> нормализованное (+гард)."""
        a = aliases.get(exact_key(name))
        if a:
            return a, self.id_icon.get(a)
        ek = exact_key(name)
        if ek in self.by_exact:
            ids = self.by_exact[ek]
            if len(ids) == 1:                       # точное совпадение
                cid = next(iter(ids))
                return cid, self.id_icon.get(cid)
            return None                             # одноимённые каналы — пропуск
        ids = self.by_norm.get(normalize(name))     # нормализованный фолбэк
        if ids and len(ids) == 1:
            cid = next(iter(ids))
            if guard_ok(name, cid):
                return cid, self.id_icon.get(cid)
        return None


def load_epg(path: str) -> Epg:
    """Читаем секцию <channel> (до первого <programme>) стримом."""
    buf = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if "<programme" in line:
                break
            buf.append(line)
    text = "".join(buf)

    epg = Epg()
    ch_re = re.compile(r'<channel\s+id="([^"]*)"\s*>(.*?)</channel>', re.S)
    dn_re = re.compile(r"<display-name[^>]*>(.*?)</display-name>", re.S)
    ic_re = re.compile(r'<icon\s+src="([^"]*)"')

    for m in ch_re.finditer(text):
        cid, body = m.group(1), m.group(2)
        icon = ic_re.search(body)
        if icon:
            epg.id_icon[cid] = icon.group(1)
        for dn in dn_re.findall(body):
            name = html.unescape(dn).strip()
            if not name:
                continue
            epg.by_exact[exact_key(name)].add(cid)
            epg.by_norm[normalize(name)].add(cid)
    return epg


def load_aliases(script_dir: str) -> dict[str, str]:
    aliases = dict(BUILTIN_ALIASES)
    path = os.path.join(script_dir, "epg_aliases.txt")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=>" not in line:
                    continue
                left, right = line.split("=>", 1)
                aliases[left.strip().casefold()] = right.strip()
    return aliases


# ---------------------------------------------------------------------------
# Работа со строкой #EXTINF
# ---------------------------------------------------------------------------
def split_name(line: str) -> str:
    """Имя канала = после разделяющей запятой (кавычки маскируем — на случай
    запятых внутри значений атрибутов/описаний)."""
    masked = re.sub(r'"[^"]*"', lambda m: " " * len(m.group()), line)
    idx = masked.find(",")
    return line[idx + 1:].strip() if idx != -1 else ""


def enrich_line(line: str, epg: Epg, aliases: dict[str, str]):
    """-> (новая_строка, вставка|None). Меняет только строки кандидатов."""
    if CAND_TOKEN not in line:
        return line, None
    has_id = "tvg-id=" in line
    has_logo = "tvg-logo=" in line
    if has_id and has_logo:
        return line, None
    name = split_name(line)
    if not name:
        return line, None
    hit = epg.lookup(name, aliases)
    if not hit:
        return line, None
    cid, icon = hit
    insert = ""
    if not has_logo and icon:
        insert += f' tvg-logo="{icon}"'
    if not has_id:
        insert += f' tvg-id="{cid}"'
    if not insert:
        return line, None
    new = line.replace(CAND_TOKEN, CAND_TOKEN + insert, 1)
    return new, insert


# ---------------------------------------------------------------------------
# Проверки целостности
# ---------------------------------------------------------------------------
def urls_multiset(lines):
    out = defaultdict(int)
    for ln in lines:
        s = ln.strip()
        if not s or s.startswith("#EXTINF"):
            continue
        key = s[1:].strip() if s.startswith("#") else s   # # запасная == рабочая
        if "://" in key:
            out[key] += 1
    return out


def cand_names_multiset(lines):
    out = defaultdict(int)
    for ln in lines:
        if ln.startswith("#EXTINF") and CAND_TOKEN in ln:
            out[split_name(ln)] += 1
    return out


def verify(old_lines, new_lines, inserts):
    assert len(old_lines) == len(new_lines), "число строк изменилось"
    for o, n, ins in zip(old_lines, new_lines, inserts):
        if ins is None:
            assert o == n, "строка изменена без вставки"
        else:
            assert n == o.replace(CAND_TOKEN, CAND_TOKEN + ins, 1), "битая вставка"
            assert n.replace(ins, "", 1) == o, "вставка необратима"
    assert urls_multiset(old_lines) == urls_multiset(new_lines), "URL-мультимножество разошлось"
    assert cand_names_multiset(old_lines) == cand_names_multiset(new_lines), "имена кандидатов разошлись"


# ---------------------------------------------------------------------------
# Обработка файла
# ---------------------------------------------------------------------------
def md5(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def process_file(path, epg, aliases, apply, backup, sample):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    src_md5 = hashlib.md5(text.encode("utf-8")).hexdigest()
    keepends = text.splitlines(keepends=True)

    new_lines, inserts = [], []
    matched, logo_n, id_n = 0, 0, 0
    samples, unmatched = [], []

    for ln in keepends:
        body = ln.rstrip("\n")
        eol = ln[len(body):]
        if body.startswith("#EXTINF") and CAND_TOKEN in body:
            new_body, ins = enrich_line(body, epg, aliases)
            if ins:
                matched += 1
                if "tvg-logo=" in ins:
                    logo_n += 1
                if "tvg-id=" in ins:
                    id_n += 1
                if len(samples) < sample:
                    samples.append((split_name(body), ins.strip()))
            elif ("tvg-id=" not in body) and len(unmatched) < sample:
                unmatched.append(split_name(body))
            new_lines.append(new_body + eol)
            inserts.append(ins)
        else:
            new_lines.append(ln)
            inserts.append(None)

    verify(keepends, new_lines, inserts)

    total_cand = sum(1 for ln in keepends
                     if ln.startswith("#EXTINF") and CAND_TOKEN in ln)
    stats = dict(file=os.path.basename(path), total_cand=total_cand,
                 matched=matched, logo=logo_n, id=id_n,
                 samples=samples, unmatched=unmatched)

    if apply and matched:
        if md5(path) != src_md5:
            raise SystemExit(f"[STOP] {path} изменился во время работы — не пишу.")
        if backup:
            bak = path + ".enrich.bak"
            with open(bak, "w", encoding="utf-8") as f:
                f.write(text)
        d = os.path.dirname(os.path.abspath(path))
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("".join(new_lines))
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        stats["written"] = True
    else:
        stats["written"] = False
    return stats


# ---------------------------------------------------------------------------
def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description="Заполнить tvg-logo/tvg-id кандидатам из EPG.")
    ap.add_argument("--epg", default=os.path.join(script_dir, "epg.xml"))
    ap.add_argument("--files", nargs="+",
                    default=[os.path.join(script_dir, "index.m3u"),
                             os.path.join(script_dir, "cinema.m3u")])
    ap.add_argument("--apply", action="store_true", help="записать изменения (иначе dry-run)")
    ap.add_argument("--no-backup", action="store_true")
    ap.add_argument("--sample", type=int, default=12, help="сколько примеров печатать")
    args = ap.parse_args()

    if not os.path.exists(args.epg):
        raise SystemExit(f"EPG не найден: {args.epg}")
    print(f"EPG: {args.epg}")
    epg = load_epg(args.epg)
    aliases = load_aliases(script_dir)
    print(f"  каналов с иконкой: {len(epg.id_icon)}; "
          f"уникальных имён (exact): {len(epg.by_exact)}; алиасов: {len(aliases)}")
    print(f"Режим: {'APPLY' if args.apply else 'DRY-RUN (файлы не меняются)'}\n")

    grand = defaultdict(int)
    for path in args.files:
        if not os.path.exists(path):
            print(f"— {path}: нет файла, пропуск")
            continue
        st = process_file(path, epg, aliases, args.apply, not args.no_backup, args.sample)
        grand["total_cand"] += st["total_cand"]
        grand["matched"] += st["matched"]
        grand["logo"] += st["logo"]
        grand["id"] += st["id"]
        print(f"[{st['file']}] кандидатов: {st['total_cand']}; "
              f"подобрано: {st['matched']} (logo {st['logo']}, id {st['id']}); "
              f"{'ЗАПИСАНО' if st['written'] else 'без записи'}")
        for nm, ins in st["samples"]:
            print(f"    + {nm}  ->  {ins}")
        if st["unmatched"]:
            print(f"    не найдено (примеры): {', '.join(st['unmatched'][:8])}")
        print()

    print(f"ИТОГО: кандидатов {grand['total_cand']}, "
          f"подобрано {grand['matched']} (logo {grand['logo']}, id {grand['id']})")
    if not args.apply and grand["matched"]:
        print("Это dry-run. Чтобы применить: python3 enrich_candidates.py --apply")


if __name__ == "__main__":
    main()
