"""The port that checks use to run external tools, and the values it exchanges.

``CommandRunner`` is what the PDF, formatting and build modules depend on; ``ToolRunner``
in ``runtime`` is its sandboxed implementation, and tests supply fakes. A runner takes
``RuntimeLimits``, returns a ``CommandResult`` for every command that started, and a
``ToolVersion`` for every tool it verified. This module imports no sandbox, process or
toolchain code.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import KW_ONLY, dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Protocol, Self

from .file_safety import byte_count_text
from .limits import (
    DEFAULT_BUILD_MEMORY_MB,
    DEFAULT_MAX_GENERATED_FILE_BYTES,
    DEFAULT_MAX_PROJECT_BYTES,
    DEFAULT_TOOL_TIMEOUT_SECONDS,
    MIB,
)
from .option_config import POSITIVE_INTEGER, Bounds, validate_scalars

if TYPE_CHECKING:
    from .resource_budget import ResourceBudget

# The part of a failed tool's standard error that a message quotes.
_STDERR_QUOTE_CHARS = 1000
# Standard output and error that one command may print before it is stopped. It is not a
# setting: a tool that prints more is looping.
_MAX_OUTPUT_BYTES = 2 * MIB


# A command-line option that names no value and no path, such as ``-pdf`` or ``--version``.
_PLAIN_OPTION = re.compile(r"--?[A-Za-z][\w-]*")
# Options a log line lists for one command; the rest are counted.
_SUMMARY_OPTIONS = 8


def argv_summary(argv: Sequence[str]) -> str:
    """Summarise a command as its tool and plain options, with the other arguments counted.

    ``pdftoppm -r 300 -png /home/ann/p.pdf out`` is summarised as
    ``pdftoppm -r -png +3 more arguments``: file names, directories and option values are
    what a log line must not carry, so only the tool's name and its option names are kept.
    At most ``_SUMMARY_OPTIONS`` options are listed.

    Returns:
        The summary for a log line, or ``(no command)`` for an empty ``argv``.
    """
    if not argv:
        return "(no command)"
    options = [argument for argument in argv[1:] if _PLAIN_OPTION.fullmatch(argument)]
    shown = options[:_SUMMARY_OPTIONS]
    others = len(argv) - 1 - len(shown)
    parts = [Path(argv[0]).name, *shown]
    if others:
        parts.append(f"+{others} more argument{'' if others == 1 else 's'}")
    return " ".join(parts)


class ToolLimitSettings(Protocol):
    """The settings that bound one tool command; ``config.Settings`` provides them."""

    @property
    def timeout_seconds(self) -> int:
        """The wall time one tool command may take, in seconds."""
        ...

    @property
    def build_memory_mb(self) -> int:
        """The memory one tool command may use, in MiB."""
        ...

    @property
    def generated_file_byte_limit(self) -> int:
        """The size of any one file: ``max_generated_file_bytes``, else ``max_bytes``."""
        ...

    @property
    def import_byte_limit(self) -> int:
        """The combined size of the copied sources: ``max_import_bytes``, else ``max_bytes``."""
        ...


@dataclass(frozen=True, slots=True)
class RuntimeLimits:
    """What one tool command may use: wall time, output, memory and the size of any file.

    The defaults come from ``limits``, the constants that also default the settings, so a
    limit has one default. A job builds its limits with ``from_settings``. Construction
    raises ``SettingsError`` when a field is not a positive integer.

    Attributes:
        FIELD_BOUNDS: The bounds :func:`validate_scalars` applies to every field.
        timeout_seconds: The wall time the command may take, in seconds.
        max_output_bytes: The standard output and error the command may print before it
            is stopped.
        max_memory_mb: The memory the command may use, in MiB.
        max_file_bytes: The largest size of any single file a command writes or opens.
        max_copy_bytes: A budget for the sources copied into a tool workspace before any
            command runs; it bounds their combined size, not a single file.
    """

    FIELD_BOUNDS: ClassVar[dict[str, Bounds]] = dict.fromkeys(
        (
            "timeout_seconds",
            "max_output_bytes",
            "max_memory_mb",
            "max_file_bytes",
            "max_copy_bytes",
        ),
        POSITIVE_INTEGER,
    )

    timeout_seconds: int = DEFAULT_TOOL_TIMEOUT_SECONDS
    max_output_bytes: int = _MAX_OUTPUT_BYTES
    max_memory_mb: int = DEFAULT_BUILD_MEMORY_MB
    max_file_bytes: int = DEFAULT_MAX_GENERATED_FILE_BYTES
    max_copy_bytes: int = DEFAULT_MAX_PROJECT_BYTES

    def __post_init__(self) -> None:
        validate_scalars(self)

    @classmethod
    def from_settings(cls, settings: ToolLimitSettings) -> Self:
        """Build the limits a job's settings give its tool commands.

        Time is ``timeout_seconds``, memory ``build_memory_mb``, the size of any one file
        ``max_generated_file_bytes`` and the copied sources ``max_import_bytes``; the two
        byte limits are ``max_bytes`` unless set.
        """
        return cls(
            timeout_seconds=settings.timeout_seconds,
            max_memory_mb=settings.build_memory_mb,
            max_file_bytes=settings.generated_file_byte_limit,
            max_copy_bytes=settings.import_byte_limit,
        )

    def to_report(self) -> dict[str, int]:
        """Return the limits of a command, as a build records them; the copy budget is not one."""
        return {
            "timeout_seconds": self.timeout_seconds,
            "max_output_bytes": self.max_output_bytes,
            # The report keeps its established key for the memory limit.
            "memory_mb": self.max_memory_mb,
            "max_file_bytes": self.max_file_bytes,
        }


@dataclass(frozen=True, slots=True)
class ToolVersion:
    """The first version lines a tool printed under isolation, and where it ran from.

    Attributes:
        version: The first version lines the tool printed.
        executable: Where the tool ran from.
        version_arguments: The arguments that made the tool print its version.
    """

    version: tuple[str, ...]
    executable: str
    version_arguments: tuple[str, ...]

    def to_report(self) -> dict[str, object]:
        """Return the version as a report records it."""
        return {
            "version": list(self.version),
            "executable": self.executable,
            "version_arguments": list(self.version_arguments),
        }


class CommandFailure(StrEnum):
    """Why a command did not complete normally; the first that applies is the cause.

    A limit that stopped the tool also leaves it with a signal status, so the limit,
    not the status, is the cause when both are present.

    Attributes:
        TIMEOUT: The time limit stopped the tool.
        OUTPUT_LIMIT: The output limit stopped the tool.
        RESOURCE_LIMIT: A resource limit stopped the tool.
        EXIT_STATUS: The tool exited with a nonzero status.
    """

    TIMEOUT = "timeout"
    OUTPUT_LIMIT = "output limit"
    RESOURCE_LIMIT = "resource limit"
    EXIT_STATUS = "exit status"


@dataclass(frozen=True, slots=True)
class CommandResult:
    """What one command printed and how it ended, including which limit stopped it.

    Attributes:
        returncode: The command's exit status.
        stdout: What the command printed on standard output.
        stderr: What the command printed on standard error.
        resolved_command: The command line as it was run.
        timed_out: Whether the time limit stopped the command.
        output_limited: Whether the output limit stopped the command.
        resource_exceeded: Why a resource limit stopped the command, or ``None``.
        resource_roots: The directories the toolchain may read from.
        duration_seconds: How long the command ran, in seconds.
    """

    returncode: int
    stdout: str
    stderr: str
    _: KW_ONLY
    resolved_command: list[str]
    timed_out: bool = False
    output_limited: bool = False
    resource_exceeded: str | None = None
    resource_roots: tuple[str, ...] = ()
    duration_seconds: float = 0.0

    @property
    def within_limits(self) -> bool:
        """Whether the tool ran to its own exit inside its time, output and resource limits."""
        return not (self.timed_out or self.output_limited or self.resource_exceeded)

    @property
    def completed_normally(self) -> bool:
        """Whether the tool exited with status 0 inside every limit."""
        return self.within_limits and not self.returncode

    @property
    def failure(self) -> CommandFailure | None:
        """The cause of an abnormal end, or ``None`` when the tool completed normally."""
        if self.timed_out:
            return CommandFailure.TIMEOUT
        if self.output_limited:
            return CommandFailure.OUTPUT_LIMIT
        if self.resource_exceeded:
            return CommandFailure.RESOURCE_LIMIT
        if self.returncode:
            return CommandFailure.EXIT_STATUS
        return None

    def failure_reason(self, tool: str, limits: RuntimeLimits | None = None) -> str | None:
        """Explain why ``tool`` did not complete normally.

        A failed exit quotes the start of the standard error.

        Args:
            tool: The name of the tool, as the reason mentions it.
            limits: The limits the command ran under; with them, a stopped tool's reason
                states the value it exceeded.

        Returns:
            The reason, or ``None`` when the tool completed normally.
        """
        match self.failure:
            case CommandFailure.TIMEOUT:
                if limits is None:
                    return f"{tool} exceeded the configured time limit"
                return (
                    f"{tool} was stopped after {limits.timeout_seconds} s because it ran too long"
                )
            case CommandFailure.OUTPUT_LIMIT:
                if limits is None:
                    return f"{tool} exceeded the diagnostic output limit"
                return (
                    f"{tool} was stopped after printing more than "
                    f"{byte_count_text(limits.max_output_bytes)} of output"
                )
            case CommandFailure.RESOURCE_LIMIT:
                return self.resource_exceeded
            case CommandFailure.EXIT_STATUS:
                return f"{tool} failed ({self.returncode}): {self.stderr[:_STDERR_QUOTE_CHARS]}"
        return None


class CommandRunner(Protocol):
    """What tool adapters need from a runner: limits, admission, frozen inputs and commands.

    Attributes:
        limits: The limits of every command the runner starts.
        budget: The resource budget that admits commands, or ``None``.
    """

    limits: RuntimeLimits
    budget: ResourceBudget | None

    def freeze_input(self, source: Path, storage: Path) -> Path:
        """Copy ``source`` once into ``storage``; tools are granted only that copy.

        Args:
            source: The input file.
            storage: The runtime-owned directory for the copies.

        Returns:
            The read-only copy that tools may read.

        Raises:
            PreparationError: The input is not a regular file, exceeds the file size limit
                or changed while it was copied.
        """
        ...

    async def tool_version(self, argv: list[str], workspace: Path) -> ToolVersion:
        """Run the version command ``argv`` under isolation in ``workspace``.

        Args:
            argv: The version command.
            workspace: The directory the command is confined to.

        Returns:
            The version lines the tool printed and where it ran from.

        Raises:
            PreparationError: The command is invalid, or the tool gave no usable version.
        """
        ...

    async def run(
        self,
        argv: list[str],
        cwd: Path,
        workspace: Path,
        *,
        readonly_inputs: tuple[Path, ...] = (),
        memory_mb: int | None = None,
    ) -> CommandResult:
        """Run one command under the runner's limits.

        Args:
            argv: The command and its arguments.
            cwd: The directory the command runs in, inside ``workspace``.
            workspace: The directory the command is confined to.
            readonly_inputs: Frozen copies (see :meth:`freeze_input`) the command may read.
            memory_mb: A memory limit for this command, in MiB; ``None`` keeps the runner's
                limit.

        Returns:
            What the command printed and how it ended.

        Raises:
            PreparationError: The command is invalid or could not start, an input is not an
                unchanged frozen copy, or the sandbox refused the command
                (``SandboxRefusedError``).
            ValueError: ``memory_mb`` is not a positive integer or exceeds the limit.
        """
        ...
