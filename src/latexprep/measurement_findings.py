"""Findings for configured PDF measurements: three-state outcomes that never pass by omission.

A configured measurement fails on a violation, is inconclusive when its evidence is
incomplete or unavailable, and passes only when everything it needed was measured.
This module builds those findings; it does not run tools or read PDF output.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence

from .errors import PreparationError
from .findings import EvidenceKind, Finding, Severity

# Violations kept in a finding's details; the count of all of them is kept separately.
MAX_SAMPLES = 100
TOOL_FAILURE_SUGGESTION = "Resolve the tool, resource, or isolation failure and repeat this check."
EVIDENCE_LIMITATION_SUGGESTION = (
    "Resolve the stated evidence/tool limitation and repeat the configured check."
)


def unavailable_finding(
    rule: str,
    error: Exception,
    *,
    severity: Severity = "error",
    suggestion: str = EVIDENCE_LIMITATION_SUGGESTION,
    details: Mapping[str, object] | None = None,
) -> Finding:
    """Report evidence that could not be measured; never a pass.

    Args:
        rule: The rule of the finding.
        error: The error that prevented the measurement; its text is the message.
        severity: The severity of the finding.
        suggestion: What the user can do about it.
        details: The details the finding reports, copied.

    Returns:
        An inconclusive finding with incomplete evidence.
    """
    return Finding(
        rule,
        str(error),
        severity=severity,
        status="inconclusive",
        incomplete=True,
        suggestion=suggestion,
        details={} if details is None else dict(details),
    )


def measurement_finding(
    rule: str,
    message: str,
    violations: Sequence[object],
    *,
    details: Mapping[str, object] | None = None,
    incomplete: bool = False,
    severity: Severity | None = None,
    evidence_kind: EvidenceKind = EvidenceKind.DERIVED,
    total_violations: int | None = None,
) -> Finding:
    """Report a configured measurement: failed on a violation, inconclusive on incomplete evidence.

    Args:
        rule: The rule of the finding.
        message: The message of the finding.
        violations: The violations found; the first ``MAX_SAMPLES`` are kept in the details.
        details: What the finding reports about the measurement; the violations, their
            count and ``incomplete`` are added to it and win over equally named entries.
        incomplete: Whether the evidence is incomplete.
        severity: The severity whatever the outcome; advisory candidates pass one fixed
            severity. Without it a violation or incomplete evidence is an error and a
            clean measurement is informational.
        evidence_kind: The kind of evidence the measurement is.
        total_violations: The count of all violations when ``violations`` holds only some
            of them; it defaults to the length of ``violations``.

    Returns:
        The finding, failed, inconclusive or passed.
    """
    status = "failed" if violations else "inconclusive" if incomplete else "passed"
    if severity is None:
        severity = "error" if violations or incomplete else "info"
    return Finding(
        rule,
        message,
        severity=severity,
        status=status,
        evidence_kind=evidence_kind,
        incomplete=incomplete,
        details={
            **(details or {}),
            "violations": list(violations[:MAX_SAMPLES]),
            "violation_count": len(violations) if total_violations is None else total_violations,
            "incomplete": incomplete,
        },
    )


def heuristic_finding(
    rule: str,
    message: str,
    violations: Sequence[object],
    *,
    details: Mapping[str, object] | None = None,
    incomplete: bool = False,
    total_violations: int | None = None,
) -> Finding:
    """Report a heuristic candidate: a warning that informs review whatever the outcome.

    Args:
        rule: The rule of the finding.
        message: The message of the finding.
        violations: The candidates found.
        details: What the finding reports, as for :func:`measurement_finding`.
        incomplete: Whether the evidence is incomplete.
        total_violations: The count of all candidates when ``violations`` holds only some.

    Returns:
        A warning with heuristic evidence.
    """
    return measurement_finding(
        rule,
        message,
        violations,
        details=details,
        incomplete=incomplete,
        severity="warning",
        evidence_kind=EvidenceKind.HEURISTIC,
        total_violations=total_violations,
    )


async def findings_or_unavailable(
    rules: Iterable[str],
    measurement: Callable[[], Awaitable[list[Finding]]],
    *,
    suggestion: str = EVIDENCE_LIMITATION_SUGGESTION,
) -> list[Finding]:
    """Run ``measurement``, reporting each of ``rules`` as unavailable evidence if it fails.

    A measurement that cannot run or cannot be read leaves every rule it feeds without
    evidence, so none of them may report a pass. Only errors a tool or its output can
    cause (``PreparationError`` and ``OSError``) are translated; a cancellation passes
    through.

    Args:
        rules: The rules the measurement feeds.
        measurement: The measurement to await.
        suggestion: The suggestion of each unavailable finding.

    Returns:
        The findings of ``measurement``, or one unavailable finding per rule.
    """
    try:
        return await measurement()
    except (PreparationError, OSError) as error:
        return [unavailable_finding(rule, error, suggestion=suggestion) for rule in rules]
