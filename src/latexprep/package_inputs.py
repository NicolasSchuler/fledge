"""Select project inputs for one observed build, without modifying the source tree."""

from __future__ import annotations

import shutil
from pathlib import Path, PurePosixPath

from .models import Change, Finding
from .scheduler import cancellation_point
from .source import analyze_selected_sources


def select_package_inputs(
    root: Path,
    destination: Path,
    main: str,
    observed: set[str] | None,
    explicit: set[str],
) -> tuple[list[Change], list[Finding]]:
    """Copy supported literal and observed inputs plus explicit retained files.

    A missing trace is different from an empty dependency set. The caller must
    reject error findings; no partially selected destination is made on failure.
    The subsequent exact archive rebuild remains required.
    """
    findings: list[Finding] = []
    if observed is None or main not in observed:
        return [], [
            Finding(
                "package.dependencies",
                "Complete build dependency evidence is unavailable; no input set was selected.",
                "error",
                "inconclusive",
                suggestion="Resolve the build's submission dependency diagnostic and retry.",
            )
        ]
    analysis = analyze_selected_sources(root, main)
    if not analysis.complete:
        return [], [
            *analysis.findings,
            Finding(
                "package.dependencies",
                "The selected source dependency graph is incomplete or ambiguous.",
                "error",
                "inconclusive",
                suggestion="Resolve unsupported or ambiguous file references before preparing.",
            ),
        ]
    included = analysis.dependencies | observed | explicit
    for name in sorted(included):
        cancellation_point()
        relative = PurePosixPath(name)
        path = root / name
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or relative.as_posix() != name
            or "\\" in name
            or path.is_symlink()
            or not path.is_file()
            or not path.resolve().is_relative_to(root.resolve())
        ):
            findings.append(
                Finding(
                    "package.dependencies",
                    "A required input or explicit retained file is missing or unsafe.",
                    "error",
                    "inconclusive",
                    path=name,
                    suggestion="Supply the project-relative file and repeat preparation.",
                )
            )
    if findings:
        return [], findings
    files = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
    changes = [
        Change(name, "exclude", "Not needed by the selected document or explicitly retained.")
        for name in sorted(files - included)
    ]
    destination.mkdir()
    for name in sorted(included):
        cancellation_point()
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / name, target)
    findings.append(
        Finding(
            "package.dependencies",
            f"Selected {len(included)} project inputs; omitted {len(changes)} unrelated files.",
            "info",
            "passed",
            details={
                "included": sorted(included),
                "explicit": sorted(explicit),
                "scope": "Supported literal dependencies and recorded inputs for the selected "
                "build configuration; not minimality across other TeX executions.",
            },
        )
    )
    return changes, findings
