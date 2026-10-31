"""Process, memory and storage accounting for restricted tool commands.

Limits are enforced by reactive samples, not by an OS hard RSS limit. A job scope
shares one monitor across concurrent commands and includes the coordinating
parent process; a command outside a job scope is watched on its own. How processes
are listed is ``process_table``'s concern, and starting and killing them is
``process_control``'s.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import NamedTuple

from .cancellation import complete_despite_cancellation
from .errors import PreparationError
from .limits import MAX_WORKSPACE_ENTRIES
from .process_control import kill_process_group, kill_unfinished, start_process
from .process_table import ProcessSample, ProcessTable

# Processes one tool command may have running at once, its own group included. TeX and
# its helpers (latexmk, bibtex or biber, makeindex, dvipdfmx) use a handful; a fork bomb
# reaches this within a sample.
_MAX_GROUP_PROCESSES = 32
_PROCESS_TABLE_TIMEOUT_SECONDS = 2
_PROCESS_LISTING_PROGRAM = "/bin/ps"
# A workspace may hold this many times the single-file ceiling in total. The ceiling
# bounds one runaway file; the multiple bounds many files that are each below it. A build
# copies the sources into its workspace, so the settings keep the import total within it.
WORKSPACE_TOTAL_FILE_LIMITS = 4
# How often a job is sampled: tightly while it ramps up, because runaway allocation and
# output growth almost always show in the first seconds, then at a relaxed rate because
# every sample walks the whole job tree.
_RAMP_UP_SECONDS = 3.0
_RAMP_UP_SAMPLE_SECONDS = 0.1
_STEADY_SAMPLE_SECONDS = 0.5

# How a resource failure begins, so that its message can be traced to the setting that
# governs the limit. A process count and the number of workspace entries are fixed limits.
_FILE_SIZE_VIOLATION = "A generated file exceeded the file size limit"
_WORKSPACE_TOTAL_VIOLATION = "Workspace exceeded the total disk limit"
_TOOL_MEMORY_VIOLATION = "Aggregate tool memory exceeded"
_JOB_MEMORY_VIOLATION = "Aggregate job memory including the parent exceeded"
_JOB_STORAGE_VIOLATION = "Job temporary storage exceeded"
_LIMIT_ADVICE = (
    (_FILE_SIZE_VIOLATION, "Raise max_generated_file_bytes, or use smaller generated files."),
    (
        _WORKSPACE_TOTAL_VIOLATION,
        "A tool workspace may hold four times max_generated_file_bytes in total, the copy of "
        "the sources included: raise max_generated_file_bytes or lower max_import_bytes.",
    ),
    (_TOOL_MEMORY_VIOLATION, "Raise build_memory_mb."),
    (_JOB_MEMORY_VIOLATION, "Raise memory_mb."),
    (_JOB_STORAGE_VIOLATION, "Raise max_temporary_bytes."),
)


def limit_advice(violation: str) -> str | None:
    """Say what to change when a resource failure message reports a configurable limit.

    Returns:
        The advice, or ``None`` when the message reports no configurable limit.
    """
    return next((advice for start, advice in _LIMIT_ADVICE if violation.startswith(start)), None)


class GroupUsage(NamedTuple):
    """The memory of one command's process group, and the limit it exceeds, if any.

    Attributes:
        resident_kb: The resident memory of the group in kilobytes.
        failure: The message for the limit the group exceeds, or ``None``.
    """

    resident_kb: int
    failure: str | None


class MemoryAccounting(StrEnum):
    """Whether the monitor could measure memory use; ``ResourceStatistics`` says why not.

    Attributes:
        NOT_SAMPLED: No job sample has measured memory yet.
        SAMPLED: The parent process and the registered process groups were sampled.
        UNAVAILABLE: The process listing failed or omitted the parent process.
    """

    NOT_SAMPLED = "not sampled"
    SAMPLED = "sampled parent and registered process groups"
    UNAVAILABLE = "unavailable"


@dataclass(slots=True)
class ResourceStatistics:
    """Observed resource use of one runner; ``resource_exceeded`` is its first job failure.

    Memory figures are in MiB and sizes in bytes.

    Attributes:
        memory_limit_mb: The memory limit of the job, the parent process included.
        parent_memory_reservation_mb: The memory reserved for the parent process, if any.
        memory_accounting: Whether memory use could be measured.
        memory_accounting_problem: Why memory use could not be measured, if it could not.
        memory_enforcement: How the memory limits are enforced.
        sample_interval_seconds: The current interval between job samples.
        peak_parent_rss_mb: The largest resident memory of the parent process.
        peak_tool_rss_mb: The largest resident memory of all tool processes together.
        peak_command_rss_mb: The largest resident memory of one command's process group.
        peak_total_rss_mb: The largest resident memory of the parent and tools together.
        peak_temp_bytes: The largest size of the job tree.
        peak_active_commands: The most commands active at once.
        peak_command_memory_reservation_mb: The largest sum of the memory reserved for
            the active commands.
        peak_command_memory_limit_mb: The largest sum of the memory limits of the active
            commands.
        samples: The number of job samples taken.
        monitor_seconds: The time spent taking job samples.
        commands: The number of commands registered.
        version_probes: The number of tool version commands run.
        resource_exceeded: The first job-wide resource failure.
    """

    memory_limit_mb: int
    parent_memory_reservation_mb: int | None
    memory_accounting: MemoryAccounting = MemoryAccounting.NOT_SAMPLED
    memory_accounting_problem: str | None = None
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
    """A running tool's process group with its limits and failure signal.

    Attributes:
        process: The running process, which leads the process group.
        workspace: The command's workspace.
        memory_mb: The memory limit of the process group in MiB.
        reserved_memory_mb: The memory reserved for the command in MiB.
        failure: Resolves to the first failure message.
        finished: Set when the command is released.
    """

    process: asyncio.subprocess.Process
    workspace: Path
    memory_mb: int
    reserved_memory_mb: int
    failure: asyncio.Future[str]
    finished: asyncio.Event = field(default_factory=asyncio.Event)

    def fail(self, message: str) -> None:
        """Record ``message`` unless a failure is already recorded, and kill the group."""
        if not self.failure.done():
            self.failure.set_result(message)
        kill_process_group(self.process.pid)


@dataclass(slots=True)
class JobScope:
    """The monitored job directory, its storage ceilings and its sampling task.

    Attributes:
        root: The job directory.
        max_temp_bytes: The largest size of the job tree in bytes.
        max_file_bytes: The largest size of one generated file in bytes.
        monitor: The task that samples the job.
    """

    root: Path
    max_temp_bytes: int
    max_file_bytes: int
    monitor: asyncio.Task[None] = field(init=False)


def sample_interval(start: float) -> float:
    """Sample tightly while a job ramps up, then relax to keep tree walks cheap.

    Runaway allocation and output growth almost always appear in the first
    seconds, where the shorter interval bounds the overshoot; afterwards the
    repeated walk of the whole job tree costs more than the added precision.

    Args:
        start: When the job started, as a ``time.monotonic()`` value.

    Returns:
        The interval in seconds.
    """
    ramping_up = time.monotonic() - start < _RAMP_UP_SECONDS
    return _RAMP_UP_SAMPLE_SECONDS if ramping_up else _STEADY_SAMPLE_SECONDS


def _peak(current: float | None, value: float) -> float:
    """Return the larger of ``value`` and the peak so far, which ``None`` counts as zero."""
    return max(current or 0, value)


def group_usage(samples: list[ProcessSample], pid: int, memory_mb: int) -> GroupUsage:
    """Measure the memory of the process group ``pid`` leads, and the limit it exceeds, if any.

    Args:
        samples: The sampled processes.
        pid: The process id of the command that leads the group.
        memory_mb: The memory limit of the group in MiB; the number of processes in the
            group is limited as well.

    Returns:
        The group's resident memory and the failure message of the memory or process
        count limit it exceeds, if any.
    """
    sizes = [sample.rss_kb for sample in samples if sample.group == pid]
    resident_kb = sum(sizes)
    if resident_kb > memory_mb * 1024:
        return GroupUsage(resident_kb, f"{_TOOL_MEMORY_VIOLATION} {memory_mb} MiB")
    if len(sizes) > _MAX_GROUP_PROCESSES:
        return GroupUsage(resident_kb, f"Tool process count exceeded {_MAX_GROUP_PROCESSES}")
    return GroupUsage(resident_kb, None)


class _WorkspaceTally:
    """Entries and bytes counted under one workspace, and the first limit it broke.

    Args:
        max_file_bytes: The largest size of one file; the workspace may hold
            ``WORKSPACE_TOTAL_FILE_LIMITS`` times as much in total.

    Attributes:
        violation: The first limit broken, or ``None``.
    """

    def __init__(self, max_file_bytes: int) -> None:
        self._max_file_bytes = max_file_bytes
        self._entries = 0
        self._bytes = 0
        self.violation: str | None = None

    def add_entries(self, count: int) -> None:
        """Count ``count`` entries against the entry limit."""
        self._entries += count
        if self.violation is None and self._entries > MAX_WORKSPACE_ENTRIES:
            self.violation = f"Workspace entry count exceeded {MAX_WORKSPACE_ENTRIES}"

    def add_file(self, size: int) -> None:
        """Count one file of ``size`` bytes against the file size and the total limits."""
        self._bytes += size
        if self.violation is not None:
            return
        if size > self._max_file_bytes:
            self.violation = _FILE_SIZE_VIOLATION
        elif self._bytes > WORKSPACE_TOTAL_FILE_LIMITS * self._max_file_bytes:
            self.violation = _WORKSPACE_TOTAL_VIOLATION


class StorageUsage(NamedTuple):
    """What one walk of a job tree found: its size, and each workspace's first violation.

    Attributes:
        tree_bytes: The total size of the files in the tree.
        violations: The first violation of each workspace in the order given, or ``None``.
    """

    tree_bytes: int
    violations: tuple[str | None, ...]


def measure_storage(root: Path, workspaces: Sequence[Path], max_file_bytes: int) -> StorageUsage:
    """Walk ``root`` once, counting every file and checking each workspace under it.

    Blocking: callers on the event loop use a worker thread. A file that disappears
    during the walk is skipped.

    Args:
        root: The tree to walk.
        workspaces: The workspaces to check; they lie inside ``root``.
        max_file_bytes: The largest size of one file; a workspace may hold
            ``WORKSPACE_TOTAL_FILE_LIMITS`` times as much in total.

    Returns:
        The total size of the tree and the first violation of each workspace.
    """
    tallies = [_WorkspaceTally(max_file_bytes) for _ in workspaces]
    tree_bytes = 0
    for directory, directories, filenames in os.walk(root, followlinks=False):
        here = Path(directory)
        owners = [
            tally
            for workspace, tally in zip(workspaces, tallies, strict=True)
            if here.is_relative_to(workspace)
        ]
        for tally in owners:
            tally.add_entries(len(directories) + len(filenames))
        for name in filenames:
            with contextlib.suppress(FileNotFoundError):
                size = (here / name).lstat().st_size
                tree_bytes += size
                for tally in owners:
                    tally.add_file(size)
    return StorageUsage(tree_bytes, tuple(tally.violation for tally in tallies))


def workspace_violation(workspace: Path, max_file_bytes: int) -> str | None:
    """Find the first limit one workspace exceeds, or ``None``. Blocking: use a worker thread.

    Args:
        workspace: The workspace to walk.
        max_file_bytes: The largest size of one file.

    Returns:
        The message of the first limit exceeded, or ``None``.
    """
    return measure_storage(workspace, (workspace,), max_file_bytes).violations[0]


class ResourceMonitor:
    """Account for one runner's commands, alone or within a shared job scope.

    Args:
        processes: Lists the processes the way the sandbox backend needs.

    Attributes:
        statistics: The observed resource use.
        active_commands: The registered commands that are still running, by process id.
        scope: The active job scope, if any.
    """

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
        """Register a started command and update the peak statistics.

        Args:
            process: The started process, which leads its own process group.
            workspace: The command's workspace.
            memory_mb: The memory limit of the process group in MiB.
            reserved_memory_mb: The memory reserved for the command in MiB.

        Returns:
            The active command.
        """
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
        """Forget a command that has finished and signal that it has."""
        self.active_commands.pop(command.process.pid, None)
        command.finished.set()

    def fail_job(self, message: str) -> None:
        """Record ``message`` as the job-wide failure unless one is recorded already.

        Every active command then fails with the recorded failure.
        """
        if self.statistics.resource_exceeded is None:
            self.statistics.resource_exceeded = message
        for command in tuple(self.active_commands.values()):
            command.fail(self.statistics.resource_exceeded)

    async def watch_command(self, pid: int, workspace: Path, *, max_file_bytes: int) -> str:
        """Watch one command outside a job scope until it exceeds a limit.

        This is the standalone-run fallback; jobs use the shared monitor of
        :meth:`job_scope`.

        Args:
            pid: The process id of the registered command.
            workspace: The command's workspace.
            max_file_bytes: The largest size of one generated file.

        Returns:
            The failure message.

        Raises:
            PreparationError: Process memory accounting is unavailable.
        """
        start = time.monotonic()
        while True:
            await asyncio.sleep(sample_interval(start))
            samples = await self.sample_processes()
            failure = group_usage(samples, pid, self.active_commands[pid].memory_mb).failure
            if failure:
                return failure
            failure = await asyncio.to_thread(workspace_violation, workspace, max_file_bytes)
            if failure:
                return failure

    async def sample_processes(self) -> list[ProcessSample]:
        """List the processes with their memory; a monitor subclass may replace this.

        Returns:
            One sample per process, attributed to the registered command groups.

        Raises:
            PreparationError: ``ps`` is missing or fails, or its output is invalid.
            TimeoutError: ``ps`` did not answer in time.
        """
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
        """Walk the job tree once, checking every active workspace on the way.

        Args:
            scope: The job scope whose tree is walked.
            workspaces: The workspaces of the active commands.

        Returns:
            The first violation of each workspace, and the size of the tree.
        """
        usage = measure_storage(scope.root, workspaces, scope.max_file_bytes)
        return usage.violations, usage.tree_bytes

    def _account_memory(
        self, samples: list[ProcessSample], commands: dict[int, ActiveCommand]
    ) -> None:
        """Record the sampled memory and fail whatever exceeds a memory limit.

        A sample without the parent process makes memory accounting unavailable and,
        once any command has been registered, fails the job.

        Args:
            samples: The sampled processes.
            commands: The active commands, by process id.
        """
        statistics = self.statistics
        parent = os.getpid()
        parent_kb = sum(sample.rss_kb for sample in samples if sample.pid == parent)
        if not parent_kb:
            statistics.memory_accounting = MemoryAccounting.UNAVAILABLE
            statistics.memory_accounting_problem = "parent process absent"
            if commands or statistics.commands:
                self.fail_job("Job resource accounting omitted the parent process")
        else:
            statistics.memory_accounting = MemoryAccounting.SAMPLED
            statistics.memory_accounting_problem = None
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
            self.fail_job(f"{_JOB_MEMORY_VIOLATION} {limit} MiB")
        for pid, command in commands.items():
            usage = group_usage(samples, pid, command.memory_mb)
            statistics.peak_command_rss_mb = _peak(
                statistics.peak_command_rss_mb, usage.resident_kb / 1024
            )
            if usage.failure:
                command.fail(usage.failure)

    async def sample_job(self, scope: JobScope) -> None:
        """Take one sample of the whole job; a monitor subclass may replace this.

        A failed process listing makes memory accounting unavailable and, once any
        command has been registered, fails the job.
        """
        start = time.monotonic()
        try:
            commands = dict(self.active_commands)
            try:
                samples = await self.sample_processes()
            # OSError includes the TimeoutError of a process table that never answered.
            except (OSError, ValueError, PreparationError) as error:
                problem = str(error) or type(error).__name__
                self.statistics.memory_accounting = MemoryAccounting.UNAVAILABLE
                self.statistics.memory_accounting_problem = problem
                if commands or self.active_commands or self.statistics.commands:
                    self.fail_job(f"Job resource accounting failed: {problem}")
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
                self.fail_job(f"{_JOB_STORAGE_VIOLATION} {scope.max_temp_bytes} bytes")
        finally:
            self.statistics.samples += 1
            self.statistics.monitor_seconds += time.monotonic() - start

    async def _monitor_job(self, scope: JobScope) -> None:
        """Sample the job until cancelled; an unexpected error fails the job."""
        start = time.monotonic()
        try:
            while True:
                await self.sample_job(scope)
                # A sample that swallowed this task's cancellation must not keep the
                # monitor alive, or job_scope would wait for it indefinitely.
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise asyncio.CancelledError
                interval = sample_interval(start)
                self.statistics.sample_interval_seconds = interval
                await asyncio.sleep(interval)
        except Exception as error:  # noqa: BLE001 - fail closed and report the cause
            # Fail closed: commands must not outlive their resource accounting.
            self.fail_job(f"Job resource accounting failed: {type(error).__name__}: {error}")

    @contextlib.asynccontextmanager
    async def job_scope(
        self, root: Path, *, max_temp_bytes: int, max_file_bytes: int
    ) -> AsyncGenerator[None]:
        """Monitor every command of the job under ``root`` for the duration of the block.

        Leaving the block fails any command still active, waits for it to finish, and
        takes a final sample.

        Args:
            root: The job directory.
            max_temp_bytes: The largest size of the job tree in bytes.
            max_file_bytes: The largest size of one generated file in bytes.

        Yields:
            ``None``, once the scope is active; the block runs within the scope.

        Raises:
            PreparationError: A scope or a command is already active, ``max_temp_bytes``
                is not a positive integer, or ``root`` is not a directory.
        """
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
        """Fail and await the active commands, stop the monitor and take a final sample."""
        try:
            if self.active_commands:
                commands = tuple(self.active_commands.values())
                self.fail_job("Job scope closed while a command was active")
                await asyncio.gather(*(item.finished.wait() for item in commands))
            scope.monitor.cancel()
            await asyncio.gather(scope.monitor, return_exceptions=True)
            # Include very short/source-only jobs in the final sample.
            await self.sample_job(scope)
        finally:
            self.scope = None
