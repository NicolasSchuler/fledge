"""GitHub Actions workflow annotations for a report, one escaped command per line."""

from __future__ import annotations

from .finding_view import (
    DEFAULT_RENDER_OPTIONS,
    RenderOptions,
    finding_views,
    not_run_summaries,
)
from .path_safety import is_relative_posix_path
from .report import RedactedReport, Report, redact_report
from .text_safety import escape_annotation_text


def render_ci_annotations(
    report: Report, *, options: RenderOptions = DEFAULT_RENDER_OPTIONS
) -> str:
    """Render a report as GitHub Actions annotations.

    The report is redacted first, and each untrusted finding occupies one physical line.

    Args:
        report: The report to render.
        options: The render options; ``show_passed`` also shows the hidden findings.

    Returns:
        The annotation commands, one per line.
    """
    return render_redacted_ci_annotations(redact_report(report), options=options)


def render_redacted_ci_annotations(
    public: RedactedReport, *, options: RenderOptions = DEFAULT_RENDER_OPTIONS
) -> str:
    """Render a report that was already redacted as GitHub Actions annotations.

    A finding under review or one that passed becomes a notice; the summaries of the
    checks that did not run follow as notices too.

    Args:
        public: The redacted report.
        options: The render options; ``show_passed`` also shows the hidden findings.

    Returns:
        The annotation commands, one per line.
    """
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
