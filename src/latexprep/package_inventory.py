"""Reconcile selection evidence with observed package files and existing ZIP sizes."""

from __future__ import annotations

import zipfile
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from .cancellation import cancellation_point
from .file_safety import is_contained_regular_file, is_plain_relative_name
from .project import cleanup_reason
from .report import Change

PackageStatus = Literal["proposed", "prepared", "verified", "incomplete"]

UNUSED_REASON = "Not needed by the selected document or explicitly retained."


@dataclass(slots=True)
class _InventoryRow:
    """One source file's place in the package; reports receive it as a dictionary."""

    source: str
    path: str
    status: Literal["included", "excluded"]
    reasons: list[str] = field(default_factory=list)
    size_bytes: int | None = None
    compressed_bytes: int | None = None

    @classmethod
    def from_report(cls, row: Mapping[str, Any]) -> _InventoryRow:
        return cls(
            row["source"],
            row["path"],
            row["status"],
            list(row["reasons"]),
            row.get("size_bytes"),
        )


def _regular_file_size(root: Path | None, name: str) -> int | None:
    """Do not follow unsafe or unavailable source paths just to obtain a size."""
    if root is None or not is_plain_relative_name(name):
        return None
    candidate = root / name
    try:
        return candidate.stat().st_size if is_contained_regular_file(root, candidate) else None
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
) -> list[dict]:
    """Describe a successful selection with independent, source-relative reasons.

    An omitted file is unused for this selection, not necessarily unused by
    another document or execution. Known debris is identified by the existing
    cleanup policy, with all selected inputs protected.
    """
    included = literal | observed | explicit | generated
    rows: list[dict] = []
    for name in sorted(files):
        cancellation_point()
        reasons = []
        for names, reason in (
            (literal, "Supported literal dependency."),
            (observed, "Recorded build input."),
            (explicit, "Explicitly retained file."),
            (generated, "Generated bibliography consumed by the build."),
        ):
            if name in names:
                reasons.append(reason)
        if name not in included:
            reasons.append(cleanup_reason(name) or UNUSED_REASON)
        row = _InventoryRow(
            name,
            name,
            "included" if name in included else "excluded",
            reasons,
            _regular_file_size(root, name),
        )
        rows.append(asdict(row))
    return rows


def assemble_package_inventory(
    *,
    selections: Iterable[Iterable[dict]] = (),
    changes: Iterable[Change] = (),
    source_root: Path | None = None,
    final_root: Path | None = None,
    path_mapping: Mapping[str, str] | None = None,
    archive: Path | None = None,
    document: str | None = None,
    status: PackageStatus = "proposed",
) -> dict:
    """Combine successive selection inventories with the actual package contents.

    Selections run from initial to transformed and use original project-relative
    names. ``path_mapping`` is the successful flattening source-to-package map.
    Exclusion/removal changes retain their explicit reasons, including import
    cleanup for files absent from the snapshot. ``source_root`` only fills missing
    source sizes and is never enumerated, so excluded metadata is not traversed.

    Without final observations the rows describe the latest successful selection,
    and removal changes on retained inputs are labelled as planned. They are not
    an exact prediction of transformed or flattened contents. ``incomplete`` can
    describe any interrupted stage. Prepared status requires final observations;
    verified status additionally requires the exact archive, with verification
    success supplied by the caller rather than inferred from the ZIP's existence.

    The ZIP central directory supplies raw and compressed file sizes without
    recompression or a build. Overhead is total archive bytes minus the compressed
    payload of its files, including framing and any directory entries.
    """
    if status not in {"proposed", "prepared", "verified", "incomplete"}:
        raise ValueError(f"Unknown package inventory status: {status}")
    if status == "verified" and archive is None:
        raise ValueError("A verified inventory requires the exact archive.")
    if status == "prepared" and final_root is None and archive is None:
        raise ValueError("A prepared inventory requires observed final files.")

    rows: dict[str, _InventoryRow] = {}
    for selection in selections:
        for item in selection:
            cancellation_point()
            row = _InventoryRow.from_report(item)
            previous = rows.get(row.source)
            # A later exclusion should retain why the original input was needed.
            if previous and row.status == "excluded":
                row.reasons = list(dict.fromkeys([*previous.reasons, *row.reasons]))
            rows[row.source] = row

    removals: dict[str, list[str]] = {}
    for change in changes:
        if change.kind not in {"exclude", "remove"}:
            continue
        removals.setdefault(change.path, []).append(change.reason)
        rows.setdefault(change.path, _InventoryRow(change.path, change.path, "excluded"))

    mapping = dict(path_mapping or {})
    inverse_mapping = {destination: source for source, destination in mapping.items()}
    if len(inverse_mapping) != len(mapping):
        raise ValueError("Package path mapping has colliding destinations.")
    actual: dict[str, tuple[int, int | None]] | None = None
    archive_bytes = None
    overhead_bytes = None
    if archive is not None:
        with zipfile.ZipFile(archive) as zipped:
            actual = {}
            for info in zipped.infolist():
                cancellation_point()
                if info.is_dir():
                    continue
                if info.filename in actual:
                    raise ValueError("Package archive has duplicate file entries.")
                actual[info.filename] = (info.file_size, info.compress_size)
        archive_bytes = archive.stat().st_size
        overhead_bytes = archive_bytes - sum(compressed or 0 for _, compressed in actual.values())
    elif final_root is not None:
        actual = {}
        for path in final_root.rglob("*"):
            cancellation_point()
            name = path.relative_to(final_root).as_posix()
            size = _regular_file_size(final_root, name)
            if size is not None:
                actual[name] = (size, None)

    if actual is not None:
        for name in actual:
            source = inverse_mapping.get(name, name)
            rows.setdefault(
                source,
                _InventoryRow(
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
            owner = inverse_mapping.get(row.path, source)
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

    return {
        "document": document,
        "status": status,
        "files": [asdict(rows[source]) for source in sorted(rows)],
        "archive_bytes": archive_bytes,
        "overhead_bytes": overhead_bytes,
    }
