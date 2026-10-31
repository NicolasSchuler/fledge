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
from .findings import Finding
from .report import Outcome, Report, outcome_from_findings
from .review_policy import apply_reviews, has_unaccepted_blockers


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
        The outcome, blocked when a selected blocking finding has not been accepted.
    """
    selected = selected_findings(report.findings, settings.checks)
    return outcome_from_findings(
        selected, blocked=has_unaccepted_blockers(selected, settings.reporting)
    )


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
