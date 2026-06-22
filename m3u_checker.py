#!/usr/bin/env python3
"""
M3U Index Updater
==================
Reads an existing index.m3u, fetches source playlists, finds channels whose
names partially match channels already in the index, checks reachability, and
inserts working URLs as commented lines (#url) directly into the matching
channel's block — right after its existing URLs.

Requirements: Python 3.8+  —  no third-party libraries.

Usage:
    python3 m3u_checker.py [options]

Examples:
    python3 m3u_checker.py
    python3 m3u_checker.py --index my_channels.m3u
    python3 m3u_checker.py --timeout 10 --workers 20
    python3 m3u_checker.py --sources https://example.com/list.m3u
    python3 m3u_checker.py --dry-run        # preview without writing
"""

import argparse
import logging
import os
import re
import sys
import time
import threading
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


# ──────────────────────────────────────────────────────────
#  Configuration
# ──────────────────────────────────────────────────────────


SOURCE_URLS: list[str] = [
    "https://raw.githubusercontent.com/iptv-free/TV/refs/heads/FREE/TV",
    "https://raw.githubusercontent.com/Dimonovich/TV/Dimonovich/FREE/TV?m3u8",
    "https://raw.githubusercontent.com/Shamilbro1/tv/refs/heads/main/TV.m3u",
    "http://cdntv.online/high/6j95s4drt3/playlist.m3u8",
    "https://raw.githubusercontent.com/smolnp/IPTVru/gh-pages/IPTVstable.m3u8",
    "https://raw.githubusercontent.com/smolnp/IPTVru/gh-pages/IPTVmir.m3u8",
    "https://raw.githubusercontent.com/smolnp/IPTVru/gh-pages/IPTVdonor.m3u",
    "https://raw.githubusercontent.com/smolnp/IPTVru/gh-pages/IPTVru.m3u",
    "https://fas-tv.com/ip/avto.m3u",
    "https://psv4.vkuserphoto.ru/s/v1/d2/8HZfJQRJ_MeeobCju10KU0HtuRxCjT95DE_iFeuUxmODSc0pzgMyQrRvjqyATaaDlxhP-USCHBzUB8hmxTvoJjgi2JRJCFTHgA9IlqFIlMfQDLbCI9yOSsJ9lmIxsDwpPKpJHiH5kh72/N4V2MS99P39SMB.m3u8",
    "https://dl.dropboxusercontent.com/s/sbm8ttki12bhr9cuxs9oz/m3u?rlkey=ujn5573apcibg3foxhq2ja7tt",
    "https://dl.dropboxusercontent.com/s/ur595ef4cqmfst951kboh/m3u?rlkey=0cw1ficfrq0m6yg2udh16qn78",
    "https://m3url.ru/LIst_9.m3u",
    "https://m3u.ch/pl/402fdf5102aacfc997279fd904643392_78d493be9df8cf5d447946793758bfa6.m3u",
    "https://iptv.org.ua/iptv/kino-plus.m3u",
    "https://pikniktv.info/download/file.php?id=127257",
    "https://dl.dropboxusercontent.com/scl/fi/thsjb093g6wkqdnpjdc82/Sport.m3u?rlkey=cixvxk8337i11u2h6vswjepvt&st=3a9a8qym&dl=0",
    "https://raw.githubusercontent.com/iptv-org/iptv/refs/heads/master/streams/ru.m3u",
    "https://mater.com.ua/ip/avto-full.m3u",
    "https://www.mylist.at/pRVGWXL.m3u",
    "https://tva.org.ua/ip/sam/iptv.m3u",
    "https://tva.org.ua/ip/sam/avto-full.m3u",
    "https://tva.org.ua/ip/sam/avto-iptv-tva.m3u",
    "https://raw.githubusercontent.com/Sanuyyq/iptv-ot-sanaeye/refs/heads/main/neisvesmokto.m3u8",
    "https://raw.githubusercontent.com/Sanuyyq/iptv-ot-sanaeye/refs/heads/main/logavnet.m3u8",
    "https://raw.githubusercontent.com/Sanuyyq/iptv-ot-sanaeye/refs/heads/main/ilook.m3u8",
    "https://raw.githubusercontent.com/Sanuyyq/iptv-ot-sanaeye/refs/heads/main/iedem.m3u8",
    "https://raw.githubusercontent.com/Sanuyyq/iptv-ot-sanaeye/refs/heads/main/fawlok_iptv.m3u8"
]

# Channels to skip entirely (exact case-insensitive name match).
# Add any unwanted channel names here.
BLOCKLIST: set[str] = {
    "1+1",
    "24 Канал",
    "33 канал",
    "Afrobeats",
    "Al Zahra TV",
    "Al-Zahra TV Turkic",
    "Aleph News",
    "Астрахань.Ru TV",
    "Az TV",
    "Azstar TV",
    "Baden TV",
    "Balaton TV",
    "Banovina TV",
    "Baraza TV Hits",
    "Bibel TV Musik",
    "BBC News Europe",
    "Бирма Play",
    "Brazzers TV",
    "Canal Motor",
    "Can TV",
    "CGTN",
    "Das Erste",
    "Delta Tv",
    "Deutsche Welle",
    "Dorcel",
    "Elektrika TV",
    "Finest TV",
    "FTV",
    "JC1",
    "Kuwait Sport Plus",
    "Храм",
    "Гродно Плюс",
    "Qazaqstan Teatr KZ",
    "Ushba-Films",
    "Motorvision",
    "Радио Город FM",
    "Новое радио (Беларусь)",
    "Мамонтёнок",
    "Film UA Drama",
    "Primokanale Sport",
    "PX SPORTS",
    "Mahni TV AZ",
    "Сиртаки ТВ",
    "Ош ТВ",
    "kabel eins",
    "Paideuma TV"
}

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

def _load_url_blocklist(path: str = "url_blocklist.txt") -> set[str]:
    """Load URL blocklist from an external file.

    Each non-empty line that doesn't start with '#' is treated as a
    substring pattern (case-insensitive).  A bare host[:port] blocks
    every URL that contains that host, a full path blocks only that path.
    A line may contain '*' as a wildcard for any run of characters, e.g.
    '*.hh.ee' blocks any host ending in '.hh.ee'.
    """
    result: set[str] = set()
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    result.add(line)
    except FileNotFoundError:
        pass
    return result

def _wildcard_to_regex(pattern: str) -> re.Pattern:
    """Compile a blocklist pattern containing '*' into a regex.

    Every character except '*' is matched literally; '*' matches any run
    of characters (including none).  Used with re.search, so a pattern
    like '*.hh.ee' blocks any URL whose host ends in '.hh.ee', and
    'rt-*-htlive.cdn.ngenix.net' blocks every such regional host.
    """
    return re.compile(".*".join(re.escape(part) for part in pattern.split("*")))

URL_BLOCKLIST: set[str] = _load_url_blocklist()

# Name aliases: maps a source channel name to the canonical name used in index.m3u.
# Key   = name as it appears in the source playlist (case-insensitive)
# Value = name as it appears in index.m3u
ALIASES: dict[str, str] = {
    "Точка отрыва SD": "Точка отрыва",
    "TPO": "БелРос",
    "Суббота": "Суббота!",
    "Пятнитца!": "Пятница!",
    "Пятница": "Пятница!",
    "NFL Network HD": "NFL Network",
    "Ֆրեշ TV FRESH": "Fresh",
    "M Sport vpn": "Megogo Sport",
    "360": "360°",
    "Че": "Че!",
    "Т24": "Техно 24",
    "Т-24": "Техно 24",
    "Теледом ТВ HD": "Теледом",
    "Тел+ Астрахань": "Культ Медиа",
    "Таврия ТВ": "Таврия",
    "Теледом HD": "Теледом",
    "Твоё ТВ HD v1": "Твоё ТВ",
    "Твоё ТВ HD v3": "Твоё ТВ",
    "Твоё ТВ HD v4": "Твоё ТВ",
    "Твоё ТВ HD v5": "Твоё ТВ",
    "Твоё ТВ HD v6": "Твоё ТВ",
    "Твоё ТВ HD v7": "Твоё ТВ",
    "Три Ангела SD": "Три Ангела",
    "Сапфир HD": "Сапфир",
    "Рыбалка": "Рыбалка и охота",
    "Сарафан ТВ": "Сарафан",
    "СТВ Беларусь": "СТВ",
    "Своё ТВ Ставрополь": "Своё ТВ",
    "Своё ТВ Ставрополь HD": "Своё ТВ",
    "Смайл ТВ HD": "Смайл ТВ",
    "Страна FM HD": "Страна FM",
    "СТС Int": "СТС",
    "Ретро ТВ": "Ретро",
    "Россия K": "Россия Культура",
    "Россия К": "Россия Культура",
    "Рыжий тв": "Рыжий",
    "Полёт ТВ HD": "Полёт ТВ HD",
    "Про100 ТВ": "Про100",
    "Первый канал HD": "Первый канал",
    "Пятый канал": "5 канал",
    "Первый Ростовский HD": "Первый Ростовский",
    "Первый Тульский HD": "Первый Тульский",
    "K1 HD": "K1",
    "НТС HD": "НТС",
    "НТМ HD": "НТМ",
    "НТС (Севастополь)": "НТС",
    "Arena Sport 1 Premium HD": "Arena Sport 1 Premium",
    "Arena Sport 2 Premium HD": "Arena Sport 2 Premium",
    "Arena Sport 3 Premium HD": "Arena Sport 3 Premium",
    "Arena Sport 4 Premium HD": "Arena Sport 4 Premium",
    "Arena Sport 5 Premium HD": "Arena Sport 5 Premium",
    "Arena Sport 1 HD": "Arena Sport 1",
    "Arena Sport 2 HD": "Arena Sport 2",
    "Arena Sport 3 HD": "Arena Sport 3",
    "Arena Sport 4 HD": "Arena Sport 4",
    "Arena Sport 5 HD": "Arena Sport 5",
    "Arena Sport 6 HD": "Arena Sport 6",
    "Arena Sport 7 HD": "Arena Sport 7",
    "Arena Sport 8 HD": "Arena Sport 8",
    "Arena Tennis HD": "Arena Tennis",
    "Arena Adrenalin HD": "Arena Adrenalin",
    "Матч! Футбол 3 SD": "Матч! Футбол 3",
    "Матч ТВ": "Матч!",
    "Матч ТВ HD": "Матч!",
    "Матч HD": "Матч!",
    "МИР 24ᴴᴰ": "Мир 24",
    "Масон ТВᴴᴰ": "Масон ТВ",
    "МузТВ": "Муз ТВ",
    "Моя Планета HD": "Моя Планета",
    "Euro Sport 1 HD srb": "Eurosport 1 Serbia",
    "Max Sport 2 HD": "Max Sport 2",
    "Sportska TV HD": "Sportska TV",
    "Euro Sport 2 HD srb": "Eurosport 2 Serbia",
    "Max Sport 1 HD": "Max Sport 1",
    "viju TV1000 kino HD": "Viju TV1000 Kino",
    "Наш кинопоказ HD": "Наш кинопоказ",
    "Комедия HD": "Комедия",
    "SD/REX": "Русский экстрим",
    "Kinojam 1 HD": "Kinojam 1",
    "Kinojam 2 HD": "Kinojam 2",
    "Мужское кино HD": "Мужское кино",
    "Роман HD": "Роман",
    "МОСФИЛЬМ": "Мосфильм",
    "Еда HD": "Еда",
    "Планета HD": "Моя Планета",
    "Discovery Россия HD": "Discovery Россия",
    "Телепутешествия HD": "Телепутешествия",
    "Охотник и рыболов HD": "Охотник и рыболов",
    "RTG": "RTG TV",
    "ОХОТНИК": "Рыболов",
    "ПЕС И КОТ": "Пёс и Ко",
    "ДОКТОР HD": "Доктор",
    "tnt-4": "ТНТ4",
    "Супергерои": "Канал Малыш",
    "2х2": "2x2",
    "МАМА": "Мама",
    "SD/REX HD": "Русский экстрим",
    "Твоё ТВ HD Юмор": "Твоё ТВ Юмор",
    "РЫЖИЙ": "Рыжий",
    "СТС kids HD": "СТС Kids",
    "ТНТ HD": "ТНТ",
    "Матч Страна": "Матч! Страна",
    "МАТЧ ИГРА HD": "Матч! Игра",
    "МАТЧ ИГРА": "Матч! Игра",
    "Матч! Футбол 1 HD": "Матч! Футбол 1",
    "Матч! Футбол 2 HD": "Матч! Футбол 2",
    "Матч! Футбол 3 HD": "Матч! Футбол 3",
    "Матч! Арена HD": "Матч! Арена",
    "Матч! Премьер HD": "Матч! Премьер",
    "Terra Incognita": "Terra",
    "Муз союз": "Муз Союз",
    "Продвижение (Ленинск-Кузнецкий)": "Продвижение",
    "РБК ТВ (Краснодар)": "РБК",
    "РБК HD": "РБК",
    "Китай ТВ HD": "Китай ТВ",
    "Кино ТВ HD": "Кино ТВ",
    "КИНОПРЕМЬЕРА HD": "Кинопремьера",
    "Киносаидание": "Киносвидание",
    "Оплот-ТВ HD": "Оплот ТВ",
    "НТВ HD": "НТВ",
    "Нано ТВ": "Нано",
    "Мульт HD": "Мульт",
    "Первый HD": "Первый канал",
    "Матч Премьер": "Матч! Премьер",
    "Нано ТВ HD": "Нано",
    "Победа HD": "Победа",
    "Пятница HD": "Пятница!",
    "Aiva HD": "Aiva",
    "Bridge HD": "Bridge",
    "Das Erste Ⓖ": "Das Erste",
    "Das Erste HD": "Das Erste",
    "Конгресс ТВ HD": "Конгресс ТВ",
    "КОНГРЕСС ТВ SD": "Конгресс ТВ",
    "Пингвин Лоло": "Пингвин",
    "Пятый канал Int.": "5 канал",
    "СТРК HD": "СТРК",
    "КХЛ HD": "KHL",
    "025 Россия РТР": "Planeta RTR",
    "026 Россия К": "Россия Культура",
    "028 Первый канал": "Первый канал",
    "Planeta RTR": "РТР-Планета",
    "032 ТНТ Comedy": "ТНТ Comedy",
    "031 Дикая рыбалка": "Дикая рыбалка",
    "027 MIR 24": "Мир 24",
    "030 Рен ТВ": "РЕН ТВ",
    "033 ТНТ4  International": "ТНТ4",
    "038 Дикая охота": "Дикая охота",
    "036 Поехали!": "Поехали!",
    "040 НТВ Право": "НТВ Право",
    "034 РБК": "РБК",
    "039 НТВ Мир": "НТВ Мир",
    "037 5 international": "5 канал",
    "043 Перец": "Перец",
    "044  Mir": "Мир",
    "047 Оружие": "Оружие",
    "046 Ретро": "Ретро",
    "048 Арсенал": "Арсенал",
    "049 Победа": "Победа",
    "050 Кто есть кто": "Кто есть кто",
    "Тонус ТВ": "Тонус",
    "052  Tonus": "Тонус",
    "053 Телепутешествия": "Телепутешествия",
    "054 Amedia Hit HD": "Amedia Hit",
    "060 Amedia Premium HD": "Amedia Premium",
    "059 ТВ-3": "ТВ-3",
    "063 A2": "Amedia 2",
    "062 A1": "Amedia 1",
    "065 Киносерия": "Киносерия",
    "066 Киносемья": "Киносемья",
    "064 Киномикс": "Киномикс",
    "061 FOX": "Fox",
    "067 Киносвидание": "Киносвидание",
    "070 Кинохит": "Кинохит",
    "068 Кинокомедия": "Кинокомедия",
    "072 Родное Кино": "Родное Кино",
    "069 Кинопремьера": "Кинопремьера",
    "071 Мужское Кино": "Мужское Кино",
    "073  Наше Новое Кино": "Наше Новое Кино",
    "074  Индийское кино": "Индийское кино",
    "Индия ТВ": "Индия",
    "075  Индия": "Индия",
    "076  Дом кино": "Дом кино",
    "077  Дом Кино Премиум": "Дом Кино Премиум",
    "078 Киноужас": "Киноужас",
    "079 Музыка Первого": "Музыка Первого",
    "083 Match Planeta": "Матч! Планета",
    "086 Шансон ТВ": "Шансон ТВ",
    "085 БОКС ТВ ПЛЮС": "Бокс ТВ",
    "094 Муз ТВ": "Муз ТВ",
    "093 BRiDGE TV Russki Hits": "BRiDGE TV Русский Хит",
    "097 СТС Kids": "СТС Kids",
    "098 Карусель": "Карусель",
    "100 O!": "O!",
    "103 Авто Плюс": "Авто Плюс",
    "107 Bridge TV": "Bridge TV",
    "105 National Geographic": "National Geographic",
    "108 Охотник и рыболов": "Охотник и рыболов",
    "110 Зоо ТВ": "Зоо ТВ",
    "109 Загородный": "Загородный",
    "112 Eurosport 2": "Eurosport 2",
    "111 Eurosport 1": "Eurosport 1",
    "113 Animal Planet": "Animal Planet",
    "115 IstoriaTV": "365 дней",
    "6ter HD": "6ter,",
    "CT SPORT HD": "CT SPORT",
    "Астрахань 24 (720p)": "Астрахань 24",
    "Kino 24 (720p)": "Kino 24",
    "Астрахань.Ru TV (480p)": "Астрахань.Ru TV",
    "Астрахань.Ru Sport (720p)": "Астрахань.Ru Sport",
    "Нано ТВ HD (576p)": "Нано",
    "Univer TV (1080p) [Not 24/7]": "Univer TV",
    "Открытый мир. Здоровье (576p)": "Открытый мир",
    "Мультимания ТВ (576p)": "Мультимания",
    "РБК (СПБ) (576p)": "РБК",
    "Живи": "Живи Активно",
    "Достор": "Доктор",
    "Блокбастер HD": "Блокбастер",
    "Арсенал HD": "Арсенал",
    "ТВ3 HD": "ТВ3",
    "Дождь HD": "Дождь",
    "Истоки Орёл": "Истоки",
    "БСТ Братск": "БСТ",
    "CARTOON NETWORK HD": "Cartoon Network",
    "GOLF CHANNEL": "Golf Channel",
    "БЕЛАРУСЬ 24 HD": "Беларусь 24",
    "БЕЛАРУСЬ 3 SD": "Беларусь 3",
    "КиноСезонᴴᴰ": "КиноСезон",
    "World Fashion Channelᴴᴰ ru": "World Fashion Channel Russia",
    "Матч! HD": "Матч!",
    "МОЙ МИР SD": "Мой мир",
    "Кухня ТВ": "Кухня",
    "Неизвестная планета HD": "Неизвестная планета",
    "Открытый мир FD": "Открытый мир",
    "Океан ТВ": "Океан",
    "Willow HD": "Willow",
    "Советское Кино SD": "Советское Кино",
    "Channel 8 International (576p)": "8tv",
    "Viasat Kino Comedy HD": "Viasat Kino Comedy",
    "ЭХО ТВ 24 (Новоуральск)": "ЭХО ТВ 24",
    "360.RU НОВОСТИᴴᴰ": "360°",
    "360.RU SD": "360°",
    "Al Jazeera English": "Al Jazeera",
    "Al Jazeeraᴴᴰ (Arabic)": "Al Jazeera (Arabic)",
    "BBC World Newsᴴᴰ": "BBC World News",
    "CCTV4ᴴᴰ": "CCTV4",
    "Первый канал Европа": "Первый канал",
    "РАДИО ГОРОД FMᴴᴰ": "Радио Город FM",
    "Культ Медиа ТВ": "Культ Медиа",
    "Астрахань 24 SD": "Астрахань 24",
    "ОЦЕᴴᴰ": "Ош ТВ",
    "312 Кино 🇰🇬": "312 Кино",
    "312 Сериал 🇰🇬": "312 Сериал",
    "Regionᴴᴰ 🇰🇬": "Region",
    "Арсенал FD": "Арсенал",
    "КиноКлассика SD": "КиноКлассика",
    "ТелеНовелла SD": "ТелеНовелла",
    "КиноДок SD*": "КиноДок",
    "Meditation Music SD*": "Meditation Music",
    "Сити Эдем ТВ SD": "Сити Эдем ТВ",
    "Terra Incognitaᴴᴰ": "Terra Incognita",
    "СИТИ ЭДЕМ Киноновелла SD": "Киноновелла",
    "Инсайт ТВᴴᴰ": "Инсайт ТВ",
    "Star Cinemaᴴᴰ": "Star Cinema",
    "Полёт ТВᴴᴰ": "Полёт ТВ",
    "Китай ТВᴴᴰ": "Китай ТВ",
    "8 канал International": "8tv",
    "Теледом SD": "Теледом",
    "ОК": "8tv",
    "Телеканал ОК": "8tv",
    "NikNik.TV SD": "NikNik.TV",
    "ASTRAKHAN.RU SPORTᴴᴰ": "Астрахань.Ru Sport",
    "ASTRAKHAN.RU SPORT": "Астрахань.Ru Sport",
    "ACI Sport SD": "ACI Sport",
    "Fun Roads SD": "Fun Roads",
    "Invivo Extreme SD": "Invivo Extreme",
    "Invivo Auto SD": "Invivo Auto",
    "ACI Sport TV SD": "ACI Sport",
    "STAR SPORTS 1 HD": "Star Sports 1",
    "DSPORTS 1 HD": "DSPORTS 1",
    "TVR Sport FHD": "TVR Sport",
    "Kuwait Sport HD": "Kuwait Sport",
    "Kuwait Sport Plus HD": "Kuwait Sport Plus",
    "ESPN 1 SD": "ESPN 1",
    "Canal Motor SD": "Canal Motor",
    "World Fashion Channel SD ru": "World Fashion Channel Russia",
    "World Fashion Channel SD": "World Fashion Channel",
    "World Fashion Channelᴴᴰ": "World Fashion Channel",
    "Fashion TV Paris L'Originalᴴᴰ": "Fashion TV Paris L'Original",
    "ВИТРИНА ТВ FD": "Витрина ТВ",
    "LUXURYᴴᴰ": "Luxury",
    "TBN BALTIAᴴᴰ": "TBN Baltia",
    "TBN Armeniaᴴᴰ": "TBN Armenia",
    "Бог Благ ТВᴴᴰ": "Бог Благ ТВ",
    "БогБлагТВᴴᴰ": "Бог Благ ТВ",
    "TV ХРАМ": "Храм",
    "ТВ МАНА ВАШᴴᴰ": "ТВ Мана Ваш",
    "Пловдивска Православна ТВᴴᴰ": "Пловдивска Православна ТВ",
    "Global Fashionᴴᴰ": "Global Fashion",
    "Күңел ТВ HD": "Күңел ТВ",
    "Моя планета SD": "Моя планета",
    "НТВ Шефᴴᴰ": "НТВ Шеф",
    "Viju+ Serial HD": "Viju+ Serial",
    "KiKA HD": "KiKA",
    "Алмазный край HD": "Алмазный край",
    "360.RU НОВОСТИ": "360.RU Новости",
    "Fox News SD": "Fox News",
    "BBC News Europe HD": "BBC News Europe",
    "Trashᴴᴰ": "Trash",
    "Sport UZ HD": "Sport UZ",
    "PX SPORTS": "PX Sports",
    "AFROBEATSᴴᴰ": "Afrobeats",
    "PRO100": "Про100",
    "ОТР HD": "ОТР",
    "019 NUR": "ТВ Спорт",
    "КиноМенюᴴᴰ": "КиноМеню",
    "BBC WN": "BBC World News",
    "33 канал (Хмельницький)": "33 канал",
    "1+1 UA": "1+1",
    "1+1 Україна": "1+1",
    "1+1 Україна (UA)": "1+1",
    "JC1 HD": "JC1",
    "Elektrika TV HD": "Elektrika TV",
    "Eurosport1HD": "Eurosport 1",
    "Eurosport2HD": "Eurosport 2",
    "Dorcel HD": "Dorcel",
    "CGTN SD": "CGTN",
    "КиноЭкшен SD*": "КиноЭкшен",
    "Afrobeats TV": "Afrobeats",
    "Meditation Musicᴴᴰ": "Meditation Music",
    "Мульт SD": "Мульт",
    "РецептыГурмана": "Рецепты Гурмана",
    "Сити Эдем Бирма Play": "Бирма Play",
    "КиноКлассика HD": "КиноКлассика",
    "Deutsche Welle HD": "Deutsche Welle",
    "Зал Суда HD": "Зал суда",
    "City Eden Birma Play HD": "Бирма Play",
    "TV Centr (1080p)": "ТВЦ",
    "REN TV HD (1080p)": "РЕН ТВ",
    "Friday! (1080p)": "Пятница!",
    "365 дней ТВ": "365 дней",
    "RTG HD": "RTG TV",
    "Инсайт ТВ UHD": "Инсайт ТВ",
    "Седьмой канал KZ": "Седьмой канал",
    "Fashion & Lifestyle HD": "Fashion & Lifestyle",
    "Start air": "Start Air",
    "Наше HD": "Наше",
    "Хит HD": "Хит",
    "Bridge HD": "Bridge TV",
    "Ля минор": "Ля-минор",
    "Бирма Плей": "Бирма Play",
    "Классика Кино SD": "Классика Кино",
    "Movie Classic": "Классика Кино",
    "Fashion TV Paris L'Original": "Fashion TV",
    "LIVETV SD": "LIVETV",
    "ЕВРОКИНО": "Еврокино",
    "Love Nature 4k": "Love Nature",
    "ACI Sport TV": "ACI Sport",
    "Мультимания (576p)": "Мультимания",
    "Продвижение (Новокузнецк)": "Продвижение",
    "СТВ HD": "СТВ",
    "7tv": "7 TV"
}

DEFAULT_INDEX_FILE  = "index.m3u"
LOG_FILE            = "m3u_checker.log"
DEFAULT_TIMEOUT_SEC = 8
DEFAULT_WORKERS     = 30

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


@dataclass
class Stats:
    sources_ok:   int = 0
    sources_fail: int = 0
    parsed:       int = 0
    candidates:   int = 0
    reachable:    int = 0
    unreachable:  int = 0
    errors:       int = 0
    inserted:     int = 0
    appended:     int = 0
    _lock:  threading.Lock = field(default_factory=threading.Lock, repr=False)
    _start: float          = field(default_factory=time.time, repr=False)

    def inc(self, **kwargs):
        with self._lock:
            for k, v in kwargs.items():
                setattr(self, k, getattr(self, k) + v)

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
        fh = logging.FileHandler(log_file, encoding="utf-8")
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


def channel_matches(src_name: str, src_tvg_id: str,
                    idx_name: str, idx_tvg_id: str) -> bool:
    """
    Match a source channel against an index channel.
    Criteria (any one is enough):
      • Exact case-insensitive name match
      • Exact case-insensitive tvg-id match (only when both sides are non-empty)
    """
    if src_name.strip().lower() == idx_name.strip().lower():
        return True
    if src_tvg_id and idx_tvg_id:
        if src_tvg_id.lower() == idx_tvg_id.lower():
            return True
    return False


# ──────────────────────────────────────────────────────────
#  index.m3u parsing and writing
# ──────────────────────────────────────────────────────────

def parse_index_m3u(path: str, log: logging.Logger) -> tuple[list[str], list[IndexBlock]]:
    """
    Parse the local index.m3u.

    Returns:
        header_lines  — lines before the first #EXTINF block (e.g. #EXTM3U)
        blocks        — list of IndexBlock, one per channel
    """
    if not os.path.exists(path):
        log.warning(f"Index file not found: {path}")
        return ["#EXTM3U\n"], []

    with open(path, encoding="utf-8") as f:
        raw_lines = f.readlines()

    header_lines: list[str] = []
    blocks: list[IndexBlock] = []
    current_block_lines: list[str] = []
    in_block = False

    def _finish_block(blines: list[str]) -> Optional[IndexBlock]:
        """Turn accumulated lines into an IndexBlock."""
        extinf = next((l for l in blines if l.strip().upper().startswith("#EXTINF")), None)
        if not extinf:
            return None
        extinf_s = extinf.strip()
        name   = _clean_name(_parse_extinf_name(extinf_s))
        tvg_id = extract_tvg_id(extinf_s)

        # Remove any URL lines that match URL_BLOCKLIST
        url_block_lower = {p.lower() for p in URL_BLOCKLIST}
        if url_block_lower:
            cleaned: list[str] = []
            for l in blines:
                stripped = l.strip()
                if stripped.startswith("#"):
                    candidate = stripped.lstrip("#").strip()
                else:
                    candidate = stripped
                if candidate.startswith(("http://", "https://", "rtmp")):
                    if any(pat in candidate.lower() for pat in url_block_lower):
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
        return IndexBlock(lines=blines, name=name, tvg_id=tvg_id, urls=urls)

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

    log.info(f"📂 Parsed index.m3u: {len(blocks)} channel blocks")
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

    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
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

    # MPEG-TS: sync byte 0x47 ('G')
    if data[0] == 0x47:
        # Дополнительная уверенность: второй sync byte через 188 байт
        if len(data) >= 189 and data[188] == 0x47:
            return True
        return True  # Одного sync byte достаточно

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
        with urllib.request.urlopen(req, timeout=timeout * 3) as resp:
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


def check_stream(ch: SourceChannel, timeout: int, log: logging.Logger) -> SourceChannel:
    """
    Двухэтапная проверка URL на наличие реального медиапотока.

    Этап 1 — HEAD (быстро, без скачивания тела):
      - Статус >= 400 → ошибка.
      - Content-Type = text/html → ошибка (геоблок, авторизационная стена).
      - Content-Type = известный медиатип (audio/*, video/*, *mpegurl) → ok.
      - Content-Type = application/octet-stream или пустой → нужна проверка байт.
      - HTTP 405 / ошибка сети → переход к GET.

    Этап 2 — GET с чтением первых байт:
      - Статус >= 400 → ошибка.
      - Content-Type = text/html → ошибка.
      - URL заканчивается на .m3u8 или Content-Type = *mpegurl →
        HLS-валидация: парсим плейлист, проверяем наличие сегментов.
      - Иначе: читаем первые 1024 байт, проверяем magic-сигнатуру.
        stream_verified=True если magic совпал.
        reachable=True консервативно даже без magic (статус 200 + не HTML).
    """
    t0 = time.time()
    is_hls = ch.url.lower().split("?")[0].endswith(".m3u8")

    # ── Этап 1: HEAD ──────────────────────────────────────────────
    need_get = True
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

            # Чёткий медиатип (не octet-stream) и не HLS — ok без GET
            if mime in _STREAM_CONTENT_TYPES and mime != "application/octet-stream" \
                    and not is_hls:
                ch.check_ms = (time.time() - t0) * 1000
                ch.content_type = mime
                ch.reachable = True
                log.debug(
                    f"   ✅ [HEAD] HTTP {code}  mime={mime}  "
                    f"{ch.check_ms:.0f}ms  {ch.name!r}"
                )
                return ch

            # Иначе (octet-stream, пустой CT, HLS) → нужен GET с байтами
            need_get = True

    except urllib.error.HTTPError as e:
        ch.http_status = e.code
        if e.code == 405:
            log.debug(f"   HEAD→405, retry GET: {ch.url}")
        else:
            ch.check_ms = (time.time() - t0) * 1000
            ch.reachable = False
            ch.check_error = f"HTTP {e.code}"
            log.debug(f"   ❌ [HEAD] HTTP {e.code}  {ch.check_ms:.0f}ms  {ch.name!r}")
            return ch
    except (urllib.error.URLError, TimeoutError, Exception) as e:
        # Сеть упала на HEAD — всё равно пробуем GET
        log.debug(f"   HEAD error ({type(e).__name__}), trying GET: {ch.url}")

    if not need_get:
        return ch

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

            # Обычный поток: читаем первые 1024 байт и проверяем magic
            first_bytes = resp.read(1024)
            ch.check_ms = (time.time() - t0) * 1000
            ch.stream_verified = _is_stream_magic(first_bytes)

            # Известный медиатип (не octet-stream) → ok независимо от magic
            if mime in _STREAM_CONTENT_TYPES and mime != "application/octet-stream":
                ch.reachable = True
            elif ch.stream_verified:
                # Magic подтвердил формат
                ch.reachable = True
            else:
                # Неизвестный тип, magic не совпал — консервативно ok
                # Для строгого режима: ch.reachable = False
                ch.reachable = True
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
        ch.check_error = str(e.reason)
        log.debug(f"   🔌 URLError: {e.reason}  {ch.name!r}")
    except TimeoutError:
        ch.reachable = False
        ch.check_error = "timeout"
        log.debug(f"   ⏱️  Timeout  {ch.name!r}")
    except Exception as e:
        ch.reachable = False
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

def find_matching_block(
    src_ch: SourceChannel,
    blocks: list[IndexBlock],
    log: logging.Logger,
) -> Optional[IndexBlock]:
    """Return the first IndexBlock matching the source channel (by name or tvg-id)."""
    for blk in blocks:
        if channel_matches(src_ch.name, src_ch.tvg_id, blk.name, blk.tvg_id):
            reason = (
                f"tvg-id={src_ch.tvg_id!r}"
                if src_ch.tvg_id and src_ch.tvg_id.lower() == blk.tvg_id.lower()
                else f"name={src_ch.name!r}"
            )
            log.debug(f"   MATCH [{reason}]: src={src_ch.name!r} ↔ idx={blk.name!r}")
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
    dry_run: bool = False,
) -> int:
    """
    Append all reachable source channels to the end of path as a 'test' group.
    Channels from multiple sources with the same name/tvg-id are grouped:
      - first URL  → active (no #)
      - the rest   → commented alternatives (#url)
    Skips URLs already anywhere in the file. Returns count of new URL lines written.
    """
    existing_urls = collect_all_file_urls(path)
    log.info(f"   URLs already in file: {len(existing_urls)}")

    # Filter out already-present URLs
    new_pairs = [(ch, blk) for ch, blk in pairs if ch.url not in existing_urls]
    skipped = len(pairs) - len(new_pairs)
    log.info(f"   New URLs to append  : {len(new_pairs)}  (skipped duplicates: {skipped})")

    if not new_pairs:
        log.info("   Nothing new to write.")
        return 0

    # Group by channel identity; preserve insertion order
    from collections import OrderedDict
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
    args = parser.parse_args()

    log_file = None if str(args.log).lower() == "none" else args.log
    log = setup_logging(log_file)
    sources = args.sources or SOURCE_URLS
    stats = Stats()

    log.info("=" * 60)
    log.info("🚀 M3U Index Updater started")
    log.info(f"   Python   : {sys.version.split()[0]}")
    log.info(f"   Index    : {args.index}")
    log.info(f"   Sources  : {len(sources)}")
    log.info(f"   Timeout  : {args.timeout}s  |  Workers: {args.workers}")
    if args.dry_run:
        log.info("   DRY RUN  : file will NOT be modified")
    log.info("=" * 60)

    # ── Step 1: Parse existing index.m3u ────────────────────────────────────
    log.info("")
    log.info("STEP 1 — Reading index.m3u")
    log.info("-" * 60)

    header_lines, blocks = parse_index_m3u(args.index, log)

    if not blocks:
        log.error("❌ index.m3u has no channel blocks. Nothing to match against. Exiting.")
        sys.exit(1)

    log.info(f"   Found {len(blocks)} channel(s) in index:")
    for b in blocks:
        log.info(f"   • {b.name!r}  ({len(b.urls)} existing URL(s))")

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
        blocklist_lower     = {b.lower() for b in BLOCKLIST}
        aliases_lower       = {k.lower(): v for k, v in ALIASES.items()}

        # URL blocklist: plain substrings vs. wildcard ('*') patterns.
        url_block_plain    = {p.lower() for p in URL_BLOCKLIST if "*" not in p}
        url_block_wildcard = [
            _wildcard_to_regex(p.lower()) for p in URL_BLOCKLIST if "*" in p
        ]

        def _is_url_blocked(ch) -> bool:
            if not url_block_plain and not url_block_wildcard:
                return False
            url_lc = ch.url.lower()
            if any(pat in url_lc for pat in url_block_plain):
                return True
            return any(rx.search(url_lc) for rx in url_block_wildcard)

        def _is_name_pattern_blocked(ch) -> bool:
            return any(p.search(ch.name) for p in BLOCKLIST_PATTERNS)

        filtered = [
            ch for ch in found
            if ch.name.strip().lower() not in blocklist_lower
            and not _is_url_blocked(ch)
            and not _is_name_pattern_blocked(ch)
        ]
        blocked = len(found) - len(filtered)
        log.info(f"   → Parsed {len(found)} channel(s)  (blocked: {blocked})")
        for ch in found:
            if ch.name.strip().lower() in blocklist_lower:
                log.debug(f"   BLOCKED (name): {ch.name!r}")
            elif _is_url_blocked(ch):
                log.debug(f"   BLOCKED (url): {ch.name!r}  {ch.url!r}")
            elif _is_name_pattern_blocked(ch):
                log.debug(f"   BLOCKED (pattern): {ch.name!r}")
        # Apply aliases: rename source channel name to canonical index name
        for ch in filtered:
            canonical = aliases_lower.get(ch.name.strip().lower())
            if canonical:
                log.debug(f"   ALIAS: {ch.name!r} → {canonical!r}")
                ch.name = canonical
        all_source_channels.extend(filtered)

    stats.parsed = len(all_source_channels)
    log.info("")
    log.info(f"📊 Sources: {stats.sources_ok} ok / {stats.sources_fail} failed")
    log.info(f"📊 Total source channels: {stats.parsed}")

    if not all_source_channels:
        log.error("❌ No channels found in any source. Exiting.")
        sys.exit(1)

    # ── Step 3: Match source channels to existing index blocks ──────────────
    log.info("")
    log.info("STEP 3 — Matching source channels to existing index blocks")
    log.info("-" * 60)

    # Pairs (source_channel, index_block) where URL is new and block matches
    update_candidates: list[tuple[SourceChannel, IndexBlock]] = []

    for src_ch in all_source_channels:
        blk = find_matching_block(src_ch, blocks, log)
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

    done_count = 0
    total = len(all_to_check)
    lock = threading.Lock()
    checked_map: dict[str, SourceChannel] = {}  # url → checked channel

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(check_stream, ch, args.timeout, log): ch
                   for ch in all_to_check}
        for future in as_completed(futures):
            ch = future.result()
            checked_map[ch.url] = ch

            with lock:
                done_count += 1
                if ch.reachable:
                    stats.inc(reachable=1)
                elif ch.check_error:
                    stats.inc(errors=1)
                else:
                    stats.inc(unreachable=1)

                if done_count % 25 == 0 or done_count == total:
                    pct = done_count / total * 100
                    log.info(
                        f"   {done_count}/{total} ({pct:.0f}%)  "
                        f"✅ {stats.reachable}  ❌ {stats.unreachable}  ⚠️  {stats.errors}"
                    )

    # ── Step 5a: Insert new URLs into matching existing blocks ───────────────
    log.info("")
    log.info("STEP 5a — Updating existing channel blocks with new URLs")
    log.info("-" * 60)

    for src_ch, blk in update_candidates:
        ch = checked_map.get(src_ch.url, src_ch)
        if not ch.reachable:
            log.debug(f"   Skip (unreachable): {ch.url}")
            continue
        inserted = insert_url_into_block(blk, ch.url, log)
        if inserted:
            stats.inserted += 1
            log.info(
                f"   ✅ {blk.name!r}  ←  #{ch.url}"
                f"  [{ch.http_status}, {ch.check_ms:.0f}ms]"
            )

    # Write updated blocks back to file
    if stats.inserted > 0 or True:  # always rewrite to keep file clean
        write_index_m3u(args.index, header_lines, blocks, log, dry_run=args.dry_run)

    # ── Step 5b: Append ALL reachable source channels to test group ──────────
    log.info("")
    log.info("STEP 5b — Appending ALL reachable source channels to 'test' group")
    log.info("-" * 60)

    # Build list of all reachable channels (with their matched block or None)
    block_by_url: dict[str, IndexBlock] = {
        src_ch.url: blk for src_ch, blk in update_candidates
    }

    all_reachable_pairs: list[tuple[SourceChannel, Optional[IndexBlock]]] = [
        (ch, block_by_url.get(ch.url)) for ch in checked_map.values()
        if ch.reachable and ch.name.strip().lower() not in blocklist_lower
   ]

    stats.appended = append_test_group(
        args.index, all_reachable_pairs, log, dry_run=args.dry_run
    )

    # ── Summary ──────────────────────────────────────────────────────────────
    log.info("")
    log.info("=" * 60)
    log.info("SUMMARY")
    log.info("=" * 60)
    log.info(f"  Sources fetched         : {stats.sources_ok}  (failed: {stats.sources_fail})")
    log.info(f"  Source channels (total) : {stats.parsed}  (unique: {len(all_to_check)})")
    log.info(f"  ✅ Reachable            : {stats.reachable}")
    log.info(f"  ❌ Unreachable          : {stats.unreachable}")
    log.info(f"  ⚠️  Errors               : {stats.errors}")
    log.info(f"  🔗 Matched existing     : {stats.candidates}  → inserted: {stats.inserted}")
    log.info(f"  🧪 Appended to test     : {stats.appended} URL(s)")
    log.info(f"  📄 Index file           : {args.index}")
    log.info(f"  ⏱️  Total time           : {stats.elapsed}")
    log.info("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n⛔ Interrupted by user.", file=sys.stderr)
        sys.exit(1)
