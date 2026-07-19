#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
group_candidates.py — разносит каналы group-title="candidate" из index.m3u по
СМЫСЛОВЫМ группам. Целевых файлов теперь ЧЕТЫРЕ:
  * index.m3u      — тематические группы (Спорт, Музыка, Новости …);
  * cinema.m3u     — киноканалы и бренды (Кино, VF, Viju, CineMan …);
  * children.m3u   — детские/мультканалы (группа «Детские»);
  * tv_series.m3u  — сериальные каналы (группа «Сериалы»).
Кандидаты живут только в index.m3u. Разнос может увести канал в любой из 4 файлов.

Логика выбора группы (в порядке доверия):
  1) точное совпадение tvg-id с каналом, УЖЕ лежащим в какой-то группе;
  2) совпадение нормализованного имени с уже разобранным каналом;
  3) правила по ключевым словам из group_rules.txt (редактируемый файл);
  4) иначе — канал остаётся в candidate.

Два режима:
  * PROPOSE (по умолчанию): пишет group_proposal.tsv — таблицу
        решение | имя | целевой_файл | группа | основание
    НИЧЕГО в плейлистах не меняет. Ты просматриваешь/правишь TSV
    (меняешь группу/файл, или ставишь в 1-й столбце skip).
  * APPLY (--apply): читает group_proposal.tsv и ФИЗИЧЕСКИ переносит каналы
    в нужные секции нужного файла, пересобирая затронутые файлы (секции
    отсортированы: кириллица, потом латиница). Нетронутые файлы не пишутся.

Безопасность APPLY:
  * бэкапы <file>.group.bak для каждого затронутого файла;
  * md5-гард (если любой файл изменился с момента чтения — стоп);
  * атомарная запись (tmp в той же папке + os.replace);
  * пост-проверка ПО ВСЕМ файлам сразу: мультимножество ВСЕХ стрим-URL и
    мультимножество имён каналов до/после должны совпадать; никаких
    дублей (группа, имя). Любое расхождение → аварийный стоп, файлы не пишутся.

Запуск:
  python3 group_candidates.py                     # propose -> group_proposal.tsv
  python3 group_candidates.py --apply             # применить проверенный TSV
"""
from __future__ import annotations

import argparse
import hashlib
import html
import os
import re
import tempfile
from collections import Counter

CAND = "candidate"
PROPOSAL = "group_proposal.tsv"

# Канонические целевые файлы (basename). candidate живёт в index.m3u.
KNOWN_FILES = ["index.m3u", "cinema.m3u", "children.m3u", "tv_series.m3u"]

# --- нормализация имён (та же логика, что и в enrich_candidates.py) ----------
QUALITY = {"hd", "sd", "fhd", "uhd", "qhd", "4k", "8k", "hq", "hevc", "h265",
           "fullhd", "ultrahd", "1080p", "720p", "576p", "480p", "orig"}
COUNTRY = {"de", "ee", "lt", "lv", "us", "ua", "pl", "ru", "fr", "es", "it",
           "tr", "az", "kz", "by", "ge", "am", "uz", "md", "kg", "tj", "il",
           "gb", "uk", "ca", "at", "ch", "nl", "cz", "sk", "ro", "bg", "rs",
           "gr", "cn", "in", "ir", "tm", "fi", "se", "no", "dk", "hu", "pt"}
STRIP_TAIL = QUALITY | COUNTRY


def normalize(name: str) -> str:
    s = html.unescape(name).strip().casefold()
    s = re.sub(r"\s*\[[^\]]*\]\s*$", "", s)
    tokens = s.split()
    while tokens and tokens[-1] in STRIP_TAIL:
        tokens.pop()
    return " ".join(tokens)


# --- разбор строки #EXTINF --------------------------------------------------
def name_of(extinf: str) -> str:
    masked = re.sub(r'"[^"]*"', lambda m: " " * len(m.group()), extinf)
    i = masked.find(",")
    return extinf[i + 1:].strip() if i != -1 else ""


def id_of(extinf: str) -> str:
    m = re.search(r'tvg-id="([^"]*)"', extinf)
    return m.group(1) if m else ""


def group_of(extinf: str) -> str:
    m = re.search(r'group-title="([^"]*)"', extinf)
    return m.group(1) if m else ""


def sort_key(name: str):
    m = re.search(r"[^\W\d_]", name.strip(), re.U)      # первая буква
    first = m.group(0) if m else ""
    is_cyr = bool(re.match(r"[а-яёА-ЯЁ]", first))
    return (0 if is_cyr else 1, name.strip().casefold())


# ---------------------------------------------------------------------------
# Модель m3u
# ---------------------------------------------------------------------------
class Record:
    __slots__ = ("lines",)

    def __init__(self, lines):
        self.lines = lines                       # список строк, 1-я = #EXTINF

    @property
    def extinf(self):
        return self.lines[0]

    @property
    def name(self):
        return name_of(self.lines[0])

    @property
    def tvg_id(self):
        return id_of(self.lines[0])

    @property
    def group(self):
        return group_of(self.lines[0])

    def set_group(self, g):
        self.lines[0] = re.sub(r'group-title="[^"]*"',
                               f'group-title="{g}"', self.lines[0], count=1)

    def stream_urls(self):
        out = []
        for ln in self.lines[1:]:
            s = ln.strip()
            if s.startswith("#EXTINF"):
                continue
            key = s[1:].strip() if s.startswith("#") else s   # # == запасная
            if "://" in key:
                out.append(key)
        return out

    def text(self):
        return "\n".join(self.lines)


class Section:
    def __init__(self, name):
        self.name = name
        self.records = []
        self.lead = []          # орфаны (строки до первого #EXTINF, не пустые)


def parse_m3u(path):
    text = open(path, encoding="utf-8").read()
    lines = text.split("\n")
    i, pre = 0, []
    while i < len(lines) and not lines[i].startswith("# "):
        pre.append(lines[i])
        i += 1
    sections, cur, buf = [], None, []

    def flush():
        nonlocal buf
        while buf and buf[-1].strip() == "":
            buf.pop()
        if buf:
            cur.records.append(Record(buf))
        buf = []

    while i < len(lines):
        ln = lines[i]
        if ln.startswith("# "):
            if cur is not None:
                flush()
                sections.append(cur)
            cur = Section(ln[2:].strip())
            buf = []
        elif cur is not None:
            if ln.startswith("#EXTINF"):
                flush()
                buf = [ln]
            elif buf:
                buf.append(ln)
            elif ln.strip() != "":
                cur.lead.append(ln)          # орфан до первого #EXTINF
        i += 1
    if cur is not None:
        flush()
        sections.append(cur)
    return "\n".join(pre).rstrip("\n"), sections


def find_section(sections, name):
    for s in sections:
        if s.name == name:
            return s
    return None


def ensure_section(sections, name):
    """тематические секции держим в алфавите, candidate — последней."""
    s = find_section(sections, name)
    if s:
        return s
    s = Section(name)
    non_cand = [x for x in sections if x.name.lower() != CAND]
    pos = len(non_cand)
    for idx, x in enumerate(non_cand):
        if sort_key(name) < sort_key(x.name):
            pos = idx
            break
    sections.insert(pos, s)
    return s


def build_file(preamble, sections, sort_names):
    """sort_names: множество имён секций, которые надо пересортировать."""
    blocks = []
    for sec in sections:
        recs = list(sec.records)
        if sec.lead and recs:                       # орфаны -> в первую запись
            first = recs[0]                          # после #EXTINF, не перед
            first.lines = [first.lines[0]] + sec.lead + first.lines[1:]
            sec.lead = []
        if sec.name in sort_names:
            recs.sort(key=lambda r: sort_key(r.name))
        body = "\n\n".join(r.text() for r in recs) if recs else ""
        blocks.append(f"# {sec.name}\n\n" + body)
    return preamble + "\n\n" + "\n\n".join(blocks) + "\n"


# ---------------------------------------------------------------------------
# Классификация
# ---------------------------------------------------------------------------
def build_reference(models):
    """models: [(sections, filename)]. -> by_id, by_name -> (file, group)."""
    by_id, by_name = {}, {}
    amb_id, amb_name = set(), set()

    def add(d, amb, key, val):
        if not key:
            return
        if key in d and d[key] != val:
            amb.add(key)
        else:
            d.setdefault(key, val)

    for sections, fname in models:
        for sec in sections:
            if sec.name.lower() == CAND:
                continue
            for r in sec.records:
                val = (fname, sec.name)
                add(by_id, amb_id, r.tvg_id, val)
                add(by_name, amb_name, normalize(r.name), val)
    for k in amb_id:
        by_id.pop(k, None)
    for k in amb_name:
        by_name.pop(k, None)
    return by_id, by_name


def canon_file(tf: str) -> str:
    """Приводит указатель целевого файла (из правил/TSV) к basename из KNOWN_FILES."""
    t = os.path.basename(tf.strip()).lower()
    if t.endswith(".m3u"):
        t = t[:-4]
    if "cinema" in t or "кино" in t:
        return "cinema.m3u"
    if "child" in t or "детск" in t:
        return "children.m3u"
    if "foreign" in t or "заруб" in t or "иностран" in t:
        return "foreign.m3u"
    if "series" in t or "serial" in t or "сериал" in t:
        return "tv_series.m3u"
    return "index.m3u"


def load_rules(path):
    rules = []
    if not os.path.exists(path):
        return rules
    for raw in open(path, encoding="utf-8"):
        line = raw.strip()
        if not line or line.startswith("#") or line.count("|") < 2:
            continue
        tf, grp, kws = line.split("|", 2)
        words = [w.strip().casefold() for w in kws.split(",") if w.strip()]
        rules.append((canon_file(tf), grp.strip(), words))
    return rules


# ---------------------------------------------------------------------------
# Спец-классификация по ПАТТЕРНАМ (сильнее ключевых слов): страна, регион,
# Триколор, бренд-пакеты. Срабатывает ДО ref/keyword.
# ---------------------------------------------------------------------------
# Код страны в хвосте имени -> русское имя группы в foreign.m3u.
# (BY=Беларусь уходит в index/Беларусь, а не в foreign.)
COUNTRY_MAP = {
    "DE": "Германия", "ES": "Испания", "US": "США", "CA": "Канада",
    "IN": "Индия", "PL": "Польша", "IT": "Италия", "LT": "Литва",
    "SE": "Швеция", "LV": "Латвия", "EE": "Эстония", "FR": "Франция",
    "TR": "Турция", "BR": "Бразилия", "AE": "ОАЭ", "UK": "Великобритания",
    "GB": "Великобритания", "UA": "Украина", "IL": "Израиль", "RS": "Сербия",
    "KZ": "Казахстан", "PT": "Португалия", "DK": "Дания", "NL": "Нидерланды",
    "QA": "Катар", "CZ": "Чехия", "KR": "Корея", "AZ": "Азербайджан",
    "RO": "Румыния", "NO": "Норвегия", "AT": "Австрия", "JP": "Япония",
    "GR": "Греция", "BG": "Болгария", "AU": "Австралия",
}
_COUNTRY_RE = re.compile(r"\b([A-Z]{2,3})\s*$")
_PAREN_RE = re.compile(r"\(([^)]+)\)\s*$")
_PAREN_SKIP = {"hd", "sd", "fhd", "uhd", "qhd", "4k", "720p", "1080p",
               "576p", "480p", "hq", "orig"}

# Установившиеся бренд-пакеты кино -> имя группы в cinema.m3u. Ключ =
# ПРЕФИКС имени (с учётом регистра). Внутри бренда жанр-каналы отсеиваются
# в Сериалы/Детские/Музыку, остальное -> своя группа бренда в cinema.
MOVIE_BRANDS = {
    "VF ": "VF", "MM ": "MM", "Liberty ": "Liberty", "BCU ": "BCU",
    "CineMan ": "CineMan", "Fresh ": "Fresh",
    "BOX ": "BOX", "Yosso TV ": "Yosso", "Z!": "Z!", "StrahTV ": "StrahTV",
    "Magic ": "Magic", "Eye ": "Eye", "PG ": "PG",
}
# Музыкальные бренды целиком -> Музыка.
MUSIC_BRANDS = ("Bridge ", "FRESH ")

_SERIES_WORDS = (
    "воронины", "друзья", "игра престолов", "клиника", "доктор хаус",
    "мыльные опер", "реальные пацаны", "сашатаня", "сваты", "след",
    "солдаты", "тайны следствия", "теория большого взрыва", "универ",
    "ходячие мертвец", "ситком", "sitcom", "сериал", "series",
    "family guy", "симпсоны", "южный парк", "полицейский с рубл",
    "x-files", "сверхъестественное", "великолепный век",
    # доп. сериальные суб-каналы брендов (StrahTV и т.п.)
    "интерны", "коломбо", "громовы", "однажды в милиции", "ольга",
    "байки из склепа",
)
_KIDS_WORDS = (
    "дисней", "disney", "пиксар", "pixar", "дрим воркс", "дримворкс",
    "dreamworks", "скуби", "scooby", "планктон", "том и джерри",
    "tom and jerry", "малыш", "никелодеон", "nickelodeon", "кроха", "репка",
)
_MUSIC_WORDS = (
    "rock", "рок", "metal", "metall", "метал", "rap", "рэп", "dance", "edm",
    "концерт", "concert", "караоке", "karaoke", "хит-парад",
    "modern talking", "michael jackson", "britney", "король и шут",
    "квартирник",
)


def _has(suf, words):
    # совпадение по ГРАНИЦЕ слова, а не подстроке: "rap" не ловит "bioGRAPhy"
    for w in words:
        if re.search(r"(?<![0-9a-zа-яё])" + re.escape(w) + r"(?![0-9a-zа-яё])",
                     suf):
            return True
    return False


def special_classify(rec):
    """Возвращает (file, group, basis) для паттернов или None."""
    name = rec.name
    # 1) иностранный канал (код страны в хвосте) -> foreign.m3u/<страна>
    m = _COUNTRY_RE.search(name)
    if m:
        code = m.group(1)
        if code == "BY":
            return "index.m3u", "Беларусь", "pat:by"
        if code in COUNTRY_MAP:
            return "foreign.m3u", COUNTRY_MAP[code], "pat:foreign"
    # 2) Триколор (кинозалы) -> cinema/Триколор
    mp = _PAREN_RE.search(name)
    if mp:
        inside = mp.group(1).strip().lower()
        if inside == "триколор":
            return "cinema.m3u", "Триколор", "pat:tricolor"
        # 3) региональный "X (место)" -> Региональные
        if inside not in _PAREN_SKIP:
            return "index.m3u", "Региональные", "pat:regional"
    # 4) музыкальные бренды целиком -> Музыка
    for pref in MUSIC_BRANDS:
        if name.startswith(pref):
            return "index.m3u", "Музыка", "brand:music"
    # 5) кино-бренды: отсев Сериалы/Детские/Музыка, иначе своя группа
    for pref, brand in MOVIE_BRANDS.items():
        if name.startswith(pref):
            suf = name[len(pref):].casefold()
            if _has(suf, _SERIES_WORDS):
                return "tv_series.m3u", "Сериалы", "brand:series"
            if _has(suf, _KIDS_WORDS):
                return "children.m3u", "Детские", "brand:kids"
            if _has(suf, _MUSIC_WORDS):
                return "index.m3u", "Музыка", "brand:music"
            return "cinema.m3u", brand, f"brand:{brand}"
    return None


def classify(rec, by_id, by_name, rules):
    sp = special_classify(rec)
    if sp:
        return sp
    if rec.tvg_id and rec.tvg_id in by_id:
        f, g = by_id[rec.tvg_id]
        return f, g, "ref:id"
    nk = normalize(rec.name)
    if nk in by_name:
        f, g = by_name[nk]
        return f, g, "ref:name"
    low = rec.name.casefold()
    for tf, grp, words in rules:
        for w in words:
            if w and w in low:
                return tf, grp, f"kw:{w}"
    return None


# ---------------------------------------------------------------------------
# PROPOSE
# ---------------------------------------------------------------------------
def do_propose(files, rules, out_path):
    models = [(f["secs"], name) for name, f in files.items()]
    by_id, by_name = build_reference(models)
    # (группа, имя), которые УЖЕ существуют — чтобы не предлагать дубли
    existing = set()
    for name, f in files.items():
        for sec in f["secs"]:
            if sec.name.lower() == CAND:
                continue
            for r in sec.records:
                existing.add((sec.name, r.name))

    cand = find_section(files["index.m3u"]["secs"], "candidate")
    rows, counts, bases = [], Counter(), Counter()
    unmatched, dups = [], []
    planned = set()
    for r in (cand.records if cand else []):
        res = classify(r, by_id, by_name, rules)
        if not res:
            unmatched.append(r.name)
            continue
        tf, grp, basis = res
        if (grp, r.name) in existing or (grp, r.name) in planned:
            dups.append((r.name, grp))          # уже есть в группе — слить вручную
            continue
        planned.add((grp, r.name))
        rows.append(("move", r.name, tf, grp, basis))
        counts[(tf, grp)] += 1
        bases[basis.split(":")[0]] += 1
    rows.sort(key=lambda x: (x[2], x[3], sort_key(x[1])))

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("# ПРЕДЛОЖЕНИЕ разбора кандидатов. Проверь и поправь, потом: "
                "python3 group_candidates.py --apply\n")
        f.write(f"# всего кандидатов: {len(cand.records) if cand else 0}; "
                f"предложено: {len(rows)}; осталось в candidate: {len(unmatched)}\n")
        f.write("# способ: точное совпадение id/имени с уже разобранными "
                f"({bases.get('ref',0)}) + ключевые слова ({bases.get('kw',0)})\n")
        f.write("# столбцы: решение<TAB>имя<TAB>целевой_файл<TAB>группа<TAB>основание\n")
        f.write("#   решение: move (перенести) или skip (оставить в candidate)\n")
        f.write("#   целевой_файл: index.m3u / cinema.m3u / children.m3u / tv_series.m3u\n")
        f.write("#\n")
        for kv in sorted(counts.items(), key=lambda x: (-x[1], x[0])):
            (tf, grp), n = kv
            f.write(f"# {n:>4}  {tf:<13} {grp}\n")
        f.write("#\n")
        for dec, nm, tf, grp, basis in rows:
            f.write(f"{dec}\t{nm}\t{tf}\t{grp}\t{basis}\n")
        if dups:
            f.write("#\n# --- ДУБЛИ: канал уже есть в этой группе. Скрипт их НЕ "
                    "трогает (оставляет в candidate). Слей ссылку вручную или "
                    "переименуй. ---\n")
            for nm, grp in sorted(dups, key=lambda x: (x[1], sort_key(x[0]))):
                f.write(f"# dup\t{nm}\t-> {grp}\n")
        if unmatched:
            f.write("#\n# --- остаются в candidate (не распознаны) ---\n")
            for nm in sorted(unmatched, key=sort_key):
                f.write(f"# skip\t{nm}\n")

    print(f"Предложение записано: {out_path}")
    print(f"  кандидатов: {len(cand.records) if cand else 0}; "
          f"предложено к переносу: {len(rows)}; "
          f"дубли (в candidate): {len(dups)}; "
          f"не распознано (в candidate): {len(unmatched)}")
    print(f"  основания: ref (совпадение) {bases.get('ref',0)}, "
          f"keyword {bases.get('kw',0)}")
    print("\n  ТОП групп по числу каналов:")
    for (tf, grp), n in sorted(counts.items(), key=lambda x: (-x[1], x[0]))[:25]:
        print(f"    {n:>4}  {tf:<13} {grp}")
    print("\n  Проверь/поправь файл и запусти: python3 group_candidates.py --apply")


# ---------------------------------------------------------------------------
# APPLY
# ---------------------------------------------------------------------------
def read_proposal(path):
    moves = {}
    if not os.path.exists(path):
        raise SystemExit(f"Нет файла предложения {path}. Сначала запусти без --apply.")
    for raw in open(path, encoding="utf-8"):
        if raw.startswith("#") or not raw.strip():
            continue
        parts = raw.rstrip("\n").split("\t")
        if len(parts) < 4:
            continue
        dec, nm, tf, grp = parts[0], parts[1], parts[2], parts[3]
        if dec.strip().lower() != "move":
            continue
        moves[nm] = (canon_file(tf), grp.strip())
    return moves


def md5_bytes(b):
    return hashlib.md5(b).hexdigest()


def atomic_write(path, text):
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _url_key(ln):
    s = ln.strip()
    if s.startswith("#EXT"):          # директивы m3u (#EXTINF/#EXTM3U/#EXTGRP)
        return None
    key = s[1:].strip() if s.startswith("#") else s   # #url == запасная
    return key if "://" in key else None


def all_urls(secs):
    c = Counter()
    for s in secs:
        for ln in s.lead:                     # орфаны над первым #EXTINF
            k = _url_key(ln)
            if k:
                c[k] += 1
        for r in s.records:
            c.update(r.stream_urls())
    return c


def urls_in_text(t):
    c = Counter()
    for ln in t.split("\n"):
        k = _url_key(ln)
        if k:
            c[k] += 1
    return c


def all_names(secs):
    c = Counter()
    for s in secs:
        for r in s.records:
            c[r.name] += 1
    return c


def do_apply(files, moves, backup=True):
    # files: dict basename -> {"path", "secs", "pre", "orig"}
    all_secs = [f["secs"] for f in files.values()]
    before_urls, before_names = Counter(), Counter()
    for secs in all_secs:
        before_urls += all_urls(secs)
        before_names += all_names(secs)

    cand = find_section(files["index.m3u"]["secs"], "candidate")
    if not cand:
        raise SystemExit("В index.m3u нет секции candidate.")

    touched = {name: set() for name in files}
    touched["index.m3u"].add("candidate")
    moved, dups = 0, []
    keep = []
    present = {r.name for r in cand.records}
    for r in cand.records:
        tgt = moves.get(r.name)
        if not tgt:
            keep.append(r)
            continue
        tf, grp = tgt
        if tf not in files:
            raise SystemExit(f"Неизвестный целевой файл в плане: {tf}")
        target = ensure_section(files[tf]["secs"], grp)
        if any(x.name == r.name for x in target.records):   # уже есть — не плодим
            keep.append(r)
            dups.append((r.name, grp))
            continue
        r.set_group(grp)
        target.records.append(r)
        touched[tf].add(grp)
        moved += 1
    cand.records = keep
    missing = [nm for nm in moves if nm not in present]

    # rebuild (все файлы; нетронутые получат идентичный набор записей)
    new_text = {name: build_file(f["pre"], f["secs"], touched[name])
                for name, f in files.items()}

    # verify (модель)
    after_urls, after_names = Counter(), Counter()
    for secs in all_secs:
        after_urls += all_urls(secs)
        after_names += all_names(secs)
    assert before_urls == after_urls, "URL-мультимножество разошлось (модель)!"
    assert before_names == after_names, "имена каналов разошлись!"
    # verify (сериализация: то, что реально уйдёт на диск)
    txt_urls = Counter()
    for name in files:
        txt_urls += urls_in_text(new_text[name])
    assert txt_urls == before_urls, "URL-мультимножество разошлось (в тексте)!"
    seen = set()
    for secs in all_secs:
        for s in secs:
            for r in s.records:
                key = (s.name, r.name)
                assert key not in seen, f"дубль (группа,имя): {key}"
                seen.add(key)

    # md5-гард: файлы не изменились с момента чтения
    for name, f in files.items():
        with open(f["path"], "rb") as fh:
            if md5_bytes(fh.read()) != md5_bytes(f["orig"]):
                raise SystemExit(f"[STOP] {f['path']} изменился во время работы — не пишу.")

    # писать только затронутые файлы (у нетронутых touched == пусто)
    changed = [name for name in files if touched[name] - ({"candidate"} if name == "index.m3u" else set())]
    if backup:
        for name in changed:
            with open(files[name]["path"] + ".group.bak", "wb") as fh:
                fh.write(files[name]["orig"])
    for name in changed:
        atomic_write(files[name]["path"], new_text[name])

    print(f"Применено. Перенесено каналов: {moved}. "
          f"Дублей оставлено в candidate: {len(dups)}. "
          f"Имён из плана не найдено: {len(missing)}.")
    if dups:
        print("  дубли (уже в группе, слей вручную):",
              ", ".join(f"{n}→{g}" for n, g in dups[:8]),
              "..." if len(dups) > 8 else "")
    if missing:
        print("  нет в candidate:", ", ".join(missing[:10]),
              "..." if len(missing) > 10 else "")
    print("  записаны файлы:", ", ".join(changed) if changed else "(ничего)")
    print("  проверка целостности (URL/имена/дубли) пройдена. Бэкапы *.group.bak.")


# ---------------------------------------------------------------------------
parse_pre_cache = {}
orig_bytes = {}


def load(path):
    with open(path, "rb") as f:
        orig_bytes[path] = f.read()
    pre, secs = parse_m3u(path)
    parse_pre_cache[path] = pre
    return secs


def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description="Разнос кандидатов по группам (4 файла).")
    ap.add_argument("--index", default=os.path.join(script_dir, "index.m3u"))
    ap.add_argument("--cinema", default=os.path.join(script_dir, "cinema.m3u"))
    ap.add_argument("--children", default=os.path.join(script_dir, "children.m3u"))
    ap.add_argument("--tv-series", default=os.path.join(script_dir, "tv_series.m3u"))
    ap.add_argument("--foreign", default=os.path.join(script_dir, "foreign.m3u"))
    ap.add_argument("--rules", default=os.path.join(script_dir, "group_rules.txt"))
    ap.add_argument("--proposal", default=os.path.join(script_dir, PROPOSAL))
    ap.add_argument("--apply", action="store_true",
                    help="применить проверенный group_proposal.tsv")
    ap.add_argument("--no-backup", action="store_true")
    args = ap.parse_args()

    # foreign.m3u может ещё не существовать — создаём с преамбулой (как др. файлы)
    if not os.path.exists(args.foreign):
        with open(args.foreign, "w", encoding="utf-8") as f:
            f.write('#EXTM3U url-tvg="https://iptvx.one/epg/epg.xml.gz, '
                    'https://epgshare01.online/epgshare01/epg_ripper_ALL_SOURCES1.xml.gz"\n')
        print(f"создан пустой {os.path.basename(args.foreign)}")

    file_paths = [args.index, args.cinema, args.children, args.tv_series,
                  args.foreign]
    files = {}
    for p in file_paths:
        if not os.path.exists(p):
            raise SystemExit(f"нет файла: {p}")
        secs = load(p)
        files[os.path.basename(p)] = {"path": p, "secs": secs,
                                      "pre": parse_pre_cache[p],
                                      "orig": orig_bytes[p]}

    if "index.m3u" not in files:
        raise SystemExit("index.m3u обязателен (в нём живут кандидаты).")

    if not args.apply:
        rules = load_rules(args.rules)
        print(f"Режим: PROPOSE (плейлисты не меняются). файлов: {len(files)}, "
              f"правил: {len(rules)}")
        do_propose(files, rules, args.proposal)
    else:
        moves = read_proposal(args.proposal)
        print(f"Режим: APPLY. переносов в плане: {len(moves)}")
        do_apply(files, moves, backup=not args.no_backup)


if __name__ == "__main__":
    main()
