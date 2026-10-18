"""Click interface with Rich terminal reports and plain compact/JSON output."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any, NamedTuple

import click
from rich.console import Console

from . import __version__
from .command_options import (
    compose_options,
    debug_option,
    discard_closed_stdout,
    print_debug_traceback,
    record_cancellation,
    record_internal_error,
    record_job_failure,
    reject_config_with_isolated,
    settings_file_options,
)
from .config import SETTING_NAMES, Settings, load_settings, resolve_config_path
from .core import run_job
from .defects import is_expected_failure
from .diagnostic_bundle import export_diagnostic_bundle
from .doctor_command import doctor_command
from .errors import PreparationError, RequestError, SettingsError
from .finding_view import RenderOptions
from .findings import Finding, cancelled_finding
from .html_report import render_html
from .init_command import init_command
from .job_context import JobCommand, JobRequest
from .pdf_previews import PdfPreviewStore
from .presets import preset_names
from .publication import validate_destination
from .report import UNFINISHED_SCOPE, USAGE_EXIT_CODE, Outcome, Report
from .report_formats import OutputFormat, write_report
from .rules import RULES, get_rule
from .setting_choices import BIBLIOGRAPHY_BACKENDS, ENGINES, LAYOUTS
from .submission_text_command import submission_text_command
from .terminal_progress import TerminalProgress
from .terminal_report import print_rule_explanation, print_rule_table

_common = compose_options(
    click.argument("source", type=click.Path(path_type=Path), metavar="INPUT"),
    settings_file_options,
    debug_option,
    click.option("--json", "json_output", is_flag=True, help="Alias for --output-format json."),
    click.option(
        "--output-format",
        type=click.Choice([item.value for item in OutputFormat]),
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
    click.option("--show-passed", is_flag=True, help="Also list passed findings and inventories."),
    click.option(
        "--show-files", is_flag=True, help="List every included and excluded submission file."
    ),
    # Accepted for compatibility; Fledge never prompts.
    click.option("--non-interactive", is_flag=True, hidden=True),
    click.option("--jobs", type=int, help="Shared CPU slots for all operations; 1 runs serially."),
    click.option("--build-jobs", type=int, help="Maximum concurrent build operations (default 1)."),
    click.option(
        "--render-jobs", type=int, help="Maximum concurrent page-pair renderers (default 1)."
    ),
    click.option(
        "--job-timeout-seconds", type=int, help="Overall job deadline, including queue waits."
    ),
    click.option("--timeout-seconds", type=int, help="Per-tool wall time limit."),
)

_online_option = click.option(
    "--online/--offline",
    default=None,
    help="Allow configured DOI/URL checks to send public queries; offline by default.",
)

_preset_option = click.option(
    "--preset",
    type=click.Choice(preset_names()),
    help=(
        "Apply an example preset beneath the settings file and command-line options; "
        "a starting point, not a compliance certification (fledge init --list)."
    ),
)

_main_option = click.option("--main", help="Relative path of the selected document.")

_selection_options = compose_options(
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
)

_build_options = compose_options(
    click.option(
        "--engine",
        type=click.Choice(ENGINES),
        help="TeX engine for the isolated build (default pdflatex).",
    ),
    click.option(
        "--bibliography-backend",
        type=click.Choice(BIBLIOGRAPHY_BACKENDS),
        help="Select and validate the bibliography backend explicitly.",
    ),
    click.option(
        "--reference-pdf", type=click.Path(path_type=Path), help="Required comparison PDF."
    ),
    click.option("--max-pages", type=int, help="Total page limit, including references."),
)

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


@contextmanager
def _usage_exit_code() -> Generator[None]:
    """Report invalid command-line syntax with an exit code no outcome uses."""
    try:
        yield
    except click.UsageError as error:
        # Click declares exit_code as a class variable; the instance value is what it reports.
        setattr(error, "exit_code", USAGE_EXIT_CODE)  # noqa: B010
        raise


class _InternalFailure(click.ClickException):
    """A defect outside every command's own guard: one plain line and the error exit code."""

    exit_code = Outcome.ERROR.exit_code


def _internal_failure(error: Exception, during: str = "") -> _InternalFailure:
    """The plain message for a defect; ``--debug`` or FLEDGE_DEBUG=1 also prints its traceback."""
    print_debug_traceback(error)
    return _InternalFailure(
        f"Internal error ({type(error).__name__}){during}. This is a defect in Fledge, not in "
        "your input. Rerun with --debug (or set FLEDGE_DEBUG=1) to print the traceback, and "
        "report it with the command you ran."
    )


class _FledgeGroup(click.Group):
    """Parsing the group's options and invoking a command both report usage errors."""

    command_class = _FledgeCommand
    group_class = type

    def make_context(self, *args: Any, **kwargs: Any) -> click.Context:
        with _usage_exit_code():
            return super().make_context(*args, **kwargs)

    def invoke(self, ctx: click.Context) -> Any:
        with _usage_exit_code():
            try:
                return super().invoke(ctx)
            except (click.ClickException, click.exceptions.Exit, click.Abort, BrokenPipeError):
                raise
            except Exception as error:
                # init, rules and the other commands have no guard of their own. Without
                # this, Python would end them with a traceback and exit status 1, the
                # code for "passed with advisories".
                raise _internal_failure(error) from error


@click.group(
    name="fledge", cls=_FledgeGroup, context_settings={"help_option_names": ["-h", "--help"]}
)
@click.version_option(__version__, prog_name="fledge")
@debug_option
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
@_main_option
def inspect_command(**options) -> int:
    """Run source, manuscript and bibliography checks without running TeX."""
    return _execute(JobCommand.INSPECT, options)


@cli.command("check")
@_common
@_selection_options
@_preset_option
@_online_option
@_main_option
@_build_options
def check_command(**options) -> int:
    """Run source checks, build the PDF in isolation and check the PDF."""
    return _execute(JobCommand.CHECK, options)


@cli.command("prepare")
@_common
@_selection_options
@_preset_option
@_online_option
@_main_option
@_build_options
@click.option("--output", type=click.Path(path_type=Path), help="New directory outside the input.")
@click.option(
    "--layout",
    type=click.Choice(LAYOUTS),
    help="Keep the folder structure (preserve, default) or move every file to the top level.",
)
@click.option(
    "--format/--no-format",
    "apply_formatting",
    default=None,
    help="Apply tex-fmt to the separate copy.",
)
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
    return _execute(JobCommand.PREPARE, options)


@cli.group("bib")
def bibliography() -> None:
    """Bibliography checks, offline unless explicitly enabled."""


@bibliography.command("check")
@_common
@_online_option
@click.option("--main", help="Relative path of the document whose citations should be checked.")
def bibliography_check(**options) -> int:
    """Check syntax, citation use and configured fields; online evidence is opt-in."""
    return _execute(JobCommand.BIBLIOGRAPHY, options)


@cli.command("fmt")
@_common
@click.option("--check", is_flag=True, help="Check only (also the default).")
@click.option("--diff", is_flag=True, help="Show proposed formatting patches in terminal output.")
def formatting(**options) -> int:
    """Read-only tex-fmt check; show differences without editing originals."""
    return _execute(JobCommand.FORMAT, options)


@cli.group("pdf")
def pdf_group() -> None:
    """Inspect a PDF independently of a source project."""


@pdf_group.command("check")
@_common
@_build_options
def pdf_check(**options) -> int:
    """Check PDF constraints; --reference-pdf adds an exact comparison."""
    return _execute(JobCommand.PDF, options)


cli.add_command(init_command)
cli.add_command(doctor_command)
cli.add_command(submission_text_command)


@cli.command("rules")
@click.option("--json", "json_output", is_flag=True, help="Print every check as JSON.")
@debug_option
def rules_command(json_output: bool) -> int:
    """List every check with its stable code and title."""
    if json_output:
        click.echo(json.dumps([rule.to_dict() for rule in RULES], indent=2, ensure_ascii=False))
    else:
        print_rule_table(RULES, Console(markup=False, highlight=False))
    return 0


@cli.command("rule")
@click.argument("code")
@click.option("--json", "json_output", is_flag=True, help="Print the explanation as JSON.")
@debug_option
def rule_command(code: str, json_output: bool) -> int:
    """Explain the check with code CODE: its scope, limits, suggested fix and tests."""
    try:
        definition = get_rule(code)
    except ValueError as error:
        raise click.BadParameter(str(error), param_hint="CODE") from error
    if json_output:
        click.echo(json.dumps(definition.to_dict(), indent=2, ensure_ascii=False))
    else:
        print_rule_explanation(definition, Console(markup=False, highlight=False))
    return 0


@dataclass(kw_only=True, slots=True)
class CommandOptions:
    """The options of a job command; options another command lacks keep their defaults."""

    command: JobCommand
    source: Path
    config: Path | None = None
    isolated: bool = False
    json_output: bool = False
    output_format: str | None = None
    html_report: Path | None = None
    preview_output: Path | None = None
    diagnostics: Path | None = None
    report: Path | None = None
    quiet: bool = False
    show_passed: bool = False
    show_files: bool = False
    non_interactive: bool = False
    jobs: int | None = None
    build_jobs: int | None = None
    render_jobs: int | None = None
    job_timeout_seconds: int | None = None
    timeout_seconds: int | None = None
    select_checks: tuple[str, ...] = ()
    ignore_checks: tuple[str, ...] = ()
    preset: str | None = None
    online: bool | None = None
    main: str | None = None
    engine: str | None = None
    bibliography_backend: str | None = None
    reference_pdf: Path | None = None
    max_pages: int | None = None
    output: Path | None = None
    layout: str | None = None
    apply_formatting: bool | None = None
    normalize_doi: bool | None = None
    cleanup: bool | None = None
    dry_run: bool = False
    check: bool = False
    diff: bool = False

    def setting_overrides(self) -> dict[str, object]:
        """The settings given on the command line; an omitted option leaves its setting alone."""
        return {
            name: value for name in _SETTING_OPTIONS if (value := getattr(self, name)) is not None
        }

    def output_choice(self) -> OutputFormat:
        if not self.json_output:
            return OutputFormat(self.output_format or OutputFormat.TERMINAL)
        if self.output_format not in {None, "json"}:
            raise click.UsageError("--json conflicts with the chosen --output-format")
        return OutputFormat.JSON


# Command options that set the top-level setting of the same name.
_SETTING_OPTIONS = tuple(item.name for item in fields(CommandOptions) if item.name in SETTING_NAMES)
# Settings whose command-line option is not the setting name in kebab case.
_SETTING_FLAGS = {"apply_formatting": "--format"}


@dataclass(slots=True)
class _ReportFiles:
    """New report files outside the input; each is set once its option was validated."""

    json: Path | None = None
    html: Path | None = None
    diagnostics: Path | None = None


class _RenderedReport(NamedTuple):
    json: str
    html: str | None


def _resolve_report_files(options: CommandOptions, files: _ReportFiles) -> None:
    source = options.source
    if options.report:
        files.json = validate_destination(source, options.report, "--report")
    if options.html_report:
        files.html = validate_destination(source, options.html_report, "--html-report")
    if options.diagnostics:
        files.diagnostics = validate_destination(source, options.diagnostics, "--diagnostics")


def _check_input_and_outputs(options: CommandOptions, files: _ReportFiles) -> None:
    source = options.source
    if not source.exists() and not source.is_symlink():
        raise RequestError(
            f"Input does not exist: {source}",
            "Pass the path of an existing PDF file; check the spelling and the current directory."
            if options.command is JobCommand.PDF
            else "Pass the path of the paper's LaTeX source folder or ZIP file; check the "
            "spelling and the current directory.",
        )
    # The job validates --output and --preview-output, naming the option at fault.
    destinations = [
        item
        for item in (files.json, files.html, files.diagnostics, options.output)
        if item is not None
    ]
    if len({item.resolve() for item in destinations}) != len(destinations):
        raise PreparationError("Output, JSON, HTML and diagnostics destinations must be distinct")


def _settings_suggestion(
    names: tuple[str, ...],
    overrides: dict[str, object],
    config_path: Path | None,
    preset: str | None,
) -> str:
    """Name where each setting at fault in a validation error was supplied."""
    sources = []
    for name in names:
        if name in overrides:
            flag = _SETTING_FLAGS.get(name, f"--{name.replace('_', '-')}")
            sources.append(f"{flag} on the command line")
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


def _load_command_settings(options: CommandOptions, report: Report) -> Settings:
    """Layer the preset, the selected settings file and the command-line options."""
    overrides = options.setting_overrides()
    config_path = resolve_config_path(options.source, options.config, isolated=options.isolated)
    report.execution["config_path"] = str(config_path) if config_path is not None else None
    report.execution["preset"] = options.preset
    try:
        settings = load_settings(config_path, overrides, preset=options.preset)
    except RequestError:
        raise
    except PreparationError as error:
        names = error.settings if isinstance(error, SettingsError) else ()
        raise RequestError(
            str(error), _settings_suggestion(names, overrides, config_path, options.preset)
        ) from error
    if options.select_checks or options.ignore_checks:
        try:
            checks = settings.checks.with_command_line(options.select_checks, options.ignore_checks)
        except PreparationError as error:
            raise RequestError(
                str(error),
                "Correct the selector and rerun the command; fledge rule CODE explains "
                "a single check.",
            ) from error
        settings = replace(settings, checks=checks)
    if options.online is not None:
        settings = replace(
            settings, online_checks=replace(settings.online_checks, online=options.online)
        )
    return settings


def _run(
    options: CommandOptions,
    settings: Settings,
    pdf_previews: PdfPreviewStore | None,
    report: Report,
) -> Report:
    request = JobRequest(
        options.command,
        options.source,
        settings,
        output=options.output,
        dry_run=options.dry_run,
        reference_pdf=options.reference_pdf,
        preview_output=options.preview_output,
        pdf_previews=pdf_previews,
    )
    error_console = Console(stderr=True, markup=False, highlight=False)
    with TerminalProgress(error_console, quiet=options.quiet) as progress:
        return asyncio.run(run_job(request, progress, report))


def _record_write_failure(report: Report, option: str, destination: Path, error: Exception) -> None:
    """Record that a requested report file could not be saved, in the report itself."""
    if not is_expected_failure(error):
        record_internal_error(report, error)
        return
    reason = error.strerror if isinstance(error, OSError) and error.strerror else str(error)
    report.outcome = Outcome.ERROR
    report.findings.append(
        Finding(
            "report.write",
            f"Cannot write {option} {destination}: {reason}",
            severity="error",
            status="failed",
            suggestion=f"Choose a {option} path in a folder that exists and is writable, with "
            "no file at that path yet, then rerun the command.",
        )
    )


def _write_reports(
    report: Report, files: _ReportFiles, preview_images: dict | None
) -> _RenderedReport:
    """Save the requested report files; a write failure is recorded in the report itself."""
    # Rendered once for the file and reused for stdout unless a later write failure
    # changes the report.
    html_document = None
    # Write the HTML and diagnostics first, so a failure is reflected in the saved JSON.
    if files.html is not None:
        try:
            html_document = render_html(report, preview_images=preview_images)
            with files.html.open("x", encoding="utf-8") as stream:
                stream.write(html_document)
        except Exception as error:
            _record_write_failure(report, "--html-report", files.html, error)
            html_document = None
    if files.diagnostics is not None:
        try:
            export_diagnostic_bundle(report, files.diagnostics)
        except Exception as error:
            _record_write_failure(report, "--diagnostics", files.diagnostics, error)
            html_document = None
    encoded = report.to_json()
    if files.json is not None:
        try:
            with files.json.open("x", encoding="utf-8") as stream:
                stream.write(encoded)
        except Exception as error:
            _record_write_failure(report, "--report", files.json, error)
            encoded = report.to_json()
            html_document = None
    return _RenderedReport(encoded, html_document)


def _deliver_report(
    report: Report,
    files: _ReportFiles,
    output_format: OutputFormat,
    options: CommandOptions,
    pdf_previews: PdfPreviewStore | None,
) -> int:
    """Save the requested report files, print the final report and return the exit code."""
    preview_images = pdf_previews.images if pdf_previews else None
    try:
        rendered = _write_reports(report, files, preview_images)
        write_report(
            report,
            output_format,
            console=Console(markup=False, highlight=False),
            options=RenderOptions(
                show_passed=options.show_passed,
                show_diff=options.diff,
                show_files=options.show_files,
            ),
            preview_images=preview_images,
            document={OutputFormat.JSON: rendered.json, OutputFormat.HTML: rendered.html}.get(
                output_format
            ),
        )
    except BrokenPipeError:
        # The reader of stdout stopped early, for example `| head`; the verdict is known.
        discard_closed_stdout()
    except Exception as error:
        # A defect while rendering must still end in the error exit code, never in a pass.
        failure = _internal_failure(error, " while writing the report")
        failure.show()
        return failure.exit_code
    return report.outcome.exit_code


def _execute(command: JobCommand, arguments: dict[str, Any]) -> int:
    options = CommandOptions(command=command, **arguments)
    reject_config_with_isolated(options.config, options.isolated)
    output_format = options.output_choice()
    report = Report(command.value, scope=UNFINISHED_SCOPE)
    files = _ReportFiles()
    pdf_previews = None
    try:
        _resolve_report_files(options, files)
        _check_input_and_outputs(options, files)
        settings = _load_command_settings(options, report)
        if files.html is not None or output_format is OutputFormat.HTML:
            pdf_previews = PdfPreviewStore()
        report = _run(options, settings, pdf_previews, report)
    except (KeyboardInterrupt, asyncio.CancelledError):
        record_cancellation(
            report, cancelled_finding("Cancelled; no verified output was published")
        )
    except Exception as error:
        record_job_failure(
            report,
            error,
            "Correct the input, option or setting named above, then rerun the command.",
        )
    return _deliver_report(report, files, output_format, options, pdf_previews)


def main(argv: list[str] | None = None) -> int:
    """Return an exit code for console scripts, embedding, and existing callers.

    Whatever goes wrong, the code is never one that means success: a defect is exit
    code 4, the same as any other error.
    """
    try:
        return cli.main(args=argv, prog_name="fledge", standalone_mode=False) or 0
    except click.ClickException as error:
        error.show()
        return error.exit_code
    except (click.Abort, KeyboardInterrupt):
        click.echo("Cancelled; no verified output was published", err=True)
        return 130
    except BrokenPipeError:
        # Output the reader no longer wants; the command itself had finished.
        discard_closed_stdout()
        return 0
    except Exception as error:
        failure = _internal_failure(error)
        failure.show()
        return failure.exit_code
