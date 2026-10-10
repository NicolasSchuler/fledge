"""Click interface with Rich terminal reports and plain compact/JSON output."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.text import Text

from . import __version__
from .config import RequestError, Settings, load_settings, resolve_config_path
from .core import JobRequest, run_job, validate_destination
from .init_command import init_command
from .models import Finding, PreparationError, Report
from .presentation import REQUEST_SCOPE, print_report, print_rules, render_compact
from .presets import preset_names
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
# Invalid command-line syntax (BSD sysexits EX_USAGE); distinct from every outcome.
USAGE_EXIT_CODE = 64


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
            "--show-passed", is_flag=True, help="Also list passed findings and inventories."
        ),
        # Accepted for compatibility; Fledge never prompts.
        click.option("--non-interactive", is_flag=True, hidden=True),
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


def _preset_option(function):
    return click.option(
        "--preset",
        type=click.Choice(preset_names()),
        help=(
            "Apply an example preset beneath the settings file and command-line options; "
            "a starting point, not a compliance certification (fledge init --list)."
        ),
    )(function)


def _selection_options(function):
    options = [
        click.option(
            "--select",
            "select_checks",
            multiple=True,
            metavar="CODE_OR_PREFIX",
            help=(
                "Run only these checks: a check code (TEX001), a code prefix (TEX, BIB1) or ALL. "
                "Repeatable; replaces checks.select from configuration."
            ),
        ),
        click.option(
            "--ignore",
            "ignore_checks",
            multiple=True,
            metavar="CODE_OR_PREFIX",
            help=(
                "Skip these checks; same syntax as --select. Repeatable; adds to checks.ignore "
                "from configuration."
            ),
        ),
    ]
    for option in reversed(options):
        function = option(function)
    return function


def _build_options(function):
    options = [
        click.option(
            "--engine",
            type=click.Choice(["pdflatex", "xelatex", "lualatex"]),
            help="TeX engine for the isolated build (default pdflatex).",
        ),
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


# Expert knobs listed under "Advanced options" in command help; nothing is removed.
ADVANCED_OPTIONS = frozenset(
    {
        "jobs",
        "build_jobs",
        "render_jobs",
        "job_timeout_seconds",
        "timeout_seconds",
        "preview_output",
        "diagnostics",
        "html_report",
        "report",
        "isolated",
        "bibliography_backend",
        "normalize_doi",
        "cleanup",
    }
)


class _FledgeCommand(click.Command):
    """Split long option lists into common and advanced help sections."""

    def format_options(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        common, advanced = [], []
        for parameter in self.get_params(ctx):
            record = parameter.get_help_record(ctx)
            if record is not None:
                (advanced if parameter.name in ADVANCED_OPTIONS else common).append(record)
        if not advanced:
            super().format_options(ctx, formatter)
            return
        with formatter.section("Common options"):
            formatter.write_dl(common)
        with formatter.section("Advanced options"):
            formatter.write_dl(advanced)


def _command_line_error(error: click.UsageError) -> None:
    # Click declares exit_code as a class default; the instance value is what it reports.
    setattr(error, "exit_code", USAGE_EXIT_CODE)  # noqa: B010


class _FledgeGroup(click.Group):
    """Report invalid command-line syntax with an exit code no outcome uses."""

    command_class = _FledgeCommand
    group_class = type

    def make_context(self, *args: Any, **kwargs: Any) -> click.Context:
        try:
            return super().make_context(*args, **kwargs)
        except click.UsageError as error:
            _command_line_error(error)
            raise

    def invoke(self, ctx: click.Context) -> Any:
        try:
            return super().invoke(ctx)
        except click.UsageError as error:
            _command_line_error(error)
            raise


@click.group(
    name="fledge", cls=_FledgeGroup, context_settings={"help_option_names": ["-h", "--help"]}
)
@click.version_option(__version__, prog_name="fledge")
def cli() -> None:
    """Fledge checks and prepares a separate LaTeX submission copy.

    INPUT is a source folder or ZIP. Originals are never edited. Each check has a
    stable code such as TEX001; 'fledge rules' lists the checks and 'fledge rule
    CODE' explains one. Reports list findings, the results of the checks, each with
    its code and a suggested next step. Choose checks with [checks] in project TOML
    configuration or with --select and --ignore; --config selects an explicit file.
    """


@cli.result_callback()
def _exit(result: int | None, **_kwargs) -> None:
    click.get_current_context().exit(result or 0)


@cli.command("inspect")
@_common
@_selection_options
@_preset_option
@_online_option
@click.option("--main", help="Relative path of the selected document.")
def inspect_command(**options) -> int:
    """Run source, manuscript and bibliography checks without running TeX."""
    return _execute("inspect", options)


@cli.command("check")
@_common
@_selection_options
@_preset_option
@_online_option
@click.option("--main", help="Relative path of the selected document.")
@_build_options
def check_command(**options) -> int:
    """Run source checks, build the PDF in isolation and check the PDF."""
    return _execute("check", options)


@cli.command("prepare")
@_common
@_selection_options
@_preset_option
@_online_option
@click.option("--main", help="Relative path of the selected document.")
@_build_options
@click.option("--output", type=click.Path(path_type=Path), help="New directory outside the input.")
@click.option(
    "--layout",
    type=click.Choice(["preserve", "flat"]),
    help="Keep the folder structure (preserve, default) or move every file to the top level.",
)
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
    dependency evidence and an exact archive rebuild are required. Ship extra
    files with [package] include = [...] in project configuration.
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
    """Check PDF constraints; --reference-pdf adds an exact comparison."""
    return _execute("pdf", options)


cli.add_command(init_command)


@cli.command("rules")
@click.option("--json", "json_output", is_flag=True, help="Print every check as JSON.")
def rules_command(json_output: bool) -> int:
    """List every check with its stable code and title."""
    if json_output:
        click.echo(json.dumps([rule.to_dict() for rule in RULES], indent=2, ensure_ascii=False))
    else:
        print_rules(RULES, Console(markup=False, highlight=False))
    return 0


@cli.command("rule")
@click.argument("code")
@click.option("--json", "json_output", is_flag=True, help="Print the explanation as JSON.")
def rule_command(code: str, json_output: bool) -> int:
    """Explain the check with code CODE: its scope, limits, suggested fix and tests."""
    try:
        definition = get_rule(code)
    except ValueError as error:
        raise click.BadParameter(str(error), param_hint="CODE") from error
    if json_output:
        click.echo(json.dumps(definition.to_dict(), indent=2, ensure_ascii=False))
    else:
        print_rules((definition,), Console(markup=False, highlight=False), explain=True)
    return 0


def _destination(source: Path, destination: Path, flag: str) -> Path:
    """Validate a new output path and name the option to change when it is unusable."""
    try:
        return validate_destination(source, destination)
    except PreparationError as error:
        message = str(error)
        if message.startswith("Output already exists"):
            fix = f"Choose a new {flag} path, or remove {destination} if it is no longer needed."
        elif message.startswith("Output must be outside"):
            fix = f"Choose a {flag} path outside the input project; originals are never written."
        else:
            fix = f"Create the parent directory first, or choose a {flag} path in an existing one."
        raise RequestError(message, fix) from error


def _settings_suggestion(
    message: str, overrides: dict[str, object], config_path: Path | None, preset: str | None
) -> str:
    """Name where each setting mentioned in a validation error was supplied."""
    mentioned = sorted(
        (match.start(), name)
        for name in Settings.__dataclass_fields__
        if (match := re.search(rf"(?<![\w.]){name}(?!\w)", message))
    )
    sources = []
    for _, name in mentioned:
        if name in overrides:
            sources.append(f"--{name.replace('_', '-')} on the command line")
        elif config_path is not None and preset is not None:
            sources.append(f"{name} in {config_path} or the {preset} preset")
        elif config_path is not None:
            sources.append(f"{name} in {config_path}")
        elif preset is not None:
            sources.append(f"{name} in the {preset} preset")
    if sources:
        return f"Change {'; '.join(sources)}, then rerun the command."
    if config_path is not None:
        return f"Correct the settings in {config_path}, then rerun the command."
    return "Correct the options named above, then rerun the command."


def _execute(command: str, options: dict[str, Any]) -> int:
    if options.get("isolated") and options.get("config") is not None:
        raise click.UsageError("--isolated cannot be combined with --config")
    output_format = options.get("output_format") or "terminal"
    if options["json_output"]:
        if options.get("output_format") not in {None, "json"}:
            raise click.UsageError("--json conflicts with the chosen --output-format")
        output_format = "json"
    report = Report(command, scope=REQUEST_SCOPE)
    report_destination = None
    html_destination = diagnostic_destination = None
    source = options["source"]
    try:
        if options["report"]:
            report_destination = _destination(source, options["report"], "--report")
        if options.get("html_report"):
            html_destination = _destination(source, options["html_report"], "--html-report")
        if options.get("diagnostics"):
            diagnostic_destination = _destination(source, options["diagnostics"], "--diagnostics")
        if not source.exists() and not source.is_symlink():
            raise RequestError(
                f"Input does not exist: {source}",
                "Pass the path of an existing PDF file; check the spelling and the current "
                "directory."
                if command == "pdf"
                else "Pass the path of the paper's LaTeX source folder or ZIP file; check the "
                "spelling and the current directory.",
            )
        # The job validates these again; checking here names the option at fault.
        for name in ("output", "preview_output"):
            if options.get(name) is not None:
                _destination(source, options[name], "--" + name.replace("_", "-"))
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
        report.execution["preset"] = options.get("preset")
        try:
            settings = load_settings(config_path, overrides, preset=options.get("preset"))
        except RequestError:
            raise
        except PreparationError as error:
            raise RequestError(
                str(error),
                _settings_suggestion(str(error), overrides, config_path, options.get("preset")),
            ) from error
        select, ignore = options.get("select_checks", ()), options.get("ignore_checks", ())
        if select or ignore:
            try:
                checks = settings.checks.with_command_line(tuple(select), tuple(ignore))
            except PreparationError as error:
                raise RequestError(
                    str(error),
                    "Correct the selector and rerun the command; fledge rule CODE explains "
                    "a single check.",
                ) from error
            settings = replace(settings, checks=checks)
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
        report.scope = REQUEST_SCOPE
        report.findings.append(
            Finding(
                "execution.request",
                str(error),
                "error",
                "inconclusive",
                suggestion=error.suggestion
                if isinstance(error, RequestError)
                else "Correct the input, option or setting named above, then rerun the command.",
            )
        )
    except Exception as error:
        # A defect must still yield a machine-readable error report and exit code.
        report.outcome = "error"
        report.findings.append(
            Finding(
                "execution.internal",
                f"Internal error ({type(error).__name__}): {error}",
                "error",
                "inconclusive",
                suggestion="Report this defect with the command and the JSON report.",
            )
        )
    from .reporting import export_diagnostic_bundle, render_ci_annotations, render_html

    # Write the HTML and diagnostics first, so a failure is reflected in the saved JSON.
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
    if output_format == "json":
        click.echo(encoded, nl=False)
    elif output_format == "compact":
        click.echo(render_compact(report, show_passed=options["show_passed"]))
    elif output_format == "html":
        click.echo(render_html(report))
    elif output_format == "ci":
        click.echo(render_ci_annotations(report, show_passed=options["show_passed"]))
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
        return cli.main(args=argv, prog_name="fledge", standalone_mode=False) or 0
    except click.ClickException as error:
        error.show()
        return error.exit_code
    except (click.Abort, KeyboardInterrupt):
        click.echo("Cancelled; no verified output was published", err=True)
        return 130
