"""GitHub Actions workflow annotations for a report, one escaped command per line."""

from __future__ import annotations

from .finding_view import (
    DEFAULT_RENDER_OPTIONS,
    RenderOptions,
    finding_views,
    not_run_summaries,
    redact_report,
)
from .path_safety import is_relative_posix_path
from .report import Report
from .text_safety import escape_annotation_text


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
        fields = ["title=" + escape_annotation_text(view.code, property_value=True)]
        if finding.path and is_relative_posix_path(finding.path, characters="literal"):
            fields.append("file=" + escape_annotation_text(finding.path, property_value=True))
            if type(finding.line) is int and finding.line > 0:
                fields.append(f"line={finding.line}")
        message = f"{finding.severity}/{view.status_label}: {view.message}"
        if view.evidence:
            message += f" | Evidence: {view.evidence}"
        if view.review:
            message += f" | {view.review}; original result retained"
        if view.next_step:
            message += f" | Next: {view.next_step}"
        lines.append(f"::{level} {','.join(fields)}::{escape_annotation_text(message)}")
    lines.extend(
        f"::notice title=Not run::{escape_annotation_text(line)}"
        for line in not_run_summaries(public)
    )
    return "\n".join(lines)
