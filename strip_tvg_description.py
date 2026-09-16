#!/usr/bin/env python3
"""Удаляет атрибут tvg-description из всех .m3u проекта: корень + tv/ video/ ratings/.

Запускать из корня проекта:
    python strip_tvg_description.py --dry-run   # только посчитать, файлы не трогать
    python strip_tvg_description.py             # удалить

Снимается только сам атрибут, остальная строка #EXTINF остаётся байт-в-байт.
Строки, где границу атрибута нельзя определить однозначно (кавычки внутри описания,
строка не похожа на #EXTINF), НЕ меняются — они печатаются списком в конце.
Файлы перезаписываются на месте (без unlink/rename); LF/CRLF и хвостовая пустая
строка сохраняются.
"""
import re
import sys
from pathlib import Path

NAME = "tvg-description"
ANY = re.compile(r"tvg-description\s*=", re.I)
TOKEN = r'(?:\s+|(?<="))[\w-]+=(?:"[^"]*"|[^\s",]*)'
HEAD = re.compile(rf'(#EXTINF:\s*-?\d+(?:\.\d+)?)((?:{TOKEN})*)(\s*,.*)', re.S)
ATTR = re.compile(r'(\s*)([\w-]+)=("[^"]*"|[^\s",]*)')
ENC = dict(encoding="utf-8", errors="surrogateescape", newline="")


def read(path):
    with open(path, **ENC) as fh:
        return fh.read()


def strip_line(line):
    """-> (строка, сколько атрибутов снято, причина — если строку не тронули)"""
    body, cr = (line[:-1], "\r") if line.endswith("\r") else (line, "")
    m = HEAD.fullmatch(body)
    if not m:
        return line, 0, "строка не разбирается как #EXTINF"
    head, attrs, title = m.groups()
    if re.search(r'[\w-]+="', title) or title.count('"') % 2:
        return line, 0, "кавычки после запятой — похоже, внутри описания есть кавычки"
    parts = list(ATTR.finditer(attrs))
    if "".join(p.group(0) for p in parts) != attrs:
        return line, 0, "не удалось разобрать атрибуты"
    out, carry, removed = [], "", 0
    for p in parts:
        ws, rest = p.group(1), p.group(0)[len(p.group(1)):]
        if p.group(2).lower() == NAME:
            removed, carry = removed + 1, carry or ws
        else:
            out.append((ws or carry) + rest)
            carry = ""
    new_body = head + "".join(out) + title
    if ANY.search(new_body):
        return line, 0, "tvg-description= внутри значения другого атрибута"
    return new_body + cr, removed, None


def main():
    args = [a for a in sys.argv[1:] if a != "--dry-run"]
    dry_run = len(args) != len(sys.argv) - 1
    root = Path(args[0] if args else ".").resolve()
    files = sorted(root.glob("*.m3u")) + sorted(root.glob("*/*.m3u"))
    if not files:
        sys.exit(f"В {root} нет .m3u — запускайте из корня проекта (где лежат tv/ и video/)")

    total, changed, skipped = 0, 0, []
    for f in files:
        text = read(f)
        lines, removed = text.split("\n"), 0
        for i, line in enumerate(lines):
            if ANY.search(line):
                lines[i], n, why = strip_line(line)
                removed += n
                if why:
                    skipped.append(f"  {f.relative_to(root)}:{i + 1}: {why}")
        print(f"{removed:6}  {f.relative_to(root)}")
        total += removed
        if removed and not dry_run:
            new = "\n".join(lines)
            assert new.count("\n") == text.count("\n"), f
            with open(f, "w", **ENC) as fh:
                fh.write(new)
            changed += 1

    verb = "найдено" if dry_run else "снято"
    print(f"файлов .m3u: {len(files)}; атрибутов {verb}: {total}"
          + ("" if dry_run else f"; файлов изменено: {changed}"))
    if skipped:
        print(f"\nНе тронуто строк: {len(skipped)} (граница атрибута неоднозначна):")
        print("\n".join(skipped[:50]))
        if len(skipped) > 50:
            print(f"  … и ещё {len(skipped) - 50}")
    if not dry_run:
        left = sum(len(ANY.findall(read(f))) for f in files)
        print(f"осталось tvg-description= во всех .m3u: {left}")

    me = Path(__file__).resolve()
    for py in sorted(root.glob("*.py")):
        rows = [str(i) for i, l in enumerate(read(py).split("\n"), 1) if NAME in l.lower()]
        if rows and py.resolve() != me:
            print(f"внимание: {py.name} (стр. {', '.join(rows)}) работает с {NAME} — "
                  "после его прогона атрибут может вернуться")


if __name__ == "__main__":
    main()
