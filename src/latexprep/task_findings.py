"""The findings that say how a scheduled task ended, and the contract readers rely on.

A task that did not run, or did not finish, is reported as one ``execution.task``
finding. Report renderers read these findings back, so the shape is a contract:

- ``skipped`` (info): the task never started because a prerequisite failed.
  ``details["task"]`` names the task and ``details["blocked_by"]`` lists the failed
  prerequisites, so a renderer can group everything one root failure prevented.
- ``inconclusive`` (error): the task ran and failed. ``details["task"]`` names it. A
  failure that is a defect in Fledge, not a problem with the input, also carries
  ``details["traceback"]``, the path-free frames, and the report-this-defect advice.

This module only builds and reads those findings; it does not decide when to report.
"""

from __future__ import annotations

from collections.abc import Iterable

from .defects import REPORT_DEFECT_SUGGESTION
from .findings import Finding
from .scheduler import TaskResult

TASK_RULE = "execution.task"
TASK_DETAIL = "task"
BLOCKED_BY_DETAIL = "blocked_by"
TRACEBACK_DETAIL = "traceback"


def blocked_task_finding(result: TaskResult) -> Finding:
    """Return the finding for a task skipped because a prerequisite failed.

    That failure reports itself, so this finding is only information.

    Args:
        result: The result of the skipped task.

    Returns:
        A skipped ``execution.task`` finding that names the task and its failed
        prerequisites.
    """
    return Finding(
        TASK_RULE,
        f"{result.name} was not run: {result.error}",
        severity="info",
        status="skipped",
        details={TASK_DETAIL: result.name, BLOCKED_BY_DETAIL: list(result.blocked_by)},
    )


def failed_task_finding(result: TaskResult) -> Finding:
    """Return the finding for a task that ran and failed.

    A task that failed by a defect also carries its traceback frames and the defect advice.

    Args:
        result: The result of the failed task.

    Returns:
        An inconclusive ``execution.task`` finding with error severity.
    """
    details: dict[str, object] = {TASK_DETAIL: result.name}
    if result.defect_frames:
        details[TRACEBACK_DETAIL] = list(result.defect_frames)
    return Finding(
        TASK_RULE,
        f"{result.name}: {result.error}",
        severity="error",
        status="inconclusive",
        suggestion=REPORT_DEFECT_SUGGESTION if result.defect_frames else None,
        details=details,
    )


def has_task_defect(findings: Iterable[Finding]) -> bool:
    """Return whether a task failed by a defect in Fledge, which makes the whole run an error."""
    return any(item.rule == TASK_RULE and TRACEBACK_DETAIL in item.details for item in findings)


def blocked_prerequisites(finding: Finding) -> tuple[str, ...]:
    """Return the failed prerequisites a skipped-task finding records.

    Args:
        finding: Any finding.

    Returns:
        The prerequisites, or an empty tuple for any other finding.
    """
    prerequisites = finding.details.get(BLOCKED_BY_DETAIL)
    if finding.rule != TASK_RULE or finding.status != "skipped":
        return ()
    if not isinstance(prerequisites, list):
        return ()
    return tuple(str(item) for item in prerequisites)
