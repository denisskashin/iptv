#!/usr/bin/env python3
"""
Фильтрует source.m3u: пропускает записи, у которых название или ссылка
уже есть в movies.m3u, watched.m3u, rus_movies.m3u или cartoons.m3u,
а также записи, чья ссылка ведёт на сайты из blocked_sites.
Сравнение названий — без учёта регистра и без года.

Использование (полное):
    python3 filter.py <source.m3u> <movies.m3u> <watched.m3u> <rus_movies.m3u> <cartoons.m3u> <output.m3u>

Использование (с конфигом filter.cfg):
    python3 filter.py <source.m3u> <output.m3u>

filter.cfg — файл рядом со скриптом, формат:
    movies        = movies.m3u
    watched       = watched.m3u
    rus_movies    = rus_movies.m3u
    cartoons      = cartoons.m3u
    blocked_sites = ashdi.vip, somesite.com
                    anothersite.net
"""

import re
import sys
import configparser
from pathlib import Path


def normalize(title: str) -> str:
    # убираем скобки с 4-значным годом: (2015), (США 2015), (US 2015), (1980)
    t = re.sub(r'\s*\([^)]*\b\d{4}\b[^)]*\)\s*', ' ', title)
    t = re.sub(r'\s+(RU|EN|UA|VO)\s*$', '', t, flags=re.IGNORECASE)
    return re.sub(r'\s+', ' ', t).strip().lower()


def _usable_title(n: str) -> bool:
    """Отсеивает мусорные «названия»: пустые и чисто числовые
    (номера серий вроде 18, 20, 164 в cartoons.m3u), которые
    по подстроке ложно совпадают с годами в названиях фильмов."""
    return bool(n) and not n.isdigit()


def extract_normalized_titles(filepath: str) -> set:
    titles = set()
    with open(filepath, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line.startswith('#EXTINF'):
                title = line.split(',', 1)[-1].strip() if ',' in line else ''
                if title:
                    n = normalize(title)
                    if _usable_title(n):
                        titles.add(n)
    return titles


def extract_titles_with_urls(filepath: str) -> set:
    """Возвращает нормализованные названия только тех записей, у которых есть URL-источник."""
    titles = set()
    with open(filepath, encoding='utf-8') as f:
        lines = f.readlines()
    for i, line in enumerate(lines):
        line = line.strip()
        if line.startswith('#EXTINF') and ',' in line:
            title = line.split(',', 1)[-1].strip()
            if title and i + 1 < len(lines):
                next_line = _strip_comment(lines[i + 1].strip())
                if next_line.startswith('http'):
                    n = normalize(title)
                    if _usable_title(n):
                        titles.add(n)
    return titles


def _strip_comment(line: str) -> str:
    """Убирает ведущий # у закомментированных ссылок вида #http://... и #https://..."""
    return line.lstrip('#') if line.startswith('#http') else line


def extract_urls(filepath: str) -> set:
    urls = set()
    with open(filepath, encoding='utf-8') as f:
        for line in f:
            line = _strip_comment(line.strip())
            if line.startswith('http'):
                urls.add(line)
    return urls


# Домены, для которых совпадение идёт только по имени файла (токен пользователя игнорируется).
# Паттерн URL: https://<domain>/f/<token>/<filename>
CDN_TOKEN_DOMAINS = {'m.cdntv.online'}


def cdn_filename(url: str) -> str | None:
    """Для известных CDN-доменов возвращает только имя файла (последний сегмент пути).
    Для остальных URL возвращает None."""
    host = re.sub(r'^https?://', '', url).split('/')[0].split(':')[0].lower()
    if host in CDN_TOKEN_DOMAINS:
        return url.rstrip('/').split('/')[-1].lower()
    return None


def extract_cdn_filenames(filepath: str) -> set:
    """Собирает имена файлов CDN-ссылок из m3u-файла."""
    names = set()
    with open(filepath, encoding='utf-8') as f:
        for line in f:
            line = _strip_comment(line.strip())
            if line.startswith('http'):
                name = cdn_filename(line)
                if name:
                    names.add(name)
    return names


def title_matches_any(n: str, *known_sets) -> bool:
    """Возвращает True, если n содержит любое известное название или наоборот."""
    if not n:
        return False
    for known in known_sets:
        for k in known:
            if k in n or n in k:
                return True
    return False


def is_blocked_site(url: str, blocked_sites: set) -> bool:
    """Возвращает True, если домен URL совпадает с одним из заблокированных сайтов."""
    if not url or not blocked_sites:
        return False
    # Убираем схему (http://, https://) и берём хост
    host = re.sub(r'^https?://', '', url).split('/')[0].split(':')[0].lower()
    for site in blocked_sites:
        site = site.lower()
        # Совпадает точно или является субдоменом
        if host == site or host.endswith('.' + site):
            return True
    return False


def filter_m3u(source: str, movies_file: str, watched_file: str, rus_movies_file: str, cartoons_file: str, output: str, blocked_sites: set = None):
    # watched: пропускаем по названию всегда (вне зависимости от наличия источника)
    watched_norm = extract_normalized_titles(watched_file)

    # movies/cartoons/rus_movies: пропускаем по названию только если у записи есть URL-источник;
    # если источника нет — оставляем в filtered (вдруг источник появится)
    movies_with_url     = extract_titles_with_urls(movies_file)
    rus_movies_with_url = extract_titles_with_urls(rus_movies_file)
    cartoons_with_url   = extract_titles_with_urls(cartoons_file)

    known_urls = (
        extract_urls(movies_file) |
        extract_urls(watched_file) |
        extract_urls(rus_movies_file) |
        extract_urls(cartoons_file)
    )

    known_cdn_filenames = (
        extract_cdn_filenames(movies_file) |
        extract_cdn_filenames(watched_file) |
        extract_cdn_filenames(rus_movies_file) |
        extract_cdn_filenames(cartoons_file)
    )

    blocked_sites = blocked_sites or set()

    out_lines = ['#EXTM3U\n\n']
    cnt_new = cnt_skip = cnt_blocked = 0

    with open(source, encoding='utf-8') as f:
        lines = [l for l in f.readlines() if not l.strip().startswith('#EXTGRP')]

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

            # watched — всегда пропускаем по названию
            title_in_watched = title_matches_any(n, watched_norm)
            # movies/cartoons/rus_movies — пропускаем по названию только если там есть источник
            title_in_others  = title_matches_any(n, movies_with_url, rus_movies_with_url, cartoons_with_url)

            url_cdn_name = cdn_filename(url) if url else None
            url_known = (
                (url in known_urls) or
                (url_cdn_name is not None and url_cdn_name in known_cdn_filenames)
            ) if url else False
            site_blocked = is_blocked_site(url, blocked_sites)

            if site_blocked:
                cnt_blocked += 1
            elif title_in_watched or title_in_others or url_known:
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

    print(f"Источник: {source}  ({cnt_new + cnt_skip + cnt_blocked} фильмов)")
    print(f"  → Итого в {output}: {cnt_new}")
    print(f"  Пропущено (название или ссылка уже есть в файлах): {cnt_skip}")
    if cnt_blocked:
        print(f"  Пропущено (заблокированные сайты): {cnt_blocked}")


def load_config() -> dict:
    cfg_path = Path(__file__).parent / 'filter.cfg'
    if not cfg_path.exists():
        return {}
    cfg = configparser.ConfigParser()
    cfg.read(cfg_path, encoding='utf-8')
    section = cfg['filter'] if 'filter' in cfg else cfg.defaults()

    # Парсим blocked_sites: поддерживаем запятые и переносы строк
    raw_blocked = section.get('blocked_sites', '')
    blocked = set()
    for part in re.split(r'[\n,]+', raw_blocked):
        part = part.strip()
        if part:
            blocked.add(part.lower())

    return {
        'movies':        section.get('movies'),
        'watched':       section.get('watched'),
        'rus_movies':    section.get('rus_movies'),
        'cartoons':      section.get('cartoons'),
        'blocked_sites': blocked,
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
        filter_m3u(sys.argv[1], cfg['movies'], cfg['watched'], cfg['rus_movies'], cfg['cartoons'], sys.argv[2],
                   blocked_sites=cfg.get('blocked_sites', set()))
    elif len(sys.argv) == 7:
        filter_m3u(*sys.argv[1:])
    else:
        print(__doc__)
        sys.exit(1)
