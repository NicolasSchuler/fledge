"""Staging copies of an imported project: document selection, transformations and flattening.

Every write goes to a new directory in the job workspace; the imported snapshot
is never modified. Workers never write source files: each plan is validated
completely before the first file changes.
"""

from __future__ import annotations

import fnmatch
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from .bibliography_transform import normalize_dois, plan_bibliography
from .cancellation import cancellation_point
from .config import Settings
from .errors import PreparationError
from .file_safety import is_contained_regular_file
from .findings import Finding, has_blockers
from .metadata_privacy import plan_metadata_sanitization
from .project import plan_cleanup
from .report import Change
from .source import plan_flatten
from .source_transform import plan_source_transforms
from .workflow_options import DocumentOptions


def _is_inside(root: Path, path: Path) -> bool:
    """Whether ``path`` is not a link and resolves inside ``root``."""
    return not path.is_symlink() and path.resolve().is_relative_to(root.resolve())


def stage_document(snapshot: Path, destination: Path, document: DocumentOptions) -> Path:
    """Materialize one explicit file selection from an already validated import."""
    source = snapshot / document.source_directory
    if not source.is_dir() or not _is_inside(snapshot, source):
        raise PreparationError(f"Missing or external source_directory for document {document.name}")
    files = sorted(path for path in source.rglob("*") if path.is_file() or path.is_symlink())
    matched: set[str] = set()
    selected: list[tuple[Path, str]] = []
    for path in files:
        cancellation_point()
        relative = path.relative_to(source).as_posix()
        patterns = {
            pattern for pattern in document.include if fnmatch.fnmatchcase(relative, pattern)
        }
        if document.include and not patterns:
            continue
        if not _is_inside(source, path):
            raise PreparationError(f"Document {document.name} contains an unsafe input")
        matched.update(patterns)
        selected.append((path, relative))
    missing = set(document.include) - matched
    if missing:
        raise PreparationError(
            f"Document {document.name} include patterns matched no files: "
            f"{', '.join(sorted(missing))}"
        )
    if document.main not in {relative for _, relative in selected}:
        raise PreparationError(
            f"Document {document.name} does not include its main file {document.main}"
        )
    destination.mkdir(parents=True)
    for path, relative in selected:
        cancellation_point()
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    return destination


def inline_bibliography_files(settings: Settings) -> set[str]:
    """The bibliography that inlining reads, which must stay in the staged copy."""
    name = settings.source_transforms.inline_bibliography
    return {name} if name else set()


def apply_contents(
    root: Path, contents: dict[str, bytes], *, originals: dict[str, bytes] | None = None
) -> None:
    """Write proposed file contents after validating every target and precondition."""
    targets: list[tuple[Path, bytes]] = []
    if originals is not None and set(contents) != set(originals):
        raise PreparationError("Formatting proposals lack complete original-byte preconditions")
    for relative, data in sorted(contents.items()):
        cancellation_point()
        path = root / relative
        if not is_contained_regular_file(root, path):
            raise PreparationError(f"Invalid transformation path: {relative}")
        if originals is not None and path.read_bytes() != originals[relative]:
            raise PreparationError(f"Source changed after formatting was planned: {relative}")
        targets.append((path, data))
    for path, data in targets:
        cancellation_point()
        path.write_bytes(data)


class _StagedEdit(NamedTuple):
    """One planned transformation; ``apply`` runs only when its findings do not block."""

    changes: list[Change]
    findings: list[Finding]
    apply: Callable[[], None]


def _metadata_edit(prepared: Path, settings: Settings) -> _StagedEdit:
    plan = plan_metadata_sanitization(prepared, settings.metadata_privacy)
    return _StagedEdit(
        plan.changes,
        plan.findings,
        lambda: apply_contents(prepared, plan.contents, originals=plan.originals),
    )


def _bibliography_edit(prepared: Path, main: str, settings: Settings) -> _StagedEdit:
    contents, changes, findings = plan_bibliography(prepared, main, settings.bibliography_transform)
    return _StagedEdit(changes, findings, lambda: apply_contents(prepared, contents))


def _doi_edit(prepared: Path, main: str) -> _StagedEdit:
    contents, changes, findings = normalize_dois(prepared, main)
    return _StagedEdit(changes, findings, lambda: apply_contents(prepared, contents))


def _source_edit(prepared: Path, main: str, settings: Settings, retained: set[str]) -> _StagedEdit:
    plan = plan_source_transforms(prepared, main, settings.source_transforms)

    def apply() -> None:
        if set(plan.removal_originals) != plan.removals:
            raise PreparationError("Source removal plan lacks complete preconditions")
        for name, original in plan.removal_originals.items():
            candidate = prepared / name
            if not _is_inside(prepared, candidate) or candidate.read_bytes() != original:
                raise PreparationError("Source changed after removal was planned")
        apply_contents(prepared, plan.contents, originals=plan.originals)
        for name in sorted(plan.removals - retained):
            cancellation_point()
            (prepared / name).unlink()

    return _StagedEdit(plan.changes, plan.findings, apply)


@dataclass(frozen=True, slots=True)
class TransformOutcome:
    """The staged copy after the selected transformations, and what they changed or found."""

    prepared: Path
    changes: list[Change]
    findings: list[Finding]


def transform_sources(
    snapshot: Path,
    work: Path,
    main: str,
    settings: Settings,
    dependencies: set[str],
    retained: set[str],
) -> TransformOutcome:
    """Copy the snapshot to staging, remove debris and apply the selected edits in order."""
    prepared = work / "prepared"
    shutil.copytree(snapshot, prepared)
    changes: list[Change] = []
    findings: list[Finding] = []
    cancellation_point()
    if settings.cleanup:
        cleanup = plan_cleanup(
            prepared, dependencies | retained | inline_bibliography_files(settings)
        )
        for change in cleanup:
            cancellation_point()
            candidate = prepared / change.path
            if candidate.is_file():
                candidate.unlink()
        changes.extend(cleanup)
    edits: list[Callable[[], _StagedEdit]] = [
        lambda: _metadata_edit(prepared, settings),
        lambda: _bibliography_edit(prepared, main, settings),
        *([lambda: _doi_edit(prepared, main)] if settings.normalize_doi else []),
        lambda: _source_edit(prepared, main, settings, retained),
    ]
    for plan in edits:
        edit = plan()
        findings.extend(edit.findings)
        changes.extend(edit.changes)
        if has_blockers(edit.findings):
            # A blocked edit is reported but not applied, and later edits are not planned.
            break
        edit.apply()
    else:
        # Every edit applied, including the source edit, which keeps retained files.
        changes = [
            change
            for change in changes
            if not (change.kind == "exclude" and change.path in retained)
        ]
    return TransformOutcome(prepared, changes, findings)


@dataclass(frozen=True, slots=True)
class FlattenOutcome:
    """The shipped sources, flattened when the flat layout is selected."""

    root: Path
    main: str
    changes: list[Change]
    findings: list[Finding]
    # Source-to-package names of a completed flattening; empty otherwise.
    path_mapping: dict[str, str]


def flatten_sources(shipment: Path, work: Path, main: str, settings: Settings) -> FlattenOutcome:
    """Move every shipped file to the top level when ``layout = "flat"``."""
    if settings.layout != "flat":
        return FlattenOutcome(shipment, main, [], [], {})
    flatten = plan_flatten(
        shipment, main, filename_overrides=settings.source_transforms.filename_overrides
    )
    if has_blockers(flatten.findings):
        return FlattenOutcome(shipment, main, flatten.changes, flatten.findings, {})
    flat = work / "flat"
    flat.mkdir()
    for old, new in sorted(flatten.mapping.items()):
        cancellation_point()
        with (flat / new).open("xb") as stream:
            stream.write(flatten.contents.get(old, (shipment / old).read_bytes()))
    return FlattenOutcome(
        flat, flatten.mapping[main], flatten.changes, flatten.findings, flatten.mapping
    )
