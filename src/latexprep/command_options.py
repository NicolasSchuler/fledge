"""Click option helpers and failure handling shared by the command adapters."""

from __future__ import annotations

import os
import sys
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

import click

from .defects import REPORT_DEFECT_SUGGESTION, is_expected_failure, summarize_traceback
from .diagnostic_logging import debug_logging, safe_diagnostic_text
from .errors import RequestError
from .findings import Finding, internal_error_finding, request_error_finding
from .report import REQUEST_SCOPE, UNFINISHED_SCOPE, Outcome, Report

F = TypeVar("F", bound=Callable[..., object])

DEBUG_ENVIRONMENT_VARIABLE = "FLEDGE_DEBUG"
_DEBUG_CONTEXT_KEY = "fledge.debug"
# One wording for the option that every command with progress offers.
QUIET_HELP = "Suppress progress messages on stderr; the final report is still printed."


def compose_options(*decorators: Callable[[F], F]) -> Callable[[F], F]:
    """Apply option decorators as if they were stacked in the listed order."""

    def apply(function: F) -> F:
        for decorator in reversed(decorators):
            function = decorator(function)
        return function

    return apply


# --config and --isolated, which select the settings file of every command.
settings_file_options = compose_options(
    click.option(
        "--config",
        type=click.Path(path_type=Path),
        help="Use this TOML settings file instead of discovering project configuration.",
    ),
    click.option(
        "--isolated", is_flag=True, help="Ignore discovered configuration and use defaults."
    ),
)


# The document a command works on, for inputs that hold several.
main_option = click.option(
    "--main",
    metavar="FILE",
    help="Main .tex file of the document, relative to the input; needed when the input "
    "holds several documents.",
)


def reject_config_with_isolated(config: Path | None, *, isolated: bool) -> None:
    if isolated and config is not None:
        raise click.UsageError("--isolated cannot be combined with --config")


def _remember_debug(
    context: click.Context,
    _parameter: click.Parameter,
    value: bool,  # noqa: FBT001 - click passes option callbacks their value positionally
) -> bool:
    """Remember ``--debug`` and, once it or ``FLEDGE_DEBUG`` asks for it, start the log lines."""
    if value:
        context.meta[_DEBUG_CONTEXT_KEY] = True
    if debug_enabled():
        # The handler is removed when the context closes, so a later command that runs in
        # the same process without --debug prints nothing.
        context.with_resource(debug_logging())
    return value


# Hidden: only a person reporting a defect needs the traceback and the log lines.
debug_option = click.option(
    "--debug",
    is_flag=True,
    hidden=True,
    expose_value=False,
    is_eager=True,
    callback=_remember_debug,
    help="Print diagnostic log lines and the traceback of an internal error to stderr.",
)


def debug_enabled() -> bool:
    """Whether ``--debug`` was given to the running command or the environment asks for it."""
    context = click.get_current_context(silent=True)
    if context is not None and context.meta.get(_DEBUG_CONTEXT_KEY):
        return True
    return os.environ.get(DEBUG_ENVIRONMENT_VARIABLE, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def print_debug_traceback(error: BaseException) -> None:
    """Write the traceback of ``error`` to stderr when debugging was requested.

    The text is made safe first, as the log lines are: credentials are redacted, this
    machine's home and temporary directories are shown generically and terminal control
    characters are removed, because a message may quote a path, a URL or a file's content.
    """
    if debug_enabled():
        click.echo(
            safe_diagnostic_text("".join(traceback.format_exception(error))), err=True, nl=False
        )


def record_request_error(report: Report, error: Exception, suggestion: str) -> None:
    """Record a rejected request; a ``RequestError`` supplies its own next step."""
    report.outcome = Outcome.ERROR
    report.scope = REQUEST_SCOPE
    report.findings.append(request_error_finding(error, suggestion))


def record_internal_error(report: Report, error: Exception) -> None:
    """Record a defect with its error type and frames; ``--debug`` also prints the traceback."""
    report.outcome = Outcome.ERROR
    report.findings.append(
        internal_error_finding(
            error,
            suggestion=f"{REPORT_DEFECT_SUGGESTION} Rerun with --debug (or set "
            f"{DEBUG_ENVIRONMENT_VARIABLE}=1) to print the full traceback.",
            frames=summarize_traceback(error),
        )
    )
    print_debug_traceback(error)


def record_job_failure(
    report: Report, error: Exception, request_suggestion: str, *, job_started: bool = False
) -> None:
    """Record why a job stopped: a correctable request error or, otherwise, a defect.

    An expected failure before the job started, or a ``RequestError`` at any time, rejects
    the request. Any other expected failure after ``job_started``, such as a full disk
    while publishing, means the run did not finish; the request was not at fault.
    """
    if is_expected_failure(error):
        record_request_error(report, error, request_suggestion)
        if job_started and not isinstance(error, RequestError):
            report.scope = UNFINISHED_SCOPE
    else:
        record_internal_error(report, error)
        report.scope = UNFINISHED_SCOPE


def record_cancellation(report: Report, finding: Finding) -> None:
    """Record that the user stopped the run; whatever it verified so far is incomplete."""
    report.outcome = Outcome.CANCELLED
    report.scope = UNFINISHED_SCOPE
    report.findings.append(finding)


def discard_closed_stdout() -> None:
    """Point stdout at the null device after a reader closed the pipe.

    Python flushes stdout again at exit; without this, that flush reports the same
    broken pipe once more after the command has already finished.
    """
    try:
        stdout_descriptor = sys.stdout.fileno()
    except (OSError, ValueError):  # no real file descriptor, as under a test runner
        return
    null = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(null, stdout_descriptor)
    finally:
        os.close(null)
