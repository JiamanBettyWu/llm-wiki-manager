# Audit Workflow — from critical edition to map

Use this when a source page has grown longer than the source it summarizes, or on a periodic pass. Triggers: "this page is too long", "unreadable", "easier to rewatch than to read", "trim", "convert to a map", or a non-empty *Pages over their length budget* section in the lint report.

A source page for a lecture series tends to grow into a **critical edition**: every instructor slip, every vault prediction and its score, every running tally, interleaved with the teaching. The teaching is still in there, but the page is now longer than the lectures and the reader rewatches instead. This skill turns such a page back into a **map** — short, scannable, pointing into the concept pages the reader actually reviews from — without losing the record.

The wiki's `AGENTS.md` owns the *budgets and shape* (this skill reads them there; it does not define them). What this skill owns is the **process**, which was the same on the three conversions it was distilled from.

## When it runs

- **On request** — "this page is too long", "convert X to a map", "trim Y".
- **Periodically** — after a course module closes, or when `lint_wiki.py`'s *Pages over their length budget* section is non-empty.
- **Never mid-series without asking.** A page still accumulating lectures carries provisional "not yet" entries that a later lecture may pay off; converting it deletes the deferral. Ask, and if converting, keep the Open questions verbatim in the threads note.

## The process

### 1 · Measure, against healthy siblings

```bash
python3 scripts/audit_measure.py --path <wiki-root> [glob]
```

Reports per page: words, **words per lecture** (from `lectures:` frontmatter), share of `##` headings that are meta rather than topic, marker density, header-block size, printed tallies. The point is the **comparison**: "10,878 words" means little; "3,626 words/lecture where the healthy sibling has 1,006" is a finding. Run the wiki's lint too — its *length budget* section is the same measurement with the wiki's thresholds.

### 2 · Read the whole page, and the concept pages it should point at

Before cutting, know two things:

- **Where is the teaching?** For each lecture section, name the concept page (and heading) that carries the explanation. If none exists, that is a finding — note it and point at the nearest neighbour, saying so inline: `*(no `pytorch` page yet)*`. Do not create concept pages mid-conversion unless asked.
- **What is on the page that is not teaching?** Errata, predictions and scores, notation tables, tallies, freshness markers, capture notes, transcript-artifact lists, `What L<n> leaves open` sections, struck-through open questions. All of it goes to the threads note.

### 3 · Triage the errata

For every correction on the page ask: **would a reader who absorbed the error get the material wrong?** Build a table with that as a column (*Yes / Mildly / No*). Only the *Yes* rows go on the map, one line each, in the section's single ⚠ block. Every row goes in the threads note. On three conversions the ratio was roughly one *Yes* in three.

A **vault** error (the page promoted an absence to a finding and a later lecture reversed it) is an erratum too — record it, with what it cost.

### 4 · Write the threads note (or extend the module's existing one)

`wiki/notes/<course>-moduleN-threads.md`. Sections that have worked: **Errata** (the triage table), **Notation** (symbol clashes across sources), **Vault predictions and readings · scored**, **Threads across the module** (one row per cross-lecture thread: thread | state | trail — *rewrite the state cell, never append to it*), **Open questions**, **Vault links the course does not draw**, **Capture notes**. Name the pre-conversion git commit at the top so no one restores it.

If the page carried a module-wide Threads table, it moves here whole and gets compressed: on one page a 20-row table went from ~6,000 words to ~1,200 with every timestamp intact, by cutting the narrated history in each cell to a state and a trail.

### 5 · Grep inbound anchors before renaming a heading

```bash
grep -rn '<slug>#' wiki/
```

Every `[[slug#Heading]]` into the page breaks silently when the heading changes. Module overviews and sibling pages are the usual culprits; three conversions had 5, 6 and 7 inbound anchors. Repoint them in the same commit. Lint's *Broken heading anchors* check confirms.

### 6 · Write the map

Per lecture, in this order: `## L<n> · <topic>` (topic only) → `**Concept page → [[slug#Heading]]**` → `**The idea:** one sentence` → bullets with bold lead-ins and the two or three timestamps that matter → the load-bearing slide → one ⚠ block. Page-level: a one-paragraph opener, a `← · →` sibling line, `## Review questions` whose answers are concept headings, `## Record` (threads note, the reader's own Q&A notes, outside sources). Drop the lecture catalog, `Where this sits`, per-lecture `leaves open`, `Related concepts`.

Keep frontmatter and `raw:` unchanged. Keep code only where the code *is* the lecture's content (a corrected training loop earned its place; a notation table did not).

### 7 · Trim the concept page if it is over budget

It is the page the reader reviews from. The usual cuts: the long notation paragraph (→ threads note), printed tallies, a `## Sources` bullet that re-narrates the source, slide embeds duplicated from the map. Reordering *is* allowed on a concept page (it is forbidden on a source page, where errata are the record).

### 8 · Bookkeep, lint, commit

Index (pass the **existing** title verbatim — an "added" line means a duplicate), log, hot.md rewrite, lint (anchors, tables, budgets), commit with the before/after word counts, push.

## What to report to the user

Before/after word counts per page; which errata were *Yes* and why; which concept pointers had no target; anything you chose not to cut and why. Ask them to read one converted page against the lectures before converting the next batch — the calibration (bullets vs prose, words per lecture) came from that read, not from the numbers.
