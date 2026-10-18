"""CLI adapter for bounded local setup diagnostics."""

from __future__ import annotations

import asyncio
from pathlib import Path

import click

from .command_options import (
    QUIET_HELP,
    debug_option,
    discard_closed_stdout,
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


@click.command("doctor", short_help="Test that your TeX setup works.")
@click.argument(
    "project",
    required=False,
    metavar="[PROJECT]",
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
@click.option("--quiet", is_flag=True, help=QUIET_HELP)
def doctor_command(
    project: Path | None,
    engine: Engine | None,
    config: Path | None,
    isolated: bool,
    json_output: bool,
    quiet: bool,
) -> int:
    """Test the local setup with a tiny built-in document.

    PROJECT selects nearby settings. With no PROJECT, defaults apply unless
    --config is supplied. Every configured document engine is exercised.
    Your manuscript is never compiled or changed. A pass covers the built-in
    fixture, not project packages, custom classes, or venue compliance.
    """
    reject_config_with_isolated(config, isolated)
    report = Report("doctor", scope=UNFINISHED_SCOPE)
    try:
        selected = (
            resolve_config_path(project or Path.cwd(), config, isolated=isolated)
            if project is not None or config is not None
            else None
        )
        settings = load_settings(selected, {"engine": engine})
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
    except Exception as error:
        record_job_failure(
            report,
            error,
            "Correct the selected settings or configuration path and rerun fledge doctor.",
        )
    try:
        write_report(
            report,
            OutputFormat.JSON if json_output else OutputFormat.TERMINAL,
            console=stdout_console(),
            options=RenderOptions(show_passed=True),
        )
    except BrokenPipeError:
        # The reader of stdout stopped early; the verdict is still the exit code.
        discard_closed_stdout()
    return report.outcome.exit_code
