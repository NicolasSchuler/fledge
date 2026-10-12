"""CLI adapter for bounded local setup diagnostics."""

from __future__ import annotations

import asyncio
from pathlib import Path

import click
from rich.console import Console

from .command_options import reject_config_with_isolated, settings_file_options
from .config import load_settings, resolve_config_path
from .doctor import run_doctor
from .finding_view import RenderOptions
from .models import (
    ENGINES,
    Engine,
    Outcome,
    PreparationError,
    Report,
    cancelled_finding,
    internal_error_finding,
    request_error_finding,
)
from .report_formats import REQUEST_SCOPE, OutputFormat, write_report
from .terminal_progress import TerminalProgress


@click.command("doctor")
@click.argument(
    "project",
    required=False,
    metavar="[PROJECT]",
    type=click.Path(exists=True, path_type=Path),
)
@click.option("--engine", type=click.Choice(ENGINES))
@settings_file_options
@click.option("--json", "json_output", is_flag=True, help="Print the structured setup report.")
@click.option("--quiet", is_flag=True, help="Hide progress; retain the final report.")
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
    report = Report("doctor", scope=REQUEST_SCOPE)
    try:
        selected = (
            resolve_config_path(project or Path.cwd(), config, isolated=isolated)
            if project is not None or config is not None
            else None
        )
        settings = load_settings(selected, {"engine": engine})
        with TerminalProgress(Console(stderr=True), quiet=quiet) as progress:
            report = asyncio.run(run_doctor(settings, progress=progress))
        report.execution["config_path"] = str(selected) if selected is not None else None
        report.execution["project"] = str(project) if project is not None else None
    except (PreparationError, OSError, ValueError) as error:
        report.outcome = Outcome.ERROR
        report.findings.append(
            request_error_finding(
                error,
                "Correct the selected settings or configuration path and rerun fledge doctor.",
            )
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        report.outcome = Outcome.CANCELLED
        report.findings.append(
            cancelled_finding(
                "Setup diagnostics were cancelled.", "Rerun fledge doctor when ready."
            )
        )
    except Exception as error:
        report.outcome = Outcome.ERROR
        report.findings.append(internal_error_finding(error))
    write_report(
        report,
        OutputFormat.JSON if json_output else OutputFormat.TERMINAL,
        console=Console(),
        options=RenderOptions(show_passed=True),
    )
    return report.outcome.exit_code
