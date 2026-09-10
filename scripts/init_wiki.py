#!/usr/bin/env python3
"""
init_wiki.py — Scaffold a new LLM-managed wiki at the given path.

Idempotent: if the wiki already exists, missing pieces are added without
overwriting existing files. Safe to re-run.

Usage:
    python init_wiki.py --path /path/to/wiki-root --name "My Wiki" --topic "Brief topic"
    python init_wiki.py                          # uses cwd, prompts for name/topic if missing
    python init_wiki.py --path . --name "Foo"    # minimal flags

Layout produced:
    <root>/
    ├── AGENTS.md          # schema, written from template (only if missing)
    ├── CLAUDE.md          # stub importing AGENTS.md (only if missing)
    ├── README.md          # human-facing overview (only if missing)
    ├── raw/.gitkeep
    └── wiki/
        ├── index.md       # only if missing
        ├── log.md         # only if missing, with one bootstrap entry
        ├── sources/.gitkeep
        ├── entities/.gitkeep
        ├── concepts/.gitkeep
        └── notes/.gitkeep
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from pathlib import Path


# Path to the skill's templates directory, resolved relative to this script.
SCRIPT_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = SCRIPT_DIR.parent / "assets" / "templates"

# CLAUDE.md is a pointer, not a second schema: Claude Code follows the @-import
# so the AGENTS.md content loads automatically, and there is only one file to edit.
CLAUDE_STUB = """# CLAUDE.md

Project guidance lives in [AGENTS.md](AGENTS.md) — the vendor-neutral source of
truth read by all coding agents. The line below imports it so Claude Code loads
the full content automatically; edit AGENTS.md, not this file.

@AGENTS.md
"""


def imports_agents_md(claude_md: Path) -> bool:
    """Stub test: does CLAUDE.md import AGENTS.md rather than carry the schema?"""
    text = claude_md.read_text(encoding="utf-8", errors="replace")
    return bool(re.search(r"^\s*@\.?/?AGENTS\.md\s*$", text, re.MULTILINE))


def render_template(template_path: Path, replacements: dict[str, str]) -> str:
    """Read a template and substitute {{KEY}} placeholders."""
    text = template_path.read_text(encoding="utf-8")
    for key, value in replacements.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def write_if_missing(path: Path, content: str, *, created: list[str], skipped: list[str]) -> None:
    """Write content to path only if path doesn't exist. Track action."""
    if path.exists():
        skipped.append(str(path))
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    created.append(str(path))


def ensure_dir_with_gitkeep(path: Path, *, created: list[str], skipped: list[str]) -> None:
    """Create a directory and a .gitkeep inside if not present."""
    path.mkdir(parents=True, exist_ok=True)
    keepfile = path / ".gitkeep"
    if not keepfile.exists():
        keepfile.write_text("", encoding="utf-8")
        created.append(str(keepfile))
    else:
        skipped.append(str(keepfile))


def init_wiki(root: Path, name: str, topic: str) -> tuple[list[str], list[str]]:
    """Scaffold the wiki at root. Returns (created, skipped) path lists."""
    created: list[str] = []
    skipped: list[str] = []

    today = dt.date.today().isoformat()

    # Top-level dirs
    root.mkdir(parents=True, exist_ok=True)
    ensure_dir_with_gitkeep(root / "raw", created=created, skipped=skipped)

    wiki_dir = root / "wiki"
    wiki_dir.mkdir(parents=True, exist_ok=True)

    # Only create categorical subdirs for fresh wikis.
    # If wiki/ already has .md content, the user has their own structure — don't impose ours.
    # Always create reports/ so lint_wiki.py has a place to write dated reports.
    wiki_has_content = any(wiki_dir.rglob("*.md"))
    if wiki_has_content:
        ensure_dir_with_gitkeep(wiki_dir / "reports", created=created, skipped=skipped)
    else:
        for sub in ("sources", "entities", "concepts", "notes", "reports"):
            ensure_dir_with_gitkeep(wiki_dir / sub, created=created, skipped=skipped)

    # AGENTS.md (schema) + CLAUDE.md (stub that imports it).
    # An existing wiki may carry its schema in CLAUDE.md under the old layout.
    # Leave it alone: writing AGENTS.md from the template alongside a real
    # CLAUDE.md schema would create the two-schema drift lint now flags.
    legacy_schema = root / "CLAUDE.md"
    legacy_is_schema = legacy_schema.exists() and not imports_agents_md(legacy_schema)

    if not legacy_is_schema:
        schema_template = None
        for candidate in ("wiki-AGENTS.md.tmpl", "wiki-CLAUDE.md.tmpl"):
            if (TEMPLATES_DIR / candidate).exists():
                schema_template = TEMPLATES_DIR / candidate
                break
        if schema_template is not None:
            content = render_template(
                schema_template,
                {"WIKI_NAME": name, "TOPIC": topic},
            )
        else:
            # Fallback minimal schema if template is missing.
            content = f"# {name}\n\n## Purpose\n\n{topic}\n"
        write_if_missing(root / "AGENTS.md", content, created=created, skipped=skipped)
        write_if_missing(root / "CLAUDE.md", CLAUDE_STUB, created=created, skipped=skipped)

    # README.md (human-facing)
    readme = (
        f"# {name}\n\n"
        f"{topic}\n\n"
        "This is an LLM-managed wiki. The agent owns `wiki/`. "
        "I curate sources in `raw/`. The schema is in `AGENTS.md`.\n\n"
        "## Layout\n\n"
        "- `raw/` — source documents I've collected\n"
        "- `wiki/` — agent-generated pages\n"
        "- `AGENTS.md` — schema for the agent (`CLAUDE.md` imports it)\n"
    )
    write_if_missing(root / "README.md", readme, created=created, skipped=skipped)

    # index.md
    index_template = TEMPLATES_DIR / "index.md.tmpl"
    if index_template.exists():
        content = index_template.read_text(encoding="utf-8")
    else:
        content = "# Index\n\n## Sources\n\n## Entities\n\n## Concepts\n\n## Notes\n"
    write_if_missing(wiki_dir / "index.md", content, created=created, skipped=skipped)

    # log.md
    log_template = TEMPLATES_DIR / "log.md.tmpl"
    if log_template.exists():
        bootstrap_details = f"Topic: {topic}" if topic else ""
        content = render_template(
            log_template,
            {"TODAY": today, "BOOTSTRAP_DETAILS": bootstrap_details},
        )
    else:
        content = f"# Log\n\n## [{today}] bootstrap | Wiki initialized\nTopic: {topic}\n"
    write_if_missing(wiki_dir / "log.md", content, created=created, skipped=skipped)

    # hot.md (Karpathy's recommended hot cache file)
    hot_template = TEMPLATES_DIR / "hot.md.tmpl"
    if hot_template.exists():
        content = render_template(hot_template, {"TODAY": today})
    else:
        content = (
            f"---\ntype: hot\ncreated: {today}\nupdated: {today}\n---\n\n"
            "# Hot\n\nHot cache — rewrite after every ingest.\n\n"
            "## Vault state\n\n*No ingests yet.*\n\n"
            "## Active knowledge\n\n*No ingests yet.*\n\n"
            "## Open work items\n\n- Drop the first source and ingest it.\n"
        )
    write_if_missing(wiki_dir / "hot.md", content, created=created, skipped=skipped)

    return created, skipped


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Scaffold a new LLM-managed wiki (idempotent).",
    )
    parser.add_argument(
        "--path", default=".",
        help="Wiki root directory (default: current directory).",
    )
    parser.add_argument(
        "--name", default=None,
        help="Wiki name. Used in AGENTS.md and README.md.",
    )
    parser.add_argument(
        "--topic", default=None,
        help="One-sentence description of the wiki's purpose.",
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="Suppress per-file output, just print summary.",
    )
    args = parser.parse_args()

    root = Path(args.path).expanduser().resolve()

    name = args.name or root.name
    topic = args.topic or "<edit AGENTS.md to describe this wiki>"

    created, skipped = init_wiki(root, name, topic)

    if not args.quiet:
        if created:
            print(f"Created {len(created)} files/directories:")
            for p in created:
                print(f"  + {p}")
        if skipped:
            print(f"Skipped {len(skipped)} (already exist):")
            for p in skipped:
                print(f"  · {p}")

    print(f"\nWiki ready at: {root}")
    if created:
        print("Next steps:")
        print("  1. Edit AGENTS.md to refine the schema for your topic.")
        print("  2. Drop your first source into raw/.")
        print("  3. Ask your coding agent to ingest it.")
    else:
        print("Wiki was already fully scaffolded — no changes made.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
