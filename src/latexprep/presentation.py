"""Human and agent-readable views of the same findings; no terminal markup from inputs."""

from __future__ import annotations

import re
from collections import Counter
from io import StringIO

from rich.console import Console
from rich.table import Table
from rich.text import Text

from .models import Finding, Report
from .reporting import review_marker
from .rules import BY_NAME, Rule


def _safe(value: str) -> str:
    # File names and diagnostics are untrusted text, never terminal escape commands.
    value = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", value)
    value = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)
    return "".join(
        char for char in value if char in "\n\t" or (ord(char) >= 32 and ord(char) != 127)
    )


def _one_line(value: str) -> str:
    return _safe(value).replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t")


def _label(finding: Finding) -> str:
    definition = BY_NAME.get(finding.rule)
    return definition.title if definition else finding.rule.replace("_", " ").replace(".", " / ")


def _location(finding: Finding) -> str:
    location = finding.path or "project"
    if document := finding.details.get("document"):
        location = f"{document}: {location}"
    if finding.line is not None:
        location += f":{finding.line}"
    elif finding.details.get("input_line") is not None:
        location += f" (reported input line {finding.details['input_line']}; file unresolved)"
    page = finding.details.get("page")
    if page is not None:
        location += f" (page {page})"
    if "stage" in finding.details:
        location += f" [{finding.details['stage']}]"
    return location


def _priority(finding: Finding) -> int:
    if finding.status == "passed":
        return 4
    if finding.severity == "error":
        return 0 if finding.status == "failed" else 1
    return 2 if finding.severity == "warning" else 3


def _visible(report: Report, show_passed: bool) -> list[Finding]:
    return sorted(
        (item for item in report.sorted_findings() if show_passed or item.status != "passed"),
        key=_priority,
    )


def _counts(report: Report) -> str:
    errors = sum(item.severity == "error" and item.status != "passed" for item in report.findings)
    warnings = sum(
        item.severity == "warning" and item.status != "passed" for item in report.findings
    )
    statuses = Counter(item.status for item in report.findings)
    incomplete = statuses["inconclusive"] + statuses["skipped"]
    return (
        f"{errors} error{'s' if errors != 1 else ''}, "
        f"{warnings} warning{'s' if warnings != 1 else ''}, {incomplete} incomplete, "
        f"{statuses['passed']} passed result{'s' if statuses['passed'] != 1 else ''}"
    )


def _selection_description(report: Report) -> str | None:
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


def print_report(
    report: Report, console: Console, *, show_diff: bool = False, show_passed: bool = False
) -> None:
    report = report.redacted_copy()
    outcome = report.outcome.replace("_", " ").capitalize()
    style = "bold red" if report.outcome in {"blocked", "error"} else "bold"
    console.print(Text(f"{outcome} — {_safe(report.scope)}", style=style))
    console.print(Text(_counts(report), style="dim"))
    if selection := _selection_description(report):
        console.print(Text(_safe(selection), style="dim"))
    if report.main:
        console.print(Text(f"Document: {_safe(report.main)}"))
    for finding in _visible(report, show_passed):
        console.print()
        code = finding.code or finding.rule
        status = "not checked" if finding.status == "skipped" else finding.status
        color = {"error": "red", "warning": "yellow", "info": "cyan"}.get(finding.severity, "")
        heading = Text(f"{code}  {_label(finding)}", style=f"bold {color}".strip())
        heading.append(f"  [{finding.severity.upper()} / {status}]", style=color)
        console.print(heading)
        console.print(Text(f"  Where: {_safe(_location(finding))}", style="dim"))
        console.print(Text(f"  {_safe(finding.message)}"))
        if finding.next_step:
            console.print(Text(f"  Next: {_safe(finding.next_step)}"))
        if marker := review_marker(finding):
            console.print(Text(f"  Review: {_safe(marker)}"))
    if not _visible(report, show_passed):
        console.print(Text("No issues found within this command's stated scope."))
    if report.changes:
        console.print(
            Text(
                f"\n{len(report.changes)} proposed/applied changes (original input unchanged):",
                style="bold",
            )
        )
        for change in report.changes:
            destination = f" -> {change.destination}" if change.destination else ""
            console.print(
                Text(_safe(f"  {change.kind}: {change.path}{destination} — {change.reason}"))
            )
            if show_diff and change.diff:
                console.print(Text(_safe(change.diff.rstrip())))
    for name, path in report.artifacts.items():
        console.print(Text(f"{name}: {_safe(path)}"))
    if any(item.code and item.status != "passed" for item in report.findings):
        console.print(Text("\nExplain a check: fledge rule CODE", style="dim"))


def render_terminal(report: Report, show_diff: bool = False, show_passed: bool = False) -> str:
    """Plain rendering for tests/embedding; the live CLI retains terminal colors."""
    output = StringIO()
    print_report(
        report,
        Console(file=output, width=100, color_system=None, markup=False, highlight=False),
        show_diff=show_diff,
        show_passed=show_passed,
    )
    return output.getvalue().rstrip()


def render_compact(report: Report, *, show_passed: bool = False) -> str:
    """Short plain records; one physical line per finding, full evidence remains in JSON."""
    report = report.redacted_copy()
    lines = [f"{report.outcome} | {_counts(report)} | scope: {_one_line(report.scope)}"]
    if selection := _selection_description(report):
        lines.append(_one_line(selection))
    if report.main:
        lines.append(f"document: {_one_line(report.main)}")
    for finding in _visible(report, show_passed):
        code = finding.code or finding.rule
        line = (
            f"{code} {finding.severity}/{finding.status} {_one_line(_location(finding))}"
            f" | {_one_line(_label(finding))}: {_one_line(finding.message)}"
        )
        if finding.next_step:
            line += f" | next: {_one_line(finding.next_step)}"
        if marker := review_marker(finding):
            line += f" | review: {_one_line(marker)}"
        lines.append(line)
    if report.changes:
        lines.append(f"changes: {len(report.changes)} (use --json for full paths and diffs)")
    for name, path in report.artifacts.items():
        lines.append(f"{name}: {_one_line(path)}")
    return "\n".join(lines)


def print_rules(rules: tuple[Rule, ...], console: Console, *, explain: bool = False) -> None:
    if explain:
        rule = rules[0]
        console.print(Text(f"{rule.code}: {rule.title}", style="bold"))
        console.print(Text(rule.description))
        console.print(Text(f"Next: {rule.fix}"))
        console.print(Text(f"Diagnostic name: {rule.name}"))
        console.print(Text("Behavioral tests:", style="bold"))
        for test in rule.tests:
            console.print(Text(f"  {test}"), soft_wrap=True)
        return
    table = Table(title=f"{len(rules)} registered checks", box=None, padding=(0, 2, 0, 0))
    table.add_column("Code", style="bold", no_wrap=True)
    table.add_column("Check")
    for rule in rules:
        table.add_row(Text(rule.code), Text(rule.title))
    console.print(table)
    console.print(Text("Explain a check and its tests: fledge rule CODE", style="dim"))
