#!/usr/bin/env python3
"""
Добавляет фильмы из new.m3u в movies.m3u в правильные группы по году,
отсортированные по алфавиту. Опционально принимает alt.m3u — файл
альтернативных источников, которые добавляются сразу после основной записи
с комментарием-пометкой источника.

Использование:
    python3 merge.py <new.m3u> <movies.m3u> [alt.m3u]

Примеры:
    python3 merge.py 2000_5_new.m3u movies.m3u
    python3 merge.py 2000_5_new.m3u movies.m3u 2000_5_new_alt.m3u

movies.m3u перезаписывается на месте.
"""

import re
import sys
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse


def normalize(title: str) -> str:
    t = re.sub(r'\s*\(\d{4}\)\s*', ' ', title)
    t = re.sub(r'\s+(RU|EN|UA|VO)\s*$', '', t, flags=re.IGNORECASE)
    return t.strip().lower()


def clean_title(title: str) -> str:
    t = re.sub(r'\s*\(\d{4}\)\s*$', '', title.strip())
    t = re.sub(r'\s+(RU|EN|UA|VO)\s*$', '', t, flags=re.IGNORECASE)
    return t.strip()


def extract_year(title: str) -> str | None:
    m = re.search(r'\((\d{4})\)', title)
    return m.group(1) if m else None


def url_source_comment(url: str) -> str:
    """Извлекает имя хоста из URL для комментария."""
    try:
        host = urlparse(url).hostname or url
        # Убираем субдомены типа s1., v1., m.
        parts = host.split('.')
        if len(parts) >= 2:
            host = '.'.join(parts[-2:])
        return f'# alt: {host}'
    except Exception:
        return '# alt source'


# ---------------------------------------------------------------------------
# Парсинг movies.m3u
# Структура: year_entries[year] = [(title, url_or_None, comment_or_None)]
# ---------------------------------------------------------------------------

def parse_movies(filepath: str):
    year_entries = defaultdict(list)
    year_order = []
    seen_years = set()
    header_lines = []

    with open(filepath, encoding='utf-8') as f:
        lines = f.readlines()

    i = 0
    while i < len(lines) and not lines[i].strip().startswith('#EXTINF'):
        header_lines.append(lines[i])
        i += 1

    while i < len(lines):
        line = lines[i].strip()
        if line.startswith('#EXTINF'):
            ym = re.search(r'group-title="(\d{4})"', line)
            year = ym.group(1) if ym else 'unknown'
            title = line.split(',', 1)[-1].strip() if ',' in line else ''

            next_line = lines[i + 1] if i + 1 < len(lines) else ''
            has_url = next_line.strip().startswith('http')
            url = next_line.strip() if has_url else None

            year_entries[year].append((title, url, None))  # comment=None для основных
            if year not in seen_years:
                year_order.append(year)
                seen_years.add(year)

            i += 2 if has_url else 1
        else:
            i += 1

    return header_lines, year_entries, year_order


# ---------------------------------------------------------------------------
# Парсинг new.m3u / alt.m3u → список (clean_title, year, url)
# ---------------------------------------------------------------------------

def parse_source(filepath: str):
    entries = []
    with open(filepath, encoding='utf-8') as f:
        lines = f.readlines()

    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith('#EXTINF'):
            raw_title = line.split(',', 1)[-1].strip() if ',' in line else ''
            year = extract_year(raw_title)
            title = clean_title(raw_title)

            next_line = lines[i + 1] if i + 1 < len(lines) else ''
            has_url = next_line.strip().startswith('http')
            url = next_line.strip() if has_url else None

            if year:
                entries.append((title, year, url))
            else:
                print(f"  [!] Год не найден, пропущено: {raw_title}")

            i += 2 if has_url else 1
        else:
            i += 1

    return entries


# ---------------------------------------------------------------------------
# Запись movies.m3u
# Сортировка: сначала основные записи по алфавиту, альт-источники сразу за ними
# ---------------------------------------------------------------------------

def write_movies(filepath: str, header_lines, year_entries, year_order):
    with open(filepath, 'w', encoding='utf-8') as f:
        for h in header_lines:
            f.write(h)

        for year in sorted(year_order):
            entries = year_entries[year]

            # Группируем: основные и альт по одному и тому же title
            primaries = [(t, u, c) for t, u, c in entries if c is None]
            alts      = [(t, u, c) for t, u, c in entries if c is not None]

            # Сортируем основные по алфавиту
            primaries.sort(key=lambda e: e[0].lower())

            # Строим индекс альтов по нормализованному title
            alt_map = defaultdict(list)
            for t, u, c in alts:
                alt_map[normalize(t)].append((t, u, c))

            for title, url, _ in primaries:
                f.write(f'#EXTINF:-1 group-title="{year}",{title}\n')
                if url:
                    f.write(f'{url}\n')
                # Пишем альт-источники сразу после основной записи
                for alt_title, alt_url, comment in alt_map.get(normalize(title), []):
                    f.write(f'{comment}\n')
                    f.write(f'#EXTINF:-1 group-title="{year}",{alt_title}\n')
                    if alt_url:
                        f.write(f'{alt_url}\n')


# ---------------------------------------------------------------------------
# Основная логика
# ---------------------------------------------------------------------------

def merge(new_file: str, movies_file: str, alt_file: str | None = None):
    print(f"Читаю {movies_file}...")
    header_lines, year_entries, year_order = parse_movies(movies_file)
    total_before = sum(len(v) for v in year_entries.values())

    # --- Добавляем новые фильмы ---
    print(f"Читаю {new_file}...")
    new_entries = parse_source(new_file)

    existing_norm = set()
    for entries in year_entries.values():
        for title, _, _ in entries:
            existing_norm.add(normalize(title))

    added = skipped_dup = 0
    for title, year, url in new_entries:
        n = normalize(title)
        if n in existing_norm:
            skipped_dup += 1
        else:
            year_entries[year].append((title, url, None))
            if year not in year_order:
                year_order.append(year)
            existing_norm.add(n)
            added += 1

    # --- Добавляем альтернативные источники ---
    added_alt = skipped_alt = 0
    if alt_file:
        print(f"Читаю альт-источники {alt_file}...")
        alt_entries = parse_source(alt_file)

        # Нормализованные title основных записей (для поиска совпадения)
        primary_norm = set()
        for entries in year_entries.values():
            for t, _, c in entries:
                if c is None:
                    primary_norm.add(normalize(t))

        for title, year, url in alt_entries:
            n = normalize(title)
            if n not in primary_norm:
                skipped_alt += 1
                print(f"  [!] Нет основной записи для альт-источника: {title}")
                continue
            comment = url_source_comment(url) if url else '# alt source'
            year_entries[year].append((title, url, comment))
            added_alt += 1

    write_movies(movies_file, header_lines, year_entries, year_order)

    total_after = sum(len(v) for v in year_entries.values())
    print(f"\nБыло в movies.m3u:      {total_before}")
    print(f"Добавлено новых:        {added}")
    if alt_file:
        print(f"Альт-источников:        {added_alt}")
        if skipped_alt:
            print(f"Альт без основной:      {skipped_alt}")
    if skipped_dup:
        print(f"Дубликаты (пропущено):  {skipped_dup}")
    print(f"Стало в movies.m3u:     {total_after}")


if __name__ == '__main__':
    if len(sys.argv) not in (3, 4):
        print(__doc__)
        sys.exit(1)
    merge(*sys.argv[1:])
