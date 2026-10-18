"""TeX source vocabularies: file suffixes, math markers and command families.

Every module that reads TeX source needs the same few lists: which file suffixes hold
TeX, how mathematics starts, which commands define macros, which commands cite. They
are written once here, as names built on each other, so that readers cannot drift
apart. The prose tables (formatting wrappers, labels, headings) live in
``literal_text``, which builds its key-argument table from the citation commands
below. This module imports nothing from the package and may be used by any layer.

File suffix sets, each a superset of the one before:

``ROOT_SUFFIXES``
    Files that can be a document root, and the only files whose text is rewritten.
``MANUSCRIPT_SUFFIXES``
    Authored manuscript text: roots plus the TikZ and PGF picture sources.
``TEX_SOURCE_SUFFIXES``
    Every TeX-like input the source graph reads: manuscript text plus class,
    package, definition and configuration files.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

ROOT_SUFFIXES = frozenset({".tex", ".ltx", ".latex"})
MANUSCRIPT_SUFFIXES = ROOT_SUFFIXES | {".tikz", ".pgf"}
TEX_SOURCE_SUFFIXES = MANUSCRIPT_SUFFIXES | {".sty", ".cls", ".def", ".cfg"}

MATH_ENVIRONMENTS = ("equation", "equation*", "align", "align*", "displaymath", "math")
# Regular-expression alternation of the math environment names.
MATH_ENVIRONMENT_PATTERN = "|".join(map(re.escape, MATH_ENVIRONMENTS))
# An unescaped ``$``, or any of ``\(``, ``\)``, ``\[`` and ``\]``: the delimiters that open
# or close inline and display math outside an environment.
MATH_DELIMITER = re.compile(r"(?<!\\)\$|\\[()[\]]")
# Where mathematics begins or ends: a math delimiter, or the start of a math environment.
# The environment name is matched as a prefix, so ``aligned`` and ``equation*`` count as well.
MATH_MARKER = re.compile(rf"{MATH_DELIMITER.pattern}|\\begin\{{(?:{MATH_ENVIRONMENT_PATTERN})")

# TeX primitives that define a macro or alias; the name follows the primitive unbraced.
PRIMITIVE_DEFINITION_COMMANDS = frozenset({"def", "gdef", "edef", "xdef", "let", "futurelet"})
# Tokens after these commands name a macro being defined, not a conditional being run.
DEFINITION_COMMANDS = PRIMITIVE_DEFINITION_COMMANDS | {"newif", "global", "long", "protected"}
# LaTeX commands that define one command, with its name in braces.
COMMAND_DEFINITION_COMMANDS = frozenset(
    {
        "newcommand",
        "renewcommand",
        "providecommand",
        "DeclareRobustCommand",
        "NewDocumentCommand",
        "RenewDocumentCommand",
        "ProvideDocumentCommand",
        "DeclareDocumentCommand",
    }
)
# Commands whose arguments declare a macro or environment. Neither the command nor
# its replacement body is executed where it stands, so it is never an unknown
# construct in the surrounding manuscript.
MACRO_DEFINITION_COMMANDS = (
    DEFINITION_COMMANDS
    | COMMAND_DEFINITION_COMMANDS
    | {
        "newcommandx",
        "renewcommandx",
        "newenvironment",
        "renewenvironment",
        "NewDocumentEnvironment",
        "RenewDocumentEnvironment",
        "DeclareMathOperator",
        "DeclarePairedDelimiter",
        "newtheorem",
        "declaretheorem",
        "newcolumntype",
        "newlength",
        "newsavebox",
        "newcounter",
    }
)

# Commands, in their lower-case spelling, that print a citation of the keys in their
# first argument. natbib and biblatex also define a capitalised twin of most of them
# (``\Cite``, ``\Textcite``); see :func:`with_capitalized_forms`.
PRINTED_CITATION_COMMANDS = frozenset(
    "cite citep citet citealp citealt citeauthor citeyear citeyearpar "
    "parencite textcite autocite footcite footcitetext smartcite supercite "
    "fullcite footfullcite notecite pnotecite fnotecite citeurl citetitle".split()
)
# Every command that adds the keys in its first argument to the bibliography:
# \nocite does so without printing a citation.
CITATION_COMMANDS = PRINTED_CITATION_COMMANDS | {"nocite"}
# biblatex commands that take one optional-argument pair and key group per citation.
MULTICITE_COMMANDS = frozenset(
    "cites parencites textcites autocites footcites footcitetexts smartcites supercites".split()
)


def with_capitalized_forms(names: Iterable[str]) -> frozenset[str]:
    """``names`` and their spellings with a capital first letter, such as ``Citet``."""
    names = frozenset(names)
    return names | {name[:1].upper() + name[1:] for name in names}
