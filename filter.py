#!/usr/bin/env python3
"""
Фильтрует source.m3u и создаёт два файла:
  - output.m3u      — фильмы которых нет ни в movies.m3u ни в watched.m3u (новые)
  - output_alt.m3u  — фильмы которые есть в watched.m3u И в movies.m3u
                      (альтернативные источники для уже существующих записей)

Сравнение без учёта регистра и без года.

Использование:
    python3 filter.py <source.m3u> <movies.m3u> <watched.m3u> <output.m3u>

Пример:
    python3 filter.py 2000_5.m3u movies.m3u watched.m3u 2000_5_new.m3u
    → создаст 2000_5_new.m3u и 2000_5_new_alt.m3u
"""

import re
import sys
from pathlib import Path


def normalize(title: str) -> str:
    t = re.sub(r'\s*\(\d{4}\)\s*', ' ', title)
    t = re.sub(r'\s+(RU|EN|UA|VO)\s*$', '', t, flags=re.IGNORECASE)
    return t.strip().lower()


def extract_normalized_titles(filepath: str) -> set:
    titles = set()
    with open(filepath, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line.startswith('#EXTINF'):
                title = line.split(',', 1)[-1].strip() if ',' in line else ''
                if title:
                    titles.add(normalize(title))
    return titles


def filter_m3u(source: str, movies_file: str, watched_file: str, output: str):
    movies_norm  = extract_normalized_titles(movies_file)
    watched_norm = extract_normalized_titles(watched_file)

    # Путь для альтернативных источников
    out_path = Path(output)
    alt_output = str(out_path.with_name(out_path.stem + '_alt' + out_path.suffix))

    new_lines = ['#EXTM3U\n\n']  # новые фильмы
    alt_lines = ['#EXTM3U\n\n']  # альтернативные источники

    cnt_new = cnt_alt = cnt_skip = 0

    with open(source, encoding='utf-8') as f:
        lines = f.readlines()

    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith('#EXTINF'):
            title = line.split(',', 1)[-1].strip() if ',' in line else ''
            n = normalize(title)
            next_line = lines[i + 1] if i + 1 < len(lines) else ''
            has_url = next_line.strip().startswith('http')
            step = 2 if has_url else 1

            in_movies  = n in movies_norm
            in_watched = n in watched_norm

            if in_watched and in_movies:
                # Альтернативный источник для существующей записи
                alt_lines.append(lines[i])
                if has_url:
                    alt_lines.append(next_line)
                cnt_alt += 1
            elif not in_movies and not in_watched:
                # Новый фильм
                new_lines.append(lines[i])
                if has_url:
                    new_lines.append(next_line)
                cnt_new += 1
            else:
                # В watched но не в movies → пропускаем
                cnt_skip += 1

            i += step
        else:
            i += 1

    with open(output, 'w', encoding='utf-8') as f:
        f.writelines(new_lines)

    with open(alt_output, 'w', encoding='utf-8') as f:
        f.writelines(alt_lines)

    print(f"Источник: {source}  ({cnt_new + cnt_alt + cnt_skip} фильмов)")
    print(f"  Новые (→ {output}):        {cnt_new}")
    print(f"  Альт-источники (→ {Path(alt_output).name}): {cnt_alt}")
    print(f"  Пропущено (watched, нет в movies): {cnt_skip}")


if __name__ == '__main__':
    if len(sys.argv) != 5:
        print(__doc__)
        sys.exit(1)
    filter_m3u(*sys.argv[1:])
