#!/usr/bin/env python3
"""
lint_wiki.py — Health check an LLM-managed wiki.

Catches mechanical issues only (broken links, orphan pages, index drift, stub
pages, log gaps). Semantic issues — stale claims, unflagged contradictions,
missing pages on cross-cutting entities — are out of scope; the LLM has to
spot those by reading.

By default writes a dated report to wiki/reports/lint-YYYY-MM-DD.md, then
auto-tracks it: adds an index entry under "Reports" and appends a log entry.
Re-running on the same day overwrites the day's report (idempotent daily).

Usage:
    python lint_wiki.py                              # default: wiki/reports/lint-<today>.md + auto-track
    python lint_wiki.py --path /path/to/wiki-root
    python lint_wiki.py --stdout                     # print to stdout, no file, no tracking
    python lint_wiki.py --report /tmp/lint.md        # custom path; auto-track only if inside wiki/
    python lint_wiki.py --no-track                   # write report file but skip index + log updates
    python lint_wiki.py --stub-words 30 --log-gap-days 60   # tune thresholds

Output is markdown, organized by severity (block, quality, suggestion).
Exit code 1 if any block-severity issue found.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path


LINK_PATTERN = re.compile(
    r"\[(?P<text>[^\]]+)\]\((?P<url>[^)\s]+)(?:\s+\"[^\"]*\")?\)"
)
# Obsidian wiki-links: [[slug]], [[slug|alias]], or [[slug\|alias]] (the pipe is
# escaped as \| inside Markdown tables so it isn't read as a column delimiter).
# Excluding \ from the slug group keeps the trailing backslash out of the slug.
WIKILINK_PATTERN = re.compile(r"\[\[([^\]|\\]+)(?:\\?\|[^\]]*)?\]\]")
LOG_DATE_PATTERN = re.compile(r"^## \[(\d{4}-\d{2}-\d{2})\]")
INDEX_LINK_PATTERN = re.compile(
    r"^\s*-\s*\[([^\]]+)\]\(([^)]+)\)"
)
# Obsidian-style [[slug]] or [[slug|alias]] wiki-links in index entries
INDEX_WIKILINK_PATTERN = re.compile(
    r"^\s*-\s*\[\[([^\]|]+)(?:\|[^\]]*)?\]\]"
)


# Schema version the bundled templates/conventions correspond to.
# Kept in sync with migrate_wiki.py's registry. The script can be invoked from
# any cwd (e.g. via subprocess), so make the script dir importable first.
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from migrate_wiki import EXPECTED_SCHEMA_VERSION
except ImportError:
    EXPECTED_SCHEMA_VERSION = 2


def is_external(url: str) -> bool:
    return url.startswith(("http://", "https://", "mailto:", "ftp://"))


def parse_frontmatter(text: str) -> dict | None:
    """
    Minimal YAML frontmatter parser (stdlib only). Supports `key: value`,
    inline lists `tags: [a, b]`, and block lists:
        tags:
          - a
          - b
    Returns None when there is no frontmatter or it can't be parsed.
    """
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---", 4)
    if end == -1:
        return None
    body = text[4:end]
    data: dict = {}
    current_list_key: str | None = None
    try:
        for line in body.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("- ") and current_list_key:
                data[current_list_key].append(stripped[2:].strip().strip("'\""))
                continue
            if ":" not in stripped:
                return None
            key, _, value = stripped.partition(":")
            key = key.strip()
            value = value.split("#", 1)[0].strip()
            if value == "":
                data[key] = []
                current_list_key = key
            elif value.startswith("[") and value.endswith("]"):
                items = [v.strip().strip("'\"") for v in value[1:-1].split(",")]
                data[key] = [v for v in items if v]
                current_list_key = None
            else:
                data[key] = value.strip("'\"")
                current_list_key = None
    except (ValueError, KeyError, AttributeError, IndexError):
        return None
    return data


def is_anchor_only(url: str) -> bool:
    return url.startswith("#")


def find_md_files(wiki_dir: Path) -> list[Path]:
    """All .md files under wiki/, excluding structural files and auto-generated reports.

    Skips:
      - wiki/index.md, wiki/log.md, wiki/hot.md (structural meta files)
      - wiki/reports/* (auto-generated lint/audit artifacts; tracked separately)
    """
    skip_names = {"index.md", "log.md", "hot.md"}
    reports_dir = (wiki_dir / "reports").resolve()
    out: list[Path] = []
    for p in wiki_dir.rglob("*.md"):
        if p.name in skip_names:
            continue
        try:
            p.resolve().relative_to(reports_dir)
            continue  # path is inside reports/, skip
        except ValueError:
            pass
        out.append(p)
    return out


def collect_links(md_path: Path, wiki_dir: Path) -> list[tuple[str, Path]]:
    """
    Returns list of (raw_url, resolved_path) for all internal links in the file.
    Handles both standard markdown links [text](url) and Obsidian [[slug]] wiki-links.
    Unresolved wiki-links resolve to the nominal path wiki/<slug>.md, which does
    not exist — so check_broken_links reports them instead of skipping silently.
    """
    text = md_path.read_text(encoding="utf-8", errors="replace")
    out: list[tuple[str, Path]] = []

    for m in LINK_PATTERN.finditer(text):
        url = m.group("url")
        if is_external(url) or is_anchor_only(url):
            continue
        target = url.split("#", 1)[0]
        if not target:
            continue
        resolved = (md_path.parent / target).resolve()
        out.append((url, resolved))

    for m in WIKILINK_PATTERN.finditer(text):
        slug = strip_heading_anchor(m.group(1).strip())
        # Same-page heading/block anchor (`[[#Section]]`) — no page to resolve.
        if slug is None:
            continue
        # Attachment embed (`![[image.png]]`, `[[doc.pdf]]`): a file with an
        # extension, not a page. Resolve the literal filename across the vault
        # (wiki/ + raw/ + ...), don't append .md.
        if re.search(r"\.[A-Za-z0-9]+$", slug):
            found = list(wiki_dir.parent.rglob(slug))
            resolved = found[0].resolve() if found else (wiki_dir / slug).resolve()
            out.append((f"[[{slug}]]", resolved))
            continue
        matches = list(wiki_dir.rglob(f"{slug}.md"))
        if matches:
            out.append((f"[[{slug}]]", matches[0].resolve()))
        else:
            out.append((f"[[{slug}]]", (wiki_dir / f"{slug}.md").resolve()))

    return out


def strip_heading_anchor(slug: str) -> str | None:
    """Reduce a wiki-link target to the page it names.

    Obsidian links can address a heading or block as well as a page:
    `[[page#Section]]`, `[[page#^block-id]]`, and — within the same file —
    `[[#Section]]`. Only the part before `#` is a filename.

    Returns the page slug, or None when the link is a *same-page* anchor and
    therefore names no page at all. Without this, `[[#Section]]` is reported as
    a dangling link to a page literally named "#Section".
    """
    if slug.startswith("#"):
        return None
    return slug.split("#", 1)[0].strip()


def check_wikilink_collisions(md_files: list[Path], wiki_dir: Path) -> list[dict]:
    """
    Wiki-link slugs that resolve to more than one file under wiki/.
    The first match wins at link time, so collisions are silent ambiguity.
    """
    collisions: list[dict] = []
    seen: set[str] = set()
    for md in md_files:
        text = md.read_text(encoding="utf-8", errors="replace")
        for m in WIKILINK_PATTERN.finditer(text):
            slug = strip_heading_anchor(m.group(1).strip())
            if slug is None or slug in seen:
                continue
            matches = list(wiki_dir.rglob(f"{slug}.md"))
            if len(matches) > 1:
                seen.add(slug)
                collisions.append({
                    "slug": slug,
                    "from": str(md),
                    "matches": [str(p) for p in matches],
                })
    return collisions


def check_broken_links(
    md_files: list[Path], wiki_dir: Path, raw_dir: Path,
) -> tuple[list[dict], list[dict], list[dict]]:
    """
    Three outputs:
      - broken: *markdown* links pointing to non-existent files inside the wiki
        (real rot — a moved or deleted page)
      - raw_missing: links to raw/ files that don't exist
      - dangling: *wiki-links* `[[slug]]` whose page doesn't exist yet. In
        Obsidian these are first-class "pages worth creating", not errors, so
        they are reported informationally, never as block-level rot.
    """
    broken: list[dict] = []
    raw_missing: list[dict] = []
    dangling: list[dict] = []
    for md in md_files:
        for url, resolved in collect_links(md, wiki_dir):
            if resolved.exists():
                continue
            entry = {"from": str(md), "url": url, "resolved": str(resolved)}
            # Categorize: raw/ miss, dangling wiki-link, or broken markdown link.
            try:
                resolved.relative_to(raw_dir)
                raw_missing.append(entry)
                continue
            except ValueError:
                pass
            if url.startswith("[["):   # Obsidian wiki-link → dangling, not rot
                dangling.append(entry)
            else:
                broken.append(entry)
    return broken, raw_missing, dangling


def check_orphans(md_files: list[Path], wiki_dir: Path, root: Path) -> list[Path]:
    """
    Pages with zero inbound links from any other page in wiki/ or from structural meta files.
    Returns list of orphan page paths.
    """
    referenced: set[Path] = set()
    content_resolved = {p.resolve() for p in md_files}

    # Scan content pages + structural meta files that contain links.
    # Try both standard (wiki/index.md) and flat-vault (root/index.md) locations.
    candidate_files = list(md_files)
    for meta in [
        wiki_dir / "index.md", root / "index.md",
        wiki_dir / "hot.md", wiki_dir / "overview.md",
    ]:
        if meta.exists() and meta.resolve() not in content_resolved:
            candidate_files.append(meta)

    for md in candidate_files:
        for _url, resolved in collect_links(md, wiki_dir):
            try:
                if resolved.exists():
                    referenced.add(resolved)
            except OSError:
                continue

    orphans = [p for p in md_files if p.resolve() not in referenced]
    return orphans


def check_index_drift(
    md_files: list[Path], wiki_dir: Path,
) -> tuple[list[Path], list[dict]]:
    """
    Returns (pages_missing_from_index, dead_index_entries).
    Checks wiki/index.md first (standard layout), then root/index.md (flat layout).
    """
    for candidate in [wiki_dir / "index.md", wiki_dir.parent / "index.md"]:
        if candidate.exists():
            index_md = candidate
            index_dir = candidate.parent
            break
    else:
        return md_files, []

    text = index_md.read_text(encoding="utf-8", errors="replace")

    # Collect targets the index points to.
    indexed_targets: set[Path] = set()
    dead: list[dict] = []
    for line in text.splitlines():
        # Standard markdown links: [Title](path.md)
        m = INDEX_LINK_PATTERN.match(line)
        if m:
            title, url = m.group(1), m.group(2)
            if is_external(url) or is_anchor_only(url):
                continue
            target = (index_dir / url.split("#", 1)[0]).resolve()
            if target.exists():
                indexed_targets.add(target)
            else:
                dead.append({"title": title, "url": url, "resolved": str(target)})
            continue

        # Obsidian wiki-links: [[slug]] or [[slug|alias]]
        wm = INDEX_WIKILINK_PATTERN.match(line)
        if wm:
            slug = wm.group(1).strip()
            # Resolve slug to a .md file anywhere under wiki_dir
            matches = list(wiki_dir.rglob(f"{slug}.md"))
            if matches:
                indexed_targets.add(matches[0].resolve())
            # Wiki-links that point to non-existent slugs are silently ignored
            # (they may be forward references or stubs)

    missing = [p for p in md_files if p.resolve() not in indexed_targets]
    return missing, dead


def check_index_duplicates(wiki_dir: Path) -> list[dict]:
    """
    Index entries pointing at the same file more than once (usually a page
    listed under several categories). One page = one index entry.
    """
    for candidate in [wiki_dir / "index.md", wiki_dir.parent / "index.md"]:
        if candidate.exists():
            index_md = candidate
            index_dir = candidate.parent
            break
    else:
        return []

    occurrences: dict[Path, list[str]] = defaultdict(list)
    category = "(no category)"
    for line in index_md.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("## "):
            category = line[3:].strip()
            continue
        m = INDEX_LINK_PATTERN.match(line)
        if m:
            url = m.group(2)
            if is_external(url) or is_anchor_only(url):
                continue
            target = (index_dir / url.split("#", 1)[0]).resolve()
            occurrences[target].append(category)
            continue
        wm = INDEX_WIKILINK_PATTERN.match(line)
        if wm:
            slug = wm.group(1).strip()
            matches = list(wiki_dir.rglob(f"{slug}.md"))
            target = matches[0].resolve() if matches else (wiki_dir / f"{slug}.md").resolve()
            occurrences[target].append(category)

    return [
        {"target": str(target), "count": len(cats), "categories": cats}
        for target, cats in occurrences.items()
        if len(cats) > 1
    ]


def check_hot_health(wiki_dir: Path, max_words: int) -> list[dict]:
    """
    hot.md is a ~500-word cache, rewritten on every ingest — not a log.
    Flags: word count over threshold, and 3+ dated `## [YYYY-MM-DD]` blocks
    (a sign the file is accumulating changelog entries that belong in log.md).
    """
    for candidate in [wiki_dir / "hot.md", wiki_dir.parent / "hot.md"]:
        if candidate.exists():
            hot_path = candidate
            break
    else:
        return []

    text = hot_path.read_text(encoding="utf-8", errors="replace")
    body = text
    if body.startswith("---\n"):
        end = body.find("\n---", 4)
        if end != -1:
            body = body[end + 4:]

    findings: list[dict] = []
    words = len(body.split())
    if words > max_words:
        findings.append({
            "path": str(hot_path),
            "issue": f"{words} words (threshold {max_words}) — rewrite, don't append",
        })
    dated_blocks = sum(
        1 for line in body.splitlines() if LOG_DATE_PATTERN.match(line)
    )
    if dated_blocks >= 3:
        findings.append({
            "path": str(hot_path),
            "issue": (
                f"{dated_blocks} dated `## [...]` blocks — hot.md is turning into "
                "a second log; move them to log.md and rewrite hot.md"
            ),
        })
    return findings


def check_tag_health(
    md_files: list[Path], max_tags: int,
) -> tuple[list[dict], list[dict], int]:
    """
    Frontmatter tag hygiene:
      - single_use: tags appearing on exactly one page (keywords, not classifiers)
      - overtagged: pages with more than max_tags tags
      - unparsed: count of files whose frontmatter could not be parsed (skipped)
    """
    tag_pages: dict[str, list[Path]] = defaultdict(list)
    overtagged: list[dict] = []
    unparsed = 0
    for md in md_files:
        text = md.read_text(encoding="utf-8", errors="replace")
        if not text.startswith("---\n"):
            continue
        fm = parse_frontmatter(text)
        if fm is None:
            unparsed += 1
            continue
        tags = fm.get("tags")
        if not isinstance(tags, list):
            continue
        for tag in tags:
            tag_pages[tag].append(md)
        if len(tags) > max_tags:
            overtagged.append({"path": str(md), "count": len(tags), "tags": tags})

    single_use = [
        {"tag": tag, "page": str(pages[0])}
        for tag, pages in sorted(tag_pages.items())
        if len(pages) == 1
    ]
    return single_use, overtagged, unparsed


def imports_agents_md(claude_md: Path) -> bool:
    """Stub test: does CLAUDE.md import AGENTS.md rather than carry the schema?"""
    text = claude_md.read_text(encoding="utf-8", errors="replace")
    return bool(re.search(r"^\s*@\.?/?AGENTS\.md\s*$", text, re.MULTILINE))


def schema_file(root: Path) -> Path:
    """
    The wiki's schema file. AGENTS.md is preferred (vendor-neutral, read by any
    coding agent); CLAUDE.md is the legacy name and still works. Returns the
    AGENTS.md path when neither exists, so callers creating one use the new name.
    """
    agents = root / "AGENTS.md"
    if agents.exists():
        return agents
    legacy = root / "CLAUDE.md"
    if legacy.exists():
        return legacy
    return agents


def check_schema_split(root: Path) -> dict | None:
    """
    Both AGENTS.md and CLAUDE.md present, and CLAUDE.md is not a stub importing
    AGENTS.md — i.e. two schema files that will drift apart. Returns a finding
    dict, else None.
    """
    agents, legacy = root / "AGENTS.md", root / "CLAUDE.md"
    if not (agents.exists() and legacy.exists()):
        return None
    if imports_agents_md(legacy):
        return None
    return {
        "hint": "CLAUDE.md should become a stub that imports AGENTS.md "
                "(a line reading `@AGENTS.md`) — see references/bootstrap-workflow.md",
    }


def check_schema_version(root: Path) -> dict | None:
    """
    Compare the wiki schema file's schema_version stamp against what this skill
    version expects. Unstamped wikis count as v1. Returns a finding dict when
    the wiki is behind, else None.
    """
    schema_md = schema_file(root)
    current = 1
    if schema_md.exists():
        fm = parse_frontmatter(schema_md.read_text(encoding="utf-8", errors="replace"))
        if fm and str(fm.get("schema_version", "")).isdigit():
            current = int(fm["schema_version"])
    if current < EXPECTED_SCHEMA_VERSION:
        return {
            "current": current,
            "expected": EXPECTED_SCHEMA_VERSION,
            "hint": "run scripts/migrate_wiki.py --path <wiki-root> (dry-run) to see the upgrade steps",
        }
    return None


def check_stub_pages(md_files: list[Path], min_words: int) -> list[dict]:
    """Pages with fewer than min_words of body text (excluding frontmatter)."""
    stubs: list[dict] = []
    for md in md_files:
        text = md.read_text(encoding="utf-8", errors="replace")
        # Strip YAML frontmatter
        if text.startswith("---\n"):
            end = text.find("\n---\n", 4)
            if end != -1:
                text = text[end + 5:]
        words = len(text.split())
        if words < min_words:
            stubs.append({"path": str(md), "words": words})
    return stubs


def check_log_gaps(wiki_dir: Path, gap_days: int) -> list[dict]:
    """Look for stretches of >gap_days between log entries in log.md."""
    for candidate in [wiki_dir / "log.md", wiki_dir.parent / "log.md"]:
        if candidate.exists():
            log_path = candidate
            break
    else:
        return []
    text = log_path.read_text(encoding="utf-8", errors="replace")
    dates: list[dt.date] = []
    for line in text.splitlines():
        m = LOG_DATE_PATTERN.match(line)
        if m:
            try:
                dates.append(dt.date.fromisoformat(m.group(1)))
            except ValueError:
                continue

    if len(dates) < 2:
        return []

    dates.sort()
    gaps: list[dict] = []
    for i in range(1, len(dates)):
        delta = (dates[i] - dates[i - 1]).days
        if delta > gap_days:
            gaps.append({
                "from": dates[i - 1].isoformat(),
                "to": dates[i].isoformat(),
                "days": delta,
            })
    return gaps


def check_slug_conventions(md_files: list[Path]) -> list[Path]:
    """Filenames that aren't lowercase-with-hyphens (excluding _-prefixed)."""
    bad: list[Path] = []
    pattern = re.compile(r"^[a-z0-9][a-z0-9\-]*\.md$")
    for md in md_files:
        name = md.name
        if name.startswith("_"):
            continue
        if not pattern.match(name):
            bad.append(md)
    return bad


def check_broken_anchors(md_files: list[Path]) -> list[dict]:
    """Same-page heading links — `[[#Section]]` — pointing at no such heading.

    An in-page anchor names no page, so the dangling-link check cannot see it:
    rename the heading and the link fails silently, looking correct in the
    source but navigating nowhere. Pages that open with a table of contents
    depend on these, so a stale one is a real defect.

    Matching follows Obsidian: the anchor is the heading's own text, compared
    case-insensitively with whitespace collapsed. Block references (`[[#^id]]`)
    are skipped — those address a block, not a heading.
    """
    out: list[dict] = []

    def norm(text: str) -> str:
        return " ".join(text.split()).casefold()

    for md in md_files:
        text = md.read_text(encoding="utf-8", errors="replace")
        headings = {norm(m.group(1)) for m in re.finditer(r"(?m)^#{1,6} +(.+?)\s*$", text)}
        if not headings:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            for m in re.finditer(r"\[\[#([^\]|\\]+)(?:\\?\|[^\]]*)?\]\]", line):
                target = m.group(1).strip()
                if target.startswith("^"):
                    continue
                if norm(target) not in headings:
                    out.append({
                        "path": str(md),
                        "line": i,
                        "anchor": m.group(0),
                    })
    return out


TABLE_DELIM_PATTERN = re.compile(r"^\s*\|?(?:\s*:?-+:?\s*\|)+\s*:?-*:?\s*\|?\s*$")
FENCE_PATTERN = re.compile(r"^\s*(?:```|~~~)")
# What may legitimately butt against a table, above and below. The two differ:
# a table cannot interrupt a *paragraph*, and a list item's text is a paragraph,
# so a bullet directly above swallows the table — while a bullet directly below
# starts a new block and ends the table cleanly.
TABLE_SAFE_ABOVE = re.compile(r"^\s*(?:#{1,6} |```|~~~|\||<)")
TABLE_SAFE_BELOW = re.compile(r"^\s*(?:#{1,6} |>|```|~~~|[-*+] |\d+[.)] |\||<)")


def check_table_rendering(md_files: list[Path]) -> list[dict]:
    """Markdown tables written in a way the renderer won't turn into a table.

    Three failures, all of which look correct in the source and show up as a
    row of literal pipes in the reader:

    * **indented** — a table at a continuation indent inside a list item, or
      behind a `>` blockquote marker. Nesting a table inside another block is
      not portable; take the table out of the nesting (promote an over-long
      bullet to its own subsection, or drop the `>` from the aside).
    * **no blank line above** — a table header row cannot interrupt a
      paragraph, so a table butted straight against the line above it is
      absorbed into that paragraph.
    * **no blank line below** — the table runs until a blank line or the start
      of another block, so a plain paragraph on the very next line is eaten as
      one more row.

    A table is located by its delimiter row (`|---|---|`), which is what makes
    a run of pipes a table in the first place; the header is the line above it.
    Fenced code blocks are skipped, so documentation *showing* table syntax is
    not flagged.

    Returns a list of {"path", "line", "reason"} dicts, one per table.
    """
    out: list[dict] = []
    bq_prefix = re.compile(r"^\s*(?:>\s?)+")

    def strip_bq(text: str) -> str:
        return bq_prefix.sub("", text)

    for md in md_files:
        lines = md.read_text(encoding="utf-8", errors="replace").splitlines()
        in_fence = False
        i = 0
        while i < len(lines):
            line = lines[i]
            if FENCE_PATTERN.match(strip_bq(line)):
                in_fence = not in_fence
                i += 1
                continue
            if in_fence or i == 0:
                i += 1
                continue
            # A delimiter row with a pipe-bearing line above it marks a table.
            body = strip_bq(line)
            if not (TABLE_DELIM_PATTERN.match(body) and "|" in body):
                i += 1
                continue
            header_idx = i - 1
            header = lines[header_idx]
            if "|" not in strip_bq(header):
                i += 1
                continue

            reasons: list[str] = []
            in_blockquote = bool(bq_prefix.match(header)) and ">" in header
            indent = len(header) - len(header.lstrip())
            if in_blockquote:
                reasons.append("inside a blockquote")
            elif indent:
                reasons.append(f"indented {indent} spaces, inside a list item")

            above = lines[header_idx - 1] if header_idx > 0 else ""
            if above.strip() and not TABLE_SAFE_ABOVE.match(strip_bq(above)):
                reasons.append("no blank line above")

            # Walk to the end of the table body.
            end = i + 1
            while end < len(lines) and lines[end].strip() and "|" in strip_bq(lines[end]):
                end += 1
            if end < len(lines):
                below = lines[end]
                if below.strip() and not TABLE_SAFE_BELOW.match(strip_bq(below)):
                    reasons.append("no blank line below")

            if reasons:
                out.append({
                    "path": str(md),
                    "line": header_idx + 1,
                    "reason": "; ".join(reasons),
                })
            i = end
    return out


CUE_TIME_PATTERN = re.compile(
    r"^\s*(\d{1,2}):(\d{2}):(\d{2})[.,](\d{1,3})\s*-->", re.MULTILINE
)
TRANSCRIPT_SUFFIXES = (".srt", ".vtt")
# A timestamp citation, anchored on the quote's closing mark so the quote's
# extent can be walked backwards from a known point. Optionally carries the
# source page ([[slug]]) and the lecture (L3) the timestamp belongs to.
CITATION_PATTERN = re.compile(
    r"(?P<close>[\"”'])"
    r"[\s.,;:]*[—–-]?\s*\(?\s*"
    r"(?:see\s*)?(?:\[\[(?P<slug>[^\]|\\#]+)(?:\\?\|[^\]]*)?\]\]\s*)?"
    r"(?:(?:L|Lecture\s*)(?P<lec>\d{1,2})\s*)?"
    r"@?\s*"
    r"(?P<ts>\d{1,2}:\d{2}(?::\d{2})?)"
    r"(?:\s*[\u2013\u2014-]\s*(?P<ts_end>\d{1,2}:\d{2}(?::\d{2})?))?"
    r"(?P<ts_more>(?:\s*,\s*\d{1,2}:\d{2}(?::\d{2})?)*)"
)
LECTURE_HEADING_PATTERN = re.compile(r"^#{2,6}\s+L(\d{1,2})\s*[·:.—–-]")
LECTURE_ENTRY_PATTERN = re.compile(r"^L(\d{1,2})\s*[·:.—–-]\s*(.+)$")


def _timestamp_seconds(ts: str) -> int:
    """`MM:SS` or `H:MM:SS` -> seconds. Cited timestamps use either form."""
    parts = [int(p) for p in ts.split(":")]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def _format_seconds(total: int) -> str:
    """Render back in the shape the vault cites: MM:SS, or H:MM:SS past an hour."""
    if total >= 3600:
        return f"{total // 3600}:{(total % 3600) // 60:02d}:{total % 60:02d}"
    return f"{total // 60:02d}:{total % 60:02d}"


def _normalize_words(text: str) -> list[str]:
    """Comparable word list: markdown, LaTeX and punctuation dropped.

    Quoted prose in the wiki carries emphasis (`**word**`), inline maths
    (`$h_0$`) and smart quotes that the transcript never has, so both sides are
    reduced to bare lowercase words before matching.
    """
    text = text.replace("’", "'").replace("‘", "'")
    text = re.sub(r"\$[^$]*\$", " ", text)          # inline LaTeX
    text = re.sub(r"<[^>]*>", " ", text)            # .vtt inline tags
    text = re.sub(r"[*_`\\\[\]]", " ", text)        # markdown emphasis, wiki-link brackets
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).split()


def parse_transcript(path: Path) -> list[tuple[int, str]]:
    """A `.srt`/`.vtt` as `(cue_start_seconds, word)` pairs, in order.

    Flattening to a word stream — rather than a list of cues — is what makes
    the check possible: a quote routinely spans a cue boundary, so the question
    "which cue does this quote start in?" is really "which cue does its first
    *word* belong to?"
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    stream: list[tuple[int, str]] = []
    matches = list(CUE_TIME_PATTERN.finditer(text))
    for i, m in enumerate(matches):
        start = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
        payload_start = text.find("\n", m.end())
        if payload_start == -1:
            continue
        payload_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        payload = text[payload_start:payload_end]
        # Drop the trailing cue number that belongs to the next .srt block.
        payload = re.sub(r"\n\s*\d+\s*$", "", payload)
        for word in _normalize_words(payload):
            stream.append((start, word))
    return stream


def resolve_raw_reference(raw_value: str, page: Path, root: Path) -> Path | None:
    """A frontmatter `raw:` value as a real path, or None.

    Re-anchors on the `raw/` segment rather than trusting the number of `../`
    steps in front of it. Those are written by hand and are commonly off by a
    level — a wrong count still reads as plausible — while `raw/` being a
    top-level directory of the wiki is an invariant. The literal relative path
    is tried first so a correctly written one keeps working, including for a
    wiki that nests `raw/` somewhere unusual.
    """
    literal = (page.parent / raw_value).resolve()
    if literal.exists():
        try:
            literal.relative_to(root.resolve())
            return literal
        except ValueError:
            return None  # outside the wiki root; not ours to check
    parts = Path(raw_value).parts
    if "raw" in parts:
        anchored = (root / Path(*parts[parts.index("raw"):])).resolve()
        if anchored.exists():
            return anchored
    return None

def check_raw_frontmatter(md_files: list[Path], root: Path) -> list[dict]:
    """A page's `raw:` frontmatter pointing at a file that isn't there.

    `raw:` is the pointer back to the source a page was made from, and it is a
    relative path — so it depends on how deep the page sits. Moving a page one
    level down (grouping a series into a subfolder) silently invalidates it
    unless the `../` count is bumped to match, and because nothing in an
    ordinary wiki *reads* `raw:`, a wrong one can sit unnoticed indefinitely.

    Reported at quality tier, with the corrected path when the target can be
    identified by re-anchoring on the `raw/` segment.
    """
    out: list[dict] = []
    for md in md_files:
        try:
            meta = parse_frontmatter(md.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if not meta:
            continue
        value = meta.get("raw")
        if not isinstance(value, str) or not value.strip():
            continue
        if (md.parent / value).resolve().exists():
            continue
        resolved = resolve_raw_reference(value, md, root)
        out.append({
            "path": str(md),
            "raw": value,
            "fix": os.path.relpath(resolved, md.parent) if resolved else None,
        })
    return out


def transcripts_for_page(page: Path, root: Path) -> dict[int | None, Path]:
    """Lecture number -> transcript file, from a page's own frontmatter.

    Two shapes are supported, both degrading to `{}` rather than guessing:

      * `raw:` naming a `.srt`/`.vtt` directly — a single-transcript page,
        filed under key `None`.
      * `raw:` plus a `lectures:` list of `L<n> · <Video title>` entries — a
        module page accumulating several videos. Each title is resolved to
        `<title>.<ext>` beside the `raw:` file, which is where the transcript
        convention puts it.
    """
    text = page.read_text(encoding="utf-8", errors="replace")
    meta = parse_frontmatter(text)
    if not meta or not meta.get("raw"):
        return {}
    raw_value = meta["raw"]
    if not isinstance(raw_value, str):
        return {}
    raw_path = resolve_raw_reference(raw_value, page, root)
    if raw_path is None:
        return {}

    lectures = meta.get("lectures")
    if isinstance(lectures, list) and lectures:
        out: dict[int | None, Path] = {}
        for entry in lectures:
            em = LECTURE_ENTRY_PATTERN.match(str(entry).strip())
            if not em:
                continue
            title = em.group(2).strip()
            for suffix in TRANSCRIPT_SUFFIXES:
                candidate = raw_path.parent / f"{title}{suffix}"
                if candidate.exists():
                    out[int(em.group(1))] = candidate
                    break
        return out

    if raw_path.suffix.lower() in TRANSCRIPT_SUFFIXES and raw_path.exists():
        return {None: raw_path}
    return {}


def _quote_before(line: str, close_index: int) -> str | None:
    """The quoted text ending at `close_index`, or None if no opener is found.

    Walking backwards from the closing mark is what makes single-quoted
    citations checkable: an apostrophe inside a quote (`Let's`) defeats any
    forward scan, but the *closing* mark is pinned by the timestamp that
    follows it.
    """
    close = line[close_index]
    openers = {'"': '"“', "”": '"“', "'": "'‘"}[close]
    i = close_index - 1
    while i >= 0:
        if line[i] in openers:
            # For a straight apostrophe, only accept it as an opener when it
            # starts a word — otherwise it is a possessive or a contraction.
            if line[i] in "'‘" and i > 0 and (line[i - 1].isalnum()):
                i -= 1
                continue
            return line[i + 1:close_index]
        i -= 1
    return None


def check_transcript_citations(md_files: list[Path], root: Path) -> list[dict]:
    """Timestamp citations pointing at the wrong cue of their transcript.

    A cue lasts about two seconds, so a quote's real start is often one or more
    cues before the memorable phrase inside it that was used to find it. Citing
    the phrase's cue instead of the quote's puts the reader seconds — sometimes
    tens of seconds — away from the words on the page, and the error is
    invisible on re-reading: every wrong timestamp is still a valid cue.

    The check flattens the transcript to a word stream tagged with cue starts,
    then asks where each quote's *first word* lives. A quote that legitimately
    begins mid-sentence cites the later cue, which is why the comparison is
    against the quote as written rather than against the surrounding sentence.

    Repeated phrasing is handled rather than reported: when a quote's opening
    words occur at several points in the transcript, any of those cues is
    accepted. Only the start is checked — an end time follows a quote that may
    contain an ellipsis, and guessing there would cost more in false positives
    than it returns.
    """
    out: list[dict] = []
    page_cache: dict[Path, dict[int | None, Path]] = {}
    stream_cache: dict[Path, list[tuple[int, str]]] = {}
    by_slug = {p.stem: p for p in md_files}

    def transcripts(page: Path) -> dict[int | None, Path]:
        if page not in page_cache:
            try:
                page_cache[page] = transcripts_for_page(page, root)
            except (OSError, ValueError):
                page_cache[page] = {}
        return page_cache[page]

    def stream(path: Path) -> list[tuple[int, str]]:
        if path not in stream_cache:
            stream_cache[path] = parse_transcript(path)
        return stream_cache[path]

    for md in md_files:
        own = transcripts(md)
        text = md.read_text(encoding="utf-8", errors="replace")
        section_lecture: int | None = None
        in_code = False
        for lineno, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("```"):
                in_code = not in_code
                continue
            if in_code:
                continue
            hm = LECTURE_HEADING_PATTERN.match(line)
            if hm:
                section_lecture = int(hm.group(1))
            for m in CITATION_PATTERN.finditer(line):
                # Which page's transcripts? An explicit [[slug]] wins; a page
                # that declares its own is the fallback.
                target = by_slug.get(m.group("slug")) if m.group("slug") else md
                if target is None:
                    continue
                table = transcripts(target)
                if not table:
                    continue
                # Which lecture? Explicit L<n>, else the enclosing `## L<n> ·`
                # section, else the page's single transcript.
                if m.group("lec"):
                    key: int | None = int(m.group("lec"))
                elif target is md and section_lecture is not None:
                    key = section_lecture
                elif len(table) == 1:
                    key = next(iter(table))
                else:
                    continue  # ambiguous on a multi-lecture page; don't guess
                path = table.get(key)
                if path is None:
                    continue
                quote = _quote_before(line, m.start("close"))
                if quote is None:
                    continue
                words = _normalize_words(quote)
                if len(words) < 3:
                    continue  # too short to locate reliably
                toks = stream(path)
                if not toks:
                    continue
                n = min(len(words), 8)
                head = words[:n]
                flat = [w for _, w in toks]
                starts = sorted({
                    toks[i][0] for i in range(len(flat) - n + 1)
                    if flat[i:i + n] == head
                })
                if not starts:
                    continue  # quote not found — a paraphrase in quote marks
                cited = _timestamp_seconds(m.group("ts"))
                if cited in starts:
                    continue
                # A range citation often covers several quotes at once — the
                # span is the claim, and only the first quote sits at its start.
                # Anything inside the span is cited correctly.
                if m.group("ts_end"):
                    end = _timestamp_seconds(m.group("ts_end"))
                    if any(cited <= s <= end for s in starts):
                        continue
                # A comma-separated list gives one timestamp per quote in a
                # sentence that quotes more than once; any of them may be this
                # quote's.
                if m.group("ts_more"):
                    extra = {
                        _timestamp_seconds(t)
                        for t in re.findall(r"\d{1,2}:\d{2}(?::\d{2})?", m.group("ts_more"))
                    }
                    if extra & set(starts):
                        continue
                out.append({
                    "path": str(md),
                    "line": lineno,
                    "cited": m.group("ts"),
                    "expected": [_format_seconds(s) for s in starts],
                    "drift": min(abs(cited - s) for s in starts),
                    "transcript": path.name,
                    "quote": " ".join(words[:8]),
                })
    # Worst first. A citation one cue early still lands the reader in the right
    # sentence; one half a minute out points at a different topic, and on a long
    # list that is the difference worth seeing first.
    out.sort(key=lambda e: -e["drift"])
    return out


def check_backticked_wikilinks(md_files: list[Path], wiki_dir: Path) -> list[dict]:
    """Wiki-links trapped inside an inline-code span — `[[slug]]` — which
    Obsidian renders as literal text instead of resolving as a link. A silent
    dead link: it looks right in the source but never navigates.

    Only a backticked wiki-link whose target **resolves to a real page** is
    reported. That separates genuine mistakes (a backticked link to a page that
    exists — you meant it to navigate) from intentional syntax examples that
    document the `[[slug]]` form with placeholders like `[[slug]]` or `[[link]]`
    (those resolve to nothing, so they are ignored — no allowlist needed).
    Limitation: a backticked link to a not-yet-created page is not flagged,
    being indistinguishable from a placeholder.

    Returns a list of {"path", "line", "snippet"} dicts, one per occurrence.
    """
    results: list[dict] = []
    code_span = re.compile(r"`([^`\n]+)`")          # text between single backticks
    for md in md_files:
        text = md.read_text(encoding="utf-8", errors="replace")
        for line_num, line in enumerate(text.splitlines(), 1):
            for m in code_span.finditer(line):       # each inline-code span
                for wl in WIKILINK_PATTERN.finditer(m.group(0)):
                    slug = wl.group(1).strip()
                    if list(wiki_dir.rglob(f"{slug}.md")):   # resolves → real bug
                        results.append({
                            "path": str(md),
                            "line": line_num,
                            "snippet": m.group(0),
                        })
                        break
    return results


def render_report(results: dict, root: Path, thresholds: dict) -> str:
    """Render the lint report as markdown."""
    today = dt.date.today().isoformat()
    lines: list[str] = []
    lines.append(f"# Lint report\n")
    lines.append(f"Wiki root: `{root}`")
    lines.append(f"Date: {today}\n")

    block_count = (
        len(results["broken_links"])
        + len(results["raw_missing"])
        + len(results["index_dead"])
    )
    quality_count = (
        len(results["orphans"])
        + len(results["index_missing"])
        + len(results["stubs"])
        + len(results["slug_mismatch"])
        + len(results["index_duplicates"])
        + len(results["hot_health"])
        + len(results["overtagged"])
        + len(results["wikilink_collisions"])
        + len(results["backticked_links"])
        + len(results["broken_anchors"])
        + len(results["table_rendering"])
        + len(results["transcript_citations"])
        + len(results["raw_frontmatter"])
        + (1 if results["schema_split"] else 0)
    )
    suggestion_count = (
        len(results["log_gaps"])
        + len(results["single_use_tags"])
        + (1 if results["schema_version"] else 0)
        + len(results["dangling_links"])
    )

    lines.append(
        f"Summary: **{block_count} block**, **{quality_count} quality**, "
        f"**{suggestion_count} suggestion**.\n"
    )

    # BLOCK
    lines.append("## Block (fix without asking)\n")
    if not block_count:
        lines.append("None. ✓\n")
    else:
        if results["broken_links"]:
            lines.append(f"### Broken links ({len(results['broken_links'])})\n")
            lines.append(
                "Markdown links pointing to files that don't exist inside the wiki "
                "(a moved or deleted page). Dangling `[[wiki-links]]` are listed "
                "separately under Suggestions.\n"
            )
            for entry in results["broken_links"]:
                lines.append(
                    f"- in `{entry['from']}`: `{entry['url']}` → `{entry['resolved']}`"
                )
            lines.append("")
        if results["raw_missing"]:
            lines.append(f"### Wiki citing missing `raw/` files ({len(results['raw_missing'])})\n")
            for entry in results["raw_missing"]:
                lines.append(
                    f"- in `{entry['from']}`: `{entry['url']}` → `{entry['resolved']}`"
                )
            lines.append("")
        if results["index_dead"]:
            lines.append(f"### Dead index entries ({len(results['index_dead'])})\n")
            lines.append("`wiki/index.md` references files that don't exist.\n")
            for entry in results["index_dead"]:
                lines.append(f"- [{entry['title']}]({entry['url']})")
            lines.append("")

    # QUALITY
    lines.append("## Quality (propose fixes, apply with approval)\n")
    if not quality_count:
        lines.append("None. ✓\n")
    else:
        if results["schema_split"]:
            lines.append("### Two schema files (AGENTS.md and CLAUDE.md)\n")
            lines.append(
                "Both exist and `CLAUDE.md` does not import `AGENTS.md`, so the wiki "
                "has two schemas that will drift apart. Keep the content in one file "
                "and make the other a pointer.\n"
            )
            lines.append(f"- {results['schema_split']['hint']}")
            lines.append("")
        if results["orphans"]:
            lines.append(f"### Orphan pages ({len(results['orphans'])})\n")
            lines.append("Pages with no inbound links from any other page or from the index.\n")
            for p in results["orphans"]:
                lines.append(f"- `{p}`")
            lines.append("")
        if results["index_missing"]:
            lines.append(f"### Pages missing from index ({len(results['index_missing'])})\n")
            for p in results["index_missing"]:
                lines.append(f"- `{p}`")
            lines.append("")
        if results["stubs"]:
            lines.append(
                f"### Stub pages — under {thresholds['stub_words']} words "
                f"({len(results['stubs'])})\n"
            )
            for s in results["stubs"]:
                lines.append(f"- `{s['path']}` ({s['words']} words)")
            lines.append("")
        if results["slug_mismatch"]:
            lines.append(f"### Slug convention mismatch ({len(results['slug_mismatch'])})\n")
            lines.append(
                "Filenames not matching `lowercase-with-hyphens.md`.\n"
            )
            for p in results["slug_mismatch"]:
                lines.append(f"- `{p}`")
            lines.append("")
        if results["index_duplicates"]:
            lines.append(
                f"### Duplicate index entries ({len(results['index_duplicates'])})\n"
            )
            lines.append(
                "The same page is listed more than once in the index. "
                "One page = one entry; pick the best category and drop the rest.\n"
            )
            for d in results["index_duplicates"]:
                cats = ", ".join(d["categories"])
                lines.append(f"- `{d['target']}` — {d['count']} entries ({cats})")
            lines.append("")
        if results["hot_health"]:
            lines.append(f"### hot.md health ({len(results['hot_health'])})\n")
            lines.append(
                "hot.md is a ~500-word cache, rewritten entirely on every ingest.\n"
            )
            for h in results["hot_health"]:
                lines.append(f"- `{h['path']}`: {h['issue']}")
            lines.append("")
        if results["overtagged"]:
            lines.append(
                f"### Pages over {thresholds['max_tags']} tags "
                f"({len(results['overtagged'])})\n"
            )
            lines.append(
                "Tags are classifiers, not keywords — trim to the canonical list "
                f"in {schema_file(root).name}.\n"
            )
            for t in results["overtagged"]:
                lines.append(f"- `{t['path']}` ({t['count']} tags: {', '.join(t['tags'])})")
            lines.append("")
        if results["wikilink_collisions"]:
            lines.append(
                f"### Ambiguous wiki-link slugs ({len(results['wikilink_collisions'])})\n"
            )
            lines.append(
                "These `[[slugs]]` match more than one file; the first match wins "
                "silently. Rename to unambiguous slugs.\n"
            )
            for c in results["wikilink_collisions"]:
                lines.append(
                    f"- `[[{c['slug']}]]` (first seen in `{c['from']}`): "
                    + ", ".join(f"`{m}`" for m in c["matches"])
                )
            lines.append("")
        if results["backticked_links"]:
            lines.append(
                f"### Backticked wiki-links — won't resolve "
                f"({len(results['backticked_links'])})\n"
            )
            lines.append(
                "A wiki-link inside an inline-code span renders as literal text, "
                "not a link — a silent dead link. Unwrap the backticks. Only links "
                "that resolve to a real page are reported, so syntax-example "
                "placeholders (`[[slug]]`, `[[link]]`) are not flagged.\n"
            )
            for entry in results["backticked_links"]:
                lines.append(
                    f"- `{entry['path']}` line {entry['line']}: {entry['snippet']}"
                )
            lines.append("")

        if results["broken_anchors"]:
            lines.append(
                f"### Broken in-page anchors — won't jump "
                f"({len(results['broken_anchors'])})\n"
            )
            lines.append(
                "A `[[#Section]]` link whose heading no longer exists on that "
                "page. It names no page, so the dangling-link check cannot see "
                "it — the link just silently goes nowhere. Fix the anchor or "
                "restore the heading.\n"
            )
            for entry in results["broken_anchors"]:
                lines.append(
                    f"- `{entry['path']}` line {entry['line']}: {entry['anchor']}"
                )
            lines.append("")

        if results["table_rendering"]:
            lines.append(
                f"### Tables that won't render "
                f"({len(results['table_rendering'])})\n"
            )
            lines.append(
                "A table the reader shows as a row of literal pipes. Nesting is "
                "the usual cause — a table at a continuation indent inside a "
                "list item, or behind a `>` blockquote marker — and the fix is "
                "to take the table out of the nesting (promote an over-long "
                "bullet to its own subsection; drop the `>` and let a leading "
                "emoji carry the aside). The other cause is a missing blank "
                "line: a table cannot interrupt a paragraph above it, and it "
                "swallows a paragraph butted against it below.\n"
            )
            for entry in results["table_rendering"]:
                lines.append(
                    f"- `{entry['path']}` line {entry['line']}: {entry['reason']}"
                )
            lines.append("")

        if results["raw_frontmatter"]:
            lines.append(
                f"### `raw:` frontmatter pointing at nothing "
                f"({len(results['raw_frontmatter'])})\n"
            )
            lines.append(
                "`raw:` is a relative path, so it depends on how deep its page "
                "sits: move a page one level down — grouping a series into a "
                "subfolder — and it needs one more `../`. Nothing in the wiki "
                "reads `raw:`, so a stale one goes unnoticed until something "
                "tries to follow it.\n"
            )
            for entry in results["raw_frontmatter"]:
                fix = f" -> `{entry['fix']}`" if entry["fix"] else " (target not found)"
                lines.append(f"- `{entry['path']}`: `{entry['raw']}`{fix}")
            lines.append("")

        if results["transcript_citations"]:
            lines.append(
                f"### Timestamp citations pointing at the wrong cue "
                f"({len(results['transcript_citations'])})\n"
            )
            lines.append(
                "The quoted words start in a different cue than the timestamp "
                "names. This happens when a memorable phrase from the middle of "
                "a quote is used to find the cue, and the quote is then written "
                "out from an earlier sentence — the citation stays a valid "
                "timestamp, so re-reading never catches it.\n"
            )
            lines.append(
                "Cite the cue holding the quote's **first word**. A quote that "
                "deliberately begins mid-sentence is right to cite the later "
                "cue, so check the quote as written, not the sentence around "
                "it. Where several cues are listed, the phrase recurs in the "
                "transcript and any of them may be the one meant.\n"
            )
            lines.append(
                "Listed worst first. One cue early still lands the reader in "
                "the right sentence; a large drift points at a different topic "
                "entirely, so work down from the top.\n"
            )
            for entry in results["transcript_citations"]:
                expected = " or ".join(entry["expected"])
                lines.append(
                    f"- `{entry['path']}` line {entry['line']}: cited "
                    f"`{entry['cited']}`, quote starts at `{expected}` "
                    f"(**{entry['drift']}s** out) in {entry['transcript']} "
                    f"— \"{entry['quote']}…\""
                )
            lines.append("")

    # SUGGESTIONS
    lines.append("## Suggestions (informational)\n")
    if not suggestion_count:
        lines.append("None. ✓\n")
    else:
        if results["dangling_links"]:
            lines.append(
                f"### Dangling wiki-links — pages worth creating "
                f"({len(results['dangling_links'])})\n"
            )
            lines.append(
                "`[[slug]]` links whose page doesn't exist yet. In Obsidian these "
                "are first-class staging markers, not errors — left informational. "
                "Scan for unintended ones (a typo, a stray `\\`, or an image embed "
                "that should be `![[...]]`).\n"
            )
            for d in results["dangling_links"]:
                lines.append(f"- `{d['from']}`: {d['url']}")
            lines.append("")
        if results["log_gaps"]:
            lines.append(
                f"### Log gaps over {thresholds['log_gap_days']} days "
                f"({len(results['log_gaps'])})\n"
            )
            for g in results["log_gaps"]:
                lines.append(f"- {g['from']} → {g['to']} ({g['days']} days)")
            lines.append("")
        if results["single_use_tags"]:
            lines.append(
                f"### Single-use tags ({len(results['single_use_tags'])})\n"
            )
            lines.append(
                "A tag on exactly one page is a keyword, not a classifier — "
                "move it to the page body or merge it into a canonical tag.\n"
            )
            for s in results["single_use_tags"]:
                lines.append(f"- `{s['tag']}` (only on `{s['page']}`)")
            lines.append("")
        if results["schema_version"]:
            sv = results["schema_version"]
            lines.append("### Wiki schema behind skill version\n")
            lines.append(
                f"- Wiki is at schema v{sv['current']}, skill expects "
                f"v{sv['expected']} — {sv['hint']}"
            )
            lines.append("")
    if results.get("tag_unparsed"):
        lines.append(
            f"*Note: frontmatter could not be parsed in "
            f"{results['tag_unparsed']} file(s); tag checks skipped them.*\n"
        )

    # Reminder of out-of-scope checks
    lines.append("---\n")
    lines.append("**Not checked here (LLM responsibility):** stale claims, ")
    lines.append("unflagged contradictions, missing pages on cross-cutting entities, ")
    lines.append(f"schema drift between `{schema_file(root).name}` and actual practice.\n")
    return "\n".join(lines)


def auto_track(
    report_path: Path,
    root: Path,
    block_total: int,
    quality_count: int,
    suggestion_count: int,
    today: str,
) -> None:
    """Update index.md and log.md to record this report.

    Best-effort: failures are reported but don't abort lint.
    Only called when report is being written inside wiki/reports/.
    """
    scripts_dir = Path(__file__).resolve().parent
    summary = (
        f"{block_total} block, {quality_count} quality, {suggestion_count} suggestion"
    )
    index_title = f"Lint {today}"
    log_title = "Health check"

    # Path relative to wiki/ for the index entry
    try:
        rel = report_path.resolve().relative_to((root / "wiki").resolve())
    except ValueError:
        return

    # update_index.py
    res = subprocess.run(
        [
            sys.executable, str(scripts_dir / "update_index.py"),
            "--path", str(root),
            "--category", "Reports",
            "--title", index_title,
            "--page-path", str(report_path),
            "--summary", summary,
        ],
        capture_output=True, text=True, check=False,
    )
    if res.returncode != 0:
        print(f"warning: update_index failed: {res.stderr.strip()}", file=sys.stderr)

    # append_log.py
    details = f"Wrote wiki/{rel.as_posix()}. {summary}."
    res = subprocess.run(
        [
            sys.executable, str(scripts_dir / "append_log.py"),
            "--path", str(root),
            "--action", "lint",
            "--title", log_title,
            "--details", details,
            "--date", today,
        ],
        capture_output=True, text=True, check=False,
    )
    if res.returncode != 0:
        print(f"warning: append_log failed: {res.stderr.strip()}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Health check an LLM-managed wiki.",
    )
    parser.add_argument(
        "--path", default=".",
        help="Wiki root directory (default: current directory).",
    )
    parser.add_argument(
        "--report", default=None,
        help="Custom report path. Default: wiki/reports/lint-<today>.md.",
    )
    parser.add_argument(
        "--stdout", action="store_true",
        help="Print report to stdout, do not write a file (no auto-track).",
    )
    parser.add_argument(
        "--no-track", action="store_true",
        help="Write the report file but skip the auto index/log updates.",
    )
    parser.add_argument(
        "--stub-words", type=int, default=50,
        help="Pages under this many body words are flagged as stubs (default: 50).",
    )
    parser.add_argument(
        "--log-gap-days", type=int, default=30,
        help="Log gaps longer than this many days are flagged (default: 30).",
    )
    parser.add_argument(
        "--hot-max-words", type=int, default=700,
        help="hot.md over this many body words is flagged (default: 700).",
    )
    parser.add_argument(
        "--max-tags", type=int, default=4,
        help="Pages with more frontmatter tags than this are flagged (default: 4).",
    )
    args = parser.parse_args()

    root = Path(args.path).expanduser().resolve()
    wiki_dir = root / "wiki"
    raw_dir = root / "raw"

    if not wiki_dir.exists():
        print(f"error: no wiki/ directory at {root}", file=sys.stderr)
        return 1

    today = dt.date.today().isoformat()

    md_files = find_md_files(wiki_dir)

    broken, raw_missing, dangling = check_broken_links(md_files, wiki_dir, raw_dir)
    orphans = check_orphans(md_files, wiki_dir, root)
    index_missing, index_dead = check_index_drift(md_files, wiki_dir)
    stubs = check_stub_pages(md_files, args.stub_words)
    log_gaps = check_log_gaps(wiki_dir, args.log_gap_days)
    slug_mismatch = check_slug_conventions(md_files)
    index_duplicates = check_index_duplicates(wiki_dir)
    hot_health = check_hot_health(wiki_dir, args.hot_max_words)
    single_use_tags, overtagged, tag_unparsed = check_tag_health(md_files, args.max_tags)
    wikilink_collisions = check_wikilink_collisions(md_files, wiki_dir)
    schema_version = check_schema_version(root)
    schema_split = check_schema_split(root)
    # hot.md is excluded from the structural checks (meta file), but it's
    # hand-written narrative prose where backticked real links matter too.
    backticked_files = [p for p in (md_files + [wiki_dir / "hot.md"]) if p.exists()]
    backticked = check_backticked_wikilinks(backticked_files, wiki_dir)
    broken_anchors = check_broken_anchors(backticked_files)
    table_rendering = check_table_rendering(backticked_files)
    # index.md carries long-lived copies of source-page summaries, citations
    # included, so it is checked alongside the pages themselves.
    citation_files = [p for p in (backticked_files + [wiki_dir / "index.md"]) if p.exists()]
    transcript_citations = check_transcript_citations(citation_files, root)
    raw_frontmatter = check_raw_frontmatter(md_files, root)

    results = {
        "broken_links": broken,
        "raw_missing": raw_missing,
        "dangling_links": dangling,
        "orphans": orphans,
        "index_missing": index_missing,
        "index_dead": index_dead,
        "stubs": stubs,
        "log_gaps": log_gaps,
        "slug_mismatch": slug_mismatch,
        "index_duplicates": index_duplicates,
        "hot_health": hot_health,
        "single_use_tags": single_use_tags,
        "overtagged": overtagged,
        "tag_unparsed": tag_unparsed,
        "wikilink_collisions": wikilink_collisions,
        "schema_version": schema_version,
        "schema_split": schema_split,
        "backticked_links": backticked,
        "broken_anchors": broken_anchors,
        "table_rendering": table_rendering,
        "transcript_citations": transcript_citations,
        "raw_frontmatter": raw_frontmatter,
    }

    report = render_report(
        results, root,
        thresholds={
            "stub_words": args.stub_words,
            "log_gap_days": args.log_gap_days,
            "max_tags": args.max_tags,
        },
    )

    block_total = len(broken) + len(raw_missing) + len(index_dead)
    quality_count = (
        (1 if schema_split else 0)
        + len(orphans) + len(index_missing) + len(stubs) + len(slug_mismatch)
        + len(index_duplicates) + len(hot_health) + len(overtagged)
        + len(wikilink_collisions) + len(backticked)
        + len(broken_anchors) + len(table_rendering)
        + len(transcript_citations) + len(raw_frontmatter)
    )
    suggestion_count = (
        len(log_gaps) + len(single_use_tags) + (1 if schema_version else 0)
        + len(dangling)
    )

    # Decide where the report goes.
    if args.stdout:
        print(report)
    else:
        if args.report:
            report_path = Path(args.report).expanduser().resolve()
        else:
            report_path = (wiki_dir / "reports" / f"lint-{today}.md").resolve()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report, encoding="utf-8")
        print(f"Report written to {report_path}")

        # Auto-track only when the report lives inside wiki/ — custom paths
        # outside the wiki are treated as one-off and not tracked.
        if not args.no_track:
            try:
                report_path.relative_to(wiki_dir.resolve())
            except ValueError:
                pass  # outside wiki/, skip tracking
            else:
                auto_track(
                    report_path, root,
                    block_total, quality_count, suggestion_count, today,
                )

    return 1 if block_total > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
