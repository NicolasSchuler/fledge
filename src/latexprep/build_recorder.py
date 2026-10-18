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
    """What the recorder shows this build read, interpreted for build checks."""

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

    ``declared_roots`` are the toolchain directories the build may legitimately read.
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
