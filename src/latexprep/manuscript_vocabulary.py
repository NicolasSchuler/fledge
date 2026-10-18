"""The command and environment vocabularies the opt-in manuscript policies share.

The policies read the expanded document without executing TeX, so they need to know
which commands and environments they interpret: everything else in the body is an
unexpanded construct that may hide or generate what a policy looks for. The tables
here are built on the shared vocabularies of ``literal_text`` and ``tex_commands``
and add only the names specific to these policies: metadata and ORCID commands, layout
overrides, float environments and the structural, mathematical and layout commands
that are known not to hide content. A table used by one policy lives in that
policy's module instead.
"""

from __future__ import annotations

from .literal_text import (
    HEADING_DEPTHS,
    KNOWN_TEXT_COMMANDS,
    PACKAGE_COMMANDS,
    REFERENCE_COMMANDS,
    TEXT_WRAPPERS,
)
from .tex_commands import (
    MULTICITE_COMMANDS,
    PRINTED_CITATION_COMMANDS,
    with_capitalized_forms,
)

# Metadata field names and the commands that declare each.
METADATA_COMMANDS = {
    "title": {"title"},
    "author": {"author"},
    "affiliation": {"affiliation"},
    "email": {"email"},
    "corresponding_author": {"correspondingauthor"},
}
# Commands that print a citation, in the spellings natbib and biblatex define.
CITATION_COMMANDS = with_capitalized_forms(PRINTED_CITATION_COMMANDS | MULTICITE_COMMANDS)
# Commands that print an ORCID identifier.
ORCID_COMMANDS = {"orcid", "ORCID", "orcidlink"}
# Commands that change page layout, spacing or type size: layout override candidates.
LAYOUT_COMMANDS = {
    "newpage",
    "clearpage",
    "cleardoublepage",
    "pagebreak",
    "enlargethispage",
    "resizebox",
    "scalebox",
    "tiny",
    "scriptsize",
    "footnotesize",
    "small",
    "linespread",
    "setstretch",
    "geometry",
    "newgeometry",
    "restoregeometry",
    "fontsize",
    "baselinestretch",
}
# Page-geometry lengths; setting one to a literal value is a layout override candidate.
LENGTH_COMMANDS = {
    "textwidth",
    "textheight",
    "oddsidemargin",
    "evensidemargin",
    "topmargin",
    "headheight",
    "headsep",
    "footskip",
    "baselineskip",
    "columnsep",
    "parskip",
    "abovecaptionskip",
    "belowcaptionskip",
    "floatsep",
    "textfloatsep",
    "intextsep",
}
# Environments that float: figures and tables, with their two-column variants.
FLOAT_ENVIRONMENTS = frozenset({"figure", "figure*", "table", "table*"})
# Interpreted commands beyond the shared vocabularies (headings, citations, references,
# packages, text wrappers and non-content layout commands).
_DOCUMENT_STRUCTURE_COMMANDS = {
    "documentclass",
    "begin",
    "end",
    "endinput",
    "input",
    "include",
    "abstract",
    "keywords",
    "Description",
    "declaration",
    "caption",
    "date",
    "thanks",
    "item",
}
_NOTE_AND_LINK_COMMANDS = {
    "footnote",
    "footnotemark",
    "footnotetext",
    "url",
    "href",
    "textsuperscript",
    "textsubscript",
}
_TABLE_COMMANDS = {"multicolumn", "multirow"}
_COLOR_COMMANDS = {"color", "textcolor"}
# Widths that scale with the layout; the page-geometry lengths are in LENGTH_COMMANDS.
_RELATIVE_WIDTHS = {"linewidth", "columnwidth"}
# Symbols and operators of inline and display mathematics, whose content these
# policies do not read. The last four are the control symbols \( \) \[ \].
_MATH_COMMANDS = {
    "text",
    "mathit",
    "mathsf",
    "mathcal",
    "frac",
    "sqrt",
    "sum",
    "prod",
    "int",
    "left",
    "right",
    "leq",
    "geq",
    "times",
    "cdot",
    "ldots",
    "alpha",
    "beta",
    "gamma",
    "delta",
    "epsilon",
    "theta",
    "lambda",
    "mu",
    "pi",
    "sigma",
    "phi",
    "omega",
    "in",
    "to",
    "infty",
    "not",
    "quad",
    "qquad",
    "overline",
    "hat",
    "bar",
    "(",
    ")",
    "[",
    "]",
}
# Every command these policies interpret; anything else in the document body is
# an unexpanded construct that may hide or generate what a policy looks for.
INTERPRETED_COMMANDS = (
    set(HEADING_DEPTHS)
    | CITATION_COMMANDS
    | REFERENCE_COMMANDS
    | ORCID_COMMANDS
    | LAYOUT_COMMANDS
    | LENGTH_COMMANDS
    | PACKAGE_COMMANDS
    | TEXT_WRAPPERS
    # One canonical non-content inventory is shared with the literal text scan so
    # the two cannot disagree about which commands are merely structural.
    | KNOWN_TEXT_COMMANDS
    | {name for names in METADATA_COMMANDS.values() for name in names}
    | _DOCUMENT_STRUCTURE_COMMANDS
    | _NOTE_AND_LINK_COMMANDS
    | _TABLE_COMMANDS
    | _COLOR_COMMANDS
    | _RELATIVE_WIDTHS
    | _MATH_COMMANDS
)
# Every environment these policies interpret; any other environment in the body is an
# unexpanded construct.
INTERPRETED_ENVIRONMENTS = {
    "document",
    "abstract",
    "keywords",
    "keyword",
    "IEEEkeywords",
    "figure",
    "figure*",
    "table",
    "table*",
    "center",
    "flushleft",
    "flushright",
    "tabular",
    "tabular*",
    "tabularx",
    "array",
    "itemize",
    "enumerate",
    "description",
    "quote",
    "quotation",
    "equation",
    "equation*",
    "align",
    "align*",
    "aligned",
    "gather",
    "gather*",
    "displaymath",
    "math",
    "split",
    "cases",
    "matrix",
    "pmatrix",
    "bmatrix",
    "minipage",
    "thebibliography",
    "verbatim",
    "Verbatim",
    "lstlisting",
    "minted",
}
# Explicit counter modifications, which can renumber floats and sections.
COUNTER_CHANGES = {"setcounter", "addtocounter", "refstepcounter", "stepcounter"}
