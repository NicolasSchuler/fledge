"""Data-only catalogue for the style suggestions of the ``STY`` family.

Every rule is a suggestion: off unless a selector other than ``ALL`` selects its code,
family or prefix, reported in a section of its own, and never changing the outcome. The
hundreds group the suggestions: ``STY1xx`` obsolete constructs and float placement,
``STY2xx`` cross-references, ``STY3xx`` typography, ``STY4xx`` wording and ``STY5xx``
accessibility, so ``STY3`` selects every typography suggestion.
"""

from .rule_definition import Rule

_L2TABU = "https://mirrors.ctan.org/info/l2tabu/english/l2tabuen.pdf"
_LSHORT = "https://ctan.org/pkg/lshort-english"
_TAGGING = "https://latex3.github.io/tagging-project/documentation/usage-instructions"
_TESTS = "tests.test_style_suggestions."
_SCOPE = (
    " Suggestion: off unless selected by its code or by STY; the literal manuscript files "
    "are read without executing TeX."
)
_PROSE = (
    " Only prose in the document body is read: the preamble, mathematics, comments, "
    "verbatim text and the arguments of commands that hold keys or values are left out."
)

RULES = (
    Rule(
        code="STY101",
        name="style.obsolete_package",
        title="Obsolete packages and output driver options",
        description=(
            "Packages that l2tabu and their own documentation call obsolete (times, a4wide, "
            "epsfig, psfig, subfigure, fixltx2e), and output driver options such as pdftex "
            "or dvips on graphics, graphicx, color, xcolor and hyperref, or among the class "
            "options, which tie the document to one engine. When MAN018 runs, it reports "
            "the driver options and psfig instead." + _SCOPE
        ),
        tests=(_TESTS + "ObsoleteConstructTests.test_obsolete_packages_and_driver_options",),
        fix=(
            "Replace the package as the suggestion says (for example times by newtxtext and "
            "newtxmath, subfigure by subcaption), or remove the driver option."
        ),
        suggestion=True,
        reference_url=_L2TABU,
    ),
    Rule(
        code="STY102",
        name="style.obsolete_command",
        title="Obsolete commands and environments",
        description=(
            "The LaTeX 2.09 font switches \\bf, \\it, \\rm, \\sf, \\tt, \\sc and \\sl, "
            "$$ display math, the eqnarray environment and the plain TeX commands \\over "
            "and \\centerline, which l2tabu lists as obsolete in LaTeX2e." + _SCOPE
        ),
        tests=(_TESTS + "ObsoleteConstructTests.test_obsolete_commands_environments_and_dollars",),
        fix=(
            "Use \\textbf{...} or \\bfseries (and their siblings), \\[ ... \\], align, "
            "\\frac{...}{...} and \\centering instead."
        ),
        suggestion=True,
        reference_url=_L2TABU,
    ),
    Rule(
        code="STY103",
        name="style.here_only_float",
        title="Floats placed with [h] alone",
        description=(
            "A figure or table whose placement option is h alone. LaTeX changes it to ht "
            "when the float does not fit where it stands, and a float that fits nowhere "
            "then waits for the end of the chapter or document." + _SCOPE
        ),
        tests=(_TESTS + "FloatAdviceTests.test_here_only_placement",),
        fix="Allow more positions, such as [htbp], or leave the placement to LaTeX.",
        suggestion=True,
        reference_url="https://texfaq.org/FAQ-floats",
    ),
    Rule(
        code="STY104",
        name="style.center_in_float",
        title="center environments inside floats",
        description=(
            "A center environment inside a figure or table, which adds vertical space "
            "around the content where \\centering adds none (l2tabu)." + _SCOPE
        ),
        tests=(_TESTS + "FloatAdviceTests.test_center_environment_in_floats",),
        fix="Replace the center environment by \\centering at the start of the float.",
        suggestion=True,
        reference_url=_L2TABU,
    ),
    Rule(
        code="STY201",
        name="style.unused_label",
        title="Labels that are never referenced",
        description=(
            "A literal \\label that no command names. A reference built from a macro "
            "parameter makes every label with its literal prefix used, and one without a "
            "prefix stops the check. Labels referenced from another document, for example "
            "through xr, cannot be seen." + _SCOPE
        ),
        tests=(_TESTS + "ReferenceTests.test_unused_labels",),
        fix=(
            "Refer to the labelled object where the text discusses it, or remove the label "
            "if nothing refers to it."
        ),
        suggestion=True,
        reference_url="https://ctan.org/pkg/refcheck",
    ),
    Rule(
        code="STY202",
        name="style.doubled_reference_name",
        title="Reference names printed twice",
        description=(
            "A name such as Figure, Table, Section or Equation typed before \\cref, \\Cref, "
            "\\autoref or \\zcref, which print the name themselves, so the PDF reads "
            "'Figure figure 2'." + _PROSE + _SCOPE
        ),
        tests=(_TESTS + "ReferenceTests.test_doubled_reference_names",),
        fix="Remove the typed name, or keep it and use \\ref.",
        suggestion=True,
        reference_url="https://ctan.org/pkg/cleveref",
    ),
    Rule(
        code="STY203",
        name="style.equation_ref",
        title="Equations referenced with \\ref",
        description=(
            "\\ref to a label set in a math environment, which prints the equation number "
            "without the parentheses that \\eqref adds. A \\ref that the source puts in "
            "parentheses itself is not reported." + _SCOPE
        ),
        tests=(_TESTS + "ReferenceTests.test_ref_to_equation_labels",),
        fix="Use \\eqref (amsmath) for equation numbers.",
        suggestion=True,
        reference_url="https://ctan.org/pkg/amsmath",
    ),
    Rule(
        code="STY301",
        name="style.reference_tie",
        title="Missing tie before references and citations",
        description=(
            "A word followed by a breakable space and \\ref, \\eqref, \\cref or \\cite, so "
            "a line may break between them." + _PROSE + _SCOPE
        ),
        tests=(_TESTS + "TypographyTests.test_untied_references",),
        fix="Join the word and the reference with a tie: Figure~\\ref{...}, see~\\cite{...}.",
        suggestion=True,
        reference_url=_LSHORT,
    ),
    Rule(
        code="STY302",
        name="style.et_al_spacing",
        title="Sentence spacing after 'et al.'",
        description=(
            "'et al.' followed by a space and a lower-case word, a command or a "
            "parenthesis, which TeX sets as the wider space after a sentence. Nothing is "
            "reported with \\frenchspacing." + _PROSE + _SCOPE
        ),
        tests=(_TESTS + "TypographyTests.test_et_al_spacing",),
        fix="Write et al.\\ before a word and et al.~ before a citation.",
        suggestion=True,
        reference_url=_LSHORT,
    ),
    Rule(
        code="STY303",
        name="style.number_range_hyphen",
        title="Number ranges with a hyphen",
        description=(
            "Two numbers joined by a hyphen, such as 1-5 or pages 10-20, where an en dash is "
            "the convention; dates, versions and longer tokens are left out." + _PROSE + _SCOPE
        ),
        tests=(_TESTS + "TypographyTests.test_hyphen_ranges",),
        fix="Write the range with an en dash: 10--20.",
        suggestion=True,
        reference_url=_LSHORT,
    ),
    Rule(
        code="STY304",
        name="style.straight_quotes",
        title="Straight double quotes",
        description=(
            'A phrase in straight double quotes ("word"), which TeX prints as two closing '
            "quotes. Nothing is reported when csquotes makes the straight quote active."
            + _PROSE
            + _SCOPE
        ),
        tests=(_TESTS + "TypographyTests.test_straight_quotes",),
        fix="Write ``word'' or use csquotes and \\enquote{word}.",
        suggestion=True,
        reference_url=_LSHORT,
    ),
    Rule(
        code="STY305",
        name="style.ellipsis",
        title="Ellipses typed as three periods",
        description=(
            "Three periods for an ellipsis, which TeX spaces as ordinary punctuation."
            + _PROSE
            + _SCOPE
        ),
        tests=(_TESTS + "TypographyTests.test_ellipses",),
        fix="Write \\dots (or \\ldots).",
        suggestion=True,
        reference_url=_LSHORT,
    ),
    Rule(
        code="STY306",
        name="style.space_before_footnote",
        title="Space before a footnote",
        description=(
            "White space before \\footnote, which TeX prints before the footnote mark. A "
            "line that ends with % has none." + _PROSE + _SCOPE
        ),
        tests=(_TESTS + "TypographyTests.test_spaced_footnotes",),
        fix="Remove the space, or end the line before \\footnote with %.",
        suggestion=True,
        reference_url=_LSHORT,
    ),
    Rule(
        code="STY307",
        name="style.consecutive_citations",
        title="Consecutive citation commands",
        description=(
            "The same citation command twice in a row, such as \\cite{a}\\cite{b}, which "
            "gives two citations where one with both keys lets the style sort and compress "
            "them. Commands with optional notes are left out." + _SCOPE
        ),
        tests=(_TESTS + "TypographyTests.test_consecutive_citations",),
        fix="Cite both keys in one command: \\cite{a,b}.",
        suggestion=True,
        reference_url="https://ctan.org/pkg/natbib",
    ),
    Rule(
        code="STY401",
        name="style.spelling_variants",
        title="Terms spelled in two ways",
        description=(
            "Both spellings of a term from a short list in one document: behaviour and "
            "behavior, modelling and modeling, analyse and analyze, colour and color, "
            "centre and center, labelled and labeled, optimise and optimize, normalise and "
            "normalize, data set and dataset. One finding per pair, at the first use of "
            "the rarer spelling." + _PROSE + _SCOPE
        ),
        tests=(_TESTS + "WordingTests.test_spelling_variants",),
        fix="Choose one spelling, usually the one the venue's language variant uses.",
        suggestion=True,
        reference_url="https://en.wikipedia.org/wiki/American_and_British_English_spelling_differences",
    ),
    Rule(
        code="STY501",
        name="style.accessibility_metadata",
        title="Tagged PDF with a document language",
        description=(
            "No \\DocumentMetadata declaration before \\documentclass, or one without a "
            "lang key, or without tagging=on or a PDF/UA pdfstandard. These ask a current LaTeX "
            "release for a tagged PDF in which assistive technology can read the "
            "structure and the language. When MAN019 runs, it reports this instead; the "
            "declaration does not prove the PDF accessible." + _SCOPE
        ),
        tests=(_TESTS + "AccessibilityTests.test_accessibility_metadata_suggestion",),
        fix=(
            "Write \\DocumentMetadata{lang=en, pdfstandard=ua-2, tagging=on} before "
            "\\documentclass, with the document's language, and check the PDF."
        ),
        suggestion=True,
        reference_url=_TAGGING,
    ),
)
