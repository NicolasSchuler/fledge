"""Reconcile selection evidence with observed package files and existing ZIP sizes.

An inventory answers, for every file of the imported project, whether it is in the
package and why. ``selection_inventory`` records one selection step's reasons. Each
later step (a transformation, flattening) may change what is selected, so
``assemble_package_inventory`` merges the steps in order, reads what the package really
holds (a folder, or the exact ZIP archive), and reconciles the two. Rows and the
inventory are typed here and become dictionaries only in ``to_report``, at the report
boundary.
"""

from __future__ import annotations

import zipfile
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Literal, NamedTuple, get_args

from .cancellation import cancellation_point
from .path_safety import is_contained, is_relative_posix_path
from .project_cleanup import cleanup_reason
from .report import Change

PackageStatus = Literal["proposed", "prepared", "verified", "incomplete"]
_PACKAGE_STATUSES = frozenset(get_args(PackageStatus))

UNUSED_REASON = "Not needed by the selected document or explicitly retained."


class Measurement(NamedTuple):
    """The size of one file in the package, and its compressed size when an archive was read.

    Attributes:
        size_bytes: The size of the file, in bytes.
        compressed_bytes: The compressed size in the archive, or ``None`` when no archive was
            read.
    """

    size_bytes: int
    compressed_bytes: int | None = None


@dataclass(slots=True)
class InventoryRow:
    """One source file's place in the package.

    Attributes:
        source: The original project-relative name.
        path: The name in the package, which a flattening may change.
        status: Whether the file is in the package.
        reasons: Why the file is included or excluded.
        size_bytes: The size of the file, or ``None`` when unknown.
        compressed_bytes: The compressed size in the archive, or ``None`` when unknown.
    """

    source: str
    path: str
    status: Literal["included", "excluded"]
    reasons: list[str] = field(default_factory=list)
    size_bytes: int | None = None
    compressed_bytes: int | None = None

    def to_report(self) -> dict[str, object]:
        """Return the row as a report dictionary."""
        return asdict(self)


@dataclass(frozen=True, slots=True)
class PackageInventory:
    """Every file of the project with its place in the package, and the archive's overhead.

    Attributes:
        document: The document the package is for, or ``None``.
        status: How far the package got: proposed, prepared, verified or incomplete.
        files: A row for every file, in the order of the source names.
        archive_bytes: The size of the exact archive, or ``None`` when no archive was read.
        overhead_bytes: The archive bytes beyond the compressed payload of its files, or
            ``None`` when no archive was read.
    """

    document: str | None
    status: PackageStatus
    files: list[InventoryRow]
    archive_bytes: int | None
    overhead_bytes: int | None

    def to_report(self) -> dict[str, object]:
        """Return the inventory as a report dictionary."""
        return {
            "document": self.document,
            "status": self.status,
            "files": [row.to_report() for row in self.files],
            "archive_bytes": self.archive_bytes,
            "overhead_bytes": self.overhead_bytes,
        }


@dataclass(frozen=True, slots=True)
class PackageObservation:
    """Where the package's final contents can be read.

    The archive takes precedence over the folder.

    Attributes:
        final_root: The folder that was packaged, or ``None``.
        archive: The exact ZIP, or ``None``.
        path_mapping: The successful flattening's source-to-package name map, or ``None``.
    """

    final_root: Path | None = None
    archive: Path | None = None
    path_mapping: Mapping[str, str] | None = None


class _Archive(NamedTuple):
    """What the ZIP central directory says about an archive.

    Attributes:
        files: The measurement of every file entry, by name.
        archive_bytes: The size of the archive.
        overhead_bytes: The archive bytes beyond the compressed payload of its files.
    """

    files: dict[str, Measurement]
    archive_bytes: int
    overhead_bytes: int


def _regular_file_size(root: Path | None, name: str) -> int | None:
    """Return the size of the regular file ``name`` under ``root``, if it is safe to read.

    Do not follow unsafe or unavailable source paths just to obtain a size.

    Args:
        root: The folder that holds the file, or ``None`` when there is none.
        name: The file, relative to ``root``.

    Returns:
        The size in bytes, or ``None`` when ``name`` is not a plain relative path, the file
        is not a regular file contained in ``root``, or it cannot be read.
    """
    if root is None or not is_relative_posix_path(name):
        return None
    candidate = root / name
    try:
        return (
            candidate.stat().st_size if is_contained(root, candidate, regular_file=True) else None
        )
    except OSError:
        return None


def selection_inventory(
    root: Path,
    *,
    files: Iterable[str],
    literal: set[str],
    observed: set[str],
    explicit: set[str],
    generated: set[str],
) -> list[InventoryRow]:
    """Describe a successful selection with independent, source-relative reasons.

    An omitted file is unused for this selection, not necessarily unused by
    another document or execution. Known debris is identified by the existing
    cleanup policy, with all selected inputs protected.

    Args:
        root: The project folder, which supplies the file sizes.
        files: Every project file.
        literal: The supported literal dependencies.
        observed: The recorded build inputs.
        explicit: The explicitly retained files.
        generated: The generated bibliography files the build consumed.

    Returns:
        A row for every file, in name order.
    """
    included = literal | observed | explicit | generated
    rows: list[InventoryRow] = []
    for name in sorted(files):
        cancellation_point()
        reasons = [
            reason
            for names, reason in (
                (literal, "Supported literal dependency."),
                (observed, "Recorded build input."),
                (explicit, "Explicitly retained file."),
                (generated, "Generated bibliography consumed by the build."),
            )
            if name in names
        ]
        if name not in included:
            reasons.append(cleanup_reason(name) or UNUSED_REASON)
        rows.append(
            InventoryRow(
                name,
                name,
                "included" if name in included else "excluded",
                reasons,
                _regular_file_size(root, name),
            )
        )
    return rows


def _merge_selections(selections: Iterable[Iterable[InventoryRow]]) -> dict[str, InventoryRow]:
    """Return the latest row of every source, from selections run from initial to transformed.

    The rows are copied: reconciling edits them. A later exclusion keeps why the original
    input was needed.
    """
    rows: dict[str, InventoryRow] = {}
    for selection in selections:
        for item in selection:
            cancellation_point()
            row = replace(item, reasons=list(item.reasons))
            previous = rows.get(row.source)
            if previous and row.status == "excluded":
                row.reasons = list(dict.fromkeys([*previous.reasons, *row.reasons]))
            rows[row.source] = row
    return rows


def _removal_reasons(
    changes: Iterable[Change], rows: dict[str, InventoryRow]
) -> dict[str, list[str]]:
    """Collect why each file was excluded or removed, keeping its explicit reasons.

    A file that only a change mentions, such as import cleanup of a file absent from
    the snapshot, is added to ``rows`` as excluded.

    Args:
        changes: The changes of the preparation; only exclusions and removals count.
        rows: The rows by source name, extended in place.

    Returns:
        The reasons of each excluded or removed file, by path.
    """
    removals: dict[str, list[str]] = {}
    for change in changes:
        if change.kind not in {"exclude", "remove"}:
            continue
        removals.setdefault(change.path, []).append(change.reason)
        rows.setdefault(change.path, InventoryRow(change.path, change.path, "excluded"))
    return removals


def _observe_archive(archive: Path) -> _Archive:
    """Read file sizes from the ZIP central directory, without recompression or a build.

    Overhead is total archive bytes minus the compressed payload of its files, including
    framing and any directory entries.

    Returns:
        The size of every file, the size of the archive, and its overhead.

    Raises:
        ValueError: The archive has duplicate file entries.
    """
    files: dict[str, Measurement] = {}
    with zipfile.ZipFile(archive) as zipped:
        for info in zipped.infolist():
            cancellation_point()
            if info.is_dir():
                continue
            if info.filename in files:
                raise ValueError("Package archive has duplicate file entries.")
            files[info.filename] = Measurement(info.file_size, info.compress_size)
    archive_bytes = archive.stat().st_size
    payload = sum(measurement.compressed_bytes or 0 for measurement in files.values())
    return _Archive(files, archive_bytes, archive_bytes - payload)


def _observe_directory(final_root: Path) -> dict[str, Measurement]:
    """Return the size of every regular file in the folder that was packaged."""
    files: dict[str, Measurement] = {}
    for path in final_root.rglob("*"):
        cancellation_point()
        name = path.relative_to(final_root).as_posix()
        size = _regular_file_size(final_root, name)
        if size is not None:
            files[name] = Measurement(size)
    return files


def _reconcile(
    rows: dict[str, InventoryRow],
    actual: dict[str, Measurement] | None,
    mapping: Mapping[str, str],
    removals: dict[str, list[str]],
    source_root: Path | None,
) -> None:
    """Set each row's package path, status, sizes and reasons from what the package holds.

    Without ``actual`` the rows describe the latest successful selection, and removals of
    retained inputs are labelled as planned.

    Args:
        rows: The rows by source name, updated in place; a package file without a row
            gets one.
        actual: What the package holds, by package name, or ``None`` when it was not
            observed.
        mapping: The source-to-package names of a flattening.
        removals: The reasons of each excluded or removed file, by source name.
        source_root: The project folder that fills missing sizes, or ``None``.
    """
    inverse = {destination: source for source, destination in mapping.items()}
    if actual is not None:
        for name in actual:
            source = inverse.get(name, name)
            rows.setdefault(
                source,
                InventoryRow(
                    source,
                    name,
                    "included",
                    ["Present in the prepared package; selection reason unavailable."],
                ),
            )
    for source, row in rows.items():
        cancellation_point()
        row.path = mapping.get(source, row.path)
        if actual is not None:
            # An excluded original file can share the basename now assigned to
            # another selected input. Its identity does not change with layout.
            owner = inverse.get(row.path, source)
            measurement = actual.get(row.path) if owner == source else None
            row.status = "included" if measurement is not None else "excluded"
            if measurement is not None:
                row.size_bytes, row.compressed_bytes = measurement
            else:
                row.reasons.extend(removals.get(source, ["Not present in the prepared package."]))
        elif source in removals:
            row.reasons.extend(
                [f"Planned removal: {reason}" for reason in removals[source]]
                if row.status == "included"
                else removals[source]
            )
        if row.size_bytes is None:
            row.size_bytes = _regular_file_size(source_root, source)
        row.reasons = list(dict.fromkeys(row.reasons))


def assemble_package_inventory(
    *,
    selections: Iterable[Iterable[InventoryRow]] = (),
    changes: Iterable[Change] = (),
    source_root: Path | None = None,
    observation: PackageObservation | None = None,
    document: str | None = None,
    status: PackageStatus = "proposed",
) -> PackageInventory:
    """Combine successive selection inventories with the actual package contents.

    Without final observations the rows describe the latest successful selection,
    and removal changes on retained inputs are labelled as planned. They are not
    an exact prediction of transformed or flattened contents.

    The ZIP central directory supplies raw and compressed file sizes without
    recompression or a build.

    Args:
        selections: The selection inventories, from initial to transformed, with original
            project-relative names.
        changes: The changes of the preparation. Exclusion/removal changes retain their
            explicit reasons, including import cleanup for files absent from the snapshot.
        source_root: The project folder. It only fills missing source sizes and is never
            enumerated, so excluded metadata is not traversed.
        observation: Where the final contents can be read; its ``path_mapping`` is the
            successful flattening source-to-package map.
        document: The document the package is for.
        status: The status of the package. ``incomplete`` can describe any interrupted
            stage. Prepared status requires final observations; verified status
            additionally requires the exact archive, with verification success supplied
            by the caller rather than inferred from the ZIP's existence.

    Returns:
        The inventory, with a row for every file in the order of the source names.

    Raises:
        ValueError: ``status`` is unknown or lacks the observations it requires, the path
            mapping has colliding destinations, or the archive has duplicate file entries.
    """
    observation = observation or PackageObservation()
    archive, final_root = observation.archive, observation.final_root
    if status not in _PACKAGE_STATUSES:
        raise ValueError(f"Unknown package inventory status: {status}")
    if status == "verified" and archive is None:
        raise ValueError("A verified inventory requires the exact archive.")
    if status == "prepared" and final_root is None and archive is None:
        raise ValueError("A prepared inventory requires observed final files.")
    mapping = dict(observation.path_mapping or {})
    if len(set(mapping.values())) != len(mapping):
        raise ValueError("Package path mapping has colliding destinations.")

    rows = _merge_selections(selections)
    removals = _removal_reasons(changes, rows)
    actual: dict[str, Measurement] | None = None
    archive_bytes = overhead_bytes = None
    if archive is not None:
        observed = _observe_archive(archive)
        actual, archive_bytes, overhead_bytes = observed
    elif final_root is not None:
        actual = _observe_directory(final_root)
    _reconcile(rows, actual, mapping, removals, source_root)
    return PackageInventory(
        document,
        status,
        [rows[source] for source in sorted(rows)],
        archive_bytes,
        overhead_bytes,
    )
