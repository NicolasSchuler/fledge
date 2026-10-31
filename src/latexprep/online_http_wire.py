"""The HTTP/1.1 bytes of one request and its bounded response.

This module owns the wire format of the one kind of exchange the online checks make: a
GET or HEAD request with a fixed set of headers, and a response whose status line,
headers and body are read within hard bounds. ``read_response`` works on any
``asyncio.StreamReader``, so it is exercised without a socket. It opens no connection,
decides nothing about redirects or retries (``online_http_session``), and refuses
anything it cannot read unambiguously: compressed bodies, conflicting framing,
duplicate length or location headers.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

from .limits import IO_CHUNK_BYTES, KIB
from .online_options import FetchOutcome, NetworkFailure
from .public_url import Target

# Longest status line, bytes of status line and headers together, and number of headers.
_MAX_STATUS_LINE_BYTES = 8 * KIB
_MAX_HEAD_BYTES = 32 * KIB
_MAX_HEADERS = 100
# A chunk-size line is a few hex digits; anything longer is not a chunk header.
_MAX_CHUNK_LINE_BYTES = 128
# Interim (1xx) responses a server may send before the final one; more is malformed.
_MAX_INTERIM_RESPONSES = 4
# Headers that must not repeat, because a second value would make the message ambiguous.
_SINGLE_VALUED_HEADERS = frozenset({"location", "content-length", "transfer-encoding"})
_STATUS_LINE = re.compile(rb"HTTP/1\.[01] ([1-5]\d\d)(?: [^\r\n]*)?\r\n")
_HEADER_NAME = re.compile(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_CHUNK_SIZE_LINE = re.compile(rb"[0-9a-fA-F]+\r\n")
# What the request asks for: JSON preferred, and the plain body, never compressed.
_ACCEPT = "application/json, */*;q=0.1"
_BODY_LIMIT_MESSAGE = "Metadata response exceeded its byte limit"


@dataclass(frozen=True)
class Response:
    """A parsed response; header names are lower-case and the body is empty unless read.

    Attributes:
        status: The status code.
        headers: The header names and values, in the order received.
        body: The body, or empty when it was not read.
    """

    status: int
    headers: tuple[tuple[str, str], ...] = ()
    body: bytes = b""

    def header(self, name: str) -> str | None:
        """Look up the value of header ``name``; a repeated single-valued header is malformed.

        Args:
            name: The header name, compared without regard to case.

        Returns:
            The first value of the header, or ``None`` when it is absent.

        Raises:
            NetworkFailure: A location, content-length or transfer-encoding header repeats.
        """
        values = [value for key, value in self.headers if key.lower() == name.lower()]
        if len(values) > 1 and name.lower() in _SINGLE_VALUED_HEADERS:
            raise NetworkFailure(FetchOutcome.MALFORMED, "Ambiguous HTTP response headers")
        return values[0] if values else None


def build_request(method: str, target: Target, user_agent: str) -> bytes:
    """Encode the request for ``target`` as ASCII bytes; the connection is closed after one reply.

    Args:
        method: The request method, ``GET`` or ``HEAD``.
        target: The host and path to request; an IPv6 host is bracketed in ``Host``.
        user_agent: The ``User-Agent`` header value.

    Returns:
        The request head, which asks for JSON preferred and an uncompressed body.
    """
    authority = f"[{target.host}]" if ":" in target.host else target.host
    request = (
        f"{method} {target.path} HTTP/1.1\r\nHost: {authority}\r\n"
        f"User-Agent: {user_agent}\r\n"
        f"Accept: {_ACCEPT}\r\nAccept-Encoding: identity\r\n"
        "Connection: close\r\n\r\n"
    )
    return request.encode("ascii")


async def _read_status(reader: asyncio.StreamReader) -> tuple[int, int]:
    """Read the status line.

    Returns:
        The status code and the byte length of the status line.

    Raises:
        NetworkFailure: The status line is invalid or longer than its bound.
    """
    line = await reader.readline()
    match = _STATUS_LINE.fullmatch(line)
    if match is None or len(line) > _MAX_STATUS_LINE_BYTES:
        raise NetworkFailure(FetchOutcome.MALFORMED, "Invalid HTTP status line")
    return int(match[1]), len(line)


async def _read_headers(reader: asyncio.StreamReader, used: int) -> tuple[tuple[str, str], ...]:
    """Read the header block; ``used`` bytes of the head budget are already spent.

    Args:
        reader: The stream positioned after the status line.
        used: The bytes of the head budget already spent on the status line.

    Returns:
        The headers with lower-case names and stripped values, in the order received.

    Raises:
        NetworkFailure: A header is invalid, or the block is incomplete or exceeds the
            bound on bytes or headers.
    """
    headers: list[tuple[str, str]] = []
    size = used
    while True:
        line = await reader.readline()
        size += len(line)
        if size > _MAX_HEAD_BYTES or len(headers) > _MAX_HEADERS or not line.endswith(b"\r\n"):
            raise NetworkFailure(
                FetchOutcome.MALFORMED, "HTTP headers exceeded their bound or were incomplete"
            )
        if line == b"\r\n":
            return tuple(headers)
        name, separator, value = line[:-2].partition(b":")
        if not separator or not _HEADER_NAME.fullmatch(name):
            raise NetworkFailure(FetchOutcome.MALFORMED, "Invalid HTTP header")
        headers.append((name.decode("ascii").lower(), value.decode("latin1").strip()))


async def _read_head(reader: asyncio.StreamReader) -> Response:
    """Read the status line and headers of one response, without its body.

    Returns:
        The response head, with an empty body.

    Raises:
        NetworkFailure: The status line or the headers are malformed or over a bound.
    """
    status, used = await _read_status(reader)
    return Response(status, await _read_headers(reader, used))


async def _read_chunked(reader: asyncio.StreamReader, limit: int) -> bytes:
    """Read a chunked body of at most ``limit`` bytes.

    Args:
        reader: The stream positioned at the first chunk-size line.
        limit: The largest accepted body.

    Returns:
        The joined chunks.

    Raises:
        NetworkFailure: A chunk-size line or chunk ending is invalid, or the body would
            exceed ``limit``.
    """
    payload = bytearray()
    while True:
        line = await reader.readline()
        if len(line) > _MAX_CHUNK_LINE_BYTES or not _CHUNK_SIZE_LINE.fullmatch(line):
            raise NetworkFailure(FetchOutcome.MALFORMED, "Invalid HTTP chunk length")
        size = int(line.strip(), 16)
        if len(payload) + size > limit:
            raise NetworkFailure(FetchOutcome.RESPONSE_LIMIT, _BODY_LIMIT_MESSAGE)
        if not size:
            return bytes(payload)
        payload.extend(await reader.readexactly(size))
        if await reader.readexactly(2) != b"\r\n":
            raise NetworkFailure(FetchOutcome.MALFORMED, "Invalid HTTP chunk ending")


async def _read_declared_length(reader: asyncio.StreamReader, length: str, limit: int) -> bytes:
    """Read a body of the declared ``Content-Length``.

    Args:
        reader: The stream positioned at the body.
        length: The ``Content-Length`` value.
        limit: The largest accepted body.

    Returns:
        The body.

    Raises:
        NetworkFailure: The length is not a decimal number or exceeds ``limit``.
    """
    if not re.fullmatch(r"\d+", length) or int(length) > limit:
        raise NetworkFailure(FetchOutcome.RESPONSE_LIMIT, _BODY_LIMIT_MESSAGE)
    return await reader.readexactly(int(length))


async def _read_until_close(reader: asyncio.StreamReader, limit: int) -> bytes:
    """Read a body without declared framing until the server closes the connection.

    Args:
        reader: The stream positioned at the body.
        limit: The largest accepted body.

    Returns:
        The body.

    Raises:
        NetworkFailure: The body exceeds ``limit``.
    """
    payload = bytearray()
    while chunk := await reader.read(min(IO_CHUNK_BYTES, limit + 1 - len(payload))):
        payload.extend(chunk)
        if len(payload) > limit:
            raise NetworkFailure(FetchOutcome.RESPONSE_LIMIT, _BODY_LIMIT_MESSAGE)
    return bytes(payload)


async def _read_body(reader: asyncio.StreamReader, head: Response, limit: int) -> bytes:
    """Read the body with the framing that the head declares.

    Args:
        reader: The stream positioned at the body.
        head: The response head, whose headers declare the encoding and framing.
        limit: The largest accepted body.

    Returns:
        The body.

    Raises:
        NetworkFailure: The body is compressed, its framing conflicts or is unsupported,
            or it is malformed or exceeds ``limit``.
    """
    if head.header("content-encoding") not in {None, "identity"}:
        raise NetworkFailure(
            FetchOutcome.MALFORMED, "Compressed metadata responses are unsupported"
        )
    length, transfer = head.header("content-length"), head.header("transfer-encoding")
    if transfer and length:
        raise NetworkFailure(FetchOutcome.MALFORMED, "Conflicting HTTP body framing")
    if transfer:
        if transfer.lower() != "chunked":
            raise NetworkFailure(FetchOutcome.MALFORMED, "Unsupported HTTP transfer encoding")
        return await _read_chunked(reader, limit)
    if length is not None:
        return await _read_declared_length(reader, length, limit)
    return await _read_until_close(reader, limit)


async def read_response(reader: asyncio.StreamReader, *, head_only: bool, limit: int) -> Response:
    """Read the final response; interim 1xx answers are skipped.

    Args:
        reader: The stream of the connection.
        head_only: Whether to skip the body; it is read only when this is false and the
            status is 2xx.
        limit: The largest accepted body; a larger body is refused rather than truncated.

    Returns:
        The final response, with its body when it was read.

    Raises:
        NetworkFailure: The response is malformed or over a bound.
        asyncio.IncompleteReadError: The connection ends early.
    """
    head = await _read_head(reader)
    interim = 0
    while 100 <= head.status < 200:
        interim += 1
        if head.status == 101 or interim > _MAX_INTERIM_RESPONSES:
            raise NetworkFailure(FetchOutcome.MALFORMED, "Unsupported interim HTTP response")
        head = await _read_head(reader)
    if head_only or not 200 <= head.status < 300:
        return head
    return Response(head.status, head.headers, await _read_body(reader, head, limit))
