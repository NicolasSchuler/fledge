"""Controlled tool execution for tests: a scripted ``CommandRunner`` and runner doubles.

``FakeCommandRunner`` implements the ``CommandRunner`` protocol that the PDF, formatting and
comparison adapters depend on, so a test needs no subclass of the sandboxed ``ToolRunner``
and no access to its private state. Scripts are plain data: the output of each tool, tools
that are missing, a failure mode, events that hold a command until the test releases it, and
a log of every call.

Builds and diagnostics need more of ``ToolRunner`` (its sandbox backend, toolchain roots and
Biber preparation), so ``FakeBuildRunner`` stays a ``ToolRunner`` and takes those through the
constructor parameters ``ToolRunner`` offers for exactly this purpose.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from collections.abc import Awaitable, Callable, Collection, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple

from latexprep.biber_toolchain import BiberToolchain
from latexprep.command_runner import CommandResult, RuntimeLimits, ToolVersion
from latexprep.errors import PreparationError
from latexprep.process_table import GroupProcessTable, ProcessSample, ProcessTable
from latexprep.resource_budget import ResourceBudget
from latexprep.resource_monitor import JobScope, ResourceMonitor, ResourceStatistics
from latexprep.runtime import ToolRunner
from latexprep.toolchain import Toolchain, ToolContext

VERSION_FLAGS = frozenset({"-v", "--version"})
# The ceiling on version lines a runner keeps, as the real runner does.
_VERSION_LINES_KEPT = 3
_FAILURE_MODES = frozenset({"timeout", "output", "resource"})


class ToolCall(NamedTuple):
    """One command a fake runner was asked to run.

    Attributes:
        argv: The command line, tool first.
        workspace: The workspace the command was given.
        readonly_inputs: The read-only inputs the command was granted.
    """

    argv: list[str]
    workspace: Path
    readonly_inputs: tuple[Path, ...]

    @property
    def tool(self) -> str:
        """The tool the command runs, its first argument."""
        return self.argv[0]

    @property
    def is_version_probe(self) -> bool:
        """Whether the command is a version probe, ending in ``-v`` or ``--version``."""
        return self.argv[-1] in VERSION_FLAGS


@dataclass(frozen=True, slots=True)
class Reply:
    """What a scripted command prints, and the files it leaves behind.

    Attributes:
        stdout: The standard output of the command.
        stderr: The standard error of the command.
        returncode: The exit status of the command.
        files: File contents by name, written into the command's workspace.
    """

    stdout: str = ""
    stderr: str = ""
    returncode: int = 0
    files: Mapping[str, bytes] = field(default_factory=dict)


# A script entry is a fixed reply, or a function of the call that returns one.
ReplyLike = str | Reply | CommandResult
Script = ReplyLike | Callable[[ToolCall], ReplyLike]


def queued(results: Iterable[CommandResult]) -> Callable[[ToolCall], CommandResult]:
    """Build a script entry that answers successive commands with successive ``results``."""
    remaining: Iterator[CommandResult] = iter(results)
    return lambda _call: next(remaining)


class FakeCommandRunner:
    """A scripted ``CommandRunner`` that answers by tool name and records every call.

    Version probes (``-v`` and ``--version``) always succeed. Attributes can be set after
    construction, which keeps a test's arrangement next to its assertions. Like the real
    runner, it hands out frozen copies of inputs and refuses to grant a read-only input that
    it did not freeze, and it probes each tool's version only once.

    Attributes:
        limits: The runtime limits, ``RuntimeLimits()`` unless given.
        budget: The shared resource budget, if any.
        replies: The script of each tool, by tool name.
        default: The script that answers every other tool.
        delay: The seconds each non-probe command sleeps before it answers.
        missing: A tool name (or several) that raises ``PreparationError``.
        failure: ``"timeout"``, ``"output"`` or ``"resource"``, reported by every command.
        expected_budget: Commands fail loudly unless a lease of it is held.
        block: An event every command waits for.
        started: An event set by the first command.
        calls: Every call in order, version probes included.
    """

    def __init__(
        self,
        replies: Mapping[str, Script] | None = None,
        *,
        default: Script = "",
        limits: RuntimeLimits | None = None,
        budget: ResourceBudget | None = None,
        delay: float = 0,
    ) -> None:
        self.limits = limits or RuntimeLimits()
        self.budget = budget
        self.replies: dict[str, Script] = dict(replies or {})
        self.default = default
        self.delay = delay
        self.missing: str | Collection[str] | None = None
        self.failure: str | None = None
        self.expected_budget: ResourceBudget | None = None
        self.block: asyncio.Event | None = None
        self.started = asyncio.Event()
        self.calls: list[ToolCall] = []
        self._frozen: dict[Path, Path] = {}
        self._versions: dict[tuple[str, ...], ToolVersion] = {}
        self._version_locks: dict[tuple[str, ...], asyncio.Lock] = {}

    @property
    def commands(self) -> list[list[str]]:
        """The command line of every call except the version probes."""
        return [call.argv for call in self.calls if not call.is_version_probe]

    @property
    def tool_versions(self) -> dict[str, ToolVersion]:
        """The verified version of each probed tool, by tool name."""
        return {Path(key[0]).name: version for key, version in sorted(self._versions.items())}

    @property
    def probes(self) -> list[str]:
        """The tool of every version probe."""
        return [call.tool for call in self.calls if call.is_version_probe]

    def respond(self, call: ToolCall) -> ReplyLike:
        """Return the reply to one non-probe command; subclasses compute theirs from the call.

        This one takes the script of the call's tool from ``replies``, or ``default``, and
        calls it with the call when it is a function.
        """
        script = self.replies.get(call.tool, self.default)
        return script(call) if callable(script) else script

    def version_output(self, call: ToolCall) -> str:
        """Return what a version probe of the call's tool prints."""
        return f"{call.tool} version fixture\n"

    def freeze_input(self, source: Path, storage: Path) -> Path:
        """Copy ``source`` into a fresh directory below ``storage``, once per source.

        The arguments are those of :meth:`CommandRunner.freeze_input`. ``storage`` is created
        if needed, and every call with the same source returns the first copy.

        Raises:
            PreparationError: The copy could not be made.
        """
        key = source.absolute()
        if key in self._frozen:
            return self._frozen[key]
        try:
            storage.mkdir(parents=True, exist_ok=True)
            snapshot = Path(tempfile.mkdtemp(prefix="input-", dir=storage))
            frozen = snapshot / source.name
            shutil.copyfile(source, frozen)
        except OSError as error:
            raise PreparationError(f"An input could not be frozen: {error}") from error
        self._frozen[key] = frozen
        return frozen

    async def tool_version(self, argv: list[str], workspace: Path) -> ToolVersion:
        """Probe a tool's version once per command line and return it.

        The arguments are those of :meth:`CommandRunner.tool_version`; ``workspace`` is also
        the working directory of the probe. Concurrent probes of the same command line share
        one run. The version holds the first output lines of the probe, the resolved tool and
        the probe arguments.

        Raises:
            PreparationError: The tool is missing, or the probe failed or printed nothing.
        """
        key = tuple(argv)
        if key not in self._versions:
            async with self._version_locks.setdefault(key, asyncio.Lock()):
                if key not in self._versions:
                    result = await self.run(argv, cwd=workspace, workspace=workspace)
                    lines = (result.stdout + result.stderr).splitlines()
                    if not result.completed_normally or not lines:
                        raise PreparationError(f"Cannot verify tool {argv[0]}")
                    self._versions[key] = ToolVersion(
                        tuple(lines[:_VERSION_LINES_KEPT]), result.resolved_command[0], key[1:]
                    )
        return self._versions[key]

    def _is_missing(self, tool: str) -> bool:
        """Return whether ``missing`` names ``tool``."""
        if self.missing is None:
            return False
        return tool == self.missing if isinstance(self.missing, str) else tool in self.missing

    def require_frozen(self, readonly_inputs: tuple[Path, ...]) -> None:
        """Refuse a read-only grant of any file this runner did not freeze.

        Raises:
            PreparationError: A path in ``readonly_inputs`` is not one of the frozen copies.
        """
        grantable = set(self._frozen.values())
        if any(path not in grantable for path in readonly_inputs):
            raise PreparationError("Read-only grants require exact runtime-frozen input files")

    async def run(
        self,
        argv: list[str],
        cwd: Path,
        workspace: Path,
        *,
        readonly_inputs: tuple[Path, ...] = (),
        memory_mb: int | None = None,
    ) -> CommandResult:
        """Record one command and answer it from its script.

        The arguments are those of :meth:`CommandRunner.run`; the fake ignores ``cwd`` and
        ``memory_mb``, and each of ``readonly_inputs`` must be a frozen copy. A version probe
        succeeds with :meth:`version_output`. Any other command sets ``started``, waits for
        ``block`` and ``delay``, and answers with :meth:`respond`: a ``CommandResult`` is
        returned unchanged, and any other reply has its files written into ``workspace`` and
        reports the ``failure`` mode.

        Raises:
            PreparationError: A read-only input was not frozen, or the tool is missing.
            AssertionError: ``expected_budget`` is set and no lease of it is held.
        """
        self.require_frozen(readonly_inputs)
        if self.expected_budget is not None and self.expected_budget.current_lease is None:
            raise AssertionError(f"{argv[0]} ran without its shared resource lease")
        call = ToolCall(list(argv), workspace, readonly_inputs)
        self.calls.append(call)
        if self._is_missing(call.tool):
            raise PreparationError(f"Missing tool: {call.tool}")
        if call.is_version_probe:
            return CommandResult(0, self.version_output(call), "", resolved_command=argv)
        self.started.set()
        if self.block is not None:
            await self.block.wait()
        await asyncio.sleep(self.delay)
        reply = self.respond(call)
        if isinstance(reply, CommandResult):
            return reply
        if isinstance(reply, str):
            reply = Reply(reply)
        for name, content in reply.files.items():
            (workspace / name).write_bytes(content)
        return CommandResult(
            reply.returncode,
            reply.stdout,
            reply.stderr,
            resolved_command=argv,
            timed_out=self.failure == "timeout",
            output_limited=self.failure == "output",
            resource_exceeded="Resource exceeded" if self.failure == "resource" else None,
        )


def fake_formatter(results: Iterable[CommandResult]) -> FakeCommandRunner:
    """Build a formatter that answers successive runs with ``results``.

    Version probes always pass.
    """
    return FakeCommandRunner(default=queued(results))


class FixtureToolchain(Toolchain):
    """Tools that resolve under one fixture directory, with the given resource roots.

    Args:
        roots: The resource roots of the toolchain.

    Attributes:
        directory: The directory every tool resolves under.
    """

    def __init__(self, directory: Path = Path("/test-tools"), roots: Iterable[Path] = ()) -> None:
        super().__init__(frozenset(), frozenset(roots), {})
        self.directory = directory

    def resolve(self, name: str) -> Path:
        """Return the path in ``directory`` that has the base name of ``name``."""
        return self.directory / Path(name).name


class UnconfinedSandbox(GroupProcessTable):
    """A backend that runs commands unwrapped, for tests of process lifetimes.

    The sandboxes of the supported platforms are refused inside restricted environments,
    where the host's own sandbox blocks both ``sandbox-exec`` and ``ps``. Production has no
    such option; isolation and accounting failures have their own tests.

    Attributes:
        description: The name of the backend.
        requires_sealed_biber: ``False``: the backend needs no sealed Biber copy.
    """

    description = "unconfined test backend"
    requires_sealed_biber = False

    def system_roots(self) -> frozenset[Path]:
        """Return no system roots."""
        return frozenset()

    def declares_tool(self, path: Path, resolved: Path) -> bool:
        """Accept every tool, whatever its path and resolved target.

        The arguments are those of :meth:`SandboxBackend.declares_tool`.
        """
        return True

    def wrap(
        self,
        command: list[str],
        workspace: Path,
        *,
        context: ToolContext,
        readonly_inputs: tuple[Path, ...],
        biber: BiberToolchain | None,
    ) -> list[str]:
        """Return ``command`` unwrapped.

        The arguments are those of :meth:`SandboxBackend.wrap`; all but ``command`` are
        ignored.
        """
        return command


class UnwatchedMonitor(ResourceMonitor):
    """A monitor whose standalone command watcher never reports a resource failure."""

    async def watch_command(self, pid: int, workspace: Path, *, max_file_bytes: int) -> str:
        """Wait until cancelled without sampling anything; the arguments are ignored."""
        await asyncio.Future()
        raise AssertionError("unreachable")


class ScriptedProcessMonitor(UnwatchedMonitor):
    """A monitor that reads process memory from a function instead of from ``ps``.

    Args:
        processes: The process table, passed to ``ResourceMonitor``.
        sample: The async function that lists the processes with their memory.

    Attributes:
        statistics: The resource statistics the monitor updates, as in ``ResourceMonitor``.
    """

    def __init__(
        self,
        processes: ProcessTable,
        statistics: ResourceStatistics,
        sample: Callable[[], Awaitable[list[ProcessSample]]],
    ) -> None:
        super().__init__(processes, statistics)
        self._sample = sample

    async def sample_processes(self) -> list[ProcessSample]:
        """Return the processes with their memory from the test's sample function."""
        return await self._sample()


class ScriptedJobMonitor(UnwatchedMonitor):
    """A monitor whose job-wide sample is a function the test supplies.

    Args:
        processes: The process table, passed to ``ResourceMonitor``.
        sample: The async function that takes one sample of a job scope.

    Attributes:
        statistics: The resource statistics the monitor updates, as in ``ResourceMonitor``.
    """

    def __init__(
        self,
        processes: ProcessTable,
        statistics: ResourceStatistics,
        sample: Callable[[JobScope], Awaitable[None]],
    ) -> None:
        super().__init__(processes, statistics)
        self._sample = sample

    async def sample_job(self, scope: JobScope) -> None:
        """Take one sample of the whole job by calling the test's sample function."""
        await self._sample(scope)


class LifecycleRunner(ToolRunner):
    """A runner of real child processes without a platform sandbox or host process accounting.

    Args:
        *args: Positional arguments of ``ToolRunner``.
        **kwargs: Keyword arguments of ``ToolRunner``; ``sandbox`` defaults to
            :class:`UnconfinedSandbox` and ``monitor_factory`` to :class:`UnwatchedMonitor`.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("sandbox", UnconfinedSandbox())
        kwargs.setdefault("monitor_factory", UnwatchedMonitor)
        super().__init__(*args, **kwargs)


class FakeBuildRunner(ToolRunner):
    """A runner that fakes the output of latexmk, so builds run without TeX.

    It never runs Biber, so automatic builds behave alike on every host.

    Args:
        resource_roots: The toolchain directories a build may report as the origin of
            loaded packages.

    Attributes:
        biber_needs_preparation: ``False``: no Biber copy is ever prepared.
        returncode: The exit status of every latexmk run.
        log: The text of the ``.log`` file each run writes.
        trace: The text of the ``.fls`` recorder file, or ``None`` for one that records the
            main file as input and the primary output.
        commands: The command line of every call, version probes included.
        biber_preparations: How often :meth:`prepare_biber` was called.
    """

    biber_needs_preparation = False

    def __init__(
        self,
        returncode: int = 0,
        log: str = "",
        trace: str | None = None,
        *,
        resource_roots: Iterable[Path] = (),
    ) -> None:
        super().__init__(toolchain=FixtureToolchain(Path("/tools"), resource_roots))
        self.returncode = returncode
        self.log = log
        self.trace = trace
        self.commands: list[list[str]] = []
        self.biber_preparations = 0

    async def prepare_biber(self, root: Path) -> Path | None:
        """Count the call and prepare nothing.

        Fake builds never launch a real tool, so no toolchain copy is prepared.

        Returns:
            ``None``, which keeps the plain tool.
        """
        self.biber_preparations += 1
        return None

    async def run(
        self,
        argv: list[str],
        cwd: Path,
        workspace: Path,
        *,
        readonly_inputs: tuple[Path, ...] = (),
        memory_mb: int | None = None,
    ) -> CommandResult:
        """Record one command and write the outputs latexmk would leave.

        The arguments are those of :meth:`CommandRunner.run`; the main file is the last
        argument and is resolved against ``cwd``, and the fake ignores ``readonly_inputs`` and
        ``memory_mb``. A version probe succeeds. Any other command writes a PDF, the ``.log``,
        the ``.fls`` recorder file and a ``.fdb_latexmk`` database for the main file into
        ``workspace / "output"``, choosing the engine from ``-xelatex`` or ``-lualatex``, and
        returns a result with ``returncode`` and no output.
        """
        self.commands.append(argv)
        if argv[-1] in VERSION_FLAGS:
            return CommandResult(0, "test tool version\n", "", resolved_command=argv)
        name = Path(argv[-1]).stem
        output = workspace / "output"
        (output / f"{name}.pdf").write_bytes(b"%PDF-1.7\npartial or successful")
        (output / f"{name}.log").write_text(self.log)
        engine = (
            "xelatex" if "-xelatex" in argv else "lualatex" if "-lualatex" in argv else "pdflatex"
        )
        primary = output / (name + (".xdv" if engine == "xelatex" else ".pdf"))
        (output / f"{name}.fls").write_text(
            self.trace if self.trace is not None else f"INPUT {cwd / argv[-1]}\nOUTPUT {primary}\n"
        )
        (output / f"{name}.fdb_latexmk").write_text(
            f'# Fdb version 4\n["{engine}"] 1 "{cwd / argv[-1]}" "{primary}" "{name}" 1 0\n'
            f'  "{cwd / argv[-1]}" 1 1 {"0" * 32} ""\n'
            f'  (generated)\n  "{primary}"\n  (rewritten before read)\n'
        )
        return CommandResult(self.returncode, "", "", resolved_command=argv)
