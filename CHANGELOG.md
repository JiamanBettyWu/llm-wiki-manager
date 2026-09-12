# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- **`AGENTS.md` is now the schema filename; `CLAUDE.md` becomes a stub that imports it.** `AGENTS.md` is the vendor-neutral name coding agents converge on, so Claude Code, Codex and others read one file instead of each re-rendering the schema under its own name. `init_wiki.py` writes `AGENTS.md` plus a `CLAUDE.md` containing an `@AGENTS.md` import line.
- `assets/templates/wiki-CLAUDE.md.tmpl` renamed to `wiki-AGENTS.md.tmpl` (`init_wiki.py` falls back to the old name if only that is present).

### Added
- Lint check **"Two schema files"** (quality tier): fires when `AGENTS.md` and `CLAUDE.md` both exist and `CLAUDE.md` does not import `AGENTS.md` — i.e. two schemas that will drift apart.
- Lint check **"Timestamp citations pointing at the wrong cue"** (quality tier): for pages citing a `.srt`/`.vtt` transcript by timestamp, verifies that each quote's **first word** falls in the cue the citation names. Catches the failure where a memorable phrase from the middle of a quote is used to locate the cue and the quote is then written out from an earlier sentence — the citation stays a syntactically valid timestamp, so proofreading never catches it. Transcripts are resolved from a page's `raw:` frontmatter, and from a `lectures:` list of `L<n> · <Video title>` entries for a page accumulating several videos; a citation names its lecture explicitly (`L3 @ 04:12`), by its enclosing `## L3 · …` section, or implicitly on a single-transcript page. Findings are sorted worst-first by drift. Two multi-quote citation shapes are accepted rather than reported: a **range** (`@ 08:28–09:15`) covering several quotes, where any quote starting inside the span is correct; and a **comma-separated list** (`@ 07:38, 08:56`) giving one timestamp per quote, where any of them may be this quote's. Repeated phrasing is accepted rather than reported: if a quote's opening words occur at several points, any of those cues passes. Pages with no resolvable transcript are skipped.
- Lint check **"`raw:` frontmatter pointing at nothing"** (quality tier): a page's `raw:` pointer that resolves to no file. `raw:` is a relative path, so moving a page one level deeper — grouping a series into a subfolder — invalidates it unless the `../` count is bumped; nothing normally reads `raw:`, so a stale one goes unnoticed. Reports the corrected path where the target can be identified by re-anchoring on the `raw/` segment.
- Lint check **"Paths in the schema file that have moved"** (quality tier): every backticked absolute path in the schema file (`AGENTS.md`, or `CLAUDE.md` on a legacy wiki) must exist. The schema names script and tool locations in prose and nothing validates them, so when one moves the file goes on asserting the old location — quietly, because a move usually leaves a symlink behind and every documented command keeps working while the documented path is wrong. A path that resolves *only* by traversing a symlink is reported too, but only when the real location is named nowhere in the schema file: naming the real target beside the convenience path clears it, while naming merely an ancestor does not, since an ancestor doesn't say where the thing is. Paths inside fenced code blocks are exempt (command examples, templates), as are spans containing `<`, `>`, `*` or `…`, which are treated as placeholders. Note that the symlink case needs `realpath` rather than an `is_symlink` test, since the link is often an interior component of the path (`.../linked-dir/scripts/`). Relative markdown links in the schema file are checked too, resolved against its own directory: a schema split across several files (`AGENTS.md` linking `docs/*.md`) hangs together on those links, nothing else in the wiki reads them, and renaming a linked file leaves a link that still looks right. External URLs, in-page anchors and absolute paths are skipped.

### Backward compatibility
- **Existing `CLAUDE.md`-only wikis are unaffected.** `lint_wiki.py` and `migrate_wiki.py` prefer `AGENTS.md` and fall back to `CLAUDE.md` for reading and for the `schema_version` stamp; `init_wiki.py` will not drop an `AGENTS.md` beside a real `CLAUDE.md` schema.

## [1.4.0] - 2026-06-10

### Added
- `scripts/migrate_wiki.py` — schema upgrade tool (v1 → v2): deduplicates `index.md`, moves dated changelog blocks from `hot.md` into `log.md`, stamps `schema_version` in the wiki's `CLAUDE.md`. Dry-run by default, applies with `--apply`. Idempotent.
- `references/migrate-workflow.md` — full migrate workflow documentation.
- Lint expansion: broken-link detection for markdown and `[[wiki-link]]` formats, duplicate index entries, hot.md bloat, tag hygiene (single-use tags, over-tagged pages), ambiguous wiki-link slugs, log gaps, and schema-version drift.
- Schema version check (Step 0) in every operating mode — offers migration when the wiki is behind the expected schema.

### Changed
- Template revisions for clearer `hot.md` structure (changelog history now lives in `log.md`).
- Lint thresholds configurable via `--stub-words`, `--log-gap-days`, `--hot-max-words`, `--max-tags`.

### Fixed
- README counts corrected (5 scripts, 11 reference docs); `migrate_wiki.py` added to the script table in both READMEs.
- `lint_wiki.py`: script directory is put on `sys.path` so the `migrate_wiki` import works when invoked from any cwd; frontmatter parser exception handling narrowed.
- `migrate_wiki.py`: the `hot.md` → `log.md` move now verifies the full block content on disk before deleting from `hot.md`.

## [1.3.0] - 2026-05-31

### Added
- Turkish README (`README.tr.md`).

### Changed
- Restructured main `README.md`.
- Removed outdated related-projects section from README.

## [1.2.1] - 2026-05-21

### Added
- CI: auto-release workflow on SKILL.md version bump (`.github/workflows/release.yml`).

## [1.2.0] - 2026-05-21

### Added
- `hot.md` hot cache — most recently ingested sources and active references.
- Obsidian wiki-link (`[[...]]`) support in lint and index handling.

### Changed
- Script improvements: flexible log/index path detection in both `wiki/` and root layouts.

## [1.1.0] - 2026-05-07

### Added
- Multi-wiki routing with safeguards — routes writes between a project wiki and a global wiki via the `External Wiki:` declaration in the project `CLAUDE.md`.
- `references/multi-wiki-routing.md` with four canonical scenarios.
- Discoverability improvements (release badge, installation instructions).

## [1.0.0] - 2026-05-07

### Added
- Initial release: Bootstrap, Ingest, Query, Update, Lint, Schema-evolve, and Teach modes.
- Scripts: `init_wiki.py`, `append_log.py`, `update_index.py`, `lint_wiki.py` (Python stdlib only, idempotent).
- 8 page templates and core reference documentation.

[1.4.0]: https://github.com/sametbrr/llm-wiki-manager/compare/v1.3.0...v1.4.0
[1.3.0]: https://github.com/sametbrr/llm-wiki-manager/compare/v1.2.1...v1.3.0
[1.2.1]: https://github.com/sametbrr/llm-wiki-manager/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/sametbrr/llm-wiki-manager/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/sametbrr/llm-wiki-manager/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/sametbrr/llm-wiki-manager/releases/tag/v1.0.0
