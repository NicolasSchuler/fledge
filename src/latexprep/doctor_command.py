"""CLI adapter for bounded local setup diagnostics."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table
from rich.text import Text

from .cli_options import FledgeCommand
from .command_options import (
    debug_option,
    discard_closed_stdout,
    quiet_option,
    record_cancellation,
    record_job_failure,
    reject_config_with_isolated,
    settings_file_options,
)
from .config import Settings, load_settings
from .config_discovery import resolve_config_path
from .doctor import run_doctor
from .execution_records import CONFIG_PATH_KEY
from .finding_view import RenderOptions
from .findings import cancelled_finding
from .report import UNFINISHED_SCOPE, Report
from .report_formats import OutputFormat, write_report
from .setting_choices import ENGINES, Engine
from .terminal_console import stderr_console, stdout_console
from .terminal_progress import TerminalProgress
from .text_safety import sanitize_terminal_text

DOCTOR_EXAMPLES = (
    ("fledge doctor", "Test TeX and the PDF tools with the default settings."),
    ("fledge doctor paper", "Test the engines that the settings of paper choose."),
    ("fledge doctor --engine lualatex", "Test one engine instead of the configured ones."),
)


def _tool_summary(record: object) -> str:
    """Describe one tool of a setup report: its first version line, or why there is none.

    Args:
        record: The entry of ``Report.tools`` that doctor wrote for the tool.

    Returns:
        The version line, "not found" or "version not verified", followed by "(optional)"
        for a tool the run did not need.
    """
    if not isinstance(record, Mapping):
        return "unknown"
    version = record.get("version")
    if not record.get("installed"):
        text = "not found"
    elif isinstance(version, list) and version and isinstance(version[0], str):
        text = version[0]
    else:
        text = "version not verified"
    return f"{text} (optional)" if record.get("required") is False else text


def print_tool_versions(tools: Mapping[str, object], console: Console) -> None:
    """List each tool doctor looked for with its version, after the setup report.

    Args:
        tools: The tools of the setup report, by name.
        console: The console that prints the list.
    """
    if not tools:
        return
    table = Table(title="Tools", title_justify="left", box=None, padding=(0, 2, 0, 2))
    table.add_column("Tool", style="bold", no_wrap=True)
    table.add_column("Version")
    for name in sorted(tools):
        table.add_row(
            Text(sanitize_terminal_text(name)),
            Text(sanitize_terminal_text(_tool_summary(tools[name]))),
        )
    console.print()
    console.print(table)


@click.command(
    "doctor",
    cls=FledgeCommand,
    short_help="Test that your TeX setup works.",
    examples=DOCTOR_EXAMPLES,
)
@click.argument(
    "project",
    required=False,
    metavar="[INPUT]",
    type=click.Path(exists=True, path_type=Path),
)
@click.option(
    "--engine",
    type=click.Choice(ENGINES),
    help="TeX engine to test instead of the configured one "
    f"(default {Settings().engine} unless a settings file chooses another).",
)
@settings_file_options
@debug_option
@click.option("--json", "json_output", is_flag=True, help="Print the structured setup report.")
@quiet_option
def doctor_command(
    project: Path | None,
    engine: Engine | None,
    config: Path | None,
    *,
    isolated: bool,
    json_output: bool,
    quiet: bool,
) -> int:
    """Test the local setup with a tiny built-in document.

    INPUT, a project folder or ZIP, selects the settings that apply to it. With no
    INPUT, defaults apply unless --config is supplied. Every configured document
    engine is exercised. Your manuscript is never compiled or changed. A pass covers
    the built-in fixture, not project packages, custom classes, or venue compliance.
    """
    reject_config_with_isolated(config, isolated=isolated)
    report = Report("doctor", scope=UNFINISHED_SCOPE)
    job_started = False
    try:
        selected = (
            resolve_config_path(project or Path.cwd(), config, isolated=isolated)
            if project is not None or config is not None
            else None
        )
        settings = load_settings(selected, {"engine": engine})
        job_started = True
        with TerminalProgress(stderr_console(), quiet=quiet) as progress:
            report = asyncio.run(run_doctor(settings, progress=progress))
        report.execution[CONFIG_PATH_KEY] = str(selected) if selected is not None else None
        report.execution["project"] = str(project) if project is not None else None
    except (KeyboardInterrupt, asyncio.CancelledError):
        record_cancellation(
            report,
            cancelled_finding(
                "Setup diagnostics were cancelled.", "Rerun fledge doctor when ready."
            ),
        )
    except Exception as error:  # noqa: BLE001 - recorded as a job failure with its cause
        record_job_failure(
            report,
            error,
            "Correct the selected settings or configuration path and rerun fledge doctor.",
            job_started=job_started,
        )
    try:
        console = stdout_console()
        write_report(
            report,
            OutputFormat.JSON if json_output else OutputFormat.TERMINAL,
            console=console,
            options=RenderOptions(show_passed=True),
        )
        if not json_output:
            print_tool_versions(report.tools, console)
    except BrokenPipeError:
        # The reader of stdout stopped early; the verdict is still the exit code.
        discard_closed_stdout()
    return report.outcome.exit_code
