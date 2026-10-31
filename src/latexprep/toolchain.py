"""Locate installed tools and the system and distribution resources they may read.

Grants derive from installed locations only: the sandbox backend's fixed system
resources, TeX Live distribution directories and Homebrew packages named by
installation receipts. A private or project directory never becomes a grant.
"""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .errors import ToolUnavailableError
from .limits import MIB

# Tools declared up front, so every command of a job shares one stable grant set.
_SANDBOXED_TOOL_NAMES = (
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
# Interpreters and shells that tools such as latexmk start.
_INTERPRETERS = (Path("/usr/bin/perl"), Path("/usr/bin/env"), Path("/bin/sh"), Path("/bin/bash"))
# Where a researcher gets each external tool: its package, with the commands that install it.
_POPPLER = "Poppler: brew install poppler, or sudo apt install poppler-utils"
_TEX_LIVE = "TeX Live: brew install --cask basictex, or sudo apt install texlive"
# BasicTeX ships neither latexmk nor Biber; tlmgr adds them, and Debian packages them apart.
_LATEXMK = (
    "TeX Live: brew install --cask basictex, then sudo tlmgr install latexmk; "
    "or sudo apt install latexmk"
)
_BIBER = (
    "TeX Live: brew install --cask basictex, then sudo tlmgr install biber; "
    "or sudo apt install biber"
)
_INSTALL_HINTS: dict[str, str] = {
    **dict.fromkeys(
        ("pdfinfo", "pdftotext", "pdftoppm", "pdffonts", "pdfimages", "pdfdetach", "pdftohtml"),
        _POPPLER,
    ),
    "qpdf": "qpdf: brew install qpdf, or sudo apt install qpdf",
    "mutool": "MuPDF: brew install mupdf-tools, or sudo apt install mupdf-tools",
    "tex-fmt": "tex-fmt: brew install tex-fmt, or a release binary from "
    "https://github.com/WGUNDERWOOD/tex-fmt/releases",
    "latexmk": _LATEXMK,
    "biber": _BIBER,
    **dict.fromkeys(
        ("pdflatex", "xelatex", "lualatex", "bibtex", "xdvipdfmx", "kpsewhich", "makeindex"),
        _TEX_LIVE,
    ),
}
# MacTeX uses a year directory; BasicTeX adds the literal "basic" suffix.
_TEXLIVE_DIRECTORY = re.compile(r"[0-9]{4}(?:basic)?")
_HOMEBREW_FORMULA = re.compile(r"[A-Za-z0-9][A-Za-z0-9@+_.-]*")
_HOMEBREW_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]*")
_MAX_HOMEBREW_PACKAGES = 256
_MAX_RECEIPT_BYTES = MIB
_MAX_TOTAL_RECEIPT_BYTES = 8 * MIB


def _resolved_tool(found: Path, name: str) -> Path:
    """Return the path through which the tool ``name``, found at ``found``, is run.

    ``latexmk`` is resolved completely; any other tool keeps its own file name inside its
    resolved directory.

    Args:
        found: The absolute path at which the tool was found on ``PATH``.
        name: The name of the tool.

    Returns:
        The fully resolved path for ``latexmk``; otherwise the resolved directory of
        ``found`` joined with its file name.
    """
    # Resolve the TeX distribution directory links while preserving engine names
    # such as pdflatex (argv[0] selects the TeX format).
    return found.resolve() if name == "latexmk" else found.parent.resolve() / found.name


def install_hint(name: str) -> str | None:
    """Return the package that provides the tool ``name`` and how to install it, when known."""
    return _INSTALL_HINTS.get(name)


def tool_path(name: str) -> Path:
    """Find the tool ``name`` on ``PATH`` and return the path it is run by.

    Args:
        name: The name of the tool.

    Returns:
        The absolute path, resolved as for the declared tools.

    Raises:
        ToolUnavailableError: The tool is not on ``PATH``; the message carries the
            installation hint when one is known.
    """
    found = shutil.which(name)
    if not found:
        hint = install_hint(name)
        how = f" ({hint})" if hint else ""
        raise ToolUnavailableError(
            f"Required tool is unavailable: {name}. Install it explicitly{how}.", name
        )
    return _resolved_tool(Path(found).absolute(), name)


def distribution_roots(executable: Path) -> set[Path]:
    """Declare installed resources, without allowing an executable's home folder.

    Args:
        executable: The executable whose resources to declare.

    Returns:
        The resolved executable, the TeX Live distribution directory that holds it, if
        any, and for a Homebrew Cellar executable its package and the packages its
        installation receipts declare.
    """
    resolved = executable.resolve()
    roots: set[Path] = {resolved}
    parts = resolved.parts
    if "texlive" in parts:
        index = parts.index("texlive")
        if len(parts) > index + 1 and _TEXLIVE_DIRECTORY.fullmatch(parts[index + 1]):
            roots.add(Path(*parts[: index + 2]))
    if "Cellar" in parts:
        index = parts.index("Cellar")
        if len(parts) > index + 2:
            package = Path(*parts[: index + 3])
            cellar = Path(*parts[: index + 1])
            roots.update(_homebrew_runtime_roots(package, cellar))
    return roots


def texlive_root(roots: frozenset[Path]) -> Path | None:
    """Return the declared TeX Live distribution directory, if the roots include one.

    Args:
        roots: The declared resource roots.

    Returns:
        The smallest root named like a TeX Live year directory, or ``None``.
    """
    return min((root for root in roots if _TEXLIVE_DIRECTORY.fullmatch(root.name)), default=None)


def _homebrew_runtime_roots(package: Path, cellar: Path) -> set[Path]:
    """Grant receipt-declared packages, including same-formula upgraded opt links.

    Homebrew updates opt links independently of dependent installation receipts.
    Both the recorded version and current alias may be needed by installed Mach-O
    load commands. Never grant the Cellar, opt directory, or an external symlink.
    At most ``_MAX_HOMEBREW_PACKAGES`` packages are visited; a receipt that is missing,
    invalid or over its size bounds adds no dependencies.

    Args:
        package: The Cellar package directory, ``Cellar/<formula>/<version>``, to start
            from.
        cellar: The Cellar directory; only package directories inside it are granted.

    Returns:
        The granted package directories and the opt links that resolve to them.
    """
    roots: set[Path] = set()
    pending = [package]
    seen: set[Path] = set()
    receipt_bytes = 0
    while pending and len(seen) < _MAX_HOMEBREW_PACKAGES:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        if not current.is_dir() or not current.is_relative_to(cellar):
            continue
        roots.add(current)
        alias = cellar.parent / "opt" / current.parent.name
        if alias.resolve() == current:
            roots.add(alias)
        receipt = current / "INSTALL_RECEIPT.json"
        try:
            size = receipt.stat().st_size
            receipt_bytes += size
            if size > _MAX_RECEIPT_BYTES or receipt_bytes > _MAX_TOTAL_RECEIPT_BYTES:
                continue
            pending.extend(_receipt_dependencies(receipt, cellar))
        except (OSError, ValueError, TypeError, RecursionError):
            # Missing/invalid provenance tightens the grant; never scan neighbors.
            continue
    return roots


def _receipt_dependencies(receipt: Path, cellar: Path) -> Iterator[Path]:
    """Yield the installed directories of the runtime dependencies one receipt declares.

    For each dependency the recorded version and the current opt link are candidates; a
    candidate counts when it resolves to a directory directly inside the formula's Cellar
    directory. A receipt that is not an object or declares more than
    ``_MAX_HOMEBREW_PACKAGES`` dependencies yields nothing.

    Args:
        receipt: The ``INSTALL_RECEIPT.json`` file of a package.
        cellar: The Cellar directory.

    Yields:
        The resolved package directories of the dependencies.

    Raises:
        OSError: The receipt cannot be read.
        ValueError: The receipt is not valid JSON text.
    """
    declared = json.loads(receipt.read_text())
    if not isinstance(declared, dict):
        return
    dependencies = declared.get("runtime_dependencies", [])
    if not isinstance(dependencies, list) or len(dependencies) > _MAX_HOMEBREW_PACKAGES:
        return
    for dependency in dependencies:
        if not isinstance(dependency, dict):
            continue
        name = str(dependency.get("full_name", "")).rsplit("/", 1)[-1]
        version = dependency.get("pkg_version", dependency.get("version"))
        if not _HOMEBREW_FORMULA.fullmatch(name):
            continue
        family = cellar / name
        candidates = []
        if isinstance(version, str) and _HOMEBREW_VERSION.fullmatch(version):
            candidates.append((family / version).resolve())
        candidates.append((cellar.parent / "opt" / name).resolve())
        for candidate in candidates:
            if candidate.parent == family and candidate.is_dir():
                yield candidate


@dataclass(frozen=True, slots=True)
class ToolContext:
    """The executables, readable resources and search path granted to one tool.

    Attributes:
        executable: The executable the tool runs.
        executables: Every executable the tool may run.
        resource_roots: The directories and files the tool may read.
        search_paths: The directories of the tool's ``PATH``, in order.
    """

    executable: Path
    executables: frozenset[Path]
    resource_roots: frozenset[Path]
    search_paths: tuple[str, ...]


class ToolDeclarations(Protocol):
    """What toolchain discovery needs from the sandbox backend."""

    def system_roots(self) -> frozenset[Path]:
        """Return the fixed system resources every tool may read.

        Returns:
            The system resource roots.
        """
        ...

    def declares_tool(self, path: Path, resolved: Path) -> bool:
        """Return whether an installed tool may become part of the declared toolchain.

        Args:
            path: The absolute path at which the tool was found on ``PATH``.
            resolved: The path the tool is run by.

        Returns:
            ``True`` when the tool may be declared.
        """
        ...


class Toolchain:
    """Installed tools and their declared resources, discovered once per runner.

    Args:
        executables: The declared executables.
        roots: The declared resource roots every tool may read.
        resolved: The path each tool is run by, keyed by name; later lookups and
            substitutions are recorded in it.
    """

    def __init__(
        self, executables: frozenset[Path], roots: frozenset[Path], resolved: dict[str, Path]
    ) -> None:
        self._executables = executables
        self._roots = roots
        self._resolved = resolved
        self._contexts: dict[Path, ToolContext] = {}

    @classmethod
    def discover(cls, backend: ToolDeclarations) -> Toolchain:
        """Discover the installed tools that ``backend`` declares, and their resources.

        The tools in ``_SANDBOXED_TOOL_NAMES`` are declared up front, so every command of a
        job shares one stable grant set; the interpreters and shells that tools start are
        declared as well. The roots are the backend's system roots and the distribution roots
        of every declared executable.

        Args:
            backend: The sandbox backend that decides which tools may be declared.

        Returns:
            The toolchain with the declared tools resolved.
        """
        declared = {*_INTERPRETERS, *Path("/usr/bin").glob("perl5.*")}
        resolved: dict[str, Path] = {}
        for name in _SANDBOXED_TOOL_NAMES:
            found = shutil.which(name)
            if not found:
                continue
            path = Path(found).absolute()
            target = _resolved_tool(path, name)
            if not backend.declares_tool(path, target):
                continue
            resolved[name] = target
            declared.update((path, target))
        roots = set(backend.system_roots())
        for item in declared:
            roots.update(distribution_roots(item))
        return cls(frozenset(declared), frozenset(roots), resolved)

    def resolve(self, name: str) -> Path:
        """Return the path the tool ``name`` is run by, looking it up on first use.

        Args:
            name: The name of the tool.

        Returns:
            A substituted copy, the declared tool, or the tool found on ``PATH``.

        Raises:
            ToolUnavailableError: The tool is not known and not on ``PATH``.
        """
        if name not in self._resolved:
            self._resolved[name] = tool_path(name)
        return self._resolved[name]

    def substitute(self, name: str, executable: Path) -> None:
        """Run a runtime-prepared copy whenever ``name`` is requested.

        Args:
            name: The name of the tool.
            executable: The prepared copy to run instead.
        """
        self._resolved[name] = executable

    def forget(self, name: str) -> None:
        """Resolve ``name`` from the installed tools again."""
        self._resolved.pop(name, None)

    @property
    def resource_roots(self) -> frozenset[Path]:
        """Stable toolchain roots; command results carry their exact declared roots."""
        return self._roots

    def context(self, executable: Path) -> ToolContext:
        """Return the executables, readable resources and search path granted to ``executable``.

        The context is built once per executable. Its search path is the executable's own
        directory, ``/usr/bin``, ``/bin`` and the directories of the declared executables,
        without repeats; its resource roots add the executable's distribution roots to the
        toolchain's.

        Args:
            executable: The executable the tool runs.

        Returns:
            The grants of the tool, the same object for every call with ``executable``.
        """
        if executable not in self._contexts:
            executables = self._executables | {executable}
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
            self._contexts[executable] = ToolContext(
                executable,
                executables,
                self._roots | distribution_roots(executable),
                search_paths,
            )
        return self._contexts[executable]
