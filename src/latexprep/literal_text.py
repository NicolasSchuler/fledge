"""Literal TeX text without expansion: shared command tables and plain-text reading.

Manuscript checks, author records and submission text read the same small set of
commands. This module is the one place that says which commands carry prose,
which only hold keys or layout values, and how literal text is recovered without
executing TeX. Every other control sequence stays visible as unresolved. The
tables are built on the vocabularies in ``tex_commands`` (citation commands, math
markers) and ``comment_markers`` (placeholder wording), so other readers that
need those lists derive them from the same names.
"""

from __future__ import annotations

import re
from typing import NamedTuple, TypedDict

from .comment_markers import MarkerCategory, marker_pattern
from .tex_commands import (
    MATH_DELIMITER,
    MATH_ENVIRONMENT_PATTERN,
    MATH_MARKER,
    PRINTED_CITATION_COMMANDS,
    with_capitalized_forms,
)
from .tex_lexer import Command, ParseLimitExceeded, mask_literals, scan_commands
from .text_edits import TextEdit, apply_text_edits

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
# Cross-references: their argument is a label key.
REFERENCE_COMMANDS = frozenset(
    {"ref", "pageref", "eqref", "autoref", "cref", "Cref", "nameref", "vref"}
)
# Label, citation and reference commands: their arguments are keys, never prose.
# \nameref and \vref print text taken from their target, so the literal scan leaves
# them unresolved instead of counting them as nothing.
KEY_ARGUMENT_COMMANDS = frozenset(
    {
        "label",
        "nocite",
        *with_capitalized_forms(PRINTED_CITATION_COMMANDS),
        *(REFERENCE_COMMANDS - {"nameref", "vref"}),
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
KEYWORD_ENVIRONMENTS = frozenset({"keywords", "keyword", "IEEEkeywords"})

# Display and inline math to drop. In the inline form an escaped character is consumed
# as a pair and the loop is possessive, so a stray ``$`` with no partner fails in
# linear time instead of retrying every way to split the escapes.
_MATH_PATTERNS = tuple(
    re.compile(pattern, re.S)
    for pattern in (
        r"\$\$.*?\$\$",
        r"(?<!\\)\$(?:\\.|[^$\\])*+\$",
        r"\\\(.*?\\\)",
        r"\\\[.*?\\\]",
    )
)

_PLACEHOLDER_MARKER = marker_pattern(
    MarkerCategory.EDIT_MARKER,
    MarkerCategory.TO_BE_DETERMINED,
    MarkerCategory.UNCERTAINTY_MARK,
)
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
    for pattern in _MATH_PATTERNS:
        value = pattern.sub(" ", value)
    if MATH_DELIMITER.search(value):
        uncertain.add("unbalanced math delimiters")
    try:
        commands = scan_commands(value)
    except ParseLimitExceeded as error:
        # The text is read only up to the lexer's bounds; nothing after them is counted.
        if error.reason in {"unclosed_group", "oversized_group"}:
            return LiteralText("", ["unclosed or oversized argument"], 1)
        return LiteralText("", ["more commands or argument text than the literal scan supports"], 1)
    edits: list[TextEdit] = []
    covered_until = -1
    for command in commands:
        if command.start < covered_until:
            continue
        interpretation = _INTERPRETATIONS.get(command.name)
        if interpretation is None:
            uncertain.add("\\" + command.name)
            unresolved_macros += 1
            edits.append(TextEdit(command.start, command.name_end, " "))
            continue
        stop = command.name_end
        if interpretation.dropped_arguments:
            dropped = (
                *command.arguments[: interpretation.dropped_arguments],
                *command.options,
            )
            stop = max((argument.end + 1 for argument in dropped), default=stop)
            covered_until = stop
        edits.append(TextEdit(command.start, stop, interpretation.replacement))
    text = apply_text_edits(value, edits).replace("{", "").replace("}", "").replace("~", " ")
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
    if not value.strip() and MATH_MARKER.search(raw):
        uncertainty.append("only mathematical content is observable")
    return " ".join(value.split()), uncertainty


def is_placeholder_text(value: str) -> bool:
    """Whether a literal value is an obvious template placeholder rather than content."""
    normalized = value.casefold().strip(" .<>[]")
    return bool(
        _PLACEHOLDER_MARKER.search(normalized)
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
