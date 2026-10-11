"""CLI adapter for bounded local setup diagnostics."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import click
from rich.console import Console

from .config import RequestError, load_settings, resolve_config_path
from .doctor import run_doctor
from .models import Finding, PreparationError, Report
from .presentation import REQUEST_SCOPE, print_report
from .terminal_progress import TerminalProgress


@click.command("doctor")
@click.argument(
    "project",
    required=False,
    metavar="[PROJECT]",
    type=click.Path(exists=True, path_type=Path),
)
@click.option("--engine", type=click.Choice(["pdflatex", "xelatex", "lualatex"]))
@click.option(
    "--config", type=click.Path(path_type=Path), help="Use an explicit TOML settings file."
)
@click.option("--isolated", is_flag=True, help="Ignore discovered project settings.")
@click.option("--json", "json_output", is_flag=True, help="Print the structured setup report.")
@click.option("--quiet", is_flag=True, help="Hide progress; retain the final report.")
def doctor_command(
    project: Path | None,
    engine: str | None,
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
    # Import after CLI construction so this adapter does not create a cycle.
    from .cli import EXIT_CODES

    if isolated and config is not None:
        raise click.UsageError("--isolated cannot be combined with --config")
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
        report.outcome = "error"
        report.findings.append(
            Finding(
                "configuration.invalid",
                str(error),
                "error",
                suggestion=error.suggestion
                if isinstance(error, RequestError)
                else "Correct the selected settings or configuration path and rerun fledge doctor.",
            )
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        report.outcome = "cancelled"
        report.findings.append(
            Finding(
                "execution.cancelled",
                "Setup diagnostics were cancelled.",
                "error",
                "inconclusive",
                suggestion="Rerun fledge doctor when ready.",
            )
        )
    except Exception as error:
        report.outcome = "error"
        report.findings.append(
            Finding(
                "execution.error",
                f"Setup diagnostics failed: {error}",
                "error",
                "inconclusive",
                suggestion="Report this failure with the structured diagnostic output.",
            )
        )
    if json_output:
        click.echo(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    else:
        print_report(report, Console(), show_passed=True)
    return EXIT_CODES[report.outcome]
