"""Fail-closed local tool execution.

macOS Seatbelt and Linux Bubblewrap are supported execution boundaries. A refused
sandbox is an execution failure, never permission to retry without isolation.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import os
import re
import stat
import tempfile
import time
from collections.abc import AsyncGenerator
from dataclasses import KW_ONLY, astuple, dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol, Self

from .biber_toolchain import BiberPreparation, create_biber_toolchain
from .file_safety import MIB, DirectoryIdentity, FileIdentity, regular_file_identity
from .models import PreparationError
from .resource_monitor import (
    ActiveCommand,
    ResourceMonitor,
    ResourceStatistics,
    complete_despite_cancellation,
    kill_process_group,
    start_process,
    workspace_violation,
)
from .sandbox import (
    SandboxBackend,
    sandbox_refusal,
    select_sandbox_backend,
    tool_environment,
    with_ulimits,
)
from .toolchain import Toolchain, tool_path

if TYPE_CHECKING:
    from .scheduler import ResourceBudget

_READ_CHUNK_BYTES = 64 * 1024
_UNLEASED_MEMORY_MB = 256
_PDFDETACH_VERSION = re.compile(r"^pdfdetach version \S+")


@dataclass(frozen=True, slots=True)
class RuntimeLimits:
    timeout_seconds: int = 120
    max_output_bytes: int = 2 * MIB
    max_memory_mb: int = 2048
    max_file_bytes: int = 256 * MIB

    def __post_init__(self) -> None:
        if any(type(value) is not int or value < 1 for value in astuple(self)):
            raise PreparationError("Tool resource limits must be positive integers")

    def to_report(self) -> dict[str, int]:
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


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str
    _: KW_ONLY
    resolved_command: list[str]
    timed_out: bool = False
    output_limited: bool = False
    resource_exceeded: str | None = None
    resource_roots: tuple[str, ...] = ()

    @property
    def within_limits(self) -> bool:
        """Whether the tool ran to its own exit inside its time, output and resource limits."""
        return not (self.timed_out or self.output_limited or self.resource_exceeded)

    @property
    def completed_normally(self) -> bool:
        """Whether the tool exited with status 0 inside every limit."""
        return self.within_limits and not self.returncode

    def failure_reason(self, tool: str) -> str | None:
        """Why ``tool`` did not complete normally, or ``None`` when it did."""
        if self.timed_out:
            return f"{tool} exceeded the configured time limit"
        if self.output_limited:
            return f"{tool} exceeded the diagnostic output limit"
        if self.resource_exceeded:
            return self.resource_exceeded
        if self.returncode:
            return f"{tool} failed ({self.returncode}): {self.stderr[:1000]}"
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


def _require_arguments(argv: list[str], subject: str) -> None:
    if not argv or any(not isinstance(arg, str) or "\0" in arg for arg in argv):
        raise PreparationError(f"{subject} must contain non-NUL string arguments")


def _verified_version(argv: list[str], executable: Path, result: CommandResult) -> ToolVersion:
    lines = (result.stdout + result.stderr).splitlines()
    # pdfdetach -v prints its version and then exits with status 99.
    detach_version = (
        executable.name == "pdfdetach"
        and argv[1:] == ["-v"]
        and any(_PDFDETACH_VERSION.match(line) for line in lines)
    )
    accepted_exit = result.returncode == 0 or (result.returncode == 99 and detach_version)
    if not (result.within_limits and accepted_exit and lines) or "Error:" in result.stderr:
        raise PreparationError(f"Cannot verify tool {argv[0]}: {result.stderr[:500]}")
    return ToolVersion(tuple(lines[:3]), result.resolved_command[0], tuple(argv[1:]))


class _FrozenInputs:
    """Runtime-owned copies of read-only inputs; only these exact files are ever granted."""

    def __init__(self) -> None:
        self._copies: dict[Path, Path] = {}
        self._identities: dict[Path, FileIdentity] = {}
        self._directories: dict[Path, DirectoryIdentity] = {}

    def overlap(self, workspace: Path) -> bool:
        return any(path.is_relative_to(workspace) for path in self._identities)

    def freeze(self, source: Path, storage: Path, *, max_file_bytes: int) -> Path:
        """Copy once into a runtime-owned directory; grant only the recorded copy."""
        source_key = source.absolute()
        if source_key in self._identities:
            self.validate((source_key,))
            return source_key
        if source_key in self._copies:
            frozen = self._copies[source_key]
            self.validate((frozen,))
            return frozen
        # Validate before resolving: resolving a final symlink would hide it.
        identity = regular_file_identity(source_key)
        source = source_key.resolve(strict=True)
        if identity.size > max_file_bytes:
            raise PreparationError("A frozen input exceeded the file size limit")
        snapshot = Path(tempfile.mkdtemp(prefix="input-", dir=self._storage(storage)))
        self._directories[snapshot] = DirectoryIdentity.from_stat(snapshot.stat())
        destination = snapshot / source.name
        try:
            self._copy(source, destination, identity, max_file_bytes)
        except BaseException:
            # Only our freshly created, unpublished partial snapshot is removed.
            destination.unlink(missing_ok=True)
            raise
        destination.chmod(0o400)
        self._identities[destination] = regular_file_identity(destination)
        self._copies[source_key] = destination
        return destination

    def _storage(self, storage: Path) -> Path:
        storage = storage.absolute()
        if storage.is_symlink():
            raise PreparationError("A frozen-input directory cannot be a symlink")
        storage = storage.resolve()
        if storage not in self._directories:
            try:
                storage.mkdir(mode=0o700, parents=True, exist_ok=False)
            except FileExistsError as error:
                raise PreparationError(
                    "Frozen inputs require a new runtime-owned directory"
                ) from error
            self._directories[storage] = DirectoryIdentity.from_stat(storage.stat())
        info = storage.lstat()
        if not stat.S_ISDIR(info.st_mode) or (
            DirectoryIdentity.from_stat(info) != self._directories[storage]
        ):
            raise PreparationError("The frozen-input directory was replaced")
        return storage

    def _copy(
        self, source: Path, destination: Path, identity: FileIdentity, max_file_bytes: int
    ) -> None:
        descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as incoming, destination.open("xb") as outgoing:
            if FileIdentity.from_stat(os.fstat(incoming.fileno())) != identity:
                raise PreparationError("An input changed before it could be frozen")
            remaining = max_file_bytes + 1
            while remaining and (chunk := incoming.read(min(MIB, remaining))):
                outgoing.write(chunk)
                remaining -= len(chunk)
            if not remaining or regular_file_identity(source) != identity:
                raise PreparationError("An input changed or exceeded its limit while freezing")

    def validate(self, inputs: tuple[Path, ...]) -> tuple[Path, ...]:
        validated = []
        for value in inputs:
            path = value.absolute()
            if path not in self._identities or path.resolve() != path:
                raise PreparationError("Read-only grants require exact runtime-frozen input files")
            if regular_file_identity(path) != self._identities[path]:
                raise PreparationError("A frozen input was modified or replaced")
            info = path.parent.lstat()
            if not stat.S_ISDIR(info.st_mode) or (
                DirectoryIdentity.from_stat(info) != self._directories[path.parent]
            ):
                raise PreparationError("A frozen-input directory was modified or replaced")
            validated.append(path)
        return tuple(validated)


@dataclass(frozen=True, slots=True)
class _Invocation:
    """A validated, sandboxed command ready to start."""

    resolved_command: list[str]
    sandboxed_command: list[str]
    cwd: Path
    workspace: Path
    environment: dict[str, str]
    memory_mb: int
    reserved_memory_mb: int
    resource_roots: tuple[str, ...]


class _Exit(NamedTuple):
    timed_out: bool
    resource_exceeded: str | None


class _BoundedOutput:
    """Collect stdout and stderr up to one shared limit; exceeding it kills the group."""

    def __init__(self, max_bytes: int, pid: int) -> None:
        self._max_bytes = max_bytes
        self._pid = pid
        self.stdout = bytearray()
        self.stderr = bytearray()
        self.limited = False

    async def collect(self, stream: asyncio.StreamReader, buffer: bytearray) -> None:
        while chunk := await stream.read(_READ_CHUNK_BYTES):
            if self.limited:
                # Keep draining after the kill: the process counts as finished
                # only once every pipe reaches end-of-file, and a reader that
                # stops early leaves buffered data that stalls its pipe forever.
                continue
            remaining = self._max_bytes - len(self.stdout) - len(self.stderr)
            buffer.extend(chunk[: max(0, remaining)])
            if len(chunk) > remaining:
                self.limited = True
                kill_process_group(self._pid)


def _kill_launched_group(process: asyncio.subprocess.Process) -> None:
    kill_process_group(process.pid)


class ToolRunner:
    """Run tools with immutable input grants and one shared job resource monitor."""

    def __init__(
        self,
        limits: RuntimeLimits | None = None,
        *,
        budget: ResourceBudget | None = None,
        source_date_epoch: int | str | None = None,
    ) -> None:
        self.limits = limits or RuntimeLimits()
        self.budget = budget
        epoch = str(int(time.time()) if source_date_epoch is None else source_date_epoch)
        if not epoch.isascii() or not epoch.isdecimal():
            raise PreparationError("The source-date epoch must be a non-negative integer")
        self._source_date_epoch = epoch
        self.sandbox: SandboxBackend = select_sandbox_backend()
        self._versions: dict[tuple[str, ...], ToolVersion] = {}
        self._version_names: dict[tuple[str, ...], str] = {}
        self._version_locks: dict[tuple[str, ...], asyncio.Lock] = {}
        self._frozen = _FrozenInputs()
        self._workspace_claims: set[Path] = set()
        self._biber = BiberPreparation()
        self._monitor = ResourceMonitor(
            self.sandbox,
            ResourceStatistics(
                memory_limit_mb=budget.memory_mb if budget else self.limits.max_memory_mb,
                parent_memory_reservation_mb=budget.coordinator_memory_mb if budget else None,
            ),
        )

    @property
    def source_date_epoch(self) -> str:
        return self._source_date_epoch

    @functools.cached_property
    def toolchain(self) -> Toolchain:
        return Toolchain.discover(self.sandbox)

    @property
    def biber_needs_preparation(self) -> bool:
        """Whether running Biber still requires the costly macOS toolchain copy."""
        return self.sandbox.requires_sealed_biber and not self._biber.ready

    @property
    def resource_roots(self) -> frozenset[Path]:
        """Stable toolchain roots; command results carry their exact declared roots."""
        return self.toolchain.resource_roots

    @property
    def tool_versions(self) -> dict[str, ToolVersion]:
        result: dict[str, ToolVersion] = {}
        for key in sorted(self._versions):
            name = self._version_names[key]
            if name in result:
                name = " ".join(key)
            result[name] = self._versions[key]
        return result

    def statistics(self) -> ResourceStatistics:
        """A snapshot of the observed resource use and any job-wide failure."""
        return replace(self._monitor.statistics)

    def _resolve_tool(self, name: str) -> Path:
        return self.toolchain.resolve(name)

    def freeze_input(self, source: Path, storage: Path) -> Path:
        """Copy once into a runtime-owned directory; grant only the recorded copy."""
        return self._frozen.freeze(source, storage, max_file_bytes=self.limits.max_file_bytes)

    async def prepare_biber(self, root: Path) -> Path | None:
        """Prepare a sandbox-executable Biber once per runner; ``None`` keeps the plain tool.

        Only Seatbelt needs the sealed copy described in ``biber_toolchain``. Linux
        Bubblewrap already permits execution inside the bound workspace, so
        nothing is prepared there.
        """
        if not self.sandbox.requires_sealed_biber:
            return None
        biber = self._biber
        async with biber.lock:
            if biber.toolchain is not None and biber.toolchain.sealed:
                return biber.toolchain.executable
            if biber.failure is not None:
                raise PreparationError(biber.failure)
            try:
                return await self._prepare_biber(root)
            except (OSError, PreparationError) as error:
                biber.fail(str(error))
                # An unusable copy must never stay the resolved tool: a later
                # probe would run it with no grants instead of reporting this.
                self.toolchain.forget("biber")
                raise PreparationError(str(error)) from error

    async def _prepare_biber(self, root: Path) -> Path:
        source = tool_path("biber")
        # The payload is counted against the job temporary budget: it is created
        # under the monitored job root whenever a job scope is active.
        scope = self._monitor.scope
        base = (scope.root if scope is not None else root.resolve(strict=True)) / ".toolchain"
        toolchain = create_biber_toolchain(source, base, max_file_bytes=self.limits.max_file_bytes)
        self.toolchain.substitute("biber", toolchain.executable)
        self._biber.toolchain = toolchain
        # The first exec stage unpacks only a handful of files, so the warm-up
        # itself must be allowed to execute inside the payload directory.
        await self.tool_version(["biber", "--noconf", "--version"], toolchain.payload)
        self._biber.toolchain = toolchain.seal()
        return toolchain.executable

    def _admission(
        self, name: str, memory_mb: int | None = None
    ) -> contextlib.AbstractAsyncContextManager[object]:
        """Lease capacity for an unleased caller; a held lease already covers its commands."""
        if self.budget is None or self.budget.current_lease is not None:
            return contextlib.nullcontext()
        memory = memory_mb or min(
            self.limits.max_memory_mb, _UNLEASED_MEMORY_MB, self.budget.available_memory_mb
        )
        return self.budget.lease(name, memory_mb=memory)

    async def tool_version(self, argv: list[str], workspace: Path) -> ToolVersion:
        _require_arguments(argv, "A version command")
        executable = self._resolve_tool(argv[0])
        key = (str(executable), *argv[1:])
        if key in self._versions:
            return self._versions[key]
        # Admission precedes the keyed lock: a waiter must not hold the lock
        # while another lease owner waits for that same version.
        async with (
            self._admission("tool version " + argv[0]),
            self._version_locks.setdefault(key, asyncio.Lock()),
        ):
            if key not in self._versions:
                self._monitor.statistics.version_probes += 1
                result = await self.run(argv, cwd=workspace, workspace=workspace)
                self._versions[key] = _verified_version(argv, executable, result)
                self._version_names[key] = Path(argv[0]).name
            return self._versions[key]

    def _sandbox_command(
        self,
        command: list[str],
        workspace: Path,
        readonly_inputs: tuple[Path, ...] = (),
    ) -> list[str]:
        return self.sandbox.wrap(
            command,
            workspace,
            context=self.toolchain.context(Path(command[0])),
            readonly_inputs=self._frozen.validate(readonly_inputs),
            biber=self._biber.scope(command),
        )

    @contextlib.asynccontextmanager
    async def job_scope(self, root: Path, *, max_temp_bytes: int) -> AsyncGenerator[Self]:
        async with self._monitor.job_scope(
            root, max_temp_bytes=max_temp_bytes, max_file_bytes=self.limits.max_file_bytes
        ):
            yield self

    async def run(
        self,
        argv: list[str],
        cwd: Path,
        workspace: Path,
        *,
        readonly_inputs: tuple[Path, ...] = (),
        memory_mb: int | None = None,
    ) -> CommandResult:
        _require_arguments(argv, "A tool command")
        if memory_mb is not None and (type(memory_mb) is not int or memory_mb < 1):
            raise PreparationError("A command memory cap must be a positive integer")
        async with self._admission("tool " + argv[0], memory_mb):
            return await self._run_command(argv, cwd, workspace, readonly_inputs, memory_mb)

    async def _run_command(
        self,
        argv: list[str],
        cwd: Path,
        workspace: Path,
        readonly_inputs: tuple[Path, ...],
        memory_mb: int | None,
    ) -> CommandResult:
        workspace = workspace.resolve(strict=True)
        cwd = cwd.resolve(strict=True)
        if not workspace.is_dir() or not cwd.is_dir() or not cwd.is_relative_to(workspace):
            raise PreparationError("Tool working directory must be inside its own workspace")
        scope = self._monitor.scope
        if scope is not None and not workspace.is_relative_to(scope.root):
            raise PreparationError("Tool workspaces must be inside the monitored job directory")
        if self._frozen.overlap(workspace):
            raise PreparationError("A tool's writable workspace cannot contain frozen inputs")
        if any(
            path.is_relative_to(workspace) or workspace.is_relative_to(path)
            for path in self._workspace_claims
        ):
            raise PreparationError(
                "Concurrent tools need separate non-overlapping writable workspaces"
            )
        if self._monitor.failure:
            raise PreparationError(self._monitor.failure)
        inputs = self._frozen.validate(readonly_inputs)
        executable = self._resolve_tool(argv[0])
        if executable.resolve().is_relative_to(workspace):
            raise PreparationError("Executing a project-supplied program is not permitted")
        command = [str(executable), *argv[1:]]
        context = self.toolchain.context(executable)
        sandboxed = self._sandbox_command(command, workspace, inputs)
        environment = tool_environment(workspace, context, source_date_epoch=self.source_date_epoch)
        biber = self._biber.scope(command)
        if biber is not None:
            # PAR execs its payload out of this directory; point it at the warmed,
            # runtime-owned copy instead of the writable per-invocation temporary.
            environment["PAR_GLOBAL_TEMP"] = str(biber.payload)
        lease = self.budget.current_lease if self.budget is not None else None
        maximum = (
            min(self.limits.max_memory_mb, lease.memory_mb) if lease else self.limits.max_memory_mb
        )
        if memory_mb is not None and memory_mb > maximum:
            raise PreparationError("A command memory cap cannot exceed its lease or runtime limit")
        command_memory_mb = maximum if memory_mb is None else memory_mb
        invocation = _Invocation(
            resolved_command=command,
            sandboxed_command=sandboxed,
            cwd=cwd,
            workspace=workspace,
            environment=environment,
            memory_mb=command_memory_mb,
            reserved_memory_mb=lease.memory_mb if lease else command_memory_mb,
            resource_roots=tuple(str(root) for root in sorted(context.resource_roots)),
        )
        self._workspace_claims.add(workspace)
        try:
            return await self._execute(invocation)
        finally:
            self._workspace_claims.discard(workspace)

    async def _execute(self, invocation: _Invocation) -> CommandResult:
        try:
            process = await start_process(
                *with_ulimits(
                    invocation.sandboxed_command,
                    max_file_bytes=self.limits.max_file_bytes,
                    timeout_seconds=self.limits.timeout_seconds,
                ),
                stop=_kill_launched_group,
                cwd=invocation.cwd,
                env=invocation.environment,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                close_fds=True,
            )
        except (OSError, ValueError) as error:
            raise PreparationError(f"Restricted tool startup failed: {error}") from error
        command = self._monitor.register(
            process,
            invocation.workspace,
            memory_mb=invocation.memory_mb,
            reserved_memory_mb=invocation.reserved_memory_mb,
        )
        output = _BoundedOutput(self.limits.max_output_bytes, process.pid)
        assert process.stdout is not None and process.stderr is not None
        readers = (
            asyncio.create_task(output.collect(process.stdout, output.stdout)),
            asyncio.create_task(output.collect(process.stderr, output.stderr)),
        )
        completion = asyncio.create_task(process.wait())
        watcher = (
            None
            if self._monitor.scope is not None
            else asyncio.create_task(
                self._monitor.watch_command(
                    process.pid, invocation.workspace, max_file_bytes=self.limits.max_file_bytes
                )
            )
        )
        try:
            exit_state = await self._wait_for_exit(command, completion, watcher)
        finally:
            await complete_despite_cancellation(self._reap(command, completion, readers, watcher))
        final_violation = await complete_despite_cancellation(
            asyncio.to_thread(workspace_violation, invocation.workspace, self.limits.max_file_bytes)
        )
        stdout = output.stdout.decode("utf-8", errors="replace")
        stderr = output.stderr.decode("utf-8", errors="replace")
        if sandbox_refusal(stderr):
            raise PreparationError(f"Restricted execution was refused: {stderr.strip()[:1000]}")
        return CommandResult(
            process.returncode or 0,
            stdout,
            stderr,
            resolved_command=invocation.resolved_command,
            timed_out=exit_state.timed_out,
            output_limited=output.limited,
            resource_exceeded=(
                exit_state.resource_exceeded or final_violation or self._monitor.failure
            ),
            resource_roots=invocation.resource_roots,
        )

    async def _wait_for_exit(
        self,
        command: ActiveCommand,
        completion: asyncio.Task[int],
        watcher: asyncio.Task[str] | None,
    ) -> _Exit:
        """Wait for the exit, the time limit or a resource failure, whichever is first."""
        watched: list[asyncio.Future[Any]] = [completion, command.failure]
        if watcher is not None:
            watched.append(watcher)
        if self._monitor.failure:
            command.fail(self._monitor.failure)
        done, _ = await asyncio.wait(
            watched, timeout=self.limits.timeout_seconds, return_when=asyncio.FIRST_COMPLETED
        )
        if not done:
            return _Exit(timed_out=True, resource_exceeded=None)
        if command.failure in done:
            return _Exit(timed_out=False, resource_exceeded=command.failure.result())
        if watcher is not None and watcher in done:
            try:
                return _Exit(timed_out=False, resource_exceeded=watcher.result())
            except Exception as error:
                return _Exit(
                    timed_out=False, resource_exceeded=f"Resource accounting failed: {error}"
                )
        return _Exit(timed_out=False, resource_exceeded=None)

    async def _reap(
        self,
        command: ActiveCommand,
        completion: asyncio.Task[int],
        readers: tuple[asyncio.Task[None], ...],
        watcher: asyncio.Task[str] | None,
    ) -> None:
        try:
            kill_process_group(command.process.pid)
            if watcher is not None:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
            await command.process.wait()
            await asyncio.gather(completion, *readers, return_exceptions=True)
        finally:
            self._monitor.release(command)
