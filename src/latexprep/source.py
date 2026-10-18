"""Conservative source inspection and literal, command-aware flat-layout planning.

The literal dependency graph comes from ``source_graph`` and is a lexical subset,
not a TeX interpreter. Flat plans reject dynamic paths, unknown file readers,
scoped paths and ambiguous lookups; callers must reject plans with error findings
before applying any mapping or contents.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .literal_text import REFERENCE_COMMANDS
from .models import Change, ChangeKind, EvidenceKind, Finding, PreparationError
from .source_graph import (
    DEFAULT_SUFFIXES_BY_FILE_COMMAND,
    FILE_COMMANDS,
    GRAPHICS_SUFFIXES,
    ROOT_SUFFIXES,
    TOOLCHAIN_FILE_COMMANDS,
    SourceGraph,
    is_document_root,
    is_literal_text,
    path_comparison_key,
    project_files,
)
from .tex_lexer import (
    MAX_SOURCE_BYTES,
    ParseLimitExceeded,
    scan_source_commands,
    split_argument_items,
)
from .text_edits import unified_diff_text

# Manuscript text, as opposed to class, style and generated template internals.
_MANUSCRIPT_SUFFIXES = {".tex", ".ltx", ".latex", ".tikz", ".pgf"}
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
_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


@dataclass
class SourceAnalysis:
    roots: list[str]
    main: str | None
    findings: list[Finding] = field(default_factory=list)
    dependencies: set[str] = field(default_factory=set)
    # Completeness is bounded by the supported literal subset, not arbitrary TeX execution.
    complete: bool = False


@dataclass
class FlattenPlan:
    mapping: dict[str, str] = field(default_factory=dict)
    contents: dict[str, bytes] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    changes: list[Change] = field(default_factory=list)


def discover_roots(root: Path) -> list[str]:
    """Find literal document roots; oversized, linked or non-UTF-8 files are skipped.

    Full analysis reports skipped-source diagnostics. This preflight performs no
    dependency lookup and never chooses between multiple plausible entry points.
    """
    roots: list[str] = []
    for name, path in project_files(root).items():
        if path.suffix.lower() not in ROOT_SUFFIXES or path.is_symlink():
            continue
        if path.stat().st_size > MAX_SOURCE_BYTES:
            continue
        try:
            text = path.read_bytes().decode("utf-8-sig")
        except (UnicodeDecodeError, OSError):
            continue
        try:
            if is_document_root(scan_source_commands(text)):
                roots.append(name)
        except ParseLimitExceeded:
            continue
    return roots


def lint_sources(graph: SourceGraph) -> list[Finding]:
    """Heuristic manuscript findings: editing markers, duplicate labels, unresolved references.

    With a selected main file, only sources visited from it are checked.
    """
    findings: list[Finding] = []
    labels: dict[str, list[tuple[str, int]]] = defaultdict(list)
    references: list[tuple[str, str, int]] = []
    for name, source in graph.sources.items():
        if graph.main and name not in graph.visited:
            continue
        # Template internals are not authored manuscript text.
        if Path(name).suffix.lower() not in _MANUSCRIPT_SUFFIXES:
            continue
        for match in re.finditer(r"\b(?:TODO|FIXME|XXX)\b|\?\?", source.masked):
            findings.append(
                Finding(
                    "source.edit_marker",
                    f"Possible unfinished editing or placeholder text: {match.group()!r}.",
                    path=name,
                    line=source.text.count("\n", 0, match.start()) + 1,
                    evidence_kind=EvidenceKind.HEURISTIC,
                )
            )
        for command in source.commands:
            if command.name in _EDIT_COMMANDS:
                findings.append(
                    Finding(
                        "source.edit_command",
                        f"\\{command.name} may produce visible editing markup "
                        "or layout diagnostics.",
                        path=name,
                        line=command.line,
                        evidence_kind=EvidenceKind.HEURISTIC,
                    )
                )
            if command.name == "label" and command.arguments:
                value = command.arguments[0].value.strip()
                if is_literal_text(value):
                    labels[value].append((name, command.line))
            if command.name in REFERENCE_COMMANDS and command.arguments:
                for argument in split_argument_items(
                    command.arguments[0], comma_separated=command.name in {"cref", "Cref"}
                ):
                    if is_literal_text(argument.value):
                        references.append((argument.value, name, command.line))
    for label, locations in labels.items():
        if len(locations) > 1:
            for name, line in locations:
                findings.append(
                    Finding(
                        "source.duplicate_label",
                        f"Label {label!r} has multiple definitions in the source graph.",
                        path=name,
                        line=line,
                        evidence_kind=EvidenceKind.HEURISTIC,
                        details={
                            "label": label,
                            "locations": [
                                {"path": path, "line": location_line}
                                for path, location_line in locations
                            ],
                        },
                    )
                )
    for label, name, line in references:
        if label not in labels:
            findings.append(
                Finding(
                    "source.unresolved_reference",
                    f"Reference {label!r} has no definition in the inspected source graph.",
                    path=name,
                    line=line,
                    evidence_kind=EvidenceKind.HEURISTIC,
                    suggestion="Confirm with compilation; dynamic labels are not interpreted.",
                    details={"label": label},
                )
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


def _split_name(value: str) -> tuple[str, str]:
    suffix = PurePosixPath(value).suffix
    # Long apparent extensions are ordinary truncatable name text. Use the
    # same rule after sanitization, which can itself create a long suffix.
    if len(suffix.encode("utf-8")) > 32:
        return value, ""
    return (value[: -len(suffix)], suffix) if suffix else (value, "")


def _fit_name(stem: str, ending: str) -> str:
    available = 200 - len(ending.encode("utf-8"))
    if available < 0:
        raise PreparationError("A filename suffix exceeds the portable filename byte budget.")
    # A single bounded slice replaces trim loops. Ignoring the incomplete
    # final UTF-8 code point removes only a character split by the byte limit.
    return stem.encode("utf-8")[:available].decode("utf-8", errors="ignore") + ending


def _safe_name(value: str) -> str:
    value = unicodedata.normalize("NFC", value)
    value = re.sub(r"[^\w.\-]", "-", value, flags=re.UNICODE).strip(" .") or "file"
    if value.split(".")[0].upper() in _RESERVED_NAMES:
        value = "file-" + value
    return _fit_name(*_split_name(value))


def _with_counter(value: str, count: int) -> str:
    stem, suffix = _split_name(value)
    return _fit_name(stem, f"-{count}" + suffix)


def _mapping(
    files: dict[str, Path],
    main: str,
    findings: list[Finding],
    filename_overrides: tuple[tuple[str, str], ...] = (),
) -> dict[str, str]:
    groups: dict[str, list[str]] = defaultdict(list)
    fixed = {
        name
        for name in files
        if name == main or Path(name).suffix.lower() in {".sty", ".cls", ".bst", ".bbl"}
    }
    overrides = dict(filename_overrides)
    if len(overrides) != len(filename_overrides):
        findings.append(
            Finding(
                "flatten.filename_overrides",
                "A source has more than one filename override.",
                severity="error",
                status="failed",
            )
        )
    for name, output in overrides.items():
        message = None
        status = "failed"
        if name not in files:
            message = "A filename override names a file absent from the project."
        elif output != _safe_name(output) or "/" in output or "\\" in output:
            message = "An override destination must be one portable filename."
        elif PurePosixPath(output).suffix != PurePosixPath(name).suffix:
            message = "A filename override must preserve the file extension."
        elif name in fixed and output != PurePosixPath(name).name:
            message = (
                "Renaming a main, class, style or generated bibliography identity is unsupported."
            )
            status = "inconclusive"
        if message:
            findings.append(
                Finding(
                    "flatten.filename_overrides",
                    message,
                    severity="error",
                    status=status,
                    path=name,
                    details={"destination": output},
                )
            )
    for name in files:
        groups[path_comparison_key(_safe_name(PurePosixPath(name).name))].append(name)
    candidates: list[tuple[int, str, str]] = []
    for name in files:
        basename = PurePosixPath(name).name
        safe = _safe_name(basename)
        if name in overrides:
            candidates.append((0, name, overrides[name]))
        elif name in fixed:
            if safe != basename:
                findings.append(
                    Finding(
                        "flatten.identity_name",
                        "This root, class, style, or generated bibliography filename "
                        "cannot be sanitized without changing its naming contract.",
                        severity="error",
                        path=name,
                        suggestion='Set layout = "preserve", or rename the original file.',
                    )
                )
            candidates.append((0, name, basename))
        elif len(groups[path_comparison_key(safe)]) == 1:
            candidates.append((1, name, safe))
        else:
            candidates.append((2, name, _safe_name("-".join(PurePosixPath(name).parts))))
    mapping: dict[str, str] = {}
    used: dict[str, str] = {}
    for priority, name, candidate in sorted(
        candidates, key=lambda item: (item[0], path_comparison_key(item[1]), item[1])
    ):
        if priority == 0 and path_comparison_key(candidate) in used:
            findings.append(
                Finding(
                    "flatten.filename_overrides"
                    if name in overrides or used[path_comparison_key(candidate)] in overrides
                    else "flatten.identity_collision",
                    "Files with required class/style, bibliography, or root identities "
                    "collide after relocation.",
                    severity="error",
                    path=name,
                    details={"other": used[path_comparison_key(candidate)], "filename": candidate},
                    suggestion='Set layout = "preserve", or separate these files into packages.',
                )
            )
            continue
        original = candidate
        counter = 2
        while path_comparison_key(candidate) in used:
            candidate = _with_counter(original, counter)
            counter += 1
        used[path_comparison_key(candidate)] = name
        mapping[name] = candidate
    if overrides and not any(item.rule == "flatten.filename_overrides" for item in findings):
        findings.append(
            Finding(
                "flatten.filename_overrides",
                "Explicit filename overrides are portable and unique.",
                severity="info",
                status="passed",
                details={"count": len(overrides)},
            )
        )
    return {name: mapping[name] for name in files if name in mapping}


def _replacement(target: str, command: str, mapping: dict[str, str]) -> str:
    output = mapping[target]
    if command in {"include", "bibliography", *TOOLCHAIN_FILE_COMMANDS}:
        (suffix,) = DEFAULT_SUFFIXES_BY_FILE_COMMAND[command]
        if output.lower().endswith(suffix):
            output = output[: -len(suffix)]
    return output


def plan_flatten(
    root: Path,
    main: str,
    *,
    filename_overrides: tuple[tuple[str, str], ...] = (),
) -> FlattenPlan:
    """Return a reviewable flat map and rewritten bytes; never mutate the project.

    ``contents`` keys are original paths. Error findings make the plan unusable;
    partial mappings are diagnostic only. Caller-owned staging and rebuilt-PDF
    verification remain required, including for a plan without error findings.
    """
    graph = SourceGraph.build(root, main)
    findings = [*graph.findings, *lint_sources(graph)]
    plan = FlattenPlan(findings=findings)
    if graph.main is None:
        return plan
    for item in graph.uncertainties:
        findings.append(
            Finding(
                "flatten.unsupported_source",
                item.message,
                severity="error",
                status="inconclusive",
                path=item.path,
                line=item.line,
                suggestion=item.suggestion
                or 'Resolve this source construct, or set layout = "preserve".',
                details={"source_rule": item.rule, **item.details},
            )
        )
    other_roots = [
        name for name in graph.roots if name != graph.main and name not in graph.subfile_targets
    ]
    if other_roots:
        findings.append(
            Finding(
                "flatten.multiple_roots",
                "Flat layout supports one document root per package, "
                "but the package contains other document roots.",
                severity="error",
                status="inconclusive",
                details={"other_roots": other_roots},
                suggestion="Select only this document and its dependencies.",
            )
        )
    for name, source in graph.sources.items():
        if name not in graph.visited and any(
            command.name in FILE_COMMANDS or command.name == "graphicspath"
            for command in source.commands
        ):
            findings.append(
                Finding(
                    "flatten.unresolved_context",
                    "An unvisited source has file references "
                    "with no established compilation context.",
                    severity="error",
                    status="inconclusive",
                    path=name,
                    suggestion="Declare a literal input for the file, exclude it from the package, "
                    'or set layout = "preserve".',
                )
            )
    plan.mapping = _mapping(graph.files, graph.main, findings, filename_overrides)
    for reference in graph.system_references:
        shadows = [
            path
            for path, output in plan.mapping.items()
            if path_comparison_key(output) == path_comparison_key(reference.basename)
        ]
        if shadows:
            findings.append(
                Finding(
                    "flatten.system_shadowing",
                    "Relocation would make a local file shadow "
                    "an unresolved toolchain class or style reference.",
                    severity="error",
                    path=reference.path,
                    line=reference.command.line,
                    details={"reference": reference.reference, "files": shadows},
                    suggestion='Set layout = "preserve", or name the intended file explicitly.',
                )
            )
    for name, output in plan.mapping.items():
        if name.endswith((".bb", ".xbb")):
            stem = name.rsplit(".", 1)[0]
            for extension in GRAPHICS_SUFFIXES:
                companion = stem + extension
                if (
                    companion in plan.mapping
                    and PurePosixPath(plan.mapping[companion]).stem != PurePosixPath(output).stem
                ):
                    findings.append(
                        Finding(
                            "flatten.companion_identity",
                            "A graphics sidecar and its image would no longer share a basename.",
                            severity="error",
                            path=name,
                            details={"companion": companion},
                            suggestion="Preserve layout or resolve companion filenames together.",
                        )
                    )
    edits: dict[str, dict[tuple[int, int], str]] = defaultdict(dict)
    for location, reference in graph.references.items():
        if reference.target in plan.mapping:
            edits[location.path][(location.start, location.end)] = _replacement(
                reference.target, reference.command, plan.mapping
            )
    for location, replacement in graph.search_arguments.items():
        edits[location.path][(location.start, location.end)] = replacement
    for name, replacements in edits.items():
        source = graph.sources[name]
        rewritten = source.text
        for (start, end), replacement in sorted(replacements.items(), reverse=True):
            rewritten = rewritten[:start] + replacement + rewritten[end:]
        if rewritten != source.text:
            plan.contents[name] = (b"\xef\xbb\xbf" if source.bom else b"") + rewritten.encode(
                "utf-8"
            )
            diff = unified_diff_text(
                name, source.text, rewritten, destination=plan.mapping.get(name, name)
            )
            plan.changes.append(
                Change(
                    name,
                    kind=ChangeKind.REWRITE,
                    reason="Update literal file references for flat layout.",
                    diff=diff,
                )
            )
    for name, output in plan.mapping.items():
        if name != output:
            plan.changes.append(
                Change(
                    name,
                    kind=ChangeKind.MOVE,
                    reason="Move selected file into the flat submission directory.",
                    destination=output,
                )
            )
    return plan
