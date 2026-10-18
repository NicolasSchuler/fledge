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
    """Evidence that could not be measured; never a pass."""
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
    """A configured measurement: failed on a violation, inconclusive on incomplete evidence.

    Without an explicit severity a violation or incomplete evidence is an error and a
    clean measurement is informational. Advisory candidates pass one fixed severity.
    ``details`` is what the finding reports about the measurement; the violations (the
    first ``MAX_SAMPLES``), their count and ``incomplete`` are added to it and win over
    equally named entries.
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
    """A heuristic candidate: a warning that informs review whatever the outcome."""
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
    """The findings of ``measurement``, or each of ``rules`` as unavailable evidence.

    A measurement that cannot run or cannot be read leaves every rule it feeds without
    evidence, so none of them may report a pass. Only errors a tool or its output can
    cause are translated; a cancellation passes through.
    """
    try:
        return await measurement()
    except (PreparationError, OSError) as error:
        return [unavailable_finding(rule, error, suggestion=suggestion) for rule in rules]
