"""Explicit literal metadata edit proposals and bounded image metadata privacy scans."""

from __future__ import annotations

import difflib
import html
import re
import struct
import unicodedata
import zlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .models import Change, Finding, PreparationError, has_blockers
from .scheduler import cancellation_point
from .source import (
    MAX_COMMANDS,
    MAX_SOURCE_BYTES,
    MAX_TOTAL_SOURCE_BYTES,
    _commands,
    _group,
    _mask,
    _ParseLimit,
    conditional_spans,
    in_conditional,
)
from .submission_checks import _inventory, _read

_PDF_FIELDS = {
    "hypersetup.pdftitle": "Title",
    "hypersetup.pdfauthor": "Author",
    "hypersetup.pdfsubject": "Subject",
    "hypersetup.pdfkeywords": "Keywords",
}
_FIELDS = {*_PDF_FIELDS, "title", "author", "email", "affiliation"}
_MAX_IMAGE_BYTES = 32 * 1024 * 1024
_MAX_IMAGE_TOTAL = 128 * 1024 * 1024
_MAX_METADATA_BYTES = 1024 * 1024
_MAX_METADATA_TOTAL = 4 * 1024 * 1024
_MAX_IMAGE_COUNT = 2000
_MAX_ENTRIES = 4096
_MAX_MATCHES = 200


def _relative(value: str) -> bool:
    return (
        bool(value)
        and not value.startswith("/")
        and not any(char in value for char in "\\:")
        and all(part not in {"", ".", ".."} for part in value.split("/"))
        and not any(unicodedata.category(char).startswith("C") for char in value)
    )


def _literal(value: str) -> bool:
    return not any(char in value for char in "\\{}%#$&^~") and not any(
        unicodedata.category(char).startswith("C") and char not in "\r\n\t" for char in value
    )


@dataclass(frozen=True)
class MetadataEdit:
    path: str
    field: str
    expected_before: str
    replacement: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.path, str)
            or not _relative(self.path)
            or (PurePosixPath(self.path).suffix.lower() not in {".tex", ".ltx", ".latex"})
        ):
            raise PreparationError("Metadata edits require a safe relative TeX manuscript path")
        if not isinstance(self.field, str) or self.field not in _FIELDS:
            raise PreparationError("Metadata edit field is outside the supported literal subset")
        for name in ("expected_before", "replacement"):
            value = getattr(self, name)
            if not isinstance(value, str) or len(value) > 65536 or not _literal(value):
                raise PreparationError(f"Metadata {name} requires bounded plain literal text")


@dataclass(frozen=True)
class MetadataPrivacyOptions:
    edits: tuple[MetadataEdit, ...] = ()
    image_identity_terms: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.edits, tuple)
            or len(self.edits) > 256
            or any(not isinstance(edit, MetadataEdit) for edit in self.edits)
        ):
            raise PreparationError("Metadata edits must be an immutable bounded list")
        if len({(edit.path, edit.field) for edit in self.edits}) != len(self.edits):
            raise PreparationError("Each source metadata field may have only one selected edit")
        values = self.image_identity_terms
        if (
            not isinstance(values, tuple)
            or len(values) > 256
            or any(
                not isinstance(value, str) or not value.strip() or len(value) > 1024
                for value in values
            )
            or len(set(values)) != len(values)
        ):
            raise PreparationError("Image identity terms must be unique nonempty bounded strings")
        expected: dict[str, str] = {}
        for edit in self.edits:
            if edit.field in _PDF_FIELDS:
                if edit.field in expected and expected[edit.field] != edit.replacement:
                    raise PreparationError("Selected edits disagree on one final PDF property")
                expected[edit.field] = edit.replacement


@dataclass
class MetadataSanitizationPlan:
    contents: dict[str, bytes] = field(default_factory=dict)
    originals: dict[str, bytes] = field(default_factory=dict)
    changes: list[Change] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)


def _hypersetup_values(text: str, start: int, end: int) -> list[tuple[str, int, int]]:
    """Locate plain keys with braced or unbraced literal values, retaining source offsets."""
    result = []
    cursor = start
    while cursor < end:
        while cursor < end and (text[cursor].isspace() or text[cursor] == ","):
            cursor += 1
        if cursor == end:
            break
        match = re.match(r"([A-Za-z]+)\s*=\s*", text[cursor:end])
        if match is None:
            raise PreparationError("Unsupported hypersetup key/value syntax")
        key = match[1]
        cursor += match.end()
        if cursor < end and text[cursor] == "{":
            value = _group(text, cursor)
            if value is None or value.end > end:
                raise PreparationError("Unclosed hypersetup value")
            result.append((key, value.start, value.end))
            cursor = value.end + 1
            while cursor < end and text[cursor].isspace():
                cursor += 1
            if cursor < end and text[cursor] != ",":
                raise PreparationError("Ambiguous hypersetup value suffix")
        else:
            stop = text.find(",", cursor, end)
            stop = end if stop < 0 else stop
            trimmed = stop
            while trimmed > cursor and text[trimmed - 1].isspace():
                trimmed -= 1
            result.append((key, cursor, trimmed))
            cursor = stop
    return result


def _edit_spans(text: str, edits: Sequence[MetadataEdit]) -> list[tuple[int, int, str]]:
    masked, incomplete = _mask(text)
    if incomplete:
        raise PreparationError("An unclosed literal region prevents metadata editing")
    try:
        commands = _commands(masked)
    except _ParseLimit:
        raise PreparationError("Source exceeds supported metadata parsing limits") from None
    if len(commands) >= MAX_COMMANDS:
        raise PreparationError("Source exceeds supported metadata command limits")
    if any(
        command.name
        in {
            "catcode",
            "csname",
            "scantokens",
            "ExplSyntaxOn",
            "directlua",
        }
        for command in commands
    ):
        raise PreparationError("Dynamic source syntax prevents a literal metadata proposal")
    # Only a conditional that encloses a selected declaration makes its old value
    # unestablished; an unrelated \if…\fi elsewhere in the file does not.
    conditionals = conditional_spans(commands)
    targets: dict[str, list[tuple[int, int]]] = {edit.field: [] for edit in edits}
    for command in commands:
        if command.name == "endinput" or (
            command.name == "end"
            and command.arguments
            and command.arguments[0].value.strip() == "document"
        ):
            if command.depth:
                raise PreparationError("Grouped document termination prevents metadata editing")
            break
        selected = command.name in targets or (
            command.name == "hypersetup" and any(key.startswith("hypersetup.") for key in targets)
        )
        if not selected:
            continue
        if in_conditional(conditionals, command.start):
            raise PreparationError(
                "Selected metadata is enclosed by conditional source control flow"
            )
        if (
            command.depth
            or len(command.arguments) != 1
            or command.options
            or text.startswith("\\" + command.name + "*", command.start)
        ):
            raise PreparationError(
                "Selected metadata is grouped, generated or has unsupported syntax"
            )
        argument = command.arguments[0]
        if command.name == "hypersetup":
            for key, start, end in _hypersetup_values(text, argument.start, argument.end):
                name = "hypersetup." + key
                if name in targets:
                    targets[name].append((start, end))
        else:
            targets[command.name].append((argument.start, argument.end))
    replacements = []
    for edit in edits:
        spans = targets[edit.field]
        if len(spans) != 1:
            raise PreparationError("Selected metadata field is missing or not a unique declaration")
        start, end = spans[0]
        actual = text[start:end]
        if not _literal(actual) or actual != edit.expected_before:
            raise PreparationError(
                "Selected metadata does not match its expected literal old value"
            )
        replacement = edit.replacement
        if edit.field.startswith("hypersetup.") and text[start - 1 : start] != "{":
            replacement = "{" + replacement + "}"
        replacements.append((start, end, replacement))
    return replacements


def plan_metadata_sanitization(
    root: Path, options: MetadataPrivacyOptions
) -> MetadataSanitizationPlan:
    """Return all-or-nothing source proposals; never modify the source directory."""
    plan = MetadataSanitizationPlan()
    if not options.edits:
        return plan
    files, issues = _inventory(root)
    if issues:
        plan.findings.append(
            Finding(
                "privacy.metadata_sanitization",
                "Source inventory is incomplete; no metadata changes are proposed.",
                "error",
                "inconclusive",
            )
        )
        return plan
    total = 0
    for name in sorted({edit.path for edit in options.edits}):
        cancellation_point()
        selected = [edit for edit in options.edits if edit.path == name]
        try:
            if name not in files:
                raise PreparationError("Explicit metadata source file is absent")
            original = _read(files[name], MAX_SOURCE_BYTES)
            total += len(original)
            if total > MAX_TOTAL_SOURCE_BYTES:
                raise PreparationError("Metadata sources exceed the total source-byte limit")
            bom = original.startswith(b"\xef\xbb\xbf")
            text = original.decode("utf-8-sig")
            spans = _edit_spans(text, selected)
            prepared = text
            for start, end, value in sorted(spans, reverse=True):
                prepared = prepared[:start] + value + prepared[end:]
            if prepared != text:
                plan.originals[name] = original
                plan.contents[name] = (b"\xef\xbb\xbf" if bom else b"") + prepared.encode("utf-8")
                plan.changes.append(
                    Change(
                        name,
                        "metadata sanitization",
                        "Explicitly selected "
                        "literal metadata replacements; final PDF recheck required.",
                        diff="".join(
                            difflib.unified_diff(
                                text.splitlines(keepends=True),
                                prepared.splitlines(keepends=True),
                                fromfile=name,
                                tofile=name,
                            )
                        ),
                    )
                )
            plan.findings.append(
                Finding(
                    "privacy.metadata_sanitization",
                    "Selected literal metadata matched its old-value "
                    "preconditions; final rebuilt PDF metadata still requires verification.",
                    "info",
                    "passed",
                    path=name,
                    details={
                        "fields": [edit.field for edit in selected],
                        "final_pdf_recheck_required": True,
                    },
                )
            )
        except (OSError, UnicodeError, PreparationError):
            plan.findings.append(
                Finding(
                    "privacy.metadata_sanitization",
                    "Metadata edit cannot be proposed: the file, "
                    "unique literal field or exact expected-before value is unavailable "
                    "or unsupported.",
                    "error",
                    "inconclusive",
                    path=name,
                    details={"fields": [edit.field for edit in selected]},
                )
            )
    if has_blockers(plan.findings):
        plan.contents.clear()
        plan.originals.clear()
        plan.changes.clear()
    return plan


def _normalized(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def check_sanitized_pdf_metadata(
    pdf_findings: Sequence[Finding], options: MetadataPrivacyOptions
) -> list[Finding]:
    """Recheck freshly inspected PDF properties; never treat a source proposal as proof."""
    if not options.edits:
        return []
    inventories = [
        finding.details.get("properties")
        for finding in pdf_findings
        if finding.rule == "pdf.metadata" and finding.status == "passed"
    ]
    if (
        len(inventories) != 1
        or not isinstance(inventories[0], dict)
        or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in inventories[0].items()
        )
    ):
        return [
            Finding(
                "privacy.sanitized_pdf_metadata",
                "Fresh final PDF property evidence is "
                "missing or ambiguous after the selected source edits.",
                "error",
                "inconclusive",
            )
        ]
    properties = {key.casefold(): value for key, value in inventories[0].items()}
    if len(properties) != len(inventories[0]) or not re.fullmatch(
        r"[1-9][0-9]*", properties.get("pages", "")
    ):
        return [
            Finding(
                "privacy.sanitized_pdf_metadata",
                "Final PDF properties have ambiguous names or lack document page-count evidence.",
                "error",
                "inconclusive",
            )
        ]
    violations = []
    for index, edit in enumerate(options.edits, 1):
        if edit.field in _PDF_FIELDS:
            key = _PDF_FIELDS[edit.field].casefold()
            if " ".join(properties.get(key, "").split()) != " ".join(edit.replacement.split()):
                violations.append(
                    {
                        "edit_index": index,
                        "property": _PDF_FIELDS[edit.field],
                        "reason": "Final value differs from the selected replacement",
                    }
                )
        old = _normalized(edit.expected_before)
        if old and old not in _normalized(edit.replacement):
            for key, value in properties.items():
                if old in _normalized(value):
                    violations.append(
                        {
                            "edit_index": index,
                            "property": key,
                            "reason": "Removed literal value remains in final metadata",
                        }
                    )
    return [
        Finding(
            "privacy.sanitized_pdf_metadata",
            "Checked selected metadata replacements and removed "
            "literal values in final reported PDF properties.",
            "error" if violations else "info",
            "failed" if violations else "passed",
            details={
                "violations": violations,
                "scope": "Reported top-level PDF properties only; hidden objects, image pixels and "
                "semantic anonymity remain unverified.",
            },
        )
    ]


def _inflate(data: bytes) -> bytes:
    decoder = zlib.decompressobj()
    result = decoder.decompress(data, _MAX_METADATA_BYTES + 1)
    if len(result) > _MAX_METADATA_BYTES or not decoder.eof or decoder.unused_data:
        raise PreparationError("Compressed metadata is incomplete or exceeds its decoded limit")
    return result


@dataclass
class _MetadataText:
    entries: list[tuple[str, str]] = field(default_factory=list)
    # Blocks deliberately not inspected. These bound the scope of a negative
    # result; unlike an error they do not make the surrounding scan unusable.
    limitations: list[str] = field(default_factory=list)
    size: int = 0

    def append(self, item: tuple[str, str]) -> None:
        self.size += len(item[1].encode("utf-8"))
        if self.size > _MAX_METADATA_TOTAL or len(self.entries) >= _MAX_ENTRIES:
            raise PreparationError("Decoded metadata exceeds the cumulative extraction limit")
        self.entries.append(item)

    def extend(self, items: Sequence[tuple[str, str]]) -> None:
        for item in items:
            self.append(item)

    def limit(self, reason: str) -> None:
        if reason not in self.limitations:
            if len(self.limitations) >= _MAX_ENTRIES:
                raise PreparationError("Skipped metadata blocks exceed the reporting limit")
            self.limitations.append(reason)


def _exif(data: bytes, result: _MetadataText) -> None:
    if len(data) < 8 or data[:2] not in {b"II", b"MM"}:
        raise PreparationError("Unsupported EXIF byte order")
    order = "<" if data[:2] == b"II" else ">"

    def integer(offset: int, size: int) -> int:
        if offset < 0 or offset + size > len(data):
            raise PreparationError("EXIF offset is outside the metadata block")
        return int.from_bytes(data[offset : offset + size], "little" if order == "<" else "big")

    if integer(2, 2) != 42:
        raise PreparationError("Unsupported EXIF TIFF header")
    pending = [integer(4, 4)]
    seen: set[int] = set()
    total = 0
    sizes = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8}
    while pending:
        offset = pending.pop()
        if not offset:
            continue
        if offset in seen:
            raise PreparationError("Cyclic EXIF directory references")
        seen.add(offset)
        count = integer(offset, 2)
        total += count
        if total > _MAX_ENTRIES:
            raise PreparationError("EXIF directory entry limit exceeded")
        for index in range(count):
            entry = offset + 2 + 12 * index
            tag, kind, count = integer(entry, 2), integer(entry + 2, 2), integer(entry + 4, 4)
            if kind not in sizes or count * sizes[kind] > _MAX_METADATA_BYTES:
                raise PreparationError("Unsupported or oversized EXIF field")
            length = count * sizes[kind]
            start = entry + 8 if length <= 4 else integer(entry + 8, 4)
            if start + length > len(data):
                raise PreparationError("EXIF value extends outside its metadata block")
            value = data[start : start + length]
            if tag in {0x8769, 0x8825, 0xA005}:
                if kind != 4 or count != 1:
                    raise PreparationError("Unsupported EXIF subdirectory pointer")
                pending.append(integer(entry + 8, 4))
            elif tag == 0x927C:
                # MakerNote holds an undocumented vendor structure whose own byte
                # order and offsets are not portable. Skipping it keeps the rest of
                # an ordinary camera image's metadata determinable.
                result.limit("EXIF MakerNote (vendor binary)")
            elif kind == 2:
                result.append((f"EXIF tag {tag:#06x}", value.rstrip(b"\0").decode("ascii")))
            elif tag == 0x9286 and kind == 7:
                if value.startswith(b"ASCII\0\0\0"):
                    decoded = value[8:].rstrip(b"\0").decode("ascii")
                elif value.startswith(b"UNICODE\0"):
                    raw = value[8:]
                    encoding = (
                        "utf-16"
                        if raw[:2] in {b"\xff\xfe", b"\xfe\xff"}
                        else ("utf-16-le" if order == "<" else "utf-16-be")
                    )
                    decoded = raw.decode(encoding).rstrip("\0")
                else:
                    raise PreparationError("Unsupported EXIF UserComment encoding")
                result.append(("EXIF UserComment", decoded))
            elif 0x9C9B <= tag <= 0x9C9F and kind == 1:
                result.append(
                    (f"EXIF Windows tag {tag:#06x}", value.decode("utf-16-le").rstrip("\0"))
                )
            elif tag == 700 and kind in {1, 7}:
                result.append(("EXIF XMP", value.rstrip(b"\0").decode("utf-8")))
        pending.append(integer(offset + 2 + 12 * integer(offset, 2), 4))


def _iptc(data: bytes, result: _MetadataText) -> None:
    """Record simple IPTC IIM application-record text datasets."""
    cursor = 0
    datasets = 0
    while cursor < len(data):
        datasets += 1
        if datasets > _MAX_ENTRIES:
            raise PreparationError("JPEG IPTC dataset limit exceeded")
        if data[cursor] != 0x1C or cursor + 5 > len(data):
            result.limit("JPEG IPTC dataset outside the supported IIM layout")
            return
        record, dataset = data[cursor + 1], data[cursor + 2]
        length = int.from_bytes(data[cursor + 3 : cursor + 5], "big")
        cursor += 5
        if length & 0x8000:
            result.limit("JPEG IPTC extended dataset length")
            return
        if cursor + length > len(data):
            result.limit("truncated JPEG IPTC dataset")
            return
        value = data[cursor : cursor + length]
        cursor += length
        # Record 2 is the application record. Dataset 0 is its binary version
        # number and other records hold envelope or object-data bytes.
        if record != 2 or dataset == 0:
            continue
        try:
            result.append((f"JPEG IPTC 2:{dataset:03d}", value.decode("utf-8")))
        except UnicodeDecodeError:
            result.limit("JPEG IPTC dataset with an undeclared text encoding")


def _photoshop(contents: bytes, result: _MetadataText) -> None:
    """Walk 8BIM image resources and inspect only the IPTC-NAA resource."""
    header = b"Photoshop 3.0\0"
    if not contents.startswith(header):
        result.limit("JPEG APP13 block outside the supported Photoshop layout")
        return
    cursor = len(header)
    blocks = 0
    while cursor < len(contents):
        blocks += 1
        if blocks > _MAX_ENTRIES:
            raise PreparationError("JPEG APP13 resource limit exceeded")
        if contents[cursor : cursor + 4] != b"8BIM" or cursor + 7 > len(contents):
            result.limit("JPEG APP13 block outside the supported Photoshop layout")
            return
        identifier = int.from_bytes(contents[cursor + 4 : cursor + 6], "big")
        # The resource name is a Pascal string padded to an even total length.
        name = 1 + contents[cursor + 6]
        cursor += 6 + name + name % 2
        if cursor + 4 > len(contents):
            result.limit("truncated JPEG APP13 resource")
            return
        size = int.from_bytes(contents[cursor : cursor + 4], "big")
        cursor += 4
        if size > _MAX_METADATA_BYTES or cursor + size > len(contents):
            result.limit("oversized or truncated JPEG APP13 resource")
            return
        block = contents[cursor : cursor + size]
        cursor += size + size % 2
        if identifier == 0x0404:
            _iptc(block, result)


def _png_metadata(data: bytes) -> _MetadataText:
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise PreparationError("PNG signature is invalid")
    result = _MetadataText()
    cursor = 8
    chunks = 0
    while cursor < len(data):
        chunks += 1
        if chunks > _MAX_ENTRIES or cursor + 12 > len(data):
            raise PreparationError("PNG chunk structure is incomplete or oversized")
        length = int.from_bytes(data[cursor : cursor + 4], "big")
        kind = data[cursor + 4 : cursor + 8]
        end = cursor + 8 + length
        if end + 4 > len(data):
            raise PreparationError("PNG chunk exceeds its file")
        contents = data[cursor + 8 : end]
        if zlib.crc32(kind + contents) != int.from_bytes(data[end : end + 4], "big"):
            raise PreparationError("PNG chunk checksum is invalid")
        if kind in {b"tEXt", b"zTXt", b"iTXt", b"eXIf"}:
            if len(contents) > _MAX_METADATA_BYTES:
                raise PreparationError("PNG metadata block exceeds the size limit")
            if kind == b"eXIf":
                _exif(contents, result)
            elif kind == b"tEXt":
                key, value = contents.split(b"\0", 1)
                result.append(("PNG tEXt", (key + b" " + value).decode("latin-1")))
            elif kind == b"zTXt":
                key, compressed = contents.split(b"\0", 1)
                if not compressed or compressed[0] != 0:
                    raise PreparationError("Unsupported PNG text compression")
                result.append(
                    ("PNG zTXt", (key + b" " + _inflate(compressed[1:])).decode("latin-1"))
                )
            else:
                key, remaining = contents.split(b"\0", 1)
                if len(remaining) < 4 or remaining[0] not in {0, 1} or remaining[1] != 0:
                    raise PreparationError("Unsupported PNG international text compression")
                language, translated, value = remaining[2:].split(b"\0", 2)
                if remaining[0]:
                    value = _inflate(value)
                result.append(
                    (
                        "PNG iTXt",
                        key.decode("latin-1")
                        + " "
                        + language.decode("ascii")
                        + " "
                        + translated.decode("utf-8")
                        + " "
                        + value.decode("utf-8"),
                    )
                )
        elif kind == b"IEND":
            if length or end + 4 != len(data):
                raise PreparationError("PNG has malformed termination or uninspected trailing data")
            return result
        cursor = end + 4
    raise PreparationError("PNG termination was not found")


def _jpeg_metadata(data: bytes) -> _MetadataText:
    if not data.startswith(b"\xff\xd8"):
        raise PreparationError("JPEG signature is invalid")
    result = _MetadataText()
    cursor = 2
    segments = 0
    while cursor < len(data):
        segments += 1
        if segments > _MAX_ENTRIES or data[cursor] != 255:
            raise PreparationError("Unsupported JPEG segment structure")
        while cursor < len(data) and data[cursor] == 255:
            cursor += 1
        if cursor >= len(data):
            break
        kind = data[cursor]
        cursor += 1
        if kind == 0xD9:
            if cursor != len(data):
                raise PreparationError("JPEG has uninspected trailing data")
            return result
        if kind == 0xDA:
            if cursor + 2 > len(data):
                raise PreparationError("JPEG scan header is truncated")
            length = int.from_bytes(data[cursor : cursor + 2], "big")
            if length < 2 or cursor + length > len(data) or not data.endswith(b"\xff\xd9"):
                raise PreparationError("JPEG scan header or termination is incomplete")
            return result
        if kind in {0x00, 0xD8} or 0xD0 <= kind <= 0xD7 or cursor + 2 > len(data):
            raise PreparationError("Unexpected standalone JPEG marker")
        length = int.from_bytes(data[cursor : cursor + 2], "big")
        if length < 2 or cursor + length > len(data):
            raise PreparationError("JPEG metadata segment is truncated")
        contents = data[cursor + 2 : cursor + length]
        if kind == 0xFE:
            result.append(("JPEG comment", contents.decode("utf-8")))
        elif kind == 0xE1:
            if contents.startswith(b"Exif\0\0"):
                _exif(contents[6:], result)
            elif contents.startswith(b"http://ns.adobe.com/xap/1.0/\0"):
                result.append(("JPEG XMP", contents.split(b"\0", 1)[1].decode("utf-8")))
            elif contents.startswith(b"http://ns.adobe.com/xmp/extension/\0"):
                # The extension header carries a GUID and binary chunk offsets
                # ahead of the XMP text; it is decoded for term matching only and
                # a term split across chunks is consequently not reconstructed.
                body = contents.split(b"\0", 1)[1]
                result.append(("JPEG extended XMP", body.decode("utf-8", errors="replace")))
                result.limit("JPEG extended XMP is matched per chunk only")
            else:
                raise PreparationError("Unsupported JPEG APP1 metadata")
        elif kind == 0xED:
            _photoshop(contents, result)
        cursor += length
    raise PreparationError("JPEG pre-scan metadata termination was not found")


def check_image_metadata(root: Path, options: MetadataPrivacyOptions) -> list[Finding]:
    """Search supported image metadata without decoding pixels or echoing private values."""
    if not options.image_identity_terms:
        return []
    files, inventory_issues = _inventory(root)
    issues: list[dict[str, str]] = (
        [{"reason": "Project inventory was incomplete"}] if inventory_issues else []
    )
    selected = [
        (name, path)
        for name, path in files.items()
        if path.suffix.lower() in {".png", ".jpg", ".jpeg"}
    ]
    if len(selected) > _MAX_IMAGE_COUNT:
        issues.append({"reason": "Image count exceeded the scan limit"})
        selected = selected[:_MAX_IMAGE_COUNT]
    terms = [_normalized(term) for term in options.image_identity_terms]
    matches = []
    limitations: list[dict[str, str]] = []
    scanned = total = metadata_total = 0
    for name, path in selected:
        cancellation_point()
        try:
            data = _read(path, _MAX_IMAGE_BYTES)
            total += len(data)
            if total > _MAX_IMAGE_TOTAL:
                issues.append({"reason": "Total image bytes exceeded the scan limit"})
                break
            text = _png_metadata(data) if path.suffix.lower() == ".png" else _jpeg_metadata(data)
            entries = text.entries
            metadata_total += sum(len(value.encode("utf-8")) for _, value in entries)
            if metadata_total > _MAX_METADATA_TOTAL:
                issues.append({"reason": "Decoded metadata exceeded the scan limit"})
                break
            scanned += 1
            # A skipped vendor block narrows this image's scope; the remaining
            # channels still support a determinable result for the project.
            limitations.extend({"path": name, "skipped": reason} for reason in text.limitations)
            for channel, value in entries:
                # XMP may encode names with numeric/predefined XML character references.
                # Decode text only; never evaluate entities or fetch XML resources.
                normalized = _normalized(html.unescape(value))
                for index, term in enumerate(terms, 1):
                    if term in normalized and len(matches) < _MAX_MATCHES:
                        matches.append({"path": name, "channel": channel, "term_index": index})
        except (PreparationError, OSError, UnicodeError, ValueError, struct.error, zlib.error):
            issues.append(
                {"path": name, "reason": "Unsupported, malformed or oversized image metadata"}
            )
    status = "failed" if matches else "inconclusive" if issues else "passed"
    return [
        Finding(
            "privacy.image_metadata",
            "Configured identity terms were found in supported image metadata."
            if matches
            else "Image metadata inspection is incomplete."
            if issues
            else "No configured identity terms were found in the supported image metadata scope."
            + (" Some vendor-specific blocks were skipped." if limitations else ""),
            "info" if status == "passed" else "error",
            status,
            details={
                "matches": matches,
                "issues": issues[:_MAX_MATCHES],
                "limitations": limitations[:_MAX_MATCHES],
                "incomplete": bool(issues),
                "images_selected": len(selected),
                "images_scanned": scanned,
                "match_output_limited": len(matches) >= _MAX_MATCHES,
                "scope": "PNG tEXt/zTXt/iTXt/EXIF and pre-scan JPEG EXIF/XMP/"
                "extended XMP/IPTC IIM application records/comments; "
                "literal text with character-reference decoding; "
                "skipped vendor blocks are listed as limitations; "
                "no OCR, pixel content, later JPEG scans, other image formats "
                "or guarantee of anonymity.",
            },
        )
    ]
