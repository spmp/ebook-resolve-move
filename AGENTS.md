# AGENTS.md

Guidance for AI coding agents and human contributors working on this repository.

## Is `CLAUDE.md` a standard?

Not universally.

- `CLAUDE.md` is a convention used by Anthropic/Claude workflows.
- A more tool-agnostic name is `AGENTS.md`.
- Many teams keep both (same content or one pointing to the other) for compatibility.

This repository uses `AGENTS.md` as the generic, vendor-neutral standard.

## Project intent

`ebook_resolve_move.py` is a safety-first ingestion script:

- resolve metadata from Hardcover API
- enrich metadata without overwriting existing fields
- move into canonical folder structure
- optionally trigger external scans (Kavita/Readarr)

Primary operational goal:

- avoid leaving valid files unmoved when we have "right enough" metadata
- while still rejecting weak no-metadata guesses

## Architecture at a glance

Single-file app with clear sections:

1. utility helpers (normalization, filesystem safety)
2. metadata models (dataclasses)
3. embedded metadata readers (EPUB/PDF)
4. filename parsing
5. non-destructive metadata writers (EPUB/PDF)
6. Hardcover API client
7. matching/scoring and candidate choice
8. move + optional sync hooks
9. watch mode (watchdog)
10. CLI/env configuration

Tests:

- `tests/test_matching_behavior.py`: decision behavior and fixtures
- `tests/test_functions.py`: helper/function-level coverage
- `tests/run_tests.py`: friendly runner (`--list`, `--verbose`, `--show-output`)

## Non-negotiable behavior constraints

1. Never overwrite existing metadata fields.
2. Only add missing metadata fields.
3. Keep matching logic test-backed.
4. Preserve observability (logs should remain stable/readable).

## Matching logic principles

- Title and author are both considered; author should not be ignored.
- Candidate flags (`title_match`, `author_match`) are key for tie handling.
- Close-score ties can be accepted when both title and author are matched sufficiently.
- In no-metadata scenarios, be stricter to avoid bad moves.
- If embedded title+author are already present, moving may still proceed even when upstream match is weak.

## How agents should modify behavior

When changing matching/decision logic:

1. Add a fixture test from real output first.
2. Make test fail.
3. Apply minimal code change.
4. Run full test suite.
5. Document behavior update in README if user-visible.

Do not introduce large refactors while fixing scoring edge cases unless requested.

## External sync targets

Current built-ins:

- Kavita
- Readarr

To add another target:

1. add config fields to `AppConfig`
2. add CLI/env plumbing in `build_config` and argparse
3. add trigger function
4. invoke after successful move
5. add tests and docs

## Agentic contribution style

- Prefer small PRs with explicit intent.
- Include before/after behavior in PR description.
- Include at least one test for each bugfix.
- Favor deterministic behavior over complex heuristics.

Agentic coding lowers the cost of contribution; this repo welcomes many small, high-signal PRs, especially fixture tests.

## Validation commands

```bash
python3 -m py_compile ebook_resolve_move.py tests/test_functions.py tests/test_matching_behavior.py tests/run_tests.py
.venv/bin/python tests/run_tests.py --verbose
```
