"""Literal TeX text without expansion: shared command tables and plain-text reading.

Manuscript checks, author records and submission text read the same small set of
commands. This module is the one place that says which commands carry prose,
which only hold keys or layout values, and how literal text is recovered without
executing TeX. Every other control sequence stays visible as unresolved.
"""

from __future__ import annotations

import re
from typing import NamedTuple, TypedDict

from .tex_lexer import Command, ParseLimitExceeded, mask_literals, scan_commands

# Formatting wrappers whose argument is retained as ordinary text.
TEXT_WRAPPERS = frozenset(
    {
        "emph",
        "textbf",
        "textit",
        "textmd",
        "textnormal",
        "textrm",
        "textsc",
        "textsf",
        "textsl",
        "texttt",
        "textup",
        "mbox",
        "underline",
        "mathrm",
        "mathbf",
    }
)
# Label, citation and reference commands: their arguments are keys, never prose.
KEY_ARGUMENT_COMMANDS = frozenset(
    {
        "label",
        "cite",
        "citep",
        "citet",
        "citeauthor",
        "citeyear",
        "parencite",
        "textcite",
        "ref",
        "pageref",
        "eqref",
        "autoref",
        "cref",
        "Cref",
        "nocite",
    }
)
# Structural, bibliographic and layout commands that contribute no countable
# manuscript text and leave no interpretation gap. Their own token disappears
# while surrounding literal text is retained. This list is the single source of
# truth shared with the manuscript policy checks' interpreted-command inventory.
_NONCONTENT_COMMANDS = frozenset(
    {
        "appendix",
        "bfseries",
        "bigskip",
        "bottomrule",
        "centering",
        "cleardoublepage",
        "clearpage",
        "displaystyle",
        "flushleft",
        "flushright",
        "footnotesize",
        "hfill",
        "hline",
        "hrule",
        "huge",
        "Huge",
        "ignorespaces",
        "itshape",
        "large",
        "Large",
        "LARGE",
        "listoffigures",
        "listoftables",
        "maketitle",
        "medskip",
        "midrule",
        "newpage",
        "noindent",
        "nonumber",
        "normalfont",
        "normalsize",
        "pagebreak",
        "printbibliography",
        "protect",
        "raggedleft",
        "raggedright",
        "relax",
        "rmfamily",
        "scriptsize",
        "selectfont",
        "sffamily",
        "small",
        "smallskip",
        "strut",
        "tableofcontents",
        "tiny",
        "today",
        "toprule",
        "ttfamily",
        "upshape",
        "vfill",
    }
)
# Non-content commands whose leading arguments hold a filename, length, counter
# or style value instead of prose; the token and that many arguments disappear.
_NONCONTENT_ARGUMENTS = {
    **{
        name: 1
        for name in (
            "addbibresource",
            "addtocounter",
            "bibliography",
            "bibliographystyle",
            "cline",
            "cmidrule",
            "graphicspath",
            "hphantom",
            "hskip",
            "hspace",
            "hypersetup",
            "includegraphics",
            "kern",
            "linespread",
            "pagenumbering",
            "pagestyle",
            "phantom",
            "refstepcounter",
            "setstretch",
            "stepcounter",
            "thispagestyle",
            "vphantom",
            "vskip",
            "vspace",
        )
    },
    "addtolength": 2,
    "fontsize": 2,
    "rule": 2,
    "setcounter": 2,
    "setlength": 2,
}
_SPACE_COMMANDS = frozenset({"par", "newline", "linebreak", "and", "sep", "\\", ",", ";", "!", " "})
_CHARACTER_COMMANDS = frozenset({"%", "&", "#", "_", "{", "}"})


class _Interpretation(NamedTuple):
    """How the literal text scan replaces one known command."""

    replacement: str
    # Leading arguments removed together with the token; every option goes too.
    dropped_arguments: int = 0


# The command groups are disjoint, so each name has exactly one interpretation.
_INTERPRETATIONS: dict[str, _Interpretation] = {
    **{name: _Interpretation(name) for name in _CHARACTER_COMMANDS},
    **{name: _Interpretation(" ") for name in _SPACE_COMMANDS | _NONCONTENT_COMMANDS},
    **{name: _Interpretation(name) for name in ("TeX", "LaTeX")},
    **{name: _Interpretation("") for name in TEXT_WRAPPERS},
    **{name: _Interpretation(" ", 1) for name in KEY_ARGUMENT_COMMANDS},
    **{name: _Interpretation(" ", count) for name, count in _NONCONTENT_ARGUMENTS.items()},
}
# Every command the literal text scan interprets without an interpretation gap.
KNOWN_TEXT_COMMANDS = frozenset(_INTERPRETATIONS)

# Sectioning commands and their depth; \section has depth one.
HEADING_DEPTHS = {
    "part": -1,
    "chapter": 0,
    "section": 1,
    "subsection": 2,
    "subsubsection": 3,
    "paragraph": 4,
    "subparagraph": 5,
}
PACKAGE_COMMANDS = frozenset({"usepackage", "RequirePackage", "RequirePackageWithOptions"})
REFERENCE_COMMANDS = frozenset(
    {"ref", "pageref", "eqref", "autoref", "cref", "Cref", "nameref", "vref"}
)
KEYWORD_ENVIRONMENTS = frozenset({"keywords", "keyword", "IEEEkeywords"})
MATH_ENVIRONMENTS = ("equation", "equation*", "align", "align*", "displaymath", "math")
# Regular-expression alternation of the math environment names.
MATH_ENVIRONMENT_PATTERN = "|".join(map(re.escape, MATH_ENVIRONMENTS))

_PLACEHOLDER_VALUES = frozenset(
    {
        "placeholder",
        "description",
        "figure description",
        "author name",
        "your name",
        "your title",
        "your affiliation",
        "email address",
    }
)


class LiteralText(NamedTuple):
    """Countable literal text, the unresolved constructs, and how often unknown macros occur.

    The occurrence count bounds how much rendered text the unresolved macros could
    contribute; it is deliberately not an estimate of their expansion.
    """

    text: str
    unresolved: list[str]
    unresolved_macros: int


def scan_literal_text(value: str) -> LiteralText:
    """Read literal text from TeX source, dropping math, keys and known layout commands."""
    value = mask_literals(value)
    uncertain: set[str] = set()
    unresolved_macros = 0
    value = re.sub(
        rf"\\begin\{{({MATH_ENVIRONMENT_PATTERN})\}}.*?\\end\{{\1\}}", " ", value, flags=re.S
    )
    for pattern in (
        r"\$\$.*?\$\$",
        r"(?<!\\)\$(?:\\.|[^$])*?(?<!\\)\$",
        r"\\\(.*?\\\)",
        r"\\\[.*?\\\]",
    ):
        value = re.sub(pattern, " ", value, flags=re.S)
    if re.search(r"(?<!\\)\$|\\[()[\]]", value):
        uncertain.add("unbalanced math delimiters")
    try:
        commands = scan_commands(value)
    except ParseLimitExceeded:
        return LiteralText("", ["unclosed or oversized argument"], 1)
    edits: list[tuple[int, int, str]] = []
    covered_until = -1
    for command in commands:
        if command.start < covered_until:
            continue
        interpretation = _INTERPRETATIONS.get(command.name)
        if interpretation is None:
            uncertain.add("\\" + command.name)
            unresolved_macros += 1
            edits.append((command.start, command.name_end, " "))
            continue
        stop = command.name_end
        if interpretation.dropped_arguments:
            dropped = (
                *command.arguments[: interpretation.dropped_arguments],
                *command.options,
            )
            stop = max((argument.end + 1 for argument in dropped), default=stop)
            covered_until = stop
        edits.append((command.start, stop, interpretation.replacement))
    for start, stop, replacement in reversed(edits):
        value = value[:start] + replacement + value[stop:]
    text = value.replace("{", "").replace("}", "").replace("~", " ")
    return LiteralText(text, sorted(uncertain), unresolved_macros)


def plain_text(value: str) -> tuple[str, list[str]]:
    """Return countable literal text and unresolved constructs, without expanding TeX."""
    text, unresolved, _ = scan_literal_text(value)
    return text, unresolved


def plain_argument_text(command: Command, index: int = 0) -> tuple[str, list[str]]:
    """Return one argument's literal text, whitespace collapsed, and its unresolved constructs."""
    if len(command.arguments) <= index:
        return "", ["missing literal argument"]
    raw = command.arguments[index].value
    value, uncertainty = plain_text(raw)
    if not value.strip() and re.search(r"(?<!\\)\$|\\[([]", raw):
        uncertainty.append("only mathematical content is observable")
    return " ".join(value.split()), uncertainty


def is_placeholder_text(value: str) -> bool:
    """Whether a literal value is an obvious template placeholder rather than content."""
    normalized = value.casefold().strip(" .<>[]")
    return bool(
        re.search(r"\b(?:todo|tbd|fixme|xxx)\b|\?\?", normalized)
        or normalized in _PLACEHOLDER_VALUES
        or re.fullmatch(
            r"(?:insert|add|enter|replace with|your) .*(?:here|description|text)", normalized
        )
    )


def normalize_heading(value: str) -> str:
    """Compare headings case-insensitively with collapsed whitespace."""
    return " ".join(value.split()).casefold()


def literal_list(value: str) -> list[str] | None:
    """Split a comma-separated literal name list; ``None`` when any item is not literal."""
    if re.search(r"[\\{}#$^~&%\x00]", value):
        return None
    items = [item.strip() for item in value.split(",")]
    return items if all(items) else None


class Declaration(TypedDict):
    """A literal class or package declaration in one source file, as reported in details."""

    path: str
    line: int
    names: list[str] | None
    options: list[str] | None
    uncertainty: list[str]


def first_argument_names(command: Command) -> list[str] | None:
    """The literal names declared in a command's first argument, such as package names."""
    return literal_list(command.arguments[0].value) if command.arguments else None
