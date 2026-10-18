"""Restricted execution backends: macOS Seatbelt and Linux Bubblewrap.

The backend is selected once per platform. A refused or unavailable sandbox is
an execution failure, never permission to run without isolation. A backend turns a
command into the argv that runs it confined to one workspace and the declared read
grants; the resource-limit wrapper, the tool environment and the refusal check are
``tool_launch``'s, and process listing is ``process_table``'s.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Protocol

from .biber_toolchain import BiberToolchain
from .errors import SandboxRefusedError
from .process_table import AncestryProcessTable, GroupProcessTable, ProcessTable
from .toolchain import ToolContext

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


class SandboxBackend(ProcessTable, Protocol):
    """One platform's isolation boundary for tool commands.

    A backend also lists processes the way its isolation makes necessary
    (``ProcessTable``), so the resource monitor attributes memory correctly.
    """

    @property
    def description(self) -> str:
        """How reports name this backend."""
        ...

    @property
    def requires_sealed_biber(self) -> bool:
        """Whether Biber may run only from a sealed runtime-owned copy."""
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


def select_sandbox_backend() -> SandboxBackend:
    match sys.platform:
        case "darwin":
            return SeatbeltSandbox()
        case "linux":
            return BubblewrapSandbox()
        case _:
            return UnavailableSandbox()


class _EveryToolDeclared:
    """Declares every installed tool: this backend adds no rule about where a tool lives."""

    def declares_tool(self, path: Path, resolved: Path) -> bool:
        return True


class SeatbeltSandbox(_EveryToolDeclared, GroupProcessTable):
    """macOS ``sandbox-exec`` with a generated deny-by-default profile."""

    description = "macOS sandbox-exec"
    requires_sealed_biber = True

    def system_roots(self) -> frozenset[Path]:
        roots = {Path(value).resolve() for value in _MACOS_SYSTEM_RESOURCES}
        roots.add(Path("/private/var/select/sh"))
        roots.update(path.resolve() for path in _MACOS_FONT_CONFIGURATIONS if path.exists())
        return frozenset(roots)

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
            raise SandboxRefusedError("Restricted execution requires macOS sandbox-exec")
        executables, roots = context.executables, context.resource_roots
        if any(path.resolve().is_relative_to(workspace) for path in executables):
            raise SandboxRefusedError("Executing a project-supplied program is not permitted")
        if any(workspace.is_relative_to(root) for root in roots if root.is_dir()):
            raise SandboxRefusedError("A job workspace cannot be inside a declared toolchain tree")
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


class BubblewrapSandbox(AncestryProcessTable):
    """Linux Bubblewrap with every namespace unshared and exact read-only mounts."""

    description = "Linux Bubblewrap"
    requires_sealed_biber = False

    def system_roots(self) -> frozenset[Path]:
        # Preserve distribution aliases (such as /lib -> /usr/lib) as distinct
        # mount destinations while exposing only their contents.
        return frozenset(
            {Path(value).resolve() for value in _LINUX_SYSTEM_RESOURCES}
            | {Path(value) for value in _LINUX_SYSTEM_RESOURCES}
        )

    def declares_tool(self, path: Path, resolved: Path) -> bool:
        return _linux_tool_path(path) and _linux_tool_path(resolved.resolve())

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
            raise SandboxRefusedError(
                "Restricted Linux execution requires Bubblewrap and permitted user namespaces; "
                "install or enable them explicitly. No unrestricted fallback is available."
            )
        if not _linux_tool_path(Path(backend).resolve()):
            raise SandboxRefusedError("The Linux sandbox backend must use a system executable")
        if any(
            not _linux_tool_path(path) or not _linux_tool_path(path.resolve())
            for path in context.executables
        ):
            raise SandboxRefusedError(
                "Linux tool executables must be installed under the supported system prefixes; "
                "private-home and project executables cannot become toolchain grants."
            )
        roots = {path for path in context.resource_roots if path.exists()}
        system_paths = {Path(value) for value in _LINUX_SYSTEM_RESOURCES}
        for path in roots:
            if (not _linux_tool_path(path) and path not in system_paths) or (
                not _linux_tool_path(path.resolve()) and path.resolve() not in system_paths
            ):
                raise SandboxRefusedError(
                    "Linux toolchain resources must use declared system paths"
                )
            if workspace.is_relative_to(path.resolve()) or path.resolve().is_relative_to(workspace):
                raise SandboxRefusedError(
                    "A job workspace cannot overlap a declared toolchain tree"
                )
        if any(path.is_relative_to(workspace) for path in readonly_inputs):
            raise SandboxRefusedError("Read-only inputs must be outside the writable workspace")
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


class UnavailableSandbox(_EveryToolDeclared, GroupProcessTable):
    """Platforms without a supported backend refuse every command."""

    description = "unavailable"
    requires_sealed_biber = False

    def system_roots(self) -> frozenset[Path]:
        return frozenset()

    def wrap(
        self,
        command: list[str],
        workspace: Path,
        *,
        context: ToolContext,
        readonly_inputs: tuple[Path, ...],
        biber: BiberToolchain | None,
    ) -> list[str]:
        raise SandboxRefusedError(
            "Restricted execution is unavailable on this platform; supported backends "
            "require macOS sandbox-exec or Linux Bubblewrap with user namespaces."
        )
