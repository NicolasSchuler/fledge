"""GitHub Actions workflow annotations for a report, one escaped command per line.

Each annotation is titled with the check code and its title. A result that follows from
another one, a suggestion, a passed result and a result under review become notices, so
the errors and warnings that need a decision stand out. GitHub resolves an annotation's
file relative to the repository root, so ``RenderOptions.annotation_prefix`` is put before
the project-relative paths of the findings. A final notice carries the verdict.
"""

from __future__ import annotations

from .finding_view import (
    DEFAULT_RENDER_OPTIONS,
    FindingView,
    RenderOptions,
    finding_views,
    not_run_summaries,
    suggestion_views,
    verdict_line,
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
        options: The render options; ``show_passed`` also shows the hidden findings and
            ``annotation_prefix`` locates the project in the repository.

    Returns:
        The annotation commands, one per line.
    """
    return render_redacted_ci_annotations(redact_report(report), options=options)


def _level(view: FindingView) -> str:
    """Choose the annotation command of a finding: error, warning or notice."""
    finding = view.finding
    if view.review or view.cause or finding.status == "passed" or finding.is_suggestion:
        return "notice"
    return {"error": "error", "warning": "warning"}.get(finding.severity, "notice")


def _file_fields(view: FindingView, prefix: str) -> list[str]:
    """Return the ``file`` and ``line`` fields of a finding's annotation, if it has a file.

    Args:
        view: The finding's view.
        prefix: The project's directory relative to the repository root, or empty.

    Returns:
        The escaped fields; none when the finding names no safe relative file.
    """
    finding = view.finding
    if not finding.path or not is_relative_posix_path(finding.path, characters="literal"):
        return []
    path = f"{prefix}/{finding.path}" if prefix else finding.path
    if not is_relative_posix_path(path, characters="literal"):
        return []
    fields = ["file=" + escape_annotation_text(path, property_value=True)]
    if type(finding.line) is int and finding.line > 0:
        fields.append(f"line={finding.line}")
    return fields


def _annotation(view: FindingView, prefix: str) -> str:
    """Write the annotation command of one finding.

    Args:
        view: The finding's view.
        prefix: The project's directory relative to the repository root, or empty.

    Returns:
        The escaped annotation command, on one line.
    """
    finding = view.finding
    title = f"{view.code} {view.title}" + (" (suggestion)" if finding.is_suggestion else "")
    fields = ["title=" + escape_annotation_text(title, property_value=True)]
    fields += _file_fields(view, prefix)
    message = f"{finding.severity}/{view.status_label}: {view.message}"
    if view.cause:
        message = f"Follows from {view.cause}. {message}"
    if view.evidence:
        message += f" | Evidence: {view.evidence}"
    if view.review:
        message += f" | {view.review}; original result retained"
    if view.next_step:
        message += f" | Next: {view.next_step}"
    return f"::{_level(view)} {','.join(fields)}::{escape_annotation_text(message)}"


def render_redacted_ci_annotations(
    public: RedactedReport, *, options: RenderOptions = DEFAULT_RENDER_OPTIONS
) -> str:
    """Render a report that was already redacted as GitHub Actions annotations.

    The findings come first, each cause before what follows from it, then the checks
    that did not run, the suggestions and a closing notice with the verdict.

    Args:
        public: The redacted report.
        options: The render options; ``show_passed`` also shows the hidden findings and
            ``annotation_prefix`` locates the project in the repository.

    Returns:
        The annotation commands, one per line.
    """
    views = finding_views(public, show_hidden=options.show_passed)
    prefix = options.annotation_prefix.strip("/")
    lines = [_annotation(view, prefix) for view in views]
    lines.extend(
        f"::notice title=Not run::{escape_annotation_text(line)}"
        for line in not_run_summaries(public)
    )
    lines.extend(
        _annotation(view, prefix)
        for view in suggestion_views(public, show_hidden=options.show_passed)
    )
    lines.append(
        "::notice title="
        + escape_annotation_text(f"Fledge {public.command}", property_value=True)
        + "::"
        + escape_annotation_text(verdict_line(public, views))
    )
    return "\n".join(lines)
