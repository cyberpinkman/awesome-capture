#!/usr/bin/env python3
"""Run the offline unittest suite and make every skip a CI failure."""

from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path
from typing import TextIO


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-directory", default="tests")
    parser.add_argument("--pattern", default="test*.py")
    parser.add_argument("--fail-on-skip", action="store_true")
    parser.add_argument("--github-annotations", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    return parser


def _annotation_escape(value: str) -> str:
    return (
        value.replace("%", "%25")
        .replace("\r", "%0D")
        .replace("\n", "%0A")
        .replace(":", "%3A")
        .replace(",", "%2C")
    )


def emit_github_annotations(
    result: unittest.TestResult,
    *,
    fail_on_skip: bool,
    stream: TextIO = sys.stderr,
) -> None:
    groups = (
        ("Unit test failure", result.failures),
        ("Unit test error", result.errors),
        (
            "Forbidden skipped test",
            result.skipped if fail_on_skip else (),
        ),
        ("Unexpected test success", result.unexpectedSuccesses),
    )
    for title, entries in groups:
        for entry in entries:
            test = entry[0] if isinstance(entry, tuple) else entry
            identifier = test.id()
            print(
                f"::error title={_annotation_escape(title)}::"
                f"{_annotation_escape(identifier)}",
                file=stream,
            )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    start = Path(args.start_directory)
    if not start.is_dir():
        print(f"test directory does not exist: {start}", file=sys.stderr)
        return 2
    suite = unittest.defaultTestLoader.discover(
        str(start),
        pattern=args.pattern,
    )
    runner = unittest.TextTestRunner(
        verbosity=1 if args.quiet else 2,
        stream=sys.stderr,
    )
    result = runner.run(suite)
    if args.github_annotations:
        emit_github_annotations(
            result,
            fail_on_skip=args.fail_on_skip,
        )
    skipped = len(result.skipped)
    unexpected = len(result.unexpectedSuccesses)
    if args.fail_on_skip and skipped:
        print(f"FAIL: {skipped} skipped test(s) are forbidden.", file=sys.stderr)
    if unexpected:
        print(f"FAIL: {unexpected} unexpected success(es).", file=sys.stderr)
    return 0 if result.wasSuccessful() and not unexpected and not (args.fail_on_skip and skipped) else 1


if __name__ == "__main__":
    raise SystemExit(main())
