#!/usr/bin/env python3
"""
Добавляет фильмы из new.m3u в movies.m3u в правильные группы по году,
отсортированные по алфавиту.

Дополнительно: записи из filtered.m3u, чей заголовок совпадает с уже
существующим фильмом в movies.m3u, добавляются как альтернативные источники
(строка #url сразу после основного URL). Такие записи удаляются из filtered.m3u
после обработки.

Добавленные как primary фильмы также удаляются из filtered.m3u.

Формат альт-источников в movies.m3u:
    #EXTINF:-1 group-title="2013",Волк с Уолл-стрит
    http://main-source.com/volk
    #http://kinoleha.net/load/volk-s-uoll-strit

При первом запуске старый формат (# alt: + дублирующий #EXTINF) автоматически
мигрирует в новый.

Использование:
    python3 merge.py <new.m3u> <movies.m3u>

movies.m3u и filtered.m3u перезаписываются на месте.
"""

import re
import sys
from collections import defaultdict
from pathlib import Path


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


# ---------------------------------------------------------------------------
# Парсинг movies.m3u
#
# Структура: year_entries[year] = [{'title': str, 'url': str|None, 'alts': [str, ...]}]
#
# Поддерживает оба формата:
#   Новый: строки #http... сразу после URL основной записи
#   Старый: # alt: <host> + дублирующий #EXTINF (мигрирует в новый при записи)
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

    # Первый проход: читаем все записи в плоский список
    raw = []  # [{'kind': 'primary'|'alt', 'title', 'url', 'year', 'alts': [...]}]

    while i < len(lines):
        line = lines[i].strip()

        if line.startswith('# alt:') or line == '# alt source':
            # Старый формат: следующая строка — #EXTINF альт-источника
            i += 1
            if i < len(lines) and lines[i].strip().startswith('#EXTINF'):
                extinf = lines[i].strip()
                ym = re.search(r'group-title="(\d{4})"', extinf)
                year = ym.group(1) if ym else 'unknown'
                title = extinf.split(',', 1)[-1].strip() if ',' in extinf else ''
                next_line = lines[i + 1].strip() if i + 1 < len(lines) else ''
                has_url = next_line.startswith('http')
                url = next_line if has_url else None
                raw.append({'kind': 'alt', 'title': title, 'url': url,
                            'year': year, 'norm': normalize(title)})
                i += 2 if has_url else 1
            # else: одинокий комментарий без EXTINF — пропускаем

        elif line.startswith('#EXTINF'):
            ym = re.search(r'group-title="(\d{4})"', line)
            year = ym.group(1) if ym else 'unknown'
            title = line.split(',', 1)[-1].strip() if ',' in line else ''
            next_line = lines[i + 1].strip() if i + 1 < len(lines) else ''
            has_url = next_line.startswith('http')
            url = next_line if has_url else None

            # Новый формат: собираем #http... строки сразу после URL
            alts = []
            j = i + (2 if has_url else 1)
            while j < len(lines):
                alt_line = lines[j].strip()
                if alt_line.startswith('#http') or alt_line.startswith('#https'):
                    alts.append(alt_line[1:])  # убираем ведущий #
                    j += 1
                else:
                    break

            raw.append({'kind': 'primary', 'title': title, 'url': url,
                        'year': year, 'norm': normalize(title), 'alts': alts})
            i = j

        else:
            i += 1

    # Второй проход: строим итоговую структуру
    # Старые alt-записи прикрепляем к соответствующим primary по нормализованному title
    primary_index: dict[str, dict] = {}  # norm → entry dict (последний primary)

    for entry in raw:
        if entry['kind'] == 'primary':
            year = entry['year']
            e = {'title': entry['title'], 'url': entry['url'], 'alts': list(entry['alts'])}
            if year not in seen_years:
                year_order.append(year)
                seen_years.add(year)
            year_entries[year].append(e)
            primary_index[entry['norm']] = e
        else:
            # Старый alt — прикрепляем к primary по совпадению title
            n = entry['norm']
            if n in primary_index and entry['url']:
                p = primary_index[n]
                if entry['url'] not in p['alts'] and entry['url'] != p['url']:
                    p['alts'].append(entry['url'])
            # Если primary не нашли — игнорируем (не должно случаться)

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
# Запись movies.m3u (новый формат)
# Альт-источники: строки #url сразу после основного URL, без # alt: и без #EXTINF
# ---------------------------------------------------------------------------

def write_movies(filepath: str, header_lines, year_entries, year_order):
    with open(filepath, 'w', encoding='utf-8') as f:
        for h in header_lines:
            f.write(h)

        for year in sorted(year_order):
            entries = year_entries[year]
            entries.sort(key=lambda e: e['title'].lower())

            for entry in entries:
                f.write(f'#EXTINF:-1 group-title="{year}",{entry["title"]}\n')
                if entry['url']:
                    f.write(f'{entry["url"]}\n')
                for alt_url in entry['alts']:
                    f.write(f'#{alt_url}\n')


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

    existing_norm = {normalize(e['title'])
                     for entries in year_entries.values()
                     for e in entries}

    added = skipped_dup = 0
    added_norms = set()
    for title, year, url in new_entries:
        n = normalize(title)
        if n in existing_norm:
            skipped_dup += 1
        else:
            year_entries[year].append({'title': title, 'url': url, 'alts': []})
            if year not in year_order:
                year_order.append(year)
            existing_norm.add(n)
            added_norms.add(n)
            added += 1

    # --- Ищем альт-источники в filtered.m3u ---
    filtered_path = str(Path(movies_file).parent / 'filtered.m3u')
    added_alt = skipped_alt = 0
    alt_consumed_norms = set()

    if Path(filtered_path).exists():
        print(f"Ищу альт-источники в filtered.m3u...")
        filtered_entries = parse_source(filtered_path)

        # Индекс primary по norm для быстрого поиска
        primary_index: dict[str, dict] = {}
        for entries in year_entries.values():
            for e in entries:
                primary_index[normalize(e['title'])] = e

        for title, year, url in filtered_entries:
            n = normalize(title)
            if n not in primary_index:
                continue  # нет основной записи → оставляем в filtered

            alt_consumed_norms.add(n)

            if not url:
                skipped_alt += 1
                continue

            primary = primary_index[n]

            # Дедупликация: URL не должен совпадать с primary URL или уже существующими alts
            if url == primary['url'] or url in primary['alts']:
                skipped_alt += 1
                continue

            primary['alts'].append(url)
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
