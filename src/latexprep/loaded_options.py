"""Bounded post-document LaTeX kernel option evidence from disposable builds.

The hook reads kernel registers, not literal source declarations.  These lists
do not describe package defaults, every consumed global option, or later setup
commands.  Recorder agreement is required before reporting a complete inventory.
"""

from __future__ import annotations

import codecs
import functools
import os
import re
import secrets
import stat
from dataclasses import asdict, dataclass, replace
from datetime import date
from importlib import resources
from pathlib import Path

from .build_dependencies import (
    PACKAGE_FILENAME,
    LoadedPackage,
    PackageOrigin,
    parse_declared_date,
)
from .errors import PreparationError
from .file_safety import FileTooLargeError, read_regular_file
from .findings import Finding
from .limits import MIB
from .setting_choices import Engine

_MAX_PACKAGES = 2048
_MAX_FIELD_UNITS = 16_384
_MAX_EVIDENCE_BYTES = 2 * MIB
_MAX_MAIN_BYTES = 8 * MIB
# The oldest kernel whose post-document option registers the hook reads.
_MINIMUM_FORMAT_DATE = date(2020, 10, 1)
_HOOK_RESOURCE = "loaded_options_hook.tex"
_UPPERCASE_HEX = re.compile(r"[0-9A-F]*")
_HOOK_ARGUMENT = re.compile(r"(?:\.\./)+latexprep-loaded-options-[0-9a-f]{32}\.tex")
_METADATA_REORDERED = "Kernel option metadata is incomplete or reordered"
_VALUES_REORDERED = "Kernel package option values are absent or reordered"
_SCOPE = (
    "Post-document detokenized kernel opt@ and raw option lists; "
    "global class options are separate. Options include explicit and forwarded "
    "kernel state, not every consumed global option, package defaults, token "
    "category codes, or later setup commands. File origins come from the recorder."
)


@functools.cache
def _hook_template() -> str:
    """The hook in loaded_options_hook.tex, application-owned TeX only.

    The TOKEN substitutions are generated lowercase hexadecimal, never source
    text or paths. Detokenization expands each register once, not the commands
    held in its option values; transport is native character units encoded as
    UTF-32BE hex so braces, pipes and newlines cannot forge rows. The kernel
    clears @filelist at begin-document unless listfiles was requested. File hooks
    retain our own bounded names without changing that user setting; the values
    themselves are still read only after document execution.
    """
    return resources.files("latexprep").joinpath(_HOOK_RESOURCE).read_text(encoding="ascii")


@dataclass(frozen=True, slots=True)
class LoadedOptionsProbe:
    token: str
    hook: Path
    sidecar: Path
    main: Path
    hook_bytes: bytes
    staged_bytes: bytes


def stage_loaded_options(project: Path, main: str, work: Path) -> LoadedOptionsProbe:
    """Add a line-neutral hook only to the existing disposable project copy."""
    main_path = project / main
    try:
        original = read_regular_file(main_path, _MAX_MAIN_BYTES)
    except FileTooLargeError:
        raise PreparationError(
            f"Loaded-option instrumentation supports main files up to {_MAX_MAIN_BYTES // MIB} MiB"
        ) from None
    token = secrets.token_hex(16)
    hook = work / f"latexprep-loaded-options-{token}.tex"
    hook_bytes = _hook_template().replace("TOKEN", token).encode("ascii")
    hook.write_bytes(hook_bytes)
    relative_hook = Path(os.path.relpath(hook, main_path.parent)).as_posix()
    # Workspaces and all path components from main are runtime-validated.  Only
    # the generated basename and ../ segments enter this TeX argument.
    if not _HOOK_ARGUMENT.fullmatch(relative_hook):
        raise PreparationError("Loaded-option hook is not outside the disposable project")
    prefix = (r"\input{" + relative_hook + r"}\relax ").encode("ascii")
    # Preserve a UTF-8 BOM and every original newline, including diagnostics on
    # the first source line.  No wrapper changes jobname or selected main file.
    offset = len(codecs.BOM_UTF8) if original.startswith(codecs.BOM_UTF8) else 0
    staged = original[:offset] + prefix + original[offset:]
    main_path.write_bytes(staged)
    return LoadedOptionsProbe(
        token, hook, work / "output" / f"{hook.stem}.lpo", main_path, hook_bytes, staged
    )


@dataclass(frozen=True, slots=True)
class PackageOptions:
    """One class or package's kernel option lists, with its recorder origin once joined."""

    name: str
    options: str
    raw_options: str
    path: str | None = None
    origin: PackageOrigin | None = None


@dataclass(frozen=True, slots=True)
class KernelOptions:
    """Post-document kernel option registers of one build."""

    engine: Engine
    format_date: str
    global_class_options: str
    unused_options: str
    packages: tuple[PackageOptions, ...]


@dataclass(frozen=True, slots=True)
class LoadedOptionsEvidence:
    """Kernel options joined with recorder origins, or why they are unavailable."""

    kernel: KernelOptions | None = None
    uncertainty: str = ""

    @property
    def complete(self) -> bool:
        return self.kernel is not None

    def details(self) -> dict[str, object]:
        kernel = self.kernel
        if kernel is None:
            return {"coverage_complete": False, "uncertainty": [self.uncertainty]}
        return {
            "format_date": kernel.format_date,
            "global_class_options": kernel.global_class_options,
            "unused_options": kernel.unused_options,
            "packages": [asdict(package) for package in kernel.packages],
            "coverage_complete": True,
            "engine": kernel.engine,
            "scope": _SCOPE,
        }

    def finding(self, main: str) -> Finding:
        if self.kernel is None:
            return Finding(
                "build.loaded_options",
                f"Actual loaded class/package options are inconclusive: {self.uncertainty}.",
                severity="error",
                status="inconclusive",
                path=main or None,
                details=self.details(),
            )
        return Finding(
            "build.loaded_options",
            f"Recorded post-document kernel options for {len(self.kernel.packages)} "
            "loaded classes/packages.",
            severity="info",
            status="passed",
            path=main,
            details=self.details(),
        )


def _native_string(value: str, engine: Engine) -> str:
    if len(value) > 8 * _MAX_FIELD_UNITS or len(value) % 8 or not _UPPERCASE_HEX.fullmatch(value):
        raise PreparationError("Option field is unavailable, oversized or malformed")
    units = [int(value[index : index + 8], 16) for index in range(0, len(value), 8)]
    unsupported = PreparationError("Option field contains unsupported native character units")
    if engine == "pdflatex":
        # pdfTeX reports bytes; together they must form UTF-8 text.
        try:
            return bytes(units).decode("utf-8")
        except (ValueError, UnicodeError) as error:
            raise unsupported from error
    # XeTeX and LuaTeX report code points; surrogates and values beyond Unicode
    # are not characters.
    if any(unit > 0x10FFFF or 0xD800 <= unit <= 0xDFFF for unit in units):
        raise unsupported
    return "".join(map(chr, units))


def _tagged_value(row: str, tag: str, engine: Engine, reordered: str) -> str:
    if not row.startswith(tag + "|"):
        raise PreparationError(reordered)
    return _native_string(row.removeprefix(tag + "|"), engine)


def _parse_options(text: str, token: str, engine: Engine) -> KernelOptions:
    rows = text.splitlines()
    if not rows or rows[0] != f"START|{token}" or rows[-1] != f"END|{token}":
        raise PreparationError("Complete current-build option start/end markers are absent")
    if len(rows) < 6 or rows[1] != f"ENGINE|{engine}":
        raise PreparationError("Option instrumentation used an unsupported or different engine")
    records = iter(rows[2:-1])
    format_date = _tagged_value(next(records), "FORMAT", engine, _METADATA_REORDERED)
    global_options = _tagged_value(next(records), "GLOBAL", engine, _METADATA_REORDERED)
    unused_options = _tagged_value(next(records), "UNUSED", engine, _METADATA_REORDERED)
    declared = parse_declared_date(format_date)
    if declared is None or declared < _MINIMUM_FORMAT_DATE:
        raise PreparationError("This LaTeX format does not establish supported kernel option state")
    packages: list[PackageOptions] = []
    names: set[str] = set()
    for row in records:
        if row.startswith("UNSUPPORTED|"):
            reason = row[:200].removeprefix("UNSUPPORTED|")
            raise PreparationError(f"Unsupported option instrumentation: {reason}")
        name = row.removeprefix("PACKAGE|")
        options, raw_options = next(records, None), next(records, None)
        if (
            not row.startswith("PACKAGE|")
            or not PACKAGE_FILENAME.fullmatch(name)
            or name in names
            or len(names) >= _MAX_PACKAGES
            or options is None
            or raw_options is None
        ):
            raise PreparationError("Kernel package option records are invalid or duplicated")
        names.add(name)
        packages.append(
            PackageOptions(
                name,
                _tagged_value(options, "OPTIONS", engine, _VALUES_REORDERED),
                _tagged_value(raw_options, "RAW", engine, _VALUES_REORDERED),
            )
        )
    if not any(name.endswith(".cls") for name in names):
        raise PreparationError("No loaded document class was established by the kernel hook")
    return KernelOptions(engine, format_date, global_options, unused_options, tuple(packages))


def loaded_options_unavailable(reason: str, main: str) -> Finding:
    return LoadedOptionsEvidence(uncertainty=reason).finding(main)


def _unchanged_regular_file(path: Path, expected: bytes) -> bool:
    info = path.lstat()
    return (
        stat.S_ISREG(info.st_mode)
        and info.st_size == len(expected)
        and path.read_bytes() == expected
    )


def collect_loaded_options(
    probe: LoadedOptionsProbe,
    packages: list[LoadedPackage],
    recorder_inputs: set[Path],
    *,
    engine: Engine,
    recorder_complete: bool,
    build_complete: bool,
    max_bytes: int,
) -> LoadedOptionsEvidence:
    """Require fresh complete evidence and a one-to-one recorder/kernel graph."""
    try:
        if not build_complete or not recorder_complete:
            raise PreparationError("A successful build and complete recorder are required")
        if probe.hook not in recorder_inputs:
            raise PreparationError("The final recorder did not observe the trusted options hook")
        if not _unchanged_regular_file(probe.hook, probe.hook_bytes) or not _unchanged_regular_file(
            probe.main, probe.staged_bytes
        ):
            raise PreparationError("The instrumented disposable source changed during execution")
        info = probe.sidecar.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > min(max_bytes, _MAX_EVIDENCE_BYTES):
            raise PreparationError("Option evidence is not a bounded regular output file")
        kernel = _parse_options(probe.sidecar.read_text(encoding="ascii"), probe.token, engine)
        recorded = {package.name: package for package in packages}
        if len(recorded) != len(packages):
            raise PreparationError("Multiple recorder paths make a package origin ambiguous")
        if {item.name for item in kernel.packages} != set(recorded):
            raise PreparationError("Kernel class/package graph does not match the final recorder")
        joined = tuple(
            replace(item, path=recorded[item.name].path, origin=recorded[item.name].origin)
            for item in kernel.packages
        )
        if any("\\" in item.options for item in joined) or "\\" in kernel.global_class_options:
            raise PreparationError(
                "Unexpanded control sequences remain in normalized kernel options"
            )
        return LoadedOptionsEvidence(replace(kernel, packages=joined))
    except OSError as error:
        # The cause names no path: the hook's files live in a private temporary directory.
        cause = (error.strerror or type(error).__name__).lower()
        return LoadedOptionsEvidence(
            uncertainty=f"The option hook or the file it writes could not be read ({cause})"
        )
    except UnicodeError:
        return LoadedOptionsEvidence(uncertainty="The option evidence is not plain ASCII text")
    except PreparationError as error:
        return LoadedOptionsEvidence(uncertainty=str(error))
