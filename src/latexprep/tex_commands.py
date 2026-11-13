"""TeX source vocabularies: file suffixes, math markers and command families.

Every module that reads TeX source needs the same few lists: which file suffixes hold
TeX, how mathematics starts, which commands define macros, which commands cite, and
which constructs are obsolete. They are written once here, as names built on each
other, so that readers cannot drift apart. The prose tables (formatting wrappers,
labels, headings) live in ``literal_text``, which builds its key-argument table from the
citation commands below. This module imports nothing from the package and may be used
by any layer.

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

MATH_ENVIRONMENTS = (
    "equation",
    "equation*",
    "align",
    "align*",
    "displaymath",
    "math",
    "eqnarray",
    "eqnarray*",
)
# Every environment whose body is mathematics, the amsmath displays included; prose checks
# leave their bodies out.
ALL_MATH_ENVIRONMENTS = frozenset(
    {
        *MATH_ENVIRONMENTS,
        *(
            name + star
            for name in ("gather", "multline", "flalign", "alignat", "dmath", "dgroup")
            for star in ("", "*")
        ),
    }
)
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
    """Return ``names`` and their spellings with a capital first letter, such as ``Citet``."""
    names = frozenset(names)
    return names | {name[:1].upper() + name[1:] for name in names}


# Obsolete constructs, each mapped to its replacement. Keep these lists small: an entry
# is added only with a published deprecation, cited beside it. "l2tabu" is the CTAN
# guide "Obsolete commands and packages" (https://ctan.org/pkg/l2tabu), version 1.8.5.7.

# The LaTeX 2.09 font switches: each resets the other font attributes instead of
# combining with them, and none adds italic correction (l2tabu 2.1.1 and its Table 1).
OBSOLETE_FONT_SWITCHES = {
    "bf": r"\textbf{...} or \bfseries",
    "it": r"\textit{...} or \itshape",
    "rm": r"\textrm{...} or \rmfamily",
    "sf": r"\textsf{...} or \sffamily",
    "tt": r"\texttt{...} or \ttfamily",
    "sc": r"\textsc{...} or \scshape",
    "sl": r"\textsl{...} or \slshape",
}
# Plain TeX commands that bypass LaTeX's interfaces: \over breaks with amsmath's \frac
# (l2tabu 2.1.2), and \centerline is incompatible with packages such as color (2.1.3).
OBSOLETE_COMMANDS = {
    "over": r"\frac{...}{...}",
    "centerline": r"{\centering ...}",
}
# eqnarray spaces its relation symbols inconsistently and lets equation numbers overprint
# the formula; amsmath's align replaces it (l2tabu 3.3).
OBSOLETE_ENVIRONMENTS = {
    "eqnarray": "align (amsmath)",
    "eqnarray*": "align* (amsmath)",
}
# Obsolete packages, each mapped to the advice that replaces it.
OBSOLETE_PACKAGES = {
    # times sets only the text fonts, so mathematics stays in Computer Modern (l2tabu
    # 2.3.1, after psnfss2e).
    "times": "use newtxtext and newtxmath, or mathptmx",
    # Versions with different margins circulate, so the layout depends on the computer
    # (l2tabu 1.1).
    "a4wide": "use the a4paper class option, or geometry for other margins",
    # epsfig is a compatibility wrapper of graphicx, and psfig was replaced by graphicx
    # (l2tabu 2.2.2).
    "epsfig": r"use graphicx and \includegraphics",
    "psfig": r"use graphicx and \includegraphics",
    # CTAN lists subfigure as obsolete, superseded by subfig and subcaption
    # (https://ctan.org/pkg/subfigure).
    "subfigure": "use subcaption",
    # fixltx2e only issues a warning in current LaTeX releases, whose kernel includes its
    # fixes since 2015 (https://ctan.org/pkg/fixltx2e).
    "fixltx2e": "remove it, since the LaTeX kernel includes its fixes",
}
# Packages that detect their output driver themselves, so a driver option only ties the
# document to one engine (grfguide and the hyperref manual on CTAN).
DRIVER_OPTION_PACKAGES = frozenset({"graphics", "graphicx", "color", "xcolor", "hyperref"})
# The output driver options of those packages.
OUTPUT_DRIVER_OPTIONS = frozenset(
    {"pdftex", "dvips", "dvipdfm", "dvipdfmx", "xetex", "luatex", "dvisvgm", "vtex"}
)
# Packages that work only with one output driver: psfig includes PostScript through dvips.
DRIVER_BOUND_PACKAGES = frozenset({"psfig"})
