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
_TEX_LIVE = "TeX Live: brew install --cask basictex, or sudo apt install texlive latexmk"
_INSTALL_HINTS: dict[str, str] = {
    **dict.fromkeys(
        ("pdfinfo", "pdftotext", "pdftoppm", "pdffonts", "pdfimages", "pdfdetach", "pdftohtml"),
        _POPPLER,
    ),
    "qpdf": "qpdf: brew install qpdf, or sudo apt install qpdf",
    "mutool": "MuPDF: brew install mupdf-tools, or sudo apt install mupdf-tools",
    "tex-fmt": "tex-fmt: brew install tex-fmt, or a release binary from "
    "https://github.com/WGUNDERWOOD/tex-fmt/releases",
    **dict.fromkeys(
        (
            "latexmk",
            "pdflatex",
            "xelatex",
            "lualatex",
            "biber",
            "bibtex",
            "xdvipdfmx",
            "kpsewhich",
            "makeindex",
        ),
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
    # Resolve the TeX distribution directory links while preserving engine names
    # such as pdflatex (argv[0] selects the TeX format).
    return found.resolve() if name == "latexmk" else found.parent.resolve() / found.name


def tool_path(name: str) -> Path:
    found = shutil.which(name)
    if not found:
        hint = f" ({_INSTALL_HINTS[name]})" if name in _INSTALL_HINTS else ""
        raise ToolUnavailableError(
            f"Required tool is unavailable: {name}. Install it explicitly{hint}.", name
        )
    return _resolved_tool(Path(found).absolute(), name)


def distribution_roots(executable: Path) -> set[Path]:
    """Declare installed resources, without allowing an executable's home folder."""
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
    """The declared TeX Live distribution directory, if the roots include one."""
    return min((root for root in roots if _TEXLIVE_DIRECTORY.fullmatch(root.name)), default=None)


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
    """Installed directories of the runtime dependencies one receipt declares."""
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
    """The executables, readable resources and search path granted to one tool."""

    executable: Path
    executables: frozenset[Path]
    resource_roots: frozenset[Path]
    search_paths: tuple[str, ...]


class ToolDeclarations(Protocol):
    """What toolchain discovery needs from the sandbox backend."""

    def system_roots(self) -> frozenset[Path]: ...

    def declares_tool(self, path: Path, resolved: Path) -> bool: ...


class Toolchain:
    """Installed tools and their declared resources, discovered once per runner."""

    def __init__(
        self, executables: frozenset[Path], roots: frozenset[Path], resolved: dict[str, Path]
    ) -> None:
        self._executables = executables
        self._roots = roots
        self._resolved = resolved
        self._contexts: dict[Path, ToolContext] = {}

    @classmethod
    def discover(cls, backend: ToolDeclarations) -> Toolchain:
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
        if name not in self._resolved:
            self._resolved[name] = tool_path(name)
        return self._resolved[name]

    def substitute(self, name: str, executable: Path) -> None:
        """Run a runtime-prepared copy whenever ``name`` is requested."""
        self._resolved[name] = executable

    def forget(self, name: str) -> None:
        """Resolve ``name`` from the installed tools again."""
        self._resolved.pop(name, None)

    @property
    def resource_roots(self) -> frozenset[Path]:
        """Stable toolchain roots; command results carry their exact declared roots."""
        return self._roots

    def context(self, executable: Path) -> ToolContext:
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
