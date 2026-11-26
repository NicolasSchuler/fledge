#!/usr/bin/env python3
"""Regenerate the check catalogue tables in docs/checks.md from the registered rules.

Run ``python scripts/generate_check_docs.py`` after changing a rule; use
``--check`` to fail when the documentation is out of date (used by the tests).

The shared rule catalogue classifies each code into one of four tables:

- always enforced: rules marked ``mandatory``, which selection cannot remove;
- on by default: checks that run without any configuration;
- opt-in: checks that need explicit settings or a dedicated command;
- suggestions: style advice that runs only when selected.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from latexprep.config import Settings  # noqa: E402
from latexprep.rule_catalogue import (  # noqa: E402
    ALWAYS,
    DEFAULT,
    OPT_IN,
    SUGGESTION,
    activation_options,
    rule_activation,
)
from latexprep.rule_definition import Rule  # noqa: E402
from latexprep.rules import RULES  # noqa: E402

DOCUMENT = ROOT / "docs" / "checks.md"
BEGIN = "% Begin generated catalogue: run scripts/generate_check_docs.py; do not edit by hand.\n"
END = "% End generated catalogue.\n"


# Characters that Markdown would read as syntax in a rule's plain title, description or
# fix: escapes, code spans, emphasis, links, raw HTML, entities, table cells, and the math
# and strikethrough of GitHub's renderer. A backslash makes each one print as itself.
_MARKDOWN_SYNTAX = re.compile(r"([\\`*_\[\]<>&|$~])")


def _cell(text: str) -> str:
    """Return plain rule text as one table cell that renders exactly as written.

    Args:
        text: A rule's title, description or fix, which is plain text, not Markdown.

    Returns:
        The text on one line, with every Markdown-significant character escaped.
    """
    return _MARKDOWN_SYNTAX.sub(r"\\\1", " ".join(text.split()))


def _table(rules: list[tuple[Rule, str]], *, with_setting: bool) -> str:
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
        "These {count} checks need the settings or command in the *Enabled by* column. "
        "Use a preset or `fledge.toml` to supply settings. Each alternative joined by *or* "
        "is sufficient to configure the check; settings joined by *and* go together, "
        "in one table where indicated. Unqualified settings go at the top level. "
        "The catalogue uses current names; see [renamed settings]"
        "(configuration.md#renamed-settings) for accepted aliases. "
        "Checks still need the relevant command and evidence: "
        "`[rendered_pdf_artwork]` applies to the manuscript PDF, while `[included_figure_artwork]` "
        "needs a source project with included PDF figures.",
    ),
    (
        SUGGESTION,
        "Suggestions",
        "These {count} checks give style advice. They run only when selected beside `ALL`, by "
        "code, family or prefix (for example `--select ALL --select STY`, or "
        '`select = ["ALL", "STY"]` in `[checks]`), are reported in a section of their own and '
        "never change the outcome or the exit code; a `% fledge: ignore CODE` comment silences "
        "one occurrence.",
    ),
    (
        ALWAYS,
        "Always enforced",
        "These {count} guards protect reading, transforming, building and publishing the "
        "prepared copy, and report what Fledge could not read or follow. They apply whenever "
        "their stage runs, and ignoring their code does not switch them off.",
    ),
)


def generated() -> str:
    defaults = Settings()
    grouped: dict[str, list[tuple[Rule, str]]] = {kind: [] for kind, _, _ in SECTIONS}
    for rule in RULES:
        activation = rule_activation(rule, defaults)
        setting = " or ".join(activation_options(activation, markdown=True))
        if activation.note:
            setting = f"{setting}. {activation.note}"
        grouped[activation.kind].append((rule, setting))
    parts = []
    for kind, heading, intro in SECTIONS:
        rules = grouped[kind]
        parts.append(f"### {heading}\n\n{intro.format(count=len(rules))}\n\n")
        parts.append(_table(rules, with_setting=kind in {OPT_IN, SUGGESTION}))
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
