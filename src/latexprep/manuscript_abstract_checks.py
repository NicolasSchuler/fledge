r"""Citations and uppercase abbreviations in the abstract (``MAN003``, ``MAN004``).

An abstract is an ``abstract`` environment or an ``\abstract{...}`` declaration. The
citation policy flags citation commands inside it. The abbreviation policy lists
uppercase tokens of two or more characters and whether the first occurrence follows
spelled-out words in parentheses; that is a review candidate, never proof that an
abbreviation is adequately defined. Word counts and presence of the abstract belong
to the constraint checks in ``manuscript``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .cancellation import cancellation_point
from .expanded_document import EnvironmentRegion
from .finding_text import plural
from .finding_weight import ADVISORY
from .findings import Finding
from .literal_text import plain_text
from .manuscript_check_options import ManuscriptCheckOptions
from .manuscript_policy_kernel import CheckOutcome, Policy, Reading, listing, policy_finding
from .manuscript_vocabulary import CITATION_COMMANDS
from .tex_lexer import command_end

_CITATIONS = Policy("manuscript.abstract_citations", "Checked the abstract citation policy.")
_ABBREVIATIONS = Policy(
    "manuscript.abstract_abbreviations",
    "Checked uppercase abbreviation candidates in abstracts.",
    ADVISORY,
)
_ABBREVIATION = re.compile(r"\b[A-Z][A-Z0-9]{1,}\b")
# Expanded words directly before "(" that may define the abbreviation that follows.
_ABBREVIATION_DEFINITION = re.compile(
    r"""
    (                                     # the expanded words:
        (?:\b[A-Za-z][A-Za-z-]*\s+){1,10} # up to ten words, each followed by space,
        [A-Za-z][A-Za-z-]*                # then the last word
    )
    \s*\($                                # directly before an opening parenthesis
    """,
    re.VERBOSE,
)


def _abstract_regions(reading: Reading) -> tuple[list[EnvironmentRegion], list[str]]:
    r"""Collect abstract environments and \abstract{...} declarations, each with its own gaps.

    A declaration without an argument, or inside a group, is an ambiguous abstract
    declaration.

    Args:
        reading: The expanded document and its interpretation gaps.

    Returns:
        The abstract regions and the delimiter problems found while collecting them.
    """
    regions, reasons = reading.scoped_regions({"abstract"})
    for command in reading.document.commands:
        if command.name != "abstract":
            continue
        start = command.arguments[0].start if command.arguments else command_end(command)
        end = command.arguments[0].end if command.arguments else command_end(command)
        unknown = (
            [] if command.arguments and not command.depth else ["ambiguous abstract declaration"]
        )
        region = EnvironmentRegion("abstract", command, start, end, uncertainty=unknown)
        reading.scope(region)
        regions.append(region)
    return regions, reasons


def _abbreviation_candidates(value: str, options: ManuscriptCheckOptions) -> list[tuple[str, bool]]:
    """Find first occurrences of uppercase tokens that the policy reports, with definition status.

    Args:
        value: The plain text of one abstract.
        options: The abbreviation policy and its exceptions; the ``forbid`` policy reports
            every token, the ``define`` policy only those without a definition candidate.

    Returns:
        Each reported token with whether its first occurrence follows spelled-out words in
        parentheses, in text order; listed exceptions are skipped.
    """
    candidates = []
    seen: set[str] = set(options.abstract_abbreviation_exceptions)
    for match in _ABBREVIATION.finditer(value):
        token = match.group()
        if token in seen:
            continue
        seen.add(token)
        # This is a review candidate, never proof of a scientifically adequate definition.
        definition = _ABBREVIATION_DEFINITION.search(value[: match.start()])
        initials = "".join(word[0].upper() for word in definition[1].split()) if definition else ""
        defined = bool(
            definition and value[match.end() :].startswith(")") and initials.endswith(token)
        )
        if options.abstract_abbreviation_policy == "forbid" or not defined:
            candidates.append((token, defined))
    return candidates


@dataclass(slots=True)
class _AbstractScan:
    """What the abstracts contain that the two policies report, and what stayed unclear.

    Attributes:
        reasons: The interpretation gaps of the document graph and of the abstracts.
        citations: The located citation commands found in abstracts.
        abbreviations: The located abbreviation candidates with their definition status.
        abbreviation_names: The abbreviation candidates' tokens, in the same order.
    """

    reasons: list[str]
    citations: list[dict[str, object]] = field(default_factory=list)
    abbreviations: list[dict[str, object]] = field(default_factory=list)
    abbreviation_names: list[str] = field(default_factory=list)


def _scan_abstracts(reading: Reading, options: ManuscriptCheckOptions) -> _AbstractScan:
    """Scan every uncontested abstract for citation commands and abbreviation candidates.

    An abstract with interpretation gaps contributes only its gaps. Citation commands are
    blanked before the abbreviation candidates are read from the plain text.

    Args:
        reading: The expanded document and its interpretation gaps.
        options: The abbreviation policy and its exceptions.

    Returns:
        The citations and abbreviation candidates found, and every gap that remains.
    """
    document = reading.document
    regions, delimiter_reasons = _abstract_regions(reading)
    scan = _AbstractScan([*document.graph_uncertainty, *delimiter_reasons])
    for region in regions:
        scan.reasons.extend(region.uncertainty)
    for region in regions:
        cancellation_point()
        if region.uncertainty:
            continue
        literal = list(document.text[region.start : region.end])
        for command in document.between(region.start, region.end):
            if command.name in CITATION_COMMANDS:
                scan.citations.append(document.located(command.start, command=command.name))
                start = command.start - region.start
                end = min(command_end(command), region.end) - region.start
                literal[start:end] = [" "] * (end - start)
        value, unknown = plain_text("".join(literal))
        scan.reasons.extend(unknown)
        for token, defined in _abbreviation_candidates(value, options):
            located = document.located(
                region.command.start, abbreviation=token, definition_candidate=defined
            )
            scan.abbreviations.append(located)
            scan.abbreviation_names.append(token)
    return scan


def abstract_checks(reading: Reading, options: ManuscriptCheckOptions) -> list[Finding]:
    """Run the citation and abbreviation policies over every uncontested abstract.

    Args:
        reading: The expanded document and its interpretation gaps.
        options: ``forbid_abstract_citations`` switches on the citation policy, and
            ``abstract_abbreviation_policy`` the abbreviation policy.

    Returns:
        One finding per policy that is switched on, citations first.
    """
    if not options.forbid_abstract_citations and options.abstract_abbreviation_policy is None:
        return []
    scan = _scan_abstracts(reading, options)
    results = []
    if options.forbid_abstract_citations:
        failure = f"{plural(len(scan.citations), 'citation command')} in the abstract"
        outcome = CheckOutcome(scan.citations, scan.reasons)
        results.append(policy_finding(_CITATIONS, outcome, failure=failure))
    if options.abstract_abbreviation_policy:
        count = plural(len(scan.abbreviations), "abstract abbreviation candidate")
        details = {
            "policy": options.abstract_abbreviation_policy,
            "exceptions": list(options.abstract_abbreviation_exceptions),
            "method": "Uppercase tokens of two or more characters; first occurrence "
            "must follow expanded words in parentheses for a definition candidate. "
            "Human review required.",
        }
        results.append(
            policy_finding(
                _ABBREVIATIONS,
                CheckOutcome(scan.abbreviations, scan.reasons),
                failure=f"{count} to review",
                named=listing("Abbreviations", scan.abbreviation_names),
                details=details,
            )
        )
    return results
