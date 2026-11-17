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
    ACCENT_MARKS,
    HEADING_DEPTHS,
    KNOWN_TEXT_COMMANDS,
    LETTER_COMMANDS,
    PACKAGE_COMMANDS,
    REFERENCE_COMMANDS,
    TEXT_WRAPPERS,
)
from .tex_commands import (
    MULTICITE_COMMANDS,
    OBSOLETE_ENVIRONMENTS,
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
    # titlesec's heading spacing, which squeezes the space around headings.
    "titlespacing",
}
# Packages whose only purpose is a smaller margin or tighter spacing; loading one is a
# layout override candidate.
LAYOUT_PACKAGES = frozenset({"fullpage", "savetrees"})
# geometry keys that set the text area or the margins, rather than the paper alone.
GEOMETRY_AREA_KEYS = frozenset(
    {
        "margin",
        "hmargin",
        "vmargin",
        "left",
        "right",
        "top",
        "bottom",
        "inner",
        "outer",
        "lmargin",
        "rmargin",
        "tmargin",
        "bmargin",
        "textwidth",
        "textheight",
        "width",
        "height",
        "total",
        "body",
        "text",
        "scale",
        "hscale",
        "vscale",
    }
)
# caption keys whose small values squeeze captions: the space around them and their size.
CAPTION_SKIP_KEYS = frozenset({"skip", "aboveskip", "belowskip"})
CAPTION_FONT_KEYS = frozenset({"font", "labelfont", "textfont", "font+"})
# Type sizes below the footnote size of the caption text are squeezing candidates.
SMALL_TYPE_SIZES = frozenset({"tiny", "scriptsize", "footnotesize"})
# titlesec package options that tighten the space around headings.
COMPACT_HEADING_OPTIONS = frozenset({"compact"})
# Display-math spacing; a zero or negative value squeezes the lines around equations.
DISPLAY_SKIP_LENGTHS = frozenset(
    {"abovedisplayskip", "belowdisplayskip", "abovedisplayshortskip", "belowdisplayshortskip"}
)
# Float placement parameters; redefining one lets floats fill more of a page.
FLOAT_FRACTIONS = frozenset(
    {
        "topfraction",
        "bottomfraction",
        "textfraction",
        "floatpagefraction",
        "dbltopfraction",
        "dblfloatpagefraction",
    }
)
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
    # Plain TeX's fraction, obsolete in LaTeX but harmless to these policies.
    "over",
    "(",
    ")",
    "[",
    "]",
}
# More mathematics: letters, relations, operators, arrows, delimiters, symbols, fonts,
# named functions and layout inside formulas. None prints a caption, label or heading.
_MORE_MATH_COMMANDS = set(
    """
    varepsilon zeta eta vartheta iota kappa nu xi varpi varrho varsigma upsilon varphi
    chi psi Gamma Delta Theta Lambda Xi Pi Sigma Upsilon Phi Psi Omega
    le ne neq ll gg approx sim simeq cong equiv propto prec succ preceq succeq subset
    supset subseteq supseteq sqsubseteq ni notin mid parallel perp models vdash dashv
    triangleq coloneqq eqqcolon doteq asymp
    pm mp div ast star circ bullet cap cup wedge vee setminus oplus otimes odot bigcup
    bigcap bigoplus bigotimes bigwedge bigvee coprod oint partial nabla
    leftarrow rightarrow Leftarrow Rightarrow leftrightarrow Leftrightarrow
    longleftarrow longrightarrow Longleftarrow Longrightarrow longleftrightarrow
    Longleftrightarrow mapsto longmapsto uparrow downarrow Uparrow Downarrow updownarrow
    hookrightarrow hookleftarrow leadsto implies impliedby iff gets nearrow searrow
    langle rangle lceil rceil lfloor rfloor lvert rvert lVert rVert vert Vert big Big
    bigg Bigg bigl bigr Bigl Bigr biggl biggr Biggl Biggr middle
    infty emptyset varnothing forall exists nexists neg lnot top bot ell hbar Re Im
    aleph prime dagger ddagger angle triangle square Box Diamond surd flat sharp natural
    cdots vdots ddots dots colon
    mathbb mathbf mathrm mathtt mathfrak mathscr mathnormal boldsymbol bm operatorname
    max min sup inf lim liminf limsup log ln exp sin cos tan arg det dim ker Pr gcd deg
    bmod pmod
    binom tfrac dfrac cfrac nicefrac sfrac overset underset stackrel substack limits
    nolimits textstyle scriptstyle mathop mathrel mathbin mathord underbrace overbrace
    widehat widetilde tilde vec dot ddot acute grave breve check overrightarrow
    """.split()
)
# Table formatting: booktabs, colortbl, array, makecell, multirow and siunitx. They
# draw rules, colour cells and format numbers.
_TABLE_FORMATTING_COMMANDS = set(
    """
    toprule midrule bottomrule cmidrule addlinespace specialrule morecmidrules
    aboverulesep belowrulesep heavyrulewidth lightrulewidth cmidrulewidth rowcolor
    cellcolor columncolor arrayrulecolor hline cline arraybackslash tabularnewline
    extracolsep tabcolsep arraystretch extrarowheight makecell thead makegapedcells
    shortstack RaggedRight RaggedLeft Centering
    num SI si qty unit ang numrange SIrange qtyrange tablenum
    """.split()
)
# Text from common packages: acronyms and glossary entries, quotations, symbols, line
# breaking, colour definitions and counter values.
_PACKAGE_TEXT_COMMANDS = set(
    """
    ac acs acl acf acp acsp aclp acfp acsu aclu Ac Acp Acl Acf Acs
    gls Gls glspl Glspl GLS acrshort acrlong acrfull acrshortpl acrlongpl
    enquote textquote checkmark textdagger textdaggerdbl textbullet textdegree
    textregistered texttrademark textcopyright textsection textparagraph textellipsis
    textendash textemdash textasciitilde textasciicircum textbackslash textbar textless
    textgreater textunderscore S P dag ddag euro xspace texorpdfstring
    nobreak allowbreak hyphenpenalty exhyphenpenalty scshape mdseries em
    definecolor colorlet providecolor colorbox fcolorbox
    arabic roman Roman alph Alph fnsymbol
    """.split()
)
# Font switches that change no size, and characters and spaces.
_PLAIN_TEXT_COMMANDS = {
    "bfseries",
    "itshape",
    "ttfamily",
    "sffamily",
    "rmfamily",
    "upshape",
    "normalfont",
    "relax",
    "protect",
    "ignorespaces",
    "strut",
    "ensuremath",
    "centering",
    "raggedright",
    "raggedleft",
    "noindent",
    "hfill",
    "newline",
    "linebreak",
    "%",
    "&",
    "#",
    "_",
    "{",
    "}",
    "\\",
    ",",
    ";",
    "!",
    " ",
    ":",
}
# Commands that print text, mathematics or table formatting and nothing a policy reads:
# no caption, label, reference, heading, citation, metadata, input or layout change. A
# macro the project defines from these alone is interpreted too (``interpret``).
NEUTRAL_COMMANDS = frozenset(
    TEXT_WRAPPERS
    | _MATH_COMMANDS
    | _MORE_MATH_COMMANDS
    | _TABLE_COMMANDS
    | _TABLE_FORMATTING_COMMANDS
    | _PACKAGE_TEXT_COMMANDS
    | _PLAIN_TEXT_COMMANDS
    | _COLOR_COMMANDS
    | _RELATIVE_WIDTHS
    | set(ACCENT_MARKS)
    | set(LETTER_COMMANDS)
    | {"$", "@", "LaTeXe", "ldots", "dots"}
)
# Lists, whose labels name an item, and threeparttable's table notes.
LIST_ENVIRONMENTS = frozenset(
    {"itemize", "enumerate", "description", "itemize*", "enumerate*", "description*", "tablenotes"}
)
# Subfigures (subfig's \subfloat, subcaption's \subcaptionbox and \subcaption, and the
# \subfigure[caption]{body} and \subtable[caption]{body} commands of the subfigure
# package and the jmlr class): their captions and labels belong to a part of the float
# (``manuscript_float_checks``).
SUBFIGURE_COMMANDS = frozenset({"subfloat", "subcaptionbox", "subfigure", "subtable"})
SUBFIGURE_ENVIRONMENTS = frozenset({"subfigure", "subtable"})
# Environments whose body is drawing or pseudo-code commands: TikZ, pgfplots, picture
# and algorithms. Their commands draw or typeset steps; a caption, label or reference
# inside is still read.
DRAWING_ENVIRONMENTS = frozenset(
    {
        "tikzpicture",
        "pgfpicture",
        "scope",
        "axis",
        "semilogxaxis",
        "semilogyaxis",
        "loglogaxis",
        "polaraxis",
        "groupplot",
        "picture",
        "forest",
        "circuitikz",
        "algorithm",
        "algorithm*",
        "algorithmic",
    }
)
# Every command these policies interpret; anything else in the document body is
# an unexpanded construct that may hide or generate what a policy looks for.
INTERPRETED_COMMANDS = (
    set(HEADING_DEPTHS)
    | CITATION_COMMANDS
    | REFERENCE_COMMANDS
    | ORCID_COMMANDS
    | LAYOUT_COMMANDS
    | LENGTH_COMMANDS
    | DISPLAY_SKIP_LENGTHS
    | FLOAT_FRACTIONS
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
    | NEUTRAL_COMMANDS
    | SUBFIGURE_COMMANDS
    | {"captionsetup", "subcaption"}
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
    "enumerate*",
    "itemize*",
    "description*",
    "threeparttable",
    "tablenotes",
    *SUBFIGURE_ENVIRONMENTS,
    *DRAWING_ENVIRONMENTS,
    # eqnarray is obsolete, but it only sets mathematics.
    *OBSOLETE_ENVIRONMENTS,
}
# Commands that change spacing, type size or page layout and print nothing a policy
# other than the layout one reads; with ``NEUTRAL_COMMANDS``, the commands a macro may
# use and still never hide a caption, label, reference or heading.
LAYOUT_ONLY_COMMANDS = frozenset(
    LAYOUT_COMMANDS
    | LENGTH_COMMANDS
    | DISPLAY_SKIP_LENGTHS
    | FLOAT_FRACTIONS
    | {
        "fontsize",
        "selectfont",
        "setlength",
        "addtolength",
        "vspace",
        "hspace",
        "vskip",
        "hskip",
        "smallskip",
        "medskip",
        "bigskip",
        "normalsize",
        "large",
        "Large",
        "LARGE",
        "huge",
        "Huge",
        "rule",
    }
)
# Environments that lay out text, lists, tables and mathematics and nothing a policy
# reads; a project environment or macro built from them alone is interpreted.
NEUTRAL_ENVIRONMENTS = frozenset(
    {
        "center",
        "flushleft",
        "flushright",
        "minipage",
        "tabular",
        "tabular*",
        "tabularx",
        "array",
        "itemize",
        "enumerate",
        "description",
        "itemize*",
        "enumerate*",
        "description*",
        "quote",
        "quotation",
        "matrix",
        "pmatrix",
        "bmatrix",
        "cases",
        "aligned",
        "split",
    }
)
# Explicit counter modifications, which can renumber floats and sections.
COUNTER_CHANGES = {"setcounter", "addtocounter", "refstepcounter", "stepcounter"}
