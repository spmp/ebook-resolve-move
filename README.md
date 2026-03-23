# ebook-resolve-move

A safety-first ebook ingestion script for:

- metadata lookup using `hardcover.bookinfo.pro`
- non-destructive metadata enrichment (add missing fields, never overwrite existing fields)
- move files into an `Author/Title/Title - Author.ext` structure
- optional post-move sync triggers for Kavita and Readarr

This project is intentionally conservative where source metadata is weak, and intentionally practical where source metadata is already strong enough to move safely.

If you hit an edge case, please open an issue or PR with a failing test fixture. That is the fastest way to improve behavior without overfitting.

For contribution workflow details, see `CONTRIBUTING.md`.

For agent-focused architecture and coding standards, see `AGENTS.md`.

## License

GPLv3 (`LICENSE`).

## Requirements

- Python 3.10+
- Dependencies:
  - `httpx`
  - `pypdf`
  - `rapidfuzz`
  - `watchdog`

## Quick Start

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

One-shot minimal run:

```bash
EBOOK_LIBRARY_ROOT=/path/to/library \
python3 ebook_resolve_move.py /path/to/incoming/book.epub
```

Watch mode:

```bash
python3 ebook_resolve_move.py \
  --watch-directory /path/to/incoming \
  --library-root /path/to/library \
  --recursive
```

## Virtualenv Portability

The script keeps a portable shebang (`#!/usr/bin/env python3`) and then tries to re-exec into local venv Python before importing dependencies.

Lookup order for interpreter re-exec:

1. `EBOOK_PYTHON`
2. `EBOOK_VENV/bin/python`
3. `<script_dir>/.venv/bin/python`
4. `<cwd>/.venv/bin/python`
5. parent directories' `.venv/bin/python`

Overrides:

- `EBOOK_PYTHON`: explicit Python path
- `EBOOK_VENV`: explicit venv path
- `EBOOK_SKIP_VENV_REEXEC=1`: disable re-exec

## CLI Options (Complete)

Positional:

- `file` (required unless `--watch-directory`)

Options:

- `-h`, `--help`
- `--library-root LIBRARY_ROOT`
- `--watch-directory WATCH_DIRECTORY`
- `--recursive`
- `--initial-scan`
- `--settle-seconds SETTLE_SECONDS`
- `--api-base API_BASE`
- `--dry-run`, `--no-dry-run`
- `--min-score MIN_SCORE`
- `--min-margin MIN_MARGIN`
- `--overwrite-existing`, `--no-overwrite-existing`
- `--kavita-scan`, `--no-kavita-scan`
- `--kavita-url KAVITA_URL`
- `--kavita-api-key KAVITA_API_KEY`
- `--kavita-library-id KAVITA_LIBRARY_ID`
- `--readarr-scan`, `--no-readarr-scan`
- `--readarr-url READARR_URL`
- `--readarr-api-key READARR_API_KEY`
- `--readarr-command-json READARR_COMMAND_JSON`

Configuration precedence is always:

1. CLI switch
2. Environment variable
3. built-in default

## Environment Variables

- `EBOOK_LIBRARY_ROOT` (required unless `--library-root` is set)
- `EBOOK_API_BASE`
- `EBOOK_DRY_RUN`
- `EBOOK_MIN_SCORE`
- `EBOOK_MIN_MARGIN`
- `EBOOK_OVERWRITE_EXISTING`
- `EBOOK_KAVITA_SCAN`
- `EBOOK_KAVITA_URL`
- `EBOOK_KAVITA_API_KEY`
- `EBOOK_KAVITA_LIBRARY_ID`
- `EBOOK_READARR_SCAN`
- `EBOOK_READARR_URL`
- `EBOOK_READARR_API_KEY`
- `EBOOK_READARR_COMMAND_JSON`
- `EBOOK_SETTLE_SECONDS`

## Metadata Behavior (Important)

The script does not replace existing metadata fields. It only adds missing values.

File collision behavior:

- default (`--no-overwrite-existing`): keep existing file and create a numbered suffix via `unique_path` (for example `(2)`, `(3)`, ...)
- with `--overwrite-existing` or `EBOOK_OVERWRITE_EXISTING=true`: replace existing destination file

### Supported formats and fields

Current embedded metadata support is intentionally explicit:

- `EPUB` (`.epub`)
  - reads: `dc:title`, `dc:creator`, `dc:identifier` (ISBN-like), `dc:publisher`, `dc:language`, `dc:description`, `dc:date`, `dc:subject`
  - writes (missing-only): `dc:title`, `dc:creator`, `dc:identifier`, `dc:publisher`, `dc:language`, `dc:description`, `dc:date`, `dc:subject`
- `KEPUB` (`.kepub`, `.kepub.epub`)
  - treated as EPUB container
  - reads/writes same fields as EPUB
- `PDF` (`.pdf`)
  - reads: `/Title`, `/Author`, `/Producer` (publisher-ish), `/Subject`, `/Keywords`
  - writes (missing-only): `/Title`, `/Author`, `/Subject`, `/Keywords`
- `DOCX` (`.docx`)
  - reads: title, author, description, created date, keywords
  - writes (missing-only): title, author, description, created date, keywords
- `ODT` (`.odt`)
  - reads: title, creator, description, language, date, keywords
  - writes (missing-only): title, creator, description, language, date, keywords
- `FB2` (`.fb2`)
  - reads: book-title, author, publisher, language, annotation/date, genres/keywords
  - writes (missing-only): title, author, publisher, language, description/date, subjects
- `FBZ` (`.fbz`)
  - reads/writes FB2 metadata inside zipped container
- `HTMLZ` (`.htmlz`) and `TXTZ` (`.txtz`)
  - reads/writes OPF metadata when an OPF file exists in the zip container
- `RTF` (`.rtf`)
  - reads/writes `\\info` fields (`title`, `author`, `subject`, `keywords`) non-destructively
- `MOBI` / `PRC` / `AZW` / `AZW1` / `AZW3`
  - reads EXTH metadata when present
  - write path currently conservative: reports `SKIP_WRITE_*` for planned enrichments in non-dry mode, so files are still moved but binary metadata is not mutated yet

Unsupported today: `azw4`, `lrf`, `tpz`, and plain `txt` (non-zipped) embedded metadata writing.

If you want richer fields or additional formats, please open a fixture-backed PR. This project welcomes incremental coverage improvements.

If embedded metadata already has title and author but upstream matching is inconclusive, the file can still be moved using embedded title+author to avoid a no-op.

## Lookup and Matching Logic

High-level flow:

1. Read embedded metadata (format-specific handler).
2. Parse filename (`author - title` where possible).
3. Build search query (prefer embedded author/title, then filename fields).
4. Fetch candidates from Hardcover API.
5. Score title + author and calculate total score.
6. Compute boolean match flags for each candidate:
   - `title_match`
   - `author_match`
7. Choose a candidate with tie handling rules.
8. Enrich missing metadata non-destructively and move file.

Why `title_match` and `author_match` matter:

- If both are true for top close candidates, we relax tie handling and choose one.
- If either is false in a close/ambiguous case, we reject and leave untouched.
- This protects no-metadata cases where filename parsing may be wrong.

## Sync Targets (Kavita, Readarr, and Equivalent)

### Kavita

Enable with:

- `--kavita-scan` or `EBOOK_KAVITA_SCAN=true`
- set `--kavita-api-key` / `EBOOK_KAVITA_API_KEY`
- optional `--kavita-library-id` / `EBOOK_KAVITA_LIBRARY_ID`

### Bookshelf/Readarr/Forks

Enable with:

- `--readarr-scan` or `EBOOK_READARR_SCAN=true`
- set `--readarr-api-key` / `EBOOK_READARR_API_KEY`

Command payload examples (`EBOOK_READARR_COMMAND_JSON`):

- default full folder rescan:

```json
{"name":"RescanFolders"}
```

- custom payload example (depends on your Readarr version and enabled commands):

```json
{"name":"RescanFolders","filterKey":"all"}
```

Tip: command names/fields can vary by version. Check your instance command schema/endpoints before changing payloads.

Readarr rename safety recommendation:

- In Readarr Media Management, disable broad auto-renaming/organizing for library scans if this script is your canonical organizer.
- Prefer settings that only rename/manage content Readarr downloaded itself.
- Option names vary by version; verify with a dry-run workflow in your environment.

## Adding Another Sync Target (and submitting a PR)

Suggested pattern:

1. Add config fields to `AppConfig`.
2. Add CLI flags and env vars in `build_config` + argparse section.
3. Add a `trigger_<target>_sync(...)` function near existing trigger hooks.
4. Call it after move in `process_file` (mirroring Kavita/Readarr flow).
5. Add unit tests for success + missing credential behavior.
6. Update README with setup examples.

PR checklist:

- keep behavior non-destructive for metadata
- include tests for new decision paths
- include clear docs for env vars and example payloads

## Tests and Collaboration Workflow

Run tests:

```bash
.venv/bin/python tests/run_tests.py --verbose
```

Useful options:

- list tests only: `.venv/bin/python tests/run_tests.py --list`
- show per-test output: `.venv/bin/python tests/run_tests.py --verbose --show-output`

### Submitting a bugfix test from console output

If the script makes a wrong move/no-move decision:

1. Copy the relevant console block (`FILE`, `EMBEDDED_*`, `FILENAME_*`, `CANDIDATE_*`, `DECISION`).
2. Add it as a fixture in `tests/test_matching_behavior.py`.
3. Add a test asserting expected `choose_candidate` result and/or `process_file` move result.
4. Make that test fail first, then patch logic, then re-run tests.

This fixture-first workflow keeps matching improvements grounded in real incidents.

## Current Test Coverage Focus

The suite already covers:

- real tie/ambiguity examples from production logs
- no-metadata accept/reject scenarios
- process-level move behavior with mocked upstream API
- core helper functions (`norm_text`, filename parsing, query building, match flags)

Please keep adding new edge cases as fixtures. Community examples are exactly what make this matcher better.
