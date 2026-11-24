#!/usr/bin/env python3
"""Project style checks that are easy to regress in prose and docs.

The project has a standing rule: no em-dashes (U+2014) in code, comments or
user-facing copy.  This test walks the tracked text files and fails when one
appears, so it cannot slip back in through a docs edit.

Run from the repository root:

    python3 tests/test_style.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Extensions that hold prose or source the user can see.
TEXT_SUFFIXES = {
    ".md", ".py", ".sh", ".service", ".desktop", ".svg", ".patch",
    ".rules", ".conf", ".json", ".txt",
}
#: Folders that are generated or not part of the project.
SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", ".pytest_cache"}

#: The character the rule forbids: em dash.
EM_DASH = "\u2014"

failures: list[str] = []
checked = 0


def check(label: str, got, want) -> None:
    global checked
    checked += 1
    if got == want:
        print(f"ok   {label}")
    else:
        failures.append(label)
        print(f"FAIL {label}: got {got!r}, want {want!r}")


def iter_text_files():
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        yield path


offenders: list[str] = []
for path in iter_text_files():
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        continue
    for number, line in enumerate(text.splitlines(), start=1):
        if EM_DASH in line:
            offenders.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()[:100]}")

check("no em-dash (U+2014) in project text files", offenders, [])

print()
if failures:
    print(f"STYLE FAILED: {len(failures)} of {checked} check(s): {', '.join(failures)}")
    sys.exit(1)
print(f"STYLE PASSED: {checked} checks")
