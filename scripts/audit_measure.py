#!/usr/bin/env python3
"""Measure the readability signals that the three M4 trims exposed.

Prototype for the wiki-audit skill's measurement pass. Reports, per page:
  words              total length
  meta%              share of ## headings that announce a correction/meta point
                     rather than a topic -- the signal that best predicted
                     "easier to rewatch than to read"
  mk/100w            correction-marker density
  hdr                words in the italic header block (between # title and
                     the first ## heading)
  cat                words in the "## Lectures on this page" catalog
  tally              printed running totals in prose (bookkeeping rule 9)
  new                stale freshness markers
"""
import re
import sys
import pathlib

META = re.compile(
    r'⚠|⏳|🔮|✅|⚡|📸|💡|⚑|📊'
    r'|\bslip\b|\boverreach\b|\bconvicts?\b|\bwrong\b|\bdeclines?\b'
    r'|leaves open|what it settles|never sa|unnamed|missing',
    re.I)
MARKER = re.compile(r'⚠|⏳|🔮|✅|⚡|📸|💡|⚑|📊')
TALLY = re.compile(r'\b\d+ of \d+\b')


def measure(path: pathlib.Path) -> dict | None:
    t = path.read_text(encoding='utf-8')
    body = t.split('---', 2)[-1] if t.startswith('---') else t
    heads = re.findall(r'^## (.+)$', body, re.M)
    if not heads:
        return None
    # header block: between the H1 and the first H2
    m = re.search(r'^# .+?$(.*?)^## ', body, re.M | re.S)
    hdr = len(m.group(1).split()) if m else 0
    c = re.search(r'^## Lectures on this page$(.*?)^## ', body, re.M | re.S)
    cat = len(c.group(1).split()) if c else 0
    words = len(body.split())
    meta = [h for h in heads if META.search(h)]
    return {
        'page': str(path.relative_to(path.parents[len(path.parts) - path.parts.index('wiki') - 2])),
        'words': words,
        'meta_pct': round(100 * len(meta) / len(heads)),
        'heads': len(heads),
        'mk_density': round(100 * len(MARKER.findall(body)) / max(words, 1), 2),
        'hdr': hdr,
        'cat': cat,
        'tally': len(TALLY.findall(body)),
        'new': body.count('🆕'),
    }


def main(root: str, pattern: str = 'wiki/sources/**/*.md') -> None:
    base = pathlib.Path(root)
    rows = []
    for f in sorted(base.glob(pattern)):
        try:
            r = measure(f)
        except Exception as e:                      # noqa: BLE001
            print(f"  ! {f.name}: {e}", file=sys.stderr)
            continue
        if r:
            r['page'] = f.name.replace('.md', '')
            rows.append(r)
    rows.sort(key=lambda r: (-r['meta_pct'], -r['words']))
    print(f"{'page':46s} {'words':>6s} {'meta%':>6s} {'mk/100w':>8s} "
          f"{'hdr':>5s} {'cat':>5s} {'tally':>6s} {'new':>4s}")
    for r in rows:
        print(f"{r['page'][:46]:46s} {r['words']:6d} {r['meta_pct']:5d}% "
              f"{r['mk_density']:8.2f} {r['hdr']:5d} {r['cat']:5d} "
              f"{r['tally']:6d} {r['new']:4d}")
    n = len(rows)
    print(f"\n{n} pages. "
          f"median meta% = {sorted(r['meta_pct'] for r in rows)[n // 2]}, "
          f"pages over 40% meta = {sum(1 for r in rows if r['meta_pct'] > 40)}, "
          f"pages with printed tallies = {sum(1 for r in rows if r['tally'])}, "
          f"pages with 🆕 = {sum(1 for r in rows if r['new'])}")


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else '.',
         sys.argv[2] if len(sys.argv) > 2 else 'wiki/sources/**/*.md')
