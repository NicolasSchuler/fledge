#!/usr/bin/env python3
"""Regenerate the check catalogue table in docs/checks.md from the registered rules.

Run ``python scripts/generate_check_docs.py`` after changing a rule; use
``--check`` to fail when the documentation is out of date (used by the tests).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from latexprep.rules import RULES  # noqa: E402

DOCUMENT = ROOT / "docs" / "checks.md"
HEADER = "| Code | Check and scope | Behavioral test file |\n| --- | --- | --- |\n"
END = "\n## Extending a check"


def _row(code: str, title: str, description: str, tests: tuple[str, ...]) -> str:
    modules = dict.fromkeys(test.split(".")[1] for test in tests)
    files = ", ".join(f"{{download}}`{name}.py <../tests/{name}.py>`" for name in modules)
    text = description.replace("|", "\\|")
    return f"| `{code}` | **{title}.** {text} | {files} |\n"


def render(document: str) -> str:
    start = document.index(HEADER) + len(HEADER)
    end = document.index(END, start)
    rows = "".join(_row(r.code, r.title, r.description, r.tests) for r in RULES)
    return document[:start] + rows + document[end:]


def main(arguments: list[str]) -> int:
    current = DOCUMENT.read_text(encoding="utf-8")
    expected = render(current)
    if "--check" in arguments:
        if current != expected:
            print(
                "docs/checks.md is out of date; run scripts/generate_check_docs.py", file=sys.stderr
            )
            return 1
        return 0
    DOCUMENT.write_text(expected, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
