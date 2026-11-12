"""Reading what a tool printed when asked for its version.

A version probe succeeds only when the tool exited normally (or, for ``pdfdetach -v``, with
its version status), within its limits and printed something; the report keeps the first
lines. Running the probe is ``runtime``'s work.
"""

from __future__ import annotations

import re
from pathlib import Path

from .command_runner import CommandResult, ToolVersion
from .errors import PreparationError
from .resource_monitor import limit_advice

_PDFDETACH_VERSION = re.compile(r"^pdfdetach version \S+")
# pdfdetach -v prints its version and then exits with this status.
_PDFDETACH_VERSION_EXIT_STATUS = 99
# Lines of a tool's version output that a report keeps: the banner and what follows it.
_VERSION_LINES_KEPT = 3


def _unverified_version_message(tool: str, result: CommandResult) -> str:
    """Explain why ``tool`` gave no usable version, without its location or its raw diagnostics.

    Args:
        tool: The tool's name.
        result: How the version command ended.

    Returns:
        The message, with the cause and what to do next.
    """
    advice = f"Run {tool} on its own to see why, or reinstall it, then try again."
    if result.timed_out:
        cause = "it did not report its version within the time limit"
    elif result.output_limited:
        cause = "it printed more than the output limit while reporting its version"
    elif result.resource_exceeded:
        cause = (
            f"it exceeded a resource limit while reporting its version ({result.resource_exceeded})"
        )
        advice = limit_advice(result.resource_exceeded) or advice
    elif result.returncode:
        cause = f"it exited with status {result.returncode} when asked for its version"
    elif "Error:" in result.stderr:
        cause = "it reported an error when asked for its version"
    else:
        cause = "it printed no version"
    return f"Cannot verify tool {tool}: {cause}. {advice}"


def verified_version(argv: list[str], executable: Path, result: CommandResult) -> ToolVersion:
    """Check the output of a version command and keep its first lines.

    ``pdfdetach -v`` also succeeds when it exits with its version status after printing
    its version.

    Args:
        argv: The version command.
        executable: The resolved tool.
        result: How the command ended.

    Returns:
        The kept version lines, where the tool ran from and its arguments.

    Raises:
        PreparationError: The command hit a limit, failed, printed nothing or reported an
            error.
    """
    lines = (result.stdout + result.stderr).splitlines()
    detach_version = (
        executable.name == "pdfdetach"
        and argv[1:] == ["-v"]
        and any(_PDFDETACH_VERSION.match(line) for line in lines)
    )
    accepted_exit = result.returncode == 0 or (
        result.returncode == _PDFDETACH_VERSION_EXIT_STATUS and detach_version
    )
    if not (result.within_limits and accepted_exit and lines) or "Error:" in result.stderr:
        raise PreparationError(_unverified_version_message(Path(argv[0]).name, result))
    return ToolVersion(
        tuple(lines[:_VERSION_LINES_KEPT]), result.resolved_command[0], tuple(argv[1:])
    )
