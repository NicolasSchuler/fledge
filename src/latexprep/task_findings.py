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

from .defects import REPORT_DEFECT_SUGGESTION
from .findings import Finding
from .scheduler import TaskResult

TASK_RULE = "execution.task"
TASK_DETAIL = "task"
BLOCKED_BY_DETAIL = "blocked_by"
TRACEBACK_DETAIL = "traceback"


def blocked_task_finding(result: TaskResult) -> Finding:
    """A task skipped because a prerequisite failed; that failure reports itself."""
    return Finding(
        TASK_RULE,
        f"{result.name} was not run: {result.error}",
        severity="info",
        status="skipped",
        details={TASK_DETAIL: result.name, BLOCKED_BY_DETAIL: list(result.blocked_by)},
    )


def failed_task_finding(result: TaskResult) -> Finding:
    """A task that ran and failed, with the defect advice when it failed by a defect."""
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


def blocked_prerequisites(finding: Finding) -> tuple[str, ...]:
    """The failed prerequisites a skipped-task finding records, or none for any other finding."""
    prerequisites = finding.details.get(BLOCKED_BY_DETAIL)
    if finding.rule != TASK_RULE or finding.status != "skipped":
        return ()
    if not isinstance(prerequisites, list):
        return ()
    return tuple(str(item) for item in prerequisites)
