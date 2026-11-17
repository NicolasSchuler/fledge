"""The Rich terminal report, and plain rendering of it for tests and embedding.

No terminal markup or control sequence from inputs reaches the terminal: every
untrusted text passes through :func:`~latexprep.text_safety.sanitize_terminal_text`.
The one-line compact report is in ``compact_report`` and the check catalogue in
``rule_catalogue_view``.
"""

from __future__ import annotations

from io import StringIO

from rich.console import Console
from rich.padding import Padding
from rich.panel import Panel
from rich.text import Text

from .coverage_hints import not_checked_summary
from .execution_records import ExecutionRecords, read_execution_records
from .finding_view import (
    ALWAYS_LISTED_RULES,
    DEFAULT_RENDER_OPTIONS,
    FindingView,
    RenderOptions,
    cancellation_line,
    change_summary,
    finding_views,
    not_run_summaries,
    result_counts,
    selection_description,
    settings_description,
    size_label,
    suggestion_views,
    verdict_line,
)
from .findings import REQUEST_ERROR_RULE, Finding
from .report import Outcome, RedactedReport, Report, redact_report
from .text_safety import sanitize_terminal_text

# Larger change sets are summarized by kind; every entry remains in the JSON report.
MAX_LISTED_CHANGES = 20
# The longest message of a follow-on result printed under its cause; JSON keeps it whole.
FOLLOW_ON_MESSAGE_CHARS = 160
# The largest compressed entries named in a package summary.
LARGEST_ENTRY_COUNT = 3
OUTCOME_STYLES: dict[Outcome, str] = {
    Outcome.PASSED: "bold green",
    Outcome.PLANNED: "bold cyan",
    Outcome.PASSED_WITH_ADVISORIES: "bold yellow",
    Outcome.ACCEPTED_EXCEPTIONS: "bold yellow",
    Outcome.BLOCKED: "bold red",
    Outcome.ERROR: "bold red",
    Outcome.INTERNAL_ERROR: "bold red",
    Outcome.CANCELLED: "bold yellow",
}


def one_line(value: str) -> str:
    r"""Sanitize ``value`` for the terminal, with line breaks and tabs shown as escapes.

    Carriage returns, line feeds and tabs become ``\r``, ``\n`` and ``\t``, so the value
    stays on one line.

    Args:
        value: The untrusted text to show.

    Returns:
        The sanitized one-line text.
    """
    return (
        sanitize_terminal_text(value).replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t")
    )


def _finding_style(finding: Finding) -> str:
    """Return the Rich style of a finding: green when it passed, else by its severity."""
    if finding.status == "passed":
        return "green"
    return {"error": "red", "warning": "yellow", "info": "cyan"}.get(finding.severity, "cyan")


def _detail(console: Console, label: str, value: str, *, style: str = "", indent: int = 2) -> None:
    """Print one indented ``label: value`` line, with ``value`` sanitized for the terminal.

    Args:
        console: The console to print on.
        label: The label, printed in cyan.
        value: The untrusted text after the label.
        style: The Rich style of the value.
        indent: How many columns the line, and each line it wraps to, is indented.
    """
    line = Text()
    line.append(f"{label}: ", style="cyan")
    line.append(sanitize_terminal_text(value), style=style)
    console.print(Padding(line, (0, 0, 0, indent), expand=False), overflow="fold")


def _where(location: str) -> Text:
    """Return the ``Where:`` line of a finding: the place as text, its stage dimmed.

    The place, a file and line, is what the reader acts on, so it keeps the weight of the
    text around it; the stage in brackets after it only says which step reported it.
    """
    place = one_line(location)
    stage = ""
    if place.endswith("]") and " [" in place:
        place, _, stage = place.rpartition(" [")
        stage = " [" + stage
    line = Text()
    line.append("Where: ", style="cyan")
    line.append(place)
    line.append(stage, style="dim")
    return line


# How the lines of a unified diff are styled, by their first characters; the file header
# comes before the added and removed lines it would otherwise match.
_DIFF_STYLES = (("+++", "bold"), ("---", "bold"), ("@@", "cyan"), ("+", "green"), ("-", "red"))


def _diff_text(diff: str) -> Text:
    """Return a unified diff with removed lines red, added lines green and hunks cyan."""
    text = Text()
    for index, line in enumerate(sanitize_terminal_text(diff.rstrip()).split("\n")):
        style = next((style for prefix, style in _DIFF_STYLES if line.startswith(prefix)), "")
        text.append("\n" if index else "")
        text.append(line, style=style)
    return text


def _measured_total(files: list[dict], field: str) -> str:
    """Return the total of one size field over package files, as a size label.

    Args:
        files: The per-file records of a package inventory.
        field: The name of the size field to add up.

    Returns:
        The size label of the total, or ``"not measured"`` when a file lacks a non-negative
        integer value for ``field``.
    """
    total = 0
    for item in files:
        value = item.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return "not measured"
        total += value
    return size_label(total)


def _package_files(package: dict[str, object]) -> list[dict]:
    """Return the per-file records of a package inventory entry that are well-formed."""
    entries = package.get("files", [])
    return [item for item in entries if isinstance(item, dict)] if isinstance(entries, list) else []


def _print_package_summary(
    package: dict[str, object],
    files: list[dict],
    status: str,
    style: str,
    console: Console,
) -> None:
    """Print the title, counts, sizes and renamed files of one package inventory.

    The largest compressed entries follow, and a note when the inventory is not verified.

    Args:
        package: The package inventory entry of the report.
        files: The well-formed per-file records of the entry.
        status: The status of the inventory.
        style: The Rich style of the status.
        console: The console to print on.
    """
    included = [item for item in files if item.get("status") == "included"]
    excluded = [item for item in files if item.get("status") == "excluded"]
    console.print()
    title = Text()
    title.append("Package contents", style="bold cyan")
    title.append(f" — {one_line(str(package.get('document', 'project')))}")
    title.append(f" [{sanitize_terminal_text(status)}]", style=style)
    console.print(title, overflow="fold")
    console.print(
        Text(
            f"  {len(included)} included, {len(excluded)} excluded; "
            f"uncompressed: {_measured_total(included, 'size_bytes')}; "
            f"compressed entries: {_measured_total(included, 'compressed_bytes')}"
        ),
        overflow="fold",
    )
    console.print(
        Text(
            f"  ZIP: {size_label(package.get('archive_bytes'))}; "
            f"ZIP overhead: {size_label(package.get('overhead_bytes'))}"
        ),
        overflow="fold",
    )
    if status != "verified":
        _detail(console, "Verification", "Package inventory has not been verified.", style=style)
    for item in included:
        if item.get("source") != item.get("path"):
            _detail(
                console, "Rename", one_line(f"{item.get('source', '')} -> {item.get('path', '')}")
            )
    contributors = sorted(
        [item for item in included if isinstance(item.get("compressed_bytes"), int)],
        key=lambda item: item["compressed_bytes"],
        reverse=True,
    )[:LARGEST_ENTRY_COUNT]
    if contributors:
        console.print(Text("  Largest compressed entries:", style="cyan"))
        for item in contributors:
            _detail(
                console,
                size_label(item["compressed_bytes"]),
                one_line(str(item.get("path", ""))),
                indent=4,
            )


def _print_package_files(files: list[dict], console: Console) -> None:
    """Print every file of a package inventory with its status, sizes and inclusion reasons.

    Args:
        files: The well-formed per-file records of a package inventory.
        console: The console to print on.
    """
    console.print(Text("  Files:", style="cyan"))
    for item in files:
        file_status = str(item.get("status", "unknown"))
        # Only the status is a label; the name and the sizes are read as text.
        text = Text("    ")
        text.append(f"[{sanitize_terminal_text(file_status)}]", style="cyan")
        text.append(" " + one_line(str(item.get("path") or item.get("source", ""))))
        if item.get("source") != item.get("path"):
            text.append(f" (source: {one_line(str(item.get('source', '')))})")
        text.append(
            f" — uncompressed {size_label(item.get('size_bytes'))}; "
            f"compressed {size_label(item.get('compressed_bytes'))}"
        )
        console.print(text, overflow="fold")
        for reason in item.get("reasons", []):
            _detail(console, "Reason", str(reason), indent=6)


def _print_package(package: dict[str, object], console: Console, *, show_files: bool) -> None:
    """Print one package inventory: its summary, and its files when they are asked for.

    Without ``show_files``, a hint to use ``--show-files`` follows a non-empty inventory.

    Args:
        package: The package inventory entry of the report.
        console: The console to print on.
        show_files: Whether to list every file.
    """
    files = _package_files(package)
    status = str(package.get("status", "incomplete"))
    style = "green" if status == "verified" else "yellow" if status == "incomplete" else "cyan"
    _print_package_summary(package, files, status, style, console)
    if show_files:
        _print_package_files(files, console)
    elif files:
        console.print(Text("  Use --show-files for every file and inclusion reason.", style="dim"))


def _print_package_contents(report: RedactedReport, console: Console, *, show_files: bool) -> None:
    """Print every package inventory of the report.

    Args:
        report: The redacted report.
        console: The console to print on.
        show_files: Whether to list every file of each package.
    """
    for package in report.package_contents:
        _print_package(package, console, show_files=show_files)


def _print_summary(
    report: RedactedReport, records: ExecutionRecords, options: RenderOptions, console: Console
) -> None:
    """Print the outcome panel, the check selection, the selected document and the settings.

    Args:
        report: The redacted report.
        records: The execution records of the report.
        options: The rendering options, which may name the settings file.
        console: The console to print on.
    """
    style = OUTCOME_STYLES[report.outcome]
    # Only the outcome carries its color: the scope and the counts are read as text, and a
    # dimmed color is hard to read on dark and on light backgrounds alike.
    summary = Text()
    summary.append(report.outcome.replace("_", " ").capitalize(), style=style)
    summary.append(f" — {one_line(report.scope)}")
    summary.append(f"\n{result_counts(report)}", style="dim")
    console.print(Panel(summary, border_style=style, padding=(0, 1), expand=False))
    if selection := selection_description(records.check_selection):
        console.print(Text(sanitize_terminal_text(selection), style="dim"))
    if report.main:
        console.print(Text(f"Document: {one_line(report.main)}"))
    if settings := settings_description(report, options):
        console.print(Text(one_line(settings), style="dim"))


def _print_heading(view: FindingView, console: Console) -> None:
    """Print the code, title and ``[SEVERITY / status]`` of a finding as one line."""
    color = _finding_style(view.finding)
    heading = Text()
    heading.append(one_line(f"{view.code}  {view.title}"), style=f"bold {color}")
    heading.append(f"  [{view.finding.severity.upper()} / {view.status_label}]", style=color)
    console.print(heading)


def _print_view(view: FindingView, console: Console) -> None:
    """Print one finding in full: heading, place, message, evidence, next step and review."""
    console.print()
    _print_heading(view, console)
    if view.location:
        console.print(Padding(_where(view.location), (0, 0, 0, 2), expand=False), overflow="fold")
    console.print(Padding(Text(sanitize_terminal_text(view.message)), (0, 0, 0, 2), expand=False))
    if view.evidence:
        _detail(console, "Evidence", one_line(view.evidence))
    if view.next_step:
        _detail(console, "Next", view.next_step)
    if view.review:
        _detail(console, "Review", view.review, style="yellow")


def _print_follow_ons(views: list[FindingView], console: Console) -> None:
    """Print the results that follow from the finding above them, two short lines each.

    Args:
        views: The follow-on views of one cause, in display order.
        console: The console to print on.
    """
    console.print(
        Padding(
            Text("Follow-on results, which usually clear once this is fixed:", style="cyan"),
            (0, 0, 0, 2),
        )
    )
    for view in views:
        color = _finding_style(view.finding)
        heading = Text()
        heading.append(one_line(f"{view.code}  {view.title}"), style=color)
        heading.append(f"  [{view.finding.severity.upper()} / {view.status_label}]", style=color)
        console.print(Padding(heading, (0, 0, 0, 4)), overflow="fold")
        detail = Text()
        detail.append(f"{one_line(view.location)}: ", style="dim")
        detail.append(_clipped_message(view.message))
        console.print(Padding(detail, (0, 0, 0, 6)), overflow="fold")


def _clipped_message(message: str) -> str:
    """Shorten a follow-on's message to one line of at most ``FOLLOW_ON_MESSAGE_CHARS``."""
    text = one_line(message)
    return (
        text if len(text) <= FOLLOW_ON_MESSAGE_CHARS else text[: FOLLOW_ON_MESSAGE_CHARS - 1] + "…"
    )


def _print_findings(report: RedactedReport, views: list[FindingView], console: Console) -> None:
    """Print each finding with its follow-ons, then what could not run or finish.

    A note that no issues were found follows when every view is of an always-listed rule.

    Args:
        report: The redacted report.
        views: The finding views to print, each cause followed by its follow-ons.
        console: The console to print on.
    """
    index = 0
    while index < len(views):
        _print_view(views[index], console)
        follow_ons = []
        index += 1
        while index < len(views) and views[index].cause:
            follow_ons.append(views[index])
            index += 1
        if follow_ons:
            _print_follow_ons(follow_ons, console)
    if all(view.finding.rule in ALWAYS_LISTED_RULES for view in views):
        console.print(Text("No issues found within this command's stated scope."))
    not_run = not_run_summaries(report)
    if not_run:
        console.print()
    for line in not_run:
        console.print(Text(sanitize_terminal_text(line), style="yellow"))


def _print_suggestions(views: list[FindingView], silenced: int, console: Console) -> None:
    """Print the suggestions in a section of their own, which never changes the result.

    Args:
        views: The suggestion views.
        silenced: How many suggestions a ``% fledge: ignore`` comment silenced; nothing is
            printed without suggestions or silenced ones.
        console: The console to print on.
    """
    if not views and not silenced:
        return
    console.print(
        Text("\nSuggestions (they never change the result or the exit code)", style="bold")
    )
    for view in views:
        line = Text()
        line.append(one_line(f"{view.code}  {view.title}"), style="cyan")
        line.append(f"  {one_line(view.location)}", style="dim")
        console.print(Padding(line, (0, 0, 0, 2)), overflow="fold")
        console.print(Padding(Text(sanitize_terminal_text(view.message)), (0, 0, 0, 4)))
        if view.next_step:
            console.print(
                Padding(Text(sanitize_terminal_text(f"Next: {view.next_step}")), (0, 0, 0, 4))
            )
    if silenced:
        console.print(Padding(Text(silenced_suggestions_line(silenced), style="dim"), (0, 0, 0, 2)))


def silenced_suggestions_line(count: int) -> str:
    """Say how many suggestions comments in the sources silenced."""
    noun = "suggestion" if count == 1 else "suggestions"
    return f"{count} {noun} silenced by % fledge: ignore comments in the sources."


def _print_changes(report: RedactedReport, options: RenderOptions, console: Console) -> None:
    """Print the proposed or applied changes, summarized by kind when there are many.

    Individual changes are listed when there are at most ``MAX_LISTED_CHANGES`` or diffs
    are asked for; each diff follows its change when diffs are shown.

    Args:
        report: The redacted report.
        options: The rendering options; ``show_diff`` lists every change with its diff.
        console: The console to print on.
    """
    if not report.changes:
        return
    count = len(report.changes)
    console.print(
        Text(
            f"\n{count} change{'s' if count != 1 else ''} to the separate copy (your files are "
            f"unchanged): {one_line(change_summary(report))}",
            style="bold",
        )
    )
    if not (options.show_diff or len(report.changes) <= MAX_LISTED_CHANGES):
        console.print(
            Text(
                f"  More than {MAX_LISTED_CHANGES} changes: individual entries are omitted; "
                "use --json for every path and reason.",
                style="dim",
            )
        )
        return
    for change in report.changes:
        destination = f" -> {change.destination}" if change.destination else ""
        _detail(
            console,
            sanitize_terminal_text(str(change.kind)),
            f"{change.path}{destination} — {change.reason}",
        )
        if options.show_diff and change.diff:
            console.print(_diff_text(change.diff))


def _print_artifacts_and_hints(
    report: RedactedReport,
    records: ExecutionRecords,
    views: list[FindingView],
    options: RenderOptions,
    console: Console,
) -> None:
    """Print published files, the opt-in checks left off, how to explain a check, the verdict.

    The opt-in checks left off are named only after a run that passed.

    Args:
        report: The redacted report.
        records: The execution records of the report.
        views: The finding views; a hint to explain a check follows when one of them has a
            code and did not pass.
        options: The rendering options, which may name the settings file.
        console: The console to print on.
    """
    if report.artifacts:
        console.print(Text("\nArtifacts", style="bold cyan"))
        for name, path in report.artifacts.items():
            _detail(console, one_line(str(name)), one_line(str(path)))
    settings_file = options.settings_file or records.config_path
    if report.outcome.passed and (
        summary := not_checked_summary(records.not_checked, settings_file)
    ):
        console.print(Text(f"\n{sanitize_terminal_text(summary)}", style="dim"))
    coded = next(
        (view.code for view in views if view.finding.code and view.finding.status != "passed"),
        None,
    )
    if coded:
        console.print(Text(f"\nExplain a check: fledge rule {one_line(coded)}", style="dim"))
    style = OUTCOME_STYLES[report.outcome]
    console.print(Text(f"\n{one_line(verdict_line(report, views))}", style=style))


def brief_status(report: RedactedReport) -> str | None:
    """Word the short status that replaces the terminal report, when one does.

    A rejected request is shown as ``Error:`` and ``Hint:`` lines and a cancelled run as
    one line, because neither says anything about the paper.

    Returns:
        The status text, or ``None`` when the full report is shown.
    """
    if report.is_request_error:
        lines = []
        for finding in report.findings:
            if finding.rule == REQUEST_ERROR_RULE:
                lines.append(f"Error: {one_line(finding.message)}")
                if finding.next_step:
                    lines.append(f"Hint: {one_line(finding.next_step)}")
        return "\n".join(lines)
    if report.outcome is Outcome.CANCELLED:
        return one_line(cancellation_line(report))
    return None


def print_redacted_report(
    report: RedactedReport, console: Console, *, options: RenderOptions = DEFAULT_RENDER_OPTIONS
) -> None:
    """Print the styled terminal report of a report that was already redacted.

    A rejected request or a cancelled run prints only its brief status (see
    :func:`brief_status`).

    Args:
        report: The redacted report.
        console: The console to print on.
        options: The rendering options.
    """
    if (status := brief_status(report)) is not None:
        console.print(Text(status), overflow="fold")
        return
    records = read_execution_records(report.execution)
    views = finding_views(report, show_hidden=options.show_passed)
    _print_summary(report, records, options, console)
    _print_findings(report, views, console)
    _print_suggestions(
        suggestion_views(report, show_hidden=options.show_passed),
        records.silenced_suggestions,
        console,
    )
    _print_changes(report, options, console)
    _print_package_contents(report, console, show_files=options.show_files)
    _print_artifacts_and_hints(report, records, views, options, console)


def print_report(
    report: Report, console: Console, *, options: RenderOptions = DEFAULT_RENDER_OPTIONS
) -> None:
    """Print the styled terminal report; the report is redacted before display.

    Args:
        report: The report to print.
        console: The console to print on.
        options: The rendering options.
    """
    print_redacted_report(redact_report(report), console, options=options)


def render_redacted_terminal(
    report: RedactedReport, *, options: RenderOptions = DEFAULT_RENDER_OPTIONS
) -> str:
    """Render an already redacted report as plain text, without styling.

    Args:
        report: The redacted report.
        options: The rendering options.

    Returns:
        The terminal report without trailing whitespace.
    """
    output = StringIO()
    print_redacted_report(
        report,
        Console(file=output, width=1000, color_system=None, markup=False, highlight=False),
        options=options,
    )
    return output.getvalue().rstrip()


def render_terminal(report: Report, *, options: RenderOptions = DEFAULT_RENDER_OPTIONS) -> str:
    """Render a report as plain text for tests and embedding.

    The live CLI retains terminal colors. The report is redacted before rendering.

    Args:
        report: The report to render.
        options: The rendering options.

    Returns:
        The terminal report without trailing whitespace.
    """
    return render_redacted_terminal(redact_report(report), options=options)
