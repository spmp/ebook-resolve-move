#!/usr/bin/env python3
import argparse
import sys
from pathlib import Path
import unittest


def iter_test_ids(suite: unittest.TestSuite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from iter_test_ids(test)
        else:
            yield test.id()


if __name__ == "__main__":
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    parser = argparse.ArgumentParser(
        description="Run ebook script unit tests with configurable output."
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show each passing test name.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List discovered tests without running them.",
    )
    parser.add_argument(
        "--show-output",
        action="store_true",
        help="Show stdout/stderr printed by tests.",
    )
    args = parser.parse_args()

    suite = unittest.defaultTestLoader.discover("tests", pattern="test_*.py")

    if args.list:
        for test_id in sorted(iter_test_ids(suite)):
            print(test_id)
        raise SystemExit(0)

    runner = unittest.TextTestRunner(
        verbosity=2 if args.verbose else 1,
        buffer=not args.show_output,
    )
    raise SystemExit(0 if runner.run(suite).wasSuccessful() else 1)
