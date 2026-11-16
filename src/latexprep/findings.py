"""Findings: one check's result, and the constructors for findings that no check produces.

A ``Finding`` is the unit every check, renderer and review decision shares. This module
owns its shape, the vocabulary of its ``severity`` and ``status``, and the keys of its
free-form ``details``; the report that collects findings is in :mod:`latexprep.report`.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import KW_ONLY, dataclass, field
from enum import StrEnum
from typing import Literal, get_args

from .errors import RequestError
from .rules import BY_NAME, OPERATIONAL_RULE_NAMES, code_for

# ``suggestion`` is optional style advice: it is reported in a section of its own and never
# changes the outcome or the exit code.
Severity = Literal["error", "warning", "info", "suggestion"]
Status = Literal["failed", "inconclusive", "skipped", "passed", "not_applicable"]
SEVERITIES: frozenset[str] = frozenset(get_args(Severity))
STATUSES: frozenset[str] = frozenset(get_args(Status))
# The finding rule that marks a request rejected before or while the job ran.
REQUEST_ERROR_RULE = "execution.request"
# The finding rule of a defect in Fledge itself.
INTERNAL_ERROR_RULE = "execution.internal"

# Results that mean Fledge could not check the paper, not that the paper needs a fix: the
# request, a missing, refused or failing tool, the sandbox, a time or resource limit, and an
# input that does not say which document to check. A blocked run whose only open blocking
# findings are these ends as ``error`` (exit code 4) instead of ``blocked`` (3).
COULD_NOT_CHECK_RULES = frozenset(
    {
        REQUEST_ERROR_RULE,
        "execution.deadline",
        "execution.resources",
        # A check or a document that stopped on an input or machine problem, such as an
        # unreadable file; a defect in Fledge is told apart by its traceback.
        "execution.task",
        "package.document",
        "build.execution_unavailable",
        "build.timeout",
        "build.output_limit",
        "build.resource_limit",
        "build.log_unavailable",
        "compare.unavailable",
        "compare.tools",
        "source.formatter_available",
        "source.format_execution",
        # The input does not select exactly one readable document to check.
        "project.main_selection",
        "source.root_ambiguous",
        "source.root_missing",
        "source.main_invalid",
        # The bibliography tool that the build needed could not be used.
        "build.bibliography_tool",
    }
)
# Diagnostics of a measurement that could not run carry this suffix, such as
# ``pdf.fonts_unavailable``; they also mean that Fledge could not check.
_UNAVAILABLE_SUFFIX = "_unavailable"
# Setup diagnostics test the tools with a built-in document and never describe a manuscript.
_SETUP_FAMILY = "doctor."
# Results that only restate another failure reported beside them: a build that exited with
# an error or left no PDF, and the dependency evidence such a build could not provide.
# Together with the ``build.<stage>`` findings of rebuild stages that did not succeed, they
# never decide on their own whether the paper needs a fix or Fledge could not check.
FOLLOW_ON_BUILD_RULES = frozenset(
    {"build.failed", "build.missing_pdf", "build.submission_dependencies"}
)
_BUILD_FAMILY = "build."
# The summary of a multi-document preparation, which fails when any document package was
# not verified. That document's own findings say why, so the summary never decides on its
# own whether the paper needs a fix or Fledge could not check.
DOCUMENT_SUMMARY_RULE = "package.independent_documents"


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
        BYTES_READ: How many bytes a scan read, which differs between a project and its
            prepared copy without changing what the scan found.
    """

    DOCUMENT = "document"
    STAGE = "stage"
    BYTES_READ = "bytes_read"
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
    def is_suggestion(self) -> bool:
        """Optional advice, which never changes the outcome or the exit code."""
        return self.severity == "suggestion"

    @property
    def prevents_checking(self) -> bool:
        """A result that says Fledge could not check something, not that the paper needs a fix.

        These are the rules in ``COULD_NOT_CHECK_RULES``, every diagnostic of a measurement
        that could not run (a rule name ending in ``_unavailable``) and every result of the
        setup diagnostics (``doctor.*``).
        """
        return (
            self.rule in COULD_NOT_CHECK_RULES
            or self.rule.endswith(_UNAVAILABLE_SUFFIX)
            or self.rule.startswith(_SETUP_FAMILY)
        )

    @property
    def is_build_follow_on(self) -> bool:
        """A build result that only restates the failure that stopped the build.

        These are the rules in ``FOLLOW_ON_BUILD_RULES`` and the ``build.<stage>`` findings
        of rebuild stages that did not succeed, the only ``build.`` names that are neither
        catalogued nor operational.
        """
        if self.rule in FOLLOW_ON_BUILD_RULES:
            return True
        return (
            self.rule.startswith(_BUILD_FAMILY)
            and self.rule not in BY_NAME
            and self.rule not in OPERATIONAL_RULE_NAMES
        )

    @property
    def is_follow_on(self) -> bool:
        """A result that only restates other results reported beside it.

        These are the build results of ``is_build_follow_on`` and the summary of a
        multi-document preparation (``DOCUMENT_SUMMARY_RULE``), whose documents' own
        findings say why a package was not verified. Neither decides on its own whether the
        paper needs a fix or Fledge could not check.
        """
        return self.is_build_follow_on or self.rule == DOCUMENT_SUMMARY_RULE

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


def stopped_build_stages(findings: Iterable[Finding]) -> frozenset[str]:
    """Return the stages whose build a tool, time or resource problem stopped.

    Such a build left a truncated log, so the other build results of its stage only
    follow from what stopped it (see :func:`follows_stopped_build`).

    Args:
        findings: The findings of a run.

    Returns:
        The stages, with an empty name for a finding without one.
    """
    return frozenset(
        item.stage or ""
        for item in findings
        if item.is_blocking and item.prevents_checking and item.rule.startswith(_BUILD_FAMILY)
    )


def follows_stopped_build(finding: Finding, stages: frozenset[str]) -> bool:
    """Return whether ``finding`` is a build result of a stage whose build was stopped.

    Args:
        finding: Any finding.
        stages: The stopped stages, as :func:`stopped_build_stages` returns them.

    Returns:
        Whether the finding only reflects the truncated log of a stopped build.
    """
    return (
        finding.rule.startswith(_BUILD_FAMILY)
        and not finding.prevents_checking
        and (finding.stage or "") in stages
    )


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
        INTERNAL_ERROR_RULE,
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
