#!/usr/bin/env python3
"""
Добавляет фильмы из new.m3u в movies.m3u в правильные группы по году,
отсортированные по алфавиту.

Дополнительно: записи из filtered.m3u, чей заголовок совпадает с уже
существующим фильмом в movies.m3u, добавляются как альтернативные источники
(с комментарием # alt: <host> перед #EXTINF). Такие записи удаляются
из filtered.m3u после обработки.

Добавленные как primary фильмы также удаляются из filtered.m3u.

Использование:
    python3 merge.py <new.m3u> <movies.m3u>

movies.m3u и filtered.m3u перезаписываются на месте.
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
        parts = host.split('.')
        if len(parts) >= 2:
            host = '.'.join(parts[-2:])
        return f'# alt: {host}'
    except Exception:
        return '# alt source'


# ---------------------------------------------------------------------------
# Парсинг movies.m3u
# Структура: year_entries[year] = [(title, url_or_None, comment_or_None)]
# comment=None  → основная запись
# comment=str   → альт-источник
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
        # Alt-комментарий: следующий #EXTINF — это альт-источник, не основная запись
        if line.startswith('# alt:') or line == '# alt source':
            comment = line
            i += 1
            if i < len(lines) and lines[i].strip().startswith('#EXTINF'):
                extinf = lines[i].strip()
                ym = re.search(r'group-title="(\d{4})"', extinf)
                year = ym.group(1) if ym else 'unknown'
                title = extinf.split(',', 1)[-1].strip() if ',' in extinf else ''
                next_line = lines[i + 1] if i + 1 < len(lines) else ''
                has_url = next_line.strip().startswith('http')
                url = next_line.strip() if has_url else None
                year_entries[year].append((title, url, comment))
                if year not in seen_years:
                    year_order.append(year)
                    seen_years.add(year)
                i += 2 if has_url else 1
            # else: одинокий комментарий без EXTINF — пропускаем
        elif line.startswith('#EXTINF'):
            ym = re.search(r'group-title="(\d{4})"', line)
            year = ym.group(1) if ym else 'unknown'
            title = line.split(',', 1)[-1].strip() if ',' in line else ''

            next_line = lines[i + 1] if i + 1 < len(lines) else ''
            has_url = next_line.strip().startswith('http')
            url = next_line.strip() if has_url else None

            year_entries[year].append((title, url, None))
            if year not in seen_years:
                year_order.append(year)
                seen_years.add(year)

            i += 2 if has_url else 1
        else:
            i += 1

    return header_lines, year_entries, year_order


# ---------------------------------------------------------------------------
# Парсинг произвольного m3u → список (clean_title, year, url)
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
# Сортировка: основные по алфавиту, альт-источники сразу за ними
# ---------------------------------------------------------------------------

def write_movies(filepath: str, header_lines, year_entries, year_order):
    with open(filepath, 'w', encoding='utf-8') as f:
        for h in header_lines:
            f.write(h)

        for year in sorted(year_order):
            entries = year_entries[year]

            primaries = [(t, u, c) for t, u, c in entries if c is None]
            alts      = [(t, u, c) for t, u, c in entries if c is not None]

            primaries.sort(key=lambda e: e[0].lower())

            alt_map = defaultdict(list)
            for t, u, c in alts:
                alt_map[normalize(t)].append((t, u, c))

            for title, url, _ in primaries:
                f.write(f'#EXTINF:-1 group-title="{year}",{title}\n')
                if url:
                    f.write(f'{url}\n')
                for alt_title, alt_url, comment in alt_map.get(normalize(title), []):
                    f.write(f'{comment}\n')
                    f.write(f'#EXTINF:-1 group-title="{year}",{alt_title}\n')
                    if alt_url:
                        f.write(f'{alt_url}\n')


# ---------------------------------------------------------------------------
# Удаление из filtered.m3u по множеству нормализованных заголовков
# ---------------------------------------------------------------------------

def remove_from_filtered(filtered_path: str, titles_norm: set) -> int:
    """Удаляет записи, чьи нормализованные заголовки есть в titles_norm.
    Возвращает количество удалённых записей."""
    p = Path(filtered_path)
    if not p.exists() or not titles_norm:
        return 0

    with open(filtered_path, encoding='utf-8') as f:
        lines = f.readlines()

    new_lines = []
    removed = 0
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith('#EXTINF'):
            raw_title = line.split(',', 1)[-1].strip() if ',' in line else ''
            n = normalize(raw_title)
            next_line = lines[i + 1] if i + 1 < len(lines) else ''
            has_url = next_line.strip().startswith('http')

            if n in titles_norm:
                removed += 1
                i += 2 if has_url else 1
            else:
                new_lines.append(lines[i])
                if has_url:
                    new_lines.append(lines[i + 1])
                i += 2 if has_url else 1
        else:
            new_lines.append(lines[i])
            i += 1

    with open(filtered_path, 'w', encoding='utf-8') as f:
        f.writelines(new_lines)

    return removed


# ---------------------------------------------------------------------------
# Основная логика
# ---------------------------------------------------------------------------

def merge(new_file: str, movies_file: str):
    print(f"Читаю {movies_file}...")
    header_lines, year_entries, year_order = parse_movies(movies_file)
    total_before = sum(len(v) for v in year_entries.values())

    # --- Добавляем новые фильмы из new.m3u ---
    print(f"Читаю {new_file}...")
    new_entries = parse_source(new_file)

    existing_norm = set()
    for entries in year_entries.values():
        for title, _, _ in entries:
            existing_norm.add(normalize(title))

    added = skipped_dup = 0
    added_norms = set()
    for title, year, url in new_entries:
        n = normalize(title)
        if n in existing_norm:
            skipped_dup += 1
        else:
            year_entries[year].append((title, url, None))
            if year not in year_order:
                year_order.append(year)
            existing_norm.add(n)
            added_norms.add(n)
            added += 1

    # --- Ищем альт-источники в filtered.m3u ---
    filtered_path = str(Path(movies_file).parent / 'filtered.m3u')
    added_alt = skipped_alt = 0
    alt_consumed_norms = set()  # titles из filtered, которые совпали с primary (для удаления)

    if Path(filtered_path).exists():
        print(f"Ищу альт-источники в filtered.m3u...")
        filtered_entries = parse_source(filtered_path)

        # Нормализованные title всех primary в movies.m3u (включая только что добавленные)
        primary_norm = {normalize(t) for entries in year_entries.values()
                        for t, _, c in entries if c is None}

        # Уже существующие (norm_title, url) в alts и primaries — для дедупликации
        existing_alt_keys: set[tuple[str, str | None]] = {
            (normalize(t), u)
            for entries in year_entries.values()
            for t, u, c in entries
            if c is not None
        }
        primary_keys: set[tuple[str, str | None]] = {
            (normalize(t), u)
            for entries in year_entries.values()
            for t, u, c in entries
            if c is None
        }

        for title, year, url in filtered_entries:
            n = normalize(title)
            if n not in primary_norm:
                continue  # нет основной записи → оставляем в filtered

            # Помечаем для удаления из filtered независимо от результата
            alt_consumed_norms.add(n)

            if (n, url) in existing_alt_keys or (n, url) in primary_keys:
                skipped_alt += 1
                continue

            comment = url_source_comment(url) if url else '# alt source'
            year_entries[year].append((title, url, comment))
            existing_alt_keys.add((n, url))
            added_alt += 1

    write_movies(movies_file, header_lines, year_entries, year_order)

    # --- Удаляем из filtered.m3u: новые primary + обработанные alt ---
    to_remove = added_norms | alt_consumed_norms
    removed_from_filtered = remove_from_filtered(filtered_path, to_remove)

    total_after = sum(len(v) for v in year_entries.values())
    print(f"\nБыло в movies.m3u:           {total_before}")
    print(f"Добавлено новых:             {added}")
    if added_alt or skipped_alt:
        print(f"Альт-источников из filtered: {added_alt}")
    if skipped_alt:
        print(f"Альт-дубликатов (пропущено):{skipped_alt}")
    if skipped_dup:
        print(f"Дубликаты (пропущено):       {skipped_dup}")
    print(f"Стало в movies.m3u:          {total_after}")
    if removed_from_filtered:
        print(f"Удалено из filtered.m3u:     {removed_from_filtered}")


if __name__ == '__main__':
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    merge(sys.argv[1], sys.argv[2])
