"""Process, memory and storage accounting for restricted tool commands.

Limits are enforced by reactive samples, not by an OS hard RSS limit. A job scope
shares one monitor across concurrent commands and includes the coordinating
parent process; a command outside a job scope is watched on its own.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple, Protocol

from .cancellation import complete_despite_cancellation
from .errors import PreparationError
from .process_control import kill_process_group, kill_unfinished, start_process

_MAX_WORKSPACE_ENTRIES = 20_000
_MAX_GROUP_PROCESSES = 32
_PROCESS_TABLE_TIMEOUT_SECONDS = 2
_PROCESS_LISTING_PROGRAM = "/bin/ps"


class ProcessSample(NamedTuple):
    """One process's resident memory, attributed to the process group it counts against."""

    pid: int
    group: int
    rss_kb: int


class GroupUsage(NamedTuple):
    resident_kb: int
    failure: str | None


class ProcessTable(Protocol):
    """How the platform lists processes; the sandbox backend supplies it."""

    @property
    def process_columns(self) -> str: ...

    def parse_processes(self, output: bytes, registered: set[int]) -> list[ProcessSample]: ...


@dataclass(slots=True)
class ResourceStatistics:
    """Observed resource use of one runner; ``resource_exceeded`` is its first job failure."""

    memory_limit_mb: int
    parent_memory_reservation_mb: int | None
    memory_accounting: str = "not sampled"
    memory_enforcement: str = "reactive samples; not an OS hard RSS limit"
    sample_interval_seconds: float = 0.1
    peak_parent_rss_mb: float | None = None
    peak_tool_rss_mb: float | None = None
    peak_command_rss_mb: float | None = None
    peak_total_rss_mb: float | None = None
    peak_temp_bytes: int = 0
    peak_active_commands: int = 0
    peak_command_memory_reservation_mb: int = 0
    peak_command_memory_limit_mb: int = 0
    samples: int = 0
    monitor_seconds: float = 0.0
    commands: int = 0
    version_probes: int = 0
    resource_exceeded: str | None = None


@dataclass(slots=True)
class ActiveCommand:
    """A running tool's process group with its limits and failure signal."""

    process: asyncio.subprocess.Process
    workspace: Path
    memory_mb: int
    reserved_memory_mb: int
    failure: asyncio.Future[str]
    finished: asyncio.Event = field(default_factory=asyncio.Event)

    def fail(self, message: str) -> None:
        if not self.failure.done():
            self.failure.set_result(message)
        kill_process_group(self.process.pid)


@dataclass(slots=True)
class JobScope:
    """The monitored job directory, its storage ceilings and its sampling task."""

    root: Path
    max_temp_bytes: int
    max_file_bytes: int
    monitor: asyncio.Task[None] = field(init=False)


def _sample_interval(start: float) -> float:
    """Sample tightly while a job ramps up, then relax to keep tree walks cheap.

    Runaway allocation and output growth almost always appear in the first
    seconds, where the shorter interval bounds the overshoot; afterwards the
    repeated walk of the whole job tree costs more than the added precision.
    """
    return 0.1 if time.monotonic() - start < 3.0 else 0.5


def _peak(current: float | None, value: float) -> float:
    return max(current or 0, value)


def _group_usage(samples: list[ProcessSample], pid: int, memory_mb: int) -> GroupUsage:
    sizes = [sample.rss_kb for sample in samples if sample.group == pid]
    resident_kb = sum(sizes)
    if resident_kb > memory_mb * 1024:
        return GroupUsage(resident_kb, f"Aggregate tool memory exceeded {memory_mb} MiB")
    if len(sizes) > _MAX_GROUP_PROCESSES:
        return GroupUsage(resident_kb, f"Tool process count exceeded {_MAX_GROUP_PROCESSES}")
    return GroupUsage(resident_kb, None)


def workspace_violation(workspace: Path, max_file_bytes: int) -> str | None:
    """Walk one workspace. Blocking: callers on the event loop use a worker thread."""
    total = count = 0
    for directory, directories, filenames in os.walk(workspace, followlinks=False):
        count += len(directories) + len(filenames)
        if count > _MAX_WORKSPACE_ENTRIES:
            return f"Workspace entry count exceeded {_MAX_WORKSPACE_ENTRIES}"
        for name in filenames:
            with contextlib.suppress(FileNotFoundError):
                size = (Path(directory) / name).lstat().st_size
                if size > max_file_bytes:
                    return "A generated file exceeded the file size limit"
                total += size
                if total > 4 * max_file_bytes:
                    return "Workspace exceeded the total disk limit"
    return None


def _tree_bytes(root: Path) -> int:
    total = 0
    for directory, _, files in os.walk(root, followlinks=False):
        for name in files:
            with contextlib.suppress(FileNotFoundError):
                total += (Path(directory) / name).lstat().st_size
    return total


class ResourceMonitor:
    """Account for one runner's commands, alone or within a shared job scope."""

    def __init__(self, processes: ProcessTable, statistics: ResourceStatistics) -> None:
        self._processes = processes
        self.statistics = statistics
        self.active_commands: dict[int, ActiveCommand] = {}
        self.scope: JobScope | None = None

    @property
    def failure(self) -> str | None:
        """The first job-wide resource failure; it stops every later command."""
        return self.statistics.resource_exceeded

    def register(
        self,
        process: asyncio.subprocess.Process,
        workspace: Path,
        *,
        memory_mb: int,
        reserved_memory_mb: int,
    ) -> ActiveCommand:
        command = ActiveCommand(
            process,
            workspace,
            memory_mb,
            reserved_memory_mb,
            asyncio.get_running_loop().create_future(),
        )
        self.active_commands[process.pid] = command
        statistics = self.statistics
        active = self.active_commands.values()
        statistics.commands += 1
        statistics.peak_active_commands = max(statistics.peak_active_commands, len(active))
        statistics.peak_command_memory_reservation_mb = max(
            statistics.peak_command_memory_reservation_mb,
            sum(item.reserved_memory_mb for item in active),
        )
        statistics.peak_command_memory_limit_mb = max(
            statistics.peak_command_memory_limit_mb, sum(item.memory_mb for item in active)
        )
        return command

    def release(self, command: ActiveCommand) -> None:
        self.active_commands.pop(command.process.pid, None)
        command.finished.set()

    def fail_job(self, message: str) -> None:
        if self.statistics.resource_exceeded is None:
            self.statistics.resource_exceeded = message
        for command in tuple(self.active_commands.values()):
            command.fail(self.statistics.resource_exceeded)

    async def watch_command(self, pid: int, workspace: Path, *, max_file_bytes: int) -> str:
        """Standalone-run fallback; jobs use the shared monitor below."""
        start = time.monotonic()
        while True:
            await asyncio.sleep(_sample_interval(start))
            samples = await self._sample_processes()
            failure = _group_usage(samples, pid, self.active_commands[pid].memory_mb).failure
            if failure:
                return failure
            failure = await asyncio.to_thread(workspace_violation, workspace, max_file_bytes)
            if failure:
                return failure

    async def _sample_processes(self) -> list[ProcessSample]:
        try:
            probe = await start_process(
                _PROCESS_LISTING_PROGRAM,
                "-axo",
                self._processes.process_columns,
                stop=kill_unfinished,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
                start_new_session=True,
            )
        except FileNotFoundError as error:
            raise PreparationError(
                "Fledge limits the memory of the programs it runs and needs "
                f"{_PROCESS_LISTING_PROGRAM} for that, but it was not found. Install the "
                "procps package (for example `sudo apt install procps` on Debian or Ubuntu), "
                "then run Fledge again."
            ) from error
        try:
            # asyncio.wait_for in Python 3.11 can drop an external cancellation that
            # arrives as the probe finishes (bpo-42130); asyncio.timeout does not.
            async with asyncio.timeout(_PROCESS_TABLE_TIMEOUT_SECONDS):
                output, _ = await probe.communicate()
        finally:
            kill_unfinished(probe)
            await complete_despite_cancellation(probe.wait())
        if probe.returncode:
            raise PreparationError("Process memory accounting is unavailable")
        return self._processes.parse_processes(output, set(self.active_commands))

    def _job_storage(
        self, scope: JobScope, workspaces: tuple[Path, ...]
    ) -> tuple[tuple[str | None, ...], int]:
        """Walk every active workspace and the whole job tree in one worker-thread pass."""
        failures = tuple(
            workspace_violation(workspace, scope.max_file_bytes) for workspace in workspaces
        )
        return failures, _tree_bytes(scope.root)

    def _account_memory(
        self, samples: list[ProcessSample], commands: dict[int, ActiveCommand]
    ) -> None:
        statistics = self.statistics
        parent = os.getpid()
        parent_kb = sum(sample.rss_kb for sample in samples if sample.pid == parent)
        if not parent_kb:
            statistics.memory_accounting = "unavailable: parent process absent"
            if commands or statistics.commands:
                self.fail_job("Job resource accounting omitted the parent process")
        else:
            statistics.memory_accounting = "sampled parent and registered process groups"
        tool_kb = sum(
            sample.rss_kb for sample in samples if sample.group in commands and sample.pid != parent
        )
        statistics.peak_parent_rss_mb = _peak(statistics.peak_parent_rss_mb, parent_kb / 1024)
        statistics.peak_tool_rss_mb = _peak(statistics.peak_tool_rss_mb, tool_kb / 1024)
        statistics.peak_total_rss_mb = _peak(
            statistics.peak_total_rss_mb, (parent_kb + tool_kb) / 1024
        )
        limit = statistics.memory_limit_mb
        if parent_kb + tool_kb > limit * 1024:
            self.fail_job(f"Aggregate job memory including the parent exceeded {limit} MiB")
        for pid, command in commands.items():
            usage = _group_usage(samples, pid, command.memory_mb)
            statistics.peak_command_rss_mb = _peak(
                statistics.peak_command_rss_mb, usage.resident_kb / 1024
            )
            if usage.failure:
                command.fail(usage.failure)

    async def _sample_job(self, scope: JobScope) -> None:
        start = time.monotonic()
        try:
            commands = dict(self.active_commands)
            try:
                samples = await self._sample_processes()
            # OSError includes the TimeoutError of a process table that never answered.
            except (OSError, ValueError, PreparationError) as error:
                self.statistics.memory_accounting = f"unavailable: {error}"
                if commands or self.active_commands or self.statistics.commands:
                    self.fail_job(f"Job resource accounting failed: {error}")
                samples = []
            if samples:
                self._account_memory(samples, commands)
            # Walking the workspaces and the job tree is blocking and grows with
            # the toolchain payload and build output, so it never runs inline.
            watched = tuple(commands.values())
            failures, total = await asyncio.to_thread(
                self._job_storage, scope, tuple(item.workspace for item in watched)
            )
            for command, failure in zip(watched, failures, strict=True):
                if failure:
                    command.fail(failure)
            self.statistics.peak_temp_bytes = max(self.statistics.peak_temp_bytes, total)
            if total > scope.max_temp_bytes:
                self.fail_job(f"Job temporary storage exceeded {scope.max_temp_bytes} bytes")
        finally:
            self.statistics.samples += 1
            self.statistics.monitor_seconds += time.monotonic() - start

    async def _monitor_job(self, scope: JobScope) -> None:
        start = time.monotonic()
        try:
            while True:
                await self._sample_job(scope)
                # A sample that swallowed this task's cancellation must not keep the
                # monitor alive, or job_scope would wait for it indefinitely.
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise asyncio.CancelledError
                interval = _sample_interval(start)
                self.statistics.sample_interval_seconds = interval
                await asyncio.sleep(interval)
        except Exception as error:
            # Fail closed: commands must not outlive their resource accounting.
            self.fail_job(f"Job resource accounting failed: {type(error).__name__}: {error}")

    @contextlib.asynccontextmanager
    async def job_scope(
        self, root: Path, *, max_temp_bytes: int, max_file_bytes: int
    ) -> AsyncGenerator[None]:
        if self.scope is not None or self.active_commands:
            raise PreparationError("A runner can have only one active job scope")
        if type(max_temp_bytes) is not int or max_temp_bytes < 1:
            raise PreparationError("The job temporary-byte budget must be a positive integer")
        root = root.resolve(strict=True)
        if not root.is_dir():
            raise PreparationError("The job scope requires a directory")
        scope = JobScope(root, max_temp_bytes, max_file_bytes)
        scope.monitor = asyncio.create_task(self._monitor_job(scope))
        self.scope = scope
        try:
            yield
        finally:
            await complete_despite_cancellation(self._close_scope(scope))

    async def _close_scope(self, scope: JobScope) -> None:
        try:
            if self.active_commands:
                commands = tuple(self.active_commands.values())
                self.fail_job("Job scope closed while a command was active")
                await asyncio.gather(*(item.finished.wait() for item in commands))
            scope.monitor.cancel()
            await asyncio.gather(scope.monitor, return_exceptions=True)
            # Include very short/source-only jobs in the final sample.
            await self._sample_job(scope)
        finally:
            self.scope = None
