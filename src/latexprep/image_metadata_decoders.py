"""Bounded decoders of the metadata blocks of PNG and JPEG files.

PNG text chunks and EXIF, and the pre-scan segments of a JPEG (EXIF, XMP, extended XMP,
IPTC IIM application records and comments), are decoded with hand-written, bounds-checked
readers into a ``MetadataText``: each decoded text with the channel it came from, and the
vendor blocks that were deliberately skipped. A reader never trusts a length it has not
checked against the block, never inflates past a limit and never reads pixels.

Does not own: which files are scanned, the matching of identity terms or home-folder paths,
or the findings (``image_metadata``).
"""

from __future__ import annotations

import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Final, Literal, NamedTuple

from .errors import PreparationError
from .limits import MIB
from .png_support import iter_png_chunks


@dataclass(frozen=True, slots=True)
class ImageScanLimits:
    """Size bounds of one image scan; the defaults suit ordinary manuscripts.

    Attributes:
        max_images: The largest number of images scanned.
        max_image_bytes: The largest size of one image file, in bytes.
        max_total_image_bytes: The largest size of every image of the project together, in
            bytes.
        max_block_bytes: The largest size of one metadata block after inflating it, in
            bytes.
        max_image_metadata_bytes: The largest size of the decoded text of one image, in
            UTF-8 bytes.
        max_total_metadata_bytes: The largest size of the decoded text of every image
            together, in UTF-8 bytes.
        max_matches: The largest number of matches kept in the report.
    """

    max_images: int = 2000
    max_image_bytes: int = 32 * MIB
    max_total_image_bytes: int = 128 * MIB
    max_block_bytes: int = MIB
    max_image_metadata_bytes: int = 4 * MIB
    max_total_metadata_bytes: int = 4 * MIB
    max_matches: int = 200


# A legitimate image holds far fewer of each; a hostile one is cut off at these counts.
_MAX_PNG_CHUNKS: Final = 4096
_MAX_JPEG_SEGMENTS: Final = 4096
_MAX_EXIF_ENTRIES: Final = 4096
_MAX_IPTC_DATASETS: Final = 4096
_MAX_PHOTOSHOP_RESOURCES: Final = 4096
_MAX_TEXT_ENTRIES_PER_IMAGE: Final = 4096
_MAX_REPORTED_LIMITATIONS: Final = 4096


# EXIF / TIFF structure.
_TIFF_MAGIC: Final = 42
_TIFF_HEADER_BYTES: Final = 8
_IFD_ENTRY_BYTES: Final = 12
_KIND_BYTE, _KIND_ASCII, _KIND_LONG, _KIND_UNDEFINED = 1, 2, 4, 7
_KIND_SIZES: Final = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8}
_INLINE_VALUE_BYTES: Final = 4  # a value this small is stored in the entry itself
_TAG_EXIF_IFD, _TAG_GPS_IFD, _TAG_INTEROPERABILITY_IFD = 0x8769, 0x8825, 0xA005
_SUBDIRECTORY_TAGS: Final = frozenset({_TAG_EXIF_IFD, _TAG_GPS_IFD, _TAG_INTEROPERABILITY_IFD})
_TAG_MAKER_NOTE: Final = 0x927C
_TAG_USER_COMMENT: Final = 0x9286
_TAG_XMP: Final = 700
_TAG_WINDOWS_TEXT_FIRST, _TAG_WINDOWS_TEXT_LAST = 0x9C9B, 0x9C9F
# Tags a researcher knows by name, so a match names the field rather than a hex code.
_EXIF_TAG_NAMES: Final = {
    0x010E: "ImageDescription",
    0x010F: "Make",
    0x0110: "Model",
    0x0131: "Software",
    0x013B: "Artist",
    0x013C: "HostComputer",
    0x8298: "Copyright",
    0x9286: "UserComment",
    0xA430: "CameraOwnerName",
    0xA431: "BodySerialNumber",
    0xA437: "LensMake",
    0x9C9B: "XPTitle",
    0x9C9C: "XPComment",
    0x9C9D: "XPAuthor",
    0x9C9E: "XPKeywords",
    0x9C9F: "XPSubject",
}
_USER_COMMENT_CHARSET_BYTES: Final = 8
_USER_COMMENT_ASCII: Final = b"ASCII\0\0\0"
_USER_COMMENT_UNICODE: Final = b"UNICODE\0"
_USER_COMMENT_UNDEFINED: Final = b"\0" * _USER_COMMENT_CHARSET_BYTES

# JPEG structure.
_JPEG_SOI: Final = b"\xff\xd8"
_JPEG_EOI: Final = b"\xff\xd9"
_MARKER_PREFIX: Final = 0xFF
_MARKER_EOI, _MARKER_SOS, _MARKER_SOI = 0xD9, 0xDA, 0xD8
_MARKER_RST_FIRST, _MARKER_RST_LAST = 0xD0, 0xD7
_MARKER_COMMENT, _MARKER_APP1, _MARKER_APP13 = 0xFE, 0xE1, 0xED
_SEGMENT_LENGTH_BYTES: Final = 2
_EXIF_APP1_PREFIX: Final = b"Exif\0\0"
_XMP_APP1_PREFIX: Final = b"http://ns.adobe.com/xap/1.0/\0"
_EXTENDED_XMP_APP1_PREFIX: Final = b"http://ns.adobe.com/xmp/extension/\0"
_PHOTOSHOP_HEADER: Final = b"Photoshop 3.0\0"
_PHOTOSHOP_RESOURCE_SIGNATURE: Final = b"8BIM"
_RESOURCE_IPTC_NAA: Final = 0x0404
_IPTC_TAG_MARKER: Final = 0x1C
_IPTC_APPLICATION_RECORD: Final = 2
_IPTC_VERSION_DATASET: Final = 0
_IPTC_EXTENDED_LENGTH_FLAG: Final = 0x8000
# Application-record datasets a researcher knows by name.
_IPTC_DATASET_NAMES: Final = {
    5: "ObjectName",
    25: "Keywords",
    80: "By-line",
    85: "By-lineTitle",
    90: "City",
    110: "Credit",
    115: "Source",
    116: "CopyrightNotice",
    120: "Caption",
    122: "Writer",
}


class MetadataText:
    """The text entries decoded from one image, and the blocks that were deliberately skipped.

    Limitations bound the scope of a negative result; unlike an error they do not make the
    surrounding scan unusable.

    Attributes:
        limits: The bounds of the scan.
        entries: Each decoded text with the channel it came from.
        limitations: The distinct descriptions of the skipped blocks.
        size: The UTF-8 bytes of decoded text so far.
    """

    def __init__(self, limits: ImageScanLimits) -> None:
        self.limits = limits
        self.entries: list[tuple[str, str]] = []
        self.limitations: list[str] = []
        self.size = 0

    def append(self, channel: str, text: str) -> None:
        """Record one decoded text entry.

        Args:
            channel: The metadata channel the text came from.
            text: The decoded text.

        Raises:
            PreparationError: The image's decoded text or entry count exceeds its limit.
        """
        self.size += len(text.encode("utf-8"))
        if (
            self.size > self.limits.max_image_metadata_bytes
            or len(self.entries) >= _MAX_TEXT_ENTRIES_PER_IMAGE
        ):
            raise PreparationError("Decoded metadata exceeds the cumulative extraction limit")
        self.entries.append((channel, text))

    def limit(self, reason: str) -> None:
        """Record a deliberately skipped block once.

        Args:
            reason: What was skipped.

        Raises:
            PreparationError: Too many distinct blocks were skipped to report.
        """
        if reason not in self.limitations:
            if len(self.limitations) >= _MAX_REPORTED_LIMITATIONS:
                raise PreparationError("Skipped metadata blocks exceed the reporting limit")
            self.limitations.append(reason)


def _inflate(data: bytes, limit: int) -> bytes:
    """Inflate a complete zlib stream whose output stays within ``limit`` bytes.

    Args:
        data: The compressed stream.
        limit: The largest inflated size, in bytes.

    Returns:
        The inflated bytes.

    Raises:
        PreparationError: The stream is incomplete, has trailing data, or inflates past
            ``limit``.
        zlib.error: The stream is damaged.
    """
    decoder = zlib.decompressobj()
    result = decoder.decompress(data, limit + 1)
    if len(result) > limit or not decoder.eof or decoder.unused_data:
        raise PreparationError("Compressed metadata is incomplete or exceeds its decoded limit")
    return result


def _decoded_text(raw: bytes) -> str:
    """Decode a metadata field whose encoding is not declared: UTF-8 if valid, else Latin-1.

    Cameras and editors store non-ASCII names in either encoding. Latin-1 gives every byte a
    character, so an accented name cannot make the whole image unreadable to the scan.
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


# --- EXIF -----------------------------------------------------------------------------


class _IfdEntry(NamedTuple):
    """One 12-byte image file directory entry, with its value located but not yet copied.

    Attributes:
        tag: The field's tag.
        kind: The field's TIFF type.
        count: The number of values.
        length: The bytes of the value.
        value_start: Where the value lives in the block.
        field_offset: Where the entry's own 4-byte value/offset field lives.
    """

    tag: int
    kind: int
    count: int
    length: int
    value_start: int
    field_offset: int


class _ExifReader:
    """Walks the directories of one TIFF-structured EXIF block, bounds-checking every read.

    Construction raises ``PreparationError`` for an unsupported byte order or TIFF header.

    Attributes:
        data: The EXIF block.
        result: Receives the decoded text and the skipped blocks.
        byte_order: The block's byte order.
    """

    def __init__(self, data: bytes, result: MetadataText) -> None:
        if len(data) < _TIFF_HEADER_BYTES or data[:2] not in {b"II", b"MM"}:
            raise PreparationError("Unsupported EXIF byte order")
        self.data = data
        self.result = result
        self.byte_order: Literal["little", "big"] = "little" if data[:2] == b"II" else "big"
        if self.integer(2, 2) != _TIFF_MAGIC:
            raise PreparationError("Unsupported EXIF TIFF header")

    def integer(self, offset: int, size: int) -> int:
        """Read an unsigned integer in the block's byte order.

        Args:
            offset: Where the integer starts in the block.
            size: Its width in bytes.

        Returns:
            The integer.

        Raises:
            PreparationError: The integer lies outside the block.
        """
        if offset < 0 or offset + size > len(self.data):
            raise PreparationError("EXIF offset is outside the metadata block")
        return int.from_bytes(self.data[offset : offset + size], self.byte_order)

    def read(self) -> None:
        """Walk every directory and its subdirectories, recording the text fields.

        Raises:
            PreparationError: The directories are cyclic, hold too many entries, or have
                an entry or value outside the block.
        """
        pending = [self.integer(4, 4)]
        seen: set[int] = set()
        total = 0
        while pending:
            offset = pending.pop()
            if not offset:
                continue
            if offset in seen:
                raise PreparationError("Cyclic EXIF directory references")
            seen.add(offset)
            entries = self.integer(offset, 2)
            total += entries
            if total > _MAX_EXIF_ENTRIES:
                raise PreparationError("EXIF directory entry limit exceeded")
            for index in range(entries):
                entry = self._entry(offset + 2 + _IFD_ENTRY_BYTES * index)
                self._record(entry, pending)
            pending.append(self.integer(offset + 2 + _IFD_ENTRY_BYTES * entries, 4))

    def _entry(self, offset: int) -> _IfdEntry:
        """Read and validate the entry at ``offset``; its value is copied only when used.

        Returns:
            The entry, with its value located in the block.

        Raises:
            PreparationError: The entry has an unsupported type, an oversized value, or a
                value outside the block.
        """
        tag, kind, count = (
            self.integer(offset, 2),
            self.integer(offset + 2, 2),
            self.integer(offset + 4, 4),
        )
        if (
            kind not in _KIND_SIZES
            or count * _KIND_SIZES[kind] > self.result.limits.max_block_bytes
        ):
            raise PreparationError("Unsupported or oversized EXIF field")
        length = count * _KIND_SIZES[kind]
        field_offset = offset + 8
        start = field_offset if length <= _INLINE_VALUE_BYTES else self.integer(field_offset, 4)
        if start + length > len(self.data):
            raise PreparationError("EXIF value extends outside its metadata block")
        return _IfdEntry(tag, kind, count, length, start, field_offset)

    def _value(self, entry: _IfdEntry) -> bytes:
        """Copy an entry's value out of the block."""
        return self.data[entry.value_start : entry.value_start + entry.length]

    def _record(self, entry: _IfdEntry, pending: list[int]) -> None:
        """Record one entry: a subdirectory, a skipped MakerNote, or a supported text field.

        Args:
            entry: The directory entry.
            pending: The directory offsets still to walk; a subdirectory pointer is added.

        Raises:
            PreparationError: A subdirectory pointer is malformed, or the decoded text
                exceeds its limit.
        """
        tag, kind = entry.tag, entry.kind
        if tag in _SUBDIRECTORY_TAGS:
            if kind != _KIND_LONG or entry.count != 1:
                raise PreparationError("Unsupported EXIF subdirectory pointer")
            pending.append(self.integer(entry.field_offset, 4))
        elif tag == _TAG_MAKER_NOTE:
            # MakerNote holds an undocumented vendor structure whose own byte
            # order and offsets are not portable. Skipping it keeps the rest of
            # an ordinary camera image's metadata determinable.
            self.result.limit("EXIF MakerNote (vendor binary)")
        elif kind == _KIND_ASCII:
            self.result.append(_exif_channel(tag), _decoded_text(self._value(entry).rstrip(b"\0")))
        elif tag == _TAG_USER_COMMENT and kind == _KIND_UNDEFINED:
            self._user_comment(self._value(entry))
        elif _TAG_WINDOWS_TEXT_FIRST <= tag <= _TAG_WINDOWS_TEXT_LAST and kind == _KIND_BYTE:
            text = self._value(entry).decode("utf-16-le", errors="replace").rstrip("\0")
            self.result.append(_exif_channel(tag, prefix="EXIF Windows"), text)
        elif tag == _TAG_XMP and kind in {_KIND_BYTE, _KIND_UNDEFINED}:
            self.result.append("EXIF XMP", _decoded_text(self._value(entry).rstrip(b"\0")))

    def _user_comment(self, value: bytes) -> None:
        """Record a UserComment, whose first eight bytes name its character set."""
        if not value.strip(b"\0 "):
            return
        charset, text = value[:_USER_COMMENT_CHARSET_BYTES], value[_USER_COMMENT_CHARSET_BYTES:]
        channel = _exif_channel(_TAG_USER_COMMENT)
        if charset in {_USER_COMMENT_ASCII, _USER_COMMENT_UNDEFINED}:
            # An undefined character set is application-defined text, usually ASCII or UTF-8.
            self.result.append(channel, _decoded_text(text.rstrip(b"\0")))
        elif charset == _USER_COMMENT_UNICODE:
            encoding = (
                "utf-16"
                if text[:2] in {b"\xff\xfe", b"\xfe\xff"}
                else ("utf-16-le" if self.byte_order == "little" else "utf-16-be")
            )
            self.result.append(channel, text.decode(encoding, errors="replace").rstrip("\0"))
        else:
            # JIS and unregistered character sets cannot be decoded reliably.
            self.result.limit("EXIF UserComment in a JIS or unrecognized character set")


def _exif_channel(tag: int, *, prefix: str = "EXIF") -> str:
    """Name the channel of an EXIF tag, by the tag's known name where it has one."""
    name = _EXIF_TAG_NAMES.get(tag)
    return f"{prefix} {name} ({tag:#06x})" if name else f"{prefix} tag {tag:#06x}"


def _read_exif(data: bytes, result: MetadataText) -> None:
    """Record the text fields of one TIFF-structured EXIF block.

    Args:
        data: The EXIF block.
        result: Receives the decoded text and the skipped blocks.

    Raises:
        PreparationError: The block is malformed or exceeds a limit.
    """
    _ExifReader(data, result).read()


# --- IPTC and Photoshop resources -----------------------------------------------------


def _iptc_channel(dataset: int) -> str:
    """Name the channel of an IPTC application-record dataset, with its known name."""
    name = _IPTC_DATASET_NAMES.get(dataset)
    return f"JPEG IPTC 2:{dataset:03d}" + (f" ({name})" if name else "")


def _read_iptc(data: bytes, result: MetadataText) -> None:
    """Record simple IPTC IIM application-record text datasets.

    A dataset outside the supported layout, with an extended length, truncated or not
    UTF-8 text, is recorded as a limitation.

    Args:
        data: The IPTC-NAA resource.
        result: Receives the decoded text and the skipped blocks.

    Raises:
        PreparationError: The block holds too many datasets, or the decoded text exceeds
            its limit.
    """
    cursor = 0
    datasets = 0
    while cursor < len(data):
        datasets += 1
        if datasets > _MAX_IPTC_DATASETS:
            raise PreparationError("JPEG IPTC dataset limit exceeded")
        if data[cursor] != _IPTC_TAG_MARKER or cursor + 5 > len(data):
            result.limit("JPEG IPTC dataset outside the supported IIM layout")
            return
        record, dataset = data[cursor + 1], data[cursor + 2]
        length = int.from_bytes(data[cursor + 3 : cursor + 5], "big")
        cursor += 5
        if length & _IPTC_EXTENDED_LENGTH_FLAG:
            result.limit("JPEG IPTC extended dataset length")
            return
        if cursor + length > len(data):
            result.limit("truncated JPEG IPTC dataset")
            return
        value = data[cursor : cursor + length]
        cursor += length
        # The application record holds text; its dataset 0 is a binary version number,
        # and other records hold envelope or object-data bytes.
        if record != _IPTC_APPLICATION_RECORD or dataset == _IPTC_VERSION_DATASET:
            continue
        try:
            result.append(_iptc_channel(dataset), value.decode("utf-8"))
        except UnicodeDecodeError:
            result.limit("JPEG IPTC dataset with an undeclared text encoding")


def _read_photoshop(contents: bytes, result: MetadataText) -> None:
    """Walk 8BIM image resources and inspect only the IPTC-NAA resource.

    A block outside the supported layout, or an oversized or truncated resource, is
    recorded as a limitation.

    Args:
        contents: The APP13 segment's data.
        result: Receives the decoded text and the skipped blocks.

    Raises:
        PreparationError: The block holds too many resources, or the IPTC datasets exceed
            a limit.
    """
    if not contents.startswith(_PHOTOSHOP_HEADER):
        result.limit("JPEG APP13 block outside the supported Photoshop layout")
        return
    cursor = len(_PHOTOSHOP_HEADER)
    resources = 0
    while cursor < len(contents):
        resources += 1
        if resources > _MAX_PHOTOSHOP_RESOURCES:
            raise PreparationError("JPEG APP13 resource limit exceeded")
        signature_end = cursor + len(_PHOTOSHOP_RESOURCE_SIGNATURE)
        if contents[cursor:signature_end] != _PHOTOSHOP_RESOURCE_SIGNATURE or cursor + 7 > len(
            contents
        ):
            result.limit("JPEG APP13 block outside the supported Photoshop layout")
            return
        identifier = int.from_bytes(contents[signature_end : signature_end + 2], "big")
        # The resource name is a Pascal string padded to an even total length.
        name = 1 + contents[cursor + 6]
        cursor += 6 + name + name % 2
        if cursor + 4 > len(contents):
            result.limit("truncated JPEG APP13 resource")
            return
        size = int.from_bytes(contents[cursor : cursor + 4], "big")
        cursor += 4
        if size > result.limits.max_block_bytes or cursor + size > len(contents):
            result.limit("oversized or truncated JPEG APP13 resource")
            return
        block = contents[cursor : cursor + size]
        cursor += size + size % 2
        if identifier == _RESOURCE_IPTC_NAA:
            _read_iptc(block, result)


# --- PNG ------------------------------------------------------------------------------


def _read_png_text(kind: bytes, contents: bytes, result: MetadataText) -> None:
    """Record one tEXt, zTXt or iTXt chunk; compressed text is inflated within the limit.

    Args:
        kind: The chunk type.
        contents: The chunk data.
        result: Receives the decoded text.

    Raises:
        PreparationError: The compression is unsupported, or inflating or the decoded
            text exceeds its limit.
        ValueError: The chunk is malformed or its text is not in the declared encoding.
        zlib.error: Compressed text is damaged.
    """
    limit = result.limits.max_block_bytes
    key, remaining = contents.split(b"\0", 1)
    if kind == b"tEXt":
        result.append("PNG tEXt", (key + b" " + remaining).decode("latin-1"))
    elif kind == b"zTXt":
        if not remaining or remaining[0] != 0:
            raise PreparationError("Unsupported PNG text compression")
        result.append("PNG zTXt", (key + b" " + _inflate(remaining[1:], limit)).decode("latin-1"))
    else:
        if len(remaining) < 4 or remaining[0] not in {0, 1} or remaining[1] != 0:
            raise PreparationError("Unsupported PNG international text compression")
        language, translated, value = remaining[2:].split(b"\0", 2)
        if remaining[0]:
            value = _inflate(value, limit)
        result.append(
            "PNG iTXt",
            " ".join(
                (
                    key.decode("latin-1"),
                    language.decode("ascii"),
                    translated.decode("utf-8"),
                    value.decode("utf-8"),
                )
            ),
        )


def png_metadata(data: bytes, limits: ImageScanLimits) -> MetadataText:
    """Decode the text and EXIF chunks of one PNG file.

    Args:
        data: The PNG file.
        limits: The bounds of the scan.

    Returns:
        The decoded text and skipped blocks.

    Raises:
        PreparationError: The file or a metadata block is malformed or exceeds a limit.
        ValueError: A text chunk is malformed.
        zlib.error: Compressed text is damaged.
    """
    result = MetadataText(limits)
    for chunk in iter_png_chunks(data, max_chunks=_MAX_PNG_CHUNKS):
        if chunk.kind not in {b"tEXt", b"zTXt", b"iTXt", b"eXIf"}:
            continue
        if len(chunk.data) > limits.max_block_bytes:
            raise PreparationError("PNG metadata block exceeds the size limit")
        if chunk.kind == b"eXIf":
            _read_exif(chunk.data, result)
        else:
            _read_png_text(chunk.kind, chunk.data, result)
    return result


# --- JPEG -----------------------------------------------------------------------------


class _JpegSegment(NamedTuple):
    """One length-prefixed JPEG segment.

    Attributes:
        marker: The marker byte that follows ``0xFF``.
        contents: The segment's data, without its length field.
    """

    marker: int
    contents: bytes


def _segment_length(data: bytes, cursor: int) -> int:
    """Read the length field at ``cursor``, which counts its own two bytes."""
    return int.from_bytes(data[cursor : cursor + _SEGMENT_LENGTH_BYTES], "big")


def _require_complete_scan_start(data: bytes, cursor: int) -> None:
    """Check that a start-of-scan header fits the file, which ends with an end-of-image marker.

    Args:
        data: The JPEG file.
        cursor: Where the header's length field starts.

    Raises:
        PreparationError: The header is truncated or the file does not end properly.
    """
    if cursor + _SEGMENT_LENGTH_BYTES > len(data):
        raise PreparationError("JPEG scan header is truncated")
    length = _segment_length(data, cursor)
    if (
        length < _SEGMENT_LENGTH_BYTES
        or cursor + length > len(data)
        or not data.endswith(_JPEG_EOI)
    ):
        raise PreparationError("JPEG scan header or termination is incomplete")


def _length_prefixed_segment(data: bytes, cursor: int, marker: int) -> _JpegSegment:
    """Read the segment of ``marker`` whose length field is at ``cursor``.

    Args:
        data: The JPEG file.
        cursor: Where the segment's length field starts.
        marker: The segment's marker byte.

    Returns:
        The segment.

    Raises:
        PreparationError: The marker stands alone, or the segment is cut off.
    """
    if (
        marker in {0x00, _MARKER_SOI}
        or _MARKER_RST_FIRST <= marker <= _MARKER_RST_LAST
        or cursor + _SEGMENT_LENGTH_BYTES > len(data)
    ):
        raise PreparationError("Unexpected standalone JPEG marker")
    length = _segment_length(data, cursor)
    if length < _SEGMENT_LENGTH_BYTES or cursor + length > len(data):
        raise PreparationError("JPEG metadata segment is truncated")
    return _JpegSegment(marker, data[cursor + _SEGMENT_LENGTH_BYTES : cursor + length])


def _iter_jpeg_segments(data: bytes) -> Iterator[_JpegSegment]:
    """Yield each length-prefixed segment before the first scan; every defect is named.

    The walk ends successfully at the end-of-image marker or after validating the start of
    the first scan. ``submission_readiness_checks`` reads colour from a bounded prefix with
    a lenient walker instead, so the two walkers deliberately stay separate.

    Args:
        data: The JPEG file.

    Yields:
        Each segment before the first scan, in file order.

    Raises:
        PreparationError: A structural defect, trailing data or a missing end.
    """
    if not data.startswith(_JPEG_SOI):
        raise PreparationError("JPEG signature is invalid")
    cursor = len(_JPEG_SOI)
    segments = 0
    while cursor < len(data):
        segments += 1
        if segments > _MAX_JPEG_SEGMENTS or data[cursor] != _MARKER_PREFIX:
            raise PreparationError("Unsupported JPEG segment structure")
        while cursor < len(data) and data[cursor] == _MARKER_PREFIX:
            cursor += 1
        if cursor >= len(data):
            break
        marker = data[cursor]
        cursor += 1
        if marker == _MARKER_EOI:
            if cursor != len(data):
                raise PreparationError("JPEG has uninspected trailing data")
            return
        if marker == _MARKER_SOS:
            _require_complete_scan_start(data, cursor)
            return
        segment = _length_prefixed_segment(data, cursor, marker)
        yield segment
        cursor += _SEGMENT_LENGTH_BYTES + len(segment.contents)
    raise PreparationError("JPEG pre-scan metadata termination was not found")


def _read_app1(contents: bytes, result: MetadataText) -> None:
    """Record an APP1 segment: EXIF, XMP or extended XMP.

    Extended XMP is matched per chunk only, which is recorded as a limitation.

    Args:
        contents: The APP1 segment's data.
        result: Receives the decoded text and the skipped blocks.

    Raises:
        PreparationError: The segment holds other APP1 metadata, is malformed, or exceeds
            a limit.
    """
    if contents.startswith(_EXIF_APP1_PREFIX):
        _read_exif(contents[len(_EXIF_APP1_PREFIX) :], result)
    elif contents.startswith(_XMP_APP1_PREFIX):
        result.append("JPEG XMP", _decoded_text(contents[len(_XMP_APP1_PREFIX) :]))
    elif contents.startswith(_EXTENDED_XMP_APP1_PREFIX):
        # The extension header carries a GUID and binary chunk offsets
        # ahead of the XMP text; it is decoded for term matching only and
        # a term split across chunks is consequently not reconstructed.
        body = contents[len(_EXTENDED_XMP_APP1_PREFIX) :]
        result.append("JPEG extended XMP", body.decode("utf-8", errors="replace"))
        result.limit("JPEG extended XMP is matched per chunk only")
    else:
        raise PreparationError("Unsupported JPEG APP1 metadata")


def jpeg_metadata(data: bytes, limits: ImageScanLimits) -> MetadataText:
    """Decode the comments, APP1 and APP13 segments of one JPEG file before its first scan.

    Args:
        data: The JPEG file.
        limits: The bounds of the scan.

    Returns:
        The decoded text and skipped blocks.

    Raises:
        PreparationError: The file or a metadata segment is malformed or exceeds a limit.
    """
    result = MetadataText(limits)
    for segment in _iter_jpeg_segments(data):
        if segment.marker == _MARKER_COMMENT:
            result.append("JPEG comment", _decoded_text(segment.contents))
        elif segment.marker == _MARKER_APP1:
            _read_app1(segment.contents, result)
        elif segment.marker == _MARKER_APP13:
            _read_photoshop(segment.contents, result)
    return result
