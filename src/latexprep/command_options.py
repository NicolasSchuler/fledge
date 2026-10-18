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
from .models import Finding, Outcome, Report, internal_error_finding, request_error_finding
from .report_formats import REQUEST_SCOPE, UNFINISHED_SCOPE

F = TypeVar("F", bound=Callable[..., object])

DEBUG_ENVIRONMENT_VARIABLE = "FLEDGE_DEBUG"
_DEBUG_CONTEXT_KEY = "fledge.debug"


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


def reject_config_with_isolated(config: Path | None, isolated: bool) -> None:
    if isolated and config is not None:
        raise click.UsageError("--isolated cannot be combined with --config")


def _remember_debug(context: click.Context, _parameter: click.Parameter, value: bool) -> bool:
    if value:
        context.meta[_DEBUG_CONTEXT_KEY] = True
    return value


# Hidden: only a person reporting a defect needs the traceback of an internal error.
debug_option = click.option(
    "--debug",
    is_flag=True,
    hidden=True,
    expose_value=False,
    is_eager=True,
    callback=_remember_debug,
    help="Print the traceback of an internal error to stderr.",
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
    """Write the traceback of ``error`` to stderr when debugging was requested."""
    if debug_enabled():
        click.echo("".join(traceback.format_exception(error)), err=True, nl=False)


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


def record_job_failure(report: Report, error: Exception, request_suggestion: str) -> None:
    """Record why a job stopped: a correctable request error or, otherwise, a defect."""
    if is_expected_failure(error):
        record_request_error(report, error, request_suggestion)
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
