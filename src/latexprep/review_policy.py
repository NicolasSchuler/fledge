"""Explicit scoped review decisions: accepted exceptions and suppressed advisories.

Review metadata is display evidence, never authorization. Gate decisions always
evaluate the explicitly supplied frozen options against the original finding.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from typing import ClassVar

from .errors import SettingsError
from .findings import DetailKey, Finding
from .option_config import POSITIVE_INTEGER, Bounds, require_records, require_text, validate_scalars
from .path_safety import is_relative_posix_path
from .report import Outcome, Report
from .rules import BY_CODE
from .text_safety import is_unsafe_character

# Explicitly enumerated presentation/content policies. New checks are ineligible
# until their evidence and safety contract have been reviewed here.
REVIEWABLE_CODES = frozenset(
    {
        "TEX001",
        "TEX002",
        "TEX003",
        "TEX004",
        "TEX101",
        "TEX102",
        "TEX103",
        "TEX104",
        "TEX105",
        "TEX106",
        "TEX107",
        "BIB006",
        "BIB007",
        "BIB102",
        "BIB103",
        "BIB104",
        "BIB105",
        "BIB106",
        "BIB107",
        "BIB108",
        "BLD005",
        "BLD006",
        "BLD007",
        "BLD103",
        "BLD104",
        "MAN001",
        "MAN002",
        "MAN003",
        "MAN004",
        "MAN005",
        "MAN006",
        "MAN007",
        "MAN008",
        "MAN009",
        "MAN010",
        "MAN011",
        "MAN012",
        "MAN013",
        "MAN014",
        "MAN015",
        "MAN016",
        "PDF001",
        "PDF101",
        "PDF102",
        "PDF103",
        "PDF104",
        "PDF105",
        "PDF201",
        "PDF202",
        "PDF203",
        "PDF204",
        "PDF205",
        "PDF206",
        "PDF207",
        "PDF208",
        "PDF209",
        "PDF212",
        "PDF213",
        "PDF214",
        "PDF215",
        "PDF216",
        "PKG101",
        "PKG102",
        "PKG103",
        "PKG104",
        "PKG105",
        "PKG106",
        "PKG107",
        "PRV001",
        "PRV002",
        "PRV003",
        "PRV004",
        "PRV005",
        "NET001",
        "NET002",
        "NET003",
        "NET004",
        "NET005",
        "NET006",
        "NET007",
        "CMP001",
        "CMP002",
        "CMP003",
        "FMT001",
    }
)
# Characters of a decision's reason, and decisions in one list.
_MAX_REASON_CHARACTERS = 4096
_MAX_REVIEW_DECISIONS = 1024
_COMPARISON_CODES = frozenset({"CMP001", "CMP002", "CMP003"})
# The stage that compares the baseline PDF with the prepared one. Only differences found
# there can be accepted as intended; the workflow tags those findings with this label.
BASELINE_VERSUS_PREPARED_STAGE = "baseline versus prepared"
_REVIEW_DIAGNOSTICS = {"report.review_unmatched", "report.review_ineligible"}


@dataclass(frozen=True)
class ReviewRule:
    """An exact diagnostic code and optional conjunctive location constraints.

    Paths are literal project-relative paths, never globs. Endpoints are inclusive;
    a start without an end selects one line/page. Missing evidence never matches a
    constrained location, and a sampled aggregate cannot establish complete scope.
    """

    FIELD_BOUNDS: ClassVar[dict[str, Bounds]] = {
        "line_start": POSITIVE_INTEGER,
        "line_end": POSITIVE_INTEGER,
        "page_start": POSITIVE_INTEGER,
        "page_end": POSITIVE_INTEGER,
    }

    code: str
    reason: str
    path: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    page_start: int | None = None
    page_end: int | None = None
    stage: str | None = None
    document: str | None = None

    def __post_init__(self) -> None:
        validate_scalars(self)
        if not isinstance(self.code, str) or self.code not in BY_CODE:
            raise SettingsError("code must be an exact registered check code", ("code",))
        require_text(
            "reason",
            self.reason,
            expected="a nonempty explanation",
            max_length=_MAX_REASON_CHARACTERS,
        )
        for name in ("path", "document"):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, str)
                or not is_relative_posix_path(value, characters="literal")
            ):
                raise SettingsError(f"{name} must be a literal safe relative path", (name,))
        require_text("stage", self.stage, expected="nonempty text", allow_none=True)
        if self.stage is not None and any(map(is_unsafe_character, self.stage)):
            raise SettingsError("stage must not contain control characters", ("stage",))
        for kind in ("line", "page"):
            start, end = getattr(self, f"{kind}_start"), getattr(self, f"{kind}_end")
            if end is not None and (start is None or end < start):
                raise SettingsError(
                    f"{kind}_end needs a {kind}_start that is not after it",
                    (f"{kind}_start", f"{kind}_end"),
                )
        if self.line_start is not None and self.path is None:
            raise SettingsError("line_start requires an exact path", ("line_start", "path"))


@dataclass(frozen=True)
class ReportingOptions:
    suppressions: tuple[ReviewRule, ...] = ()
    accepted_exceptions: tuple[ReviewRule, ...] = ()

    def __post_init__(self) -> None:
        for name in ("suppressions", "accepted_exceptions"):
            require_records(
                name,
                getattr(self, name),
                ReviewRule,
                max_items=_MAX_REVIEW_DECISIONS,
                distinct_by=lambda rule: rule,
                distinct_description="decision",
            )


def _affected_pages(finding: Finding) -> set[int] | None:
    details = finding.details
    direct = details.get(DetailKey.PAGE)
    if type(direct) is int and direct > 0:
        return {direct}
    changed = details.get(DetailKey.CHANGED_PAGES)
    if isinstance(changed, (tuple, list)) and changed:
        if all(type(value) is int and value > 0 for value in changed):
            return set(changed)
        return None
    violations = details.get(DetailKey.VIOLATIONS)
    if isinstance(violations, (tuple, list)) and violations:
        # A bounded sample cannot authorize the unlisted violations.
        count = details.get(DetailKey.VIOLATION_COUNT)
        if type(count) is not int or count != len(violations):
            return None
        pages: set[int] = set()
        for item in violations:
            value = item.get(DetailKey.PAGE) if isinstance(item, dict) else None
            if type(value) is not int or value < 1:
                return None
            pages.add(value)
        return pages
    return None


def _matches(finding: Finding, review: ReviewRule) -> bool:
    if finding.code != review.code:
        return False
    for key in (DetailKey.STAGE, DetailKey.DOCUMENT):
        value = getattr(review, key.value)
        if value is not None and finding.details.get(key) != value:
            return False
    if review.path is not None and finding.path != review.path:
        return False
    if review.line_start is not None:
        if type(finding.line) is not int or not (
            review.line_start <= finding.line <= (review.line_end or review.line_start)
        ):
            return False
    if review.page_start is not None:
        pages = _affected_pages(finding)
        if pages is None or not all(
            review.page_start <= page <= (review.page_end or review.page_start) for page in pages
        ):
            return False
    return True


def _eligible(finding: Finding, action: str) -> bool:
    if finding.code not in REVIEWABLE_CODES or finding.status != "failed":
        return False
    # Partial, unavailable or bounded evidence cannot support a waiver.
    if finding.incomplete or (action == "suppressed" and finding.severity == "error"):
        return False
    if finding.code in _COMPARISON_CODES:
        return action == "accepted_exception" and (finding.stage == BASELINE_VERSUS_PREPARED_STAGE)
    return True


def _decisions(options: ReportingOptions) -> list[tuple[str, ReviewRule]]:
    return [("accepted_exception", rule) for rule in options.accepted_exceptions] + [
        ("suppressed", rule) for rule in options.suppressions
    ]


def has_unaccepted_blockers(findings: Sequence[Finding], options: ReportingOptions) -> bool:
    """Evaluate explicit current decisions; stored review metadata grants no authority."""
    return any(
        finding.is_blocking
        and not any(
            _matches(finding, rule) and _eligible(finding, "accepted_exception")
            for rule in options.accepted_exceptions
        )
        for finding in findings
    )


def reviewed_findings(findings: Sequence[Finding], options: ReportingOptions) -> list[Finding]:
    """Add visible decisions without rewriting the original result or its evidence.

    A decision that matches no finding, or only findings it cannot change, adds a
    ``report.review_unmatched`` or ``report.review_ineligible`` diagnostic.
    """
    result: list[Finding] = []
    decisions = _decisions(options)
    matched = [False] * len(decisions)
    ineligible = [False] * len(decisions)
    for finding in findings:
        if finding.rule in _REVIEW_DIAGNOSTICS:
            continue
        details = dict(finding.details)
        details.pop(DetailKey.REVIEW, None)
        clean = replace(finding, details=details)
        applied = []
        for index, (action, decision) in enumerate(decisions):
            if not _matches(clean, decision):
                continue
            matched[index] = True
            if _eligible(clean, action):
                applied.append(
                    {"action": action, "reason": decision.reason, "scope": asdict(decision)}
                )
            else:
                ineligible[index] = True
        if applied:
            details[DetailKey.REVIEW.value] = applied
        result.append(replace(clean, details=details))
    for index, (action, decision) in enumerate(decisions):
        if matched[index] and not ineligible[index]:
            continue
        result.append(
            Finding(
                "report.review_ineligible" if ineligible[index] else "report.review_unmatched",
                "A requested review decision cannot change this result."
                if ineligible[index]
                else "No finding matches this review decision's full scope.",
                severity="warning",
                status="inconclusive",
                path=decision.path,
                line=decision.line_start,
                details={"action": action, "requested_review": asdict(decision)},
            )
        )
    return result


def apply_reviews(report: Report, options: ReportingOptions) -> Report:
    """Return a report with visible decisions, preserving the workflow's outcome.

    A caller may label a successfully completed workflow ``accepted_exceptions``.
    This function never upgrades a blocked, cancelled, incomplete or errored run.
    """
    findings = reviewed_findings(report.findings, options)
    outcome = report.outcome
    if outcome in {Outcome.PASSED, Outcome.PASSED_WITH_ADVISORIES, Outcome.ACCEPTED_EXCEPTIONS}:
        if has_unaccepted_blockers(findings, options):
            outcome = Outcome.BLOCKED
        elif any(
            _matches(finding, decision) and _eligible(finding, "accepted_exception")
            for finding in findings
            for decision in options.accepted_exceptions
        ):
            outcome = Outcome.ACCEPTED_EXCEPTIONS
        elif outcome == Outcome.PASSED and any(
            finding.severity == "warning" and finding.status != "passed" for finding in findings
        ):
            outcome = Outcome.PASSED_WITH_ADVISORIES
    return replace(report, findings=findings, outcome=outcome)


def review_marker(finding: Finding) -> str:
    """A display-only marker; never use it for acceptance or execution decisions."""
    entries = finding.details.get(DetailKey.REVIEW)
    if not isinstance(entries, (list, tuple)):
        return ""
    labels = {"suppressed": "Suppressed advisory", "accepted_exception": "Accepted exception"}
    return "; ".join(
        f"{labels[entry['action']]}: {entry['reason']}"
        for entry in entries
        if isinstance(entry, dict)
        and entry.get("action") in labels
        and isinstance(entry.get("reason"), str)
    )
