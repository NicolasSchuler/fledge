"""The ``fledge`` command line: commands, options, settings layering and exit codes.

Owns the Click command tree and what a command does around a job: choosing the settings
file, layering the preset, file and command-line options into ``Settings``, running the
job, and turning every failure into a report and an exit code. The options and help of
the commands are in ``cli_options``, the readings of common command-line mistakes in
``cli_inputs``, saving report files in ``report_files``, printing the final report in
``report_formats``, and the commands that are not jobs (``init``, ``doctor``,
``submission-text``) in their own modules.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any

import click

from . import __version__
from .check_selection import resolve_selectors, selected_codes
from .cli_completion import complete_check_codes, complete_check_selectors
from .cli_inputs import check_selection_note, choose_input, require_destination
from .cli_options import (
    ADVANCED_OPTIONS,
    BIBLIOGRAPHY_CHECK_EXAMPLES,
    CHECK_EXAMPLES,
    FORMATTING_EXAMPLES,
    INSPECT_EXAMPLES,
    PDF_CHECK_EXAMPLES,
    PREPARE_EXAMPLES,
    RULES_EXAMPLES,
    JobCommandHelp,
    bibliography_check_options,
    check_options,
    formatting_options,
    inspect_options,
    pdf_check_options,
    prepare_options,
)
from .command_options import (
    debug_option,
    discard_closed_stdout,
    print_debug_traceback,
    record_cancellation,
    record_job_failure,
    reject_config_with_isolated,
)
from .config import SETTING_NAMES, Settings, load_settings
from .config_discovery import resolve_config_path
from .core import run_job
from .doctor_command import doctor_command
from .errors import PreparationError, RequestError, SettingsError
from .execution_records import CONFIG_PATH_KEY, record_settings_source
from .finding_view import CANCELLED_AFTER_PUBLICATION, RenderOptions
from .findings import cancelled_finding
from .init_command import init_command
from .job_context import JobCommand, JobRequest
from .pdf_previews import PdfPreviewStore
from .report import UNFINISHED_SCOPE, USAGE_EXIT_CODE, Outcome, RedactedReport, Report
from .report_files import ReportFiles, resolve_report_files, save_reports
from .report_formats import (
    OutputFormat,
    annotation_prefix,
    working_directory_path,
    write_redacted_report,
)
from .rule_catalogue_view import print_rule_explanation, print_rule_table, rule_payload
from .rule_definition import RULE_SCHEMA_VERSION
from .rules import BY_CODE, get_rule
from .submission_text_command import submission_text_command
from .terminal_console import stderr_console, stdout_console
from .terminal_progress import TerminalProgress

__all__ = [
    "ADVANCED_OPTIONS",
    "COMMAND_ORDER",
    "USAGE_EXIT_CODE",
    "CommandOptions",
    "cli",
    "main",
    "settings_suggestion",
]

# The commands in the order a paper meets them: check it, prepare it, look closer at one
# part, look up the checks, then set up. Help and shell completion list them this way.
COMMAND_ORDER = (
    "inspect",
    "check",
    "prepare",
    "bib",
    "pdf",
    "fmt",
    "submission-text",
    "rules",
    "rule",
    "doctor",
    "init",
)


@contextmanager
def _usage_exit_code() -> Generator[None]:
    """Report invalid command-line syntax with an exit code no outcome uses.

    Raises:
        click.UsageError: A usage error raised inside the block, re-raised with
            ``USAGE_EXIT_CODE`` as its exit code.
    """
    try:
        yield
    except click.UsageError as error:
        # Click declares exit_code as a class variable; the instance value is what it reports.
        setattr(error, "exit_code", USAGE_EXIT_CODE)  # noqa: B010
        raise


class _InternalFailure(click.ClickException):
    """A defect outside every command's own guard: one plain line and the internal-error code.

    Attributes:
        exit_code: The exit code of the internal-error outcome.
    """

    exit_code = Outcome.INTERNAL_ERROR.exit_code


def _internal_failure(error: Exception, during: str = "") -> _InternalFailure:
    """Make the plain message for a defect; ``--debug`` or FLEDGE_DEBUG=1 also prints its traceback.

    Args:
        error: The unexpected error.
        during: Text appended to the error type in the message, such as
            ``" while writing the report"``.

    Returns:
        The exception to show, with the internal-error exit code.
    """
    print_debug_traceback(error)
    return _InternalFailure(
        f"Internal error ({type(error).__name__}){during}. This is a defect in Fledge, not in "
        "your input. Rerun with --debug (or set FLEDGE_DEBUG=1) to print the traceback, and "
        "report it with the command you ran."
    )


class _FledgeGroup(click.Group):
    """Parsing the group's options and invoking a command both report usage errors.

    Attributes:
        command_class: The class of the commands of the group, which splits their help
            into common and advanced options and ends it with examples.
        group_class: ``type``, so that subgroups use this class too.
    """

    command_class = JobCommandHelp
    group_class = type

    def list_commands(self, ctx: click.Context) -> list[str]:
        """List the commands in ``COMMAND_ORDER``, then any others by name.

        Args:
            ctx: The context of the group, unused: the order is fixed.

        Returns:
            The names of the commands of this group.
        """
        del ctx
        ordered = [name for name in COMMAND_ORDER if name in self.commands]
        return ordered + sorted(set(self.commands) - set(ordered))

    def make_context(self, *args: Any, **kwargs: Any) -> click.Context:
        """Parse the options of the group, reporting usage errors with ``USAGE_EXIT_CODE``.

        Args:
            *args: The positional arguments of ``click.Group.make_context``.
            **kwargs: The keyword arguments of ``click.Group.make_context``.

        Returns:
            The new context.
        """
        with _usage_exit_code():
            return super().make_context(*args, **kwargs)

    def invoke(self, ctx: click.Context) -> Any:
        """Invoke the selected command, turning an unguarded defect into an internal failure.

        Usage errors are reported with ``USAGE_EXIT_CODE``. Click's own exceptions, exits
        and aborts and a broken pipe pass through unchanged.

        Args:
            ctx: The context of the group.

        Returns:
            What the command returned.

        Raises:
            _InternalFailure: The command raised any other exception.
        """
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
    """Exit with the code that the command returned, or 0 when it returned none.

    Args:
        result: The exit code that the command returned.
        **_kwargs: The parameters of the group, unused.
    """
    click.get_current_context().exit(result or 0)


@cli.command(
    "inspect",
    short_help="Check sources and bibliography without running TeX.",
    examples=INSPECT_EXAMPLES,
)
@inspect_options
def inspect_command(**options) -> int:
    """Run source, manuscript and bibliography checks without running TeX."""
    return _execute(JobCommand.INSPECT, options)


@cli.command(
    "check", short_help="Build the PDF and check the sources and the PDF.", examples=CHECK_EXAMPLES
)
@check_options
def check_command(**options) -> int:
    """Run source checks, build the PDF in a separate restricted workspace and check the PDF."""
    return _execute(JobCommand.CHECK, options)


@cli.command(
    "prepare",
    short_help="Create a verified submission copy of the paper.",
    examples=PREPARE_EXAMPLES,
)
@prepare_options
def prepare_command(**options) -> int:
    """Package the selected paper's needed inputs and explicit deliverables.

    Unrelated files are omitted from the copy; originals stay unchanged. Complete
    dependency evidence and an exact archive rebuild are required. Ship extra
    files with [package] include = [...] in project configuration.
    """
    return _execute(JobCommand.PREPARE, options)


@cli.group("bib", short_help="Bibliography checks (offline unless you allow online).")
def bibliography() -> None:
    """Bibliography checks, offline unless explicitly enabled."""


@bibliography.command("check", examples=BIBLIOGRAPHY_CHECK_EXAMPLES)
@bibliography_check_options
def bibliography_check(**options) -> int:
    """Check syntax, citation use and configured fields; online evidence is opt-in."""
    return _execute(JobCommand.BIBLIOGRAPHY, options)


@cli.command(
    "fmt",
    short_help="Check tex-fmt formatting without changing any file.",
    examples=FORMATTING_EXAMPLES,
)
@formatting_options
def formatting(**options) -> int:
    """Read-only tex-fmt check; show differences without editing originals."""
    return _execute(JobCommand.FORMAT, options)


@cli.group("pdf", short_help="Check a PDF without its source project.")
def pdf_group() -> None:
    """Inspect a PDF independently of a source project."""


@pdf_group.command("check", examples=PDF_CHECK_EXAMPLES)
@pdf_check_options
def pdf_check(**options) -> int:
    """Check PDF constraints; --reference-pdf adds an exact comparison."""
    return _execute(JobCommand.PDF, options)


cli.add_command(init_command)
cli.add_command(doctor_command)
cli.add_command(submission_text_command)


def _chosen_rules(selectors: tuple[str, ...]) -> list[str]:
    """List the check codes that the selectors of ``fledge rules`` choose, sorted by code.

    Args:
        selectors: Families, code prefixes, codes or rule names, in any case; none chooses
            every check.

    Returns:
        The chosen codes in code order.

    Raises:
        click.BadParameter: A selector matches no check.
    """
    try:
        resolved = resolve_selectors(selectors, "fledge rules") if selectors else ("ALL",)
    except PreparationError as error:
        raise click.BadParameter(str(error), param_hint="SELECTOR") from error
    return sorted({code for selector in resolved for code in selected_codes(selector)})


@cli.command(
    "rules",
    short_help="List the checks with their codes, titles and when they run.",
    examples=RULES_EXAMPLES,
)
@click.argument(
    "selectors",
    nargs=-1,
    metavar="[SELECTOR]...",
    shell_complete=complete_check_selectors,
)
@click.option("--json", "json_output", is_flag=True, help="Print the listed checks as JSON.")
@debug_option
def rules_command(selectors: tuple[str, ...], *, json_output: bool) -> int:
    """List the checks by code, with their titles and whether they run by default.

    SELECTOR limits the list to a family such as TEX, a code prefix such as BIB1, or one
    code, in any case. Opt-in checks run only once their setting is configured.
    """
    rules = [BY_CODE[code] for code in _chosen_rules(selectors)]
    defaults = Settings()
    if json_output:
        payload = {
            "schema_version": RULE_SCHEMA_VERSION,
            "rules": [rule_payload(rule, defaults) for rule in rules],
        }
        click.echo(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print_rule_table(rules, stdout_console(), defaults=defaults)
    return 0


@cli.command("rule", short_help="Explain one check by its code.")
@click.argument("code", shell_complete=complete_check_codes)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Print the explanation as JSON, including the tests that demonstrate the check.",
)
@debug_option
def rule_command(code: str, *, json_output: bool) -> int:
    """Explain the check with code CODE: its scope, limits and suggested fix."""
    try:
        definition = get_rule(code)
    except ValueError as error:
        raise click.BadParameter(str(error), param_hint="CODE") from error
    defaults = Settings()
    if json_output:
        payload = {"schema_version": RULE_SCHEMA_VERSION, **rule_payload(definition, defaults)}
        click.echo(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print_rule_explanation(definition, stdout_console(), defaults=defaults)
    return 0


@dataclass(kw_only=True, slots=True)
class CommandOptions:
    """The options of a job command; options another command lacks keep their defaults.

    An option that sets a setting and is ``None`` leaves that setting alone; see
    :meth:`setting_overrides`.

    Attributes:
        command: The job command.
        source: The input: a source folder or ZIP file, or a PDF for ``pdf check``.
        config: The value of ``--config``.
        isolated: Whether ``--isolated`` was given.
        json_output: Whether ``--json`` was given.
        output_format: The value of ``--output-format``.
        html_report: The value of ``--html-report``.
        preview_output: The value of ``--preview-output``.
        diagnostics: The value of ``--diagnostics``.
        report: The value of ``--json-report``.
        quiet: Whether ``--quiet`` was given.
        show_passed: Whether ``--show-passed`` was given.
        show_files: Whether ``--show-files`` was given.
        jobs: The value of ``--jobs``.
        build_jobs: The value of ``--build-jobs``.
        render_jobs: The value of ``--render-jobs``.
        job_timeout_seconds: The value of ``--job-timeout-seconds``.
        timeout_seconds: The value of ``--timeout-seconds``.
        select_checks: The selectors given with ``--select``.
        ignore_checks: The selectors given with ``--ignore``.
        preset: The value of ``--preset``.
        online: ``True`` for ``--online``, ``False`` for ``--offline``, ``None`` when neither was
            given.
        main: The value of ``--main``.
        engine: The value of ``--engine``.
        bibliography_backend: The value of ``--bibliography-backend``.
        reference_pdf: The value of ``--reference-pdf``.
        max_pages: The value of ``--max-pages``.
        output: The value of ``--output``.
        layout: The value of ``--layout``.
        apply_formatting: ``True`` for ``--format``, ``False`` for ``--no-format``, ``None`` when
            neither was given.
        normalize_doi: ``True`` for ``--normalize-doi``, ``False`` for ``--no-normalize-doi``,
            ``None`` when neither was given.
        cleanup: ``True`` for ``--cleanup``, ``False`` for ``--no-cleanup``, ``None`` when neither
            was given.
        dry_run: Whether ``--dry-run`` was given.
        diff: Whether ``--diff`` was given.
        force: Whether ``--force`` was given.
    """

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
    diff: bool = False
    force: bool = False

    def setting_overrides(self) -> dict[str, object]:
        """Collect the settings given on the command line.

        An omitted option leaves its setting alone.

        Returns:
            The value of each given option that sets the top-level setting of the same
            name, by setting name.
        """
        return {
            name: value for name in _SETTING_OPTIONS if (value := getattr(self, name)) is not None
        }

    def output_choice(self) -> OutputFormat:
        """Choose the format of the final report.

        Returns:
            JSON for ``--json``, otherwise the ``--output-format`` choice, which defaults
            to the terminal format.

        Raises:
            click.UsageError: ``--json`` and ``--output-format`` do not agree.
        """
        if not self.json_output:
            return OutputFormat(self.output_format or OutputFormat.TERMINAL)
        if self.output_format not in {None, "json"}:
            raise click.UsageError("--json conflicts with the chosen --output-format")
        return OutputFormat.JSON


# The job commands that take --main, and so may take a .tex file as INPUT.
_COMMANDS_WITH_MAIN = frozenset(
    {JobCommand.INSPECT, JobCommand.CHECK, JobCommand.PREPARE, JobCommand.BIBLIOGRAPHY}
)
# Command options that set the top-level setting of the same name.
_SETTING_OPTIONS = tuple(item.name for item in fields(CommandOptions) if item.name in SETTING_NAMES)
# Settings whose command-line option is not the setting name in kebab case.
_SETTING_FLAGS = {"apply_formatting": "--format"}


def _check_input(options: CommandOptions) -> None:
    """Raise a request error with command-specific advice when the input does not exist."""
    source = options.source
    if not source.exists() and not source.is_symlink():
        raise RequestError(
            f"Input does not exist: {source}",
            "Pass the path of an existing PDF file; check the spelling and the current directory."
            if options.command is JobCommand.PDF
            else "Pass the path of the paper's LaTeX source folder or ZIP file; check the "
            "spelling and the current directory.",
        )


def settings_suggestion(
    names: tuple[str, ...],
    overrides: dict[str, object],
    config_path: Path | None,
    preset: str | None,
) -> str:
    """Name where each setting at fault in a validation error was supplied.

    Args:
        names: The settings at fault.
        overrides: The settings given on the command line.
        config_path: The settings file in use, if any.
        preset: The preset in use, if any.

    Returns:
        A next step that names the command-line option, settings file or preset of each
        setting at fault; when none can be named, one that asks to correct the settings
        file or the options.
    """
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
    """Layer the preset, the selected settings file and the command-line options.

    ``--select``, ``--ignore`` and ``--online``/``--offline`` are applied last.

    Args:
        options: The options of the command.
        report: The report that records where the settings came from.

    Returns:
        The settings of the job.

    Raises:
        RequestError: A setting or a check selector is invalid.
    """
    overrides = options.setting_overrides()
    config_path = resolve_config_path(options.source, options.config, isolated=options.isolated)
    record_settings_source(
        report.execution,
        config_path=str(config_path) if config_path is not None else None,
        preset=options.preset,
    )
    try:
        settings = load_settings(config_path, overrides, preset=options.preset)
    except RequestError:
        raise
    except PreparationError as error:
        names = error.settings if isinstance(error, SettingsError) else ()
        raise RequestError(
            str(error), settings_suggestion(names, overrides, config_path, options.preset)
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
) -> RedactedReport:
    """Run the job with progress messages on stderr.

    Args:
        options: The options of the command.
        settings: The settings of the job.
        pdf_previews: The store of page previews for an HTML report, or ``None`` when no
            HTML report is wanted.
        report: The report that the job fills in.

    Returns:
        The sanitized snapshot of the finished report.
    """
    request = JobRequest(
        options.command,
        options.source,
        settings,
        output=options.output,
        dry_run=options.dry_run,
        reference_pdf=options.reference_pdf,
        preview_output=options.preview_output,
        pdf_previews=pdf_previews,
        force=options.force,
    )
    with TerminalProgress(stderr_console(), quiet=options.quiet) as progress:
        return asyncio.run(run_job(request, progress, report))


def _shown_settings_file(report: Report) -> str | None:
    """Name the settings file a run uses as a path the user can open, before redaction.

    The job returns its report with local paths generalized; the reader of this run is on
    this machine and is shown the real place, relative to the working directory.

    Returns:
        The path, or ``None`` when the run uses no settings file.
    """
    config_path = report.execution.get(CONFIG_PATH_KEY)
    return working_directory_path(config_path) if isinstance(config_path, str) else None


def _deliver_report(
    report: Report,
    files: ReportFiles,
    output_format: OutputFormat,
    options: CommandOptions,
    pdf_previews: PdfPreviewStore | None,
    settings_file: str | None = None,
    *,
    public: RedactedReport | None = None,
) -> int:
    """Save the requested report files, print the final report and return the exit code.

    A reader that closes stdout early does not change the exit code; a defect while the
    report is written ends in the internal-error exit code.

    Args:
        report: The raw report, including the context needed to redact any write failures.
        files: The report files to save.
        output_format: The format of the printed report.
        options: The options of the command, which choose what the printed report shows.
        pdf_previews: The page previews for an HTML report, if any.
        settings_file: The settings file as the printed report names it, if any.
        public: The sanitized snapshot of a finished job, if the job returned one.

    Returns:
        The exit code of the outcome of the report, or the internal-error exit code after
        a defect.
    """
    preview_images = pdf_previews.images if pdf_previews else None
    outcome = (public if public is not None else report).outcome
    try:
        public, rendered = save_reports(report, files, preview_images, public=public)
        outcome = public.outcome
        write_redacted_report(
            public,
            output_format,
            console=stdout_console(),
            options=RenderOptions(
                show_passed=options.show_passed,
                show_diff=options.diff,
                show_files=options.show_files,
                settings_file=settings_file,
                annotation_prefix=annotation_prefix(options.source),
            ),
            preview_images=preview_images,
            document={OutputFormat.JSON: rendered.json, OutputFormat.HTML: rendered.html}.get(
                output_format
            ),
        )
    except BrokenPipeError:
        # The reader of stdout stopped early, for example `| head`; the verdict is known.
        discard_closed_stdout()
    except Exception as error:  # noqa: BLE001 - a rendering defect ends in the internal-error code
        # A defect while rendering must still end in the internal-error code, never in a pass.
        failure = _internal_failure(error, " while writing the report")
        failure.show()
        return failure.exit_code
    return outcome.exit_code


def _interrupted_report_message(report: Report) -> str:
    """Compose the message for an interruption while the final report was written.

    Returns:
        The message, which says whether the verified output was already published and
        stays in place.
    """
    if report.submission_written:
        return (
            "Cancelled while writing the report; the verified output was already published "
            "and stays in place"
        )
    return "Cancelled; no verified output was published"


def _checked_options(options: CommandOptions) -> CommandOptions:
    """Check the options of a job command and read common mistakes as what was meant.

    A ``.tex`` file given as INPUT becomes its folder and ``--main``, and ``--main``
    written as a path into INPUT becomes relative to it; a note on stderr says so unless
    ``--quiet`` is given. A warning about checks that ``--select`` chose but the command
    cannot run goes to stderr even with ``--quiet``, which silences progress only.

    Args:
        options: The options as Click parsed them.

    Returns:
        The options to run with.

    Raises:
        click.UsageError: ``--config`` was combined with ``--isolated``; INPUT is a
            ``.tex`` file and ``--main`` was given as well; ``prepare`` has neither
            ``--output`` nor ``--dry-run``; or ``--select`` chose only checks that the
            command cannot run.
    """
    reject_config_with_isolated(options.config, isolated=options.isolated)
    if options.command in _COMMANDS_WITH_MAIN:
        choice = choose_input(options.source, options.main)
        options = replace(options, source=choice.source, main=choice.main)
        if choice.note is not None and not options.quiet:
            click.echo(choice.note, err=True)
    if options.command is JobCommand.PREPARE:
        require_destination(output=options.output, dry_run=options.dry_run)
    warning = check_selection_note(options.command, options.select_checks, dry_run=options.dry_run)
    if warning is not None:
        click.echo(warning, err=True)
    return options


def _execute(command: JobCommand, arguments: dict[str, Any]) -> int:
    """Run a job command and deliver its report.

    Once the options are checked, a failure or cancellation is recorded in the report
    instead of escaping; an interruption while the report is delivered ends in the
    cancelled exit code.

    Args:
        command: The job command.
        arguments: The options that Click parsed, by parameter name.

    Returns:
        The exit code.

    Raises:
        click.UsageError: ``--config`` was combined with ``--isolated``, ``--json`` does
            not agree with ``--output-format``, or the command line asks for something the
            command cannot do (see :func:`_checked_options`).
    """
    options = _checked_options(CommandOptions(command=command, **arguments))
    output_format = options.output_choice()
    report = Report(command.value, scope=UNFINISHED_SCOPE)
    public = None
    files = ReportFiles()
    pdf_previews = None
    job_started = False
    settings_file = None
    try:
        resolve_report_files(
            options.source,
            json=options.report,
            html=options.html_report,
            diagnostics=options.diagnostics,
            files=files,
            force=options.force,
            output=options.output,
            preview_output=options.preview_output,
        )
        _check_input(options)
        settings = _load_command_settings(options, report)
        settings_file = _shown_settings_file(report)
        if files.html is not None or output_format is OutputFormat.HTML:
            pdf_previews = PdfPreviewStore()
        job_started = True
        public = _run(options, settings, pdf_previews, report)
    except (KeyboardInterrupt, asyncio.CancelledError):
        # Publication clears staged artifacts on rollback but keeps them after commit.
        message = (
            CANCELLED_AFTER_PUBLICATION
            if report.submission_written
            else "Cancelled; no verified output was published"
        )
        record_cancellation(report, cancelled_finding(message))
    except Exception as error:  # noqa: BLE001 - recorded as a job failure with its cause
        record_job_failure(
            report,
            error,
            "Correct the input, option or setting named above, then rerun the command.",
            job_started=job_started,
        )
    try:
        return _deliver_report(
            report, files, output_format, options, pdf_previews, settings_file, public=public
        )
    except KeyboardInterrupt:
        # The job has finished; whatever it published stays, so the message must say so.
        click.echo(_interrupted_report_message(public if public is not None else report), err=True)
        return Outcome.CANCELLED.exit_code


def main(argv: list[str] | None = None) -> int:
    """Return an exit code for console scripts, embedding, and existing callers.

    Whatever goes wrong, the code is never one that means success: a defect in Fledge is
    exit code 70, distinct from 4, which means that Fledge could not check the input.

    Args:
        argv: The command-line arguments without the program name; ``None`` reads them from
            ``sys.argv``.

    Returns:
        The exit code; 130 when the user cancelled, and 0 when the reader closed stdout
        after the command had finished.
    """
    try:
        return cli.main(args=argv, prog_name="fledge", standalone_mode=False) or 0
    except click.ClickException as error:
        error.show()
        return error.exit_code
    except (click.Abort, KeyboardInterrupt):
        # Outside a job's report boundary, the state of publication is unknown.
        click.echo("Cancelled", err=True)
        return 130
    except BrokenPipeError:
        # Output the reader no longer wants; the command itself had finished.
        discard_closed_stdout()
        return 0
    except Exception as error:  # noqa: BLE001 - last boundary: a defect becomes exit code 70
        failure = _internal_failure(error)
        failure.show()
        return failure.exit_code
