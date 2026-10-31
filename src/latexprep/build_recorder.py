"""What the TeX recorder shows one build read, judged for build checks.

The recorder (``.fls``) lists every file the engine opened. This module reads it from
the build's output folder, sorts the inputs into project files, toolchain files and
undeclared external files, collects the class and package files with their declared
releases, and reports a trace that is missing, incomplete or does not show the main
file as findings. Parsing the trace itself is ``build_dependencies``'s work.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .build_dependencies import RecorderTrace, read_recorder_trace
from .build_workspace import BuildLayout
from .errors import PreparationError
from .findings import Finding
from .package_headers import LoadedPackage, read_loaded_package

# Distinct class and package files recorded per build. A document that loads more is
# reported as an incomplete trace rather than read without bound.
MAX_LOADED_PACKAGES = 2048
_PACKAGE_SUFFIXES = frozenset({".cls", ".sty"})
_TRACE_RULE = "build.dependency_trace"
_EXTERNAL_RULE = "build.external_dependency"


@dataclass(frozen=True, slots=True)
class RecorderEvidence:
    """What the recorder shows this build read, interpreted for build checks.

    Attributes:
        trace: The parsed recorder trace, or ``None`` when none could be read.
        inputs: Every recorded input, resolved.
        project_inputs: The recorded inputs inside the project copy, relative to it.
        loaded_packages: The class and package files read from declared locations, with
            their declared releases.
        complete: Whether the trace shows that the main file was read and holds no more
            than ``MAX_LOADED_PACKAGES`` class and package files.
        findings: The findings about a missing or incomplete trace and about undeclared
            external inputs.
    """

    trace: RecorderTrace | None
    inputs: frozenset[Path]
    project_inputs: frozenset[str]
    loaded_packages: tuple[LoadedPackage, ...]
    complete: bool
    findings: tuple[Finding, ...]


def read_recorder(
    main: str, layout: BuildLayout, declared_roots: tuple[Path, ...], max_bytes: int
) -> RecorderEvidence:
    """Read and interpret the recorder trace of the build of ``main``.

    Args:
        main: The main file, relative to the project.
        layout: The workspace of the build, whose output folder holds the trace.
        declared_roots: The toolchain directories the build may legitimately read.
        max_bytes: The largest trace, in bytes, that is read.

    Returns:
        The evidence of the trace; when no complete, bounded trace could be read, an
        incomplete evidence with a finding that says so.
    """
    stem = PurePosixPath(main).stem
    try:
        trace = read_recorder_trace(
            layout.output / f"{stem}.fls", layout.output, layout.cwd, max_bytes
        )
    except (OSError, PreparationError):
        missing = Finding(
            _TRACE_RULE,
            "A complete, bounded recorder trace was not produced.",
            severity="error",
            status="inconclusive",
            path=main,
        )
        return RecorderEvidence(None, frozenset(), frozenset(), (), False, (missing,))
    return _interpret(trace, main, layout, declared_roots)


def _interpret(
    trace: RecorderTrace, main: str, layout: BuildLayout, declared_roots: tuple[Path, ...]
) -> RecorderEvidence:
    """Sort the recorded inputs and collect the loaded class and package files.

    An input is declared when it lies in the project copy, the workspace or one of
    ``declared_roots``; an undeclared input is reported as a finding. The evidence is
    complete only when the trace shows that the main file was read and holds no more
    than ``MAX_LOADED_PACKAGES`` class and package files.

    Args:
        trace: The parsed recorder trace.
        main: The main file, relative to the project.
        layout: The workspace of the build.
        declared_roots: The toolchain directories the build may legitimately read.

    Returns:
        The evidence of the trace.
    """
    inputs: set[Path] = set()
    project_inputs: set[str] = set()
    packages: dict[Path, LoadedPackage] = {}
    findings: list[Finding] = []
    too_many_packages = False
    for recorded in trace.inputs:
        path = recorded.absolute.resolve()
        inputs.add(path)
        in_project = path.is_relative_to(layout.project)
        declared = (
            in_project
            or path.is_relative_to(layout.work)
            or any(path.is_relative_to(root) for root in declared_roots)
        )
        if declared and path.suffix.lower() in _PACKAGE_SUFFIXES and path not in packages:
            if len(packages) >= MAX_LOADED_PACKAGES:
                too_many_packages = True
            else:
                packages[path] = read_loaded_package(path, layout.project)
        if in_project:
            project_inputs.add(path.relative_to(layout.project).as_posix())
        elif not declared:
            findings.append(
                Finding(
                    _EXTERNAL_RULE,
                    f"Recorder contains an undeclared external input: {Path(recorded.recorded)}",
                    severity="error",
                    path=main,
                )
            )
    read_main = (layout.project / main).resolve() in inputs
    if not read_main:
        findings.append(
            Finding(
                _TRACE_RULE,
                "Recorder trace does not establish that the selected main file was read.",
                severity="error",
                status="inconclusive",
                path=main,
            )
        )
    return RecorderEvidence(
        trace,
        frozenset(inputs),
        frozenset(project_inputs),
        tuple(packages.values()),
        read_main and not too_many_packages,
        tuple(findings),
    )
