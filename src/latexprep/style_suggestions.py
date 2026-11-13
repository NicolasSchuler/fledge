r"""Optional style suggestions about the manuscript sources: the ``STY`` family.

A suggestion is advice the author may decline. It never changes the outcome or the exit
code, it runs only when a selector other than ``ALL`` selects its code, family or prefix,
and a ``% fledge: ignore CODE`` comment silences one occurrence. This module runs the
selected checks over one expanded document and owns the smallest of them:

``style_obsolete``     obsolete packages and driver options, obsolete commands (``STY1xx``)
``style_references``   unused labels, doubled reference names, ``\ref`` to equations (``STY2xx``)
``style_typography``   ties, "et al.", ranges, quotes, ellipses, footnotes, citations (``STY3xx``)
this module            float placement (``STY103``, ``STY104``), spelling variants
                       (``STY401``) and accessibility metadata (``STY501``)

Every check reads the literal source without executing TeX, so a suggestion may be wrong
where macros change what the source means.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection

from .cancellation import cancellation_point
from .expanded_document import ExpandedDocument, environment_regions
from .findings import Finding
from .manuscript_metadata_checks import accessibility_metadata
from .manuscript_vocabulary import FLOAT_ENVIRONMENTS
from .rules import BY_CODE
from .style_obsolete import obsolete_commands, obsolete_packages
from .style_references import doubled_reference_names, equation_references, unused_labels
from .style_support import Occurrence, StyleContext, occurrence_findings
from .style_typography import (
    consecutive_citations,
    ellipses,
    et_al_spacing,
    hyphen_ranges,
    spaced_footnotes,
    straight_quotes,
    untied_references,
)

# Spelling pairs: two spellings of one term, each with its inflected forms. British and
# American spellings, and the open and closed compound, mixed in one document read as
# carelessness. Keep the list short and unambiguous: "analyses" is left out, because it
# is also the plural of "analysis" in both spellings.
_SPELLING_PAIRS = (
    ("behaviour", r"behaviours?|behavioural(?:ly)?", "behavior", r"behaviors?|behavioral(?:ly)?"),
    ("modelling", r"modell(?:ing|ed|er|ers)", "modeling", r"model(?:ing|ed|er|ers)"),
    ("analyse", r"analys(?:e|ed|ing)", "analyze", r"analyz(?:e|es|ed|ing)"),
    ("colour", r"colours?|coloured|colouring", "color", r"colors?|colored|coloring"),
    ("centre", r"centres?|centred", "center", r"centers?|centered"),
    ("labelled", r"labell(?:ed|ing)", "labeled", r"label(?:ed|ing)"),
    (
        "optimise",
        r"optimis(?:e|es|ed|ing|ation|ations|er)",
        "optimize",
        r"optimiz(?:e|es|ed|ing|ation|ations|er)",
    ),
    ("normalise", r"normalis(?:e|es|ed|ing|ation)", "normalize", r"normaliz(?:e|es|ed|ing|ation)"),
    ("data set", r"data[\s~]+sets?", "dataset", r"datasets?"),
)


def _word(pattern: str) -> re.Pattern[str]:
    """Compile a spelling pattern as a whole word, in any case, never a command name."""
    return re.compile(rf"(?<![\\\w])(?:{pattern})(?!\w)", re.IGNORECASE)


_SPELLING_PATTERNS = tuple(
    (first, _word(first_pattern), second, _word(second_pattern))
    for first, first_pattern, second, second_pattern in _SPELLING_PAIRS
)


def _here_only_floats(context: StyleContext) -> list[Occurrence]:
    r"""Find figures and tables whose placement option is ``h`` alone.

    Args:
        context: The document.

    Returns:
        One occurrence per float, at its ``\begin``.
    """
    return [
        Occurrence(
            command.start,
            f"The {command.arguments[0].value.strip()} is placed with [h] alone; LaTeX turns "
            "that into [ht] when the float does not fit, and may then move it far away.",
            suggestion="Use [htbp], or leave the placement to LaTeX.",
        )
        for command in context.document.commands
        if command.name == "begin"
        and command.arguments
        and command.arguments[0].value.strip() in FLOAT_ENVIRONMENTS
        and command.options
        and command.options[0].value.strip() == "h"
    ]


def _centered_floats(context: StyleContext) -> list[Occurrence]:
    r"""Find center environments inside figures and tables, where ``\centering`` is meant.

    Args:
        context: The document.

    Returns:
        One occurrence per center environment, at its ``\begin``.
    """
    regions, _ = environment_regions(context.document, FLOAT_ENVIRONMENTS)
    return [
        Occurrence(
            command.start,
            f"A center environment inside a {region.kind} adds vertical space above and "
            "below its content.",
            suggestion=r"Use \centering at the start of the float instead.",
        )
        for region in regions
        for command in region.commands
        if command.begins("center")
    ]


def _spelling_variants(context: StyleContext) -> list[Occurrence]:
    """Find terms spelled in two ways in one document, such as behaviour and behavior.

    Args:
        context: The document.

    Returns:
        One occurrence per pair found in both spellings, at the first use of the rarer one.
    """
    prose = context.prose
    occurrences = []
    for first, first_pattern, second, second_pattern in _SPELLING_PATTERNS:
        cancellation_point()
        first_offsets = [match.start() for match in first_pattern.finditer(prose)]
        second_offsets = [match.start() for match in second_pattern.finditer(prose)]
        if not first_offsets or not second_offsets:
            continue
        rarer = first_offsets if len(first_offsets) < len(second_offsets) else second_offsets
        occurrences.append(
            Occurrence(
                rarer[0],
                f"Both {first!r} ({len(first_offsets)}) and {second!r} "
                f"({len(second_offsets)}) occur; one spelling reads more consistently.",
                suggestion=f"Use either {first!r} or {second!r} throughout.",
                details={first: len(first_offsets), second: len(second_offsets)},
            )
        )
    return sorted(occurrences, key=lambda occurrence: occurrence.offset)


def _accessibility_metadata(context: StyleContext) -> list[Occurrence]:
    r"""Report a ``\DocumentMetadata`` declaration that lacks a language or tagging, if any.

    Args:
        context: The document.

    Returns:
        One occurrence when something is missing; none when the declaration is complete or
        could not be read.
    """
    metadata = accessibility_metadata(context.document)
    if not metadata.missing or metadata.offset is None:
        return []
    return [
        Occurrence(
            metadata.offset,
            "The document does not ask LaTeX for a tagged PDF with a language; missing: "
            + ", ".join(metadata.missing)
            + ".",
            suggestion=r"Before \documentclass, write \DocumentMetadata{lang=en, "
            "pdfstandard=ua-2, tagging=on} with the document's language, and build with a "
            "current LaTeX release.",
            details={"missing": metadata.missing},
        )
    ]


StyleCheck = Callable[[StyleContext], list[Occurrence]]
# Every style suggestion by code, in reporting order.
_CHECKS: dict[str, StyleCheck] = {
    "STY101": obsolete_packages,
    "STY102": obsolete_commands,
    "STY103": _here_only_floats,
    "STY104": _centered_floats,
    "STY201": unused_labels,
    "STY202": doubled_reference_names,
    "STY203": equation_references,
    "STY301": untied_references,
    "STY302": et_al_spacing,
    "STY303": hyphen_ranges,
    "STY304": straight_quotes,
    "STY305": ellipses,
    "STY306": spaced_footnotes,
    "STY307": consecutive_citations,
    "STY401": _spelling_variants,
    "STY501": _accessibility_metadata,
}
STYLE_CODES = tuple(_CHECKS)


def style_suggestions(
    document: ExpandedDocument,
    selected: Collection[str],
    *,
    output_drivers_checked: bool = False,
    document_metadata_checked: bool = False,
) -> list[Finding]:
    """Run the selected style suggestions over one expanded document.

    Args:
        document: The expanded document.
        selected: The codes of the suggestions to run; other codes are ignored.
        output_drivers_checked: Whether the output driver policy (``MAN018``) runs, which
            then reports driver options and psfig instead of ``STY101``.
        document_metadata_checked: Whether the accessibility metadata policy (``MAN019``)
            runs, which then replaces ``STY501``.

    Returns:
        The suggestions, by code and then in source order.
    """
    context = StyleContext(document)
    findings: list[Finding] = []
    for code, check in _CHECKS.items():
        if code not in selected or (code == "STY501" and document_metadata_checked):
            continue
        if code == "STY101":
            occurrences = obsolete_packages(context, drivers=not output_drivers_checked)
        else:
            occurrences = check(context)
        findings.extend(occurrence_findings(document, BY_CODE[code].name, occurrences))
    return findings
