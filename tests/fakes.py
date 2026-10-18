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
    """One command a fake runner was asked to run."""

    argv: list[str]
    workspace: Path
    readonly_inputs: tuple[Path, ...]

    @property
    def tool(self) -> str:
        return self.argv[0]

    @property
    def is_version_probe(self) -> bool:
        return self.argv[-1] in VERSION_FLAGS


@dataclass(frozen=True, slots=True)
class Reply:
    """What a scripted command prints; ``files`` are written into the command's workspace."""

    stdout: str = ""
    stderr: str = ""
    returncode: int = 0
    files: Mapping[str, bytes] = field(default_factory=dict)


# A script entry is a fixed reply, or a function of the call that returns one.
ReplyLike = str | Reply | CommandResult
Script = ReplyLike | Callable[[ToolCall], ReplyLike]


def queued(results: Iterable[CommandResult]) -> Callable[[ToolCall], CommandResult]:
    """A script entry that answers successive commands with successive ``results``."""
    remaining: Iterator[CommandResult] = iter(results)
    return lambda _call: next(remaining)


class FakeCommandRunner:
    """A scripted ``CommandRunner``: answers by tool name and records every call.

    ``replies`` maps a tool name to its script; ``default`` answers every other tool. Version
    probes (``-v`` and ``--version``) always succeed. Attributes can be set after
    construction, which keeps a test's arrangement next to its assertions:

    - ``missing``: a tool name (or several) that raises ``PreparationError``;
    - ``failure``: ``"timeout"``, ``"output"`` or ``"resource"``, reported by every command;
    - ``expected_budget``: commands fail loudly unless a lease of it is held;
    - ``block``: an event every command waits for, and ``started``, set by the first command.

    Like the real runner, it hands out frozen copies of inputs and refuses to grant a
    read-only input that it did not freeze, and it probes each tool's version only once.
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
        """The reply to one non-probe command; subclasses compute theirs from the call."""
        script = self.replies.get(call.tool, self.default)
        return script(call) if callable(script) else script

    def version_output(self, call: ToolCall) -> str:
        return f"{call.tool} version fixture\n"

    def freeze_input(self, source: Path, storage: Path) -> Path:
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
        if self.missing is None:
            return False
        return tool == self.missing if isinstance(self.missing, str) else tool in self.missing

    def require_frozen(self, readonly_inputs: tuple[Path, ...]) -> None:
        """Refuse a read-only grant of any file this runner did not freeze."""
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
    """A formatter that answers successive runs with ``results``; version probes always pass."""
    return FakeCommandRunner(default=queued(results))


class FixtureToolchain(Toolchain):
    """Tools that resolve under one fixture directory, with the given resource roots."""

    def __init__(self, directory: Path = Path("/test-tools"), roots: Iterable[Path] = ()) -> None:
        super().__init__(frozenset(), frozenset(roots), {})
        self.directory = directory

    def resolve(self, name: str) -> Path:
        return self.directory / Path(name).name


class UnconfinedSandbox(GroupProcessTable):
    """A backend that runs commands unwrapped, for tests of process lifetimes.

    The sandboxes of the supported platforms are refused inside restricted environments,
    where the host's own sandbox blocks both ``sandbox-exec`` and ``ps``. Production has no
    such option; isolation and accounting failures have their own tests.
    """

    description = "unconfined test backend"
    requires_sealed_biber = False

    def system_roots(self) -> frozenset[Path]:
        return frozenset()

    def declares_tool(self, path: Path, resolved: Path) -> bool:
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
        return command


class UnwatchedMonitor(ResourceMonitor):
    """A monitor whose standalone command watcher never reports a resource failure."""

    async def watch_command(self, pid: int, workspace: Path, *, max_file_bytes: int) -> str:
        await asyncio.Future()
        raise AssertionError("unreachable")


class ScriptedProcessMonitor(UnwatchedMonitor):
    """A monitor that reads process memory from a function instead of from ``ps``."""

    def __init__(
        self,
        processes: ProcessTable,
        statistics: ResourceStatistics,
        sample: Callable[[], Awaitable[list[ProcessSample]]],
    ) -> None:
        super().__init__(processes, statistics)
        self._sample = sample

    async def sample_processes(self) -> list[ProcessSample]:
        return await self._sample()


class ScriptedJobMonitor(UnwatchedMonitor):
    """A monitor whose job-wide sample is a function the test supplies."""

    def __init__(
        self,
        processes: ProcessTable,
        statistics: ResourceStatistics,
        sample: Callable[[JobScope], Awaitable[None]],
    ) -> None:
        super().__init__(processes, statistics)
        self._sample = sample

    async def sample_job(self, scope: JobScope) -> None:
        await self._sample(scope)


class LifecycleRunner(ToolRunner):
    """Run real child processes without a platform sandbox or host process accounting."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("sandbox", UnconfinedSandbox())
        kwargs.setdefault("monitor_factory", UnwatchedMonitor)
        super().__init__(*args, **kwargs)


class FakeBuildRunner(ToolRunner):
    """A runner that fakes the output of latexmk, so builds run without TeX.

    It never runs Biber, so automatic builds behave alike on every host. ``resource_roots``
    are the toolchain directories a build may report as the origin of loaded packages.
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
        """Fake builds never launch a real tool, so no toolchain copy is prepared."""
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
