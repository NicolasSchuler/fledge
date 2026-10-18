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
    """The tool and its plain options with the other arguments counted, for log lines.

    ``pdftoppm -r 300 -png /home/ann/p.pdf out`` is summarised as
    ``pdftoppm -r -png +3 more arguments``: file names, directories and option values are
    what a log line must not carry, so only the tool's name and its option names are kept.
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
    def timeout_seconds(self) -> int: ...

    @property
    def build_memory_mb(self) -> int: ...

    @property
    def generated_file_byte_limit(self) -> int: ...

    @property
    def import_byte_limit(self) -> int: ...


@dataclass(frozen=True, slots=True)
class RuntimeLimits:
    """What one tool command may use: wall time, output, memory and the size of any file.

    ``max_file_bytes`` bounds any single file a command writes or opens.
    ``max_copy_bytes`` is a budget for the sources copied into a tool workspace before
    any command runs; it bounds their combined size, not a single file.

    The defaults come from ``limits``, the constants that also default the settings, so a
    limit has one default. A job builds its limits with ``from_settings``.
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
        """The limits a job's settings give its tool commands.

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
        """The limits of a command, as a build records them; the copy budget is not one."""
        return {
            "timeout_seconds": self.timeout_seconds,
            "max_output_bytes": self.max_output_bytes,
            # The report keeps its established key for the memory limit.
            "memory_mb": self.max_memory_mb,
            "max_file_bytes": self.max_file_bytes,
        }


@dataclass(frozen=True, slots=True)
class ToolVersion:
    """The first version lines a tool printed under isolation, and where it ran from."""

    version: tuple[str, ...]
    executable: str
    version_arguments: tuple[str, ...]

    def to_report(self) -> dict[str, object]:
        return {
            "version": list(self.version),
            "executable": self.executable,
            "version_arguments": list(self.version_arguments),
        }


class CommandFailure(StrEnum):
    """Why a command did not complete normally; the first that applies is the cause.

    A limit that stopped the tool also leaves it with a signal status, so the limit,
    not the status, is the cause when both are present.
    """

    TIMEOUT = "timeout"
    OUTPUT_LIMIT = "output limit"
    RESOURCE_LIMIT = "resource limit"
    EXIT_STATUS = "exit status"


@dataclass(frozen=True, slots=True)
class CommandResult:
    """What one command printed and how it ended, including which limit stopped it."""

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
        """Why ``tool`` did not complete normally, or ``None`` when it did.

        With the ``limits`` the command ran under, a stopped tool's reason states the
        value it exceeded.
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
    """What tool adapters need from a runner: limits, admission, frozen inputs and commands."""

    limits: RuntimeLimits
    budget: ResourceBudget | None

    def freeze_input(self, source: Path, storage: Path) -> Path: ...

    async def tool_version(self, argv: list[str], workspace: Path) -> ToolVersion: ...

    async def run(
        self,
        argv: list[str],
        cwd: Path,
        workspace: Path,
        *,
        readonly_inputs: tuple[Path, ...] = (),
        memory_mb: int | None = None,
    ) -> CommandResult: ...
