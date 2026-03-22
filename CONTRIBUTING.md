# Contributing

Thanks for considering a contribution.

This project is intentionally practical and test-driven. The easiest, highest-value contribution is often a single failing fixture test based on real console output.

If you use AI/agentic coding tools, even better: please use them to speed up small, targeted improvements.

## Why contributions are easy here

- The core behavior lives in a single script: `ebook_resolve_move.py`.
- Matching behavior is covered by fixture-based tests.
- Most bug reports can be turned into one test + one small logic patch.

## Fast path: submit a bugfix test from logs

When you hit a bad decision (`moved` vs `left untouched`):

1. Copy the console block for that file:
   - `FILE`
   - `EMBEDDED_*`
   - `FILENAME_*`
   - `CANDIDATE_*`
   - `DECISION`
2. Add it as a fixture in `tests/test_matching_behavior.py`.
3. Add an assertion for expected behavior.
4. Run tests and make sure it fails first.
5. Patch logic minimally.
6. Re-run tests.

This is the preferred workflow because it prevents overfitting and preserves prior behavior.

## Development setup

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install httpx pypdf rapidfuzz watchdog
```

## Run tests

```bash
.venv/bin/python tests/run_tests.py --verbose
```

Useful options:

- list tests: `.venv/bin/python tests/run_tests.py --list`
- show test stdout/stderr: `.venv/bin/python tests/run_tests.py --verbose --show-output`

## Contribution guidelines

- Keep metadata writes non-destructive: only fill missing fields.
- Prefer small, focused PRs.
- Add or update tests for every behavioral change.
- Preserve existing logging shape where possible (fixtures rely on it).
- Document new CLI/env options in `README.md`.

## Coding style

- Python, straightforward over clever.
- Avoid unnecessary dependencies.
- Keep matching logic explainable and test-backed.

## Pull request checklist

- [ ] Includes tests for behavior change
- [ ] Existing tests still pass
- [ ] README updated if user-facing behavior changed
- [ ] No metadata overwrite behavior introduced

## Reporting issues

Please include:

- command used (or watcher context)
- console output block
- expected behavior
- actual behavior

If you can include a minimal fixture in `tests/test_matching_behavior.py`, that's ideal.

## Licensing

By contributing, you agree your contributions are licensed under GPLv3 (project license).
