"""GitHub Actions workflow annotations for a report, one escaped command per line."""

from __future__ import annotations

import unicodedata

from .finding_view import (
    DEFAULT_RENDER_OPTIONS,
    RenderOptions,
    finding_views,
    not_run_summaries,
    redact_report,
)
from .models import Report
from .review_policy import is_literal_relative_path


def _escape_annotation(value: str, *, property_value: bool = False) -> str:
    """Escape untrusted text for a workflow command message or property value."""
    result = value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    result = "".join(
        f"%{ord(char):02X}"
        if ord(char) < 32 or ord(char) == 127
        else f"\\u{ord(char):04x}"
        if unicodedata.category(char).startswith("C")
        else char
        for char in result
    )
    return result.replace(":", "%3A").replace(",", "%2C") if property_value else result


def render_ci_annotations(
    report: Report, *, options: RenderOptions = DEFAULT_RENDER_OPTIONS
) -> str:
    """GitHub Actions annotations; each untrusted finding occupies one physical line."""
    public = redact_report(report)
    lines = []
    for view in finding_views(public, show_hidden=options.show_passed):
        finding = view.finding
        level = (
            "notice"
            if view.review or finding.status == "passed"
            else {"error": "error", "warning": "warning"}.get(finding.severity, "notice")
        )
        fields = ["title=" + _escape_annotation(view.code, property_value=True)]
        if finding.path and is_literal_relative_path(finding.path):
            fields.append("file=" + _escape_annotation(finding.path, property_value=True))
            if type(finding.line) is int and finding.line > 0:
                fields.append(f"line={finding.line}")
        message = f"{finding.severity}/{view.status_label}: {view.message}"
        if view.evidence:
            message += f" | Evidence: {view.evidence}"
        if view.review:
            message += f" | {view.review}; original result retained"
        if view.next_step:
            message += f" | Next: {view.next_step}"
        lines.append(f"::{level} {','.join(fields)}::{_escape_annotation(message)}")
    lines.extend(
        f"::notice title=Not run::{_escape_annotation(line)}" for line in not_run_summaries(public)
    )
    return "\n".join(lines)
