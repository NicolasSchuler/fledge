"""The report a job produces, with its outcome, changes and sanitized serialization.

This module owns what every command ends in: the ``Report``, its ``Outcome`` and exit
codes, the ``Change`` and ``StageRecord`` entries, and the JSON layout described by
``docs/_static/report.schema.json``. How a report is displayed is in the renderer
modules; the findings it collects are in :mod:`latexprep.findings`.
"""

from __future__ import annotations

import json
from dataclasses import KW_ONLY, asdict, dataclass, field
from enum import StrEnum
from typing import Any, Literal, NewType, NotRequired, TypedDict

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
# The scope of the content-free report that ``Report.redacted_copy`` returns in place of a
# report it cannot sanitize; publication recognises a withheld report by it.
WITHHELD_SCOPE = "report sanitization"


class Outcome(StrEnum):
    """The overall result of a job; serialized as its string value.

    Attributes:
        PASSED: Everything in the stated scope passed.
        PLANNED: A ``--dry-run`` plan; nothing was verified.
        PASSED_WITH_ADVISORIES: Required checks passed; warnings need review.
        ACCEPTED_EXCEPTIONS: Passed only because of reviewed exceptions in the settings.
        BLOCKED: A required check failed or lacked evidence.
        ERROR: An input, configuration, execution or report-writing problem, or an
            internal error.
        CANCELLED: The run was interrupted.
    """

    PASSED = "passed"
    PLANNED = "planned"
    PASSED_WITH_ADVISORIES = "passed_with_advisories"
    ACCEPTED_EXCEPTIONS = "accepted_exceptions"
    BLOCKED = "blocked"
    ERROR = "error"
    CANCELLED = "cancelled"

    @property
    def exit_code(self) -> int:
        """The process exit code of the outcome.

        0 passed or planned, 1 advisories, 2 exceptions, 3 blocked, 4 error, 130 cancelled.
        """
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
    """The kind of edit a preparation change makes to the separate copy.

    Attributes:
        EXCLUDE: A file is left out of the copy.
        REMOVE: A file is removed, such as an input merged into the main source.
        MOVE: A file moves to another path, such as into the flat layout.
        REWRITE: The text of a file is rewritten.
        UNWRAP: The single ZIP wrapper directory is removed from the project root.
        FORMAT: A source file is formatted with ``tex-fmt``.
        BIBLIOGRAPHY_TRANSFORM: Selected bibliography edits are applied.
        NORMALIZE_DOI: A DOI is normalized.
        METADATA_SANITIZATION: Metadata is sanitized.
    """

    EXCLUDE = "exclude"
    REMOVE = "remove"
    MOVE = "move"
    REWRITE = "rewrite"
    UNWRAP = "unwrap"
    FORMAT = "format"
    BIBLIOGRAPHY_TRANSFORM = "bibliography_transform"
    NORMALIZE_DOI = "normalize_doi"
    METADATA_SANITIZATION = "metadata_sanitization"


# How a stage ended. Scheduled tasks and builds report succeeded, failed, blocked or
# cancelled; setup diagnostics report passed, failed, inconclusive or skipped.
StageStatus = Literal[
    "succeeded", "failed", "blocked", "cancelled", "passed", "inconclusive", "skipped"
]


class StageRecord(TypedDict):
    """One task, build or diagnostic step in ``Report.stages``.

    Attributes:
        name: Identifies the stage.
        status: How the stage ended.
        command: The command the stage ran, if recorded.
        elapsed_seconds: How long the stage ran, in seconds.
        queue_seconds: How long the stage waited to start, in seconds.
        document: For a multi-document job, the document the stage belongs to.
    """

    name: str
    status: StageStatus
    command: NotRequired[list[str]]
    elapsed_seconds: NotRequired[float]
    queue_seconds: NotRequired[float]
    document: NotRequired[str]


@dataclass(frozen=True, slots=True)
class Change:
    """One edit a preparation makes, or would make, to the separate copy.

    The original input is never edited.

    Attributes:
        path: The source path inside the project.
        kind: The kind of edit.
        reason: Why the edit is made.
        destination: The path in the copy when the file moves.
        diff: The unified diff of a rewrite.
    """

    path: str
    _: KW_ONLY
    kind: ChangeKind
    reason: str
    destination: str | None = None
    diff: str | None = None


@dataclass(slots=True)
class Report:
    """What a command found, changed and did, as one serializable record.

    A report starts as ``Outcome.BLOCKED`` so that a run that never reaches its end
    fails closed instead of passing; whoever finishes the run sets the real outcome.
    ``settings``, ``tools`` and ``execution`` are descriptive records for people and
    scripts; renderers read the parts they need through
    :mod:`latexprep.execution_records`. Output goes through ``redacted_copy``,
    ``to_dict`` or ``to_json``, which remove credentials and generalize local paths.

    Attributes:
        command: The command that produced the report, one of ``REPORT_COMMANDS``.
        outcome: The overall result.
        scope: What the results cover.
        main: The selected main document, if any.
        findings: The findings of the run.
        changes: The edits made, or proposed, to the separate copy.
        settings: The descriptive record of the settings.
        tools: The descriptive record of the tools.
        stages: The tasks, builds and diagnostic steps.
        artifacts: The paths of the files the job published, by name.
        execution: The descriptive record of the execution.
        package_contents: The inventories of the prepared package contents.
    """

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
        """Whether the request was rejected, so no check result describes the project."""
        return (
            self.outcome is Outcome.ERROR
            and self.scope == REQUEST_SCOPE
            and any(finding.rule == REQUEST_ERROR_RULE for finding in self.findings)
        )

    def redacted_copy(self) -> Report:
        """Sanitize every output field together, including settings and tool inventories.

        Artifact paths keep their location; every other local path is generalized.

        Returns:
            The sanitized report, or a report without content that names the cause when
            the output cannot be sanitized safely.
        """
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
            result = Report(**self._with_control_data(values))
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

    def _with_control_data(self, values: dict[str, Any]) -> dict[str, Any]:
        """Restore the original enums and rule names in the redacted ``values``.

        These finite, application-defined enums and registered rule IDs are control data,
        not manuscript content (a password could be "passed"). Redaction returns plain
        strings, so the original enum members are restored; a value outside an enum
        raises, which withholds the report.

        Returns:
            ``values``, updated in place, with findings and changes as objects again.

        Raises:
            ValueError: An original value lies outside its enum.
        """
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
        return values

    def sorted_findings(self) -> list[Finding]:
        """Return the findings in a stable order that does not depend on the order checks ran in."""
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
        """Return the sanitized report in its JSON layout, with deterministic ordering."""
        return redacted_dict(redact_report(self))

    def to_json(self) -> str:
        """Return ``to_dict`` as indented UTF-8 JSON text ending in a newline."""
        return redacted_json(redact_report(self))


# A report whose output fields were redacted together; renderers are built on one so that
# a report is sanitized once per run, however many formats are written.
RedactedReport = NewType("RedactedReport", Report)


def redact_report(report: Report) -> RedactedReport:
    """Sanitize every output field once, before any of it is displayed or saved."""
    return RedactedReport(report.redacted_copy())


def redacted_dict(public: RedactedReport) -> dict[str, object]:
    """Return the JSON layout of an already sanitized report, with deterministic ordering."""
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


def redacted_json(public: RedactedReport) -> str:
    """Return ``redacted_dict`` as indented UTF-8 JSON text ending in a newline."""
    return json.dumps(redacted_dict(public), indent=2, ensure_ascii=False) + "\n"


# Why a report was withheld: its size or nesting exceeded the redaction limits.
_REDACTION_LIMIT_CAUSE = "redaction_limit"


def _withheld_report(command: str, cause: str) -> Report:
    """Build a report without content, for output that cannot be sanitized safely.

    Args:
        command: The command of the original report; one outside ``REPORT_COMMANDS``
            becomes ``inspect``.
        cause: ``redaction_limit`` or the name of the error that stopped sanitizing, so
            an oversized report and a defect are distinguishable; it never quotes
            content.

    Returns:
        An error report with one finding that names the cause.
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
    """Classify a completed run: blocked, passed with open warnings, or passed.

    Args:
        findings: The findings of the run; a warning that did not pass is open.
        blocked: Whether the run is blocked, whatever the findings say.

    Returns:
        ``Outcome.BLOCKED``, ``Outcome.PASSED_WITH_ADVISORIES`` or ``Outcome.PASSED``.
    """
    if blocked:
        return Outcome.BLOCKED
    if any(item.severity == "warning" and item.status != "passed" for item in findings):
        return Outcome.PASSED_WITH_ADVISORIES
    return Outcome.PASSED


def _page_order(page: object) -> tuple[int, int, str]:
    """Return a sort key that orders numeric pages by number before any other label."""
    if isinstance(page, int):
        return 0, page, ""
    if isinstance(page, str) and page.isdecimal():
        return 0, int(page), ""
    return 1, 0, str(page or "")
