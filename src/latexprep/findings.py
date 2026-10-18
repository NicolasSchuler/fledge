"""Findings: one check's result, and the constructors for findings that no check produces."""

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
    """How directly a finding's evidence supports its conclusion."""

    DIRECT = "direct"
    DERIVED = "derived"
    HEURISTIC = "heuristic"


@dataclass(frozen=True, slots=True)
class Finding:
    rule: str
    message: str
    _: KW_ONLY
    severity: Severity = "warning"
    status: Status = "failed"
    path: str | None = None
    line: int | None = None
    evidence_kind: EvidenceKind = EvidenceKind.DIRECT
    # The check's evidence was partial, unavailable or bounded, as the producer
    # knows it; such a failure can be neither accepted nor suppressed.
    incomplete: bool = False
    suggestion: str | None = None
    details: dict[str, object] = field(default_factory=dict)

    @property
    def code(self) -> str | None:
        return code_for(self.rule)

    @property
    def is_blocking(self) -> bool:
        """An error that did not pass: a failure, or a check that could not decide."""
        return self.severity == "error" and self.status in {"failed", "inconclusive", "skipped"}

    @property
    def document(self) -> str | None:
        value = self.details.get("document")
        return value if isinstance(value, str) else None

    @property
    def stage(self) -> str | None:
        value = self.details.get("stage")
        return value if isinstance(value, str) else None

    @property
    def page(self) -> object:
        return self.details.get("page")

    @property
    def next_step(self) -> str | None:
        if self.suggestion:
            return self.suggestion
        if self.status in {"inconclusive", "skipped"}:
            return (
                "Resolve the missing or ambiguous evidence described above, then repeat this check."
            )
        definition = BY_NAME.get(self.rule)
        return definition.fix if definition and self.status == "failed" else None


def has_blockers(findings: list[Finding]) -> bool:
    return any(item.is_blocking for item in findings)


def request_error_finding(error: Exception, suggestion: str) -> Finding:
    """Describe a rejected request; a RequestError supplies its own next step."""
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
    """A defect must still yield a machine-readable error report and exit code.

    ``frames`` summarize where it was raised; see ``defects.summarize_traceback``.
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
    return Finding(
        "execution.cancelled",
        message,
        severity="error",
        status="inconclusive",
        suggestion=suggestion,
    )
