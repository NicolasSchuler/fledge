"""Configurable constraints on the literal declarations of one selected source graph.

The constraints are the document class and class options, forbidden packages, a required
abstract with a word range, a keyword count and required section headings. Each is decided
from the evidence ``manuscript_scan`` collects, and a constraint that the evidence cannot
settle is inconclusive, never passed. This module does not execute TeX or inventory
packages loaded by the toolchain. Counts use an explicitly documented lexical convention,
not rendered PDF words. The opt-in structure policies (allowlist, headings, metadata,
floats) are the separate ``manuscript_checks``.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, NamedTuple

from .analysed_source import AnalysedSource, reuse_or_analyse
from .cancellation import cancellation_point
from .errors import SettingsError
from .expanded_document import CONTENT_INPUT_COMMANDS, reachable_sources
from .finding_text import check_status, list_locations, more_suffix, plural
from .finding_weight import POLICY
from .findings import Finding, Status
from .literal_text import Declaration, literal_list, normalize_heading
from .manuscript_scan import (
    COUNT_METHOD,
    KEYWORD_COUNT_METHOD,
    AbstractCount,
    KeywordCount,
    LiteralHeading,
    ManuscriptScan,
    abstract_count,
    graph_reasons,
    keyword_count,
)
from .option_config import (
    NONNEGATIVE_INTEGER,
    Bounds,
    require_text,
    require_unique_strings,
    validate_scalars,
)
from .source_graph import SourceGraph


@dataclass(frozen=True)
class ManuscriptOptions:
    """The constraints a manuscript must meet; an unset value is not checked.

    Construction raises ``SettingsError`` when a value is invalid or repeated, a class or
    package entry is not a single literal name, a minimum lies above its maximum, or a
    class option is both required and forbidden.

    Attributes:
        FIELD_BOUNDS: The bounds :func:`validate_scalars` applies to the word and keyword
            counts; each must be a nonnegative integer.
        expected_document_class: The document class the manuscript must declare.
        required_class_options: The class options that must be declared.
        forbidden_class_options: The class options that must not be declared.
        forbidden_packages: The packages that must not be declared directly.
        require_abstract: Whether one nonempty abstract is required.
        abstract_min_words: The fewest source words the abstract may have.
        abstract_max_words: The most source words the abstract may have.
        keywords_min_count: The fewest keyword phrases allowed.
        keywords_max_count: The most keyword phrases allowed.
        required_sections: The headings that must be present.
    """

    # Word and keyword counts; zero is a valid limit.
    FIELD_BOUNDS: ClassVar[dict[str, Bounds]] = {
        "abstract_min_words": NONNEGATIVE_INTEGER,
        "abstract_max_words": NONNEGATIVE_INTEGER,
        "keywords_min_count": NONNEGATIVE_INTEGER,
        "keywords_max_count": NONNEGATIVE_INTEGER,
    }

    expected_document_class: str | None = None
    required_class_options: tuple[str, ...] = ()
    forbidden_class_options: tuple[str, ...] = ()
    forbidden_packages: tuple[str, ...] = ()
    require_abstract: bool = False
    abstract_min_words: int | None = None
    abstract_max_words: int | None = None
    keywords_min_count: int | None = None
    keywords_max_count: int | None = None
    required_sections: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        validate_scalars(self)
        document_class = self.expected_document_class
        require_text(
            "expected_document_class",
            document_class,
            expected="a single nonempty class name",
            allow_none=True,
        )
        if document_class is not None and literal_list(document_class) != [document_class]:
            raise SettingsError(
                "expected_document_class must be a single nonempty class name",
                ("expected_document_class",),
            )
        for name in (
            "required_class_options",
            "forbidden_class_options",
            "forbidden_packages",
            "required_sections",
        ):
            require_unique_strings(name, getattr(self, name))
        for name in ("required_class_options", "forbidden_class_options", "forbidden_packages"):
            for value in getattr(self, name):
                if literal_list(value) != [value]:
                    raise SettingsError(
                        f"{name} entry {value!r} must be a single literal name "
                        "without commas or TeX commands",
                        (name,),
                    )
        for lower_name, upper_name in (
            ("abstract_min_words", "abstract_max_words"),
            ("keywords_min_count", "keywords_max_count"),
        ):
            lower, upper = getattr(self, lower_name), getattr(self, upper_name)
            if lower is not None and upper is not None and lower > upper:
                raise SettingsError(
                    f"{lower_name} must not exceed {upper_name}", (lower_name, upper_name)
                )
        both = sorted(set(self.required_class_options) & set(self.forbidden_class_options))
        if both:
            raise SettingsError(
                f"Class option {', '.join(map(repr, both))} is in both required_class_options "
                "and forbidden_class_options",
                ("required_class_options", "forbidden_class_options"),
            )


# Offending names spelled out in a message; the details list every one.
_MAX_NAMED = 3


def _named(names: Iterable[str]) -> str:
    """List the first few distinct names, ``a, b, c (+2 more)``, or ``none``."""
    unique = list(dict.fromkeys(names))
    if not unique:
        return "none"
    return ", ".join(unique[:_MAX_NAMED]) + more_suffix(len(unique) - _MAX_NAMED)


def _limits(minimum: int | None, maximum: int | None, noun: str) -> str:
    """Describe the configured range in words: ``at most 250 words``, ``3 to 5 keywords``.

    Args:
        minimum: The lower limit, or ``None``.
        maximum: The upper limit, or ``None``.
        noun: The singular noun of what is counted.

    Returns:
        The range, or ``no limit`` when neither limit is set.
    """
    if minimum is None and maximum is None:
        return "no limit"
    if minimum is None:
        return f"at most {plural(maximum or 0, noun)}"
    if maximum is None:
        return f"at least {plural(minimum, noun)}"
    if minimum == maximum:
        return f"exactly {plural(minimum, noun)}"
    return f"{minimum} to {plural(maximum, noun)}"


class _Verdict(NamedTuple):
    """What a constraint concluded: the message, the status and the details behind it.

    Attributes:
        message: The message of the finding, before any inconclusive reason.
        status: The status of the finding.
        details: The details behind the conclusion.
    """

    message: str
    status: Status
    details: dict[str, object]


class _Gaps(NamedTuple):
    """The reasons a result is unclear, and whether they leave the evidence incomplete.

    Attributes:
        reasons: The reasons a message may quote, the graph's first.
        incomplete: Whether interpretation gaps leave the evidence incomplete.
    """

    reasons: list[str]
    incomplete: bool


@dataclass(frozen=True)
class _Evidence:
    """Everything the configured constraints are decided from.

    Attributes:
        main: The main file, which a finding names when no single item is located.
        graph_reasons: The sorted, distinct reasons the graph as a whole is unclear.
        classes: The document class declarations.
        packages: The direct package declarations.
        abstracts: The word count of each abstract.
        keywords: The phrase count of each keyword block.
        headings: The literal headings.
        sources: The sources the main file reaches.
        content_sources: The sources among them that supply manuscript content.
    """

    main: str
    graph_reasons: list[str]
    classes: list[Declaration]
    packages: list[Declaration]
    abstracts: list[AbstractCount]
    keywords: list[KeywordCount]
    headings: list[LiteralHeading]
    sources: set[str]
    content_sources: set[str]

    @property
    def class_uncertain(self) -> bool:
        """Whether the class is unclear: graph gaps, not one declaration, or an unclear one."""
        return bool(
            self.graph_reasons
            or len(self.classes) != 1
            or any(item["uncertainty"] for item in self.classes)
        )

    @property
    def abstract_uncertain(self) -> bool:
        """Whether the abstract is unclear: graph gaps, several abstracts, or an unclear one."""
        return bool(
            self.graph_reasons
            or len(self.abstracts) > 1
            or any(item.uncertainty for item in self.abstracts)
        )

    def gaps(
        self,
        item_gaps: Iterable[Sequence[str]],
        item_reasons: Iterable[Sequence[str]] | None = None,
    ) -> _Gaps:
        """Combine the graph's reasons and each item's, in order.

        Args:
            item_gaps: Each item's interpretation gaps; they alone, with the graph's
                reasons, make the evidence incomplete.
            item_reasons: What the message may quote, which can add reasons that only
                explain an inconclusive result; it defaults to ``item_gaps``.

        Returns:
            The quotable reasons, and whether the evidence is incomplete.
        """
        gap_groups = [list(group) for group in item_gaps]
        quoted = gap_groups if item_reasons is None else [list(group) for group in item_reasons]
        return _Gaps(
            [*self.graph_reasons, *(reason for group in quoted for reason in group)],
            bool([*self.graph_reasons, *(reason for group in gap_groups for reason in group)]),
        )

    def finding(
        self,
        rule: str,
        verdict: _Verdict,
        gaps: _Gaps,
        located: Sequence[tuple[str, int]] = (),
    ) -> Finding:
        """Build a constraint finding; a single located item supplies its path and line.

        Args:
            rule: The rule of the finding.
            verdict: The message, status and details; an inconclusive message gets the
                first reason appended.
            gaps: The reasons and whether the evidence is incomplete.
            located: The path and line of each relevant item; with any other number than
                one, the finding names the main file without a line.

        Returns:
            The finding, with a suggestion when it is inconclusive.
        """
        message, status, details = verdict
        if status == "inconclusive":
            reason = (
                gaps.reasons[0] if gaps.reasons else "no unique supported literal interpretation"
            )
            message += f" Inconclusive: {reason.rstrip('.')}."
        path, line = located[0] if len(located) == 1 else (self.main, None)
        return Finding(
            rule,
            message,
            severity=POLICY.severity(status),
            status=status,
            path=path,
            line=line,
            evidence_kind=POLICY.evidence_kind,
            incomplete=gaps.incomplete,
            details=details,
            # Confirmed violations use the registry's code-specific next step.
            suggestion=(
                "Resolve the ambiguous source construct described above, including input "
                "boundaries or macro-generated declarations, and repeat this check."
                if status == "inconclusive"
                else None
            ),
        )


def _where(declarations: Sequence[Declaration]) -> list[tuple[str, int]]:
    """List the path and line of each declaration."""
    return [(item["path"], item["line"]) for item in declarations]


def _uncertainty(declarations: Sequence[Declaration]) -> list[list[str]]:
    """List the interpretation gaps of each declaration."""
    return [item["uncertainty"] for item in declarations]


def _inventory_finding(evidence: _Evidence) -> Finding:
    """Report the collected literal structures and package declarations as details."""
    return Finding(
        "manuscript.inventory",
        "Collected literal manuscript structures and direct package declarations "
        "in the selected source graph.",
        severity="info",
        status="passed",
        evidence_kind=POLICY.evidence_kind,
        details={
            "sources": sorted(evidence.sources),
            "content_sources": sorted(evidence.content_sources),
            "document_classes": evidence.classes,
            "package_declarations": evidence.packages,
            "abstracts": [item.as_dict() for item in evidence.abstracts],
            "keywords": [item.as_dict() for item in evidence.keywords],
            "headings": [item.as_dict() for item in evidence.headings],
            "uncertainty": evidence.graph_reasons,
            "word_count_method": COUNT_METHOD,
            "scope": "Literal source declarations only; no TeX execution "
            "or toolchain package inventory.",
        },
    )


def _document_class_finding(evidence: _Evidence, expected: str) -> Finding:
    """Compare the single declared document class with ``expected``.

    Args:
        evidence: The collected evidence.
        expected: The document class the manuscript must declare.

    Returns:
        The ``manuscript.document_class`` finding, inconclusive when the class is unclear.
    """
    classes = evidence.classes
    actual = classes[0]["names"] if len(classes) == 1 else None
    if evidence.class_uncertain or actual is None:
        status: Status = "inconclusive"
    else:
        status = "passed" if actual == [expected] else "failed"
    return evidence.finding(
        "manuscript.document_class",
        _Verdict(
            f"Declared document class: {', '.join(actual) if actual else 'unavailable'}; "
            f"expected: {expected}.",
            status,
            {"expected": expected, "declarations": classes, "uncertainty": evidence.graph_reasons},
        ),
        evidence.gaps(_uncertainty(classes)),
        _where(classes),
    )


def _class_options_finding(evidence: _Evidence, options: ManuscriptOptions) -> Finding:
    """Check the declared class options against the required and forbidden ones.

    Args:
        evidence: The collected evidence.
        options: The constraints, which list the required and forbidden class options.

    Returns:
        The ``manuscript.class_options`` finding, inconclusive when the class is unclear.
    """
    classes = evidence.classes
    declared = classes[0]["options"] if len(classes) == 1 else None
    actual = set(declared) if isinstance(declared, list) else set()
    missing = sorted(set(options.required_class_options) - actual)
    forbidden = sorted(set(options.forbidden_class_options) & actual)
    if evidence.class_uncertain:
        status: Status = "inconclusive"
    else:
        status = "failed" if missing or forbidden else "passed"
    return evidence.finding(
        "manuscript.class_options",
        _Verdict(
            f"Missing required class options: {_named(missing)}; "
            f"forbidden class options present: {_named(forbidden)}.",
            status,
            {
                "required": list(options.required_class_options),
                "forbidden": list(options.forbidden_class_options),
                "missing": missing,
                "present_forbidden": forbidden,
                "declarations": classes,
                "uncertainty": evidence.graph_reasons,
            },
        ),
        evidence.gaps(_uncertainty(classes)),
        _where(classes),
    )


def _forbidden_packages_finding(evidence: _Evidence, forbidden: tuple[str, ...]) -> Finding:
    """Find direct declarations of forbidden packages.

    An unclear declaration is never a violation, but leaves the result incomplete.

    Args:
        evidence: The collected evidence.
        forbidden: The packages that must not be declared directly.

    Returns:
        The ``manuscript.forbidden_packages`` finding.
    """
    packages = evidence.packages
    matches = [
        item
        for item in packages
        if isinstance(item["names"], list)
        and set(item["names"]) & set(forbidden)
        and not item["uncertainty"]
    ]
    uncertain = bool(evidence.graph_reasons or any(item["uncertainty"] for item in packages))
    found = [name for item in matches for name in item["names"] or [] if name in forbidden]
    return evidence.finding(
        "manuscript.forbidden_packages",
        _Verdict(
            f"Forbidden packages declared: {_named(found)} ({list_locations(matches)})."
            if matches
            else f"No forbidden package is declared directly (forbidden: {_named(forbidden)}).",
            check_status(failed=bool(matches), incomplete=uncertain),
            {
                "forbidden": list(forbidden),
                "matches": matches,
                "declarations": packages,
                "uncertainty": evidence.graph_reasons,
                "scope": "Does not establish the transitive packages loaded by TeX.",
            },
        ),
        evidence.gaps(_uncertainty(packages)),
        _where(matches),
    )


def _abstract_gaps(evidence: _Evidence) -> _Gaps:
    """Collect the abstract gaps; unresolved macros only widen the counted range.

    They can explain an inconclusive count, but a range outside the limits is complete
    evidence of a violation, so they are quoted without making the evidence incomplete.
    """
    abstracts = evidence.abstracts
    return evidence.gaps([item.gaps for item in abstracts], [item.reasons for item in abstracts])


def _abstract_required_finding(evidence: _Evidence) -> Finding:
    """Check that exactly one abstract with nonempty literal text is declared.

    Args:
        evidence: The collected evidence.

    Returns:
        The ``manuscript.abstract_required`` finding, inconclusive when the abstract or
        its content is unclear.
    """
    abstracts = evidence.abstracts
    if evidence.abstract_uncertain or any(item.content_uncertainty for item in abstracts):
        status: Status = "inconclusive"
    elif len(abstracts) == 1 and abstracts[0].nonempty_literal_text:
        status = "passed"
    else:
        status = "failed"
    return evidence.finding(
        "manuscript.abstract_required",
        _Verdict(
            f"Found {plural(len(abstracts), 'supported literal abstract declaration')}; "
            "one nonempty abstract is required.",
            status,
            {
                "abstracts": [item.as_dict() for item in abstracts],
                "uncertainty": evidence.graph_reasons,
            },
        ),
        _abstract_gaps(evidence),
        [(item.path, item.line) for item in abstracts],
    )


def _abstract_words_finding(
    evidence: _Evidence, minimum: int | None, maximum: int | None
) -> Finding:
    """Check the abstract's source word count range against the configured limits.

    No abstract counts as zero words; several abstracts leave the count unavailable.

    Args:
        evidence: The collected evidence.
        minimum: The fewest words allowed, or ``None``.
        maximum: The most words allowed, or ``None``.

    Returns:
        The ``manuscript.abstract_words`` finding, decisive only when the range lies
        wholly inside or wholly outside the limits.
    """
    abstracts = evidence.abstracts
    # Unresolved macros bound the count instead of discarding it: a range that
    # lies wholly inside or wholly outside the configured limits is decisive.
    bounds: tuple[int, int] | None = None
    if not abstracts:
        bounds = (0, 0)
    elif len(abstracts) == 1:
        bounds = abstracts[0].word_count_range
    status: Status = "inconclusive"
    if not evidence.abstract_uncertain and bounds is not None:
        lower, upper = bounds
        inside = (minimum is None or lower >= minimum) and (maximum is None or upper <= maximum)
        outside = (minimum is not None and upper < minimum) or (
            maximum is not None and lower > maximum
        )
        if inside:
            status = "passed"
        elif outside:
            status = "failed"
    if bounds is None:
        reported = "unavailable"
    elif bounds[0] == bounds[1]:
        reported = str(bounds[0])
    else:
        reported = f"{bounds[0]} to {bounds[1]}"
    return evidence.finding(
        "manuscript.abstract_words",
        _Verdict(
            f"Abstract source word count: {reported}; "
            f"allowed: {_limits(minimum, maximum, 'word')}.",
            status,
            {
                "minimum": minimum,
                "maximum": maximum,
                "word_count_range": list(bounds) if bounds is not None else None,
                "abstracts": [item.as_dict() for item in abstracts],
                "counting_method": COUNT_METHOD,
                "uncertainty": evidence.graph_reasons,
            },
        ),
        _abstract_gaps(evidence),
        [(item.path, item.line) for item in abstracts],
    )


def _keyword_count_finding(
    evidence: _Evidence, minimum: int | None, maximum: int | None
) -> Finding:
    """Check the keyword phrase count against the configured limits.

    No keyword block counts as zero phrases; several blocks leave the result
    inconclusive.

    Args:
        evidence: The collected evidence.
        minimum: The fewest phrases allowed, or ``None``.
        maximum: The most phrases allowed, or ``None``.

    Returns:
        The ``manuscript.keyword_count`` finding.
    """
    keywords = evidence.keywords
    count = keywords[0].count if len(keywords) == 1 else 0
    uncertain = bool(
        evidence.graph_reasons or len(keywords) > 1 or any(item.uncertainty for item in keywords)
    )
    if uncertain:
        status: Status = "inconclusive"
    elif (minimum is not None and count < minimum) or (maximum is not None and count > maximum):
        status = "failed"
    else:
        status = "passed"
    return evidence.finding(
        "manuscript.keyword_count",
        _Verdict(
            f"Literal keyword count: {count}; allowed: {_limits(minimum, maximum, 'keyword')}.",
            status,
            {
                "minimum": minimum,
                "maximum": maximum,
                "count": count,
                "keywords": [item.as_dict() for item in keywords],
                "counting_method": KEYWORD_COUNT_METHOD,
                "uncertainty": evidence.graph_reasons,
            },
        ),
        evidence.gaps(item.uncertainty for item in keywords),
        [(item.path, item.line) for item in keywords],
    )


def _required_sections_finding(evidence: _Evidence, required: tuple[str, ...]) -> Finding:
    """Check that each required heading is present, matched after heading normalization.

    Args:
        evidence: The collected evidence.
        required: The headings that must be present.

    Returns:
        The ``manuscript.required_sections`` finding, which separates missing headings
        from those that could not be confirmed.
    """
    headings = evidence.headings
    present = {normalize_heading(item.heading) for item in headings if not item.uncertainty}
    # A heading with an unresolved title could carry any required name, so no
    # absence claim survives it. A resolved but conditional heading only
    # prevents a claim about its own name.
    opaque = bool(evidence.graph_reasons) or any(not item.title_resolved for item in headings)
    unresolved_names = {
        normalize_heading(item.heading)
        for item in headings
        if item.uncertainty and item.title_resolved
    }
    missing: list[str] = []
    unconfirmed: list[str] = []
    for heading in required:
        key = normalize_heading(heading)
        if key in present:
            continue
        if opaque or key in unresolved_names:
            unconfirmed.append(heading)
        else:
            missing.append(heading)
    uncertain = bool(
        unconfirmed or evidence.graph_reasons or any(item.uncertainty for item in headings)
    )
    if missing:
        message = f"Missing required literal headings: {', '.join(missing)}"
        if unconfirmed:
            message += f"; could not confirm: {', '.join(unconfirmed)}"
    elif unconfirmed:
        message = f"Could not confirm required literal headings: {', '.join(unconfirmed)}"
    elif uncertain:
        message = "Required literal headings matched an incompletely interpreted source"
    else:
        message = "All required literal headings are present"
    return evidence.finding(
        "manuscript.required_sections",
        _Verdict(
            message + "; content adequacy is not assessed.",
            check_status(failed=bool(missing), incomplete=uncertain),
            {
                "required": list(required),
                "missing": missing,
                "unconfirmed": unconfirmed,
                "headings": [item.as_dict() for item in headings],
                "matching": "Case-insensitive, collapsed whitespace, "
                "common literal formatting wrappers removed.",
                "uncertainty": evidence.graph_reasons,
            },
        ),
        evidence.gaps(item.uncertainty for item in headings),
    )


def _collect_evidence(main: str, graph: SourceGraph) -> _Evidence:
    """Scan the sources the main file reaches for the declarations the constraints need.

    Args:
        main: The main file.
        graph: The source graph of the main file.

    Returns:
        The collected evidence.
    """
    selected = reachable_sources(graph)
    # Content follows input/include/import edges. Definitions inside class/style files are
    # package evidence, not manuscript abstracts, keywords, or section headings.
    content = reachable_sources(graph, CONTENT_INPUT_COMMANDS)
    scan = ManuscriptScan(graph_reasons(graph, selected, content))
    for name in sorted(selected):
        cancellation_point()
        scan.read(name, graph.sources[name], content=name in content)
    return _Evidence(
        main=main,
        graph_reasons=sorted(set(scan.graph_reasons)),
        classes=scan.classes,
        packages=scan.packages,
        abstracts=[abstract_count(block) for block in scan.abstracts],
        keywords=[keyword_count(block) for block in scan.keywords],
        headings=scan.headings,
        sources=selected,
        content_sources=content,
    )


def check_manuscript(
    root: Path, main: str, options: ManuscriptOptions, *, analysed: AnalysedSource | None = None
) -> list[Finding]:
    """Check explicit constraints; only the selected graph supplies manuscript evidence.

    Args:
        root: The project root to analyse.
        main: The main file.
        options: The constraints to check.
        analysed: The source analysis of the same root and main file, shared with other
            checks; without one the graph is built here.

    Returns:
        The inventory finding followed by one finding per configured constraint, or an
        empty list when no constraint is configured.
    """
    if options == ManuscriptOptions():
        return []
    evidence = _collect_evidence(main, reuse_or_analyse(root, main, analysed).graph)
    findings = [_inventory_finding(evidence)]
    if options.expected_document_class is not None:
        findings.append(_document_class_finding(evidence, options.expected_document_class))
    if options.required_class_options or options.forbidden_class_options:
        findings.append(_class_options_finding(evidence, options))
    if options.forbidden_packages:
        findings.append(_forbidden_packages_finding(evidence, options.forbidden_packages))
    if options.require_abstract:
        findings.append(_abstract_required_finding(evidence))
    if options.abstract_min_words is not None or options.abstract_max_words is not None:
        findings.append(
            _abstract_words_finding(
                evidence, options.abstract_min_words, options.abstract_max_words
            )
        )
    if options.keywords_min_count is not None or options.keywords_max_count is not None:
        findings.append(
            _keyword_count_finding(evidence, options.keywords_min_count, options.keywords_max_count)
        )
    if options.required_sections:
        findings.append(_required_sections_finding(evidence, options.required_sections))
    return findings
