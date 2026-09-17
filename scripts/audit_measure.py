#!/usr/bin/env python3
"""Measure the readability signals of lecture-note pages, for comparison
against healthy siblings. Stdlib only.

Per page:
  words    body words, frontmatter and fenced code excluded
  lect     number of `lectures:` entries in the frontmatter (0 if none)
  w/lect   words per lecture
  meta%    share of ## headings that announce a correction or meta point
           rather than a topic -- the signal that best predicted "easier
           to rewatch than to read"
  mk/100w  correction-marker density
  hdr      words between the H1 and the first ## (the header block)
  tally    printed running totals in prose ("3 of 52")

Usage: measure.py --path <wiki-root> [glob]   (default glob: wiki/sources/**/*.md)
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

META = re.compile(
    r"⚠|⏳|🔮|✅|⚡|📸|💡|⚑|📊"
    r"|\bslip\b|\boverreach\b|\bconvicts?\b|\bwrong\b|\bdeclines?\b"
    r"|leaves open|what it settles|never sa|unnamed|missing",
    re.I,
)
MARKER = re.compile(r"⚠|⏳|🔮|✅|⚡|📸|💡|⚑|📊")
TALLY = re.compile(r"\b\d+ of \d+\b")
FENCE = re.compile(r"^```.*?^```", re.M | re.S)


def split_frontmatter(text: str) -> tuple[str, str]:
    if not text.startswith("---\n"):
        return "", text
    end = text.find("\n---", 4)
    if end == -1:
        return "", text
    return text[4:end], text[end + 4:]


def count_lectures(fm: str) -> int:
    m = re.search(r"^lectures:\s*$((?:\n[ \t]+- .*)+)", fm, re.M)
    if m:
        return len(re.findall(r"^[ \t]+- ", m.group(1), re.M))
    m = re.search(r"^lectures:\s*\[(.*)\]", fm, re.M)
    return len([x for x in m.group(1).split(",") if x.strip()]) if m else 0


def measure(path: pathlib.Path) -> dict | None:
    fm, body = split_frontmatter(path.read_text(encoding="utf-8"))
    heads = re.findall(r"^## (.+)$", body, re.M)
    if not heads:
        return None
    prose = FENCE.sub("", body)
    words = len(prose.split())
    m = re.search(r"^# .+?$(.*?)^## ", body, re.M | re.S)
    hdr = len(m.group(1).split()) if m else 0
    lect = count_lectures(fm)
    meta = [h for h in heads if META.search(h)]
    return {
        "page": path.stem,
        "words": words,
        "lect": lect,
        "wpl": words // lect if lect else 0,
        "meta_pct": round(100 * len(meta) / len(heads)),
        "mk": round(100 * len(MARKER.findall(prose)) / max(words, 1), 2),
        "hdr": hdr,
        "tally": len(TALLY.findall(prose)),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--path", default=".", help="wiki root")
    ap.add_argument("glob", nargs="?", default="wiki/sources/**/*.md")
    a = ap.parse_args()
    base = pathlib.Path(a.path).expanduser().resolve()
    rows = []
    for f in sorted(base.glob(a.glob)):
        try:
            r = measure(f)
        except Exception as e:  # noqa: BLE001
            print(f"  ! {f.name}: {e}", file=sys.stderr)
            continue
        if r:
            rows.append(r)
    if not rows:
        print("no pages matched")
        return 1
    rows.sort(key=lambda r: (-r["wpl"], -r["words"]))
    print(f"{'page':48s} {'words':>6s} {'lect':>4s} {'w/lect':>6s} {'meta%':>5s} {'mk/100w':>7s} {'hdr':>5s} {'tally':>5s}")
    for r in rows:
        print(f"{r['page'][:48]:48s} {r['words']:6d} {r['lect']:4d} {r['wpl']:6d} {r['meta_pct']:4d}% {r['mk']:7.2f} {r['hdr']:5d} {r['tally']:5d}")
    lect_rows = [r for r in rows if r["lect"]]
    if lect_rows:
        wpl = sorted(r["wpl"] for r in lect_rows)
        print(f"\n{len(lect_rows)} lecture pages · median words/lecture {wpl[len(wpl)//2]} · "
              f"min {wpl[0]} · max {wpl[-1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
