"""Structured results shared by the core, tool adapters, and interfaces."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from .redaction import redact_data
from .rules import BY_NAME, code_for


class PreparationError(Exception):
    """An actionable input, configuration, or execution error."""


@dataclass(frozen=True)
class Finding:
    rule: str
    message: str
    severity: str = "warning"
    status: str = "failed"
    path: str | None = None
    line: int | None = None
    evidence: str = "direct"
    suggestion: str | None = None
    details: dict[str, object] = field(default_factory=dict)

    @property
    def code(self) -> str | None:
        return code_for(self.rule)

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


@dataclass(frozen=True)
class Change:
    path: str
    kind: str
    reason: str
    destination: str | None = None
    diff: str | None = None


@dataclass
class Report:
    command: str
    outcome: str = "blocked"
    scope: str = "source analysis"
    main: str | None = None
    findings: list[Finding] = field(default_factory=list)
    changes: list[Change] = field(default_factory=list)
    settings: dict[str, object] = field(default_factory=dict)
    tools: dict[str, object] = field(default_factory=dict)
    stages: list[dict[str, object]] = field(default_factory=list)
    artifacts: dict[str, str] = field(default_factory=dict)
    execution: dict[str, object] = field(default_factory=dict)

    def redacted_copy(self) -> Report:
        """Sanitize every output field together, including settings and tool inventories."""
        values = redact_data(asdict(self))

        def withheld() -> Report:
            return Report(
                "report",
                "error",
                "report sanitization",
                findings=[
                    Finding(
                        "report.redaction_limit",
                        "Report cannot be safely represented within redaction limits; "
                        "raw details were withheld.",
                        "error",
                        "inconclusive",
                        suggestion="Reduce the selected checks or input size and repeat the job.",
                    )
                ],
            )

        if not isinstance(values, dict):
            return withheld()
        try:
            # These finite, application-defined enums and registered rule IDs are
            # control data, not manuscript content (a password could be "passed").
            if self.command in {"inspect", "check", "prepare", "bib", "fmt", "pdf", "report"}:
                values["command"] = self.command
            outcomes = {
                "passed",
                "planned",
                "passed_with_advisories",
                "accepted_exceptions",
                "blocked",
                "error",
                "cancelled",
            }
            if self.outcome in outcomes:
                values["outcome"] = self.outcome
            for item, original in zip(values["findings"], self.findings, strict=True):
                if original.code is not None:
                    item["rule"] = original.rule
                if original.severity in {"error", "warning", "info"}:
                    item["severity"] = original.severity
                if original.status in {"failed", "inconclusive", "skipped", "passed"}:
                    item["status"] = original.status
            values["findings"] = [Finding(**item) for item in values["findings"]]
            values["changes"] = [Change(**item) for item in values["changes"]]
            result = Report(**values)
        except (KeyError, TypeError, ValueError):
            # A short credential can also occur in a schema key. Never reconstruct
            # from raw content or throw a diagnostic containing that content.
            return withheld()
        if result.outcome not in outcomes:
            return withheld()
        if any(
            not isinstance(stage, dict) or not isinstance(stage.get("name"), str)
            for stage in result.stages
        ):
            return withheld()
        return result

    def sorted_findings(self) -> list[Finding]:
        return sorted(
            self.findings,
            key=lambda item: (
                str(item.details.get("document", "")),
                str(item.details.get("stage", "")),
                item.path or "",
                _page_order(item.details.get("page")),
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
        result = asdict(public)
        result["findings"] = [
            {**asdict(finding), "code": finding.code, "suggestion": finding.next_step}
            for finding in public.sorted_findings()
        ]
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


def has_blockers(findings: list[Finding]) -> bool:
    return any(
        item.severity == "error" and item.status in {"failed", "inconclusive", "skipped"}
        for item in findings
    )


def _page_order(page: object) -> tuple[int, int, str]:
    if isinstance(page, int):
        return 0, page, ""
    if isinstance(page, str) and page.isdecimal():
        return 0, int(page), ""
    return 1, 0, str(page or "")
