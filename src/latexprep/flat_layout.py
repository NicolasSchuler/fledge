"""Planning a flat layout: every shipped file at the top level, references rewritten to match.

``plan_flatten`` builds the literal dependency graph (``source_graph``), chooses a
portable top-level name for every file, and rewrites the literal file references of
the sources that name them. It returns a reviewable plan and never touches the
project. A flat layout is only safe when the graph is complete, so every gap the graph
found becomes an error finding, and callers must reject a plan with error findings
before applying its mapping or contents. Inspecting sources without planning is
``source_analysis``.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path, PurePosixPath
from typing import NamedTuple

from .errors import PreparationError
from .findings import Finding
from .project_files import path_comparison_key
from .report import Change, ChangeKind
from .source_analysis import lint_sources
from .source_file_commands import (
    DEFAULT_SUFFIXES_BY_FILE_COMMAND,
    FILE_COMMANDS,
    GRAPHICS_SUFFIXES,
    TOOLCHAIN_FILE_COMMANDS,
)
from .source_graph import SourceGraph
from .text_edits import TextEdit, apply_text_edits, unified_diff_text

# Longest file name, in UTF-8 bytes, that every file system a submission may travel
# through accepts, with room left for a collision counter.
_MAX_FILENAME_BYTES = 200
# Longest suffix, in UTF-8 bytes, still treated as an extension; a longer one is part of
# the name and may be truncated with it.
_MAX_SUFFIX_BYTES = 32
_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
# Files whose name is part of a contract with TeX or a bibliography tool, besides the main file.
_IDENTITY_SUFFIXES = frozenset({".sty", ".cls", ".bst", ".bbl"})


class NamePriority(IntEnum):
    """Who keeps a plain name when files collide; the lower value is served first."""

    # The main file, a class, style or bibliography, or a file with an explicit override.
    REQUIRED = 0
    # A file whose sanitized name no other file shares.
    UNIQUE = 1
    # A file that shares its name with another, so its directories join the name.
    QUALIFIED = 2


class _Candidate(NamedTuple):
    priority: NamePriority
    path: str
    name: str


class FlatNames(NamedTuple):
    """The flat name of every file that received one, and what naming found wrong."""

    mapping: dict[str, str]
    findings: list[Finding]


@dataclass
class FlattenPlan:
    """A proposed flat layout: where each file goes, new contents, and what to review.

    ``contents`` keys are original paths. ``mapping`` is diagnostic only while any
    finding is an error.
    """

    mapping: dict[str, str] = field(default_factory=dict)
    contents: dict[str, bytes] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    changes: list[Change] = field(default_factory=list)


class _Rewrites(NamedTuple):
    contents: dict[str, bytes]
    changes: list[Change]


def _split_name(value: str) -> tuple[str, str]:
    suffix = PurePosixPath(value).suffix
    # Long apparent extensions are ordinary truncatable name text. Use the
    # same rule after sanitization, which can itself create a long suffix.
    if len(suffix.encode("utf-8")) > _MAX_SUFFIX_BYTES:
        return value, ""
    return (value[: -len(suffix)], suffix) if suffix else (value, "")


def _fit_name(stem: str, ending: str) -> str:
    available = _MAX_FILENAME_BYTES - len(ending.encode("utf-8"))
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


def _override_findings(
    files: Iterable[str],
    fixed: set[str],
    overrides: tuple[tuple[str, str], ...],
    requested: dict[str, str],
) -> list[Finding]:
    """What is wrong with the requested filename overrides, if anything."""
    known = set(files)
    findings: list[Finding] = []
    if len(requested) != len(overrides):
        findings.append(
            Finding(
                "flatten.filename_overrides",
                "A source has more than one filename override.",
                severity="error",
                status="failed",
            )
        )
    for name, output in requested.items():
        message = None
        status = "failed"
        if name not in known:
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
    return findings


def _name_candidates(
    files: list[str], fixed: set[str], requested: dict[str, str], findings: list[Finding]
) -> list[_Candidate]:
    """The name each file would like, and how strongly; unsanitizable identities are reported."""
    groups: dict[str, list[str]] = defaultdict(list)
    for name in files:
        groups[path_comparison_key(_safe_name(PurePosixPath(name).name))].append(name)
    candidates: list[_Candidate] = []
    for name in files:
        basename = PurePosixPath(name).name
        safe = _safe_name(basename)
        if name in requested:
            candidates.append(_Candidate(NamePriority.REQUIRED, name, requested[name]))
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
            candidates.append(_Candidate(NamePriority.REQUIRED, name, basename))
        elif len(groups[path_comparison_key(safe)]) == 1:
            candidates.append(_Candidate(NamePriority.UNIQUE, name, safe))
        else:
            qualified = _safe_name("-".join(PurePosixPath(name).parts))
            candidates.append(_Candidate(NamePriority.QUALIFIED, name, qualified))
    return candidates


def _assign_unique_names(
    candidates: list[_Candidate], requested: dict[str, str], findings: list[Finding]
) -> dict[str, str]:
    """Give each candidate its name, or the next free one by counter.

    Two required names that collide are an error: neither may be renamed.
    """
    mapping: dict[str, str] = {}
    used: dict[str, str] = {}
    for candidate in sorted(
        candidates, key=lambda item: (item.priority, path_comparison_key(item.path), item.path)
    ):
        name = candidate.name
        owner = used.get(path_comparison_key(name))
        if candidate.priority is NamePriority.REQUIRED and owner is not None:
            findings.append(
                Finding(
                    "flatten.filename_overrides"
                    if candidate.path in requested or owner in requested
                    else "flatten.identity_collision",
                    "Files with required class/style, bibliography, or root identities "
                    "collide after relocation.",
                    severity="error",
                    path=candidate.path,
                    details={"other": owner, "filename": name},
                    suggestion='Set layout = "preserve", or separate these files into packages.',
                )
            )
            continue
        counter = 2
        while path_comparison_key(name) in used:
            name = _with_counter(candidate.name, counter)
            counter += 1
        used[path_comparison_key(name)] = candidate.path
        mapping[candidate.path] = name
    return mapping


def flat_filenames(
    files: Iterable[str], main: str, filename_overrides: tuple[tuple[str, str], ...] = ()
) -> FlatNames:
    """Choose a portable top-level name for every file.

    The main file, classes, styles and generated bibliographies keep their names, and an
    explicit override replaces a name; two required names that collide are an error.
    Other files take their sanitized name when it is unique, else their directories
    joined into it, with a counter added if names still collide.
    """
    names = list(files)
    fixed = {
        name
        for name in names
        if name == main or PurePosixPath(name).suffix.lower() in _IDENTITY_SUFFIXES
    }
    requested = dict(filename_overrides)
    findings = _override_findings(names, fixed, filename_overrides, requested)
    candidates = _name_candidates(names, fixed, requested, findings)
    mapping = _assign_unique_names(candidates, requested, findings)
    if requested and not any(item.rule == "flatten.filename_overrides" for item in findings):
        findings.append(
            Finding(
                "flatten.filename_overrides",
                "Explicit filename overrides are portable and unique.",
                severity="info",
                status="passed",
                details={"count": len(requested)},
            )
        )
    return FlatNames({name: mapping[name] for name in names if name in mapping}, findings)


def _replacement(target: str, command: str, mapping: dict[str, str]) -> str:
    output = mapping[target]
    if command in {"include", "bibliography", *TOOLCHAIN_FILE_COMMANDS}:
        (suffix,) = DEFAULT_SUFFIXES_BY_FILE_COMMAND[command]
        if output.lower().endswith(suffix):
            output = output[: -len(suffix)]
    return output


def _translate_uncertainties(graph: SourceGraph) -> list[Finding]:
    """Every gap in the graph as a flat-layout error: what cannot be followed cannot be moved."""
    return [
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
        for item in graph.uncertainties
    ]


def _check_roots(graph: SourceGraph) -> list[Finding]:
    """A flat package holds one document; a second root cannot share the top level."""
    other_roots = graph.other_roots(include_subfiles=False)
    if not other_roots:
        return []
    return [
        Finding(
            "flatten.multiple_roots",
            "Flat layout supports one document root per package, "
            "but the package contains other document roots.",
            severity="error",
            status="inconclusive",
            details={"other_roots": other_roots},
            suggestion="Select only this document and its dependencies.",
        )
    ]


def _check_unvisited_sources(graph: SourceGraph) -> list[Finding]:
    """Sources the walk never reached that still name files: their directory is unknown."""
    return [
        Finding(
            "flatten.unresolved_context",
            "An unvisited source has file references with no established compilation context.",
            severity="error",
            status="inconclusive",
            path=name,
            suggestion="Declare a literal input for the file, exclude it from the package, "
            'or set layout = "preserve".',
        )
        for name, source in graph.sources.items()
        if name not in graph.visited
        and any(
            command.name in FILE_COMMANDS or command.name == "graphicspath"
            for command in source.commands
        )
    ]


def _check_system_shadowing(graph: SourceGraph, mapping: dict[str, str]) -> list[Finding]:
    """A relocated file that would take the name of a class or style TeX loads from its install."""
    findings = []
    for reference in graph.system_references:
        shadows = [
            path
            for path, output in mapping.items()
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
    return findings


def _check_companions(mapping: dict[str, str]) -> list[Finding]:
    """A graphics sidecar (``.bb``, ``.xbb``) must keep sharing its image's base name."""
    findings = []
    for name, output in mapping.items():
        if not name.endswith((".bb", ".xbb")):
            continue
        stem = name.rsplit(".", 1)[0]
        for extension in GRAPHICS_SUFFIXES:
            companion = stem + extension
            if (
                companion in mapping
                and PurePosixPath(mapping[companion]).stem != PurePosixPath(output).stem
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
    return findings


def _rewrite_sources(graph: SourceGraph, mapping: dict[str, str]) -> _Rewrites:
    """Rewrite each literal file reference, and each search path, to the flat names."""
    edits: dict[str, dict[tuple[int, int], str]] = defaultdict(dict)
    for location, reference in graph.references.items():
        if reference.target in mapping:
            edits[location.path][(location.start, location.end)] = _replacement(
                reference.target, reference.command, mapping
            )
    for location, replacement in graph.search_arguments.items():
        edits[location.path][(location.start, location.end)] = replacement
    contents: dict[str, bytes] = {}
    changes: list[Change] = []
    for name, replacements in edits.items():
        source = graph.sources[name]
        rewritten = apply_text_edits(
            source.text,
            (
                TextEdit(start, end, replacement)
                for (start, end), replacement in replacements.items()
            ),
        )
        if rewritten == source.text:
            continue
        contents[name] = source.encode(rewritten)
        diff = unified_diff_text(name, source.text, rewritten, destination=mapping.get(name, name))
        changes.append(
            Change(
                name,
                kind=ChangeKind.REWRITE,
                reason="Update literal file references for flat layout.",
                diff=diff,
            )
        )
    return _Rewrites(contents, changes)


def _move_changes(mapping: dict[str, str]) -> list[Change]:
    return [
        Change(
            name,
            kind=ChangeKind.MOVE,
            reason="Move selected file into the flat submission directory.",
            destination=output,
        )
        for name, output in mapping.items()
        if name != output
    ]


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
    findings.extend(_translate_uncertainties(graph))
    findings.extend(_check_roots(graph))
    findings.extend(_check_unvisited_sources(graph))
    names = flat_filenames(graph.files, graph.main, filename_overrides)
    plan.mapping = names.mapping
    findings.extend(names.findings)
    findings.extend(_check_system_shadowing(graph, plan.mapping))
    findings.extend(_check_companions(plan.mapping))
    rewrites = _rewrite_sources(graph, plan.mapping)
    plan.contents = rewrites.contents
    plan.changes = [*rewrites.changes, *_move_changes(plan.mapping)]
    return plan
