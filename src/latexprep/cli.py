"""Click interface with Rich terminal reports and plain compact/JSON output."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.text import Text

from . import __version__
from .config import Settings, load_settings, resolve_config_path
from .core import JobRequest, run_job, validate_destination
from .models import Finding, PreparationError, Report
from .presentation import print_report, print_rules, render_compact
from .rules import RULES, get_rule

EXIT_CODES = {
    "passed": 0,
    "planned": 0,
    "passed_with_advisories": 1,
    "accepted_exceptions": 2,
    "blocked": 3,
    "error": 4,
    "cancelled": 130,
}


def _common(function):
    options = [
        click.argument("source", type=click.Path(path_type=Path), metavar="INPUT"),
        click.option(
            "--config",
            type=click.Path(path_type=Path),
            help="Use this TOML settings file instead of discovering project configuration.",
        ),
        click.option(
            "--isolated", is_flag=True, help="Ignore discovered configuration and use defaults."
        ),
        click.option("--json", "json_output", is_flag=True, help="Alias for --output-format json."),
        click.option(
            "--output-format",
            type=click.Choice(["terminal", "compact", "json", "html", "ci"]),
            help="Terminal, agent-friendly compact, JSON, offline HTML or CI annotations.",
        ),
        click.option(
            "--html-report",
            type=click.Path(path_type=Path),
            help="Save an escaped offline HTML report to a new file outside the input.",
        ),
        click.option(
            "--preview-output",
            type=click.Path(path_type=Path),
            help="New directory for explicitly selected grayscale page previews.",
        ),
        click.option(
            "--diagnostics",
            type=click.Path(path_type=Path),
            help="Export an explicitly unverified diagnostic ZIP of sanitized reports and diffs.",
        ),
        click.option(
            "--report",
            type=click.Path(path_type=Path),
            help="Also save JSON to a new file outside the input.",
        ),
        click.option("--quiet", is_flag=True, help="Suppress progress on stderr."),
        click.option(
            "--show-passed", is_flag=True, help="Also show passed results and inventories."
        ),
        click.option("--non-interactive", is_flag=True, help="Never prompt (also the default)."),
        click.option(
            "--jobs", type=int, help="Shared CPU slots for all operations; 1 runs serially."
        ),
        click.option(
            "--build-jobs", type=int, help="Maximum concurrent build operations (default 1)."
        ),
        click.option(
            "--render-jobs", type=int, help="Maximum concurrent page-pair renderers (default 1)."
        ),
        click.option(
            "--job-timeout-seconds", type=int, help="Overall job deadline, including queue waits."
        ),
        click.option("--timeout-seconds", type=int, help="Per-tool wall time limit."),
    ]
    for option in reversed(options):
        function = option(function)
    return function


def _online_option(function):
    return click.option(
        "--online/--offline",
        default=None,
        help="Allow configured DOI/URL checks to send public queries; offline by default.",
    )(function)


def _build_options(function):
    options = [
        click.option("--engine", type=click.Choice(["pdflatex", "xelatex", "lualatex"])),
        click.option(
            "--bibliography-backend",
            type=click.Choice(["auto", "bibtex", "biber"]),
            help="Select and validate the bibliography backend explicitly.",
        ),
        click.option(
            "--reference-pdf", type=click.Path(path_type=Path), help="Required comparison PDF."
        ),
        click.option("--max-pages", type=int, help="Total page limit, including references."),
    ]
    for option in reversed(options):
        function = option(function)
    return function


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="latex-prep")
def cli() -> None:
    """Check and prepare a separate LaTeX submission copy.

    INPUT is a source folder or ZIP. Originals are never edited. Check codes and
    suggested next steps appear in reports. Check selection and generic limits
    come from project TOML configuration; --config selects an explicit file.
    """


@cli.result_callback()
def _exit(result: int | None, **_kwargs) -> None:
    click.get_current_context().exit(result or 0)


@cli.command("inspect")
@_common
@_online_option
@click.option("--main", help="Relative path of the selected document.")
def inspect_command(**options) -> int:
    """Check source, manuscript constraints and bibliography without running TeX."""
    return _execute("inspect", options)


@cli.command("check")
@_common
@_online_option
@click.option("--main", help="Relative path of the selected document.")
@_build_options
def check_command(**options) -> int:
    """Check source, build in isolation and inspect the PDF."""
    return _execute("check", options)


@cli.command("prepare")
@_common
@_online_option
@click.option("--main", help="Relative path of the selected document.")
@_build_options
@click.option("--output", type=click.Path(path_type=Path), help="New directory outside the input.")
@click.option("--layout", type=click.Choice(["preserve", "flat"]))
@click.option("--format/--no-format", default=None, help="Apply tex-fmt to the separate copy.")
@click.option(
    "--normalize-doi/--no-normalize-doi", default=None, help="Normalize literal DOI prefixes."
)
@click.option(
    "--cleanup/--no-cleanup",
    default=None,
    help="Remove known debris before edits; needed-input packaging always applies.",
)
@click.option(
    "--dry-run", is_flag=True, help="Build the baseline and preview edits; no final bundle."
)
def prepare_command(**options) -> int:
    """Package the selected paper's needed inputs and explicit deliverables.

    Unrelated files are omitted from the copy; originals stay unchanged. Complete
    dependency evidence and an exact archive rebuild are required. Retain extra
    files with submission_checks.required_deliverables in project configuration.
    """
    return _execute("prepare", options)


@cli.group("bib")
def bibliography() -> None:
    """Bibliography checks, offline unless explicitly enabled."""


@bibliography.command("check")
@_common
@_online_option
@click.option("--main", help="Relative path of the document whose citations should be checked.")
def bibliography_check(**options) -> int:
    """Check syntax, citation use and configured fields; online evidence is opt-in."""
    return _execute("bib", options)


@cli.command("fmt")
@_common
@click.option("--check", is_flag=True, help="Check only (also the default).")
@click.option("--diff", is_flag=True, help="Show proposed formatting patches in terminal output.")
def formatting(**options) -> int:
    """Read-only tex-fmt check; show differences without editing originals."""
    return _execute("fmt", options)


@cli.group("pdf")
def pdf_group() -> None:
    """Inspect a PDF independently of a source project."""


@pdf_group.command("check")
@_common
@_build_options
def pdf_check(**options) -> int:
    """Inspect PDF constraints; --reference-pdf adds an exact comparison."""
    return _execute("pdf", options)


@cli.command("rules")
@click.option("--json", "json_output", is_flag=True, help="Print the complete catalogue as JSON.")
def rules_command(json_output: bool) -> int:
    """List stable check codes and descriptive names."""
    if json_output:
        click.echo(json.dumps([rule.to_dict() for rule in RULES], indent=2, ensure_ascii=False))
    else:
        print_rules(RULES, Console(markup=False, highlight=False))
    return 0


@cli.command("rule")
@click.argument("code")
@click.option("--json", "json_output", is_flag=True)
def rule_command(code: str, json_output: bool) -> int:
    """Explain CODE, its limitations, suggested fix and behavioral unit tests."""
    try:
        definition = get_rule(code)
    except ValueError as error:
        raise click.BadParameter(str(error), param_hint="CODE") from error
    if json_output:
        click.echo(json.dumps(definition.to_dict(), indent=2, ensure_ascii=False))
    else:
        print_rules((definition,), Console(markup=False, highlight=False), explain=True)
    return 0


def _execute(command: str, options: dict[str, Any]) -> int:
    if options.get("isolated") and options.get("config") is not None:
        raise click.UsageError("--isolated cannot be combined with --config")
    output_format = options.get("output_format") or "terminal"
    if options["json_output"]:
        if options.get("output_format") not in {None, "json"}:
            raise click.UsageError("--json conflicts with the chosen --output-format")
        output_format = "json"
    report = Report(command, scope="request validation")
    report_destination = None
    html_destination = diagnostic_destination = None
    source = options["source"]
    try:
        if options["report"]:
            report_destination = validate_destination(source, options["report"])
        if options.get("html_report"):
            html_destination = validate_destination(source, options["html_report"])
        if options.get("diagnostics"):
            diagnostic_destination = validate_destination(source, options["diagnostics"])
        destinations = [
            item
            for item in (
                report_destination,
                html_destination,
                diagnostic_destination,
                options.get("output"),
            )
            if item is not None
        ]
        if len({item.resolve() for item in destinations}) != len(destinations):
            raise PreparationError(
                "Output, JSON, HTML and diagnostics destinations must be distinct"
            )
        overrides = {
            name: options[name]
            for name in Settings.__dataclass_fields__
            if options.get(name) is not None
        }
        config_path = resolve_config_path(
            source, options.get("config"), isolated=options.get("isolated", False)
        )
        report.execution["config_path"] = str(config_path) if config_path is not None else None
        settings = load_settings(config_path, overrides)
        if options.get("online") is not None:
            settings = replace(
                settings,
                online_checks=replace(settings.online_checks, online=options["online"]),
            )
        request = JobRequest(
            command,
            source,
            settings,
            output=options.get("output"),
            dry_run=options.get("dry_run", False),
            reference_pdf=options.get("reference_pdf"),
            preview_output=options.get("preview_output"),
        )
        error_console = Console(stderr=True, markup=False, highlight=False)
        progress = None if options["quiet"] else lambda message: error_console.print(Text(message))
        report = asyncio.run(run_job(request, progress, report))
    except KeyboardInterrupt:
        report.outcome = "cancelled"
        report.findings.append(
            Finding(
                "execution.cancelled",
                "Cancelled; no verified output was published",
                "error",
                "inconclusive",
            )
        )
    except (PreparationError, OSError, ValueError) as error:
        report.outcome = "error"
        report.findings.append(Finding("execution.request", str(error), "error", "inconclusive"))
    encoded = json.dumps(report.to_dict(), indent=2, ensure_ascii=False) + "\n"
    if report_destination is not None:
        try:
            with report_destination.open("x", encoding="utf-8") as stream:
                stream.write(encoded)
        except OSError as error:
            report.outcome = "error"
            report.findings.append(
                Finding("report.write", f"Cannot write report: {error}", "error", "failed")
            )
            encoded = json.dumps(report.to_dict(), indent=2, ensure_ascii=False) + "\n"
    if (
        html_destination is not None
        or diagnostic_destination is not None
        or output_format in {"html", "ci"}
    ):
        from .reporting import export_diagnostic_bundle, render_ci_annotations, render_html

        try:
            if html_destination is not None:
                with html_destination.open("x", encoding="utf-8") as stream:
                    stream.write(render_html(report))
            if diagnostic_destination is not None:
                export_diagnostic_bundle(report, diagnostic_destination)
        except (PreparationError, OSError, ValueError) as error:
            report.outcome = "error"
            report.findings.append(Finding("report.write", str(error), "error", "failed"))
            encoded = json.dumps(report.to_dict(), indent=2, ensure_ascii=False) + "\n"
    if output_format == "json":
        click.echo(encoded, nl=False)
    elif output_format == "compact":
        click.echo(render_compact(report, show_passed=options["show_passed"]))
    elif output_format == "html":
        click.echo(render_html(report))
    elif output_format == "ci":
        click.echo(render_ci_annotations(report))
    else:
        print_report(
            report,
            Console(markup=False, highlight=False),
            show_diff=options.get("diff", False),
            show_passed=options["show_passed"],
        )
    return EXIT_CODES[report.outcome]


def main(argv: list[str] | None = None) -> int:
    """Return an exit code for console scripts, embedding, and existing callers."""
    try:
        return cli.main(args=argv, prog_name="latex-prep", standalone_mode=False) or 0
    except click.ClickException as error:
        error.show()
        return error.exit_code
    except (click.Abort, KeyboardInterrupt):
        click.echo("Cancelled; no verified output was published", err=True)
        return 130
