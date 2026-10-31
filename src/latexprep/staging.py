"""Staging copies of an imported project: document selection, transformations and flattening.

This is the orchestration layer between the planners and the file system. Planners
(``metadata_privacy``, ``bibliography_transform``, ``source_transform``,
``flat_layout``) only propose; this module decides the order, applies a proposal, and
copies files into the job workspace. Every write goes to a new directory there; the
imported snapshot is never modified. Workers never write source files: each plan is
validated completely before the first file changes.
"""

from __future__ import annotations

import fnmatch
import shutil
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from .bibliography_transform import normalize_dois, plan_bibliography
from .cancellation import cancellation_point
from .config import Settings
from .errors import PreparationError
from .findings import Finding, has_blockers
from .flat_layout import plan_flatten
from .metadata_source_edits import plan_metadata_sanitization
from .path_safety import is_contained
from .project_cleanup import plan_cleanup
from .report import Change
from .source_transform import plan_source_transforms
from .workflow_options import DocumentOptions


def stage_document(snapshot: Path, destination: Path, document: DocumentOptions) -> Path:
    """Materialize one explicit file selection from an already validated import.

    A file is selected when ``document.include`` is empty or one of its patterns matches
    the file's path relative to the document's source directory.

    Args:
        snapshot: The imported project.
        destination: The directory to create and copy the selected files into.
        document: The document whose source directory, include patterns and main file
            decide the selection.

    Returns:
        ``destination``.

    Raises:
        PreparationError: The source directory is missing or outside the snapshot, a
            selected input is a link or leads outside the source directory, an include
            pattern matched no file, or the selection lacks the document's main file.
    """
    source = snapshot / document.source_directory
    if not source.is_dir() or not is_contained(snapshot, source):
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
        if not is_contained(source, path):
            raise PreparationError(
                f"Document {document.name} contains an unsafe input, {relative!r}, which is a "
                "link or leads outside its source directory; replace it with the file itself"
            )
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
    """Return the bibliography that inlining reads, which must stay in the staged copy.

    Args:
        settings: The settings whose source transformations name the bibliography.

    Returns:
        The project path of the bibliography to inline, or an empty set when none is
        selected.
    """
    name = settings.source_transforms.inline_bibliography
    return {name} if name else set()


def apply_contents(
    root: Path, contents: dict[str, bytes], *, originals: dict[str, bytes] | None = None
) -> None:
    """Write proposed file contents after validating every target and precondition.

    No file is written until every target has been validated.

    Args:
        root: The directory that holds the files.
        contents: The new bytes of each file, by path relative to ``root``.
        originals: The bytes each file had when its edit was planned; when given, a file
            whose bytes differ is not written.

    Raises:
        PreparationError: ``originals`` does not cover exactly the files of ``contents``, a
            target is not a regular file inside ``root``, or a file changed after its edit
            was planned.
    """
    targets: list[tuple[Path, bytes]] = []
    if originals is not None and set(contents) != set(originals):
        raise PreparationError(
            "Proposed contents lack the original bytes needed to confirm the files did not change"
        )
    for relative, data in sorted(contents.items()):
        cancellation_point()
        path = root / relative
        if not is_contained(root, path, regular_file=True):
            raise PreparationError(f"Invalid transformation path: {relative}")
        if originals is not None and path.read_bytes() != originals[relative]:
            raise PreparationError(f"Source changed after its edit was planned: {relative}")
        targets.append((path, data))
    for path, data in targets:
        cancellation_point()
        path.write_bytes(data)


class _StagedEdit(NamedTuple):
    """One planned transformation; ``apply`` runs only when its findings do not block.

    Attributes:
        changes: The changes the transformation proposes.
        findings: The findings of planning the transformation.
        apply: Writes the planned transformation into the staged copy.
    """

    changes: list[Change]
    findings: list[Finding]
    apply: Callable[[], None]


def _metadata_edit(prepared: Path, settings: Settings) -> _StagedEdit:
    """Plan the metadata sanitization of the staged copy.

    Args:
        prepared: The staged copy.
        settings: The settings whose metadata privacy options apply.

    Returns:
        The planned edit, which checks the original bytes before writing.
    """
    plan = plan_metadata_sanitization(prepared, settings.metadata_privacy)
    return _StagedEdit(
        plan.changes,
        plan.findings,
        lambda: apply_contents(prepared, plan.contents, originals=plan.originals),
    )


def _bibliography_edit(prepared: Path, main: str, settings: Settings) -> _StagedEdit:
    """Plan the bibliography transformation of the staged copy.

    Args:
        prepared: The staged copy.
        main: The project path of the main file.
        settings: The settings whose bibliography transformation options apply.

    Returns:
        The planned edit.
    """
    contents, changes, findings = plan_bibliography(prepared, main, settings.bibliography_transform)
    return _StagedEdit(changes, findings, lambda: apply_contents(prepared, contents))


def _doi_edit(prepared: Path, main: str) -> _StagedEdit:
    """Plan the DOI normalization of the staged copy's bibliography.

    Args:
        prepared: The staged copy.
        main: The project path of the main file.

    Returns:
        The planned edit.
    """
    contents, changes, findings = normalize_dois(prepared, main)
    return _StagedEdit(changes, findings, lambda: apply_contents(prepared, contents))


def _source_edit(prepared: Path, main: str, settings: Settings, retained: set[str]) -> _StagedEdit:
    """Plan the source transformations of the staged copy.

    Args:
        prepared: The staged copy.
        main: The project path of the main file.
        settings: The settings whose source transformation options apply.
        retained: The files shipped although the build does not read them; a merged input
            among them is not removed.

    Returns:
        The planned edit.
    """
    plan = plan_source_transforms(prepared, main, settings.source_transforms)

    def apply() -> None:
        """Write the planned contents and remove the merged inputs that are not retained.

        Raises:
            PreparationError: The removal plan lacks the original bytes of a removal, a file
                to remove is no longer a regular file inside the staged copy or changed after
                the removal was planned, or :func:`apply_contents` rejects the contents.
        """
        if set(plan.removal_originals) != plan.removals:
            raise PreparationError("Source removal plan lacks complete preconditions")
        for name, original in plan.removal_originals.items():
            candidate = prepared / name
            if (
                not is_contained(prepared, candidate, regular_file=True)
                or candidate.read_bytes() != original
            ):
                raise PreparationError("Source changed after removal was planned")
        apply_contents(prepared, plan.contents, originals=plan.originals)
        for name in sorted(plan.removals - retained):
            cancellation_point()
            (prepared / name).unlink()

    return _StagedEdit(plan.changes, plan.findings, apply)


def _staged_edits(
    prepared: Path, main: str, settings: Settings, retained: set[str]
) -> Iterator[_StagedEdit]:
    """Plan each selected edit just before it is applied: later plans read the earlier results.

    The order is metadata sanitization, the bibliography transformation, DOI
    normalization when ``settings.normalize_doi`` is set, then the source transformations.

    Args:
        prepared: The staged copy.
        main: The project path of the main file.
        settings: The settings that select and configure the edits.
        retained: The files shipped although the build does not read them.

    Yields:
        Each planned edit.
    """
    yield _metadata_edit(prepared, settings)
    yield _bibliography_edit(prepared, main, settings)
    if settings.normalize_doi:
        yield _doi_edit(prepared, main)
    yield _source_edit(prepared, main, settings, retained)


def _apply_edits(
    edits: Iterator[_StagedEdit], changes: list[Change], findings: list[Finding]
) -> bool:
    """Apply the edits in order, collecting their changes and findings.

    A blocked edit is reported but not applied, and later edits are not planned.

    Args:
        edits: The planned edits, planned lazily as the iterator advances.
        changes: The list that receives every reported edit's changes.
        findings: The list that receives every reported edit's findings.

    Returns:
        Whether all edits applied.
    """
    for edit in edits:
        findings.extend(edit.findings)
        changes.extend(edit.changes)
        if has_blockers(edit.findings):
            return False
        edit.apply()
    return True


@dataclass(frozen=True, slots=True)
class TransformOutcome:
    """The staged copy after the selected transformations, and what they changed or found.

    Attributes:
        prepared: The staged copy.
        changes: The cleanup exclusions and the changes of the reported edits.
        findings: The findings of the reported edits.
    """

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
    """Copy the snapshot to staging, remove debris and apply the selected edits in order.

    Debris is removed only when ``settings.cleanup`` is set. When every edit applies, the
    exclusions of retained files are dropped from the changes, since those files stay.

    Args:
        snapshot: The imported project, which is never modified.
        work: The job workspace; the staged copy is created as its ``prepared`` directory.
        main: The project path of the main file.
        settings: The settings that select the cleanup and the edits.
        dependencies: The inputs the build observed, which cleanup keeps.
        retained: The files shipped although the build does not read them, which cleanup
            and source transformations keep.

    Returns:
        The staged copy with the changes and findings of the transformations.
    """
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
    if _apply_edits(_staged_edits(prepared, main, settings, retained), changes, findings):
        # Every edit applied, including the source edit, which keeps retained files.
        changes = [
            change
            for change in changes
            if not (change.kind == "exclude" and change.path in retained)
        ]
    return TransformOutcome(prepared, changes, findings)


@dataclass(frozen=True, slots=True)
class FlattenOutcome:
    """The shipped sources, flattened when the flat layout is selected.

    Attributes:
        root: The directory that holds the shipped sources.
        main: The main file's path within ``root``.
        changes: The changes the flattening proposes.
        findings: The findings of planning the flattening.
        path_mapping: The flat name of each shipped file.
    """

    root: Path
    main: str
    changes: list[Change]
    findings: list[Finding]
    # Source-to-package names of a completed flattening; empty otherwise.
    path_mapping: dict[str, str]


def flatten_sources(shipment: Path, work: Path, main: str, settings: Settings) -> FlattenOutcome:
    """Move every shipped file to the top level when ``layout = "flat"``.

    A blocked flattening leaves the sources where they are and reports its findings.

    Args:
        shipment: The directory that holds the shipped sources.
        work: The job workspace; the flattened copy is created as its ``flat`` directory.
        main: The project path of the main file.
        settings: The settings that select the layout and the filename overrides.

    Returns:
        The flattened sources, or the unchanged ``shipment`` when the layout is not flat or
        the flattening is blocked.
    """
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
