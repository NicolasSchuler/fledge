"""Rich terminal and plain compact reports, plus the terminal check catalogue.

No terminal markup or control sequence from inputs reaches the terminal: every
untrusted text passes through :func:`sanitize_terminal_text`.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Sequence
from io import StringIO

from rich.console import Console
from rich.padding import Padding
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .coverage_hints import not_checked_summary
from .finding_view import (
    ALWAYS_LISTED_RULES,
    DEFAULT_RENDER_OPTIONS,
    RedactedReport,
    RenderOptions,
    finding_views,
    is_scope_disclaimer,
    merge_stage_copies,
    not_run_summaries,
    redact_report,
    size_label,
)
from .models import Finding, Outcome, Report
from .rule_definition import Rule

# Larger change sets are summarized by kind; every entry remains in the JSON report.
MAX_LISTED_CHANGES = 20
OUTCOME_STYLES: dict[Outcome, str] = {
    Outcome.PASSED: "bold green",
    Outcome.PLANNED: "bold cyan",
    Outcome.PASSED_WITH_ADVISORIES: "bold yellow",
    Outcome.ACCEPTED_EXCEPTIONS: "bold yellow",
    Outcome.BLOCKED: "bold red",
    Outcome.ERROR: "bold red",
    Outcome.CANCELLED: "bold yellow",
}


def _visible_format_character(char: str) -> str:
    """Show an invisible format character, such as a bidi override, as a visible escape."""
    code = ord(char)
    return f"\\u{code:04x}" if code <= 0xFFFF else f"\\U{code:08x}"


def sanitize_terminal_text(value: str) -> str:
    """Remove escape sequences and control characters, keeping newlines and tabs.

    Invisible format characters (Unicode category Cf, for example right-to-left
    overrides that can reorder a displayed path) are shown as ``\\uXXXX`` escapes.
    """
    # File names and diagnostics are untrusted text, never terminal escape commands.
    value = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", value)
    value = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)
    return "".join(
        _visible_format_character(char) if unicodedata.category(char) == "Cf" else char
        for char in value
        if char in "\n\t" or (ord(char) >= 32 and not 127 <= ord(char) <= 159)
    )


def _one_line(value: str) -> str:
    return (
        sanitize_terminal_text(value).replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t")
    )


def _counts(report: RedactedReport) -> str:
    findings = [entry.finding for entry in merge_stage_copies(report.findings)]
    errors = sum(item.severity == "error" and item.status != "passed" for item in findings)
    warnings = sum(item.severity == "warning" and item.status != "passed" for item in findings)
    statuses = Counter(item.status for item in findings)
    # Informational scope notes are neither failures nor missing evidence: count them apart.
    skipped_notes = sum(item.status == "skipped" and is_scope_disclaimer(item) for item in findings)
    not_applicable = statuses["not_applicable"] + skipped_notes
    incomplete = statuses["inconclusive"] + statuses["skipped"] - skipped_notes
    return (
        f"{errors} error{'s' if errors != 1 else ''}, "
        f"{warnings} warning{'s' if warnings != 1 else ''}, {incomplete} incomplete, "
        f"{not_applicable} not applicable, "
        f"{statuses['passed']} passed result{'s' if statuses['passed'] != 1 else ''}"
    )


def _change_summary(report: RedactedReport) -> str:
    kinds = Counter(change.kind for change in report.changes)
    ordered = sorted(kinds.items(), key=lambda item: (-item[1], item[0]))
    return ", ".join(f"{kind}: {count}" for kind, count in ordered)


def _selection_description(report: RedactedReport) -> str | None:
    selection = report.execution.get("check_selection")
    if not isinstance(selection, dict):
        return None
    selected, ignored = selection.get("select", ["ALL"]), selection.get("ignore", [])
    if selected == ["ALL"] and ignored == []:
        return None
    if not isinstance(selected, list) or not isinstance(ignored, list):
        return None
    return (
        f"Checks: select {', '.join(map(str, selected)) or '(none)'}; "
        f"ignore {', '.join(map(str, ignored)) or '(none)'}; required validation retained"
    )


def _finding_style(finding: Finding) -> str:
    if finding.status == "passed":
        return "green"
    return {"error": "red", "warning": "yellow", "info": "cyan"}.get(finding.severity, "cyan")


def _detail(console: Console, label: str, value: str, *, style: str = "") -> None:
    line = Text()
    line.append(f"{label}: ", style="cyan")
    line.append(sanitize_terminal_text(value), style=style)
    console.print(Padding(line, (0, 0, 0, 2), expand=False), overflow="fold")


def _measured_total(files: list[dict], field: str) -> str:
    total = 0
    for item in files:
        value = item.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return "not measured"
        total += value
    return size_label(total)


def _print_package_contents(report: RedactedReport, console: Console, show_files: bool) -> None:
    for package in report.package_contents:
        entries = package.get("files", [])
        files = (
            [item for item in entries if isinstance(item, dict)]
            if isinstance(entries, list)
            else []
        )
        included = [item for item in files if item.get("status") == "included"]
        excluded = [item for item in files if item.get("status") == "excluded"]
        status = str(package.get("status", "incomplete"))
        style = "green" if status == "verified" else "yellow" if status == "incomplete" else "cyan"
        console.print()
        title = Text("Package contents", style="bold cyan")
        title.append(f" — {_one_line(str(package.get('document', 'project')))}", style="default")
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
            _detail(
                console, "Verification", "Package inventory has not been verified.", style=style
            )
        renamed = [item for item in included if item.get("source") != item.get("path")]
        for item in renamed:
            _detail(
                console,
                "Rename",
                _one_line(f"{item.get('source', '')} -> {item.get('path', '')}"),
            )
        contributors = sorted(
            [item for item in included if isinstance(item.get("compressed_bytes"), int)],
            key=lambda item: item["compressed_bytes"],
            reverse=True,
        )[:3]
        if contributors:
            console.print(Text("  Largest compressed entries:", style="cyan"))
            for item in contributors:
                _detail(
                    console,
                    size_label(item["compressed_bytes"]),
                    _one_line(str(item.get("path", ""))),
                )
        if show_files:
            console.print(Text("  Files:", style="cyan"))
            for item in files:
                file_status = str(item.get("status", "unknown"))
                text = Text(f"  [{sanitize_terminal_text(file_status)}] ", style="cyan")
                text.append(
                    _one_line(str(item.get("path") or item.get("source", ""))), style="default"
                )
                if item.get("source") != item.get("path"):
                    text.append(f" (source: {_one_line(str(item.get('source', '')))})")
                text.append(
                    f" — uncompressed {size_label(item.get('size_bytes'))}; "
                    f"compressed {size_label(item.get('compressed_bytes'))}"
                )
                console.print(text, overflow="fold")
                for reason in item.get("reasons", []):
                    _detail(console, "Reason", str(reason))
        elif files:
            console.print(
                Text("  Use --show-files for every file and inclusion reason.", style="dim")
            )


def print_report(
    report: Report, console: Console, *, options: RenderOptions = DEFAULT_RENDER_OPTIONS
) -> None:
    """Print the styled terminal report; the report is redacted before display."""
    public = redact_report(report)
    outcome = public.outcome.replace("_", " ").capitalize()
    style = OUTCOME_STYLES[public.outcome]
    request_error = public.is_request_error
    summary = Text()
    summary.append("Request error" if request_error else outcome, style=style)
    if not request_error:
        summary.append(f" — {_one_line(public.scope)}")
    summary.append(f"\n{_counts(public)}", style="dim")
    if request_error:
        console.print(summary)
    else:
        console.print(Panel(summary, border_style=style, padding=(0, 1), expand=False))
    if selection := _selection_description(public):
        console.print(Text(sanitize_terminal_text(selection), style="dim"))
    if public.main:
        console.print(Text(f"Document: {_one_line(public.main)}"))
    views = finding_views(public, show_hidden=options.show_passed)
    for view in views:
        console.print()
        color = _finding_style(view.finding)
        heading = Text(_one_line(f"{view.code}  {view.title}"), style=f"bold {color}")
        heading.append(f"  [{view.finding.severity.upper()} / {view.status_label}]", style=color)
        console.print(heading)
        _detail(console, "Where", _one_line(view.location), style="dim")
        console.print(
            Padding(Text(sanitize_terminal_text(view.message)), (0, 0, 0, 2), expand=False)
        )
        if view.evidence:
            _detail(console, "Evidence", _one_line(view.evidence))
        if view.next_step:
            _detail(console, "Next", view.next_step)
        if view.review:
            _detail(console, "Review", view.review, style="yellow")
    if all(view.finding.rule in ALWAYS_LISTED_RULES for view in views):
        console.print(Text("No issues found within this command's stated scope."))
    not_run = not_run_summaries(public)
    if not_run:
        console.print()
    for line in not_run:
        console.print(Text(sanitize_terminal_text(line), style="yellow"))
    if public.changes:
        console.print(
            Text(
                f"\n{len(public.changes)} proposed/applied changes (original input unchanged): "
                f"{_one_line(_change_summary(public))}",
                style="bold",
            )
        )
        if options.show_diff or len(public.changes) <= MAX_LISTED_CHANGES:
            for change in public.changes:
                destination = f" -> {change.destination}" if change.destination else ""
                console.print(
                    Text(
                        sanitize_terminal_text(
                            f"  {change.kind}: {change.path}{destination} — {change.reason}"
                        )
                    )
                )
                if options.show_diff and change.diff:
                    console.print(Text(sanitize_terminal_text(change.diff.rstrip())))
        else:
            console.print(
                Text(
                    f"  More than {MAX_LISTED_CHANGES} changes: individual entries are omitted; "
                    "use --json for every path and reason.",
                    style="dim",
                )
            )
    _print_package_contents(public, console, options.show_files)
    if public.artifacts:
        console.print(Text("\nArtifacts", style="bold cyan"))
        for name, path in public.artifacts.items():
            console.print(Text(_one_line(f"{name}: {path}")), overflow="fold")
    if summary := not_checked_summary(
        public.execution.get("not_checked"), public.execution.get("config_path")
    ):
        console.print(Text(f"\n{sanitize_terminal_text(summary)}", style="dim"))
    if any(view.finding.code and view.finding.status != "passed" for view in views):
        console.print(Text("\nExplain a check: fledge rule CODE", style="dim"))


def render_terminal(report: Report, *, options: RenderOptions = DEFAULT_RENDER_OPTIONS) -> str:
    """Plain rendering for tests/embedding; the live CLI retains terminal colors."""
    output = StringIO()
    print_report(
        report,
        Console(file=output, width=1000, color_system=None, markup=False, highlight=False),
        options=options,
    )
    return output.getvalue().rstrip()


def render_compact(report: Report, *, options: RenderOptions = DEFAULT_RENDER_OPTIONS) -> str:
    """Short plain records; one physical line per finding, full evidence remains in JSON."""
    public = redact_report(report)
    lines = [f"{public.outcome} | {_counts(public)} | scope: {_one_line(public.scope)}"]
    if selection := _selection_description(public):
        lines.append(_one_line(selection))
    if public.main:
        lines.append(f"document: {_one_line(public.main)}")
    for view in finding_views(public, show_hidden=options.show_passed):
        line = (
            f"{_one_line(view.code)} {view.finding.severity}/{view.status_label} "
            f"{_one_line(view.location)}"
            f" | {_one_line(view.title)}: {_one_line(view.message)}"
        )
        if view.evidence:
            line += f" | evidence: {_one_line(view.evidence)}"
        if view.next_step:
            line += f" | next: {_one_line(view.next_step)}"
        if view.review:
            line += f" | review: {_one_line(view.review)}"
        lines.append(line)
    lines.extend(f"not run: {_one_line(line)}" for line in not_run_summaries(public))
    if public.changes:
        lines.append(
            f"changes: {len(public.changes)} ({_one_line(_change_summary(public))}; "
            "use --json for full paths and diffs)"
        )
    for name, path in public.artifacts.items():
        lines.append(f"{_one_line(name)}: {_one_line(path)}")
    return "\n".join(lines)


def print_rule_table(rules: Sequence[Rule], console: Console) -> None:
    """List every given check with its code and title."""
    table = Table(title=f"{len(rules)} registered checks", box=None, padding=(0, 2, 0, 0))
    table.add_column("Code", style="bold", no_wrap=True)
    table.add_column("Check")
    for rule in rules:
        table.add_row(Text(rule.code), Text(rule.title))
    console.print(table)
    console.print(Text("Explain a check and its tests: fledge rule CODE", style="dim"))


def print_rule_explanation(rule: Rule, console: Console) -> None:
    """Explain one check: its scope, suggested fix, diagnostic name and tests."""
    console.print(Text(f"{rule.code}: {rule.title}", style="bold"))
    console.print(Text(rule.description))
    console.print(Text(f"Next: {rule.fix}"))
    console.print(Text(f"Diagnostic name: {rule.name}"))
    console.print(Text("Behavioral tests:", style="bold"))
    for test in rule.tests:
        console.print(Text(f"  {test}"), soft_wrap=True)
