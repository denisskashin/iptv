#!/usr/bin/env python3
"""
Фильтрует source.m3u: пропускает записи, у которых название или ссылка
уже есть в movies.m3u, watched.m3u, rus_movies.m3u или cartoons.m3u.
Сравнение названий — без учёта регистра и без года.

Использование (полное):
    python3 filter.py <source.m3u> <movies.m3u> <watched.m3u> <rus_movies.m3u> <cartoons.m3u> <output.m3u>

Использование (с конфигом filter.cfg):
    python3 filter.py <source.m3u> <output.m3u>

filter.cfg — файл рядом со скриптом, формат:
    movies     = movies.m3u
    watched    = watched.m3u
    rus_movies = rus_movies.m3u
    cartoons   = cartoons.m3u
"""

import re
import sys
import configparser
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


def extract_urls(filepath: str) -> set:
    urls = set()
    with open(filepath, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line.startswith('http'):
                urls.add(line)
    return urls


def filter_m3u(source: str, movies_file: str, watched_file: str, rus_movies_file: str, cartoons_file: str, output: str):
    movies_norm     = extract_normalized_titles(movies_file)
    watched_norm    = extract_normalized_titles(watched_file)
    rus_movies_norm = extract_normalized_titles(rus_movies_file)
    cartoons_norm   = extract_normalized_titles(cartoons_file)

    known_urls = (
        extract_urls(movies_file) |
        extract_urls(watched_file) |
        extract_urls(rus_movies_file) |
        extract_urls(cartoons_file)
    )

    out_lines = ['#EXTM3U\n\n']
    cnt_new = cnt_skip = 0

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

            url = next_line.strip() if has_url else ''

            title_known = (
                n in movies_norm or
                n in watched_norm or
                n in rus_movies_norm or
                n in cartoons_norm
            )
            url_known = url in known_urls if url else False

            if title_known or url_known:
                cnt_skip += 1
            else:
                out_lines.append(lines[i])
                if has_url:
                    out_lines.append(next_line)
                cnt_new += 1

            i += step
        else:
            i += 1

    with open(output, 'w', encoding='utf-8') as f:
        f.writelines(out_lines)

    print(f"Источник: {source}  ({cnt_new + cnt_skip} фильмов)")
    print(f"  → Итого в {output}: {cnt_new}")
    print(f"  Пропущено (название или ссылка уже есть в файлах): {cnt_skip}")


def load_config() -> dict:
    cfg_path = Path(__file__).parent / 'filter.cfg'
    if not cfg_path.exists():
        return {}
    cfg = configparser.ConfigParser()
    cfg.read(cfg_path, encoding='utf-8')
    section = cfg['filter'] if 'filter' in cfg else cfg.defaults()
    return {
        'movies':     section.get('movies'),
        'watched':    section.get('watched'),
        'rus_movies': section.get('rus_movies'),
        'cartoons':   section.get('cartoons'),
    }


if __name__ == '__main__':
    if len(sys.argv) == 3:
        # Короткий вызов: source output — остальное из конфига
        cfg = load_config()
        missing = [k for k in ('movies', 'watched', 'rus_movies', 'cartoons') if not cfg.get(k)]
        if missing:
            print(f"Ошибка: в filter.cfg не заданы: {', '.join(missing)}")
            print(__doc__)
            sys.exit(1)
        filter_m3u(sys.argv[1], cfg['movies'], cfg['watched'], cfg['rus_movies'], cfg['cartoons'], sys.argv[2])
    elif len(sys.argv) == 7:
        filter_m3u(*sys.argv[1:])
    else:
        print(__doc__)
        sys.exit(1)
