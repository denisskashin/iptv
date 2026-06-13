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
    "https://tva.org.ua/ip/sam/avto-iptv-tva.m3u"
]

# Channels to skip entirely (exact case-insensitive name match).
# Add any unwanted channel names here.
BLOCKLIST: set[str] = {
    "#Ё (Самара)",
    ".black HD",
    ".Black",
    ".red HD",
    ".red",
    ".sci-fi HD",
    ".sci-fi",
    "1 Radio",
    "1+1 HD",
    "1+1 UA",
    "1+1 Марафон UA",
    "1+1 Украина HD",
    "1+1 Україна (UA)",
    "1+1 Україна",
    "1+1",
    "10 Канал (Мордовия)",
    "10 канал (Новокузнецк)",
    "101_BNT 1 HD",
    "102_BNT 2 HD",
    "103_bTV HD",
    "104_bTV Action HD",
    "105_bTV Cinema",
    "111_bTV Comedy HD",
    "112_bTV Story",
    "113_NOVA HD",
    "114_Nova News HD",
    "115_Kino Nova",
    "12 КАНАЛ (ОМСК)",
    "12 канал HD",
    "12 канал Омск",
    "12 канал Череповец",
    "12-kanal",
    "121_Diema",
    "122_Diema Family",
    "123_Vivacom Arena",
    "124_Cinestar",
    "125_Cinestar A&T",
    "14 Channel",
    "1HD Music Television HD",
    "1HD Music Television",
    "1HD",
    "1in AM",
    "1mus.com",
    "1rostov",
    "1TV HD",
    "1TV Sport",
    "1TV",
    "1tvcrimea",
    "2 Radio",
    "2+2 HD",
    "2+2",
    "21 tv",
    "21TV AM",
    "24 KZ",
    "24 Канал",
    "24 техно",
    "24 Тува",
    "24",
    "24KZ",
    "25 регион (Владивосток)",
    "25 регион",
    "27 канал Прокопьевск",
    "2TV HD",
    "30A MUSIC TV",
    "30A The Beach Show",
    "31 канал (Казахстан)",
    "31 канал (Челябинск)",
    "31 канал",
    "312 KG",
    "312 Кино  KG",
    "312 Кино",
    "312 Музыка",
    "312",
    "312Кино",
    "33 канал (Хмельницький)",
    "33 квадратных метра",
    "36.6 HD",
    "360 Tune Box",
    "360 Новости HD",
    "360 Новости",
    "360TuneBox",
    "360TV HD",
    "360° HD",
    "360° Международный HD",
    "360° Новости HD",
    "360° Новости",
    "365 Дней HD",
    "365 дней ТВ",
    "365 ТВ",
    "3JIou TaTapuH",
    "3ooпарк",
    "4 Kurd",
    "4 канал (Екатеринбург)",
    "4 канал",
    "41 канал (Воронеж)",
    "41 регион (Петропавловск-Камчатский)",
    "41 регион",
    "41 телеканал (Воронеж)",
    "43 канал (Туапсе)",
    "43 канал HD",
    "49 канал Новосибирск",
    "4ever Cinema HD",
    "4ever Drama HD",
    "4ever music hd",
    "4ever Theater HD",
    "4Fun Dance",
    "4Fun Kids",
    "4Fun.TV",
    "4K 60fps COLORFUL WORLD - TRUE CINEMATIC",
    "4K 60fps",
    "4K HDR 60FPS Dolby Vision",
    "4K UHD релакс",
    "4K ULTRA HD",
    "4K Video ULTRA HD 60 FPS",
    "4K Кино и сериалы",
    "4K Кино",
    "4K красота: закаты леса и мегаполисы",
    "4K Удивительные животные",
    "4K — Кино и сериалы",
    "4U TV",
    "4Y Baltic",
    "5 kanal",
    "5 канал HD",
    "5 канал SD",
    "51 Radio TV",
    "555",
    "5Sport Gold",
    "5TV",
    "6 ТВ",
    "7 канал (Казахстан)",
    "7 канал (Красноярск)",
    "7 канал Красноярск",
    "7 канал",
    "7 ТВ HD",
    "70-80 TV [it]",
    "78 (Санкт-Петербург)",
    "78 канал (Санкт-Петербург)",
    "78 канал",
    "7TV",
    "7X MUSIC",
    "7ТВ",
    "8 TV HD LV",
    "8 канал ( Новосибирск)",
    "8 канал (Владивосток)",
    "8 канал (Красноярск)",
    "8 канал (Красноярский край)",
    "8 канал (Новосибирск)",
    "8 Канал HD",
    "8 канал Беларусь",
    "8 Канал",
    "80s Maxi Mix radio",
    "86 Сургут",
    "88 STEREO TV",
    "8TV [LV]",
    "8TV",
    "8ТВ",
    "9 канал (Израиль)",
    "9 канал [IL]",
    "A Haber",
    "A1 720x576",
    "A1 HD",
    "A1",
    "A2 HD",
    "A2",
    "A21 Network",
    "AABC",
    "AASS",
    "Abai TV HD",
    "Abai TV",
    "Abaza TV",
    "ABC News Live 1",
    "ABC News Live 10",
    "ABC News Live 2",
    "ABC News Live 3",
    "ABC News Live 4",
    "ABC News Live 5",
    "ABC News Live 6",
    "ABC News Live 7",
    "ABC News Live 8",
    "ABC News Live 9",
    "ABC News Live [US]",
    "ABC News Live",
    "ABN",
    "Activa TV (720p)",
    "Adjara TV",
    "adult1",
    "adult2",
    "adult6",
    "AFN Music",
    "Afrobeats 247",
    "Afrobeats TV",
    "Afrobeats",
    "AfroTurk",
    "Agro TV",
    "Ai Video",
    "Aisman",
    "AIVA",
    "Akhali Formula",
    "Akit TV",
    "Aksu TV",
    "Akudji",
    "Al Jazeera International",
    "Al Sunnah",
    "Al Zahra TV",
    "AL ZEHRA TV",
    "Al-Zahra TV Turkic",
    "Alanya Posta TV",
    "ALCARRIA TV",
    "Ale Kino+ HD",
    "aleksey_24RUS",
    "Aleph News",
    "Alex.Films",
    "Alfa Omega TV",
    "Alfa TVP",
    "Alhayat TV",
    "Allgäu TV",
    "Almahriah TV",
    "ALO-TV [EE]",
    "alpha Cinema",
    "Alpha Cinemaᴴᴰ",
    "alpha Funny HD",
    "alpha Moretime",
    "Alpha Moretimeᴴᴰ",
    "Alt-info",
    "Altyn Asyr TM",
    "Altyn Asyr",
    "Alyx Star Лесби с Jazmin Luv (12min)",
    "Amedia 1 HD",
    "Amedia 1",
    "Amedia 2 HD",
    "Amedia 2",
    "Amedia HIT HD",
    "Amedia HIT",
    "Amedia Premium HD",
    "Amedia Premium",
    "amedia1",
    "amedia2",
    "Amga",
    "Amidia Hit HD",
    "Amidia Hit",
    "Amudaryo TV",
    "Anadolu Net TV",
    "Anal",
    "Andijon",
    "Android TV + Free TV",
    "ANEWZ",
    "Ani",
    "Animal Planet [PL]",
    "Animal Planet HD",
    "Anime Kids",
    "Anime TV HD",
    "Anime TV",
    "Anixe HD",
    "ANTENA HD",
    "Aqlvoy TV UZ",
    "Aqlvoy",
    "Ararat",
    "ARAS TV",
    "ARB Gunes",
    "ARB",
    "Argo TV",
    "Argo_13",
    "Arirang Korea",
    "Arirang",
    "Arma TV",
    "Armenia 1",
    "Armenia 2",
    "Armenia TV",
    "Armtoon",
    "Art Sport 1 HD",
    "Art Sport 2 HD",
    "Art Sport 3 HD",
    "Art Sport 4 HD",
    "Art Sport 5 HD",
    "Art Sport 6 HD",
    "Arte HD",
    "ARTN TV",
    "Artn",
    "ArtSport1HD",
    "ArtSport2",
    "ArtSport3",
    "ArtSport4",
    "ArtSport5",
    "ArtSport6",
    "Ascabat",
    "Astrakhan.RU Sport HD",
    "Astrakhan.RU SPORT",
    "Astrix116",
    "Astro Arena 1 HD",
    "Atameken Business Channel",
    "Atlanta Channel (720p)",
    "Atlas TV",
    "ATR",
    "ATV Azerbaijan HD",
    "ATV Bazmoc HD",
    "ATV HD",
    "ATV Kinoman HD AM",
    "ATV Kinoman",
    "ATV",
    "atvmedia",
    "Augsburg TV",
    "Aurora Arte",
    "Avia Stream",
    "AviaStream",
    "Axterix",
    "AYAZ TV",
    "AYM HD",
    "Az TV",
    "Azatutyun HD",
    "AZIYA TV",
    "Azstar TV",
    "AzTV HD",
    "Aztv",
    "Babes TV HD",
    "Babes TV",
    "BABY TIME",
    "Baby TV",
    "Baden TV",
    "Baku TV (720p)",
    "BAKU TV",
    "BALAPAN",
    "Balaton TV",
    "Balticum Auksinis HD",
    "Balticum Platinum HD",
    "Balticum TV HD LT",
    "BangU",
    "Banovina TV",
    "Baraza TV Hits",
    "Barely Legal TV",
    "BastiBubu",
    "Batumi TV",
    "Bazmots TV",
    "BBC America",
    "BBC Earth",
    "BBC World News",
    "BBC WORLD",
    "BCU Action HD",
    "BCU Catastrophe HD",
    "BCU Cinema HD",
    "BCU Cinema+ HD",
    "BCU Comedy HD",
    "BCU Cosmo HD",
    "BCU Criminal HD",
    "BCU Fantastic HD",
    "BCU Filmystic HD",
    "BCU History HD",
    "BCU Kids 4K",
    "BCU Kids HD",
    "BCU Kids+ HD",
    "BCU Kinorating HD",
    "BCU Kinorating",
    "BCU Marvel HD",
    "BCU Media HD",
    "BCU Premiere HD",
    "BCU Premiere Ultra 4K",
    "BCU Reality HD",
    "BCU Romantic HD",
    "BCU RUSerial HD",
    "BCU Russian HD",
    "BCU Stars HD",
    "BCU Survival HD",
    "BCU TruMotion HD",
    "BCU Ultra 4K",
    "BCU VHS HD",
    "BCU Кинозал Premiere 1 HD",
    "BCU Кинозал Premiere 2 HD",
    "BCU Кинозал Premiere 3 HD",
    "BCU Мультсериал HD",
    "BCU Мультсериал",
    "BCU Сваты HD",
    "BCU СССР HD",
    "Beach TV (30A) (720p)",
    "Beach TV (Panama City) (720p)",
    "Beach TV (Pawleys Island) (720p)",
    "Bein EXTRAS",
    "Bein Sport 2 HD 🇫🇷",
    "Bein Sport 3 HD 🇫🇷",
    "Bein Sport HD 🇫🇷",
    "BEK TV Sports",
    "Belsat TV",
    "BeritaSatu World [IDN]",
    "Best4Sport 2 HD",
    "Best4Sport HD",
    "Better Life Nature Channel",
    "Bibel TV Musik",
    "Big Ass",
    "Big Dick",
    "Big Planet HD",
    "Big Tits",
    "Biz Cinema UZ",
    "Biz Cinema",
    "Biz Music HD UZ",
    "BIZ Music",
    "Biz TV",
    "BiZFm",
    "Biznes TV",
    "BizTV HD UZ",
    "Biztv",
    "black",
    "BLAST CHANNEL TV",
    "Blockbusters Time FHD",
    "Blockbusters Time",
    "Blonde",
    "Bloomberg HT",
    "Bloomberg TV Europe HD",
    "Bloomberg TV",
    "Bloomberg",
    "Blowjob",
    "BMG",
    "Bolajon",
    "Bollywood 4U",
    "Bollywood HD",
    "Bollywood",
    "Bolt HD",
    "Bolt",
    "Boomerang",
    "BOSSFILM",
    "BOX Anime HD",
    "BOX Apocalypse HD",
    "BOX Autotrend HD",
    "BOX Be ON Edge 1 Live HD",
    "BOX Be ON Edge 2 Live HD",
    "BOX Be ON Edge HD",
    "BOX Cyber HD",
    "BOX Docu HD",
    "BOX Fantasy HD",
    "BOX Franchise HD",
    "BOX Game HD",
    "BOX Gangster HD",
    "BOX Ghost HD",
    "BOX Gurman HD",
    "BOX Hybrid HD",
    "BOX M.Serial HD",
    "BOX Memory HD",
    "BOX Metall HD",
    "BOX Music 4K",
    "BOX Remast 4K",
    "BOX Remast+ 4K",
    "BOX RU.RAP HD",
    "BOX Serial 4K",
    "BOX Serial HD",
    "BOX Sitcom HD",
    "BOX Sportcast HD",
    "BOX SportCast Live 1 HD",
    "BOX SportCast Live 2 HD",
    "BOX SportCast Live 3 HD",
    "BOX SportCast Live 5 HD",
    "BOX SportCast Live 6 HD",
    "BOX SportCast Live 9 HD",
    "BOX Spy HD",
    "BOX Stories HD",
    "BOX Travel HD",
    "BOX Travel Premiere HD",
    "BOX Western HD",
    "BOX Zombie HD",
    "Brazzers HD",
    "Brazzers TV",
    "Brazzers",
    "Breathtaking landscape 4K",
    "Bridge Classic",
    "Bridge Deluxe HD",
    "Bridge Deluxe",
    "BRIDGE Hits",
    "BRIDGE Rock",
    "Bridge TV Classic",
    "BRIDGE TV DELUXE",
    "BRIDGE TV FRESH",
    "Bridge TV Hits",
    "BRIDGE TV ROCK",
    "BRIDGE TV Русский Хит",
    "BRIDGE TV Шлягер",
    "BRIDGE TV Этно",
    "Bridge TV",
    "BRIDGE РУССКИЙ ХИТ",
    "BRIDGE ШЛЯГЕР",
    "BRIDGE",
    "Briç ve Satranç TV",
    "Brodilo TV",
    "BRTV",
    "Brunette",
    "BT Sport 4K",
    "BTV HD",
    "BTV",
    "Bukhara",
    "Bun TV",
    "Bursa AS TV",
    "Busuioc TV",
    "BUSUIOC",
    "C1 HD (Сургут)",
    "C1",
    "C86",
    "CafeTV24 Veneto",
    "Campus TV",
    "Can TV",
    "Canal 1",
    "Canal 11",
    "Canal Motor",
    "Canale 2 Altamura",
    "Cars  Stars TV HD",
    "Cartoon Classics",
    "Cartoon Network",
    "Cartoonito HD",
    "Cartoons 90",
    "Cartoons BIG",
    "Cartoons Short",
    "Cartoons_90",
    "Cartoons_BIG",
    "Cartoons_Short",
    "CAY TV",
    "CBC Azerbaijan TV HD",
    "CBC Sport TV HD",
    "CBC",
    "CCTV4",
    "Cekmeköy TV",
    "Cento X Cento",
    "CGTN SD",
    "CGTN Русский SD",
    "CGTN",
    "Channel 11 [IL]",
    "CHD-TV Rock",
    "CHD-TV RU Rock HD",
    "Che TV",
    "Che",
    "Chuvashiya online",
    "Cine Classic",
    "Cine+ HD",
    "Cine+ Hit HD",
    "Cine+ Kids HD",
    "Cine+ Legend",
    "CineFamily",
    "CineFrisson",
    "Cinema (uzb)",
    "Cinema 1",
    "Cinema 10",
    "Cinema 11",
    "Cinema 12",
    "Cinema 14",
    "Cinema 15",
    "Cinema 16",
    "Cinema 17",
    "Cinema 18",
    "Cinema 19",
    "Cinema 2",
    "Cinema 20",
    "Cinema 24",
    "Cinema 25",
    "Cinema 28",
    "Cinema 29",
    "Cinema 3",
    "Cinema 30",
    "Cinema 34",
    "Cinema 36",
    "Cinema 4",
    "Cinema 5",
    "Cinema 6",
    "Cinema 7",
    "Cinema 8",
    "Cinema 9",
    "Cinema Azino777",
    "Cinema Comedy HD",
    "Cinema Etv",
    "Cinema Family HD",
    "Cinema HD",
    "Cinema Legend HD",
    "Cinema Megahit HD",
    "Cinema Prime",
    "Cinema SD",
    "Cinema UZ",
    "Cinema",
    "Cinema_fitness",
    "CineMan Action",
    "CineMan CCCP 4K",
    "CineMan CCCP FHD",
    "CineMan ExExEx TWO",
    "CineMan KIDS 4K",
    "CineMan Marvel",
    "CineMan Melodrama",
    "CineMan MiniSeries",
    "CineMan OLD 4K",
    "CineMan Premium",
    "Cineman Relax 4k",
    "CineMan Thriller",
    "CineMan Top",
    "CineMan VHS",
    "CineMan Военные Сериалы",
    "CineMan Глухарь + Карпов",
    "CineMan Дубляж СССР",
    "CineMan Катастрофы",
    "CineMan Комедийные сериалы",
    "CineMan Комедия",
    "CineMan Криминальные Сериалы",
    "CineMan Лесник",
    "CineMan Мелодрама",
    "CineMan Ментовские Войны",
    "CineMan ПёС + Лихач",
    "CineMan ПёС",
    "CineMan расследование авиакатастроф",
    "CineMan РуКино",
    "CineMan Сваты",
    "CineMan Симпсоны",
    "CineMan Скорая помощь",
    "CineMan СССР 4К",
    "CineMan СССР",
    "CineMan Ужасы",
    "CineMan Фитнес",
    "CineMan",
    "CineMan_Action",
    "CineMan_Comedy",
    "Cineman_Gluhar_Karpov",
    "CineMan_Katasrofi",
    "Cineman_Katastrofi_doc",
    "CineMan_lesnik",
    "CineMan_Marvel",
    "CineMan_Melodrama",
    "CineMan_mentovskiye_voyni",
    "CineMan_MiniSeries",
    "CineMan_Pes",
    "CineMan_Premium",
    "Cineman_relax_4k",
    "CineMan_Rukino",
    "CineMan_Simpson",
    "CineMan_Skoraya_pomosh",
    "CineMan_Svati",
    "CineMan_Thriller",
    "CineMan_Top",
    "CineMan_Ujasi",
    "CineMan_VHS",
    "Cinemaraton",
    "Cinemator",
    "Cinemax",
    "Cirque du Soleil",
    "City TV",
    "Clarity4k Anime",
    "Clarity4K Asia",
    "Clarity4K Avto Blog",
    "Clarity4K BluesString",
    "Clarity4K Bollywood music",
    "Clarity4k Deep House",
    "Clarity4k DeepMe",
    "Clarity4K France music",
    "Clarity4K Galaxy Films",
    "Clarity4K Gamefilm",
    "Clarity4K HBO series",
    "Clarity4K Heavy metal",
    "Clarity4K Korean music",
    "Clarity4K Kинодети CССР",
    "Clarity4K Latin music",
    "Clarity4K Netflix",
    "Clarity4K RemasterWave",
    "Clarity4K Russian music",
    "Clarity4K Travel blog",
    "Clarity4k UFO",
    "Clarity4K Ukrainian music",
    "Clarity4K UrbanBlack",
    "Clarity4K Walt Disney",
    "Clarity4K World Music",
    "Clarity4k Боевик VHS",
    "Clarity4k Боевик",
    "Clarity4k Вселенная",
    "Clarity4K Города",
    "Clarity4k Драмы",
    "Clarity4k Единоборства",
    "Clarity4K Запал",
    "Clarity4K Звериный мир",
    "Clarity4K Кино СССР",
    "Clarity4K КиноНовинки",
    "Clarity4k КиноФраншизы",
    "Clarity4K Киношарм",
    "Clarity4k Классика кино",
    "Clarity4K Комедия (VHS)",
    "Clarity4K Комедия СССР 1",
    "Clarity4K Комедия СССР 2",
    "Clarity4K Комедия",
    "Clarity4K КосмоМир",
    "Clarity4K Молодежные комедии",
    "Clarity4K Мультимир",
    "Clarity4K Мультляндия",
    "Clarity4K Приключения",
    "Clarity4k Русские сериалы",
    "Clarity4K Семейный",
    "Clarity4k Сумеречный Эфир",
    "Clarity4K Театр",
    "Clarity4K Триллер",
    "Clarity4K Ужасы  VHS",
    "Clarity4K Ужасы VHS",
    "Clarity4K Ужасы",
    "Clarity4K Фантастика",
    "Clarity4K Фэнтези",
    "Classic Music HD",
    "Classic Radio",
    "Club 85",
    "Club Anal",
    "Club BBW",
    "Club Parody",
    "Club Sweetie Fox",
    "Club Teens HD",
    "Clubbing TV HD",
    "Clubbing TV",
    "CNA",
    "CNBC [US]",
    "CNBC",
    "CNN International",
    "CNN TÜRK HD",
    "CNN",
    "COLORFUL WORLD ANIMALS - ANIMALS SOUNDS",
    "Comedy Arkhi",
    "Comedy",
    "COMPANY TV HD",
    "Company TV",
    "Compilation",
    "Continent E HD",
    "CPS Action HD",
    "CPS Action",
    "CPS Anime HD",
    "CPS Anime",
    "CPS Cartoon HD",
    "CPS Cartoon",
    "CPS Comedy HD",
    "CPS Comedy",
    "CPS Drama HD",
    "CPS Drama",
    "CPS Fiction HD",
    "CPS Fiction",
    "CPS Fresh HD",
    "CPS Fresh UA HD",
    "CPS Fresh UA",
    "CPS Fresh",
    "CPS FreshUA",
    "CPS Investigation",
    "CPS Jackie Chan HD",
    "CPS Mix HD",
    "Crime Today",
    "CTC kids",
    "CTC",
    "CTPK HD",
    "Cuckold HD",
    "Cuckold",
    "Cum4k UHD",
    "Cum4K",
    "Curiosity Stream HD",
    "Curiosity Stream",
    "D1",
    "Da Vinci HD",
    "Da Vinci Learning Europe",
    "Da Vinci Learning",
    "Da Vinci",
    "DANCE TV TECHNO",
    "DanceHits80",
    "Dar21",
    "Dardimandi",
    "Das Erste",
    "Dasturxon",
    "DasturxonTV",
    "DAZN Combat German",
    "DE-Baden-TV+",
    "Deejay TV HD",
    "Deejay TV",
    "Delfi TV HD LT",
    "Delta Travel",
    "Delta TV",
    "Deluxe Dance",
    "DELUXE MUSIC SD",
    "Deluxe Music",
    "Deluxe Rap",
    "Demo 4K",
    "Deniz Postası TV",
    "Denov TV",
    "Desire",
    "Deutsche Welle",
    "DeVchatA",
    "Dia TV",
    "Diema Sport 2 HD",
    "Diema Sport 2",
    "Diema Sport 3",
    "Diema Sport HD",
    "Diema Sport",
    "Dimpon TV",
    "Discovery Channel HD",
    "Discovery Channel",
    "Discovery HD",
    "Discovery Science",
    "Disney Channel",
    "Disney Junior",
    "Disney XD",
    "Divi Sport",
    "DiviSport",
    "Diyanet TV",
    "Diyar TV",
    "DİM TV",
    "dj Zour",
    "DocuBox HD",
    "DocuBox",
    "DOLBY VISION OLED Test Video (2024)",
    "Dom kino",
    "Donau TV",
    "Dorama Hit HD",
    "Dorcel HD",
    "DORCEL TV HD",
    "Dorcel TV",
    "DorcelHD",
    "Dost TV",
    "DREAM TURK HD",
    "Dream Turk",
    "Drive",
    "DSTV",
    "Duck TV HD",
    "Duck TV",
    "Ducktv HD",
    "ducktv plus",
    "DuckTV",
    "Dunya TV",
    "Dunyo Bo'ylab",
    "Dunyo bo`ylab UZ",
    "Dunyo bo`ylab",
    "Dunyo Boylab",
    "Dunyo",
    "Duo 3 HD",
    "Duo 4 HD",
    "Duo 5 HD",
    "Duo 6 HD",
    "Duo 7 [LV]",
    "Duo 7",
    "DW Russian",
    "E TV",
    "E! Entertainment [PL]",
    "E! Entertainment",
    "Eesti Kanal EE",
    "Egrisi TV",
    "EL TV (260p) [Not 24/7]",
    "Elektrika TV HD",
    "Eliqqala TV",
    "Ellikqala TV",
    "ENGLISH CLUB TV HD",
    "English Club TV",
    "English Club",
    "Enki Benki",
    "Enter-фильм HD",
    "Enter-фильм",
    "Epic Drama HD",
    "Epic Drama",
    "Epic HD",
    "Equalympic",
    "Er TV",
    "Erkir Media",
    "ERT News",
    "Erz-TV Stollberg (576p)",
    "ES TV",
    "ESKA TV",
    "Esperia TV Calabria",
    "ESPN Premium HD Испания 🇪🇸",
    "ESPN2 HD Испания 🇪🇸",
    "ESPN4 HD Испания 🇪🇸",
    "ESPN5 HD Испания 🇪🇸",
    "ESPN6 HD Испания 🇪🇸",
    "ESPN7 HD Испания 🇪🇸",
    "Espresso TV HD",
    "Espresso TV",
    "Est TV",
    "ETB Basque",
    "ETV 2 HD",
    "ETV HD",
    "ETV Kayseri",
    "ETV+ HD",
    "Eu Music HD",
    "EU.Music HD",
    "Euro HD",
    "Euro Indie Music Chart TV",
    "Euro Indie Music Chart",
    "Euro Indie Music",
    "Euro Sport 1 HD 🇫🇷",
    "Euro Sport 1 HD",
    "Euro Sport 2 HD",
    "Euro-D",
    "Eurochannel",
    "EuroD HD",
    "eurokino",
    "Euronews Georgia",
    "Euronews",
    "Europa Plus TV",
    "Europa Plus",
    "Eurosport 1 [PL]",
    "Eurosport 1 HD",
    "Eurosport 2 [PL]",
    "Eurosport 2 FR",
    "Eurosport 2 HD",
    "eurosport",
    "Eurosport1HD",
    "Eurosport2HD",
    "EvGAMEN",
    "Evrokino",
    "EWTN Latvija",
    "EWTN",
    "Exclusiv TV [MD]",
    "Extasy 4K",
    "EXTREMA TV",
    "Extreme Sports [RU]",
    "Factoria TV HD",
    "FAN HD",
    "Fantastika",
    "FAP TV 2 HD",
    "FAP TV 4 HD",
    "FAP TV Compilation HD",
    "FAP TV Lesbian HD",
    "Farg'ona",
    "Fashion Channel",
    "Fashion One",
    "Fashion TV Europe",
    "Fashion TV HD Ukraine",
    "Fashion TV HD",
    "Fashion",
    "FashionBox HD",
    "FashionBox",
    "Fast & FunBox HD",
    "FastSports",
    "FB TV",
    "FBTV",
    "fenikspluskino",
    "Fergana MTRK",
    "Fetish HD",
    "Fetish",
    "FGS",
    "Fight Network HD",
    "Fight Network",
    "FightBox HD",
    "FightBox",
    "FightKlub",
    "FILM TV GROUP МультКазки",
    "FILM TV GROUP",
    "FILM TV Кінозал",
    "FILM TV Леді Баг & Щенячий патруль.",
    "Film UA Drama",
    "Film Visit",
    "Film Zone",
    "Film.Ua Drama",
    "FilmBox Arthouse [PL]",
    "Filmbox Arthouse",
    "FilmBox Russia",
    "Filmbox",
    "FilmOFF",
    "FilmON",
    "FilmUA Drama HD LV",
    "FILMUA Drama HD",
    "FilmUA LIVE HD",
    "Filmzon",
    "FilmZone [EE]",
    "Filmzone HD",
    "Filmzone Plus HD",
    "FilmZone",
    "Filstalwelle",
    "Finest TV",
    "Fitness TV",
    "Flixsnip",
    "FLYING OVER SEYCHELLES",
    "Fokus TV",
    "FON Music HD",
    "FON Music",
    "Food Network HD",
    "Food Network Европа",
    "Food Network",
    "FoodTime HD",
    "FoodTime",
    "Foreign Languages UZ",
    "Formula TV HD",
    "Formula TV",
    "Formula",
    "Fortuna TV",
    "Fox Business [US]",
    "Fox HD",
    "Fox Life HD",
    "Fox life",
    "Fox News",
    "FOX Sports",
    "Fox",
    "France 2",
    "France 24 English",
    "France 24 HD",
    "France 24",
    "Franken Fernsehen HD",
    "Freedom",
    "FreeNews",
    "FrenchLover HD",
    "FrenchLover",
    "Fresh Adventure",
    "Fresh Cinema",
    "Fresh Comedy",
    "Fresh Family",
    "Fresh Fantastic",
    "Fresh HD AM",
    "Fresh Horror",
    "Fresh Kids",
    "Fresh Premiere",
    "Fresh Rating",
    "Fresh Romantic",
    "Fresh Russian",
    "Fresh Series",
    "Fresh Soviet",
    "Fresh Thriller",
    "Fresh TV",
    "Fresh VHS",
    "Friday",
    "FS1 AUSTRIA",
    "FTF Sports",
    "FTV Türk",
    "FTV",
    "fubo Sports Network",
    "Futbol TV",
    "FUTBOL UZ",
    "Futbol",
    "FX HD",
    "FX Life",
    "FX",
    "Gags Network",
    "Galaxy",
    "Gameplay трейлеры",
    "Gameplay",
    "Gametoon HD",
    "Gangbang",
    "GDS",
    "Geghama TV",
    "Genuine TV",
    "Georgian Times",
    "GLN 24 (Геленджик)",
    "GLN 24",
    "Global Star TV",
    "GlobalMedia Агата Кристи",
    "Globus Music",
    "Globus TV",
    "GNC",
    "Go3 Films HD",
    "Go3 Sport 1 HD",
    "Go3 Sport 2 HD",
    "Go3 Sport 3 HD",
    "Go3 Sport 4 HD",
    "Go3 Sport 4",
    "Go3 Sport Open HD",
    "Go3 Sport",
    "Gold Line Brief",
    "Gold Line",
    "Gold Television",
    "Gold TV",
    "GPSComedy HD",
    "Groove Radio",
    "Groovy",
    "Gubka bob",
    "Gulli girl",
    "Gulli",
    "Guria TV",
    "Gurinel TV HD",
    "Gurjaani TV",
    "H1 Lratvakan",
    "H1 Satellite",
    "H1",
    "H2 [PL]",
    "H2 HD",
    "H2",
    "Haber 61 TV",
    "HalkTV",
    "Happy Radio TV",
    "Hardcore",
    "Hay TV",
    "HayKino",
    "HCT",
    "HD Media",
    "HDL",
    "HGTV HD",
    "HHQ",
    "HISTORY [PL]",
    "History HD",
    "Hit FM",
    "Hollywood HD",
    "Hollywood",
    "Home 4K",
    "Horizon TV",
    "Horror TV HD",
    "Horror TV",
    "Horse and Country TV",
    "Horse and Country",
    "HOSEN TV",
    "House Floor",
    "House_ukr",
    "HSE (1080p)",
    "HSE24 Extra (1080p)",
    "HSE24 Trend (576p)",
    "HSE24",
    "HT-Spor TV",
    "Hunat TV",
    "Hustler HD",
    "Hybrid Education",
    "I-kbol",
    "Icaro TV Rimini",
    "ICTV 2 HD",
    "ICTV HD",
    "ICTV серіали HD",
    "ICTV",
    "ID Investigation Discovery",
    "IDMAN TV AZ",
    "illuzion plus",
    "illuzionplus",
    "IMEDI HD",
    "Imedi TV",
    "Imervizia",
    "Info TV HD",
    "Info TV",
    "Info",
    "Info: info",
    "Info: ZunoTV",
    "InMuNa Live",
    "InRating HD",
    "Insight TV HD",
    "Insight TV",
    "Insight UHD",
    "Interracial HD",
    "Interracial",
    "IPTV Ужастик",
    "IPTV Фантастика",
    "IPTVPLAY PROMO",
    "IPTVPLAY TEST",
    "IPTVPLAY ВЕДЬМАК",
    "IPTVPLAY ВЕДЬМАКᴴᴰ",
    "IPTVPLAY Великая Отечественная HD",
    "IPTVPLAY Великая Отечественнаяᴴᴰ",
    "IPTVPLAY ДЕСЯТОЕ КОРОЛЕВСТВО",
    "IPTVPLAY ДЕСЯТОЕ КОРОЛЕВСТВОᴴᴰ",
    "IPTVPLAY ИНТЕРНЫ SD",
    "IPTVPLAY ИНТЕРНЫ",
    "IPTVPLAY МультСказки HD",
    "IPTVPLAY МультСказкиᴴᴰ",
    "IPTVPLAY ПАМЯТЬ",
    "IPTVPLAY ПАМЯТЬᴴᴰ",
    "IPTVPLAY УЖАСТИК HD",
    "IPTVPLAY УЖАСТИК",
    "IPTVPLAY УЖАСТИКᴴᴰ",
    "IPTVPLAY ФАНТАСТИКА HD",
    "IPTVPLAY ФАНТАСТИКА",
    "IPTVPLAY ФАНТАСТИКАᴴᴰ",
    "ishonch TV",
    "Istiqlol TV",
    "Isvicre TV",
    "Italia 2 TV",
    "iTV [LV]",
    "ITV AZ",
    "ITV Cinema",
    "ITV Music",
    "ITV",
    "iz",
    "Jahonnamo",
    "Jambyl",
    "JanTV",
    "Jaslar TV",
    "JaslarTV",
    "JC1 HD",
    "JimJam",
    "Jizzax TV",
    "Jizzax",
    "jk_90",
    "jk_90210",
    "jk_aladin",
    "jk_Alf",
    "jk_American_Dad",
    "jk_arnold",
    "jk_balance",
    "jk_Ben_Holly",
    "jk_berega",
    "jk_Besson",
    "jk_Big_Bang",
    "jk_biograf",
    "jk_black_cloak",
    "jk_Black_Mirror",
    "jk_boys",
    "jk_brookllyn",
    "jk_Buffy",
    "jk_Butler",
    "jk_Call_Saul",
    "jk_Cartoon",
    "jk_Castle",
    "jk_Chip_Dale",
    "jk_Cinema",
    "jk_Cinema2",
    "jk_Cinema3",
    "jk_Colombo",
    "jk_Criminal",
    "jk_Crowe",
    "jk_csi_m",
    "jk_csi_NY",
    "jk_csi_vegas",
    "jk_dark",
    "jk_desperate_ukr",
    "jk_detektiv",
    "jk_Dexter",
    "jk_doku_Ukraine",
    "jk_dram",
    "jk_ducks",
    "jk_duva",
    "jk_element",
    "jk_Emeli",
    "jk_fineas",
    "jk_Flintstones",
    "jk_formula",
    "jk_FUBAR",
    "jk_futurama",
    "jk_gorets",
    "jk_Gostri_Kartuz",
    "jk_Gravity_Falls",
    "jk_Grifins",
    "jk_grimm",
    "jk_Hemsworth",
    "jk_Heroes",
    "jk_House_Dragon",
    "jk_house_of_cards",
    "jk_Kameron",
    "jk_kids",
    "jk_King",
    "jk_Kopola",
    "jk_korona",
    "jk_kosty",
    "jk_Kunis",
    "jk_ledi_Bag",
    "jk_Lie_To_Me",
    "jk_Lilo",
    "jk_lost",
    "jk_Magic",
    "jk_Mel",
    "jk_Melrose_Place",
    "jk_mentalist",
    "jk_Money_Heist",
    "jk_Morning_Show",
    "jk_Muhtesem",
    "jk_NCIS",
    "jk_Neeson",
    "jk_Nolan",
    "jk_office",
    "jk_ograblenie",
    "jk_Only_Murders",
    "jk_panda",
    "jk_PAW_Patrol",
    "jk_Pepa_pig",
    "jk_pingvins",
    "jk_Poirot",
    "jk_Reeves",
    "jk_relax",
    "jk_Rik_i_Marti",
    "jk_Ritchie",
    "jk_rizdvo",
    "jk_Robocar_Poli",
    "jk_rusaloshka",
    "jk_Scorpion",
    "jk_Scorsese",
    "jk_Scrubs",
    "jk_serial",
    "jk_sexsity",
    "jk_Sherlock",
    "jk_Skott",
    "jk_Sliders",
    "jk_South_Park",
    "jk_speed",
    "jk_Spilberg",
    "jk_Stargate",
    "jk_strah",
    "jk_Succession",
    "jk_Tarantino",
    "jk_timon",
    "jk_TomJery",
    "jk_Twin_Peaks",
    "jk_Van_Dame",
    "jk_Vedmak",
    "jk_Winx",
    "jk_x_files",
    "jk_Your_mother",
    "Joy cook",
    "JTV",
    "JTX Online",
    "Jurnal TV HD",
    "K-Baseball TV",
    "K1HO",
    "KAN 11",
    "Kanal 12",
    "Kanal 15",
    "Kanal 2 HD",
    "Kanal 23",
    "Kanal 26",
    "Kanal 3",
    "Kanal 53",
    "KANAL 61",
    "Kanal 7 HD LV",
    "Kanal 7",
    "Kanal D",
    "Kanal Firat",
    "Kanal V",
    "kANAL Z",
    "Kapaz TV",
    "Kavkasia TV",
    "Kayhan Afghan",
    "Kazakh TV",
    "KBS World",
    "Kent Türk TV",
    "Kentron TV",
    "Kentron",
    "Kernel TV Винни и его друзья",
    "Kernel TV",
    "Keshet 12",
    "Key TV (720p)",
    "KHL HD",
    "KHL Prime HD",
    "KHL Prime",
    "Kids TV",
    "Kidzone Max HD",
    "KidZone Max",
    "Kidzone Mini HD LT",
    "Kidzone Mini HD",
    "kineko",
    "Kino 1",
    "Kino 1ᴴᴰ",
    "Kino 24",
    "Kino Polska HD",
    "Kino TV",
    "KinoFilm",
    "Kinofon",
    "KinoJam FHD",
    "KinoJam",
    "KinoKazka HD",
    "Kinokomediya",
    "kinolampa FHD",
    "kinolampa",
    "KinoLiving HD",
    "Kinoliving",
    "KinoMag",
    "Kinoman HD",
    "kinoman",
    "kinomiks",
    "KinoMix FHD",
    "KinoMix Юрич",
    "Kinomix",
    "Kinopokaz",
    "KINOPRO",
    "Kinorating",
    "Kinoseria",
    "KINOSTREAM",
    "Kinosweet HD",
    "Kinosweet",
    "Kinoteatr HD",
    "Kinoteatr",
    "Kinowalk FHD",
    "KINOWALK",
    "Kinowalk_tv",
    "Kinowood HD",
    "KINOХИТ HD",
    "Kiss FM",
    "Kiss Kiss Italia HD",
    "Kiss Kiss Napoli HD",
    "Kiss Kiss TV HD",
    "KO TV",
    "Komediya",
    "KONUL TV AZERBAIJAN",
    "Konul TV",
    "Konya Olay TV",
    "Kotayk TV",
    "Kpop Play TV",
    "KRAL Pop TV",
    "KRAL POP",
    "Kronehit HD",
    "Kronehit TV HD",
    "Kronehit TV",
    "KulturMD (1080p)",
    "KVARTAL TV",
    "Kycman",
    "Kино TV",
    "La 1 Canarias",
    "LaLe",
    "Lalegul TV",
    "lampoTV",
    "LangLab HD",
    "Latina HD",
    "Latina",
    "Latvijas Slagerkanals",
    "Latvijas Šlāgerkanāls",
    "LAV-KINO",
    "legendarniy24",
    "Legion tv",
    "LentaKino",
    "Lesbian",
    "LIBERTY  ХИП-ХОП",
    "LIBERTY ANIME",
    "LIBERTY AVTO GIR",
    "LIBERTY BBC",
    "LIBERTY BEBIMULT",
    "LIBERTY CUBE 4K",
    "LIBERTY DC",
    "LIBERTY DISNEY",
    "LIBERTY DREAM WORKS",
    "LIBERTY FAN",
    "LIBERTY KINO ENG FHD",
    "LIBERTY KINO UKR 4K",
    "LIBERTY MARVEL 4K",
    "LIBERTY MULT ENG",
    "LIBERTY MULT UKR 4K",
    "LIBERTY MUZYKA DE FHD",
    "LIBERTY MUZYKA DJ",
    "LIBERTY MUZYKA DZHAZ FHD",
    "LIBERTY MUZYKA INDI FHD",
    "LIBERTY MUZYKA K-POP FHD",
    "LIBERTY MUZYKA KHIP-KHOP FHD",
    "LIBERTY MUZYKA KHITY90-X FHD",
    "LIBERTY MUZYKA LATINO FHD",
    "LIBERTY MUZYKA POP-MUZYKA FHD",
    "LIBERTY MYUZIKL",
    "Liberty Netflix 4K",
    "Liberty Netflix",
    "LIBERTY PLANKTON",
    "Liberty RuFilm",
    "LIBERTY RUS FILM 4K",
    "LIBERTY SERIAL FHD",
    "Liberty Thriller",
    "LIBERTY TURK FILM 4K",
    "LIBERTY XX Век 4K",
    "Liberty Аванпост 4K",
    "Liberty аванпост",
    "LIBERTY Боевики",
    "LIBERTY Джаз",
    "LIBERTY Документалки",
    "Liberty драма",
    "LIBERTY Инди",
    "Liberty Индия 4K",
    "Liberty Индия",
    "LIBERTY К-ПОП",
    "Liberty Кино UKR HD",
    "LIBERTY Кино UKR",
    "Liberty Кино Микс 4K",
    "Liberty киномикс",
    "LIBERTY Комедии",
    "LIBERTY Короткометражное",
    "Liberty Криминал 4K",
    "Liberty криминал",
    "LIBERTY Крош",
    "LIBERTY Латино",
    "LIBERTY Легенда 4K",
    "LIBERTY МЕДИВАЛ 4К",
    "Liberty Мелодрамы FHD",
    "LIBERTY Мелодрамы",
    "LIBERTY Микс Музыка",
    "LIBERTY МиМ",
    "LIBERTY Музыка DJ FHD",
    "LIBERTY Музыка Рок FHD",
    "LIBERTY Мульт 4K",
    "LIBERTY Мульт UKR",
    "LIBERTY Пиксар",
    "LIBERTY Планета 360",
    "LIBERTY Поп-Музыка",
    "LIBERTY Рок",
    "Liberty РусФильм 4K",
    "LIBERTY Сваты",
    "LIBERTY Семейный",
    "Liberty сериал",
    "Liberty Сериалы FHD",
    "LIBERTY Сериалы",
    "LIBERTY Симпсоны",
    "LIBERTY Сказки",
    "LIBERTY Союз 4K",
    "Liberty Триллер 4K",
    "LIBERTY Триллеры",
    "Liberty Турк Фильм 4K",
    "Liberty Турк фильм",
    "LIBERTY Ужасы 4K",
    "LIBERTY ХИП-ХОП",
    "LIBERTY Хиты 90-х",
    "LIBERTY Шоу",
    "LIBERTY Эротика",
    "LIBERTY Южный парк",
    "Lietuvos ryto TV HD",
    "Lifehack",
    "Light Channel (576p) [Not 24/7]",
    "Light Channel",
    "Lisfix",
    "Liuks!",
    "Liuks",
    "Live Cams",
    "Live TV",
    "LNK HD",
    "Logovo Films",
    "Lori HD",
    "Lounge FM",
    "Love Nature HD (4K)",
    "Love Nature",
    "Love",
    "Loves Berry HD (18+)",
    "LRT HD",
    "LRT Klasika [LT]",
    "LRT Klasika",
    "LRT Lituanica HD",
    "LRT Plius HD",
    "LTV1 HD",
    "LTV7 HD",
    "LubimoeKino",
    "Lux TV",
    "LuxTV",
    "Luxury HD",
    "LUXURY",
    "Luys TV",
    "Luys",
    "M Sport",
    "M+ Deportes [ES]",
    "M-1 Global",
    "Madaniyat va ma'rifat",
    "Madaniyat va Marifat",
    "Madeniyat va marafat",
    "Maestro",
    "Mafia",
    "Magic TV SD",
    "Magic TV",
    "Mahalla",
    "Makon TV HD",
    "Makon TV",
    "MakonTV HD",
    "MakonTV",
    "Maksim Films",
    "Maná Tserkov' Onlayn",
    "Marao",
    "Marneuli TV",
    "MaviKaradeniz",
    "MAX Sport 1",
    "MAX Sport 4",
    "MAX Sport",
    "MAX",
    "MaxSport1HR",
    "MaxSport2HR",
    "MC EU TV",
    "MCM HD",
    "MCM POP HD",
    "MCM Pop",
    "Mcm top",
    "MDL",
    "Med Muzik",
    "Medeniyyet TV",
    "MEGOGO MUSIC",
    "Megogo Sport",
    "Melodia FM",
    "Meltem TV",
    "Mening Yurtim FHD UZ",
    "Mercan TV",
    "Meteo24",
    "Metro TV [PL]",
    "Metro TV",
    "Mezzo",
    "Midnight Lust Music",
    "Milady Television",
    "Milf HD",
    "MILF",
    "Milliy SD",
    "Milliy TV HD",
    "Milliy TV UZ",
    "Milliy TV",
    "Milliy",
    "MilliyTV",
    "Mimi TV",
    "MiMi",
    "Mind Blowing 4K",
    "MiniMax_Agata",
    "MiniMax_New_007",
    "MiniMax_New_Classic",
    "MiniMax_New_Drama",
    "MiniMax_New_Fantastika",
    "MiniMax_New_FILM1",
    "MiniMax_New_FILM2",
    "MiniMax_New_FILM3",
    "MiniMax_New_Istoriya",
    "MiniMax_New_MAKROMIR",
    "MiniMax_New_MEGAMIR",
    "MiniMax_New_MIKROMIR",
    "MiniMax_New_Pogrujeniye",
    "MiniMax_New_Priklyucheniya",
    "MiniMax_New_Scorost",
    "MiniMax_New_Triller",
    "MiniMax_New_UFO",
    "MiniMax_New_Western",
    "MiniMax_NEWRUS",
    "MIR",
    "MIR24",
    "Miras",
    "mirtv",
    "MIXM",
    "Mixtape [PL]",
    "Mixtape",
    "MM 007 HD",
    "MM Classic HD",
    "MM NewFilm 1 HD",
    "MM NewFilm 2 HD",
    "MM NewFilm 3 HD",
    "MM NewFilm RU HD",
    "MM UFO HD",
    "MM Агата Кристи HD",
    "MM Вестерн HD",
    "MM Драма HD",
    "MM История HD",
    "MM Макромир HD",
    "MM Мегамир HD",
    "MM Микромир HD",
    "MM Погружение HD",
    "MM Приключения HD",
    "MM Скорость HD",
    "MM Триллер HD",
    "MM Фантастика HD",
    "MMA TV",
    "MMA-TV",
    "MMA-TV.com HD",
    "MMA-TV.com",
    "Moldova 1",
    "Moldova 2",
    "Moldova TV sd",
    "Monster Jam",
    "More Than Sports TV [DE]",
    "More Than Sports TV",
    "More Than Sports",
    "mosfilm",
    "MOSOBR.TV",
    "Motor sport revue HD",
    "Motor Sport Revue",
    "Motor Sport",
    "Motorvision TV",
    "Motorvision",
    "MovieToper FHD",
    "MovieToper",
    "Movify Kino",
    "MS Animated HD",
    "MS Animated",
    "MS Crime HD",
    "MS Magic HD",
    "MS NOW",
    "MS Prisons HD",
    "MS Toons HD",
    "MS Young Blood HD",
    "MSG [US]",
    "MSG",
    "MTV 00s",
    "MTV [US]",
    "MTV Live",
    "Muloqot TV",
    "MULTIMEDIOS COSTA RICA",
    "Music Box Classic",
    "Music Channel",
    "Music Reload TV HD",
    "MusicBox Georgia",
    "Muz TV AZ",
    "MUZVAR TV HD",
    "Muzzon",
    "Muzzone",
    "MY5 HD",
    "My5 International",
    "MY5 SD",
    "My5 UZ",
    "My5",
    "MY5TV International",
    "Myday TV",
    "MYDAYTV",
    "MyTime movie network Spain HD",
    "MyTV",
    "MyTV2",
    "MyTV3",
    "MyZen TV",
    "MyZen.tv HD",
    "München TV",
    "Namangan",
    "Nano TV",
    "nano",
    "Nasaf TV",
    "nashanovoekino",
    "Nashe Radio",
    "Nat Geo People [PL]",
    "Nat Geo Wild HD",
    "Nat Geo Wild",
    "natanatty",
    "Natgeo WILD",
    "National 24 Plus",
    "National Geographic Baltic",
    "National Geographic Channel HD",
    "National Geographic HD",
    "National Geographic Wild HD",
    "National_Geographic_HD",
    "Naturescape",
    "Nautical Channel",
    "Navigator TV",
    "NavigatorTV",
    "Navo",
    "Navoi",
    "NBA TV HD",
    "Nbc 15 Madison Wi (Wmtvnbc) (720P)",
    "New Armenia",
    "New Orleans TV (720p)",
    "NewsMax TV [US]",
    "NEXT TV",
    "NEXT Venture",
    "NEXT-TV (Башкортостан г.Нефтекамск)",
    "NEXT-TV",
    "NFL Network HD",
    "NHK World Japan",
    "NHL (Интересные моменты)",
    "Nick Jr HD",
    "Nick jr",
    "Nickelodeon Baltic",
    "Nickelodeon",
    "NickMusic [US]",
    "NickMusic",
    "Nicktoons",
    "nikatv",
    "NIKI Junior HD",
    "Niki Junior",
    "NIKI Kids HD",
    "NikNik-TV",
    "NM Television",
    "nntv",
    "No Name",
    "Noroeste TV HD",
    "Nostalgiya",
    "nostalgy",
    "Nothing Scripted",
    "Novella TV HD",
    "Novella TV",
    "NOVXX",
    "Now 70's",
    "Now 80's",
    "Now Rock",
    "NOW series HD",
    "Noyan Tapan",
    "NR1 DANCE HD",
    "Nr1 Turk",
    "NRJ Ukraine",
    "NRWision (1080p)",
    "NRWision",
    "nst",
    "NTA",
    "ntm13",
    "ntv (o'zbekiston)",
    "NTV HD TR",
    "NTV HD",
    "Ntv UZ",
    "NTV",
    "Number 1 Ask",
    "Number 1 Damar",
    "Number 1 Dance",
    "Number 1 TV",
    "NUMBER 1",
    "NUMBER ONE TÜRK ASK",
    "NUMBER ONE TÜRK DAMAR",
    "NUMBER ONE TÜRK DANCE",
    "Number One Türk",
    "Nur TV",
    "Nurafshon TV UZ",
    "Nurafshon TV",
    "Nurafshon",
    "Nuta TV [PL]",
    "Nuta TV",
    "nvk-online",
    "O'zbekiston 24 HD",
    "O'zbekiston 24",
    "O'zbekiston tarixi FHD",
    "O'zbekiston tarixi",
    "O'zbekiston",
    "O'zbekiston-24",
    "O'zbekiston4",
    "O2",
    "O2TV",
    "Oberpfalz TV",
    "Obieqtivi TV",
    "OBOZREVATEL HD",
    "OBOZREVATEL TV HD",
    "Ocko Expres",
    "Ocko Express Tv",
    "Ocko Express",
    "Ocko Gold",
    "Ocko STAR",
    "Ocko TV",
    "Odishi TV",
    "OK Dessau (1080p) [Not 24/7]",
    "OK Dessau",
    "OK Magdeburg (1080p)",
    "OK Merseburg-Querfurt (1080p) [Not 24/7]",
    "OK Salzwedel (1080p) [Not 24/7]",
    "OK Stendal (1080p)",
    "OK Stendal",
    "OK Wernigerode (1080p)",
    "Okko Sport",
    "Okko Спорт",
    "Olay Türk TV Kayseri",
    "On Air TV",
    "On4",
    "Onda Algeciras TV",
    "Onda Valencia (720p)",
    "One Planet HD",
    "One Planet",
    "One Planet+ HD",
    "ONE",
    "ONE2 HD",
    "Ontustik",
    "Orange TV (720p)",
    "ORDU BEL TV",
    "Orler TV Favaro Veneto",
    "OstWest",
    "OTV [LV]",
    "OTV HD",
    "OTV",
    "OVAA TV",
    "Ozbekiston Tarixi HD UZ",
    "Ozbekiston",
    "Palitra News",
    "Palitranews",
    "PanArmenian TV",
    "Pannon RTV",
    "PARADISE ON EARTH Best Travel",
    "Parlamenti",
    "PBS KET (720p)",
    "Persiana Music",
    "Persiana Nostalgia",
    "PERSIANA SONNATI TV",
    "PFL MMA",
    "Pink O TV",
    "PK TV",
    "Plan B HD",
    "Plan B",
    "PLANETA RTR",
    "planeta",
    "Play 4 Fun",
    "Play-x (90-е) HD",
    "Play-x 6 кадров HD",
    "Play-x Beast Games HD",
    "Play-x music Enigmatic HD",
    "Play-x Retro SEX HD",
    "Play-x Spy Sex HD",
    "Play-x Баскетбол USA HD",
    "Play-x Военная приемка HD",
    "Play-x География уральских пельменей HD",
    "Play-x Женский стендап HD",
    "Play-x Игра (тнт) HD",
    "Play-x Импровизаторы HD",
    "Play-x Кинозал 1 HD",
    "Play-x Кинозал 10 HD",
    "Play-x Кинозал 11 HD",
    "Play-x Кинозал 12 HD",
    "Play-x Кинозал 13 HD",
    "Play-x Кинозал 14 HD",
    "Play-x Кинозал 15 HD",
    "Play-x Кинозал 16 HD",
    "Play-x Кинозал 17 HD",
    "Play-x Кинозал 18 HD",
    "Play-x Кинозал 19 HD",
    "Play-x Кинозал 2 HD",
    "Play-x Кинозал 20 HD",
    "Play-x Кинозал 21 HD",
    "Play-x Кинозал 22 HD",
    "Play-x Кинозал 23 HD",
    "Play-x Кинозал 3 HD",
    "Play-x Кинозал 4 HD",
    "Play-x Кинозал 5 HD",
    "Play-x Кинозал 6 HD",
    "Play-x Кинозал 7 HD",
    "Play-x Кинозал 8 HD",
    "Play-x Кинозал 9 HD",
    "Play-x Между нами HD",
    "Play-x Муз Golden Hits 90s HD",
    "Play-x Муз Music 80s HD",
    "Play-x Муз Old School Hits HD",
    "Play-x Муз Rock Pop Ballads HD",
    "Play-x Муз Блатной Хит HD",
    "Play-x Муз Муз Золотой век HD",
    "Play-x Пограничный HD",
    "Play-x Последний герой HD",
    "Play-x Про людей и войну HD",
    "Play-x Русская дорога HD",
    "Play-x Тайны Чапман HD",
    "Play-x Уральские пельмени HD",
    "Playboy TV HD",
    "PLAYBOY TV",
    "Pluto TV Cine Acción",
    "Pluto TV Cine Clásico",
    "Pluto TV Comedia",
    "Pluto TV MTV Originals",
    "Pluto TV Telenovelas",
    "Pluto TV Toons Clásico",
    "Poker Night TV",
    "POLAR 2 HD",
    "Polar TV",
    "Polo TV",
    "Polonia 1",
    "Polsat 1",
    "Polsat Games",
    "Polsat News 2",
    "Polsat News Polityka",
    "Polsat News",
    "Polsat Seriale",
    "Polsat Sport 1",
    "Polsat Sport Premium 2",
    "Polsat Sport",
    "Polsat",
    "POP World TV (720p)",
    "Popcorn Theatre",
    "Porn Classic",
    "Pornstar HD",
    "Pornstar",
    "PosTV",
    "POV HD",
    "POV",
    "Power Dance TV",
    "Power Dance",
    "Power Love TV",
    "Power Love",
    "POWER TURK SLOW",
    "POWER TURK TAPTAZE",
    "Power Turk",
    "POWER TV HD",
    "Power TV",
    "Power Türk [TR]",
    "POWER TÜRK AKUSTIK",
    "POWER TÜRK HD",
    "POWER TÜRK SLOW",
    "POWER TÜRK TAPTAZE",
    "PowerTurk Akustik",
    "PowerTurk Taptaze",
    "PowerTürk Akustik TV",
    "PowerTürk Akustik",
    "PowerTürk Slow TV",
    "PowerTürk Slow",
    "PowerTürk Taptaze TV",
    "PowerTürk Taptaze",
    "PowerTürk TV",
    "Poytaxt Ru",
    "Poytaxt Uz",
    "Prima News SD",
    "Prima Tv Moldova HD",
    "Prima TV Sicilia",
    "prima-tv",
    "Primocanale Sport",
    "Private HD",
    "Pro Arena",
    "Pro TV Chisinau HD",
    "Pro TV Moldova",
    "Pro TV",
    "PRO100 TV",
    "Pro100",
    "Prodigy_movie",
    "ProKino HD",
    "ProSieben",
    "PROSTO DRIVE",
    "Prosto Fishing",
    "Prosto GAME",
    "Prosto Night",
    "Prosto Park",
    "Prosto Ukraine",
    "Prosto.TV",
    "Provence",
    "PULS 2",
    "Pulsi TV",
    "PunktUM",
    "Punt 3 Vall Uixó HD",
    "Q Sport Arena",
    "Q Sport League",
    "Qaf TV",
    "Qaraqalpaqstan",
    "Qartuli Arkhi",
    "Qartuli TV",
    "Qasaqstan",
    "Qashkadaryo",
    "Qazaq TV (KZ)",
    "Qazaqstan TV",
    "QAZAQSTAN",
    "QAZSPORT HD",
    "Qello Concerts by Stingray",
    "QVC Style",
    "R Serials",
    "RACER International",
    "Racing America",
    "Radio 51",
    "Radio Grand",
    "Radio Ibiza TV",
    "Radio Iglesias",
    "Radio Love FM",
    "RADIO PADOVA TV",
    "Radio Relax",
    "Radio ROKS",
    "RADIO STUDIO ONE VISUAL TV",
    "Radio SWH TV",
    "RADIO ZETA HD TV",
    "RadioJazz",
    "RADIOKIDS.FM",
    "RAI 1 HD",
    "RAI 2 HD",
    "Rai 3 HD",
    "Rai News 24",
    "Rai Sport HD",
    "RAI Sport",
    "Ran 1",
    "Ran TV Israel",
    "Razer Sharp 60 fps 4K",
    "rbc",
    "RBK",
    "RDS Social TV",
    "Re TV",
    "Re:TV HD",
    "Re:TV",
    "Real Madrid",
    "Realitatea TV",
    "Reality Kings TV HD",
    "ReanimatoR_VHS",
    "RecreatiON",
    "Red Bull TV [AT]",
    "Red Lips 18+",
    "Red",
    "Redlight HD",
    "RELAX",
    "RELAXON",
    "Reload Radio Music Power HD",
    "REN MD",
    "Renessans TV UZ",
    "Renessans TV",
    "Reshet 13",
    "Reteveneta",
    "RETRO MUSIC TV",
    "Retro Music",
    "Retro TV",
    "Retro",
    "Retro_Music_Television",
    "Retrovision Classic",
    "Retrovision Motor",
    "Retrovision Movies",
    "Retrovision Кинопанорама",
    "Retrovision",
    "Revel TV",
    "RFO",
    "Riga TV24 HD LV",
    "Riga TV24",
    "RIMEX TV",
    "Rioni",
    "RiZE TURK TV",
    "RK TV",
    "RNF",
    "Rock TV",
    "Romance HD",
    "ROMEO Video",
    "Rossiya24",
    "RossiyaHD",
    "Rouge TV 720p",
    "Rough",
    "RT DOC HD",
    "RT Documentary",
    "RT HD",
    "RT News HD",
    "RT News",
    "RT Д English",
    "RTD English",
    "RTG HD",
    "RTG International",
    "RTP Sicilia",
    "RTV",
    "RTVI Retro",
    "RTVS Sport HD [SK]",
    "RTД HD",
    "RTД",
    "RU .Black",
    "RU Amedia 2",
    "RU Amedia HIT",
    "RU Sci Fi",
    "RU TV Беларусь",
    "Ru TV",
    "RU Viasat Explore ᵀᵛˢʰᵃᵐ",
    "RU Viasat History ᵀᵛˢʰᵃᵐ",
    "RU Viasat Nature ᵀᵛˢʰᵃᵐ",
    "RU viju TV1000 Action",
    "RU viju TV1000 Русское",
    "RU viju TV1000",
    "RU viju+ Comedy",
    "RU viju+ megahit",
    "RU viju+ premiere",
    "RU Авто24 ᵀᵛˢʰᵃᵐ",
    "RU Бобер ᵀᵛˢʰᵃᵐ",
    "RU Большая азия ᵀᵛˢʰᵃᵐ",
    "RU Диалоги о рыбалке ᵀᵛˢʰᵃᵐ",
    "RU Живи Активно ᵀᵛˢʰᵃᵐ",
    "RU Загородная Жизнь ᵀᵛˢʰᵃᵐ",
    "RU Здоовое ТВ ᵀᵛˢʰᵃᵐ",
    "RU Киносат",
    "RU Киносвидание",
    "RU Кинохит",
    "RU Моя планета ᵀᵛˢʰᵃᵐ",
    "RU Мужской ᵀᵛˢʰᵃᵐ",
    "RU Наша тема ᵀᵛˢʰᵃᵐ",
    "RU Русский Бестселлер",
    "RU Русский Детектив",
    "RU Русский Роман",
    "RU Точка отрыва ᵀᵛˢʰᵃᵐ",
    "Ru-TV",
    "RU.TV Moldova",
    "RU.TV Беларусь",
    "RU.TV",
    "Rugby TV",
    "Rus serials",
    "Russia Today HD",
    "Russia Today",
    "Russian Extreme HD",
    "Russian HD",
    "RUSSIAN MUSIC BOX",
    "Russian MusicBox",
    "Russian r",
    "Russian",
    "Russischer Jahrmarkt",
    "Rustavi 2",
    "Ruxsor TV",
    "S Sport 2 [TR]",
    "S Sport 2",
    "Saarland Fernsehen 1",
    "Saarland Fernsehen 2",
    "Safina",
    "Saga TV HD",
    "Samarkand",
    "Samepo Arkhi",
    "San Porto",
    "Saperavi TV",
    "Saryarqa",
    "Sat7 Turk",
    "Sat7 Türk",
    "Sat7turk",
    "Satranç TV",
    "Scarface",
    "Sci Fi",
    "Sci-Fi",
    "Scripach tv FHD",
    "Scripach TV",
    "ScripachTV",
    "SD REX",
    "Sdasu TV",
    "Sea TV HD",
    "SeleCaoTV",
    "SeleCaoTV1",
    "Semerkand TV",
    "Serial Productions",
    "serial4u",
    "SerialTV",
    "Setanta Kyrgyzstan",
    "Setanta Qazaqstan HD",
    "Setanta Qazaqstan",
    "Setanta Sport Eurasia Plus",
    "Setanta Sport Eurasia",
    "Setanta Sport+ [KZ]",
    "Setanta Sports 1 GE",
    "Setanta Sports 1 HD",
    "Setanta Sports 1 KZ",
    "Setanta Sports 1",
    "Setanta Sports 2 HD",
    "Setanta Sports 2",
    "Setanta Sports 3",
    "Setanta Sports HD",
    "Setanta Sports Ukraine HD",
    "Setanta Sports+ HD Украина",
    "Setanta Sports+ HD",
    "Setanta Sports+ Ukraine HD",
    "Setanta Казахстан HD",
    "Setanta",
    "Setanta_Sports_1_HD",
    "Setanta_Sports_2_HD",
    "Sevimili UZ",
    "Sevimli",
    "Sezoni TV",
    "ShaidenRogue на свежем воздухе (14min)",
    "Shant Gyumri",
    "Shant HD",
    "Shant Kids",
    "Shant music",
    "shant premium",
    "shant serial",
    "Shifo TV",
    "Shoghakat",
    "Shoni TV",
    "Shopping Live",
    "Shot TV",
    "shottv",
    "Show TV",
    "Shoxakat TV",
    "Silk Documentary",
    "Silk Hollywood Movies",
    "Silk kids",
    "Silk Movie Collection",
    "Silk Sport 1",
    "Silk Sport 2",
    "Silk Sport 3",
    "Silk Universal",
    "Silk Way",
    "simferopol24",
    "Simpsons Channel",
    "Sinema 360",
    "Sirdaryo",
    "sixx",
    "Sky Cinema Action HD DE",
    "Sky Cinema Family HD DE",
    "Sky Cinema Special HD DE",
    "SKY Folk TV",
    "SKY HIGH ARCHIVE FHD",
    "Sky High Bat S FHD",
    "SKY HIGH BEYOND S FHD",
    "Sky High Beyonds FHD",
    "SKY HIGH BOOM FHD",
    "Sky High Brain HD",
    "SKY HIGH BRICK S FHD",
    "Sky High Bunny HD",
    "SKY HIGH CASE VHS FHD",
    "SKY HIGH CIVIL S FHD",
    "Sky High Concert HD",
    "SKY HIGH DOC FHD",
    "Sky High Doc HD",
    "SKY HIGH DRAGON FHD",
    "Sky High DRAMA 4K",
    "Sky High Dust S FHD",
    "SKY HIGH FEAR VHS FHD",
    "Sky High Fear VHS",
    "SKY HIGH FIREZONE FHD",
    "SKY HIGH FUTURE FHD",
    "SKY HIGH GUNRUSH FHD",
    "SKY HIGH HEART S FHD",
    "SKY HIGH HORROR 4K HDR",
    "SKY HIGH JETIX HD",
    "SKY HIGH JOKE FHD",
    "SKY HIGH JUNIOR FHD",
    "Sky High LO FI HD",
    "SKY HIGH LOFI HD",
    "SKY HIGH MEDY VHS FHD",
    "Sky High Mono FHD",
    "Sky High Music HD",
    "SKY HIGH NATURE FHD",
    "Sky High Nature HDR 4K",
    "Sky High Neon VHS FHD",
    "SKY HIGH NEONVHS FHD",
    "Sky High Nick S FHD",
    "Sky High Nord S FHD",
    "Sky High Quant S FHD",
    "SKY HIGH RED S FHD",
    "Sky High RedRoom FHD",
    "Sky High Ring S FHD",
    "Sky High Romix FHD",
    "SKY HIGH SCREAM FHD",
    "Sky High Scream HD",
    "Sky High SEX",
    "SKY HIGH SPACE 4K HDR",
    "SKY HIGH STRIKE VHS FHD",
    "SKY HIGH TANOS S FHD",
    "SKY HIGH TEEN FHD",
    "SKY HIGH TOON S FHD",
    "SKY HIGH TRACE S FHD",
    "Sky High Undead FHD",
    "SKY HIGH VOTUM S FHD",
    "SKY HIGH WIZZ VHS FHD",
    "SKY HIGH WOONDER FHD",
    "SKY HIGH ZIPPY S FHD",
    "Sky Sport 1 HD DE",
    "Sky Sport 2 HD DE",
    "Sky Sports Cricket HD",
    "Sky Sports F1 HD",
    "Sky Sports Main Event HD",
    "Sky Sports Premier League HD",
    "Sky Sports Premier League",
    "SKY SPORTS RACING UK",
    "Slow Karadeniz",
    "Smartzone HD",
    "Sochi 24 HD",
    "Sochi Live HD",
    "Sochi Live",
    "Solo ink",
    "Song TV Armenia",
    "Song TV Russia",
    "Song TV Армения",
    "Song TV Россия",
    "Song TV",
    "Sonnenklar.TV",
    "Sony Sci-Fi",
    "Sony Sports Ten 1 HD",
    "Sony Sports Ten 1",
    "Sony Sports Ten 3 HD",
    "Sony Sports Ten 3",
    "Sony Sports",
    "sovainfo",
    "Space Stream",
    "Spirit TV",
    "Sport 1 Baltic",
    "Sport 1 HD DE",
    "Sport 1 Israel",
    "Sport 2 Israel",
    "Sport 3 Israel",
    "Sport 4 Israel",
    "Sport 5 Israel",
    "Sport 6 Israel",
    "Sport Plus Kazakhstan",
    "Sport Plus",
    "Sport UZ HD",
    "Sport Uz",
    "sport Арена HD",
    "sport Арена",
    "sport Боец",
    "sport Игра HD",
    "sport Игра",
    "sport Страна",
    "sport Футбол 1 HD",
    "sport Футбол 2 HD",
    "sport Футбол 3 HD",
    "Sport1 UA 1 HD",
    "Sports Grid",
    "SportsGrid",
    "SSAFilm",
    "Star Cinema HD",
    "Star Cinema Россия HD",
    "Star Cinema",
    "Star Family HD",
    "Star Family Россия HD",
    "Star Family",
    "Star Media",
    "STAR TV",
    "Start Air HD",
    "START Air",
    "Start World HD",
    "START World",
    "Starvision TV",
    "Stereo Plus",
    "Stingray Classic Rock",
    "Stingray Classica",
    "Stingray CMusic",
    "Stingray DJAZZ",
    "Stingray Easy Listening",
    "Stingray Flashback 70s",
    "Stingray Hit List",
    "Stingray Holiday Hits",
    "Stingray Hot Country",
    "Stingray Karaoke",
    "Stingray Naturescape",
    "Stingray Nothin' But 90s",
    "Stingray Pop Adult",
    "Stingray Remember the 80s",
    "Stingray Rock Alternative",
    "Stingray Romance Latino",
    "Stingray Smooth Jazz",
    "Stingray Soul Storm",
    "Stingray The Spa",
    "Stingray Today's KPOP",
    "Stingray Today's Latin Pop",
    "Stingray Urban Beat",
    "Stopklatka TV",
    "StrahTV Sky2000 HD",
    "StrahTV The X-Files",
    "StrahTV VHS HD",
    "StrahTV WWE Russian",
    "StrahTV Весёлая Карусель HD",
    "StrahTV Ералаш HD",
    "StrahTV Интерны ТВ",
    "StrahTV Космо HD",
    "StrahTV Кроха ТВ",
    "StrahTV Назад в 90-e",
    "StrahTV Сваты",
    "StrahTV СССР",
    "StrahTV Страх HD",
    "StrahTV Универ ТВ",
    "StrahTV Фантастика HD",
    "StrahTV Хит HD",
    "Stream CITY",
    "Strongman Champions",
    "STV HD",
    "STV Pirma! HD",
    "STV Pirmā!",
    "stv24",
    "STZ Telebista (1080p)",
    "Sumiko HD",
    "Sun RTV",
    "SunFM Gold",
    "SunFM Rock",
    "SUNSET 1 HD",
    "SunsetOne",
    "Super Baltic HD",
    "Super Baltic",
    "Super Mario 2: Galaktika 2026 🔥",
    "Super Plus",
    "Super Polsat",
    "Super Radio",
    "Super RTL HD",
    "Super RTL",
    "Super Six",
    "Super TV Brescia",
    "Super TV HD",
    "Super+ HD",
    "Surxondaryo MTRK",
    "Surxondaryo",
    "Suspense HD",
    "swat2k",
    "Sweet Kino HD",
    "Swiss Sport TV [CH]",
    "SYFY",
    "Syunik TV",
    "Sälem Älem",
    "Sälem, älem! [KZ]",
    "Sälem, älem!",
    "sХузур ТВ",
    "T News",
    "t24",
    "Taevas TV7 EE",
    "Tafu TV",
    "Tamilan TV",
    "Tanamgzavri",
    "Taraqqiyot TV UZ",
    "Taraqqiyot TV",
    "Taraqqiyot",
    "tasix-media kino",
    "Tasix-Media",
    "Tatai TV",
    "Tava TV",
    "Tavush TV",
    "TBMM TV",
    "TBN Armenia",
    "TBN Baltia EE",
    "TBN BALTIA",
    "TDK 42",
    "Techno Warehouse",
    "Teen",
    "Tele 5",
    "Tele Elx HD",
    "TeleChiara Vicenza",
    "Telefoggia",
    "telePAVIA",
    "Telequattro Friuli",
    "TeleRomagna24",
    "Teletricolore Reggio",
    "Televízia OSEM",
    "Telewizja Torun",
    "Tempo TV",
    "Tennis Channel",
    "Tennis+",
    "Terra HD",
    "TEVE 2 HD",
    "Teve2 HD",
    "Teve2",
    "The Country Network [US]",
    "The Explorers HD",
    "The explorers",
    "The Fishing and Hunting",
    "THE MOST BEAUTIFUL 4K 60fps",
    "The World Poker Tour",
    "The_Last_of_Us",
    "This is Bulgaria HD",
    "Threesome",
    "THT",
    "Tiankov FOLK",
    "Tiji",
    "Time Line",
    "Timeless Dizi Channel",
    "TimeToHorror FHD",
    "TimeToMovie FHD",
    "TimeToMovie",
    "Tiny4k UHD",
    "Tiny4K",
    "TinyTeen HD",
    "Tivi 6",
    "TLC HD",
    "TMTV",
    "TNA Wrestling Channel",
    "TNT Music",
    "tnv",
    "Tok TV",
    "Ton TV",
    "TOP Barca",
    "Top Shop TV",
    "TopMoment LiVE",
    "Toshkent HD",
    "Toshkent",
    "Tr VPN SPORT 1",
    "Tr VPN SPORT 2",
    "Tr VPN SPORT 3",
    "Trace Gospel",
    "Trace Latina",
    "Trace Sport Stars HD",
    "Trace Sports",
    "Trace Urban HD",
    "Trace Urban",
    "TrainStream",
    "Trash HD",
    "Trash",
    "Travel Channel HD",
    "Travel channel Европа HD",
    "Travel Guide TV",
    "Travel TV",
    "Travel XP HD",
    "Travel+Adventure HD",
    "Travel-tv (O'zbekiston)",
    "Travelxp HD",
    "Travelxp",
    "Trimedio TV",
    "TRT 1HD",
    "TRT Arapca",
    "TRT Avaz",
    "TRT Cocuk",
    "TRT DIYANET COCUK",
    "TRT EBA Ortaokul",
    "TRT Haber TV",
    "TRT Haber",
    "TRT Kurdî",
    "TRT Music",
    "TRT Muzik",
    "TRT Turk TV",
    "TRT TURK",
    "TRT Türk",
    "TRT World",
    "TRT Çocuk",
    "TruckFm",
    "Trwam TV",
    "Tsayg TV",
    "TTV [PL]",
    "TTV Kino",
    "TTV Musiqa",
    "TTV Telekanal",
    "TTV",
    "Tun-Tunik",
    "TURAN TV HD",
    "Turan TV",
    "Turkmen owazy",
    "Turkmen sport",
    "Turkmenistan",
    "TV 1 Kayseri",
    "TV 1",
    "TV 1000 Action",
    "TV 1000",
    "TV 264",
    "TV BRICS",
    "TV BRNO 1 HD",
    "TV Extra",
    "TV Formula",
    "TV Jurmala HD",
    "TV Keszthely",
    "TV Kujawy",
    "TV Maná Russkiy",
    "TV Monitoringi",
    "TV Net",
    "TV Pirveli",
    "TV Plus",
    "TV Puls",
    "TV Republika",
    "TV Rivne 1",
    "TV Ružinov HD",
    "TV Torun",
    "TV Trwam",
    "TV XXI",
    "TV Губерния (Воронеж)",
    "TV ГУБЕРНИЯ",
    "tv-gubernia",
    "TV1 HD",
    "TV1 KG HD [KG]",
    "TV1 KG",
    "TV1",
    "TV1000 Action HD",
    "TV1000 action",
    "TV1000 HD",
    "TV1000 Кино",
    "TV1000 Новелла",
    "TV1000 русское кино",
    "tv1000",
    "tv1000action",
    "TV25",
    "TV3 Gold HD EE",
    "TV3 Gold",
    "TV3 HD EE",
    "TV3 HD LT",
    "TV3 HD LV",
    "TV3 Life HD",
    "TV3 Mini HD",
    "TV3 Plus HD LV",
    "TV4 HD",
    "TV4",
    "TV5 Plus",
    "TV5",
    "TV538 [NL]",
    "TV538",
    "TV5Monde",
    "TV6 HD LT",
    "TV6 HD LV",
    "TV6",
    "TV8 [LT]",
    "TV8 HD LT",
    "TVA Vicenza",
    "TVA",
    "tva-in-ua--2",
    "tva-in-ua-1",
    "tva-in-ua-10",
    "tva-in-ua-11",
    "tva-in-ua-12",
    "tva-in-ua-13",
    "tva-in-ua-14",
    "tva-in-ua-15",
    "tva-in-ua-16",
    "tva-in-ua-17",
    "tva-in-ua-18",
    "tva-in-ua-19",
    "tva-in-ua-20",
    "tva-in-ua-3",
    "tva-in-ua-4",
    "tva-in-ua-5",
    "tva-in-ua-6",
    "tva-in-ua-7",
    "tva-in-ua-8",
    "tva-in-ua-9",
    "tva-x1",
    "tva-x10",
    "tva-x11",
    "tva-x12",
    "tva-x13",
    "tva-x14",
    "tva-x15",
    "tva-x16",
    "tva-x17",
    "tva-x18",
    "tva-x19",
    "tva-x2",
    "tva-x20",
    "tva-x21",
    "tva-x22",
    "tva-x23",
    "tva-x24 G",
    "tva-x3",
    "tva-x4",
    "tva-x5",
    "tva-x6",
    "tva-x7",
    "tva-x8",
    "tva-x9",
    "tva-xx1",
    "tva;org;ua Canale 7",
    "TVBoom",
    "TVBoom.VIP",
    "TVC",
    "TVii.TV HD",
    "tvknews",
    "tvkrasnodar",
    "TVM3",
    "TVN 7",
    "TVN",
    "Tvnet",
    "TVP 1",
    "TVP ABC",
    "TVP Dokument",
    "TVP HD",
    "TVP Historia",
    "TVP Info HD",
    "TVP Info",
    "TVP Kobieta",
    "TVP Kultura",
    "TVP Nauka",
    "TVP Polonia",
    "TVP Rozrywka",
    "TVP Sport",
    "TVP World",
    "TVP1",
    "TVP2",
    "TVP3 Rzeszow",
    "TVPLAY MORTAL KOMBATᴴᴰ",
    "TVPlus Suceava",
    "TVR Moldova",
    "TVR Sport",
    "TVRI",
    "TVT 1",
    "TVT Zgorzelec",
    "TVT",
    "tvzvezda",
    "TVоя Тюмень",
    "TyC Sports",
    "TÜRKMEN SPOR",
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
    "Арсенал FD": "Арсенал"
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
