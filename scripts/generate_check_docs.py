#!/usr/bin/env python3
"""Regenerate the check catalogue tables in docs/checks.md from the registered rules.

Run ``python scripts/generate_check_docs.py`` after changing a rule; use
``--check`` to fail when the documentation is out of date (used by the tests).

Each code is placed in one of three tables, derived from
``latexprep.check_policy.MANDATORY_CODES``, ``check_policy.opt_in_activation()``
and the default ``Settings``:

- always enforced: guards in ``MANDATORY_CODES``, which selection cannot remove;
- on by default: checks that run without any configuration;
- opt-in: checks whose activating setting is off or empty by default.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from latexprep import check_policy  # noqa: E402
from latexprep.config import Settings  # noqa: E402
from latexprep.rules import RULES, Rule  # noqa: E402

DOCUMENT = ROOT / "docs" / "checks.md"
BEGIN = "% Begin generated catalogue: run scripts/generate_check_docs.py; do not edit by hand.\n"
END = "% End generated catalogue.\n"

# Opt-in checks activated outside the selection policy's activation tables.
_ACTIVATED_ELSEWHERE: dict[str, tuple[str | None, tuple[str, ...]]] = {
    "FMT001": (None, ("format",)),
}

ALWAYS = "always"
DEFAULT = "default"
OPT_IN = "opt-in"


def _activation() -> dict[str, list[tuple[str | None, tuple[str, ...]]]]:
    """Map each opt-in code to the (table, fields) that switch it on."""
    result = {
        code: [(group, tuple(fields)) for group, fields in entries]
        for code, entries in check_policy.opt_in_activation().items()
    }
    for code, entry in _ACTIVATED_ELSEWHERE.items():
        result.setdefault(code, [entry])
    return result


def classify(rule: Rule, defaults: Settings | None = None) -> tuple[str, str]:
    """Return the rule's kind and, for opt-in checks, the settings that enable it."""
    if rule.code in check_policy.MANDATORY_CODES:
        return ALWAYS, ""
    entries = _activation().get(rule.code)
    if not entries:
        return DEFAULT, ""
    defaults = defaults or Settings()
    group, fields = entries[0]
    holder = defaults if group is None else getattr(defaults, group)
    disabled = all(not getattr(holder, field) for field in fields)
    if not disabled:
        return DEFAULT, ""
    tables = " or ".join(dict.fromkeys(f"`{name}`" for name, _ in entries if name))
    names = " or ".join(f"`{field}`" for field in fields)
    return OPT_IN, f"{names} in {tables}" if tables else names


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def _table(rules: list[tuple[Rule, str]], with_setting: bool) -> str:
    columns = ["Code", "Check and scope", *(["Enabled by"] if with_setting else []), "Typical fix"]
    lines = ["| " + " | ".join(columns) + " |", "|" + " --- |" * len(columns)]
    for rule, setting in rules:
        cells = [f"`{rule.code}`", f"**{_cell(rule.title)}.** {_cell(rule.description)}"]
        if with_setting:
            cells.append(setting)
        cells.append(_cell(rule.fix))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


SECTIONS = (
    (
        DEFAULT,
        "On by default",
        "These {count} checks run without any configuration whenever the command reaches the "
        "evidence they need. Turn one off with `[checks] ignore`.",
    ),
    (
        OPT_IN,
        "Opt-in checks",
        "These {count} checks run only after you supply the setting in the *Enabled by* column, "
        "for example through a preset or your `fledge.toml`. Online checks also need "
        "network permission.",
    ),
    (
        ALWAYS,
        "Always enforced",
        "These {count} guards protect reading, transforming, building and publishing the "
        "prepared copy. They apply whenever their stage runs, and ignoring their code "
        "does not switch them off.",
    ),
)


def generated() -> str:
    defaults = Settings()
    grouped: dict[str, list[tuple[Rule, str]]] = {kind: [] for kind, _, _ in SECTIONS}
    for rule in RULES:
        kind, setting = classify(rule, defaults)
        grouped[kind].append((rule, setting))
    parts = []
    for kind, heading, intro in SECTIONS:
        rules = grouped[kind]
        parts.append(f"### {heading}\n\n{intro.format(count=len(rules))}\n\n")
        parts.append(_table(rules, with_setting=kind == OPT_IN))
        parts.append("\n")
    return "".join(parts).rstrip("\n") + "\n"


def render(document: str) -> str:
    start = document.index(BEGIN) + len(BEGIN)
    end = document.index(END, start)
    return document[:start] + "\n" + generated() + "\n" + document[end:]


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
