"""Bounded reader of the metadata a PDF file carries, for the privacy checks.

The reader collects the document information dictionary, every XMP metadata stream, the
application data of ``/PieceInfo`` dictionaries, and what pdfTeX and LuaTeX copy into the
form XObject of each included PDF: ``/PTEX.FileName``, ``/PTEX.InfoDict`` and the included
page's ``/Metadata`` and ``/PieceInfo``. It walks the file object by object with a
hand-written lexer and never renders a page or interprets a content stream. Only object
streams and metadata streams are decompressed. Every count, length, nesting level and
decompressed size is bounded. A file that exceeds a bound, is encrypted or is not a PDF
raises ``PreparationError``, which callers report as an incomplete scan of that file; an
object that cannot be parsed is skipped.

Does not own: which files are read, the matching of identity terms or home-folder paths,
or the findings (``image_metadata``, ``submission_checks``).
"""

from __future__ import annotations

import html
import re
import zlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final, NamedTuple

from .cancellation import cancellation_point
from .errors import PreparationError
from .limits import MIB


@dataclass(frozen=True, slots=True)
class PdfMetadataLimits:
    """Size bounds of reading one PDF; the defaults suit figures and ordinary papers.

    Attributes:
        max_objects: The most indirect objects read, in the file and in object streams.
        max_stream_bytes: The largest decompressed object or metadata stream, in bytes.
        max_decoded_bytes: The most decompressed bytes of all streams together.
        max_text_bytes: The most metadata text collected, in UTF-8 bytes.
        max_entries: The most metadata entries collected.
    """

    max_objects: int = 200_000
    max_stream_bytes: int = 8 * MIB
    max_decoded_bytes: int = 64 * MIB
    max_text_bytes: int = 4 * MIB
    max_entries: int = 4096


class MetadataOrigin(StrEnum):
    """Whose metadata an entry is.

    Attributes:
        DOCUMENT: The file's own metadata: its information dictionary, XMP and application
            data.
        INCLUDED: Metadata that pdfTeX or LuaTeX copied from an included PDF into the form
            XObject that draws it.
    """

    DOCUMENT = "document"
    INCLUDED = "included"


class PdfMetadataEntry(NamedTuple):
    """One metadata text and where in the PDF it was found.

    Attributes:
        origin: Whose metadata the text is.
        channel: The field, such as ``Info Author``, ``XMP dc:creator``,
            ``PTEX.FileName`` or ``PTEX.InfoDict Title``.
        text: The decoded text.
    """

    origin: MetadataOrigin
    channel: str
    text: str


@dataclass
class PdfMetadata:
    """The metadata read from one PDF.

    Attributes:
        entries: Each metadata text with its origin and channel.
        limitations: The distinct kinds of data that were deliberately not decoded.
        included_objects: The form XObjects that carry information copied from an
            included PDF.
        decoded_bytes: The decompressed stream bytes the read needed, which callers that
            read many files count against a shared budget.
    """

    entries: list[PdfMetadataEntry] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    included_objects: int = 0
    decoded_bytes: int = 0


class NotPdfError(PreparationError):
    """The bytes are not a PDF the reader can walk: no header, or no readable object.

    A caller that already knows the file is meant to be a PDF reports this like any other
    unreadable file; one that also runs a PDF tool on the file may leave it to that tool.
    """


class Reference(NamedTuple):
    """An indirect reference, ``12 0 R``.

    Attributes:
        number: The object number.
        generation: The generation number.
    """

    number: int
    generation: int


class Name(str):
    """A PDF name object, kept apart from decoded strings; the value has no slash."""

    __slots__ = ()


class _Keyword(str):
    """A bare keyword such as ``obj`` or ``R`` that is not a value."""

    __slots__ = ()


class _Malformed(Exception):
    """An object that the lexer cannot read; the reader skips it.

    Attributes:
        position: Where reading stopped, so the next attempt starts after the bytes that
            this one already examined and the whole walk stays linear in the file size.
    """

    def __init__(self, position: int) -> None:
        super().__init__(position)
        self.position = position


@dataclass(frozen=True, slots=True)
class _Stream:
    """A stream object: its dictionary and its raw, still encoded bytes.

    Attributes:
        dictionary: The stream dictionary.
        raw: The encoded stream data.
    """

    dictionary: dict[str, object]
    raw: bytes


_WHITESPACE: Final = frozenset(b"\x00\t\n\x0c\r ")
# The most levels of nested arrays and dictionaries, and of references followed in a chain.
_MAX_NESTING: Final = 32
_MAX_REFERENCE_CHAIN: Final = 8
# The most items in one array or dictionary, and the largest string or name, in bytes.
_MAX_ITEMS: Final = 65_536
_MAX_TOKEN_BYTES: Final = MIB
# Where the header must start; some writers put a few bytes before it.
_HEADER_WINDOW: Final = 1024
_OBJECT_HEADER: Final = re.compile(
    rb"(?<![0-9])(\d{1,10})[\x00\t\n\x0c\r ]+(\d{1,5})[\x00\t\n\x0c\r ]+obj(?![A-Za-z])"
)
_TRAILER: Final = re.compile(rb"trailer[\x00\t\n\x0c\r ]*<<")
_NUMBER: Final = re.compile(rb"[+-]?(?:\d+(?:\.\d*)?|\.\d+)")
_REFERENCE_TAIL: Final = re.compile(rb"[\x00\t\n\x0c\r ]+(\d{1,5})[\x00\t\n\x0c\r ]+R(?![A-Za-z])")
_REGULAR: Final = re.compile(rb"[^\x00\t\n\x0c\r ()<>\[\]{}/%]+")
_ESCAPES: Final = {
    ord("n"): b"\n",
    ord("r"): b"\r",
    ord("t"): b"\t",
    ord("b"): b"\b",
    ord("f"): b"\f",
    ord("("): b"(",
    ord(")"): b")",
    ord("\\"): b"\\",
}
# Keys whose presence marks a form XObject that draws an included PDF.
_INCLUDED_KEYS: Final = (
    "PTEX.FileName",
    "PTEX.InfoDict",
    "PTEX.PageNumber",
    "Metadata",
    "PieceInfo",
)
# XMP elements that only structure values; a text belongs to the property around them.
_XMP_CONTAINERS: Final = frozenset(
    {
        "rdf:li",
        "rdf:Seq",
        "rdf:Bag",
        "rdf:Alt",
        "rdf:Description",
        "rdf:RDF",
        "x:xmpmeta",
        "x:xapmeta",
    }
)
_XMP_IGNORED_ATTRIBUTES: Final = frozenset(
    {"rdf:about", "rdf:parseType", "rdf:resource", "rdf:nodeID", "xml:lang", "x:xmptk"}
)
_XMP_TOKEN: Final = re.compile(
    r"<(?P<close>/)?(?P<name>[A-Za-z_][\w.\-]*(?::[\w.\-]+)?)(?P<attributes>[^<>]*?)(?P<empty>/)?>"
    r"|<[?!][^>]*>|(?P<text>[^<]+)"
)
_XMP_ATTRIBUTE: Final = re.compile(r"([A-Za-z_][\w.\-]*:[\w.\-]+)\s*=\s*(?:\"([^\"]*)\"|'([^']*)')")


class _Lexer:
    """Reads PDF values from bytes, within the nesting and size bounds.

    Attributes:
        data: The bytes being read.
    """

    def __init__(self, data: bytes) -> None:
        self.data = data

    def skip(self, position: int) -> int:
        """Return the position of the next token after whitespace and comments."""
        data = self.data
        while position < len(data):
            byte = data[position]
            if byte in _WHITESPACE:
                position += 1
            elif byte == ord("%"):
                end = data.find(b"\n", position)
                position = len(data) if end < 0 else end + 1
            else:
                break
        return position

    def value(self, position: int, depth: int = 0) -> tuple[object, int]:
        """Read one value at ``position``.

        Args:
            position: Where to start, before any whitespace.
            depth: The nesting level of the value.

        Returns:
            The value and the position after it.

        Raises:
            _Malformed: The bytes are not a value the lexer reads, or a bound is exceeded.
        """
        if depth > _MAX_NESTING:
            raise _Malformed(position)
        position = self.skip(position)
        data = self.data
        if position >= len(data):
            raise _Malformed(len(data))
        if data.startswith(b"<<", position):
            return self._dictionary(position + 2, depth)
        lead = data[position]
        if lead == ord("<"):
            return self._hex_string(position + 1)
        if lead == ord("("):
            return self._literal_string(position + 1)
        if lead == ord("["):
            return self._array(position + 1, depth)
        if lead == ord("/"):
            return self._name(position + 1)
        if lead in b"+-.0123456789":
            return self._number(position)
        return self._keyword(position)

    def _dictionary(self, position: int, depth: int) -> tuple[dict[str, object], int]:
        """Read the entries of a dictionary whose ``<<`` ends before ``position``."""
        entries: dict[str, object] = {}
        while True:
            position = self.skip(position)
            if self.data.startswith(b">>", position):
                return entries, position + 2
            key, position = self.value(position, depth + 1)
            if not isinstance(key, Name) or len(entries) >= _MAX_ITEMS:
                raise _Malformed(position)
            entries[str(key)], position = self.value(position, depth + 1)

    def _array(self, position: int, depth: int) -> tuple[list[object], int]:
        """Read the items of an array whose ``[`` ends before ``position``."""
        items: list[object] = []
        while True:
            position = self.skip(position)
            if position < len(self.data) and self.data[position] == ord("]"):
                return items, position + 1
            if len(items) >= _MAX_ITEMS:
                raise _Malformed(position)
            item, position = self.value(position, depth + 1)
            items.append(item)

    def _name(self, position: int) -> tuple[Name, int]:
        """Read a name whose slash ends before ``position``, decoding ``#xx`` escapes."""
        match = _REGULAR.match(self.data, position)
        raw = match.group() if match else b""
        if len(raw) > _MAX_TOKEN_BYTES:
            raise _Malformed(position + len(raw))
        decoded = re.sub(rb"#([0-9A-Fa-f]{2})", lambda item: bytes.fromhex(item[1].decode()), raw)
        return Name(decoded.decode("latin-1")), position + len(raw)

    def _number(self, position: int) -> tuple[object, int]:
        """Read a number, or the reference ``N G R`` that starts with one."""
        match = _NUMBER.match(self.data, position)
        if match is None:
            raise _Malformed(position + 1)
        text = match.group()
        end = match.end()
        if b"." in text:
            return float(text), end
        number = int(text)
        tail = _REFERENCE_TAIL.match(self.data, end)
        if tail is not None and number >= 0:
            return Reference(number, int(tail[1])), tail.end()
        return number, end

    def _keyword(self, position: int) -> tuple[object, int]:
        """Read a bare keyword: ``true``, ``false``, ``null`` or another word."""
        match = _REGULAR.match(self.data, position)
        if match is None:
            raise _Malformed(position + 1)
        word = match.group()
        values: dict[bytes, object] = {b"true": True, b"false": False, b"null": None}
        if word in values:
            return values[word], match.end()
        return _Keyword(word.decode("latin-1")), match.end()

    def _hex_string(self, position: int) -> tuple[bytes, int]:
        """Read a hexadecimal string whose ``<`` ends before ``position``."""
        end = self.data.find(b">", position, position + 2 * _MAX_TOKEN_BYTES + 1)
        if end < 0:
            raise _Malformed(min(len(self.data), position + 2 * _MAX_TOKEN_BYTES))
        digits = bytes(byte for byte in self.data[position:end] if byte not in _WHITESPACE)
        if len(digits) % 2:
            digits += b"0"
        try:
            return bytes.fromhex(digits.decode("ascii")), end + 1
        except (UnicodeDecodeError, ValueError) as error:
            raise _Malformed(end + 1) from error

    def _literal_string(self, position: int) -> tuple[bytes, int]:
        """Read a literal string whose ``(`` ends before ``position``, with its escapes."""
        data = self.data
        result = bytearray()
        level = 1
        while position < len(data):
            byte = data[position]
            position += 1
            if byte == ord("\\"):
                position = self._escape(position, result)
                continue
            if byte == ord("("):
                level += 1
            elif byte == ord(")"):
                level -= 1
                if not level:
                    return bytes(result), position
            result.append(byte)
            if len(result) > _MAX_TOKEN_BYTES:
                raise _Malformed(position)
        raise _Malformed(len(data))

    def _escape(self, position: int, result: bytearray) -> int:
        """Append the character of the escape after a backslash; return where it ends."""
        data = self.data
        if position >= len(data):
            raise _Malformed(len(data))
        byte = data[position]
        if byte in _ESCAPES:
            result += _ESCAPES[byte]
            return position + 1
        if byte in b"01234567":
            digits = re.match(rb"[0-7]{1,3}", data[position : position + 3])
            if digits is None:
                raise _Malformed(position)
            result.append(int(digits.group(), 8) & 0xFF)
            return position + len(digits.group())
        if byte == ord("\r"):
            return position + (2 if data.startswith(b"\r\n", position) else 1)
        # A line continuation ends here; an unknown escape stands for the character itself.
        if byte != ord("\n"):
            result.append(byte)
        return position + 1


def pdf_text(raw: bytes) -> str:
    """Decode a PDF text string: UTF-16 or UTF-8 with a byte-order mark, else UTF-8 or Latin-1.

    PDFDocEncoding agrees with Latin-1 for the letters of names; the few symbols where the
    two differ do not affect matching names or paths.

    Args:
        raw: The string's bytes.

    Returns:
        The decoded text; undecodable UTF-16 units become replacement characters.
    """
    if raw.startswith(b"\xfe\xff"):
        return raw[2:].decode("utf-16-be", errors="replace")
    if raw.startswith(b"\xff\xfe"):
        return raw[2:].decode("utf-16-le", errors="replace")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", errors="replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def _xmp_text(raw: bytes) -> str:
    """Decode an XMP packet: UTF-16 with a byte-order mark, otherwise UTF-8."""
    if raw.startswith((b"\xfe\xff", b"\xff\xfe")):
        return raw.decode("utf-16", errors="replace")
    return raw.decode("utf-8", errors="replace")


def xmp_fields(raw: bytes) -> Iterator[tuple[str, str]]:
    """Yield each property of an XMP packet with its text, decoding character references only.

    The packet is scanned with regular expressions instead of an XML parser, so no entity
    is expanded and no resource is fetched. A text belongs to the innermost element that
    is not an RDF container, so a creator listed in ``rdf:Seq`` is reported as
    ``dc:creator``. Properties written as attributes are reported under their name.

    Args:
        raw: The bytes of the packet.

    Yields:
        The property name and its text, in packet order.
    """
    stack: list[str] = []
    for token in _XMP_TOKEN.finditer(_xmp_text(raw)):
        if token["text"] is not None:
            text = html.unescape(token["text"]).strip()
            owner = next((name for name in reversed(stack) if name not in _XMP_CONTAINERS), None)
            if text and owner is not None:
                yield owner, text
            continue
        name = token["name"]
        if name is None:
            continue
        if token["close"]:
            while stack and stack.pop() != name:
                pass
            continue
        for attribute in _XMP_ATTRIBUTE.finditer(token["attributes"]):
            key = attribute[1]
            value = html.unescape(attribute[2] if attribute[2] is not None else attribute[3])
            if (
                key not in _XMP_IGNORED_ATTRIBUTES
                and not key.startswith("xmlns:")
                and value.strip()
            ):
                yield key, value.strip()
        if not token["empty"] and len(stack) < _MAX_NESTING:
            stack.append(name)


class _ObjectWalk:
    """The indirect objects and trailers of one PDF, read with bounded effort.

    Attributes:
        lexer: Reads values from the file.
        limits: The bounds of the read.
        objects: Each object's value by object number; the last definition wins.
        streams: Each stream object by object number.
        trailers: The trailer dictionaries, classic and cross-reference streams.
        limitations: The kinds of data that were deliberately not decoded.
        decoded_bytes: The decompressed bytes so far.
        read_objects: The objects read so far.
    """

    def __init__(self, data: bytes, limits: PdfMetadataLimits) -> None:
        self.lexer = _Lexer(data)
        self.limits = limits
        self.objects: dict[int, object] = {}
        self.streams: dict[int, _Stream] = {}
        self.trailers: list[dict[str, object]] = []
        self.limitations: list[str] = []
        self.decoded_bytes = 0
        self.read_objects = 0

    def note(self, limitation: str) -> None:
        """Record a kind of data that was deliberately not decoded, once."""
        if limitation not in self.limitations:
            self.limitations.append(limitation)

    def count(self) -> None:
        """Count one object read, every so often letting a cancellation through.

        Raises:
            PreparationError: The file holds more objects than the limit.
        """
        self.read_objects += 1
        if self.read_objects > self.limits.max_objects:
            raise PreparationError("The PDF holds more objects than the metadata scan reads")
        if not self.read_objects % 256:
            cancellation_point()

    def read_file(self) -> None:
        """Read every indirect object of the file in order, then the trailers and object streams.

        After an object that cannot be parsed, reading resumes where the lexer stopped, so
        no byte is read twice by failing attempts.
        """
        data = self.lexer.data
        position = 0
        while match := _OBJECT_HEADER.search(data, position):
            self.count()
            try:
                value, after = self.lexer.value(match.end())
            except _Malformed as error:
                self.note("objects that could not be parsed")
                position = max(match.end(), error.position)
                continue
            position = self._store(int(match[1]), value, after)
        position = 0
        while match := _TRAILER.search(data, position):
            self.count()
            try:
                trailer, position = self.lexer.value(match.end() - 2)
            except _Malformed as error:
                position = max(match.end(), error.position)
                continue
            if isinstance(trailer, dict):
                self.trailers.append(trailer)
        for stream in list(self.streams.values()):
            if stream.dictionary.get("Type") == "ObjStm":
                self._read_object_stream(stream)

    def _store(self, number: int, value: object, after: int) -> int:
        """Keep one object read from the file; return where the next one may start."""
        data = self.lexer.data
        start = self.lexer.skip(after)
        if not (isinstance(value, dict) and data.startswith(b"stream", start)):
            self.objects[number] = value
            return after
        raw, end = self._stream_data(value, start + len(b"stream"))
        self.objects[number] = value
        self.streams[number] = _Stream(value, raw)
        if value.get("Type") == "XRef":
            self.trailers.append(value)
        return end

    def _stream_data(self, dictionary: dict[str, object], position: int) -> tuple[bytes, int]:
        """Locate the encoded data of a stream whose ``stream`` keyword ends at ``position``.

        A direct ``/Length`` is trusted when ``endstream`` follows it; otherwise the data
        runs to the next ``endstream``, or to the end of the file.

        Returns:
            The data and the position after it.
        """
        data = self.lexer.data
        if data.startswith(b"\r\n", position):
            position += 2
        elif data.startswith((b"\n", b"\r"), position):
            position += 1
        length = dictionary.get("Length")
        if isinstance(length, int) and 0 <= length <= len(data) - position:
            end = position + length
            if data.startswith(b"endstream", self.lexer.skip(end)):
                return data[position:end], end
        end = data.find(b"endstream", position)
        if end < 0:
            return data[position:], len(data)
        return data[position:end].rstrip(b"\r\n"), end

    def decoded(self, stream: _Stream) -> bytes | None:
        """Return the decompressed data of a stream, or ``None`` for an unsupported filter.

        Raises:
            PreparationError: The stream decompresses past a limit or is damaged.
        """
        filters = stream.dictionary.get("Filter")
        names = filters if isinstance(filters, list) else [] if filters is None else [filters]
        if not names:
            data = stream.raw
            if len(data) > self.limits.max_stream_bytes:
                raise PreparationError("A PDF metadata stream exceeds the decompression limit")
        elif names == ["FlateDecode"] and not _has_predictor(stream.dictionary):
            data = _inflate(stream.raw, self.limits.max_stream_bytes)
        else:
            self.note("streams with filters other than FlateDecode")
            return None
        self.decoded_bytes += len(data)
        if self.decoded_bytes > self.limits.max_decoded_bytes:
            raise PreparationError("The PDF's metadata streams exceed the decompression limit")
        return data

    def _read_object_stream(self, stream: _Stream) -> None:
        """Read the objects packed in one object stream; defined objects are kept as they are."""
        count, first = stream.dictionary.get("N"), stream.dictionary.get("First")
        if not (isinstance(count, int) and isinstance(first, int)) or count < 0 or first < 0:
            return
        data = self.decoded(stream)
        if data is None:
            return
        lexer = _Lexer(data)
        position = 0
        offsets: list[tuple[int, int]] = []
        for _ in range(count):
            self.count()
            try:
                number, position = lexer.value(position)
                offset, position = lexer.value(position)
            except _Malformed:
                return
            if not (isinstance(number, int) and isinstance(offset, int)):
                return
            offsets.append((number, offset))
        for number, offset in offsets:
            try:
                value, _ = lexer.value(first + offset)
            except _Malformed:
                self.note("objects that could not be parsed")
                continue
            self.objects.setdefault(number, value)

    def resolve(self, value: object) -> object:
        """Follow a chain of references to the value it names; a broken one names ``None``."""
        for _ in range(_MAX_REFERENCE_CHAIN):
            if not isinstance(value, Reference):
                return value
            value = self.objects.get(value.number)
        return None if isinstance(value, Reference) else value


def _has_predictor(dictionary: dict[str, object]) -> bool:
    """Return whether a stream's decode parameters ask for a predictor, which is not supported."""
    parameters = dictionary.get("DecodeParms")
    if isinstance(parameters, list):
        parameters = parameters[0] if len(parameters) == 1 else None
    predictor = parameters.get("Predictor", 1) if isinstance(parameters, dict) else 1
    return predictor != 1


def _inflate(raw: bytes, limit: int) -> bytes:
    """Inflate a zlib stream to at most ``limit`` bytes; bytes after its end are ignored.

    Raises:
        PreparationError: The stream is damaged, incomplete or inflates past ``limit``.
    """
    decoder = zlib.decompressobj()
    try:
        result = decoder.decompress(raw, limit + 1)
    except zlib.error as error:
        raise PreparationError("A compressed PDF metadata stream is damaged") from error
    if len(result) > limit:
        raise PreparationError("A PDF metadata stream exceeds the decompression limit")
    if not decoder.eof:
        raise PreparationError("A compressed PDF metadata stream is incomplete")
    return result


class _Collector:
    """Turns the objects of one walked PDF into metadata entries, within the text bounds.

    Attributes:
        walk: The objects of the PDF.
        result: The entries collected so far.
        size: The UTF-8 bytes of the collected text.
        visited: The objects whose strings were already collected.
    """

    def __init__(self, walk: _ObjectWalk) -> None:
        self.walk = walk
        self.result = PdfMetadata()
        self.size = 0
        self.visited: set[int] = set()

    def add(self, origin: MetadataOrigin, channel: str, text: str) -> None:
        """Record one entry.

        Raises:
            PreparationError: The collected text or entries exceed their limits.
        """
        limits = self.walk.limits
        self.size += len(text.encode("utf-8", errors="replace"))
        if self.size > limits.max_text_bytes or len(self.result.entries) >= limits.max_entries:
            raise PreparationError("The PDF's metadata exceeds the extraction limit")
        self.result.entries.append(PdfMetadataEntry(origin, channel, text))

    def strings(self, value: object, origin: MetadataOrigin, channel: str, depth: int = 0) -> None:
        """Record every string inside ``value``, following references a bounded number of levels."""
        if isinstance(value, Reference):
            if value.number in self.visited or depth > _MAX_NESTING:
                return
            self.visited.add(value.number)
            if value.number in self.walk.streams:
                self.walk.note("application data streams (PieceInfo)")
                return
            value = self.walk.resolve(value)
        if isinstance(value, bytes):
            if text := pdf_text(value).strip():
                self.add(origin, channel, text)
        elif isinstance(value, dict):
            for item in value.values():
                self.strings(item, origin, channel, depth + 1)
        elif isinstance(value, list):
            for item in value:
                self.strings(item, origin, channel, depth + 1)

    def dictionary_strings(self, value: object, origin: MetadataOrigin, prefix: str) -> None:
        """Record the string values of a dictionary, each under ``prefix`` and its key."""
        if isinstance(value, Reference):
            if value.number in self.visited:
                return
            self.visited.add(value.number)
        resolved = self.walk.resolve(value)
        if not isinstance(resolved, dict):
            return
        for key, item in resolved.items():
            text = self.walk.resolve(item)
            if isinstance(text, bytes) and (decoded := pdf_text(text).strip()):
                self.add(origin, f"{prefix} {key}", decoded)

    def xmp(self, number: int, origin: MetadataOrigin, prefix: str) -> None:
        """Record the properties of the XMP stream ``number``, once."""
        stream = self.walk.streams.get(number)
        if stream is None or number in self.visited:
            return
        self.visited.add(number)
        data = self.walk.decoded(stream)
        if data is None:
            return
        for name, text in xmp_fields(data):
            self.add(origin, f"{prefix} {name}", text)


def _included_forms(walk: _ObjectWalk) -> Iterator[dict[str, object]]:
    """Yield the dictionaries of the form XObjects that carry an included PDF's information."""
    for stream in walk.streams.values():
        dictionary = stream.dictionary
        if dictionary.get("Subtype") == "Form" and any(key in dictionary for key in _INCLUDED_KEYS):
            yield dictionary


def _collect_included(collector: _Collector, form: dict[str, object]) -> None:
    """Record what pdfTeX or LuaTeX copied from an included PDF into one form XObject."""
    included = MetadataOrigin.INCLUDED
    name = collector.walk.resolve(form.get("PTEX.FileName"))
    if isinstance(name, bytes) and (text := pdf_text(name).strip()):
        collector.add(included, "PTEX.FileName", text)
    collector.dictionary_strings(form.get("PTEX.InfoDict"), included, "PTEX.InfoDict")
    metadata = form.get("Metadata")
    if isinstance(metadata, Reference):
        collector.xmp(metadata.number, included, "included XMP")
    if "PieceInfo" in form:
        collector.strings(form["PieceInfo"], included, "included PieceInfo")


def _collect_document(collector: _Collector) -> None:
    """Record the file's own information dictionary, XMP streams and page application data."""
    walk = collector.walk
    document = MetadataOrigin.DOCUMENT
    for trailer in walk.trailers:
        collector.dictionary_strings(trailer.get("Info"), document, "Info")
    for number, stream in walk.streams.items():
        if stream.dictionary.get("Type") == "Metadata" or stream.dictionary.get("Subtype") == "XML":
            collector.xmp(number, document, "XMP")
    for value in walk.objects.values():
        if isinstance(value, dict) and value.get("Type") == "Page" and "PieceInfo" in value:
            collector.strings(value["PieceInfo"], document, "PieceInfo")


def read_pdf_metadata(data: bytes, limits: PdfMetadataLimits | None = None) -> PdfMetadata:
    """Read the metadata a PDF carries, and what it copied from included PDFs.

    Included-PDF information is collected first, so that an XMP stream referenced by a
    form XObject counts as copied from the included PDF rather than as the file's own.

    Args:
        data: The PDF file.
        limits: The bounds of the read; the defaults when ``None``.

    Returns:
        The entries, the limitations and the count of form XObjects with included-PDF
        information.

    Raises:
        NotPdfError: The file does not start like a PDF or has no readable object.
        PreparationError: The PDF is encrypted or exceeds a limit.
    """
    if b"%PDF-" not in data[:_HEADER_WINDOW]:
        raise NotPdfError("The file does not start like a PDF")
    walk = _ObjectWalk(data, limits or PdfMetadataLimits())
    walk.read_file()
    if not walk.objects:
        raise NotPdfError("No PDF object could be read")
    if any("Encrypt" in trailer for trailer in walk.trailers):
        raise PreparationError("The PDF is encrypted, so its metadata cannot be read")
    collector = _Collector(walk)
    for form in _included_forms(walk):
        collector.result.included_objects += 1
        _collect_included(collector, form)
    _collect_document(collector)
    collector.result.limitations = walk.limitations
    collector.result.decoded_bytes = walk.decoded_bytes
    return collector.result
