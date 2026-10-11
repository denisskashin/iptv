#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
epg_check.py — у каких каналов tv/*.m3u нет программы передач в приложении.

Приложение (Видеотека) берёт программу из iptvx и привязывает её к каналу
ТОЛЬКО по tvg-id: id канала в XMLTV совпадает с tvg-id без учёта регистра.
Нет совпадения — у канала нет ни программы, ни архива, ни строки «сейчас
идёт». Скрипт проверяет каждый играбельный канал (#EXTINF со ссылкой) и
делит их на четыре вида:

  ok                     — программа есть;
  нет tvg-id             — атрибута нет вовсе;
  tvg-id нет в программе — такого id в XMLTV нет (опечатка, название вместо
                           id, канал переименован);
  id есть, передач нет   — id в XMLTV есть, но передач под ним нет: чинить в
                           плейлисте нечего, это пустота у iptvx.

Для первых трёх подсказывает кандидатов из XMLTV — по нормализованному имени
(название канала и tvg-name против id и display-name) и по паре «id ↔ id-hd».
Кандидат — только подсказка: одинаковое имя бывает у разных каналов
(«11 канал» в iptvx — днепровский, «Life TV» — Точка.РФ). Решать глазами:
группа, источник, логотип.

Отдельно — «программа кончилась»: передачи у канала есть, но последняя
кончилась раньше, чем скачан epg.xml. У iptvx так выглядит канал, чью
программу перестали обновлять: в приложении у него нет ни «сейчас идёт»,
ни новых записей в архиве.

Ещё — «чужой канал»: ссылка, по которой играет не тот канал, что в
названии. У edem номер канала в ссылке (…/iptv/<токен>/<номер>/index.m3u8)
сквозной для всех его доменов, поэтому плейлисты edem/ilook из sources.txt
дают словарь «номер → имя». Расхождение имени с нашим — подозрение:
m3u_checker.py подбирает ссылки по названиям и так уже ставил Family HDR и
Fantasy на поток BCU СССР, а FRESH Hits — на FRESH Concerts (11.10.2026).
Бывают и ложные тревоги — переименованный канал («Food Time» у edem «Еда»).
Без сети или с --no-edem эта часть пропускается.

Ничего не пишет в плейлисты — только отчёт (и CSV с --csv).

Программа (epg.xml, формат XMLTV iptvx.one) — та же, что у
enrich_candidates.py. Свежую скачать:
  curl -L https://iptvx.one/epg/epg.xml.gz | gunzip > epg.xml
Скрипт предупредит, если epg.xml старше суток.

Запуск:
  python3 epg_check.py                      # tv/*.m3u против ./epg.xml
  python3 epg_check.py --epg ~/x/epg.xml    # другая программа
  python3 epg_check.py --files tv/children.m3u
  python3 epg_check.py --csv epg_check.csv  # всё построчно в таблицу
  python3 epg_check.py --no-edem            # без сверки ссылок с edem
  python3 epg_check.py --edem iedem.m3u8    # свой плейлист edem (файл/URL)
"""

import argparse
import csv
import glob
import os
import re
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
PLAYLIST_GLOB = os.path.join(HERE, 'tv', '*.m3u')

ATTR = re.compile(r'([\w-]+)="([^"]*)"')
CHANNEL = re.compile(r'<channel id="([^"]*)">(.*?)</channel>')
DISPLAY = re.compile(r'<display-name[^>]*>(.*?)</display-name>')
PROG_CHANNEL = re.compile(r'\bchannel="([^"]*)"')
PROG_STOP = re.compile(r'\bstop="(\d{14})(?: ([+-]\d{4}))?"')

OK = 'ok'
NO_ID = 'нет tvg-id'
UNKNOWN_ID = 'tvg-id нет в программе'
EMPTY_ID = 'id есть, передач нет'
STATUSES = (OK, NO_ID, UNKNOWN_ID, EMPTY_ID)


# ── плейлисты ────────────────────────────────────────────────────────────

def title_comma(line: str) -> int:
    """Первая запятая вне кавычек — как в m3u_parser.dart приложения:
    в group-title бывает запятая («Дом, дача и ремонт»)."""
    quoted = False
    for i, ch in enumerate(line):
        if ch == '"':
            quoted = not quoted
        elif ch == ',' and not quoted:
            return i
    return -1


def read_playlist(path: str) -> list:
    """Играбельные записи: #EXTINF, за которым есть ссылка (http… или
    зеркало #http…). #EXTINF без ссылки — заготовка, приложение её не
    показывает."""
    entries, cur = [], None
    with open(path, encoding='utf-8', errors='replace') as f:
        for n, raw in enumerate(f, 1):
            line = raw.strip()
            if line.startswith('#EXTINF'):
                cut = title_comma(line)
                head, title = (line[:cut], line[cut + 1:]) if cut >= 0 else (line, '')
                cur = {'line': n, 'title': title.strip(), 'urls': [],
                       'attrs': {k.lower(): v for k, v in ATTR.findall(head)}}
                entries.append(cur)
            elif cur is not None and (line.startswith('http') or line.startswith('#http')):
                cur['urls'].append(line.lstrip('#'))
    return [e for e in entries if e['urls']]


# ── программа ────────────────────────────────────────────────────────────

def unescape(s: str) -> str:
    return (s.replace('&lt;', '<').replace('&gt;', '>').replace('&quot;', '"')
             .replace('&apos;', "'").replace('&amp;', '&'))


def parse_stop(digits: str, tz: str) -> datetime:
    t = datetime.strptime(digits, '%Y%m%d%H%M%S')
    if tz:
        sign = 1 if tz[0] == '+' else -1
        t -= sign * timedelta(hours=int(tz[1:3]), minutes=int(tz[3:5]))
    return t.replace(tzinfo=timezone.utc)


def read_epg(path: str):
    """Каналы (id → display-name) и передачи (id → сколько, последний
    stop). XMLTV iptvx — по элементу на строку, поэтому читаем строками, а
    не XML-парсером: полгигабайта так проходят за секунды."""
    names, count, last = {}, defaultdict(int), {}
    with open(path, encoding='utf-8', errors='replace') as f:
        for line in f:
            if line.startswith('<programme'):
                m = PROG_CHANNEL.search(line)
                if not m:
                    continue
                cid = m.group(1).strip().lower()
                count[cid] += 1
                s = PROG_STOP.search(line)
                if s:
                    stop = parse_stop(s.group(1), s.group(2) or '')
                    if cid not in last or stop > last[cid]:
                        last[cid] = stop
            elif line.startswith('<channel'):
                m = CHANNEL.search(line)
                if m:
                    names[m.group(1).strip().lower()] = [
                        unescape(d).strip() for d in DISPLAY.findall(m.group(2))]
    if not names:
        sys.exit(f'{path}: каналов не нашлось — не XMLTV iptvx или формат '
                 'сменился (ждём по элементу на строку)')
    return names, count, last


def norm(s: str) -> str:
    """Имя для сравнения: без регистра, ё, скобок, «HD/4K/+2/ТВ» и знаков."""
    s = s.lower().replace('ё', 'е')
    s = re.sub(r'\(.*?\)|\[.*?\]', ' ', s)
    s = re.sub(r'\b(hd|fhd|uhd|4k|sd|hdr|orig|original|\+\d+|телеканал|tv|тв)\b',
               ' ', s)
    return re.sub(r'[^\w]+', '', s)


# ── сверка ───────────────────────────────────────────────────────────────

def check(entries, names, count, by_norm):
    for e in entries:
        tid = e['attrs'].get('tvg-id', '').strip()
        low = tid.lower()
        if not tid:
            status = NO_ID
        elif low not in names and not count.get(low):
            status = UNKNOWN_ID
        elif not count.get(low):
            status = EMPTY_ID
        else:
            status = OK
        cands = []
        if status != OK:
            for key in (norm(e['title']), norm(e['attrs'].get('tvg-name', ''))):
                cands += sorted(by_norm.get(key, ())) if key else []
            if low:
                for v in (low + '-hd', re.sub(r'-hd$', '', low)):
                    if v != low:
                        cands.append(v)
            cands = [c for c in dict.fromkeys(cands) if count.get(c) and c != low]
        yield e, tid, status, cands


# ── чужой канал по номеру edem ───────────────────────────────────────────

EDEM_NUM = re.compile(r'/iptv/[A-Z0-9]+/(\d+)/(?:index|video|mono)\.m3u8')
SOURCES = os.path.join(HERE, 'sources.txt')

# Слова, которые ничего не говорят о канале: бренд пакета и качество.
# Без них «Fresh Rock» и «FRESH Dance HD» не совпадут по «fresh».
NOISE = re.compile(
    r'\b(hd|fhd|uhd|4k|hdr|orig|tv|тв|vf|bcu|pg|mm|z|yosso|citrus|cineman|'
    r'liberty|venera|strahtv|box|team|kernel|fresh|cartoons?|kids|kino|кино|'
    r'канал|channel|premium|plus|int|international|the)\b')
LATIN = str.maketrans({
    'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ж': 'z',
    'з': 'z', 'и': 'i', 'й': 'i', 'к': 'k', 'л': 'l', 'м': 'm', 'н': 'n',
    'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u', 'ф': 'f',
    'х': 'h', 'ц': 'c', 'ч': 'c', 'ш': 's', 'щ': 's', 'ы': 'y', 'э': 'e',
    'ю': 'u', 'я': 'a', 'ъ': '', 'ь': ''})


def edem_sources() -> list:
    """Плейлисты edem/ilook из sources.txt (не EPG и не картинки)."""
    try:
        with open(SOURCES, encoding='utf-8') as f:
            return [s.strip() for s in f
                    if s.strip() and not s.startswith('#')
                    and re.search(r'iedem|ilook', s) and 'epg.one' not in s]
    except OSError:
        return []


def read_text(src: str) -> str:
    if re.match(r'https?://', src):
        req = urllib.request.Request(src, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode('utf-8', errors='replace')
    with open(os.path.expanduser(src), encoding='utf-8', errors='replace') as f:
        return f.read()


def edem_names(srcs: list) -> dict:
    """Номер канала edem → имена из плейлистов edem."""
    names = defaultdict(set)
    for src in srcs:
        try:
            text = read_text(src)
        except Exception as e:  # сеть, 404, таймаут — остальные источники идут
            print(f'  edem: не скачался {src[:70]} ({e})')
            continue
        title = None
        for raw in text.splitlines():
            line = raw.strip()
            if line.startswith('#EXTINF'):
                cut = title_comma(line)
                title = line[cut + 1:].strip() if cut >= 0 else None
            elif title and line.startswith('http'):
                m = EDEM_NUM.search(line)
                if m:
                    names[m.group(1)].add(title)
                title = None
    return names


def words(name: str) -> set:
    s = NOISE.sub(' ', name.lower().replace('ё', 'е'))
    return {w for w in re.findall(r'\w+', s) if len(w) > 1}


def same_channel(ours: str, theirs: str) -> bool:
    """Похожи ли имена: общее слово или одинаковое начало слова латиницей
    («Пиксар» ↔ «Pixar», «Восток» ↔ «Vostok»). Не с чем сравнивать (одни
    бренды) — считаем, что похожи: тревога без улик хуже тишины."""
    a, b = words(ours), words(theirs)
    if not a or not b or a & b:
        return True
    la = {w.translate(LATIN).replace('x', 'ks')[:4] for w in a}
    lb = {w.translate(LATIN).replace('x', 'ks')[:4] for w in b}
    return bool(la & lb)


def foreign_streams(files: list, names: dict) -> list:
    """Ссылки, по номеру которых у edem другой канал."""
    found = []
    for path in files:
        for e in read_playlist(path):
            seen = set()
            for i, url in enumerate(e['urls']):
                m = EDEM_NUM.search(url)
                theirs = names.get(m.group(1)) if m else None
                if not theirs or m.group(1) in seen:
                    continue
                seen.add(m.group(1))
                if any(same_channel(e['title'], t) for t in theirs):
                    continue
                found.append((os.path.relpath(path, HERE), e['line'], e['title'],
                              m.group(1), 'основная' if i == 0 else 'зеркало',
                              sorted(theirs)[:2]))
    return found


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    ap.add_argument('--epg', default=os.path.join(HERE, 'epg.xml'))
    ap.add_argument('--files', nargs='*', default=None,
                    help='плейлисты (по умолчанию tv/*.m3u)')
    ap.add_argument('--csv', default=None, help='записать всё в CSV')
    ap.add_argument('--edem', nargs='*', default=None,
                    help='плейлисты edem для сверки ссылок (по умолчанию — '
                         'iedem/ilook из sources.txt)')
    ap.add_argument('--no-edem', action='store_true',
                    help='не сверять ссылки с edem')
    args = ap.parse_args()

    files = args.files or sorted(glob.glob(PLAYLIST_GLOB))
    names, count, last = read_epg(args.epg)
    newest = max(last.values()) if last else None
    fetched = datetime.fromtimestamp(os.path.getmtime(args.epg), timezone.utc)
    age = datetime.now(timezone.utc) - fetched
    print(f'программа: {args.epg} — каналов {len(names)}, с передачами '
          f'{sum(1 for v in count.values() if v)}, последняя передача '
          f'до {newest:%d.%m %H:%M} UTC' if newest else '')
    if age > timedelta(days=1):
        print(f'!! epg.xml скачан {age.days} дн. назад — сверка по старой '
              'программе. Свежую: curl -L https://iptvx.one/epg/epg.xml.gz '
              '| gunzip > epg.xml')

    by_norm = defaultdict(set)
    for cid, dn in names.items():
        for n in dn + [cid]:
            if norm(n):
                by_norm[norm(n)].add(cid)

    rows, totals = [], defaultdict(int)
    print()
    for path in files:
        entries = read_playlist(path)
        per = defaultdict(int)
        problems, ending = [], []
        for e, tid, status, cands in check(entries, names, count, by_norm):
            per[status] += 1
            low = tid.lower()
            if status != OK:
                problems.append((e, tid, status, cands))
            elif last.get(low) and last[low] < fetched:
                ending.append((e, tid, last[low]))
            rows.append({
                'файл': os.path.relpath(path, HERE), 'строка': e['line'],
                'группа': e['attrs'].get('group-title', ''), 'канал': e['title'],
                'tvg-id': tid, 'итог': status, 'передач': count.get(low, 0),
                'последняя': f'{last[low]:%Y-%m-%d %H:%M}' if low in last else '',
                'кандидаты': ' | '.join(cands[:3]),
            })
        for k, v in per.items():
            totals[k] += v
        name = os.path.relpath(path, HERE)
        print(f'{name}: каналов {len(entries)}, с программой {per[OK]}'
              + ''.join(f', {s} {per[s]}' for s in STATUSES[1:] if per[s]))
        for e, tid, status, cands in problems:
            hint = ''
            if cands:
                hint = '  → ' + '; '.join(
                    f'{c} ({count[c]} передач: {", ".join(names.get(c, [])[:2])})'
                    for c in cands[:3])
            idpart = f' [{tid}]' if tid else ''
            print(f'  {e["line"]:>5}  {status:<22} {e["title"]}{idpart}{hint}')
        for e, tid, stop in ending:
            print(f'  {e["line"]:>5}  {"программа кончилась":<22} '
                  f'{e["title"]} [{tid}] — до {stop:%d.%m %H:%M} UTC')
    total = sum(totals.values())
    print(f'\nвсего каналов {total}, с программой {totals[OK]}'
          + ''.join(f', {s} {totals[s]}' for s in STATUSES[1:] if totals[s]))

    if not args.no_edem:
        srcs = args.edem if args.edem else edem_sources()
        edem = edem_names(srcs) if srcs else {}
        if edem:
            bad = foreign_streams(files, edem)
            print(f'\nчужой канал по номеру edem (словарь: {len(edem)} номеров '
                  f'из {len(srcs)} плейлистов): {len(bad) or "нет"}')
            for name, line, title, num, kind, theirs in bad:
                print(f'  {name}  {line:>5}  {title} — {num} ({kind}): у edem '
                      + ', '.join(f'«{t}»' for t in theirs))
        else:
            print('\nчужой канал по номеру edem: словаря нет — плейлисты edem '
                  'не скачались (или --edem пуст)')

    if args.csv and rows:
        with open(args.csv, 'w', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f'CSV: {args.csv}')


if __name__ == '__main__':
    main()
