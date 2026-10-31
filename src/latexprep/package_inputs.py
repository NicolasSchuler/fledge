"""Select project inputs for one observed build, without modifying the source tree.

The files a package needs are the union of three kinds of evidence: the literal
dependencies of the source graph, the inputs the build recorded, and the files the user
asks to keep. ``select_package_inputs`` takes that union, checks that every file is a
plain, regular, contained file, and copies it into a new destination. It owns the
choice and the copy; the reasons behind each file are recorded as inventory rows
(``package_inventory``), and recorded inputs come from the build, not from here.
"""

from __future__ import annotations

import shutil
from collections.abc import Set
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import NamedTuple

from .cancellation import cancellation_point
from .findings import Finding
from .package_inventory import UNUSED_REASON, InventoryRow, selection_inventory
from .path_safety import is_contained, is_relative_posix_path
from .report import Change, ChangeKind
from .source_analysis import SourceAnalysis, analyze_selected_sources

# Generated bibliography files that the TeX run reads: the .bbl, which services
# compiling without BibTeX or Biber (arXiv among them) use instead of the .bib,
# and biblatex's .run.xml control file, which biblatex reads back on later passes.
GENERATED_BIBLIOGRAPHY_SUFFIXES = (".bbl", ".run.xml")
_RULE = "package.dependencies"


@dataclass(frozen=True, slots=True)
class InputEvidence:
    """What is known about the files one build needs.

    Attributes:
        observed: The inputs the build recorded; ``None`` means no complete trace exists,
            which is different from an empty set.
        explicit: The files the settings ask to keep.
        generated_reads: The names of generated bibliography files the build read, which
            decide whether a supplied copy is kept.
    """

    observed: Set[str] | None
    explicit: Set[str]
    generated_reads: Set[str] = frozenset()


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


def generated_bibliography_inputs(root: Path, main: str, generated_reads: Set[str]) -> set[str]:
    """Find the supplied <main>.bbl and <main>.run.xml beside the main file that the build read.

    The build regenerates these files in its output directory, so the recorder
    shows that a file of the same name was read rather than the supplied copy.
    A supplied copy is retained only when this build read one with its name,
    so a stale file from an unrelated configuration does not ship. Links and
    non-regular files are never retained.

    Args:
        root: The project folder.
        main: The main file, relative to ``root``.
        generated_reads: The names of the generated bibliography files the build read.

    Returns:
        The supplied copies to retain, relative to ``root``.
    """
    main_path = PurePosixPath(main)
    read = {PurePosixPath(name).name for name in generated_reads}
    retained: set[str] = set()
    for suffix in GENERATED_BIBLIOGRAPHY_SUFFIXES:
        name = main_path.stem + suffix
        candidate = (main_path.parent / name).as_posix()
        if name in read and is_contained(root, root / candidate, regular_file=True):
            retained.add(candidate)
    return retained


def _missing_trace() -> InputSelectionResult:
    """Return the result of a build without a complete trace: one error, nothing selected."""
    return InputSelectionResult(
        [],
        [
            Finding(
                _RULE,
                "Complete build dependency evidence is unavailable; no input set was selected.",
                severity="error",
                status="inconclusive",
                suggestion="Resolve the build's submission dependency diagnostic and retry.",
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


def _copy_inputs(root: Path, destination: Path, included: Set[str]) -> None:
    """Copy the selected inputs into ``destination``, which must not exist yet.

    Args:
        root: The project folder.
        destination: The folder to create and copy into.
        included: The selected inputs, relative to ``root``.
    """
    destination.mkdir()
    for name in sorted(included):
        cancellation_point()
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / name, target)


def _selection_finding(
    included: Set[str],
    omitted: int,
    evidence: InputEvidence,
    bibliography: Set[str],
) -> Finding:
    """Return the finding that sums up a successful selection.

    Args:
        included: The selected inputs.
        omitted: The number of project files that were left out.
        evidence: The evidence of the selection; its explicit files go into the details.
        bibliography: The supplied generated bibliography files retained beyond the
            recorded and explicit inputs.

    Returns:
        A passed info finding.
    """
    retained_bibliography = (
        f" Retained the generated bibliography {', '.join(sorted(bibliography))} for services "
        "that compile without BibTeX or Biber."
        if bibliography
        else ""
    )
    return Finding(
        _RULE,
        f"Selected {len(included)} project inputs; omitted {omitted} unrelated files."
        + retained_bibliography,
        severity="info",
        status="passed",
        details={
            "included": sorted(included),
            "explicit": sorted(evidence.explicit),
            "generated_bibliography": sorted(bibliography),
            "scope": "Supported literal dependencies and recorded inputs for the selected "
            "build configuration; not minimality across other TeX executions.",
        },
    )


def select_package_inputs(
    root: Path, destination: Path, main: str, evidence: InputEvidence
) -> InputSelectionResult:
    """Copy supported literal and observed inputs plus explicit retained files.

    A supplied generated bibliography beside the main file is retained when the
    build consumed a regenerated file of the same name (``evidence.generated_reads``).

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
        return _missing_trace()
    analysis = analyze_selected_sources(root, main)
    findings = _graph_advisories(analysis)
    generated = generated_bibliography_inputs(root, main, evidence.generated_reads)
    bibliography = generated - observed - evidence.explicit
    included = analysis.dependencies | observed | evidence.explicit | bibliography
    unusable = _unusable_inputs(root, included)
    if unusable:
        return InputSelectionResult([], [*findings, *unusable], [])
    files = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
    changes = [
        Change(name, kind=ChangeKind.EXCLUDE, reason=UNUSED_REASON)
        for name in sorted(files - included)
    ]
    _copy_inputs(root, destination, included)
    inventory = selection_inventory(
        root,
        files=files,
        literal=analysis.dependencies,
        observed=set(observed),
        explicit=set(evidence.explicit),
        generated=generated,
    )
    findings.append(_selection_finding(included, len(changes), evidence, bibliography))
    return InputSelectionResult(changes, findings, inventory)
