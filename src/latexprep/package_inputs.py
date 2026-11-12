"""Select project inputs for one observed build, without modifying the source tree.

The files a package needs are the union of three kinds of evidence: the literal
dependencies of the source graph, the inputs the build recorded, and the files the user
asks to keep. ``select_package_inputs`` takes that union, checks that every file is a
plain, regular, contained file, and copies it into a new destination. It owns the
choice and the copy; the reasons behind each file are recorded as inventory rows
(``package_inventory``), and recorded inputs come from the build, not from here.

Besides the project's files, a package carries the generated files that a receiving
service may not rebuild (``generated_files``): the index, glossary and nomenclature
files the verified build generated always, because arXiv runs neither makeindex nor
makeglossaries, and the generated ``.bbl`` only when the settings say that the service
runs neither BibTeX nor Biber. They are copied from the build's output folder to sit
beside the main file, where TeX looks for them.
"""

from __future__ import annotations

import shutil
from collections.abc import Mapping, Set
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import NamedTuple

from .build_result import BuildResult
from .cancellation import cancellation_point
from .findings import Finding
from .generated_files import BIBLIOGRAPHY_SUFFIX, GENERATED_SUFFIXES, INDEX_SUFFIXES, generated_kind
from .package_inventory import InventoryRow, selection_inventory, unused_reason
from .path_safety import is_contained, is_relative_posix_path
from .report import Change, ChangeKind
from .source_analysis import SourceAnalysis, analyze_selected_sources

_RULE = "package.dependencies"


def shipped_generated_suffixes(*, venue_runs_bibliography_tools: bool) -> frozenset[str]:
    """Return the suffixes of the generated files a package carries from its verified build.

    Args:
        venue_runs_bibliography_tools: Whether the receiving service runs BibTeX or Biber
            when no .bbl is uploaded; only when it does not is the .bbl shipped.

    Returns:
        The index, glossary and nomenclature suffixes, and ``.bbl`` when it is shipped.
    """
    return frozenset(INDEX_SUFFIXES) | (
        frozenset() if venue_runs_bibliography_tools else frozenset({BIBLIOGRAPHY_SUFFIX})
    )


@dataclass(frozen=True, slots=True)
class InputEvidence:
    """What is known about the files one build needs.

    Attributes:
        observed: The inputs the build recorded; ``None`` means no complete trace exists,
            which is different from an empty set.
        explicit: The files the settings ask to keep.
        generated_files: Where each generated file the build read lies, by its name
            relative to the folder that holds it.
        shipped_suffixes: The suffixes of the generated files the package carries from
            the build (see :func:`shipped_generated_suffixes`).
        uncertainty: Why the trace is incomplete, when ``observed`` is ``None``.
    """

    observed: Set[str] | None
    explicit: Set[str]
    generated_files: Mapping[str, Path] = field(default_factory=dict)
    shipped_suffixes: frozenset[str] = frozenset(INDEX_SUFFIXES)
    uncertainty: tuple[str, ...] = ()


class InputSelectionResult(NamedTuple):
    """The outcome of selecting inputs: what was left out, what was found, and why.

    Attributes:
        changes: An exclusion for every project file that was left out.
        findings: The findings of the selection.
        inventory: A row for every project file, with the reasons it was selected or left
            out. It is empty when nothing was selected.
    """

    changes: list[Change]
    findings: list[Finding]
    inventory: list[InventoryRow]


def build_evidence(
    result: BuildResult, explicit: Set[str], shipped_suffixes: frozenset[str]
) -> InputEvidence:
    """Gather what one finished build shows about the files a package needs.

    Args:
        result: The build.
        explicit: The files the settings ask to keep.
        shipped_suffixes: The suffixes of the generated files the package carries.

    Returns:
        The evidence, with the build's generated files and, without a complete trace,
        why it is incomplete.
    """
    dependencies = result.tools.submission_dependencies
    return InputEvidence(
        result.submission_inputs,
        explicit,
        generated_files=dict(dependencies.generated_files) if dependencies else {},
        shipped_suffixes=shipped_suffixes,
        uncertainty=dependencies.uncertainty if dependencies else (),
    )


def shipped_generated_files(main: str, evidence: InputEvidence) -> dict[str, Path]:
    """Return the generated files the package carries from the build, by package name.

    A generated file is shipped when the build read it from directly inside the folder
    that held it, its suffix is shipped, and it is a regular, unlinked file. It goes
    beside the main file, where TeX looks for it, unless the settings keep a supplied
    file of that name, which is then shipped instead.

    Args:
        main: The main file, relative to the project.
        evidence: What is known about the build.

    Returns:
        The build's file to copy, by its project-relative name in the package.
    """
    folder = PurePosixPath(main).parent
    shipped: dict[str, Path] = {}
    for name, path in sorted(evidence.generated_files.items()):
        if "/" in name or PurePosixPath(name).suffix not in evidence.shipped_suffixes:
            continue
        target = (folder / name).as_posix()
        if target in evidence.explicit or path.is_symlink() or not path.is_file():
            continue
        shipped[target] = path
    return shipped


def _regenerated_supplied_files(main: str, evidence: InputEvidence) -> set[str]:
    """Return the names beside the main file of generated files the build rewrote.

    A supplied file of such a name was not what the build read; when it is not shipped
    from the build, its inventory row says why.

    Args:
        main: The main file, relative to the project.
        evidence: What is known about the build.

    Returns:
        The project-relative names.
    """
    folder = PurePosixPath(main).parent
    return {
        (folder / name).as_posix()
        for name in evidence.generated_files
        if "/" not in name and PurePosixPath(name).suffix in GENERATED_SUFFIXES
    }


def _missing_trace(uncertainty: tuple[str, ...]) -> InputSelectionResult:
    """Return the result of a build without a complete trace: one error, nothing selected.

    Args:
        uncertainty: Why the trace is incomplete; the first reason is quoted.

    Returns:
        The result, with no changes and no inventory.
    """
    reason = f" ({uncertainty[0].rstrip('.')})" if uncertainty else ""
    return InputSelectionResult(
        [],
        [
            Finding(
                _RULE,
                f"Complete build dependency evidence is unavailable{reason}; no input set was "
                "selected.",
                severity="error",
                status="inconclusive",
                suggestion="Resolve the reason above, or the build's submission dependency "
                "diagnostic, and retry.",
                details={"uncertainty": list(uncertainty)},
            )
        ],
        [],
    )


def _graph_advisories(analysis: SourceAnalysis) -> list[Finding]:
    """Return findings that say the literal graph is incomplete, as advice rather than errors.

    Recorded inputs are direct evidence of what this build read, so an incomplete literal
    graph only widens the selection; the exact archive rebuild still verifies the result.
    """
    if analysis.complete:
        return []
    findings = [
        replace(item, severity="warning")
        for item in analysis.findings
        if item.status == "inconclusive"
    ]
    findings.append(
        Finding(
            _RULE,
            "The literal source dependency graph is incomplete or ambiguous; "
            "the recorded build inputs were used to select this document's files.",
            severity="warning",
            status="inconclusive",
            suggestion="Review the source diagnostics above; "
            "the rebuilt archive verifies the selected input set.",
        )
    )
    return findings


def _input_problem(root: Path, name: str) -> str | None:
    """Say why ``name`` cannot be copied as an input, or return ``None`` when it can.

    Args:
        root: The project folder.
        name: The input, relative to ``root``.

    Returns:
        The reason, such as "does not exist in the project", or ``None``.
    """
    if not is_relative_posix_path(name):
        return "has a name that is not a plain project-relative path"
    path = root / name
    if path.is_symlink():
        return "is a symbolic link, and links are not shipped"
    if not path.exists():
        return "does not exist in the project"
    if not is_contained(root, path, regular_file=True):
        return "is not a regular file inside the project"
    return None


def _unusable_inputs(root: Path, included: Set[str]) -> list[Finding]:
    """Return an error for every input that is missing, not a plain name, linked or not a file.

    Args:
        root: The project folder.
        included: The selected inputs, relative to ``root``.

    Returns:
        The errors, in the order of the input names.
    """
    findings = []
    for name in sorted(included):
        cancellation_point()
        problem = _input_problem(root, name)
        if problem is not None:
            findings.append(
                Finding(
                    _RULE,
                    f"A required input or explicit retained file {problem}.",
                    severity="error",
                    status="inconclusive",
                    path=name,
                    suggestion="Supply the project-relative file and repeat preparation.",
                )
            )
    return findings


def _copy_inputs(
    root: Path, destination: Path, included: Set[str], built: Mapping[str, Path]
) -> None:
    """Copy the selected inputs and the shipped generated files into ``destination``.

    Args:
        root: The project folder.
        destination: The folder to create and copy into; it must not exist yet.
        included: The selected inputs, relative to ``root``.
        built: The build's generated files to copy, by their name in ``destination``.
    """
    destination.mkdir()
    sources = {name: root / name for name in included} | dict(built)
    for name in sorted(sources):
        cancellation_point()
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(sources[name], target)


def _shipped_generated_text(built: Set[str]) -> str:
    """Say which generated files the package carries from the build, and for whom."""
    if not built:
        return ""
    tools = sorted(
        {kind.tool for name in built if (kind := generated_kind(name)) is not None}, key=len
    )
    return (
        f" Shipped {', '.join(sorted(built))}, generated by the verified build, for services "
        f"that do not run {' or '.join(tools)}."
    )


def _selection_finding(
    included: Set[str], omitted: int, evidence: InputEvidence, built: Set[str]
) -> Finding:
    """Return the finding that sums up a successful selection.

    Args:
        included: The selected project inputs and shipped generated files.
        omitted: The number of project files that were left out.
        evidence: The evidence of the selection; its explicit files go into the details.
        built: The generated files the package carries from the build.

    Returns:
        A passed info finding.
    """
    return Finding(
        _RULE,
        f"Selected {len(included)} project inputs; omitted {omitted} unrelated files."
        + _shipped_generated_text(built),
        severity="info",
        status="passed",
        details={
            "included": sorted(included),
            "explicit": sorted(evidence.explicit),
            "generated_files": sorted(built),
            "generated_bibliography": sorted(
                name for name in built if name.endswith(BIBLIOGRAPHY_SUFFIX)
            ),
            "scope": "Supported literal dependencies and recorded inputs for the selected "
            "build configuration; not minimality across other TeX executions.",
        },
    )


def select_package_inputs(
    root: Path, destination: Path, main: str, evidence: InputEvidence
) -> InputSelectionResult:
    """Copy supported literal and observed inputs, explicit retained files and generated files.

    The generated files the package carries from the build are chosen by
    :func:`shipped_generated_files`.

    A missing trace is different from an empty dependency set. An incomplete
    literal graph is advisory once the trace is complete, because the recorded
    inputs are direct evidence. The caller must reject error findings; no
    partially selected destination is made on failure. The subsequent exact
    archive rebuild remains required.

    Args:
        root: The project folder.
        destination: The new folder to copy the selected inputs into.
        main: The main file, relative to ``root``.
        evidence: What is known about the files the build needs.

    Returns:
        The files left out, the findings, and the inventory of every project file.
    """
    observed = evidence.observed
    if observed is None or main not in observed:
        return _missing_trace(evidence.uncertainty)
    analysis = analyze_selected_sources(root, main)
    findings = _graph_advisories(analysis)
    built = shipped_generated_files(main, evidence)
    included = (analysis.dependencies | observed | evidence.explicit) - set(built)
    unusable = _unusable_inputs(root, included)
    if unusable:
        return InputSelectionResult([], [*findings, *unusable], [])
    files = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
    shipped = included | set(built)
    regenerated = _regenerated_supplied_files(main, evidence) - shipped
    changes = [
        Change(name, kind=ChangeKind.EXCLUDE, reason=unused_reason(name, regenerated))
        for name in sorted(files - shipped)
    ]
    _copy_inputs(root, destination, included, built)
    inventory = selection_inventory(
        root,
        files=files,
        literal=analysis.dependencies,
        observed=set(observed),
        explicit=set(evidence.explicit),
        built=set(built),
        regenerated=regenerated,
    )
    findings.append(_selection_finding(shipped, len(changes), evidence, set(built)))
    return InputSelectionResult(changes, findings, inventory)
