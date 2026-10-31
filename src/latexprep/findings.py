"""Findings: one check's result, and the constructors for findings that no check produces.

A ``Finding`` is the unit every check, renderer and review decision shares. This module
owns its shape, the vocabulary of its ``severity`` and ``status``, and the keys of its
free-form ``details``; the report that collects findings is in :mod:`latexprep.report`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import KW_ONLY, dataclass, field
from enum import StrEnum
from typing import Literal, get_args

from .errors import RequestError
from .rules import BY_NAME, code_for

Severity = Literal["error", "warning", "info"]
Status = Literal["failed", "inconclusive", "skipped", "passed", "not_applicable"]
SEVERITIES: frozenset[str] = frozenset(get_args(Severity))
STATUSES: frozenset[str] = frozenset(get_args(Status))
# The finding rule that marks a request rejected before or while the job ran.
REQUEST_ERROR_RULE = "execution.request"


class EvidenceKind(StrEnum):
    """How directly a finding's evidence supports its conclusion.

    Attributes:
        DIRECT: Evidence read directly from file contents, bytes or tool output.
        DERIVED: A verdict derived from the literal source.
        HEURISTIC: A heuristic observation.
    """

    DIRECT = "direct"
    DERIVED = "derived"
    HEURISTIC = "heuristic"


class DetailKey(StrEnum):
    """The ``Finding.details`` keys that more than one module reads.

    ``details`` is free-form evidence that a check records for people and for the
    renderers; these keys are the part with a contract. A member equals its string, so
    a check may still write the plain string.

    Attributes:
        DOCUMENT: The document of a multi-document job that the finding belongs to.
        STAGE: The job stage that produced the finding.
        PAGE: The PDF page the finding concerns.
        INPUT_LINE: The input line a build log reported for a file it did not resolve.
        REVIEW: The review decisions applied to the finding.
        VIOLATIONS: The individual violation records.
        VIOLATION_COUNT: How many violations there are when ``violations`` lists fewer.
        CHANGED_PAGES: The pages a comparison found changed.
        DIFFERING_PAGES: The pages whose dimensions differ from the expected size.
        DIMENSIONS_PT: The page dimensions, in PDF points.
        BOUNDS_PT: Boxes as left, top, right and bottom, in PDF points.
        BBOX: A box, read when ``bounds_pt`` is absent.
    """

    DOCUMENT = "document"
    STAGE = "stage"
    PAGE = "page"
    INPUT_LINE = "input_line"
    REVIEW = "review"
    VIOLATIONS = "violations"
    VIOLATION_COUNT = "violation_count"
    CHANGED_PAGES = "changed_pages"
    DIFFERING_PAGES = "differing_pages"
    DIMENSIONS_PT = "dimensions_pt"
    BOUNDS_PT = "bounds_pt"
    BBOX = "bbox"


@dataclass(frozen=True, slots=True)
class Finding:
    """The result of one check, or an execution diagnostic that no check produces.

    ``code`` is the stable public check code derived from ``rule``, ``None`` for
    diagnostics such as ``execution.internal``. Only an error that did not pass blocks
    (``is_blocking``).

    Attributes:
        rule: The descriptive dotted name (``source.edit_marker``).
        message: What the check reports.
        severity: How much the finding matters.
        status: What happened.
        path: The file the finding concerns, if any.
        line: The line of ``path`` the finding concerns, if any.
        evidence_kind: How directly the evidence supports the conclusion.
        incomplete: The check's evidence was partial, unavailable or bounded, as the
            producer knows it; such a failure can be neither accepted nor suppressed.
        suggestion: The fix for this occurrence; ``next_step`` falls back to the rule's
            generic fix.
        details: Evidence for renderers and JSON; the keys in :class:`DetailKey` have
            shared meaning.
    """

    rule: str
    message: str
    _: KW_ONLY
    severity: Severity = "warning"
    status: Status = "failed"
    path: str | None = None
    line: int | None = None
    evidence_kind: EvidenceKind = EvidenceKind.DIRECT
    incomplete: bool = False
    suggestion: str | None = None
    details: dict[str, object] = field(default_factory=dict)

    @property
    def code(self) -> str | None:
        """The stable public check code of ``rule``, or ``None`` for an uncoded diagnostic."""
        return code_for(self.rule)

    @property
    def is_blocking(self) -> bool:
        """An error that did not pass: a failure, or a check that could not decide."""
        return self.severity == "error" and self.status in {"failed", "inconclusive", "skipped"}

    @property
    def is_scope_disclaimer(self) -> bool:
        """An informational note that a check was out of scope; shown only on request."""
        return self.severity == "info" and self.status in {"skipped", "not_applicable"}

    @property
    def document(self) -> str | None:
        """The document of a multi-document job that this finding belongs to."""
        value = self.details.get(DetailKey.DOCUMENT)
        return value if isinstance(value, str) else None

    @property
    def stage(self) -> str | None:
        """The job stage (baseline, final, archive rebuild) that produced this finding."""
        value = self.details.get(DetailKey.STAGE)
        return value if isinstance(value, str) else None

    @property
    def page(self) -> object:
        """The PDF page the finding concerns, as the check recorded it."""
        return self.details.get(DetailKey.PAGE)

    @property
    def next_step(self) -> str | None:
        """What to do next: the suggestion, a generic prompt for missing evidence, or the fix."""
        if self.suggestion:
            return self.suggestion
        if self.status in {"inconclusive", "skipped"}:
            return (
                "Resolve the missing or ambiguous evidence described above, then repeat this check."
            )
        definition = BY_NAME.get(self.rule)
        return definition.fix if definition and self.status == "failed" else None


def has_blockers(findings: list[Finding]) -> bool:
    """Return whether any finding is a blocking error."""
    return any(item.is_blocking for item in findings)


def request_error_finding(error: Exception, suggestion: str) -> Finding:
    """Describe a rejected request; a RequestError supplies its own next step.

    Args:
        error: The error that rejected the request; its text is the message.
        suggestion: The next step when ``error`` is not a ``RequestError``.

    Returns:
        An inconclusive ``execution.request`` error.
    """
    return Finding(
        REQUEST_ERROR_RULE,
        str(error),
        severity="error",
        status="inconclusive",
        suggestion=error.suggestion if isinstance(error, RequestError) else suggestion,
    )


def internal_error_finding(
    error: Exception,
    *,
    suggestion: str = "Report this defect with the command and the JSON report.",
    frames: Sequence[str] = (),
) -> Finding:
    """Describe a defect as an inconclusive ``execution.internal`` error.

    A defect must still yield a machine-readable error report and exit code.

    Args:
        error: The unexpected error; its type and text are reported.
        suggestion: The next step.
        frames: A summary of where the error was raised; see
            ``defects.summarize_traceback``.

    Returns:
        The finding.
    """
    return Finding(
        "execution.internal",
        f"Internal error ({type(error).__name__}): {error}",
        severity="error",
        status="inconclusive",
        suggestion=suggestion,
        details={"error_type": type(error).__name__, "frames": list(frames)},
    )


def cancelled_finding(message: str, suggestion: str | None = None) -> Finding:
    """Describe a run the user stopped.

    Args:
        message: What was cancelled.
        suggestion: The next step, if any.

    Returns:
        An inconclusive ``execution.cancelled`` error.
    """
    return Finding(
        "execution.cancelled",
        message,
        severity="error",
        status="inconclusive",
        suggestion=suggestion,
    )
