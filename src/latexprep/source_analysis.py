"""Inspecting a project's sources without editing them: roots, dependencies and lint.

``discover_roots`` is the cheap preflight that finds literal document roots.
``analyze_sources`` and ``analyze_selected_sources`` build the literal dependency graph
(``source_graph``) and report its findings, the files the selected document reads, and
whether the graph is complete; ``lint_sources`` adds heuristic manuscript findings.
The graph is a lexical subset, not a TeX interpreter, so completeness means "nothing
the subset could not follow", not "nothing else is read". Planning a flat layout is
``flat_layout``.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from .comment_markers import MarkerCategory, marker_pattern
from .file_safety import read_regular_file
from .findings import EvidenceKind, Finding
from .literal_text import REFERENCE_COMMANDS
from .project_files import project_files
from .source_file_commands import is_literal_text
from .source_graph import SourceGraph
from .source_loading import ParsedSource, is_document_root
from .tex_commands import MANUSCRIPT_SUFFIXES, ROOT_SUFFIXES
from .tex_lexer import (
    MAX_SOURCE_BYTES,
    ParseLimitExceeded,
    scan_source_commands,
    split_argument_items,
)

# Active manuscript text is matched case-sensitively: lowercase "todo" or "xxx" can be
# ordinary words of the manuscript, unlike the capitalised markers.
_EDIT_MARKER = marker_pattern(
    MarkerCategory.EDIT_MARKER, MarkerCategory.UNCERTAINTY_MARK, ignore_case=False
)
# Ordinary formatting such as \textcolor is not an editing marker.
_EDIT_COMMANDS = {
    "todo",
    "TODO",
    "hl",
    "added",
    "deleted",
    "replaced",
    "showframe",
    "linenumbers",
    "SetWatermarkText",
    "draftwatermark",
}


@dataclass
class SourceAnalysis:
    """What source inspection found for one project: roots, main file, findings, reads."""

    roots: list[str]
    main: str | None
    findings: list[Finding] = field(default_factory=list)
    dependencies: set[str] = field(default_factory=set)
    # Completeness is bounded by the supported literal subset, not arbitrary TeX execution.
    complete: bool = False


def discover_roots(root: Path) -> list[str]:
    """Find literal document roots; oversized, linked or non-UTF-8 files are skipped.

    Full analysis reports skipped-source diagnostics. This preflight performs no
    dependency lookup and never chooses between multiple plausible entry points.
    """
    roots: list[str] = []
    for name, path in project_files(root).items():
        if path.suffix.lower() not in ROOT_SUFFIXES:
            continue
        try:
            text = read_regular_file(path, MAX_SOURCE_BYTES).decode("utf-8-sig")
        except (UnicodeDecodeError, OSError):
            continue
        try:
            if is_document_root(scan_source_commands(text)):
                roots.append(name)
        except ParseLimitExceeded:
            continue
    return roots


def _manuscript_sources(graph: SourceGraph) -> Iterator[tuple[str, ParsedSource]]:
    """The sources lint reads: authored manuscript text, from the visited files if a main is set."""
    for name, source in graph.sources.items():
        if graph.main and name not in graph.visited:
            continue
        # Template internals are not authored manuscript text.
        if Path(name).suffix.lower() in MANUSCRIPT_SUFFIXES:
            yield name, source


def _edit_findings(name: str, source: ParsedSource) -> list[Finding]:
    """Editing markers in the active text and commands that print editing markup."""
    findings = [
        Finding(
            "source.edit_marker",
            f"Possible unfinished editing or placeholder text: {match.group()!r}.",
            path=name,
            line=source.line_at(match.start()),
            evidence_kind=EvidenceKind.HEURISTIC,
        )
        for match in _EDIT_MARKER.finditer(source.masked)
    ]
    findings.extend(
        Finding(
            "source.edit_command",
            f"\\{command.name} may produce visible editing markup or layout diagnostics.",
            path=name,
            line=command.line,
            evidence_kind=EvidenceKind.HEURISTIC,
        )
        for command in source.commands
        if command.name in _EDIT_COMMANDS
    )
    return findings


def _defined_labels(source: ParsedSource) -> list[tuple[str, int]]:
    """The literal labels a source defines, with their lines."""
    labels = []
    for command in source.commands:
        if command.name == "label" and command.arguments:
            value = command.arguments[0].value.strip()
            if is_literal_text(value):
                labels.append((value, command.line))
    return labels


def _referenced_labels(source: ParsedSource) -> list[tuple[str, int]]:
    """The literal labels a source refers to, with their lines."""
    references = []
    for command in source.commands:
        if command.name in REFERENCE_COMMANDS and command.arguments:
            for argument in split_argument_items(
                command.arguments[0], comma_separated=command.name in {"cref", "Cref"}
            ):
                if is_literal_text(argument.value):
                    references.append((argument.value, command.line))
    return references


def _duplicate_label_findings(labels: dict[str, list[tuple[str, int]]]) -> list[Finding]:
    return [
        Finding(
            "source.duplicate_label",
            f"Label {label!r} has multiple definitions in the source graph.",
            path=name,
            line=line,
            evidence_kind=EvidenceKind.HEURISTIC,
            details={
                "label": label,
                "locations": [
                    {"path": path, "line": location_line} for path, location_line in locations
                ],
            },
        )
        for label, locations in labels.items()
        if len(locations) > 1
        for name, line in locations
    ]


def lint_sources(graph: SourceGraph) -> list[Finding]:
    """Heuristic manuscript findings: editing markers, duplicate labels, unresolved references.

    With a selected main file, only sources visited from it are checked.
    """
    findings: list[Finding] = []
    labels: dict[str, list[tuple[str, int]]] = defaultdict(list)
    references: list[tuple[str, str, int]] = []
    for name, source in _manuscript_sources(graph):
        findings.extend(_edit_findings(name, source))
        for label, line in _defined_labels(source):
            labels[label].append((name, line))
        references.extend((label, name, line) for label, line in _referenced_labels(source))
    findings.extend(_duplicate_label_findings(labels))
    findings.extend(
        Finding(
            "source.unresolved_reference",
            f"Reference {label!r} has no definition in the inspected source graph.",
            path=name,
            line=line,
            evidence_kind=EvidenceKind.HEURISTIC,
            suggestion="Confirm with compilation; dynamic labels are not interpreted.",
            details={"label": label},
        )
        for label, name, line in references
        if label not in labels
    )
    return findings


def analyze_sources(root: Path, main: str | None = None) -> SourceAnalysis:
    """Inspect a selected root and report literal dependencies without editing files."""
    graph = SourceGraph.build(root, main)
    # Every source file is read to discover roots, but a selected document is not
    # described by lexical uncertainties of files outside its own graph.
    outside = {
        id(item)
        for item in graph.uncertainties
        if graph.main is not None and item.path is not None and item.path not in graph.visited
    }
    findings = [
        *(item for item in graph.findings if id(item) not in outside),
        *lint_sources(graph),
    ]
    return SourceAnalysis(
        graph.roots,
        graph.main,
        findings,
        graph.dependencies,
        complete=bool(graph.main in graph.visited)
        and all(id(item) in outside for item in graph.uncertainties)
        and not any(finding.severity == "error" for finding in findings),
    )


def analyze_selected_sources(root: Path, main: str) -> SourceAnalysis:
    """Inspect only sources reached from an explicit main, leaving other sources unread.

    Dependencies and completeness describe the supported literal subset. Runtime
    input evidence and isolated rebuild verification remain necessary for packaging.
    """
    graph = SourceGraph.build(root, main, scope="selected")
    findings = [*graph.findings, *lint_sources(graph)]
    return SourceAnalysis(
        graph.roots,
        graph.main,
        findings,
        graph.dependencies,
        complete=bool(graph.main in graph.visited)
        and not graph.uncertainties
        and not any(finding.severity == "error" for finding in findings),
    )
