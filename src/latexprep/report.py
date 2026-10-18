"""The report a job produces, with its outcome, changes and sanitized serialization."""

from __future__ import annotations

import json
from dataclasses import KW_ONLY, asdict, dataclass, field
from enum import StrEnum
from typing import NotRequired, TypedDict

from .findings import REQUEST_ERROR_RULE, SEVERITIES, STATUSES, EvidenceKind, Finding
from .redaction import redact_data, scrub_local_paths

# Increases only when a JSON report field is removed, renamed or changes meaning;
# new fields may appear within a version. docs/_static/report.schema.json describes it.
REPORT_SCHEMA_VERSION = 2
REPORT_COMMANDS: frozenset[str] = frozenset(
    {"inspect", "check", "prepare", "bib", "fmt", "pdf", "doctor"}
)
# Invalid command-line syntax (BSD sysexits EX_USAGE); distinct from every outcome.
USAGE_EXIT_CODE = 64
# The scope of a report whose request was rejected before or while the job ran.
REQUEST_SCOPE = "request validation"
# The scope of a report whose job was cancelled or hit a defect before it finished.
UNFINISHED_SCOPE = "run did not finish"


class Outcome(StrEnum):
    """The overall result of a job; serialized as its string value."""

    PASSED = "passed"
    PLANNED = "planned"
    PASSED_WITH_ADVISORIES = "passed_with_advisories"
    ACCEPTED_EXCEPTIONS = "accepted_exceptions"
    BLOCKED = "blocked"
    ERROR = "error"
    CANCELLED = "cancelled"

    @property
    def exit_code(self) -> int:
        return _EXIT_CODE_BY_OUTCOME[self]


_EXIT_CODE_BY_OUTCOME: dict[Outcome, int] = {
    Outcome.PASSED: 0,
    Outcome.PLANNED: 0,
    Outcome.PASSED_WITH_ADVISORIES: 1,
    Outcome.ACCEPTED_EXCEPTIONS: 2,
    Outcome.BLOCKED: 3,
    Outcome.ERROR: 4,
    Outcome.CANCELLED: 130,
}


class ChangeKind(StrEnum):
    """The kind of edit a preparation change makes to the separate copy."""

    EXCLUDE = "exclude"
    REMOVE = "remove"
    MOVE = "move"
    REWRITE = "rewrite"
    UNWRAP = "unwrap"
    FORMAT = "format"
    BIBLIOGRAPHY_TRANSFORM = "bibliography_transform"
    NORMALIZE_DOI = "normalize_doi"
    METADATA_SANITIZATION = "metadata_sanitization"


class StageRecord(TypedDict):
    """One task, build or diagnostic step in ``Report.stages``."""

    name: str
    status: str
    command: NotRequired[list[str]]
    elapsed_seconds: NotRequired[float]
    queue_seconds: NotRequired[float]
    document: NotRequired[str]


@dataclass(frozen=True, slots=True)
class Change:
    path: str
    _: KW_ONLY
    kind: ChangeKind
    reason: str
    destination: str | None = None
    diff: str | None = None


@dataclass(slots=True)
class Report:
    command: str
    _: KW_ONLY
    outcome: Outcome = Outcome.BLOCKED
    scope: str = "source analysis"
    main: str | None = None
    findings: list[Finding] = field(default_factory=list)
    changes: list[Change] = field(default_factory=list)
    settings: dict[str, object] = field(default_factory=dict)
    tools: dict[str, object] = field(default_factory=dict)
    stages: list[StageRecord] = field(default_factory=list)
    artifacts: dict[str, str] = field(default_factory=dict)
    execution: dict[str, object] = field(default_factory=dict)
    package_contents: list[dict[str, object]] = field(default_factory=list)

    @property
    def is_request_error(self) -> bool:
        """The request was rejected, so no check result describes the project."""
        return self.outcome is Outcome.ERROR and any(
            finding.rule == REQUEST_ERROR_RULE for finding in self.findings
        )

    def redacted_copy(self) -> Report:
        """Sanitize every output field together, including settings and tool inventories."""
        values = redact_data(asdict(self))
        if not isinstance(values, dict):
            return _withheld_report(self.command, _REDACTION_LIMIT_CAUSE)
        # Artifact paths name the files a job published, which callers and scripts open,
        # so they keep their location; every other local path is generalized.
        artifacts = values.get("artifacts")
        values = scrub_local_paths(values)
        if "artifacts" in values:
            values["artifacts"] = artifacts
        try:
            # These finite, application-defined enums and registered rule IDs are
            # control data, not manuscript content (a password could be "passed").
            # Redaction returns plain strings, so the original enum members are restored;
            # a value outside an enum withholds the report.
            if self.command in REPORT_COMMANDS:
                values["command"] = self.command
            values["outcome"] = Outcome(self.outcome)
            for item, original in zip(values["findings"], self.findings, strict=True):
                if original.code is not None:
                    item["rule"] = original.rule
                if original.severity in SEVERITIES:
                    item["severity"] = original.severity
                if original.status in STATUSES:
                    item["status"] = original.status
                item["evidence_kind"] = EvidenceKind(original.evidence_kind)
                item["incomplete"] = original.incomplete
            for item, original in zip(values["changes"], self.changes, strict=True):
                item["kind"] = ChangeKind(original.kind)
            values["findings"] = [Finding(**item) for item in values["findings"]]
            values["changes"] = [Change(**item) for item in values["changes"]]
            result = Report(**values)
        except (KeyError, TypeError, ValueError) as error:
            # A short credential can also occur in a schema key. Never reconstruct
            # from raw content or throw a diagnostic containing that content; the
            # error type alone tells such a collision or defect from an oversized report.
            return _withheld_report(self.command, type(error).__name__)
        if any(
            not isinstance(stage, dict) or not isinstance(stage.get("name"), str)
            for stage in result.stages
        ):
            return _withheld_report(self.command, "invalid_stage_record")
        return result

    def sorted_findings(self) -> list[Finding]:
        return sorted(
            self.findings,
            key=lambda item: (
                item.document or "",
                item.stage or "",
                item.path or "",
                _page_order(item.page),
                item.line or 0,
                item.code or "",
                item.rule,
                item.message,
                item.status,
                item.severity,
                json.dumps(item.details, sort_keys=True, default=str),
            ),
        )

    def to_dict(self) -> dict[str, object]:
        public = self.redacted_copy()
        result: dict[str, object] = {"schema_version": REPORT_SCHEMA_VERSION, **asdict(public)}
        result["findings"] = [
            {**asdict(finding), "code": finding.code, "next_step": finding.next_step}
            for finding in public.sorted_findings()
        ]
        for finding in result["findings"]:
            # The explicit suggestion, if any, is the first choice of next_step.
            del finding["suggestion"]
        result["changes"] = [
            asdict(change)
            for change in sorted(
                public.changes,
                key=lambda item: (item.path, item.kind, item.destination or "", item.reason),
            )
        ]
        result["tools"] = dict(sorted(public.tools.items()))
        result["stages"] = sorted(public.stages, key=lambda item: str(item["name"]))
        return result

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n"


# Why a report was withheld: its size or nesting exceeded the redaction limits.
_REDACTION_LIMIT_CAUSE = "redaction_limit"


def _withheld_report(command: str, cause: str) -> Report:
    """A report without content, for output that cannot be sanitized safely.

    ``cause`` is ``redaction_limit`` or the name of the error that stopped sanitizing,
    so an oversized report and a defect are distinguishable; it never quotes content.
    """
    if cause == _REDACTION_LIMIT_CAUSE:
        message = (
            "Report cannot be safely represented within redaction limits; "
            "raw details were withheld."
        )
        suggestion = "Reduce the selected checks or input size and repeat the job."
    else:
        message = (
            f"Report could not be safely sanitized (cause: {cause}); raw details were withheld."
        )
        suggestion = (
            "Repeat the job; if this persists, report the cause shown together with the command "
            "you ran. A very short credential-like value in your settings can also trigger it."
        )
    return Report(
        command if command in REPORT_COMMANDS else "inspect",
        outcome=Outcome.ERROR,
        scope="report sanitization",
        findings=[
            Finding(
                "report.redaction_limit",
                message,
                severity="error",
                status="inconclusive",
                suggestion=suggestion,
                details={"cause": cause},
            )
        ],
    )


def outcome_from_findings(findings: list[Finding], *, blocked: bool) -> Outcome:
    """Classify a completed run: blocked, passed with open warnings, or passed."""
    if blocked:
        return Outcome.BLOCKED
    if any(item.severity == "warning" and item.status != "passed" for item in findings):
        return Outcome.PASSED_WITH_ADVISORIES
    return Outcome.PASSED


def _page_order(page: object) -> tuple[int, int, str]:
    if isinstance(page, int):
        return 0, page, ""
    if isinstance(page, str) and page.isdecimal():
        return 0, int(page), ""
    return 1, 0, str(page or "")
