"""How a job's findings become its outcome: stage tags, blocking, check selection and review.

Every stage of a job reads and extends one ``Report`` through these functions, so the
rules for which findings count and when the job is blocked live in one place. This
module does not run checks and does not publish anything.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace

from .check_policy import selected_findings
from .config import Settings
from .findings import INTERNAL_ERROR_RULE, Finding
from .report import Outcome, Report, blocked_outcome, outcome_from_findings
from .review_policy import apply_reviews, has_unaccepted_blockers, unaccepted_blockers
from .task_findings import TRACEBACK_DETAIL, has_task_defect

# A failed document whose traceback is recorded stopped by a defect in Fledge.
_DOCUMENT_FAILURE_RULE = "package.document"


def record_findings(report: Report, findings: Iterable[Finding], stage: str) -> None:
    """Add findings to the report, each tagged with the stage that produced it.

    Args:
        report: The report whose findings are extended.
        findings: The findings to add.
        stage: The name stored under ``"stage"`` in each finding's details.
    """
    report.findings.extend(
        replace(finding, details={**finding.details, "stage": stage}) for finding in findings
    )


def is_blocked(report: Report, settings: Settings) -> bool:
    """Return whether a selected, unaccepted blocking finding stops the job.

    The report is not changed.

    Args:
        report: The report whose findings are examined.
        settings: The settings that supply check selection and review decisions.

    Returns:
        ``True`` when a selected blocking finding has not been accepted.
    """
    selected = selected_findings(report.findings, settings.checks)
    return has_unaccepted_blockers(selected, settings.reporting)


def job_outcome(report: Report, settings: Settings) -> Outcome:
    """Classify a completed run from its selected findings.

    Args:
        report: The report of the completed run.
        settings: The settings that supply check selection and review decisions.

    Returns:
        The outcome: blocked, or an error when only problems that prevented checking
        remain, when a selected blocking finding has not been accepted; otherwise passed,
        with or without advisories.
    """
    selected = selected_findings(report.findings, settings.checks)
    if blockers := unaccepted_blockers(selected, settings.reporting):
        return blocked_outcome(blockers)
    return outcome_from_findings(selected, blocked=False)


def select_and_review(report: Report, settings: Settings) -> None:
    """Apply check selection to the report once, then the configured review decisions.

    The report's findings and outcome are replaced in place.

    Args:
        report: The report to select from and review.
        settings: The settings that supply check selection and review decisions.
    """
    report.findings = selected_findings(report.findings, settings.checks)
    reviewed = apply_reviews(report, settings.reporting)
    report.findings, report.outcome = reviewed.findings, reviewed.outcome


def settled_outcome(report: Report, settings: Settings) -> Outcome:
    """Return the outcome a finished job ends with, once its last findings are recorded.

    A defect in Fledge, in the run or in one of its checks, makes it an internal error
    whatever else was found. A blocked run whose open blocking findings only say that
    Fledge could not check becomes an error; one with a paper problem stays blocked.
    Every other outcome is kept.

    Args:
        report: The finished report.
        settings: The settings that supply the review decisions.

    Returns:
        The outcome the report should carry.
    """
    if report.outcome in {Outcome.ERROR, Outcome.BLOCKED} and _has_defect(report.findings):
        return Outcome.INTERNAL_ERROR
    if report.outcome is Outcome.BLOCKED:
        return blocked_outcome(unaccepted_blockers(report.findings, settings.reporting))
    return report.outcome


def _has_defect(findings: list[Finding]) -> bool:
    """Return whether a defect in Fledge stopped the run, one of its checks or a document."""
    return has_task_defect(findings) or any(
        item.rule == INTERNAL_ERROR_RULE
        or (item.rule == _DOCUMENT_FAILURE_RULE and TRACEBACK_DETAIL in item.details)
        for item in findings
    )
