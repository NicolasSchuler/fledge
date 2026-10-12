"""Restricted execution backends: macOS Seatbelt and Linux Bubblewrap.

The backend is selected once per platform. A refused or unavailable sandbox is
an execution failure, never permission to run without isolation.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Protocol

from .biber_toolchain import BiberToolchain
from .models import PreparationError
from .resource_monitor import ProcessSample
from .toolchain import ToolContext, texlive_root

_MACOS_SYSTEM_RESOURCES = (
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
_MACOS_FONT_CONFIGURATIONS = (Path("/opt/homebrew/etc/fonts"), Path("/usr/local/etc/fonts"))
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
# Only trusted system directories may supply the sandbox program itself.
_SYSTEM_PROGRAM_PATH = "/usr/bin:/bin"
_REFUSAL_MARKERS = ("sandbox_apply:", "sandbox initialization failed", "sandbox_compile:")


class SandboxBackend(Protocol):
    """One platform's isolation boundary for tool commands."""

    @property
    def description(self) -> str:
        """How reports name this backend."""
        ...

    @property
    def requires_sealed_biber(self) -> bool:
        """Whether Biber may run only from a sealed runtime-owned copy."""
        ...

    @property
    def process_columns(self) -> str:
        """The ``ps -o`` columns that ``parse_processes`` reads."""
        ...

    def system_roots(self) -> frozenset[Path]:
        """Fixed system resources every tool may read."""
        ...

    def declares_tool(self, path: Path, resolved: Path) -> bool:
        """Whether an installed tool may become part of the declared toolchain."""
        ...

    def wrap(
        self,
        command: list[str],
        workspace: Path,
        *,
        context: ToolContext,
        readonly_inputs: tuple[Path, ...],
        biber: BiberToolchain | None,
    ) -> list[str]:
        """Return ``command`` confined to ``workspace`` and the declared grants."""
        ...

    def parse_processes(self, output: bytes, registered: set[int]) -> list[ProcessSample]:
        """Attribute each listed process to the registered command group it belongs to."""
        ...


def select_sandbox_backend() -> SandboxBackend:
    match sys.platform:
        case "darwin":
            return SeatbeltSandbox()
        case "linux":
            return BubblewrapSandbox()
        case _:
            return UnavailableSandbox()


def _process_groups(output: bytes) -> list[ProcessSample]:
    """Read ``pid pgid rss`` rows; each command already leads its own process group."""
    samples = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) != 3:
            continue
        try:
            samples.append(ProcessSample(*(int(value) for value in fields)))
        except ValueError as error:
            raise PreparationError("Process accounting returned invalid values") from error
    if not samples:
        raise PreparationError("Process memory accounting returned no processes")
    return samples


class SeatbeltSandbox:
    """macOS ``sandbox-exec`` with a generated deny-by-default profile."""

    description = "macOS sandbox-exec"
    requires_sealed_biber = True
    process_columns = "pid=,pgid=,rss="

    def system_roots(self) -> frozenset[Path]:
        roots = {Path(value).resolve() for value in _MACOS_SYSTEM_RESOURCES}
        roots.add(Path("/private/var/select/sh"))
        roots.update(path.resolve() for path in _MACOS_FONT_CONFIGURATIONS if path.exists())
        return frozenset(roots)

    def declares_tool(self, path: Path, resolved: Path) -> bool:
        return True

    def parse_processes(self, output: bytes, registered: set[int]) -> list[ProcessSample]:
        return _process_groups(output)

    def wrap(
        self,
        command: list[str],
        workspace: Path,
        *,
        context: ToolContext,
        readonly_inputs: tuple[Path, ...],
        biber: BiberToolchain | None,
    ) -> list[str]:
        backend = shutil.which("sandbox-exec", path=_SYSTEM_PROGRAM_PATH)
        if backend is None:
            raise PreparationError("Restricted execution requires macOS sandbox-exec")
        executables, roots = context.executables, context.resource_roots
        if any(path.resolve().is_relative_to(workspace) for path in executables):
            raise PreparationError("Executing a project-supplied program is not permitted")
        if any(workspace.is_relative_to(root) for root in roots if root.is_dir()):
            raise PreparationError("A job workspace cannot be inside a declared toolchain tree")
        isolated = () if biber is None else (biber.executable, biber.payload)
        profile = _seatbelt_profile(workspace, context, readonly_inputs, isolated)
        return [backend, "-p", profile, *command]


def _seatbelt_profile(
    workspace: Path,
    context: ToolContext,
    readonly_inputs: tuple[Path, ...],
    isolated: tuple[Path, ...],
) -> str:
    def quote(path: Path) -> str:
        return json.dumps(str(path), ensure_ascii=False)

    def rule(path: Path) -> str:
        return f"({'subpath' if path.is_dir() else 'literal'} {quote(path)})"

    executables, roots = context.executables, context.resource_roots
    reads = [f"(subpath {quote(workspace)})"]
    mappings = []
    for root in sorted(roots):
        entry = rule(root)
        reads.append(entry)
        mappings.append(entry)
    reads.extend(f"(literal {quote(path)})" for path in sorted(executables | set(readonly_inputs)))
    ancestors = {
        parent
        for root in roots | {workspace} | set(readonly_inputs) | set(isolated)
        for parent in root.parents
    }
    metadata = " ".join(f"(literal {quote(parent)})" for parent in sorted(ancestors))
    execution = " ".join(
        f"(literal {quote(path)})"
        for path in sorted(executables | {item.resolve() for item in executables})
    )
    # A read-only runtime-owned payload outside every tool workspace; execution
    # still never originates in project-writable storage.
    targets = " ".join(rule(path) for path in isolated)
    granted = (
        ()
        if not targets
        else (
            f"(allow process-exec {targets})",
            f"(allow file-read* {targets})",
            f"(allow file-map-executable {targets})",
        )
    )
    return "\n".join(
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


def _linux_tool_path(path: Path) -> bool:
    """Never infer a Linux toolchain grant from a private executable directory."""
    return any(path.is_relative_to(prefix) for prefix in _LINUX_TOOL_PREFIXES)


class BubblewrapSandbox:
    """Linux Bubblewrap with every namespace unshared and exact read-only mounts."""

    description = "Linux Bubblewrap"
    requires_sealed_biber = False
    process_columns = "pid=,ppid=,pgid=,rss="

    def system_roots(self) -> frozenset[Path]:
        # Preserve distribution aliases (such as /lib -> /usr/lib) as distinct
        # mount destinations while exposing only their contents.
        return frozenset(
            {Path(value).resolve() for value in _LINUX_SYSTEM_RESOURCES}
            | {Path(value) for value in _LINUX_SYSTEM_RESOURCES}
        )

    def declares_tool(self, path: Path, resolved: Path) -> bool:
        return _linux_tool_path(path) and _linux_tool_path(resolved.resolve())

    def parse_processes(self, output: bytes, registered: set[int]) -> list[ProcessSample]:
        return _linux_process_groups(output, registered)

    def wrap(
        self,
        command: list[str],
        workspace: Path,
        *,
        context: ToolContext,
        readonly_inputs: tuple[Path, ...],
        biber: BiberToolchain | None,
    ) -> list[str]:
        backend = shutil.which("bwrap", path=_SYSTEM_PROGRAM_PATH)
        if backend is None:
            raise PreparationError(
                "Restricted Linux execution requires Bubblewrap and permitted user namespaces; "
                "install or enable them explicitly. No unrestricted fallback is available."
            )
        if not _linux_tool_path(Path(backend).resolve()):
            raise PreparationError("The Linux sandbox backend must use a system executable")
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
            if workspace.is_relative_to(path.resolve()) or path.resolve().is_relative_to(workspace):
                raise PreparationError("A job workspace cannot overlap a declared toolchain tree")
        if any(path.is_relative_to(workspace) for path in readonly_inputs):
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
        mounts = (
            roots | {path for path in context.executables if path.exists()} | set(readonly_inputs)
        )
        for path in sorted(mounts, key=lambda value: (len(value.parts), str(value))):
            wrapped.extend(("--ro-bind", str(path), str(path)))
        wrapped.extend(("--bind", str(workspace), str(workspace)))
        # Keep anonymous container storage read-only as well: all generated files
        # must remain in the monitored workspace, rather than an uncounted tmpfs.
        for path in ("/proc", "/dev", "/"):
            wrapped.extend(("--remount-ro", path))
        return [*wrapped, "--", *command]


def _linux_process_groups(output: bytes, registered: set[int]) -> list[ProcessSample]:
    """Account for Bubblewrap descendants even after its --new-session split."""
    records: dict[int, tuple[int, int, int]] = {}
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
    samples = []
    for pid, (_, group, rss) in records.items():
        current = pid
        visited: set[int] = set()
        while current in records and current not in visited:
            if current in registered:
                group = current
                break
            visited.add(current)
            current = records[current][0]
        samples.append(ProcessSample(pid, group, rss))
    return samples


class UnavailableSandbox:
    """Platforms without a supported backend refuse every command."""

    description = "unavailable"
    requires_sealed_biber = False
    process_columns = SeatbeltSandbox.process_columns

    def system_roots(self) -> frozenset[Path]:
        return frozenset()

    def declares_tool(self, path: Path, resolved: Path) -> bool:
        return True

    def parse_processes(self, output: bytes, registered: set[int]) -> list[ProcessSample]:
        return _process_groups(output)

    def wrap(
        self,
        command: list[str],
        workspace: Path,
        *,
        context: ToolContext,
        readonly_inputs: tuple[Path, ...],
        biber: BiberToolchain | None,
    ) -> list[str]:
        raise PreparationError(
            "Restricted execution is unavailable on this platform; supported backends "
            "require macOS sandbox-exec or Linux Bubblewrap with user namespaces."
        )


def sandbox_refusal(stderr: str) -> bool:
    """Whether a backend reported that it could not apply its isolation."""
    return any(marker in stderr for marker in _REFUSAL_MARKERS)


def with_ulimits(command: list[str], *, max_file_bytes: int, timeout_seconds: int) -> list[str]:
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
        str(max_file_bytes // 1024),
        str(timeout_seconds),
        *command,
    ]


def tool_environment(
    workspace: Path, context: ToolContext, *, source_date_epoch: str
) -> dict[str, str]:
    """A fresh per-invocation environment; nothing is inherited from the caller."""
    container = workspace / ".tool-state"
    container.mkdir(exist_ok=True)
    state = Path(tempfile.mkdtemp(prefix="invocation-", dir=container))
    for name in ("tmp", "config", "cache", "texmf", "texmf-config", "texmf-var"):
        (state / name).mkdir()
    # HOME is deliberately neither inherited nor repurposed.
    environment = {
        "PATH": os.pathsep.join(context.search_paths),
        # A neutral account name: latexmk warns when USER is unset, and the
        # real name never needs to reach a tool.
        "USER": "fledge",
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
        "SOURCE_DATE_EPOCH": source_date_epoch,
        "FORCE_SOURCE_DATE": "1",
    }
    root = texlive_root(context.resource_roots)
    if root is not None:
        search = (
            state / "texmf-config",
            state / "texmf-var",
            state / "texmf",
            root / "texmf-config",
            root / "texmf-var",
            root / "texmf-dist",
        )
        environment.update(
            {
                "TEXMFCNF": str(root / "texmf-dist/web2c"),
                "TEXMFSYSCONFIG": str(root / "texmf-config"),
                "TEXMFSYSVAR": str(root / "texmf-var"),
                "TEXMF": "{" + ",".join(map(str, search)) + "}",
            }
        )
    return environment
