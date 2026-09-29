#!/usr/bin/env python3
"""Offline docs check: relative Markdown links resolve, and every test named in the docs exists.

Checks every tracked ``*.md`` outside ``service/tests/fixtures``:

* ``[text](relative/path)`` and ``[text](relative/path#anchor)`` point at a file or
  directory that exists (external ``http(s)``/``mailto`` links are not fetched: offline);
* ``[text](#anchor)`` and ``path.md#anchor`` anchors match a heading in the target file;
* every backticked ``test_...`` name in the docs is defined in ``service/tests/``.

Standard library only. Exit 1 with one line per problem.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")
TEST_REF = re.compile(r"`(test_[a-z0-9_]+)`")
HEADING = re.compile(r"^#{1,6}\s+(.*)$", re.MULTILINE)
FENCE = re.compile(r"```.*?```", re.DOTALL)


def slug(heading: str) -> str:
    """GitHub's anchor for a heading: lowercase, drop punctuation, spaces to hyphens."""
    text = re.sub(r"`", "", heading.strip().lower())
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def anchors(path: Path) -> set[str]:
    return {slug(h) for h in HEADING.findall(FENCE.sub("", path.read_text(encoding="utf-8")))}


def markdown_files() -> list[Path]:
    listed = subprocess.run(
        ["git", "ls-files", "*.md", "**/*.md"],  # noqa: S607 - git on PATH
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return [ROOT / p for p in sorted(set(listed)) if "tests/fixtures" not in p]


def defined_tests() -> set[str]:
    names: set[str] = set()
    for path in (ROOT / "service" / "tests").glob("test_*.py"):
        names.update(re.findall(r"^def (test_[a-z0-9_]+)", path.read_text(), re.MULTILINE))
    return names


def main() -> int:
    problems: list[str] = []
    tests = defined_tests()
    for md in markdown_files():
        text = FENCE.sub("", md.read_text(encoding="utf-8"))
        rel = md.relative_to(ROOT)
        for target in LINK.findall(text):
            if re.match(r"^(https?:|mailto:)", target):
                continue
            path_part, _, anchor = target.partition("#")
            dest = (md.parent / path_part).resolve() if path_part else md
            if not dest.exists():
                problems.append(f"{rel}: broken link {target}")
                continue
            if anchor and dest.suffix == ".md" and anchor not in anchors(dest):
                problems.append(f"{rel}: no heading for #{anchor} in {dest.relative_to(ROOT)}")
        for name in TEST_REF.findall(md.read_text(encoding="utf-8")):
            if name not in tests:
                problems.append(f"{rel}: names {name}, which no test defines")
    for problem in problems:
        print(problem)
    print(f"docs-check: {len(markdown_files())} files, {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
