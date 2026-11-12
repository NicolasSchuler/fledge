"""The compact report: short plain records for scripts and coding agents.

One physical line per finding; the full evidence stays in the JSON report. Everything
shown passes through :func:`~latexprep.terminal_report.one_line`, so no untrusted text
can add a line or a terminal control sequence.
"""

from __future__ import annotations

from .execution_records import read_execution_records
from .finding_view import (
    DEFAULT_RENDER_OPTIONS,
    FindingView,
    RenderOptions,
    change_summary,
    finding_views,
    not_run_summaries,
    result_counts,
    selection_description,
    settings_description,
    suggestion_views,
    verdict_line,
)
from .report import RedactedReport, Report, redact_report
from .terminal_report import one_line, silenced_suggestions_line


def _record(view: FindingView) -> str:
    """Write one finding as one line: code, severity and status, place, then the texts."""
    line = (
        f"{one_line(view.code)} {view.finding.severity}/{view.status_label} "
        f"{one_line(view.location)}"
    )
    if view.cause:
        line += f" | caused by: {one_line(view.cause)}"
    line += f" | {one_line(view.title)}: {one_line(view.message)}"
    if view.evidence:
        line += f" | evidence: {one_line(view.evidence)}"
    if view.next_step:
        line += f" | next: {one_line(view.next_step)}"
    if view.review:
        line += f" | review: {one_line(view.review)}"
    return line


def render_redacted_compact(
    report: RedactedReport, *, options: RenderOptions = DEFAULT_RENDER_OPTIONS
) -> str:
    """Render a report that was already redacted as short plain records.

    The first line holds the outcome, the counts and the scope, and the last the verdict.
    A result that follows from another comes right after it and says ``caused by:``;
    suggestions follow the findings after a ``suggestions:`` line.

    Args:
        report: The redacted report.
        options: Rendering choices; ``show_passed`` also lists the findings that are
            hidden by default, and ``settings_file`` names the settings file.

    Returns:
        The records joined by newlines, one physical line per finding.
    """
    lines = [f"{report.outcome} | {result_counts(report)} | scope: {one_line(report.scope)}"]
    records = read_execution_records(report.execution)
    if selection := selection_description(records.check_selection):
        lines.append(one_line(selection))
    if report.main:
        lines.append(f"document: {one_line(report.main)}")
    if settings := settings_description(report, options):
        lines.append(one_line(settings[:1].lower() + settings[1:]))
    views = finding_views(report, show_hidden=options.show_passed)
    lines.extend(_record(view) for view in views)
    lines.extend(f"not run: {one_line(line)}" for line in not_run_summaries(report))
    suggestions = suggestion_views(report, show_hidden=options.show_passed)
    if suggestions or records.silenced_suggestions:
        lines.append(
            f"suggestions: {len(suggestions)} (they never change the outcome or the exit code)"
        )
        lines.extend(_record(view) for view in suggestions)
        if records.silenced_suggestions:
            lines.append(silenced_suggestions_line(records.silenced_suggestions))
    if report.changes:
        lines.append(
            f"changes: {len(report.changes)} ({one_line(change_summary(report))}; "
            "use --json for full paths and diffs)"
        )
    for name, path in report.artifacts.items():
        lines.append(f"{one_line(name)}: {one_line(path)}")
    lines.append(f"verdict: {one_line(verdict_line(report, views))}")
    return "\n".join(lines)


def render_compact(report: Report, *, options: RenderOptions = DEFAULT_RENDER_OPTIONS) -> str:
    """Redact a report and render it as short plain records.

    One physical line per finding; the full evidence remains in the JSON report.

    Args:
        report: The report to redact and render.
        options: Rendering choices, as for :func:`render_redacted_compact`.

    Returns:
        The records joined by newlines.
    """
    return render_redacted_compact(redact_report(report), options=options)
