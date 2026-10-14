#!/usr/bin/env python3
"""Fail on docstrings longer than 5 lines, and on history in docstrings or comments.

Docstrings say what the code does. The commit message keeps why it changed.
"""

from __future__ import annotations

import argparse
import ast
import io
import re
import sys
import tokenize
from pathlib import Path

MAX_LINES = 5
ROOTS = ("portolan_registry_qgis", "scripts", "tests")
# Synced from portolan-ops. Fix it there.
SYNCED = frozenset({Path("scripts/pip_audit_ignores.py")})
HISTORY = re.compile(
    r"\bBefore,"
    r"|\bpreviously\b"
    r"|\bno longer\b"
    r"|\bused to (?:be|have)\b"
    r"|\bwas (?:added|changed|moved|removed|renamed)\b"
    r"|\b(?:issue|PR|pull request|fixes|closes) #\d+"
    r"|\(#\d+\)",
    re.IGNORECASE,
)
_Documented = ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef


def _docstrings(tree: ast.Module) -> list[tuple[int, str]]:
    """Return the line and text of each docstring in ``tree``."""
    return [
        (node.body[0].lineno, text)
        for node in ast.walk(tree)
        if isinstance(node, _Documented) and (text := ast.get_docstring(node))
    ]


def _comments(source: str) -> list[tuple[int, str]]:
    """Return the line and text of each comment in ``source``."""
    tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    return [(t.start[0], t.string) for t in tokens if t.type == tokenize.COMMENT]


def problems(source: str) -> list[tuple[int, str]]:
    """Return the line and message of each problem in ``source``."""
    found = []
    for line, text in _docstrings(ast.parse(source)):
        if (count := len(text.splitlines())) > MAX_LINES:
            found.append((line, f"docstring has {count} lines, max {MAX_LINES}"))
        if match := HISTORY.search(text):
            found.append((line, f"history in docstring: {match.group()!r}"))
    for line, text in _comments(source):
        if match := HISTORY.search(text):
            found.append((line, f"history in comment: {match.group()!r}"))
    return sorted(found)


def main(argv: list[str] | None = None) -> int:
    """Check the Python files under ``ROOTS``, and return 1 on any problem."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="*", type=Path, default=[Path(r) for r in ROOTS])
    args = parser.parse_args(argv)
    found = (f for p in args.paths for f in ([p] if p.is_file() else p.rglob("*.py")))
    files = sorted(f for f in found if f not in SYNCED)
    failed = 0
    for path in files:
        for line, message in problems(path.read_text(encoding="utf-8")):
            print(f"{path}:{line}: {message}")
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
