"""Checks that make a ZIP archive safe to extract, before and while its entries are read.

``check_zip_directory`` bounds the central directory (entry count, size, offsets and
ZIP64 records) from the raw bytes, before ``zipfile`` allocates one object per entry,
and ``zip_entries`` then admits each entry only if its name is safe, it is a plain
file or folder, it is not encrypted and its sizes and compression ratio stay within
the import limits. Nothing is extracted here: writing the files is ``project``'s work,
and the limits themselves are ``import_budget``'s.
"""

from __future__ import annotations

import os
import stat
import struct
import zipfile
from dataclasses import dataclass
from typing import BinaryIO

from .errors import PreparationError
from .file_safety import byte_count_text
from .import_budget import ImportBudget, entry_limit_error
from .limits import MIB
from .project_names import PathRegistry, ProjectEntry, normalize_path

MAX_DIRECTORY_BYTES = 16 * MIB

_END_SIGNATURE = b"PK\x05\x06"
# signature, disk, directory disk, entries on this disk, total entries, size, offset, comment
_END_RECORD = struct.Struct("<4s4H2IH")
# The comment after the end record is at most 65535 bytes; the end record is found in that tail.
_MAX_COMMENT_BYTES = 0xFFFF
_END_SEARCH_BYTES = _END_RECORD.size + _MAX_COMMENT_BYTES

_ZIP64_LOCATOR_SIGNATURE = b"PK\x06\x07"
# signature, disk with the ZIP64 end record, its offset, total disks
_ZIP64_LOCATOR = struct.Struct("<4sIQI")
_ZIP64_END_SIGNATURE = b"PK\x06\x06"
# signature, record size, version made by, version needed, disk, directory disk,
# entries on this disk, total entries, directory size, directory offset
_ZIP64_END_RECORD = struct.Struct("<4sQ2H2I4Q")
# Bytes of a ZIP64 end record that precede the part its size field counts.
_ZIP64_RECORD_PREFIX_BYTES = 12
# The smallest value of the ZIP64 record size field: the fixed fields after the prefix.
_ZIP64_MIN_RECORD_SIZE = _ZIP64_END_RECORD.size - _ZIP64_RECORD_PREFIX_BYTES

_CENTRAL_SIGNATURE = b"PK\x01\x02"
_CENTRAL_HEADER_BYTES = 46
# The lengths of the name, the extra field and the comment, which follow the header.
_CENTRAL_LENGTHS = struct.Struct("<3H")
_CENTRAL_LENGTHS_OFFSET = 28

# General-purpose flag bits: bit 0 marks an encrypted entry, bit 6 strong encryption.
_ENCRYPTED_FLAGS = 0x01 | 0x40
# MS-DOS attribute bit that marks a directory in a ZIP entry's external attributes.
DOS_DIRECTORY_ATTRIBUTE = 0x10
_ALLOWED_COMPRESSION = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
# Unix file types an entry may declare; 0 means the archive recorded none.
_ALLOWED_FILE_TYPES = {0, stat.S_IFREG, stat.S_IFDIR}


@dataclass(frozen=True, slots=True)
class _DirectoryLocation:
    """Where the central directory lies, from the end record or its ZIP64 replacement."""

    disk: int
    directory_disk: int
    disk_entries: int
    total_entries: int
    directory_size: int
    directory_offset: int
    # Position at which the central directory must end: the start of the record after it.
    end: int


def check_zip_directory(stream: BinaryIO, budget: ImportBudget) -> None:
    """Bound the central directory before ZipFile loads it into memory.

    Raises ``PreparationError`` for a missing or inconsistent end record, a multi-disk
    or embedded archive, too many entries or too much metadata. Leaves ``stream`` at
    its start.
    """
    location = _locate_directory(stream)
    _check_directory_bounds(location, budget)
    _check_central_entries(stream, location, budget)
    stream.seek(0)


def _locate_directory(stream: BinaryIO) -> _DirectoryLocation:
    """Find the end record, and follow a ZIP64 locator to the record that replaces it."""
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    tail_size = min(size, _END_SEARCH_BYTES)
    stream.seek(size - tail_size)
    tail = stream.read(tail_size)
    end_index = tail.rfind(_END_SIGNATURE)
    if end_index < 0 or len(tail) - end_index < _END_RECORD.size:
        raise PreparationError("ZIP end-of-directory record is missing or truncated.")
    _, disk, directory_disk, disk_entries, total, directory_size, offset, comment_size = (
        _END_RECORD.unpack_from(tail, end_index)
    )
    if len(tail) - end_index != _END_RECORD.size + comment_size:
        raise PreparationError("ZIP comment or trailing data is malformed.")
    end = size - tail_size + end_index
    location = _DirectoryLocation(
        disk, directory_disk, disk_entries, total, directory_size, offset, end
    )
    return _follow_zip64_locator(stream, location)


def _follow_zip64_locator(stream: BinaryIO, location: _DirectoryLocation) -> _DirectoryLocation:
    """Replace the end record's values with the ZIP64 record's, when a locator announces one."""
    end = location.end
    if end < _ZIP64_LOCATOR.size:
        return location
    stream.seek(end - _ZIP64_LOCATOR.size)
    locator = stream.read(_ZIP64_LOCATOR.size)
    if not locator.startswith(_ZIP64_LOCATOR_SIGNATURE):
        return location
    _, locator_disk, record_offset, total_disks = _ZIP64_LOCATOR.unpack(locator)
    if locator_disk or total_disks != 1:
        raise PreparationError("Multi-disk ZIP archives are unsupported.")
    if record_offset > end - _ZIP64_END_RECORD.size - _ZIP64_LOCATOR.size:
        raise PreparationError("ZIP64 directory location is outside the archive.")
    stream.seek(record_offset)
    record = stream.read(_ZIP64_END_RECORD.size)
    if len(record) != _ZIP64_END_RECORD.size or not record.startswith(_ZIP64_END_SIGNATURE):
        raise PreparationError("ZIP64 directory record is malformed.")
    _, record_size, _, _, disk, directory_disk, disk_entries, total, size, offset = (
        _ZIP64_END_RECORD.unpack(record)
    )
    locator_start = end - _ZIP64_LOCATOR.size
    if (
        record_size < _ZIP64_MIN_RECORD_SIZE
        or record_offset + record_size + _ZIP64_RECORD_PREFIX_BYTES != locator_start
    ):
        raise PreparationError("ZIP64 directory offsets are inconsistent.")
    return _DirectoryLocation(
        disk, directory_disk, disk_entries, total, size, offset, record_offset
    )


def _check_directory_bounds(location: _DirectoryLocation, budget: ImportBudget) -> None:
    if location.disk or location.directory_disk or location.disk_entries != location.total_entries:
        raise PreparationError("Multi-disk ZIP archives are unsupported or inconsistent.")
    if location.total_entries > budget.limits.max_files:
        raise entry_limit_error(budget.limits.max_files)
    if location.directory_size > MAX_DIRECTORY_BYTES:
        raise PreparationError(
            "ZIP central-directory metadata exceeds the "
            f"{byte_count_text(MAX_DIRECTORY_BYTES)} limit. "
            "Create a smaller archive containing only the project files."
        )
    if location.directory_offset + location.directory_size != location.end:
        raise PreparationError(
            "ZIP directory offsets are invalid; embedded archives are unsupported."
        )


def _check_central_entries(
    stream: BinaryIO, location: _DirectoryLocation, budget: ImportBudget
) -> None:
    """Walk the central directory header by header and count what it actually holds."""
    stream.seek(location.directory_offset)
    remaining = location.directory_size
    actual_count = 0
    while remaining:
        budget.check_time()
        header = stream.read(_CENTRAL_HEADER_BYTES)
        if len(header) != _CENTRAL_HEADER_BYTES or not header.startswith(_CENTRAL_SIGNATURE):
            raise PreparationError("ZIP central directory is malformed.")
        variable = sum(_CENTRAL_LENGTHS.unpack_from(header, _CENTRAL_LENGTHS_OFFSET))
        entry_size = _CENTRAL_HEADER_BYTES + variable
        if entry_size > remaining:
            raise PreparationError("ZIP central directory is truncated.")
        actual_count += 1
        if actual_count > budget.limits.max_files:
            raise entry_limit_error(budget.limits.max_files)
        stream.seek(variable, os.SEEK_CUR)
        remaining -= entry_size
    if actual_count != location.total_entries:
        raise PreparationError("ZIP directory entry counts are inconsistent.")


def zip_entries(archive: zipfile.ZipFile, budget: ImportBudget) -> list[ProjectEntry]:
    """The archive's entries by normalized path, each checked against the import limits."""
    entries: list[ProjectEntry] = []
    registry = PathRegistry()
    declared_bytes = 0
    for info in archive.infolist():
        path = normalize_path(info.orig_filename)
        budget.add_entry(path)
        is_dir = _entry_is_directory(info, path)
        if info.flag_bits & _ENCRYPTED_FLAGS:
            raise PreparationError(f"Encrypted ZIP entries are unsupported: {path!r}.")
        if info.compress_type not in _ALLOWED_COMPRESSION:
            raise PreparationError(f"Unsupported ZIP compression for {path!r}.")
        registry.add(path, is_dir)
        declared_bytes += info.file_size
        if declared_bytes > budget.limits.max_bytes:
            raise PreparationError(
                f"ZIP exceeds the {byte_count_text(budget.limits.max_bytes)} size limit. "
                "Remove large files that are not needed from the archive and run again."
            )
        if info.file_size > max(info.compress_size, 1) * budget.limits.max_ratio:
            raise PreparationError(f"ZIP compression ratio exceeds the limit for {path!r}.")
        entries.append(ProjectEntry(path, is_dir, info.file_size, info))
    return sorted(entries, key=lambda entry: entry.path)


def _entry_is_directory(info: zipfile.ZipInfo, path: str) -> bool:
    """Whether the entry is a folder; links, special files and contradictions are refused."""
    file_type = stat.S_IFMT(info.external_attr >> 16)
    if file_type not in _ALLOWED_FILE_TYPES:
        raise PreparationError(
            f"ZIP links and special files are forbidden, but {path!r} is one. "
            "Create the archive again with the real files instead of links."
        )
    is_dir = (
        info.is_dir()
        or file_type == stat.S_IFDIR
        or bool(info.external_attr & DOS_DIRECTORY_ATTRIBUTE)
    )
    if is_dir and (file_type == stat.S_IFREG or info.file_size):
        raise PreparationError(f"Conflicting ZIP directory metadata: {path!r}.")
    return is_dir
