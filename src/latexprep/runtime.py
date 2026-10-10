"""Fail-closed local tool execution and isolated LaTeX builds.

macOS Seatbelt and Linux Bubblewrap are supported execution boundaries. A refused
sandbox is an execution failure, never permission to retry without isolation.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import stat
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from collections.abc import AsyncGenerator, Awaitable
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from .scheduler import ResourceBudget

from .build_dependencies import collect_submission_dependencies
from .loaded_options import (
    collect_loaded_options,
    loaded_options_unavailable,
    stage_loaded_options,
)
from .models import Finding, PreparationError
from .source import _commands, _ParseLimit, mask_literals


@dataclass(frozen=True)
class RuntimeLimits:
    timeout_seconds: int = 120
    max_output_bytes: int = 2_097_152
    memory_mb: int = 2048
    max_file_bytes: int = 268_435_456

    def __post_init__(self) -> None:
        if any(type(value) is not int or value < 1 for value in asdict(self).values()):
            raise PreparationError("Tool resource limits must be positive integers")


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    command: list[str]
    output_limited: bool = False
    resource_exceeded: str | None = None
    resource_roots: tuple[str, ...] = ()


@dataclass
class BuildResult:
    success: bool
    pdf: Path | None = None
    findings: list[Finding] = field(default_factory=list)
    dependencies: set[str] = field(default_factory=set)
    tools: dict[str, object] = field(default_factory=dict)
    command: list[str] = field(default_factory=list)
    log: str = ""
    loaded_packages: list[dict[str, str | None]] = field(default_factory=list)
    recorder_complete: bool = False
    submission_inputs: set[str] | None = None
    # Generated files read by the build (output- or project-relative), e.g. main.bbl.
    generated_reads: frozenset[str] = frozenset()


_CHILD_TOOLS = (
    "latexmk",
    "pdflatex",
    "xelatex",
    "lualatex",
    "bibtex",
    "biber",
    "xdvipdfmx",
    "kpsewhich",
    "makeindex",
    "tex-fmt",
    "pdfinfo",
    "pdftotext",
    "pdftoppm",
    "pdffonts",
    "pdfdetach",
    "pdfimages",
)

# Only these policy-level log errors may be deselected. Tool failures, incomplete
# output/recorder evidence, TeX errors and unresolved reruns remain build failures.
SELECTABLE_BUILD_ERRORS = frozenset({"BLD001", "BLD002"})
_SYSTEM_RESOURCES = (
    "/System/Library",
    "/System/Volumes/Preboot/Cryptexes/OS",
    "/System/Cryptexes/OS",
    "/usr/lib",
    "/usr/share",
    "/Library/Fonts",
    "/private/var/db/timezone",
    "/private/etc/localtime",
    "/private/var/select/sh",
    "/dev/null",
    "/dev/zero",
    "/dev/random",
    "/dev/urandom",
    "/dev/fd",
)
_LINUX_SYSTEM_RESOURCES = (
    "/lib",
    "/lib64",
    "/usr/lib",
    "/usr/lib64",
    "/usr/share/texlive",
    "/usr/share/texmf",
    "/usr/share/fonts",
    "/usr/share/fontconfig",
    "/usr/share/poppler",
    "/usr/share/ghostscript",
    "/usr/share/perl",
    "/usr/share/perl5",
    "/usr/share/locale",
    "/usr/share/zoneinfo",
    "/etc/ld.so.cache",
    "/etc/fonts",
    "/etc/texmf",
    "/etc/localtime",
    "/var/lib/texmf",
    "/var/cache/fontconfig",
)
_LINUX_TOOL_PREFIXES = (Path("/usr"), Path("/bin"), Path("/lib"), Path("/lib64"))
# Mach-O universal ("fat") container headers, always big-endian. The 32-bit form
# uses fat_arch records and the 64-bit form fat_arch_64 records.
_FAT_MAGIC_32 = 0xCAFEBABE
_FAT_MAGIC_64 = 0xCAFEBABF
_FAT_CPU_TYPES = {"arm64": 0x0100000C, "x86_64": 0x01000007}
# A runtime-owned toolchain copy is interpolated into latexmk's Perl configuration
# and into the command line latexmk builds for it, so its path must be inert in
# both. Application-generated temporary paths satisfy this; nothing else is used.
_INERT_PATH = re.compile(r"[A-Za-z0-9_./-]+")


def _host_macho_slice(data: bytes) -> bytes:
    """Return the host-architecture slice of a Mach-O executable image.

    Universal binaries are split here rather than with ``lipo``: the Xcode
    command-line stub that ``lipo`` resolves to is never a permitted grant, and
    a universal Biber wrapper would shell out to it inside the sandbox. A
    single-architecture image is already the slice and is returned unchanged.
    """
    if len(data) < 8:
        raise PreparationError("The tool executable is too small to inspect")
    magic = int.from_bytes(data[:4], "big")
    if magic not in {_FAT_MAGIC_32, _FAT_MAGIC_64}:
        return data
    wide = magic == _FAT_MAGIC_64
    width = 32 if wide else 20
    count = int.from_bytes(data[4:8], "big")
    if not 1 <= count <= 64 or 8 + count * width > len(data):
        raise PreparationError("The universal executable header is truncated or unusable")
    machine = platform.machine()
    if machine not in _FAT_CPU_TYPES:
        raise PreparationError(f"Unsupported host architecture for tool isolation: {machine}")
    for index in range(count):
        record = data[8 + index * width : 8 + (index + 1) * width]
        if int.from_bytes(record[:4], "big") != _FAT_CPU_TYPES[machine]:
            continue
        offset, size = (
            (int.from_bytes(record[8:16], "big"), int.from_bytes(record[16:24], "big"))
            if wide
            else (int.from_bytes(record[8:12], "big"), int.from_bytes(record[12:16], "big"))
        )
        if size < 8 or offset < 8 + count * width or offset + size > len(data):
            raise PreparationError("The universal executable slice lies outside its container")
        return data[offset : offset + size]
    raise PreparationError(f"The universal executable has no {machine} slice")


@dataclass(frozen=True)
class _BiberToolchain:
    """A runtime-owned single-architecture Biber with its extracted PAR payload.

    ``executable`` lives beside ``payload`` rather than inside it, so no granted
    program is ever reachable from its own writable workspace during warm-up.
    """

    executable: Path
    payload: Path
    identity: tuple[int, int, int, int]
    payload_identity: tuple[int, int]

    def validate(self) -> None:
        """Confirm the granted copies are still the ones this runner prepared."""
        info = self.executable.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or (
                info.st_dev,
                info.st_ino,
                info.st_size,
                info.st_mtime_ns,
            )
            != self.identity
        ):
            raise PreparationError("The prepared bibliography tool was modified or replaced")
        info = self.payload.lstat()
        if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != self.payload_identity:
            raise PreparationError("The prepared bibliography payload was replaced")


def _seal_toolchain(root: Path) -> None:
    """Drop every write bit so granted execution cannot be redirected later."""
    for path in sorted(root.rglob("*"), reverse=True):
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            path.chmod(0o500)
        elif stat.S_ISREG(info.st_mode):
            path.chmod(0o500 if info.st_mode & 0o111 else 0o400)
        else:
            raise PreparationError("The prepared tool payload contains an unsupported entry")
    root.chmod(0o500)


def _linux_tool_path(path: Path) -> bool:
    """Never infer a Linux toolchain grant from a private executable directory."""
    return any(_inside(path, prefix) for prefix in _LINUX_TOOL_PREFIXES)


def _inside(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _tool_path(name: str) -> Path:
    found = shutil.which(name)
    if not found:
        raise PreparationError(f"Required tool is unavailable: {name}. Install it explicitly.")
    path = Path(found).absolute()
    # Resolve the TeX distribution directory links while preserving engine names
    # such as pdflatex (argv[0] selects the TeX format).
    return path.resolve() if path.name == "latexmk" else path.parent.resolve() / path.name


def _distribution_roots(executable: Path) -> set[Path]:
    """Declare installed resources, without allowing an executable's home folder."""
    resolved = executable.resolve()
    roots: set[Path] = {resolved}
    parts = resolved.parts
    if "texlive" in parts:
        index = parts.index("texlive")
        if len(parts) > index + 1 and re.fullmatch(r"\d{4}", parts[index + 1]):
            roots.add(Path(*parts[: index + 2]))
    if "Cellar" in parts:
        index = parts.index("Cellar")
        if len(parts) > index + 2:
            package = Path(*parts[: index + 3])
            cellar = Path(*parts[: index + 1])
            roots.update(_homebrew_runtime_roots(package, cellar))
    return roots


def _homebrew_runtime_roots(package: Path, cellar: Path) -> set[Path]:
    """Grant receipt-declared packages, including same-formula upgraded opt links.

    Homebrew updates opt links independently of dependent installation receipts.
    Both the recorded version and current alias may be needed by installed Mach-O
    load commands. Never grant the Cellar, opt directory, or an external symlink.
    """
    roots: set[Path] = set()
    pending = [package]
    seen: set[Path] = set()
    receipt_bytes = 0
    while pending and len(seen) < 256:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        if not current.is_dir() or not _inside(current, cellar):
            continue
        roots.add(current)
        alias = cellar.parent / "opt" / current.parent.name
        if alias.resolve() == current:
            roots.add(alias)
        receipt = current / "INSTALL_RECEIPT.json"
        try:
            size = receipt.stat().st_size
            receipt_bytes += size
            if size > 1_048_576 or receipt_bytes > 8_388_608:
                continue
            dependencies = json.loads(receipt.read_text()).get("runtime_dependencies", [])
            if not isinstance(dependencies, list) or len(dependencies) > 256:
                continue
            for dependency in dependencies:
                if not isinstance(dependency, dict):
                    continue
                name = str(dependency.get("full_name", "")).rsplit("/", 1)[-1]
                version = dependency.get("pkg_version", dependency.get("version"))
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9@+_.-]*", name):
                    continue
                family = cellar / name
                candidates = []
                if isinstance(version, str) and re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9_.+-]*", version
                ):
                    candidates.append((family / version).resolve())
                candidate_alias = cellar.parent / "opt" / name
                candidates.append(candidate_alias.resolve())
                for candidate in candidates:
                    if candidate.parent != family or not candidate.is_dir():
                        continue
                    pending.append(candidate)
        except (OSError, ValueError, TypeError, RecursionError):
            # Missing/invalid provenance tightens the grant; never scan neighbors.
            continue
    return roots


@dataclass(frozen=True)
class _ToolContext:
    executable: Path
    executables: frozenset[Path]
    resource_roots: frozenset[Path]
    search_paths: tuple[str, ...]


@dataclass
class _ProcessGroup:
    process: asyncio.subprocess.Process
    workspace: Path
    memory_mb: int
    reserved_memory_mb: int
    failure: asyncio.Future[str]
    finished: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class _RuntimeStatistics:
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


def _sample_interval(start: float) -> float:
    """Sample tightly while a job ramps up, then relax to keep tree walks cheap.

    Runaway allocation and output growth almost always appear in the first
    seconds, where the shorter interval bounds the overshoot; afterwards the
    repeated walk of the whole job tree costs more than the added precision.
    """
    return 0.1 if time.monotonic() - start < 3.0 else 0.5


_CleanupResult = TypeVar("_CleanupResult")


async def _finish_cleanup(work: Awaitable[_CleanupResult]) -> _CleanupResult:
    """Complete owned cleanup despite repeated cancellation of its caller."""
    task = asyncio.ensure_future(work)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


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
        self._resolved_tools: dict[str, Path] = {}
        self._contexts: dict[Path, _ToolContext] = {}
        self._toolchain_executables: frozenset[Path] | None = None
        self._toolchain_roots: frozenset[Path] | None = None
        self._version_locks: dict[tuple[str, ...], asyncio.Lock] = {}
        self._versions: dict[tuple[str, ...], dict[str, object]] = {}
        self._version_names: dict[tuple[str, ...], str] = {}
        self._frozen_inputs: dict[Path, Path] = {}
        self._frozen_files: dict[Path, tuple[int, int, int, int]] = {}
        self._artifact_stores: dict[Path, tuple[int, int]] = {}
        self._groups: dict[int, _ProcessGroup] = {}
        self._workspace_claims: set[Path] = set()
        self._job_root: Path | None = None
        self._job_max_temp_bytes = 0
        self._job_monitor: asyncio.Task[None] | None = None
        self._job_failure: str | None = None
        self._biber_toolchain: _BiberToolchain | None = None
        self._biber_ready = False
        self._biber_failure: str | None = None
        self._biber_preparation = asyncio.Lock()
        self._metrics = _RuntimeStatistics(
            memory_limit_mb=budget.memory_mb if budget else self.limits.memory_mb,
            parent_memory_reservation_mb=budget.parent_memory_mb if budget else None,
        )

    @property
    def source_date_epoch(self) -> str:
        return self._source_date_epoch

    @property
    def resource_roots(self) -> frozenset[Path]:
        """Stable toolchain roots; command results carry their exact declared roots."""
        return self._tool_context(Path("/bin/sh")).resource_roots

    @property
    def tool_versions(self) -> dict[str, object]:
        result: dict[str, object] = {}
        for key in sorted(self._versions):
            name = self._version_names[key]
            if name in result:
                name = " ".join(key)
            result[name] = copy.deepcopy(self._versions[key])
        return result

    def statistics(self) -> dict[str, object]:
        return {**asdict(self._metrics), "resource_exceeded": self._job_failure}

    def _resolve_tool(self, name: str) -> Path:
        if name not in self._resolved_tools:
            self._resolved_tools[name] = _tool_path(name)
        return self._resolved_tools[name]

    def _tool_context(self, executable: Path) -> _ToolContext:
        if executable in self._contexts:
            return self._contexts[executable]
        if self._toolchain_executables is None:
            declared = {
                Path("/usr/bin/perl"),
                Path("/usr/bin/env"),
                Path("/bin/sh"),
                Path("/bin/bash"),
                *Path("/usr/bin").glob("perl5.*"),
            }
            for name in _CHILD_TOOLS:
                found = shutil.which(name)
                if found:
                    path = Path(found).absolute()
                    resolved = (
                        path.resolve() if name == "latexmk" else path.parent.resolve() / path.name
                    )
                    if sys.platform == "linux" and not (
                        _linux_tool_path(path) and _linux_tool_path(resolved.resolve())
                    ):
                        continue
                    self._resolved_tools.setdefault(name, resolved)
                    declared.update((path, resolved))
            self._toolchain_executables = frozenset(declared)
        executables = self._toolchain_executables | {executable}
        if self._toolchain_roots is None:
            resources = _LINUX_SYSTEM_RESOURCES if sys.platform == "linux" else _SYSTEM_RESOURCES
            roots = {Path(value).resolve() for value in resources}
            if sys.platform == "linux":
                # Preserve distribution aliases (such as /lib -> /usr/lib) as
                # distinct mount destinations while exposing only their contents.
                roots.update(Path(value) for value in resources)
            else:
                roots.add(Path("/private/var/select/sh"))
            for item in self._toolchain_executables:
                roots.update(_distribution_roots(item))
            font_configs = (
                ()
                if sys.platform == "linux"
                else (Path("/opt/homebrew/etc/fonts"), Path("/usr/local/etc/fonts"))
            )
            for candidate in font_configs:
                if candidate.exists():
                    roots.add(candidate.resolve())
            self._toolchain_roots = frozenset(roots)
        roots = self._toolchain_roots | _distribution_roots(executable)
        search_paths = tuple(
            dict.fromkeys(
                [
                    str(executable.parent),
                    "/usr/bin",
                    "/bin",
                    *sorted({str(item.parent.resolve()) for item in executables}),
                ]
            )
        )
        context = _ToolContext(executable, executables, frozenset(roots), search_paths)
        self._contexts[executable] = context
        return context

    @staticmethod
    def _file_identity(path: Path) -> tuple[int, int, int, int]:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise PreparationError(f"A declared input must be a regular file: {path.name}")
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns

    def freeze_input(self, source: Path, storage: Path) -> Path:
        """Copy once into a runtime-owned directory; grant only the recorded copy."""
        source_key = source.absolute()
        if source_key in self._frozen_files:
            self._validate_readonly_inputs((source_key,))
            return source_key
        if source_key in self._frozen_inputs:
            frozen = self._frozen_inputs[source_key]
            self._validate_readonly_inputs((frozen,))
            return frozen
        # Validate before resolving: resolving a final symlink would hide it.
        identity = self._file_identity(source_key)
        source = source_key.resolve(strict=True)
        if identity[2] > self.limits.max_file_bytes:
            raise PreparationError("A frozen input exceeded the file size limit")
        storage = storage.absolute()
        if storage.is_symlink():
            raise PreparationError("A frozen-input directory cannot be a symlink")
        storage = storage.resolve()
        if storage not in self._artifact_stores:
            try:
                storage.mkdir(mode=0o700, parents=True, exist_ok=False)
            except FileExistsError as error:
                raise PreparationError(
                    "Frozen inputs require a new runtime-owned directory"
                ) from error
            info = storage.stat()
            self._artifact_stores[storage] = info.st_dev, info.st_ino
        info = storage.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or (info.st_dev, info.st_ino) != self._artifact_stores[storage]
        ):
            raise PreparationError("The frozen-input directory was replaced")
        snapshot = Path(tempfile.mkdtemp(prefix="input-", dir=storage))
        info = snapshot.stat()
        self._artifact_stores[snapshot] = info.st_dev, info.st_ino
        destination = snapshot / source.name
        try:
            descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as incoming, destination.open("xb") as outgoing:
                opened = os.fstat(incoming.fileno())
                if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != identity:
                    raise PreparationError("An input changed before it could be frozen")
                remaining = self.limits.max_file_bytes + 1
                while remaining and (chunk := incoming.read(min(1_048_576, remaining))):
                    outgoing.write(chunk)
                    remaining -= len(chunk)
                if not remaining or self._file_identity(source) != identity:
                    raise PreparationError("An input changed or exceeded its limit while freezing")
        except BaseException:
            # Only our freshly created, unpublished partial snapshot is removed.
            destination.unlink(missing_ok=True)
            raise
        destination.chmod(0o400)
        self._frozen_files[destination] = self._file_identity(destination)
        self._frozen_inputs[source_key] = destination
        return destination

    def _validate_readonly_inputs(self, inputs: tuple[Path, ...]) -> tuple[Path, ...]:
        validated = []
        for value in inputs:
            path = value.absolute()
            if path not in self._frozen_files or path.resolve() != path:
                raise PreparationError("Read-only grants require exact runtime-frozen input files")
            if self._file_identity(path) != self._frozen_files[path]:
                raise PreparationError("A frozen input was modified or replaced")
            parent = path.parent
            info = parent.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or (info.st_dev, info.st_ino) != self._artifact_stores[parent]
            ):
                raise PreparationError("A frozen-input directory was modified or replaced")
            validated.append(path)
        return tuple(validated)

    async def prepare_biber(self, root: Path) -> Path | None:
        """Prepare a sandbox-executable Biber once per runner; ``None`` keeps the plain tool.

        TeX Live ships Biber on macOS as a PAR-packed universal binary. The
        universal wrapper shells out to the Xcode ``lipo`` stub, and the packed
        payload execs itself out of ``$PAR_GLOBAL_TEMP``; neither is reachable
        under a fail-closed profile. A single-architecture slice plus a warmed,
        then read-only, payload directory is, and both live outside every tool
        workspace. Linux Bubblewrap already permits execution inside the bound
        workspace, so nothing is prepared there.
        """
        if sys.platform != "darwin":
            return None
        async with self._biber_preparation:
            if self._biber_ready and self._biber_toolchain is not None:
                return self._biber_toolchain.executable
            if self._biber_failure is not None:
                raise PreparationError(self._biber_failure)
            try:
                return await self._prepare_biber(root)
            except (OSError, PreparationError) as error:
                self._biber_failure = str(error)
                self._biber_toolchain = None
                # An unusable copy must never stay the resolved tool: a later
                # probe would run it with no grants instead of reporting this.
                self._resolved_tools.pop("biber", None)
                raise PreparationError(self._biber_failure) from error

    async def _prepare_biber(self, root: Path) -> Path:
        source = _tool_path("biber")
        if source.stat().st_size > self.limits.max_file_bytes:
            raise PreparationError("The installed biber exceeds the tool file size limit")
        image = _host_macho_slice(source.read_bytes())
        # The payload is counted against the job temporary budget: it is created
        # under the monitored job root whenever a job scope is active.
        base = (self._job_root or root.resolve(strict=True)) / ".toolchain"
        base = base / f"biber-{hashlib.sha256(image).hexdigest()[:16]}"
        executable, payload = base / "biber", base / "payload"
        if not _INERT_PATH.fullmatch(str(executable)):
            raise PreparationError(
                "An isolated bibliography tool needs a path of letters, digits, underscores, "
                f"dots, slashes or hyphens: {executable}"
            )
        try:
            base.mkdir(mode=0o700, parents=True, exist_ok=False)
        except FileExistsError as error:
            raise PreparationError(
                "An isolated bibliography tool requires a new runtime-owned directory"
            ) from error
        with executable.open("xb") as outgoing:
            outgoing.write(image)
        executable.chmod(0o500)
        payload.mkdir(mode=0o700)
        info = payload.stat()
        self._resolved_tools["biber"] = executable
        self._biber_toolchain = _BiberToolchain(
            executable,
            payload,
            self._file_identity(executable),
            (info.st_dev, info.st_ino),
        )
        # The first exec stage unpacks only a handful of files, so the warm-up
        # itself must be allowed to execute inside the payload directory.
        await self.tool_version(["biber", "--noconf", "--version"], payload)
        _seal_toolchain(base)
        self._biber_toolchain = replace(
            self._biber_toolchain, identity=self._file_identity(executable)
        )
        self._biber_ready = True
        return executable

    def _biber_scope(self, command: list[str]) -> _BiberToolchain | None:
        """Return the prepared payload only when this command may execute Biber.

        The runtime-owned path appears in the version probe and in the ``$biber``
        rule of a latexmk build that permits Biber. A build that disabled Biber
        never mentions it and therefore never receives these grants.
        """
        toolchain = self._biber_toolchain
        if toolchain is None or not any(str(toolchain.executable) in arg for arg in command):
            return None
        toolchain.validate()
        return toolchain

    async def tool_version(self, argv: list[str], workspace: Path) -> dict[str, object]:
        if not argv or any(not isinstance(arg, str) or "\0" in arg for arg in argv):
            raise PreparationError("A version command must contain non-NUL string arguments")
        executable = self._resolve_tool(argv[0])
        key = (str(executable), *argv[1:])
        if key in self._versions:
            return copy.deepcopy(self._versions[key])
        if self.budget is not None and self.budget.current_lease is None:
            # Admission precedes the keyed lock: a waiter must not hold the lock
            # while another lease owner waits for that same version.
            memory = min(self.limits.memory_mb, 256, self.budget.available_memory_mb)
            async with self.budget.lease("tool version " + argv[0], memory_mb=memory):
                return await self.tool_version(argv, workspace)
        lock = self._version_locks.setdefault(key, asyncio.Lock())
        async with lock:
            if key not in self._versions:
                self._metrics.version_probes = int(self._metrics.version_probes) + 1
                result = await self.run(argv, cwd=workspace, workspace=workspace)
                lines = (result.stdout + result.stderr).splitlines()
                expected_detach = (
                    executable.name == "pdfdetach"
                    and argv[1:] == ["-v"]
                    and any(re.match(r"^pdfdetach version \S+", line) for line in lines)
                )
                if (
                    (result.returncode != 0 and not (result.returncode == 99 and expected_detach))
                    or result.timed_out
                    or result.output_limited
                    or result.resource_exceeded
                    or not lines
                    or "Error:" in result.stderr
                ):
                    raise PreparationError(f"Cannot verify tool {argv[0]}: {result.stderr[:500]}")
                self._versions[key] = {
                    "version": lines[:3],
                    "executable": result.command[0],
                    "version_arguments": list(argv[1:]),
                }
                self._version_names[key] = Path(argv[0]).name
            return copy.deepcopy(self._versions[key])

    def _sandbox_command(
        self,
        command: list[str],
        workspace: Path,
        readonly_inputs: tuple[Path, ...] = (),
    ) -> list[str]:
        if sys.platform == "linux":
            return self._linux_sandbox_command(command, workspace, readonly_inputs)
        if sys.platform != "darwin":
            raise PreparationError(
                "Restricted execution is unavailable on this platform; supported backends "
                "require macOS sandbox-exec or Linux Bubblewrap with user namespaces."
            )
        backend = shutil.which("sandbox-exec", path="/usr/bin:/bin")
        if backend is None:
            raise PreparationError("Restricted execution requires macOS sandbox-exec")
        context = self._tool_context(Path(command[0]))
        executables, roots = context.executables, context.resource_roots
        if any(_inside(path.resolve(), workspace) for path in executables):
            raise PreparationError("Executing a project-supplied program is not permitted")
        if any(_inside(workspace, root) for root in roots if root.is_dir()):
            raise PreparationError("A job workspace cannot be inside a declared toolchain tree")
        inputs = self._validate_readonly_inputs(readonly_inputs)
        toolchain = self._biber_scope(command)
        isolated = () if toolchain is None else (toolchain.executable, toolchain.payload)

        def quote(path: Path) -> str:
            return json.dumps(str(path), ensure_ascii=False)

        reads = [f"(subpath {quote(workspace)})"]
        mappings = []
        for root in sorted(roots):
            rule = f"({'subpath' if root.is_dir() else 'literal'} {quote(root)})"
            reads.append(rule)
            mappings.append(rule)
        for path in sorted(executables | set(inputs)):
            reads.append(f"(literal {quote(path)})")
        ancestors = {
            parent
            for root in roots | {workspace} | set(inputs) | set(isolated)
            for parent in root.parents
        }
        metadata = " ".join(f"(literal {quote(parent)})" for parent in sorted(ancestors))
        execution = " ".join(
            f"(literal {quote(path)})"
            for path in sorted(executables | {item.resolve() for item in executables})
        )
        # A read-only runtime-owned payload outside every tool workspace; execution
        # still never originates in project-writable storage.
        targets = " ".join(
            f"({'subpath' if path.is_dir() else 'literal'} {quote(path)})" for path in isolated
        )
        granted = (
            ()
            if not targets
            else (
                f"(allow process-exec {targets})",
                f"(allow file-read* {targets})",
                f"(allow file-map-executable {targets})",
            )
        )
        profile = "\n".join(
            (
                "(version 1)",
                "(deny default)",
                "(allow process-fork)",
                f"(allow process-exec {execution})",
                "(allow sysctl-read)",
                '(allow file-read* (literal "/"))',
                f"(allow file-read* {' '.join(reads)})",
                f"(allow file-map-executable {' '.join(mappings)})",
                f"(allow file-read-metadata {metadata})",
                f"(allow file-write* (subpath {quote(workspace)}))",
                '(allow file-write-data (literal "/dev/null"))',
                *granted,
            )
        )
        return [backend, "-p", profile, *command]

    def _linux_sandbox_command(
        self,
        command: list[str],
        workspace: Path,
        readonly_inputs: tuple[Path, ...],
    ) -> list[str]:
        backend = shutil.which("bwrap", path="/usr/bin:/bin")
        if backend is None:
            raise PreparationError(
                "Restricted Linux execution requires Bubblewrap and permitted user namespaces; "
                "install or enable them explicitly. No unrestricted fallback is available."
            )
        if not _linux_tool_path(Path(backend).resolve()):
            raise PreparationError("The Linux sandbox backend must use a system executable")
        context = self._tool_context(Path(command[0]))
        if any(
            not _linux_tool_path(path) or not _linux_tool_path(path.resolve())
            for path in context.executables
        ):
            raise PreparationError(
                "Linux tool executables must be installed under the supported system prefixes; "
                "private-home and project executables cannot become toolchain grants."
            )
        roots = {path for path in context.resource_roots if path.exists()}
        system_paths = {Path(value) for value in _LINUX_SYSTEM_RESOURCES}
        for path in roots:
            if (not _linux_tool_path(path) and path not in system_paths) or (
                not _linux_tool_path(path.resolve()) and path.resolve() not in system_paths
            ):
                raise PreparationError("Linux toolchain resources must use declared system paths")
            if _inside(workspace, path.resolve()) or _inside(path.resolve(), workspace):
                raise PreparationError("A job workspace cannot overlap a declared toolchain tree")
        inputs = self._validate_readonly_inputs(readonly_inputs)
        if any(_inside(path, workspace) for path in inputs):
            raise PreparationError("Read-only inputs must be outside the writable workspace")
        # Every namespace below is mandatory. Do not use *-try, share-net, or
        # automatic retries: missing kernel/backend support is a closed failure.
        wrapped = [
            backend,
            "--unshare-user",
            "--unshare-ipc",
            "--unshare-pid",
            "--unshare-net",
            "--unshare-uts",
            "--unshare-cgroup",
            "--disable-userns",
            "--cap-drop",
            "ALL",
            "--new-session",
            "--die-with-parent",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
        ]
        # Mount exact binaries as well as resources: /usr/bin, /etc, /home and
        # the host root are never directory grants. Same-path mounts preserve
        # TeX lookup and interpreter contracts without copying host configuration.
        mounts = roots | {path for path in context.executables if path.exists()} | set(inputs)
        for path in sorted(mounts, key=lambda value: (len(value.parts), str(value))):
            wrapped.extend(("--ro-bind", str(path), str(path)))
        wrapped.extend(("--bind", str(workspace), str(workspace)))
        # Keep anonymous container storage read-only as well: all generated files
        # must remain in the monitored workspace, rather than an uncounted tmpfs.
        for path in ("/proc", "/dev", "/"):
            wrapped.extend(("--remount-ro", path))
        return [*wrapped, "--", *command]

    def _environment(self, workspace: Path, executable: Path) -> dict[str, str]:
        container = workspace / ".tool-state"
        container.mkdir(exist_ok=True)
        state = Path(tempfile.mkdtemp(prefix="invocation-", dir=container))
        for name in ("tmp", "config", "cache", "texmf", "texmf-config", "texmf-var"):
            (state / name).mkdir()
        context = self._tool_context(executable)
        # HOME and CODEX_HOME are deliberately neither inherited nor repurposed.
        env = {
            "PATH": os.pathsep.join(context.search_paths),
            "LANG": "C",
            "LC_ALL": "C",
            "TMPDIR": str(state / "tmp"),
            "TMP": str(state / "tmp"),
            "TEMP": str(state / "tmp"),
            "XDG_CONFIG_HOME": str(state / "config"),
            "XDG_CACHE_HOME": str(state / "cache"),
            "TEXMFHOME": str(state / "texmf"),
            "TEXMFCONFIG": str(state / "texmf-config"),
            "TEXMFVAR": str(state / "texmf-var"),
            "TEXMFOUTPUT": str(state / "tmp"),
            "TEXMFLOCAL": str(state / "texmf"),
            "VARTEXFONTS": str(state / "texmf-var"),
            "TEXINPUTS": ".:",
            "BIBINPUTS": ".:",
            "BSTINPUTS": ".:",
            "shell_escape": "f",
            "openin_any": "r",
            "openout_any": "p",
            "MKTEXPK": "0",
            "MKTEXTFM": "0",
            "MKTEXFMT": "0",
            "MKTEXMF": "0",
            "OMP_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1",
            "VECLIB_MAXIMUM_THREADS": "1",
            "RAYON_NUM_THREADS": "1",
            "PAR_TMPDIR": str(state / "tmp"),
            "SOURCE_DATE_EPOCH": self.source_date_epoch,
            "FORCE_SOURCE_DATE": "1",
        }
        tex_roots = sorted(
            root for root in context.resource_roots if re.fullmatch(r"\d{4}", root.name)
        )
        if tex_roots:
            root = tex_roots[0]
            env.update(
                {
                    "TEXMFCNF": str(root / "texmf-dist/web2c"),
                    "TEXMFSYSCONFIG": str(root / "texmf-config"),
                    "TEXMFSYSVAR": str(root / "texmf-var"),
                    "TEXMF": "{"
                    + ",".join(
                        map(
                            str,
                            (
                                state / "texmf-config",
                                state / "texmf-var",
                                state / "texmf",
                                root / "texmf-config",
                                root / "texmf-var",
                                root / "texmf-dist",
                            ),
                        )
                    )
                    + "}",
                }
            )
        return env

    def _limited_command(self, command: list[str]) -> list[str]:
        # Fixed trusted shell; command arguments are never interpreted as shell source.
        script = (
            'ulimit -c 0 || exit 125; ulimit -f "$1" || exit 125; '
            'ulimit -t "$2" || exit 125; ulimit -n 256 || exit 125; '
            'shift 2; exec "$@"'
        )
        # `ulimit -f` counts 1024-byte units in bash outside POSIX mode, which is
        # what the computed argument assumes. Dash, the Debian/Ubuntu /bin/sh,
        # counts 512-byte blocks instead and would halve the limit; macOS /bin/sh
        # is bash and already agrees with /bin/bash here.
        shell = "/bin/bash" if os.path.isfile("/bin/bash") else "/bin/sh"
        return [
            shell,
            "-c",
            script,
            "latexprep-limits",
            str(self.limits.max_file_bytes // 1024),
            str(self.limits.timeout_seconds),
            *command,
        ]

    async def _process_sample(self) -> list[tuple[int, int, int]]:
        launch = asyncio.create_task(
            asyncio.create_subprocess_exec(
                "/bin/ps",
                "-axo",
                "pid=,ppid=,pgid=,rss=" if sys.platform == "linux" else "pid=,pgid=,rss=",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
            )
        )
        try:
            probe = await asyncio.shield(launch)
        except asyncio.CancelledError:

            async def stop_probe() -> None:
                try:
                    pending = await launch
                except (OSError, ValueError):
                    return
                if pending.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        pending.kill()
                await pending.communicate()

            await _finish_cleanup(stop_probe())
            raise
        try:
            # asyncio.wait_for in Python 3.11 can drop an external cancellation that
            # arrives as the probe finishes (bpo-42130); asyncio.timeout does not.
            async with asyncio.timeout(2):
                output, _ = await probe.communicate()
        finally:
            if probe.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    probe.kill()
            await _finish_cleanup(probe.wait())
        if probe.returncode:
            raise PreparationError("Process memory accounting is unavailable")
        if sys.platform == "linux":
            return _linux_process_groups(output, set(self._groups))
        rows = []
        for line in output.splitlines():
            fields = line.split()
            if len(fields) == 3:
                rows.append((int(fields[0]), int(fields[1]), int(fields[2])))
        if not rows:
            raise PreparationError("Process memory accounting returned no processes")
        return rows

    def _workspace_usage(self, workspace: Path) -> tuple[int, str | None]:
        """Walk one workspace. Blocking: callers on the event loop use a worker thread."""
        total = count = 0
        for directory, directories, filenames in os.walk(workspace, followlinks=False):
            count += len(directories) + len(filenames)
            if count > 20_000:
                return total, "Workspace entry count exceeded 20000"
            for name in filenames:
                with contextlib.suppress(FileNotFoundError):
                    size = (Path(directory) / name).lstat().st_size
                    if size > self.limits.max_file_bytes:
                        return total, "A generated file exceeded the file size limit"
                    total += size
                    if total > 4 * self.limits.max_file_bytes:
                        return total, "Workspace exceeded the total disk limit"
        return total, None

    def _job_storage(self, workspaces: tuple[Path, ...]) -> tuple[tuple[str | None, ...], int]:
        """Walk every active workspace and the whole job tree in one worker-thread pass."""
        assert self._job_root is not None
        failures = tuple(self._workspace_usage(workspace)[1] for workspace in workspaces)
        total = 0
        for directory, _, files in os.walk(self._job_root, followlinks=False):
            for name in files:
                with contextlib.suppress(FileNotFoundError):
                    total += (Path(directory) / name).lstat().st_size
        return failures, total

    @staticmethod
    def _group_usage(
        rows: list[tuple[int, int, int]], pid: int, memory_mb: int
    ) -> tuple[int, str | None]:
        sizes = [rss for _, group, rss in rows if group == pid]
        resident_kb = sum(sizes)
        if resident_kb > memory_mb * 1024:
            return resident_kb, f"Aggregate tool memory exceeded {memory_mb} MiB"
        if len(sizes) > 32:
            return resident_kb, "Tool process count exceeded 32"
        return resident_kb, None

    async def _resource_monitor(self, pid: int, workspace: Path) -> str:
        """Standalone-run fallback; jobs use the shared monitor below."""
        start = time.monotonic()
        while True:
            await asyncio.sleep(_sample_interval(start))
            rows = await self._process_sample()
            _, failure = self._group_usage(rows, pid, self._groups[pid].memory_mb)
            if failure:
                return failure
            _, failure = await asyncio.to_thread(self._workspace_usage, workspace)
            if failure:
                return failure

    def _fail_group(self, registration: _ProcessGroup, message: str) -> None:
        if not registration.failure.done():
            registration.failure.set_result(message)
        _kill_group(registration.process.pid)

    def _fail_job(self, message: str) -> None:
        if self._job_failure is None:
            self._job_failure = message
        for registration in tuple(self._groups.values()):
            self._fail_group(registration, self._job_failure)

    async def _sample_job(self) -> None:
        assert self._job_root is not None
        start = time.monotonic()
        try:
            groups = dict(self._groups)
            try:
                rows = await self._process_sample()
            except Exception as error:
                self._metrics.memory_accounting = f"unavailable: {error}"
                if groups or self._groups or self._metrics.commands:
                    self._fail_job(f"Job resource accounting failed: {error}")
                rows = []
            if rows:
                parent_kb = sum(rss for pid, _, rss in rows if pid == os.getpid())
                if not parent_kb:
                    self._metrics.memory_accounting = "unavailable: parent process absent"
                    if groups or self._metrics.commands:
                        self._fail_job("Job resource accounting omitted the parent process")
                else:
                    self._metrics.memory_accounting = "sampled parent and registered process groups"
                tool_kb = sum(
                    rss for pid, group, rss in rows if group in groups and pid != os.getpid()
                )
                for key, value in (
                    ("peak_parent_rss_mb", parent_kb / 1024),
                    ("peak_tool_rss_mb", tool_kb / 1024),
                    ("peak_total_rss_mb", (parent_kb + tool_kb) / 1024),
                ):
                    setattr(self._metrics, key, max(getattr(self._metrics, key) or 0, value))
                limit = self.budget.memory_mb if self.budget else self.limits.memory_mb
                if parent_kb + tool_kb > limit * 1024:
                    self._fail_job(
                        f"Aggregate job memory including the parent exceeded {limit} MiB"
                    )
                for pid, registration in groups.items():
                    resident_kb, failure = self._group_usage(rows, pid, registration.memory_mb)
                    self._metrics.peak_command_rss_mb = max(
                        self._metrics.peak_command_rss_mb or 0,
                        resident_kb / 1024,
                    )
                    if failure:
                        self._fail_group(registration, failure)
            # Walking the workspaces and the job tree is blocking and grows with
            # the toolchain payload and build output, so it never runs inline.
            registrations = tuple(groups.values())
            failures, total = await asyncio.to_thread(
                self._job_storage, tuple(item.workspace for item in registrations)
            )
            for registration, failure in zip(registrations, failures, strict=True):
                if failure:
                    self._fail_group(registration, failure)
            self._metrics.peak_temp_bytes = max(int(self._metrics.peak_temp_bytes), total)
            if total > self._job_max_temp_bytes:
                self._fail_job(f"Job temporary storage exceeded {self._job_max_temp_bytes} bytes")
        finally:
            self._metrics.samples = int(self._metrics.samples) + 1
            self._metrics.monitor_seconds = (
                float(self._metrics.monitor_seconds) + time.monotonic() - start
            )

    async def _monitor_job(self) -> None:
        start = time.monotonic()
        try:
            while True:
                await self._sample_job()
                # A sample that swallowed this task's cancellation must not keep the
                # monitor alive, or job_scope would wait for it indefinitely.
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise asyncio.CancelledError
                interval = _sample_interval(start)
                self._metrics.sample_interval_seconds = interval
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._fail_job(f"Job resource accounting failed: {error}")

    @contextlib.asynccontextmanager
    async def job_scope(self, root: Path, *, max_temp_bytes: int) -> AsyncGenerator[ToolRunner]:
        if self._job_root is not None or self._groups:
            raise PreparationError("A runner can have only one active job scope")
        if type(max_temp_bytes) is not int or max_temp_bytes < 1:
            raise PreparationError("The job temporary-byte budget must be a positive integer")
        root = root.resolve(strict=True)
        if not root.is_dir():
            raise PreparationError("The job scope requires a directory")
        self._job_root = root
        self._job_max_temp_bytes = max_temp_bytes
        self._job_monitor = asyncio.create_task(self._monitor_job())
        try:
            yield self
        finally:

            async def close() -> None:
                try:
                    if self._groups:
                        registrations = tuple(self._groups.values())
                        self._fail_job("Job scope closed while a command was active")
                        await asyncio.gather(*(item.finished.wait() for item in registrations))
                    assert self._job_monitor is not None
                    self._job_monitor.cancel()
                    await asyncio.gather(self._job_monitor, return_exceptions=True)
                    # Include very short/source-only jobs in the final sample.
                    await self._sample_job()
                finally:
                    self._job_monitor = None
                    self._job_root = None

            await _finish_cleanup(close())

    async def run(
        self,
        argv: list[str],
        cwd: Path,
        workspace: Path,
        *,
        readonly_inputs: tuple[Path, ...] = (),
        memory_mb: int | None = None,
    ) -> CommandResult:
        if not argv or any(not isinstance(arg, str) or "\0" in arg for arg in argv):
            raise PreparationError("A tool command must contain non-NUL string arguments")
        if memory_mb is not None and (type(memory_mb) is not int or memory_mb < 1):
            raise PreparationError("A command memory cap must be a positive integer")
        if self.budget is not None and self.budget.current_lease is None:
            memory = memory_mb or min(self.limits.memory_mb, 256, self.budget.available_memory_mb)
            async with self.budget.lease(
                "tool " + (argv[0] if argv else "unknown"), memory_mb=memory
            ):
                return await self._run_command(argv, cwd, workspace, readonly_inputs, memory_mb)
        return await self._run_command(argv, cwd, workspace, readonly_inputs, memory_mb)

    async def _run_command(
        self,
        argv: list[str],
        cwd: Path,
        workspace: Path,
        readonly_inputs: tuple[Path, ...],
        memory_mb: int | None,
    ) -> CommandResult:
        if not argv or any(not isinstance(arg, str) or "\0" in arg for arg in argv):
            raise PreparationError("A tool command must contain non-NUL string arguments")
        workspace = workspace.resolve(strict=True)
        cwd = cwd.resolve(strict=True)
        if not workspace.is_dir() or not cwd.is_dir() or not _inside(cwd, workspace):
            raise PreparationError("Tool working directory must be inside its own workspace")
        if self._job_root is not None and not _inside(workspace, self._job_root):
            raise PreparationError("Tool workspaces must be inside the monitored job directory")
        if any(_inside(path, workspace) for path in self._frozen_files):
            raise PreparationError("A tool's writable workspace cannot contain frozen inputs")
        if any(
            _inside(path, workspace) or _inside(workspace, path) for path in self._workspace_claims
        ):
            raise PreparationError(
                "Concurrent tools need separate non-overlapping writable workspaces"
            )
        if self._job_failure:
            raise PreparationError(self._job_failure)
        inputs = self._validate_readonly_inputs(readonly_inputs)
        executable = self._resolve_tool(argv[0])
        if _inside(executable.resolve(), workspace):
            raise PreparationError("Executing a project-supplied program is not permitted")
        command = [str(executable), *argv[1:]]
        context = self._tool_context(executable)
        wrapped = self._sandbox_command(command, workspace, inputs)
        environment = self._environment(workspace, executable)
        toolchain = self._biber_scope(command)
        if toolchain is not None:
            # PAR execs its payload out of this directory; point it at the warmed,
            # runtime-owned copy instead of the writable per-invocation temporary.
            environment["PAR_GLOBAL_TEMP"] = str(toolchain.payload)
        lease = self.budget.current_lease if self.budget is not None else None
        maximum = min(self.limits.memory_mb, lease.memory_mb) if lease else self.limits.memory_mb
        if memory_mb is not None and memory_mb > maximum:
            raise PreparationError("A command memory cap cannot exceed its lease or runtime limit")
        command_memory_mb = maximum if memory_mb is None else memory_mb
        reserved_memory_mb = lease.memory_mb if lease else command_memory_mb
        self._workspace_claims.add(workspace)
        try:
            return await self._execute(
                command,
                wrapped,
                cwd,
                workspace,
                environment,
                command_memory_mb,
                reserved_memory_mb,
                context,
            )
        finally:
            self._workspace_claims.discard(workspace)

    async def _execute(
        self,
        command: list[str],
        wrapped: list[str],
        cwd: Path,
        workspace: Path,
        environment: dict[str, str],
        memory_mb: int,
        reserved_memory_mb: int,
        context: _ToolContext,
    ) -> CommandResult:
        launch = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *self._limited_command(wrapped),
                cwd=cwd,
                env=environment,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                close_fds=True,
            )
        )
        try:
            process = await asyncio.shield(launch)
        except asyncio.CancelledError:

            async def stop_launch() -> None:
                try:
                    pending = await launch
                except (OSError, ValueError):
                    return
                _kill_group(pending.pid)
                await pending.communicate()

            await _finish_cleanup(stop_launch())
            raise
        except (OSError, ValueError) as error:
            raise PreparationError(f"Restricted tool startup failed: {error}") from error
        registration = _ProcessGroup(
            process,
            workspace,
            memory_mb,
            reserved_memory_mb,
            asyncio.get_running_loop().create_future(),
        )
        self._groups[process.pid] = registration
        self._metrics.commands = int(self._metrics.commands) + 1
        self._metrics.peak_active_commands = max(
            int(self._metrics.peak_active_commands), len(self._groups)
        )
        self._metrics.peak_command_memory_reservation_mb = max(
            int(self._metrics.peak_command_memory_reservation_mb),
            sum(item.reserved_memory_mb for item in self._groups.values()),
        )
        self._metrics.peak_command_memory_limit_mb = max(
            self._metrics.peak_command_memory_limit_mb,
            sum(item.memory_mb for item in self._groups.values()),
        )
        buffers = [bytearray(), bytearray()]
        output_limited = False

        async def collect(stream: asyncio.StreamReader, index: int) -> None:
            nonlocal output_limited
            while chunk := await stream.read(65_536):
                if output_limited:
                    # Keep draining after the kill: the process counts as finished
                    # only once every pipe reaches end-of-file, and a reader that
                    # stops early leaves buffered data that stalls its pipe forever.
                    continue
                remaining = self.limits.max_output_bytes - sum(map(len, buffers))
                buffers[index].extend(chunk[: max(0, remaining)])
                if len(chunk) > remaining:
                    output_limited = True
                    _kill_group(process.pid)

        assert process.stdout is not None and process.stderr is not None
        readers = [
            asyncio.create_task(collect(process.stdout, 0)),
            asyncio.create_task(collect(process.stderr, 1)),
        ]
        completion = asyncio.create_task(process.wait())
        monitor = (
            None
            if self._job_monitor is not None
            else asyncio.create_task(self._resource_monitor(process.pid, workspace))
        )
        timed_out = False
        exceeded = None
        try:
            watched = [completion, registration.failure]
            if monitor is not None:
                watched.append(monitor)
            if self._job_failure:
                self._fail_group(registration, self._job_failure)
            done, _ = await asyncio.wait(
                watched, timeout=self.limits.timeout_seconds, return_when=asyncio.FIRST_COMPLETED
            )
            if not done:
                timed_out = True
            elif registration.failure in done:
                exceeded = registration.failure.result()
            elif monitor is not None and monitor in done:
                try:
                    exceeded = monitor.result()
                except Exception as error:
                    exceeded = f"Resource accounting failed: {error}"
        finally:

            async def close() -> None:
                try:
                    _kill_group(process.pid)
                    if monitor is not None:
                        monitor.cancel()
                        await asyncio.gather(monitor, return_exceptions=True)
                    await process.wait()
                    await asyncio.gather(completion, *readers, return_exceptions=True)
                finally:
                    self._groups.pop(process.pid, None)
                    registration.finished.set()

            await _finish_cleanup(close())
        _, final_violation = await _finish_cleanup(
            asyncio.to_thread(self._workspace_usage, workspace)
        )
        exceeded = exceeded or final_violation
        stdout, stderr = (bytes(data).decode("utf-8", errors="replace") for data in buffers)
        if any(
            marker in stderr
            for marker in ("sandbox_apply:", "sandbox initialization failed", "sandbox_compile:")
        ):
            raise PreparationError(f"Restricted execution was refused: {stderr.strip()[:1000]}")
        return CommandResult(
            process.returncode or 0,
            stdout,
            stderr,
            timed_out,
            command,
            output_limited,
            exceeded or self._job_failure,
            tuple(str(root) for root in sorted(context.resource_roots)),
        )


def _linux_process_groups(output: bytes, registered: set[int]) -> list[tuple[int, int, int]]:
    """Account for Bubblewrap descendants even after its --new-session split."""
    records = {}
    for line in output.splitlines():
        fields = line.split()
        if len(fields) != 4:
            raise PreparationError("Linux process accounting returned an incomplete row")
        try:
            pid, parent, group, rss = (int(value) for value in fields)
        except ValueError as error:
            raise PreparationError("Linux process accounting returned invalid values") from error
        if pid < 1 or parent < 0 or group < 0 or rss < 0 or pid in records:
            raise PreparationError("Linux process accounting returned invalid process identities")
        records[pid] = (parent, group, rss)
    if not records:
        raise PreparationError("Process memory accounting returned no processes")
    rows = []
    for pid, (_, group, rss) in records.items():
        current = pid
        visited: set[int] = set()
        while current in records and current not in visited:
            if current in registered:
                group = current
                break
            visited.add(current)
            current = records[current][0]
        rows.append((pid, group, rss))
    return rows


def _kill_group(pid: int) -> None:
    # macOS reports EPERM instead of ESRCH for a group whose leader is already a
    # zombie being reaped; either way no signalable member of our group remains.
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)


def _read_bounded(path: Path, limit: int) -> str:
    if not path.is_file():
        return ""
    with path.open("rb") as stream:
        return stream.read(limit).decode("utf-8", errors="replace")


def _log_tail(log: str, *, lines: int = 60, max_characters: int = 8000) -> str:
    """Keep the closing log lines that explain a failure; reports redact centrally."""
    return "\n".join(log.splitlines()[-lines:])[-max_characters:]


def _log_findings(
    log: str,
    main: str,
    project: Path | None = None,
    cwd: Path | None = None,
) -> list[Finding]:
    findings: list[Finding] = []
    if project is not None:
        project = project.resolve()
    seen: set[tuple[str, str]] = set()
    patterns = (
        (
            "build.undefined_citation",
            r"Citation .+? undefined|There were undefined citations",
            "error",
        ),
        (
            "build.undefined_reference",
            r"Reference .+? undefined|There were undefined references",
            "error",
        ),
        ("build.duplicate_label", r"Label .+? multiply defined|multiply-defined labels", "warning"),
        ("build.missing_character", r"Missing character:", "warning"),
        ("build.font_substitution", r"Font Warning:|font shapes were not available", "warning"),
        ("build.overfull_box", r"Overfull \\[hv]box", "warning"),
        ("build.underfull_box", r"Underfull \\[hv]box", "warning"),
        (
            "build.rerun_required",
            r"Rerun to get|Label\(s\) may have changed|Please \(?re\)?run|"
            r"Please rerun|rerun LaTeX",
            "error",
        ),
        (
            "build.tex_error",
            r"^! |^.+?:\d+: .*(?:Error|Undefined control sequence)|"
            r"Fatal error|Emergency stop",
            "error",
        ),
    )
    lines = log.splitlines()
    for index, line in enumerate(lines):
        message = line.strip()
        if index + 1 < len(lines) and re.match(r"^(?:\s+|\([A-Za-z-]+\))", lines[index + 1]):
            message += " " + lines[index + 1].strip()
        for rule, pattern, severity in patterns:
            if re.search(pattern, message, re.IGNORECASE) and (rule, message) not in seen:
                seen.add((rule, message))
                input_line = re.search(r"(?:input line|at lines?)\s*(\d+)", message)
                explicit = re.match(r"^([^:\n]+):(\d+):", message)
                overflow = re.search(r"\(([\d.]+)pt too (wide|high)\)", message)
                details: dict[str, object] = {"document": main}
                source_path = None
                source_line = None
                if input_line:
                    details["input_line"] = int(input_line[1])
                if explicit:
                    filename = Path(explicit[1])
                    details["compiler_filename"] = explicit[1]
                    details["compiler_line"] = int(explicit[2])
                    if project is not None and cwd is not None:
                        resolved = (
                            filename if filename.is_absolute() else cwd / filename
                        ).resolve()
                        if _inside(resolved, project):
                            source_path = resolved.relative_to(project).as_posix()
                    elif not filename.is_absolute():
                        relative = Path(os.path.normpath(str(Path(main).parent / filename)))
                        if ".." not in relative.parts:
                            source_path = relative.as_posix()
                    if source_path is not None:
                        source_line = int(explicit[2])
                if overflow:
                    details.update({"overflow_pt": float(overflow[1]), "direction": overflow[2]})
                findings.append(
                    Finding(
                        rule,
                        message[:2000],
                        severity,
                        path=source_path,
                        line=source_line,
                        details=details,
                    )
                )
    return findings


def _copy_source(source: Path, destination: Path, max_total_bytes: int) -> None:
    """Import a bounded project snapshot into a disposable writable build copy.

    ``max_total_bytes`` is the per-job import budget: it caps the combined size
    of every imported file rather than the size of any single one. Callers pass
    ``RuntimeLimits.max_file_bytes``, which therefore acts as the whole-tree
    limit here, not as a per-file limit.
    """
    total = count = 0
    for item in source.rglob("*"):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or not (item.is_file() or item.is_dir()):
            raise PreparationError(f"Build inputs must be regular files/directories: {item.name}")
        count += 1
        total += info.st_size if item.is_file() else 0
        if count > 20_000 or total > max_total_bytes:
            raise PreparationError("Build input exceeds file-count or total-size limits")
    shutil.copytree(source, destination)
    # Imported snapshots may be read-only; writable disposable build copies are distinct.
    destination.chmod(0o700)
    for item in destination.rglob("*"):
        item.chmod(0o700 if item.is_dir() else 0o600)


def _bibliography_evidence(output: Path, limit: int) -> dict[str, object]:
    """Read fresh backend control files; this does not assert a backend ran."""
    backends: set[str] = set()
    controls: list[dict[str, str]] = []
    uncertainty: list[str] = []
    files: list[Path] = []
    total = 0
    for path in output.rglob("*"):
        if path.suffix not in {".bcf", ".aux"}:
            continue
        if len(files) >= 1000:
            uncertainty.append("Bibliography control evidence exceeds 1000 files")
            break
        files.append(path)
    biber_stems: set[Path] = set()
    for path in sorted(files, key=lambda item: (item.suffix != ".bcf", str(item))):
        relative = path.relative_to(output).as_posix()
        try:
            if path.is_symlink() or not path.is_file() or not _inside(path.resolve(), output):
                raise PreparationError("control file is not an owned regular file")
            total += path.stat().st_size
            if total > limit:
                raise PreparationError(
                    "combined control evidence exceeds the diagnostic size limit"
                )
            data = path.read_bytes()
            if len(data) > limit:
                raise PreparationError("control file exceeds the diagnostic size limit")
            text = data.decode("utf-8")
            if path.suffix == ".bcf":
                if re.search(r"<!\s*(?:DOCTYPE|ENTITY)", text, re.IGNORECASE):
                    raise PreparationError("BCF declarations are unsupported")
                document = ET.fromstring(text)
                if document.tag.rsplit("}", 1)[-1] != "controlfile":
                    raise PreparationError("BCF root is not a bibliography control file")
                biber_stems.add(path.with_suffix(""))
                backends.add("biber")
                controls.append({"path": relative, "backend": "biber", "evidence": "bcf"})
            elif path.with_suffix("") not in biber_stems:
                commands = _commands(mask_literals(text))
                resources = [command for command in commands if command.name == "bibdata"]
                if any(
                    not command.arguments or not command.arguments[0].value for command in resources
                ):
                    raise PreparationError("AUX bibliography data declaration is incomplete")
                if resources:
                    backends.add("bibtex")
                    controls.append({"path": relative, "backend": "bibtex", "evidence": "aux"})
        except (OSError, UnicodeError, ET.ParseError, _ParseLimit, PreparationError) as error:
            uncertainty.append(f"{relative}: {error}")
    return {
        "observed_backends": sorted(backends),
        "control_files": controls,
        "uncertainty": uncertainty,
        "coverage": "Fresh .bcf and .aux backend requirements, not proof of backend execution.",
    }


def _bibliography_policy(evidence: dict[str, object], requested: str, main: str) -> Finding:
    observed = evidence["observed_backends"]
    assert isinstance(observed, list)
    mismatch = requested != "auto" and any(backend != requested for backend in observed)
    uncertain = bool(evidence["uncertainty"])
    status = "failed" if mismatch else "inconclusive" if uncertain else "passed"
    if not observed and not uncertain:
        status = "not_applicable"
    return Finding(
        "build.bibliography_backend",
        (
            "Fresh bibliography controls require a backend outside the selected strategy."
            if mismatch
            else "Checked the bibliography backend against fresh build control files."
        ),
        "error" if mismatch or uncertain else "info",
        status,
        path=main,
        details={"requested_backend": requested, **evidence},
    )


async def build_project(
    source: Path,
    main: str,
    work: Path,
    engine: str,
    runner: ToolRunner,
    *,
    bibliography_backend: str = "auto",
    inventory_loaded_options: bool = False,
    ignored_log_checks: tuple[str, ...] = (),
) -> BuildResult:
    result = BuildResult(success=False)
    if engine not in {"pdflatex", "xelatex", "lualatex"}:
        raise PreparationError(f"Unsupported TeX engine: {engine}")
    if bibliography_backend not in {"auto", "bibtex", "biber"}:
        raise PreparationError("Bibliography backend must be auto, bibtex or biber")
    if type(inventory_loaded_options) is not bool:
        raise PreparationError("inventory_loaded_options must be a boolean")
    if not isinstance(ignored_log_checks, tuple) or any(
        not isinstance(code, str) or code not in SELECTABLE_BUILD_ERRORS
        for code in ignored_log_checks
    ):
        raise PreparationError("Only undefined-citation/reference log checks may be ignored")
    relative = PurePosixPath(main)
    if (
        relative.is_absolute()
        or relative.suffix != ".tex"
        or ".." in relative.parts
        or not relative.parts
        or any(not re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in relative.parts)
    ):
        raise PreparationError(
            "Build main must be a relative .tex path with letters, digits, underscores, "
            "dots or hyphens; other filename forms are not yet supported."
        )
    source = source.resolve(strict=True)
    if not (source / main).is_file():
        raise PreparationError(f"Build main file does not exist: {main}")
    work = work.absolute()
    if work.exists() or _inside(work.resolve(), source):
        raise PreparationError("Each build requires a new workspace outside its source tree")
    work.mkdir(parents=True)
    work = work.resolve()
    project, output = work / "project", work / "output"
    _copy_source(source, project, max_total_bytes=runner.limits.max_file_bytes)
    output.mkdir()
    options_probe = None
    options_error = None
    if inventory_loaded_options:
        try:
            options_probe = stage_loaded_options(project, main, work)
        except (OSError, PreparationError) as error:
            options_error = str(error)
    cwd = project / relative.parent
    output_argument = os.path.relpath(output, cwd)
    mode = {"pdflatex": "-pdf", "xelatex": "-xelatex", "lualatex": "-lualatex"}[engine]
    biber_error: str | None = None
    biber_executable = "biber"
    if bibliography_backend != "bibtex":
        # Prepared once per runner and reused by every later build of the job.
        # A failure is reported through the bibliography-tool check below rather
        # than raised, because an auto build may never need Biber at all.
        try:
            prepared = await runner.prepare_biber(work.parent)
        except PreparationError as error:
            biber_error = str(error)
        else:
            if prepared is not None:
                biber_executable = str(prepared)
    # Static trusted configuration overrides no imported rc, rules, or environment.
    controlled_rules = (
        "$max_repeat=5; $bibtex_use=1; $use_make_for_missing_files=0; @cus_dep_list=();"
    )
    if bibliography_backend != "auto":
        # Latexmk's documented internal-command adapter returns failure without
        # launching the unselected backend. All code is application-owned; no
        # filename or user-provided string is interpolated into Perl or a shell.
        controlled_rules += (
            " sub latexprep_backend_blocked { "
            "warn 'Bibliography backend is disabled by the selected strategy'; return 1; } "
            + {
                "bibtex": "$biber='internal latexprep_backend_blocked';",
                "biber": "$bibtex='internal latexprep_backend_blocked';",
            }[bibliography_backend]
        )
    if bibliography_backend != "bibtex":
        # Only an application-generated path reaches this Perl literal, and
        # prepare_biber rejects any path that is not inert in Perl and the shell.
        controlled_rules += f" $biber='{biber_executable} --noconf %O %B';"
    command = [
        "latexmk",
        "-norc",
        "-e",
        controlled_rules,
        "-use-make-",
        "-view=none",
        "-interaction=nonstopmode",
        "-halt-on-error",
        "-file-line-error",
        "-no-shell-escape",
        "-recorder",
        mode,
        f"-outdir={output_argument}",
        "./" + relative.name,
    ]
    result.command = command
    result.tools["execution"] = {
        "backend": {
            "darwin": "macOS sandbox-exec",
            "linux": "Linux Bubblewrap",
        }.get(sys.platform, "unavailable"),
        "limits": asdict(runner.limits),
        "network": "denied",
        "shell_escape": "disabled",
        "imported_configuration": "disabled",
        "source_date_epoch": getattr(runner, "source_date_epoch", None),
    }
    try:
        for name, version_flag in (("latexmk", "-v"), (engine, "--version")):
            version_command = (
                [name, "-norc", version_flag] if name == "latexmk" else [name, version_flag]
            )
            result.tools[name] = copy.deepcopy(
                await runner.tool_version(version_command, workspace=work)
            )
        execution = await runner.run(command, cwd=cwd, workspace=work)
    except PreparationError as error:
        result.findings.append(
            Finding("build.execution_unavailable", str(error), "error", "inconclusive")
        )
        if inventory_loaded_options:
            result.findings.append(
                loaded_options_unavailable("Build execution was unavailable", main)
            )
        return result
    result.command = execution.command
    declared_roots = (
        tuple(Path(root) for root in execution.resource_roots)
        if execution.resource_roots
        else tuple(runner.resource_roots)
    )
    result.tools["resource_roots"] = sorted(str(root) for root in declared_roots)
    stem = relative.stem
    result.log = _read_bounded(output / f"{stem}.log", runner.limits.max_output_bytes)
    result.findings.extend(_log_findings(result.log, main, project, cwd))
    log_path = output / f"{stem}.log"
    if not log_path.is_file() or log_path.stat().st_size > runner.limits.max_output_bytes:
        result.findings.append(
            Finding(
                "build.log_unavailable",
                "A complete bounded final-pass log was not produced.",
                "error",
                "inconclusive",
                path=main,
            )
        )
    for condition, rule, message in (
        (execution.timed_out, "build.timeout", "The build exceeded its time limit."),
        (
            execution.output_limited,
            "build.output_limit",
            "The build exceeded its diagnostic output limit.",
        ),
        (
            bool(execution.resource_exceeded),
            "build.resource_limit",
            execution.resource_exceeded or "",
        ),
        (
            bool(execution.returncode),
            "build.failed",
            f"Build tool exited with status {execution.returncode}.",
        ),
    ):
        if condition:
            details: dict[str, object] = {"stderr": execution.stderr[-4000:]}
            if rule == "build.failed":
                # TeX reports the actual cause only in the log: latexmk's own
                # stderr names the failed rule, not the offending input.
                details["log_tail"] = _log_tail(result.log)
            result.findings.append(Finding(rule, message, "error", path=main, details=details))
    recorder = output / f"{stem}.fls"
    recorder_inputs: set[Path] = set()
    if recorder.exists() and recorder.stat().st_size <= runner.limits.max_output_bytes:
        result.recorder_complete = True
        observed_main = False
        package_paths: set[Path] = set()
        for line in recorder.read_text(errors="replace").splitlines():
            if not line.startswith("INPUT "):
                continue
            observed = Path(line[6:].strip('"'))
            path = (observed if observed.is_absolute() else cwd / observed).resolve()
            recorder_inputs.add(path)
            observed_main = observed_main or path == (project / main).resolve()
            permitted = (
                _inside(path, project)
                or _inside(path, work)
                or any(_inside(path, root) for root in declared_roots)
            )
            if permitted and path.suffix.lower() in {".cls", ".sty"} and path not in package_paths:
                if len(package_paths) >= 2048:
                    result.recorder_complete = False
                else:
                    package_paths.add(path)
                    result.loaded_packages.append(_package_record(path, project))
            if _inside(path, project):
                result.dependencies.add(path.relative_to(project).as_posix())
            elif not _inside(path, work) and not any(
                _inside(path, root) for root in declared_roots
            ):
                result.findings.append(
                    Finding(
                        "build.external_dependency",
                        f"Recorder contains an undeclared external input: {observed}",
                        "error",
                        path=main,
                    )
                )
        if not observed_main:
            result.recorder_complete = False
            result.findings.append(
                Finding(
                    "build.dependency_trace",
                    "Recorder trace does not establish that the selected main file was read.",
                    "error",
                    "inconclusive",
                    path=main,
                )
            )
    else:
        result.findings.append(
            Finding(
                "build.dependency_trace",
                "A complete, bounded recorder trace was not produced.",
                "error",
                "inconclusive",
                path=main,
            )
        )
    submission = collect_submission_dependencies(
        source=source,
        project=project,
        cwd=cwd,
        output=output,
        main=main,
        engine=engine,
        recorder_inputs=recorder_inputs,
        recorder_complete=result.recorder_complete,
        resource_roots=declared_roots,
        owned_inputs=(options_probe.hook,) if options_probe is not None else (),
        max_bytes=runner.limits.max_output_bytes,
    )
    result.submission_inputs = submission.inputs
    result.generated_reads = submission.generated_reads
    result.tools["submission_dependencies"] = submission.details
    if submission.inputs is None:
        result.findings.append(
            Finding(
                "build.submission_dependencies",
                "Build dependency evidence is incomplete for submission packaging.",
                "warning",
                "inconclusive",
                path=main,
                details=submission.details,
            )
        )
    bibliography = _bibliography_evidence(output, runner.limits.max_output_bytes)
    result.tools["bibliography"] = {"requested_backend": bibliography_backend, **bibliography}
    result.findings.append(_bibliography_policy(bibliography, bibliography_backend, main))
    observed_backends = bibliography["observed_backends"]
    assert isinstance(observed_backends, list)
    for name in observed_backends:
        if bibliography_backend != "auto" and name != bibliography_backend:
            continue
        version_command = (
            ["biber", "--noconf", "--version"] if name == "biber" else ["bibtex", "--version"]
        )
        try:
            if name == "biber" and biber_error is not None:
                raise PreparationError(
                    f"Cannot run biber under restricted execution: {biber_error}"
                )
            result.tools[name] = copy.deepcopy(
                await runner.tool_version(version_command, workspace=work)
            )
        except PreparationError as error:
            result.findings.append(
                Finding(
                    "build.bibliography_tool",
                    str(error),
                    "error",
                    "inconclusive",
                    path=main,
                    details={"backend": name},
                )
            )
        else:
            result.findings.append(
                Finding(
                    "build.bibliography_tool",
                    f"Verified the locally available {name} version in isolation.",
                    "info",
                    "passed",
                    path=main,
                    details={"backend": name, "version": result.tools[name]},
                )
            )
    pdf = output / f"{stem}.pdf"
    valid_pdf = pdf.is_file() and 5 <= pdf.stat().st_size <= runner.limits.max_file_bytes
    if valid_pdf:
        with pdf.open("rb") as stream:
            valid_pdf = stream.read(5) == b"%PDF-"
    if not valid_pdf:
        result.findings.append(
            Finding(
                "build.missing_pdf",
                "No complete new PDF was produced.",
                "error",
                path=main,
                details={"log_tail": _log_tail(result.log)},
            )
        )
    result.success = bool(valid_pdf) and not any(
        item.severity == "error" and item.code not in ignored_log_checks for item in result.findings
    )
    if inventory_loaded_options:
        options_finding = (
            collect_loaded_options(
                options_probe,
                result.loaded_packages,
                recorder_inputs,
                main=main,
                engine=engine,
                recorder_complete=result.recorder_complete,
                build_complete=result.success,
                max_bytes=runner.limits.max_output_bytes,
            )
            if options_probe is not None
            else loaded_options_unavailable(
                options_error or "Instrumentation was unavailable", main
            )
        )
        result.tools["loaded_options"] = copy.deepcopy(options_finding.details)
        result.findings.append(options_finding)
        result.success = result.success and options_finding.status == "passed"
    if result.success:
        result.pdf = pdf
        result.findings.append(
            Finding(
                "build.completed", "Fresh isolated build completed.", "info", "passed", path=main
            )
        )
    return result


def _package_record(path: Path, project: Path) -> dict[str, str | None]:
    """Record bounded literal header metadata, never execute package source."""
    local = _inside(path, project)
    record: dict[str, str | None] = {
        "name": path.name,
        "path": path.relative_to(project).as_posix() if local else str(path),
        "origin": "project" if local else "toolchain",
        "declared_date": None,
        "declared_version": None,
    }
    try:
        with path.open("rb") as stream:
            header = stream.read(32768).decode("utf-8", errors="replace")
    except OSError:
        return record
    header = mask_literals(header)
    ordinary = list(
        re.finditer(r"\\Provides(?:Package|Class)\s*\{([^{}]+)\}\s*\[([^\[\]]*)\]", header)
    )
    expl = list(
        re.finditer(
            r"\\ProvidesExpl(?:Package|Class)\s*\{([^{}]+)\}\s*\{([^{}]+)\}\s*\{([^{}]+)\}", header
        )
    )
    matches = [*ordinary, *expl]
    if len(matches) != 1:
        return record
    declaration = matches[0]
    try:
        # Only an unconditional, top-level literal header can establish the date.
        # In particular, declarations after endinput or inside definitions cannot.
        commands = _commands(header[: declaration.end()])
    except _ParseLimit:
        return record
    if any(
        command.depth or command.name != "NeedsTeXFormat"
        for command in commands
        if command.start < declaration.start()
    ) or not any(
        command.start == declaration.start() and command.depth == 0 for command in commands
    ):
        return record
    declarations = [(match[1], match[2]) for match in ordinary]
    declarations.extend((match[1], f"{match[2]} {match[3]}") for match in expl)
    if len(declarations) == 1 and declarations[0][0].strip() == path.stem:
        match = re.match(r"\s*(\d{4}[-/]\d{2}[-/]\d{2})(?:\s+(\S+))?", declarations[0][1])
        if match:
            record["declared_date"] = match[1]
            record["declared_version"] = match[2]
    return record
