"""Explicit literal metadata edit proposals and bounded image metadata privacy scans."""

from __future__ import annotations

import codecs
import html
import re
import unicodedata
import zlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .cancellation import cancellation_point
from .errors import PreparationError, SettingsError
from .file_safety import read_regular_file
from .file_scan import check_status, inventory_files, plural, redact_secrets
from .findings import Finding, has_blockers
from .limits import MIB
from .option_config import require_choice, require_records, require_text, require_unique_strings
from .path_safety import is_relative_posix_path
from .png_support import iter_png_chunks
from .report import Change, ChangeKind
from .tex_commands import ROOT_SUFFIXES
from .tex_lexer import (
    MAX_SOURCE_BYTES,
    MAX_TOTAL_SOURCE_BYTES,
    Command,
    ParseLimitExceeded,
    blank_regions,
    comment_starts,
    conditional_spans,
    in_conditional,
    read_group,
    scan_commands,
    source_regions,
)
from .text_edits import OverlappingEdits, TextEdit, apply_text_edits, unified_diff_text, with_bom
from .text_safety import is_unsafe_character

_PDF_FIELDS = {
    "hypersetup.pdftitle": "Title",
    "hypersetup.pdfauthor": "Author",
    "hypersetup.pdfsubject": "Subject",
    "hypersetup.pdfkeywords": "Keywords",
}
_FIELDS = {*_PDF_FIELDS, "title", "author", "email", "affiliation"}
_MAX_IMAGE_BYTES = 32 * MIB
_MAX_IMAGE_TOTAL = 128 * MIB
_MAX_METADATA_BYTES = MIB
_MAX_METADATA_TOTAL = 4 * MIB
_MAX_IMAGE_COUNT = 2000
_MAX_ENTRIES = 4096
_MAX_EDITS = 256  # selected source metadata edits
_MAX_IDENTITY_TERMS = 256
_MAX_TERM_CHARACTERS = 1024  # characters in one identity term
_MAX_EDIT_TEXT_CHARACTERS = 65536  # characters in an edit's expected or replacement text
_MAX_MATCHES = 200
# Problems named in a message; the finding's details list every one.
_MAX_LISTED_PROBLEMS = 3
_SCANNED_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg"})
# Figure formats the scan does not read, named in the result so a pass is not over-read.
_UNSCANNED_FIGURE_FORMATS = "PDF, EPS, SVG, TIFF, GIF and WebP"
# Commands that can change a declaration while TeX runs, so its source text is not final.
_DYNAMIC_COMMANDS = frozenset({"catcode", "csname", "scantokens", "ExplSyntaxOn", "directlua"})


def _literal(value: str) -> bool:
    return not any(char in value for char in "\\{}%#$&^~") and not any(
        is_unsafe_character(char) and char not in "\r\n\t" for char in value
    )


@dataclass(frozen=True)
class MetadataEdit:
    path: str
    field: str
    expected_before: str
    replacement: str

    def __post_init__(self) -> None:
        require_text("path", self.path, expected="a safe relative TeX manuscript path")
        if not is_relative_posix_path(self.path, characters="literal") or (
            PurePosixPath(self.path).suffix.lower() not in ROOT_SUFFIXES
        ):
            raise SettingsError("path must be a safe relative TeX manuscript path", ("path",))
        require_choice("field", self.field, sorted(_FIELDS))
        for name in ("expected_before", "replacement"):
            value = getattr(self, name)
            require_text(
                name,
                value,
                expected="bounded plain literal text",
                max_length=_MAX_EDIT_TEXT_CHARACTERS,
                allow_blank=True,
            )
            if not _literal(value):
                raise SettingsError(
                    f"{name} must be plain literal text without TeX commands or control characters",
                    (name,),
                )


@dataclass(frozen=True)
class MetadataPrivacyOptions:
    edits: tuple[MetadataEdit, ...] = ()
    image_identity_terms: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require_records(
            "edits",
            self.edits,
            MetadataEdit,
            max_items=_MAX_EDITS,
            distinct_by=lambda edit: (edit.path, edit.field),
            distinct_description="source metadata field",
        )
        require_unique_strings(
            "image_identity_terms",
            self.image_identity_terms,
            max_items=_MAX_IDENTITY_TERMS,
            max_length=_MAX_TERM_CHARACTERS,
            allow_surrounding_whitespace=True,
        )
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


# A hyperref setting is ``key``, ``key=value`` or ``key={value}``. A key without a value is a
# yes/no option such as ``colorlinks`` or ``hidelinks``.
_HYPERSETUP_KEY = re.compile(r"([A-Za-z]+)\s*(=\s*)?")


def _field_label(field_name: str) -> str:
    """How a selected field is named to the researcher: its command or hyperref key."""
    if field_name.startswith("hypersetup."):
        return f"hyperref key {field_name.removeprefix('hypersetup.')}"
    return f"\\{field_name}"


def _hypersetup_values(text: str, start: int, end: int) -> list[tuple[str, int, int]]:
    """Locate plain keys with braced or unbraced literal values, retaining source offsets.

    Keys without a value are accepted and skipped; they hold no text to edit.
    """
    result = []
    cursor = start
    while cursor < end:
        while cursor < end and (text[cursor].isspace() or text[cursor] == ","):
            cursor += 1
        if cursor == end:
            break
        match = _HYPERSETUP_KEY.match(text, cursor, end)
        if match is None:
            line = text.count("\n", 0, cursor) + 1
            raise PreparationError(
                f"\\hypersetup on line {line} has a setting that is not written as "
                "key, key=value or key={value}"
            )
        key = match[1]
        cursor = match.end()
        if match[2] is None:
            while cursor < end and text[cursor].isspace():
                cursor += 1
            if cursor < end and text[cursor] != ",":
                raise PreparationError(
                    f"hyperref key {key} is followed by text that is neither '=' nor ','"
                )
            continue
        if cursor < end and text[cursor] == "{":
            value = read_group(text, cursor)
            if value is None or value.end > end:
                raise PreparationError(f"The braced value of hyperref key {key} is not closed")
            result.append((key, value.start, value.end))
            cursor = value.end + 1
            while cursor < end and text[cursor].isspace():
                cursor += 1
            if cursor < end and text[cursor] != ",":
                raise PreparationError(
                    f"The braced value of hyperref key {key} is followed by text before the "
                    "next comma"
                )
        else:
            stop = text.find(",", cursor, end)
            stop = end if stop < 0 else stop
            trimmed = stop
            while trimmed > cursor and text[trimmed - 1].isspace():
                trimmed -= 1
            result.append((key, cursor, trimmed))
            cursor = stop
    return result


def _selected_command_problem(command: Command, text: str) -> str | None:
    """Why a selected declaration cannot be edited safely, or None when it is a plain one."""
    name = f"\\{command.name}"
    if command.depth:
        return f"{name} is inside braces or an environment, so its scope is not certain"
    if command.options:
        return f"{name} has an optional argument, which this edit does not support"
    if len(command.arguments) != 1:
        return f"{name} does not have exactly one braced argument"
    if text.startswith("\\" + command.name + "*", command.start):
        return f"{name} is used in its starred form, which this edit does not support"
    return None


def _edit_spans(text: str, edits: Sequence[MetadataEdit]) -> list[TextEdit]:
    regions, unclosed = source_regions(text)
    if unclosed:
        raise PreparationError("An unclosed literal region prevents metadata editing")
    try:
        commands = scan_commands(blank_regions(text, regions), comment_starts(regions))
    except ParseLimitExceeded:
        raise PreparationError("Source exceeds supported metadata parsing limits") from None
    dynamic = next(
        (command.name for command in commands if command.name in _DYNAMIC_COMMANDS), None
    )
    if dynamic is not None:
        raise PreparationError(
            f"\\{dynamic} can change metadata while TeX runs, so the declared values are "
            "not certain"
        )
    # Only a conditional that encloses a selected declaration makes its old value
    # unestablished; an unrelated \if…\fi elsewhere in the file does not.
    conditionals = conditional_spans(commands)
    targets: dict[str, list[tuple[int, int]]] = {edit.field: [] for edit in edits}
    for command in commands:
        if command.name == "endinput" or command.ends("document"):
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
                f"\\{command.name} is inside conditional TeX (\\if ... \\fi), so which value "
                "applies is not certain"
            )
        problem = _selected_command_problem(command, text)
        if problem is not None:
            raise PreparationError(problem)
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
        label = _field_label(edit.field)
        if len(spans) != 1:
            found = "was not found" if not spans else f"was declared {len(spans)} times"
            raise PreparationError(f"{label} {found}; exactly one declaration is required")
        start, end = spans[0]
        actual = text[start:end]
        if not _literal(actual) or actual != edit.expected_before:
            raise PreparationError(
                f"{label} does not hold the expected old value; the source changed or "
                "expected_before is wrong"
            )
        replacement = edit.replacement
        if edit.field.startswith("hypersetup.") and text[start - 1 : start] != "{":
            replacement = "{" + replacement + "}"
        replacements.append(TextEdit(start, end, replacement))
    return replacements


def plan_metadata_sanitization(
    root: Path, options: MetadataPrivacyOptions
) -> MetadataSanitizationPlan:
    """Return all-or-nothing source proposals; never modify the source directory."""
    plan = MetadataSanitizationPlan()
    if not options.edits:
        return plan
    files, issues = inventory_files(root)
    if issues:
        plan.findings.append(
            Finding(
                "privacy.metadata_sanitization",
                "Source inventory is incomplete; no metadata changes are proposed.",
                severity="error",
                status="inconclusive",
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
            original = read_regular_file(files[name], MAX_SOURCE_BYTES)
            total += len(original)
            if total > MAX_TOTAL_SOURCE_BYTES:
                raise PreparationError("Metadata sources exceed the total source-byte limit")
            bom = original.startswith(codecs.BOM_UTF8)
            text = original.decode("utf-8-sig")
            try:
                prepared = apply_text_edits(text, _edit_spans(text, selected))
            except OverlappingEdits:
                raise PreparationError("Metadata edits address overlapping source text") from None
            if prepared != text:
                plan.originals[name] = original
                plan.contents[name] = with_bom(prepared, bom).encode("utf-8")
                plan.changes.append(
                    Change(
                        name,
                        kind=ChangeKind.METADATA_SANITIZATION,
                        reason="Explicitly selected "
                        "literal metadata replacements; final PDF recheck required.",
                        diff=unified_diff_text(name, text, prepared),
                    )
                )
            plan.findings.append(
                Finding(
                    "privacy.metadata_sanitization",
                    "Selected literal metadata matched its old-value "
                    "preconditions; final rebuilt PDF metadata still requires verification.",
                    severity="info",
                    status="passed",
                    path=name,
                    details={
                        "fields": [edit.field for edit in selected],
                        "final_pdf_recheck_required": True,
                    },
                )
            )
        except (OSError, UnicodeError, PreparationError) as error:
            # Rejection messages are fixed text; file-system errors may name absolute paths.
            reason = (
                str(error)
                if isinstance(error, PreparationError)
                else "the file is unreadable or is not UTF-8 text"
            )
            plan.findings.append(
                Finding(
                    "privacy.metadata_sanitization",
                    f"Metadata edit cannot be proposed: {reason}.",
                    severity="error",
                    status="inconclusive",
                    path=name,
                    suggestion="Compare the edit's path, field and expected_before with the "
                    "source file, correct the edit or the source, and run again.",
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
                severity="error",
                status="inconclusive",
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
                severity="error",
                status="inconclusive",
            )
        ]
    names = {key.casefold(): key for key in inventories[0]}
    violations: list[dict[str, object]] = []

    def violation(index: int, name: str, problem: str) -> None:
        # The reason names the edit and property, never a value, and is what a terminal
        # report shows for each violation.
        violations.append(
            {
                "edit_index": index,
                "property": name,
                "reason": f"Edit {index}: PDF property {name} {problem}",
            }
        )

    for index, edit in enumerate(options.edits, 1):
        if edit.field in _PDF_FIELDS:
            key = _PDF_FIELDS[edit.field].casefold()
            if " ".join(properties.get(key, "").split()) != " ".join(edit.replacement.split()):
                violation(index, _PDF_FIELDS[edit.field], "does not show the selected replacement")
        old = _normalized(edit.expected_before)
        if old and old not in _normalized(edit.replacement):
            for key, value in properties.items():
                if old in _normalized(value):
                    violation(index, names[key], "still contains the value that the edit removes")
    return [
        Finding(
            "privacy.sanitized_pdf_metadata",
            _recheck_message(violations),
            severity="error" if violations else "info",
            status="failed" if violations else "passed",
            details={
                "violations": violations,
                "scope": "Reported top-level PDF properties only; hidden objects, image pixels and "
                "semantic anonymity remain unverified.",
            },
        )
    ]


def _recheck_message(violations: Sequence[dict[str, object]]) -> str:
    """Outcome of the final-PDF recheck, naming the first few problems."""
    if not violations:
        return (
            "Checked selected metadata replacements and removed "
            "literal values in final reported PDF properties."
        )
    shown = "; ".join(str(item["reason"]) for item in violations[:_MAX_LISTED_PROBLEMS])
    more = len(violations) - _MAX_LISTED_PROBLEMS
    return (
        f"Final PDF metadata does not match the selected edits: {shown}"
        + (f"; and {more} more" if more > 0 else "")
        + ". Correct the metadata in the source, rebuild the PDF and run the check again."
    )


def _inflate(data: bytes) -> bytes:
    decoder = zlib.decompressobj()
    result = decoder.decompress(data, _MAX_METADATA_BYTES + 1)
    if len(result) > _MAX_METADATA_BYTES or not decoder.eof or decoder.unused_data:
        raise PreparationError("Compressed metadata is incomplete or exceeds its decoded limit")
    return result


def _decoded_text(raw: bytes) -> str:
    """Text of a metadata field whose encoding is not declared: UTF-8 if valid, else Latin-1.

    Cameras and editors store non-ASCII names in either encoding. Latin-1 gives every byte a
    character, so an accented name cannot make the whole image unreadable to the scan.
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def _user_comment(value: bytes, byte_order: str, result: _MetadataText) -> None:
    """Record an EXIF UserComment, whose first eight bytes name its character set."""
    if not value.strip(b"\0 "):
        return
    charset, text = value[:8], value[8:]
    if charset == b"ASCII\0\0\0":
        result.append(("EXIF UserComment", _decoded_text(text.rstrip(b"\0"))))
    elif charset == b"UNICODE\0":
        encoding = (
            "utf-16"
            if text[:2] in {b"\xff\xfe", b"\xfe\xff"}
            else ("utf-16-le" if byte_order == "<" else "utf-16-be")
        )
        result.append(("EXIF UserComment", text.decode(encoding, errors="replace").rstrip("\0")))
    elif charset == b"\0" * 8:
        # An undefined character set is application-defined text, usually ASCII or UTF-8.
        result.append(("EXIF UserComment", _decoded_text(text.rstrip(b"\0"))))
    else:
        # JIS and unregistered character sets cannot be decoded reliably.
        result.limit("EXIF UserComment in a JIS or unrecognized character set")


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
                result.append((f"EXIF tag {tag:#06x}", _decoded_text(value.rstrip(b"\0"))))
            elif tag == 0x9286 and kind == 7:
                _user_comment(value, order, result)
            elif 0x9C9B <= tag <= 0x9C9F and kind == 1:
                result.append(
                    (
                        f"EXIF Windows tag {tag:#06x}",
                        value.decode("utf-16-le", errors="replace").rstrip("\0"),
                    )
                )
            elif tag == 700 and kind in {1, 7}:
                result.append(("EXIF XMP", _decoded_text(value.rstrip(b"\0"))))
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
    result = _MetadataText()
    for chunk in iter_png_chunks(data, max_chunks=_MAX_ENTRIES):
        kind, contents = chunk
        if kind not in {b"tEXt", b"zTXt", b"iTXt", b"eXIf"}:
            continue
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
            result.append(("PNG zTXt", (key + b" " + _inflate(compressed[1:])).decode("latin-1")))
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
    return result


def _jpeg_metadata(data: bytes) -> _MetadataText:
    """Walk a whole JPEG's pre-scan segments strictly; every structural defect is named.

    ``submission_readiness_checks`` reads colour from a bounded prefix with a lenient
    walker instead, so the two walkers deliberately stay separate.
    """
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
            result.append(("JPEG comment", _decoded_text(contents)))
        elif kind == 0xE1:
            if contents.startswith(b"Exif\0\0"):
                _exif(contents[6:], result)
            elif contents.startswith(b"http://ns.adobe.com/xap/1.0/\0"):
                result.append(("JPEG XMP", _decoded_text(contents.split(b"\0", 1)[1])))
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


def _unreadable_reason(error: Exception) -> str:
    """Why an image's metadata could not be read, without quoting file contents or paths.

    Parser rejections are fixed text; other errors may quote file contents or paths, so they
    are described by kind only.
    """
    if isinstance(error, PreparationError):
        return str(error)
    if isinstance(error, UnicodeError):
        return "a metadata text field uses an unsupported character encoding"
    if isinstance(error, zlib.error):
        return "compressed metadata is damaged"
    if isinstance(error, OSError):
        return "the file is unreadable, not a regular file or larger than the scan limit"
    return "a metadata block is malformed"


def _first_problem(issues: Sequence[dict[str, str]]) -> str:
    """The first scan problem with its image path, and how many others there are."""
    first = issues[0]
    where = f"{redact_secrets(first['path'])}: " if "path" in first else ""
    others = len(issues) - 1
    return f"{where}{first['reason']}" + (f" (and {others} more)" if others else "")


def _image_scan_message(
    status: str,
    *,
    matches: Sequence[dict[str, object]],
    issues: Sequence[dict[str, str]],
    scanned: int,
    selected: int,
    has_limitations: bool,
) -> str:
    """The outcome of the image scan; a pass states what was and was not covered."""
    match status:
        case "failed":
            message = (
                "Configured identity terms were found in the metadata of "
                f"{plural(len({item['path'] for item in matches}), 'image')}."
            )
            if issues:
                message += f" Other images could not be fully checked: {_first_problem(issues)}."
            return message
        case "inconclusive":
            return (
                f"Image metadata could not be fully checked: {_first_problem(issues)}. "
                f"{scanned} of {plural(selected, 'PNG/JPEG image')} were scanned."
            )
    covered = (
        f"the metadata of {plural(scanned, 'PNG/JPEG image')}"
        if scanned
        else "any PNG or JPEG image (none was found)"
    )
    message = (
        f"No configured identity terms were found in {covered}. "
        f"{_UNSCANNED_FIGURE_FORMATS} figures and image pixels were not scanned."
    )
    if has_limitations:
        message += " Some vendor-specific blocks were skipped."
    return message


def check_image_metadata(root: Path, options: MetadataPrivacyOptions) -> list[Finding]:
    """Search supported image metadata without decoding pixels or echoing private values."""
    if not options.image_identity_terms:
        return []
    files, inventory_issues = inventory_files(root)
    issues: list[dict[str, str]] = (
        [{"reason": "Project inventory was incomplete"}] if inventory_issues else []
    )
    selected = [
        (name, path)
        for name, path in files.items()
        if path.suffix.lower() in _SCANNED_IMAGE_SUFFIXES
    ]
    if len(selected) > _MAX_IMAGE_COUNT:
        issues.append({"reason": "Image count exceeded the scan limit"})
        selected = selected[:_MAX_IMAGE_COUNT]
    terms = [_normalized(term) for term in options.image_identity_terms]
    matches: list[dict[str, object]] = []
    match_output_limited = False
    limitations: list[dict[str, str]] = []
    scanned = total = metadata_total = 0
    for name, path in selected:
        cancellation_point()
        try:
            data = read_regular_file(path, _MAX_IMAGE_BYTES)
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
                    if term not in normalized:
                        continue
                    if len(matches) < _MAX_MATCHES:
                        matches.append({"path": name, "channel": channel, "term_index": index})
                    else:
                        match_output_limited = True
        except (PreparationError, OSError, ValueError, zlib.error) as error:
            issues.append({"path": name, "reason": _unreadable_reason(error)})
    status = check_status(failed=bool(matches), incomplete=bool(issues))
    return [
        Finding(
            "privacy.image_metadata",
            _image_scan_message(
                status,
                matches=matches,
                issues=issues,
                scanned=scanned,
                selected=len(selected),
                has_limitations=bool(limitations),
            ),
            severity="info" if status == "passed" else "error",
            status=status,
            suggestion=(
                "Re-save or re-export each listed image with standard metadata, or remove it "
                "from the project, then run the check again."
                if status == "inconclusive"
                else None
            ),
            details={
                "matches": matches,
                "issues": issues[:_MAX_MATCHES],
                "limitations": limitations[:_MAX_MATCHES],
                "incomplete": bool(issues),
                "images_selected": len(selected),
                "images_scanned": scanned,
                "match_output_limited": match_output_limited,
                "scope": "PNG tEXt/zTXt/iTXt/EXIF and pre-scan JPEG EXIF/XMP/"
                "extended XMP/IPTC IIM application records/comments; "
                "literal text with character-reference decoding; "
                "skipped vendor blocks are listed as limitations; "
                "no OCR, pixel content, later JPEG scans, other image formats "
                "or guarantee of anonymity.",
            },
        )
    ]
