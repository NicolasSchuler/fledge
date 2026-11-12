"""The options, help layout and examples of the ``fledge`` commands.

Owns what each job command accepts and how its help reads: one decorator per option,
the options of each command in the order its help lists them (the input and the main
file first, expert settings last), the split into common and advanced options, and the
examples at the end of the help. A command offers only the options that change what it
does; ``inspect``, for example, builds nothing, so it has no build or render limits.
What a command does with its options is in ``cli``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import click

from .cli_completion import complete_check_selectors
from .command_options import (
    compose_options,
    debug_option,
    main_option,
    quiet_option,
    settings_file_options,
)
from .config import MAX_CONCURRENCY, MAX_DEFAULT_JOBS, MAX_DEFAULT_RENDER_JOBS, Settings
from .presets import preset_names
from .report_formats import OutputFormat
from .setting_choices import BIBLIOGRAPHY_BACKENDS, ENGINE_SETTINGS, LAYOUTS

# The defaults that option help quotes, read from the settings so help cannot drift. The
# parallelism defaults follow the CPU count, so help describes them instead of quoting.
_DEFAULTS = Settings()
# How wide the command column of the examples may grow before its text wraps below it.
_EXAMPLE_COLUMN_WIDTH = 48

# Expert settings, listed under "Advanced options" in command help; nothing is removed.
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
        "force",
        "isolated",
        "bibliography_backend",
        "normalize_doi",
        "cleanup",
    }
)


class FledgeCommand(click.Command):
    """A command whose help ends with examples, and may list expert options separately.

    Args:
        *args: The positional arguments of ``click.Command``.
        examples: Example command lines, each with a short explanation.
        **kwargs: The keyword arguments of ``click.Command``.

    Attributes:
        advanced_options: The parameter names listed under "Advanced options"; with none
            of them among its options, the help has one "Options" section.
        examples: The example command lines, each with a short explanation.
    """

    advanced_options: frozenset[str] = frozenset()

    def __init__(self, *args: Any, examples: tuple[tuple[str, str], ...] = (), **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.examples = examples

    def format_options(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        """Write the option help; with an advanced option, in common and advanced sections.

        Args:
            ctx: The context of the command.
            formatter: The help formatter that receives the sections.
        """
        common, advanced = [], []
        for parameter in self.get_params(ctx):
            # The command's text describes its arguments, as in Click's own option
            # section; since Click 8.5.1 arguments return help records too.
            if isinstance(parameter, click.Argument):
                continue
            record = parameter.get_help_record(ctx)
            if record is not None:
                (advanced if parameter.name in self.advanced_options else common).append(record)
        if not advanced:
            super().format_options(ctx, formatter)
            return
        with formatter.section("Common options"):
            formatter.write_dl(common)
        with formatter.section("Advanced options"):
            formatter.write_dl(advanced)

    def format_epilog(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        """Write the epilog, then the examples as a section of their own.

        Args:
            ctx: The context of the command.
            formatter: The help formatter that receives the section.
        """
        super().format_epilog(ctx, formatter)
        if self.examples:
            with formatter.section("Examples"):
                formatter.write_dl(list(self.examples), col_max=_EXAMPLE_COLUMN_WIDTH)


class JobCommandHelp(FledgeCommand):
    """A command of the ``fledge`` group, whose help lists ``ADVANCED_OPTIONS`` separately.

    Attributes:
        advanced_options: ``ADVANCED_OPTIONS``.
    """

    advanced_options = ADVANCED_OPTIONS


# The input of every command that reads a paper, and the PDF that `pdf check` reads.
input_argument = click.argument("source", type=click.Path(path_type=Path), metavar="INPUT")
pdf_argument = click.argument("source", type=click.Path(path_type=Path), metavar="PDF")

output_option = click.option(
    "-o",
    "--output",
    type=click.Path(path_type=Path),
    metavar="DIR",
    help="Folder for the verified submission copy: new or empty, and outside INPUT. "
    "Required unless --dry-run is given.",
)
preset_option = click.option(
    "--preset",
    type=click.Choice(preset_names()),
    help=(
        "Apply an example preset beneath the settings file and command-line options; "
        "a starting point, not a compliance certification (fledge init --list)."
    ),
)
select_option = click.option(
    "--select",
    "select_checks",
    multiple=True,
    metavar="CODE_OR_PREFIX",
    shell_complete=complete_check_selectors,
    help=(
        "Run only these checks: a check code (TEX001), a code prefix (TEX, BIB1) or ALL, "
        "in any case. Repeatable; replaces checks.select from configuration."
    ),
)
ignore_option = click.option(
    "--ignore",
    "ignore_checks",
    multiple=True,
    metavar="CODE_OR_PREFIX",
    shell_complete=complete_check_selectors,
    help=(
        "Skip these checks; same syntax as --select. Repeatable; adds to checks.ignore "
        "from configuration."
    ),
)
online_option = click.option(
    "--online/--offline",
    default=None,
    help="Allow configured DOI/URL checks to send public queries; offline by default.",
)
engine_option = click.option(
    "--engine",
    type=click.Choice(ENGINE_SETTINGS),
    help="TeX engine that builds the PDF; auto follows the engine comment in the main "
    f"file, else uses pdflatex (default {_DEFAULTS.engine}).",
)
bibliography_backend_option = click.option(
    "--bibliography-backend",
    type=click.Choice(BIBLIOGRAPHY_BACKENDS),
    help="Bibliography tool: bibtex, biber, or auto to detect it from the sources "
    f"(default {_DEFAULTS.bibliography_backend}).",
)
built_reference_option = click.option(
    "--reference-pdf",
    type=click.Path(path_type=Path),
    help="PDF that the build must match page by page, for example the version you "
    "already submitted.",
)
# `pdf check` builds nothing, so its reference is compared with the PDF it checks.
checked_reference_option = click.option(
    "--reference-pdf",
    type=click.Path(path_type=Path),
    help="PDF to compare the checked PDF with page by page, for example the version "
    "you already submitted.",
)
max_pages_option = click.option(
    "--max-pages",
    type=click.IntRange(min=1),
    metavar="PAGES",
    help="Total page limit, including references.",
)
layout_option = click.option(
    "--layout",
    type=click.Choice(LAYOUTS),
    help="Keep the folder structure (preserve) or move every file to the top level (flat); "
    f"default {_DEFAULTS.layout}.",
)
format_option = click.option(
    "--format/--no-format",
    "apply_formatting",
    default=None,
    help="Apply tex-fmt to the separate copy.",
)
normalize_doi_option = click.option(
    "--normalize-doi/--no-normalize-doi", default=None, help="Normalize literal DOI prefixes."
)
cleanup_option = click.option(
    "--cleanup/--no-cleanup",
    default=None,
    help="Remove known build debris from the copy before editing; files the paper needs "
    f"are always kept (default {'on' if _DEFAULTS.cleanup else 'off'}).",
)
dry_run_option = click.option(
    "--dry-run",
    is_flag=True,
    help="Check and plan the edits without writing a submission copy; add --diff to see them.",
)
planned_diff_option = click.option(
    "--diff", is_flag=True, help="Show the diff of every planned or applied edit."
)
formatting_diff_option = click.option(
    "--diff", is_flag=True, help="Show proposed formatting patches in terminal output."
)
# Fledge never edits the input in `fmt`, so --check, which the documentation names, adds
# nothing.
formatting_check_option = click.option(
    "--check", is_flag=True, expose_value=False, help="Check only (also the default)."
)
force_option = click.option(
    "--force",
    is_flag=True,
    help="Replace an output or preview folder that Fledge wrote earlier, once the new run "
    "has succeeded; any other existing folder is still refused.",
)
json_option = click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Print the report as JSON (the same as --output-format json).",
)
output_format_option = click.option(
    "--output-format",
    type=click.Choice([item.value for item in OutputFormat]),
    help="How to print the report: terminal (default), compact records for scripts and "
    "agents, JSON, an HTML page, or CI annotations.",
)
show_passed_option = click.option(
    "--show-passed", is_flag=True, help="Also list passed findings and inventories."
)
show_files_option = click.option(
    "--show-files", is_flag=True, help="List every included and excluded submission file."
)
html_report_option = click.option(
    "--html-report",
    type=click.Path(path_type=Path),
    help="Also save the report as an HTML page that opens offline, in a new file outside "
    "the input.",
)
preview_output_option = click.option(
    "--preview-output",
    type=click.Path(path_type=Path),
    metavar="DIR",
    help="Folder for the grayscale page previews that the settings ask for: new or empty, "
    "and outside the input.",
)
diagnostics_option = click.option(
    "--diagnostics",
    type=click.Path(path_type=Path),
    help="Save a ZIP of the redacted report and diffs to share when asking for help; it is "
    "not a verified submission.",
)
report_option = click.option(
    "--json-report",
    "--report",
    "report",
    type=click.Path(path_type=Path),
    help="Also save the JSON report to a new file outside the input.",
)
jobs_option = click.option(
    "--jobs",
    type=click.IntRange(1, MAX_CONCURRENCY),
    metavar="N",
    help="How many operations may run at once; 1 runs them one after another (default: "
    f"the number of CPUs, at most {MAX_DEFAULT_JOBS}).",
)
build_jobs_option = click.option(
    "--build-jobs",
    type=click.IntRange(1, MAX_CONCURRENCY),
    metavar="N",
    help=f"How many TeX builds may run at once (default {_DEFAULTS.build_jobs}).",
)
render_jobs_option = click.option(
    "--render-jobs",
    type=click.IntRange(1, MAX_CONCURRENCY),
    metavar="N",
    help="How many PDF pages may be rendered at once (default: half the number of CPUs, "
    f"from 1 to {MAX_DEFAULT_RENDER_JOBS}).",
)
job_timeout_option = click.option(
    "--job-timeout-seconds",
    type=click.IntRange(min=1),
    metavar="SECONDS",
    help="Longest the whole run may take, in seconds, including time spent waiting "
    f"(default {_DEFAULTS.job_timeout_seconds}).",
)
timeout_option = click.option(
    "--timeout-seconds",
    type=click.IntRange(min=1),
    metavar="SECONDS",
    help="Longest one external tool, such as latexmk, may run, in seconds "
    f"(default {_DEFAULTS.timeout_seconds}).",
)

# How settings are chosen and how the report is shown, for every job command.
_settings_and_report = compose_options(
    settings_file_options,
    json_option,
    output_format_option,
    quiet_option,
    show_passed_option,
)
# The report files every job command can save.
_report_files = compose_options(html_report_option, diagnostics_option, report_option)
# The limits of a run that builds nothing and renders nothing.
_run_limits = compose_options(jobs_option, job_timeout_option, timeout_option)

inspect_options = compose_options(
    input_argument,
    main_option,
    preset_option,
    select_option,
    ignore_option,
    online_option,
    _settings_and_report,
    _report_files,
    _run_limits,
    debug_option,
)
check_options = compose_options(
    input_argument,
    main_option,
    preset_option,
    select_option,
    ignore_option,
    engine_option,
    built_reference_option,
    max_pages_option,
    online_option,
    _settings_and_report,
    _report_files,
    preview_output_option,
    force_option,
    jobs_option,
    build_jobs_option,
    render_jobs_option,
    job_timeout_option,
    timeout_option,
    bibliography_backend_option,
    debug_option,
)
prepare_options = compose_options(
    input_argument,
    output_option,
    main_option,
    preset_option,
    select_option,
    ignore_option,
    engine_option,
    built_reference_option,
    max_pages_option,
    layout_option,
    format_option,
    dry_run_option,
    planned_diff_option,
    online_option,
    _settings_and_report,
    show_files_option,
    _report_files,
    preview_output_option,
    force_option,
    jobs_option,
    build_jobs_option,
    render_jobs_option,
    job_timeout_option,
    timeout_option,
    bibliography_backend_option,
    normalize_doi_option,
    cleanup_option,
    debug_option,
)
bibliography_check_options = compose_options(
    input_argument,
    main_option,
    preset_option,
    select_option,
    ignore_option,
    online_option,
    _settings_and_report,
    _report_files,
    _run_limits,
    debug_option,
)
formatting_options = compose_options(
    input_argument,
    formatting_check_option,
    formatting_diff_option,
    _settings_and_report,
    _report_files,
    _run_limits,
    debug_option,
)
pdf_check_options = compose_options(
    pdf_argument,
    preset_option,
    select_option,
    ignore_option,
    checked_reference_option,
    max_pages_option,
    _settings_and_report,
    _report_files,
    preview_output_option,
    force_option,
    jobs_option,
    render_jobs_option,
    job_timeout_option,
    timeout_option,
    debug_option,
)

INSPECT_EXAMPLES = (
    ("fledge inspect paper", "Check the sources in the folder paper; no TeX needed."),
    ("fledge inspect paper.zip -m main.tex", "Name the main file of a project with several."),
    ("fledge inspect paper --select TEX001", "Run one check; fledge rules lists them all."),
)
CHECK_EXAMPLES = (
    ("fledge check paper", "Build the PDF and check the sources and the PDF."),
    ("fledge check paper --preset arxiv", "Add the checks the arxiv preset switches on."),
    ("fledge check paper --max-pages 10", "Also enforce a page limit."),
)
PREPARE_EXAMPLES = (
    ("fledge prepare paper -o submission", "Write the verified copy into submission."),
    ("fledge prepare paper --dry-run --diff", "Show the planned edits; write nothing."),
    ("fledge prepare paper -o submission --force", "Replace the copy of an earlier run."),
)
BIBLIOGRAPHY_CHECK_EXAMPLES = (
    ("fledge bib check paper", "Check the bibliography offline."),
    ("fledge bib check paper --online", "Allow the DOI and URL lookups you configured."),
)
FORMATTING_EXAMPLES = (("fledge fmt paper --diff", "Show what tex-fmt would change."),)
PDF_CHECK_EXAMPLES = (
    ("fledge pdf check paper.pdf --max-pages 10", "Check the page count of a PDF."),
    ("fledge pdf check new.pdf --reference-pdf old.pdf", "Compare two PDFs page by page."),
)
RULES_EXAMPLES = (
    ("fledge rules TEX", "List the checks whose codes start with TEX."),
    ("fledge rule TEX001", "Explain one check."),
)
