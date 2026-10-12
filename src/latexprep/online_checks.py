"""Explicit, bounded online reference checks; no rewriting or implicit networking.

Crossref field/filter semantics:
https://github.com/Crossref/rest-api-doc/blob/master/api_format.md
https://www.crossref.org/documentation/retrieve-metadata/rest-api/rest-api-filters/

The transport intentionally uses direct sockets, without urllib proxy, cookie or
authentication machinery. Each validated DNS answer is pinned through connection
establishment. A disposable resolver child makes system DNS cancellation bounded.
"""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import html
import ipaddress
import json
import math
import re
import socket
import ssl
import sys
import time
import unicodedata
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

from . import __version__
from .bibtex_syntax import BibDocument, BibFileReader, Entry, field_literal, read_doi
from .citation_graph import resource_declarations
from .expanded_document import reachable_sources
from .models import EvidenceKind, Finding, PreparationError, Severity, Status
from .option_config import require_booleans
from .scheduler import ResourceBudget, bounded_map, cancellation_point, run_in_thread
from .source_graph import SourceGraph
from .tex_lexer import ParseLimitExceeded, mask_literals, scan_commands

_CROSSREF = "https://api.crossref.org/works"
_MAX_INPUT_BYTES = 16 * 1024 * 1024
_MAX_BIB_BYTES = 2 * 1024 * 1024
_MAX_REFERENCES = 2000
_SECRET_KEY = re.compile(
    r"token|secret|password|passwd|api.?key|authorization|signature|credential|session|jwt|bearer"
    r"|^(?:auth|key|sig|code)$",
    re.I,
)
_DOI = re.compile(r"10\.\d{4,9}/[^\s<>\"{}\\]{1,2048}", re.I)
_EMAIL = re.compile(
    r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
    r"@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+"
)
# Crossref's polite pool keys on a contact address inside the product comment.
_PRODUCT = f"fledge/{__version__}"
_DEFAULT_USER_AGENT = f"{_PRODUCT} (explicit reference checks)"
# Each selectable check's option and rule, in catalogue order (NET001-NET007).
_CHECK_RULES = {
    "online_doi_resolution": "online.doi_resolution",
    "online_metadata": "online.metadata",
    "online_missing_doi": "online.missing_doi",
    "online_published_versions": "online.published_version",
    "online_notices": "online.notices",
    "online_reference_links": "online.reference_link",
    "online_replication_links": "online.replication_link",
}
# The only check that reads configured and source links instead of bibliography entries.
_SOURCE_LINK_CHECK = "online_replication_links"
# Special-use blocks whose is_global classification differs across supported Python
# releases, including embedded-IPv4 transition routes.
_SPECIAL_IPV6_NETWORKS = tuple(
    ipaddress.IPv6Network(prefix)
    for prefix in (
        "64:ff9b::/96",
        "64:ff9b:1::/48",
        "2002::/16",
        "2001::/23",
        "3fff::/20",
        "5f00::/16",
    )
)
_SPECIAL_IPV4_NETWORKS = tuple(
    ipaddress.IPv4Network(prefix) for prefix in ("192.0.0.0/24", "192.88.99.0/24")
)
_T = TypeVar("_T")


class FetchOutcome(StrEnum):
    """How one bounded request ended; the values are the reported outcome strings."""

    AVAILABLE = "available"
    AUTHENTICATION_REQUIRED = "authentication_required"
    ACCESS_DENIED = "access_denied"
    NOT_FOUND = "not_found"
    GONE = "gone"
    RATE_LIMITED = "rate_limited"
    UNAVAILABLE = "unavailable"
    UNSAFE_TARGET = "unsafe_target"
    MALFORMED = "malformed"
    RESPONSE_LIMIT = "response_limit"
    REQUEST_LIMIT = "request_limit"
    REDIRECT_LOOP = "redirect_loop"
    REDIRECT_LIMIT = "redirect_limit"
    TIMEOUT = "timeout"
    IDENTIFIER_MISMATCH = "identifier_mismatch"
    INPUT_UNAVAILABLE = "input_unavailable"


@dataclass(frozen=True)
class OnlineOptions:
    """Every remote check is opt-in and bounded; nothing is retried automatically.

    ``online_max_requests`` caps the whole job, not each check. Its default of 40
    is reached quickly by an ordinary bibliography: one DOI resolution, metadata
    lookup, notice query or URL probe each spend one request, so a project with
    more references than the remaining budget reports ``request_limit`` for the
    unvisited entries instead of silently treating them as verified. Raise it
    deliberately (up to 1000) when complete reference coverage is required.

    ``online_contact_email`` is sent only in the User-Agent request header, for
    Crossref's polite pool. It never enters a finding, detail or report field.
    """

    online: bool = False
    online_doi_resolution: bool = False
    online_metadata: bool = False
    online_missing_doi: bool = False
    online_published_versions: bool = False
    online_notices: bool = False
    online_reference_links: bool = False
    online_replication_links: bool = False
    online_replication_urls: tuple[str, ...] = ()
    online_contact_email: str | None = None
    online_max_requests: int = 40
    online_timeout_seconds: int = 10
    online_max_redirects: int = 4
    online_max_response_bytes: int = 1_048_576
    online_jobs: int = 2
    online_provider_interval_seconds: float = 1.0

    def __post_init__(self) -> None:
        require_booleans(self, "online", *_CHECK_RULES)
        for name, low, high in (
            ("online_max_requests", 1, 1000),
            ("online_timeout_seconds", 1, 60),
            ("online_max_redirects", 0, 10),
            ("online_max_response_bytes", 1024, 4_194_304),
            ("online_jobs", 1, 8),
        ):
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise PreparationError(f"{name} must be an integer from {low} to {high}")
        interval = self.online_provider_interval_seconds
        if (
            isinstance(interval, bool)
            or not isinstance(interval, (int, float))
            or not math.isfinite(interval)
            or not 0.1 <= interval <= 60
        ):
            raise PreparationError("online_provider_interval_seconds must be from 0.1 to 60")
        if (
            not isinstance(self.online_replication_urls, tuple)
            or len(self.online_replication_urls) > 1000
            or any(not isinstance(url, str) or not url for url in self.online_replication_urls)
        ):
            raise PreparationError(
                "online_replication_urls must be a tuple of at most 1000 nonempty URLs"
            )
        email = self.online_contact_email
        if email is not None and (
            not isinstance(email, str) or len(email) > 254 or _EMAIL.fullmatch(email) is None
        ):
            raise PreparationError(
                "online_contact_email must be one simple email address of at most 254 characters"
            )

    @property
    def user_agent(self) -> str:
        """The exact User-Agent header value; the only place a contact address is used."""
        email = self.online_contact_email
        return f"{_PRODUCT} (mailto:{email})" if email else _DEFAULT_USER_AGENT

    @property
    def selected_rules(self) -> list[str]:
        """Rules of the selected checks, whether or not network access is allowed."""
        return [rule for name, rule in _CHECK_RULES.items() if getattr(self, name)]

    @property
    def reads_bibliography(self) -> bool:
        """Whether a selected check requests bibliography entries."""
        return any(getattr(self, name) for name in _CHECK_RULES if name != _SOURCE_LINK_CHECK)


class NetworkFailure(Exception):
    """A bounded, redacted failure; never contains provider bodies or credentials."""

    def __init__(self, outcome: FetchOutcome, message: str) -> None:
        super().__init__(message)
        self.outcome = outcome


@dataclass(frozen=True)
class Target:
    url: str
    scheme: str
    host: str
    port: int
    path: str


def redact_url(value: str) -> str:
    try:
        parts = urlsplit(value)
        host = parts.hostname or "invalid-target"
        authority = f"[{host}]" if ":" in host else host
        if parts.port:
            authority += f":{parts.port}"
        query = urlencode(
            [
                (key, "[redacted]" if _SECRET_KEY.search(key) else val)
                for key, val in parse_qsl(parts.query.replace(";", "&"), keep_blank_values=True)
            ]
        )
        return urlunsplit((parts.scheme, authority, parts.path, query, ""))
    except ValueError:
        return "[invalid or credential-bearing URL]"


def _target(url: str) -> Target:
    if (
        not isinstance(url, str)
        or len(url) > 8192
        or any(ord(char) < 33 or ord(char) == 127 for char in url)
        or "\\" in url
    ):
        raise NetworkFailure(
            FetchOutcome.UNSAFE_TARGET, "URL contains unsupported whitespace, controls or escapes"
        )
    try:
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError
        if parts.username is not None or parts.password is not None:
            raise NetworkFailure(
                FetchOutcome.UNSAFE_TARGET, "Credential-bearing URLs are never requested"
            )
        if any(
            _SECRET_KEY.search(key)
            for key, _ in parse_qsl(parts.query.replace(";", "&"), keep_blank_values=True)
        ):
            raise NetworkFailure(
                FetchOutcome.UNSAFE_TARGET,
                "Credential-bearing query parameters are never requested",
            )
        host = parts.hostname.encode("idna").decode("ascii").lower().rstrip(".")
        if host.endswith(
            (
                ".localhost",
                ".local",
                ".localdomain",
                ".internal",
                ".home",
                ".onion",
                ".test",
                ".invalid",
                ".example",
            )
        ):
            raise NetworkFailure(
                FetchOutcome.UNSAFE_TARGET, "Reserved or local DNS names are forbidden"
            )
        port = parts.port or (443 if parts.scheme == "https" else 80)
        if port != (443 if parts.scheme == "https" else 80) or "%" in host:
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if (
                "." not in host
                or len(host) > 253
                or any(
                    not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                    for label in host.split(".")
                )
            ):
                raise ValueError from None
        else:
            _public_ip(str(address))
    except (ValueError, UnicodeError) as error:
        raise NetworkFailure(
            FetchOutcome.UNSAFE_TARGET,
            "Only public HTTP/HTTPS URLs on their standard ports are supported",
        ) from error
    authority = f"[{host}]" if ":" in host else host
    path = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = quote(parts.query, safe="%=&;/:?@!$'()*+,-._~")
    request_path = path + ("?" + query if query else "")
    return Target(
        urlunsplit((parts.scheme, authority, path, query, "")),
        parts.scheme,
        host,
        port,
        request_path,
    )


def _public_ip(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as error:
        raise NetworkFailure(
            FetchOutcome.UNSAFE_TARGET, "DNS returned an invalid IP address"
        ) from error
    mapped = getattr(address, "ipv4_mapped", None)
    checked = mapped or address
    special = (
        isinstance(address, ipaddress.IPv6Address)
        and any(address in network for network in _SPECIAL_IPV6_NETWORKS)
    ) or (
        isinstance(checked, ipaddress.IPv4Address)
        and any(checked in network for network in _SPECIAL_IPV4_NETWORKS)
    )
    if (
        not checked.is_global
        or special
        or checked.is_multicast
        or checked.is_reserved
        or checked.is_loopback
        or checked.is_link_local
        or checked.is_unspecified
        or getattr(address, "scope_id", None)
    ):
        raise NetworkFailure(
            FetchOutcome.UNSAFE_TARGET,
            "Private, local, reserved or multicast network targets are forbidden",
        )
    return str(address)


_DNS_SCRIPT = """import json,socket,sys
rows=socket.getaddrinfo(sys.argv[1],int(sys.argv[2]),type=socket.SOCK_STREAM)
addresses=sorted({row[4][0] for row in rows})
if not addresses or len(addresses)>32: raise SystemExit(2)
print(json.dumps(addresses))
"""


async def _drain(work: Awaitable[_T]) -> _T:
    """Await ``work`` to completion even when the caller is cancelled meanwhile.

    Cleanup such as reaping the resolver child must finish before cancellation
    propagates; a cancellation received while waiting is re-raised afterwards.
    """
    task = asyncio.ensure_future(work)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


async def _resolve(host: str, port: int) -> tuple[str, ...]:
    try:
        return (_public_ip(str(ipaddress.ip_address(host))),)
    except ValueError:
        pass
    launch = asyncio.create_task(
        asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-c",
            _DNS_SCRIPT,
            host,
            str(port),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={"LANG": "C"},
            close_fds=True,
        )
    )
    process = None
    try:
        process = await asyncio.shield(launch)
        assert process.stdout is not None
        data = bytearray()
        while chunk := await process.stdout.read(8193 - len(data)):
            data.extend(chunk)
            if len(data) > 8192:
                raise NetworkFailure(
                    FetchOutcome.UNAVAILABLE, "DNS response exceeded its byte limit"
                )
        await process.wait()
        if process.returncode or len(data) > 8192:
            raise NetworkFailure(FetchOutcome.UNAVAILABLE, "Public DNS resolution did not complete")
        addresses = json.loads(data)
        if not isinstance(addresses, list) or not 1 <= len(addresses) <= 32:
            raise ValueError
        return tuple(_public_ip(value) for value in addresses)
    except (ValueError, OSError) as error:
        raise NetworkFailure(
            FetchOutcome.UNAVAILABLE, "Public DNS resolution did not complete"
        ) from error
    finally:

        async def close() -> None:
            child = process
            if child is None:
                try:
                    child = await launch
                except OSError:
                    return
            if child.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    child.kill()
            await child.wait()

        await _drain(close())


@dataclass(frozen=True)
class Response:
    status: int
    headers: tuple[tuple[str, str], ...] = ()
    body: bytes = b""

    def header(self, name: str) -> str | None:
        values = [value for key, value in self.headers if key.lower() == name.lower()]
        if len(values) > 1 and name.lower() in {"location", "content-length", "transfer-encoding"}:
            raise NetworkFailure(FetchOutcome.MALFORMED, "Ambiguous HTTP response headers")
        return values[0] if values else None


async def _exchange(
    target: Target,
    address: str,
    method: str,
    body: bool,
    maximum: int,
    user_agent: str = _DEFAULT_USER_AGENT,
) -> Response:
    """Connect to a numeric address once; TLS verifies the original DNS hostname."""
    address = _public_ip(address)
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setblocking(False)
    writer: asyncio.StreamWriter | None = None
    try:
        await asyncio.get_running_loop().sock_connect(sock, (address, target.port))
        context = ssl.create_default_context() if target.scheme == "https" else None
        reader, writer = await asyncio.open_connection(
            sock=sock,
            ssl=context,
            server_hostname=target.host if context else None,
            limit=65_536,
        )
        authority = f"[{target.host}]" if ":" in target.host else target.host
        request = (
            f"{method} {target.path} HTTP/1.1\r\nHost: {authority}\r\n"
            f"User-Agent: {user_agent}\r\n"
            "Accept: application/json, */*;q=0.1\r\nAccept-Encoding: identity\r\n"
            "Connection: close\r\n\r\n"
        )
        writer.write(request.encode("ascii"))
        await writer.drain()
        line = await reader.readline()
        match = re.fullmatch(rb"HTTP/1\.[01] ([1-5]\d\d)(?: [^\r\n]*)?\r\n", line)
        if match is None or len(line) > 8192:
            raise NetworkFailure(FetchOutcome.MALFORMED, "Invalid HTTP status line")
        headers = []
        size = len(line)
        while True:
            line = await reader.readline()
            size += len(line)
            if size > 32_768 or len(headers) > 100 or not line.endswith(b"\r\n"):
                raise NetworkFailure(
                    FetchOutcome.MALFORMED, "HTTP headers exceeded their bound or were incomplete"
                )
            if line == b"\r\n":
                break
            name, separator, value = line[:-2].partition(b":")
            if not separator or not re.fullmatch(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
                raise NetworkFailure(FetchOutcome.MALFORMED, "Invalid HTTP header")
            headers.append((name.decode("ascii").lower(), value.decode("latin1").strip()))
        response = Response(int(match[1]), tuple(headers))
        if not body or not 200 <= response.status < 300 or method == "HEAD":
            return response
        if response.header("content-encoding") not in {None, "identity"}:
            raise NetworkFailure(
                FetchOutcome.MALFORMED, "Compressed metadata responses are unsupported"
            )
        length, transfer = response.header("content-length"), response.header("transfer-encoding")
        if transfer and length:
            raise NetworkFailure(FetchOutcome.MALFORMED, "Conflicting HTTP body framing")
        payload = bytearray()
        if transfer:
            if transfer.lower() != "chunked":
                raise NetworkFailure(FetchOutcome.MALFORMED, "Unsupported HTTP transfer encoding")
            while True:
                line = await reader.readline()
                if len(line) > 128 or not re.fullmatch(rb"[0-9a-fA-F]+\r\n", line):
                    raise NetworkFailure(FetchOutcome.MALFORMED, "Invalid HTTP chunk length")
                chunk = int(line.strip(), 16)
                if len(payload) + chunk > maximum:
                    raise NetworkFailure(
                        FetchOutcome.RESPONSE_LIMIT, "Metadata response exceeded its byte limit"
                    )
                if not chunk:
                    break
                payload.extend(await reader.readexactly(chunk))
                if await reader.readexactly(2) != b"\r\n":
                    raise NetworkFailure(FetchOutcome.MALFORMED, "Invalid HTTP chunk ending")
        elif length is not None:
            if not re.fullmatch(r"\d+", length) or int(length) > maximum:
                raise NetworkFailure(
                    FetchOutcome.RESPONSE_LIMIT, "Metadata response exceeded its byte limit"
                )
            payload.extend(await reader.readexactly(int(length)))
        else:
            while chunk := await reader.read(min(65_536, maximum + 1 - len(payload))):
                payload.extend(chunk)
                if len(payload) > maximum:
                    raise NetworkFailure(
                        FetchOutcome.RESPONSE_LIMIT, "Metadata response exceeded its byte limit"
                    )
        return Response(response.status, response.headers, bytes(payload))
    finally:
        if writer is not None:
            writer.close()
            # Closing the transport is synchronous; do not let peer shutdown stall cleanup.
            if writer.transport is not None:
                writer.transport.abort()
        else:
            sock.close()


@dataclass(frozen=True)
class Fetch:
    outcome: FetchOutcome
    url: str
    response: Response | None = None
    redirects: tuple[str, ...] = ()
    message: str = ""


_HTTP_OUTCOMES = {
    401: FetchOutcome.AUTHENTICATION_REQUIRED,
    403: FetchOutcome.ACCESS_DENIED,
    404: FetchOutcome.NOT_FOUND,
    410: FetchOutcome.GONE,
    429: FetchOutcome.RATE_LIMITED,
}


def _http_outcome(status: int) -> FetchOutcome:
    if 200 <= status < 300:
        return FetchOutcome.AVAILABLE
    return _HTTP_OUTCOMES.get(status, FetchOutcome.UNAVAILABLE)


class _Client:
    def __init__(self, options: OnlineOptions, budget: ResourceBudget | None) -> None:
        self.options, self.budget = options, budget
        self.requests = 0
        # One lock per provider host serializes its requests and spacing.
        self.locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.next_request: dict[str, float] = {}
        self.throttled: set[str] = set()
        self.cache: dict[tuple[str, bool], Fetch] = {}
        self.cache_bytes = 0
        # One lock per request lets concurrent identical requests share one result.
        self.cache_locks: defaultdict[tuple[str, bool], asyncio.Lock] = defaultdict(asyncio.Lock)

    async def _one(self, target: Target, method: str, body: bool) -> Response:
        async with self.locks[target.host]:
            if target.host in self.throttled:
                raise NetworkFailure(
                    FetchOutcome.RATE_LIMITED,
                    "Provider throttled an earlier request; no automatic retry",
                )
            await asyncio.sleep(
                max(0.0, self.next_request.get(target.host, 0.0) - time.monotonic())
            )
            if self.requests >= self.options.online_max_requests:
                raise NetworkFailure(FetchOutcome.REQUEST_LIMIT, "Online request budget exhausted")
            reservation = (
                self.budget.lease("online request", memory_mb=64)
                if self.budget
                else contextlib.nullcontext()
            )
            async with reservation:
                # Different providers may have waited for the same shared resource slot.
                # Admission and increment must not be separated by an await.
                if self.requests >= self.options.online_max_requests:
                    raise NetworkFailure(
                        FetchOutcome.REQUEST_LIMIT, "Online request budget exhausted"
                    )
                self.requests += 1
                self.next_request[target.host] = (
                    time.monotonic() + self.options.online_provider_interval_seconds
                )
                async with asyncio.timeout(self.options.online_timeout_seconds):
                    addresses = await _resolve(target.host, target.port)
                    # Recheck injected/resolver answers and pin exactly one validated address.
                    addresses = tuple(_public_ip(address) for address in addresses)
                    if not addresses:
                        raise NetworkFailure(
                            FetchOutcome.UNAVAILABLE, "DNS returned no public addresses"
                        )
                    response = await _exchange(
                        target,
                        addresses[0],
                        method,
                        body,
                        self.options.online_max_response_bytes,
                        self.options.user_agent,
                    )
                if response.status == 429:
                    self.throttled.add(target.host)
                return response

    async def fetch(self, url: str, *, body: bool = False) -> Fetch:
        key = (url, body)
        async with self.cache_locks[key]:
            if key in self.cache:
                return self.cache[key]
            current, redirects, visited = url, [], set()
            try:
                for _ in range(self.options.online_max_redirects + 1):
                    target = _target(current)
                    if target.url in visited:
                        raise NetworkFailure(
                            FetchOutcome.REDIRECT_LOOP, "HTTP redirect loop detected"
                        )
                    visited.add(target.url)
                    response = await self._one(target, "GET" if body else "HEAD", body)
                    if not body and response.status in {405, 501}:
                        response = await self._one(target, "GET", False)
                    if response.status in {301, 302, 303, 307, 308}:
                        location = response.header("location")
                        if not location:
                            raise NetworkFailure(
                                FetchOutcome.MALFORMED, "Redirect omitted its destination"
                            )
                        current = urljoin(target.url, location)
                        # Validate even the final over-budget redirect without contacting it.
                        _target(current)
                        redirects.append(redact_url(current))
                        continue
                    result = Fetch(
                        _http_outcome(response.status),
                        redact_url(target.url),
                        response,
                        tuple(redirects),
                    )
                    break
                else:
                    raise NetworkFailure(
                        FetchOutcome.REDIRECT_LIMIT, "HTTP redirect limit exhausted"
                    )
            except NetworkFailure as error:
                result = Fetch(
                    error.outcome,
                    redact_url(current),
                    redirects=tuple(redirects),
                    message=str(error),
                )
            except TimeoutError:
                result = Fetch(
                    FetchOutcome.TIMEOUT,
                    redact_url(current),
                    redirects=tuple(redirects),
                    message="Network request exceeded its deadline",
                )
            except (OSError, ValueError, asyncio.IncompleteReadError) as error:
                result = Fetch(
                    FetchOutcome.UNAVAILABLE,
                    redact_url(current),
                    redirects=tuple(redirects),
                    message=f"Network transport failed ({type(error).__name__})",
                )
            self.cache[key] = result
            self.cache_bytes += len(result.response.body) if result.response else 0
            while len(self.cache) > 128 or self.cache_bytes > 4_194_304:
                removed = self.cache.pop(next(iter(self.cache)))
                self.cache_bytes -= len(removed.response.body) if removed.response else 0
            return result


@dataclass(frozen=True)
class Reference:
    key: str
    path: str | None
    line: int | None
    kind: str
    values: dict[str, str | None]
    doi: str | None = None


def _bibliography_paths(root: Path, inspection: SourceGraph, gaps: list[str]) -> list[Path]:
    """The selected graph's declared resources, or every .bib file without a document root."""
    resources, resource_reasons = (
        resource_declarations(inspection, reachable_sources(inspection))
        if inspection.main
        else (set(), [])
    )
    if resources:
        gaps.extend(resource_reasons)
        return [root / name for name in sorted(resources)]
    if not inspection.roots:
        # A bibliography-only directory states its own scope: every local file.
        return sorted(path for path in root.rglob("*") if path.suffix.lower() == ".bib")
    gaps.append(
        "The selected source graph declares no resolvable bibliography resource; "
        "no entry was requested"
    )
    return []


def _entry_reference(document: BibDocument, entry: Entry) -> Reference:
    """The bounded literal values a lookup may share; a repeated field becomes unknown."""
    values: dict[str, str | None] = {}
    doi = None
    for item in entry.fields:
        if item.name in values:
            values[item.name] = None
            if item.name == "doi":
                doi = None
            continue
        value = field_literal(item, max_chars=8192)
        values[item.name] = None if value is None else value.replace("{", "").replace("}", "")
        if item.name == "doi":
            doi = read_doi(item).identifier
            if doi is not None and _DOI.fullmatch(doi) is None:
                doi = None
    return Reference(
        entry.key, document.path, document.line(entry.start), entry.entry_type, values, doi
    )


def _bibliography_references(root: Path, paths: list[Path], gaps: list[str]) -> list[Reference]:
    references: list[Reference] = []
    reader = BibFileReader(max_file_bytes=_MAX_BIB_BYTES, max_total_bytes=_MAX_INPUT_BYTES)
    for path in paths:
        cancellation_point()
        if (
            path.is_symlink()
            or not path.is_file()
            or not path.resolve().is_relative_to(root.resolve())
        ):
            gaps.append("A bibliography is not a regular contained file")
            continue
        if path.stat().st_size > reader.allowance:
            gaps.append("Online bibliography scan exceeded its 2 MiB/file or 16 MiB total bound")
            continue
        try:
            document = reader.read(path, path.relative_to(root).as_posix())
        except UnicodeError:
            gaps.append("A bibliography is not UTF-8")
            continue
        if document is None:
            gaps.append("A bibliography grew beyond the bounded scan size")
            continue
        if not document.valid:
            gaps.append(f"{document.path}: bibliography syntax is incomplete")
            continue
        for entry in document.entries:
            cancellation_point()
            if len(references) >= _MAX_REFERENCES:
                gaps.append("Online bibliography scan exceeded its 2000-entry bound")
                break
            references.append(_entry_reference(document, entry))
    return references


def _source_links(
    inspection: SourceGraph, options: OnlineOptions, gaps: list[str]
) -> tuple[list[Reference], bool]:
    """Configured and literal source URLs; False when the link bound cut the scan short."""
    links = [
        Reference(f"configured link {index}", None, None, "url", {"url": url})
        for index, url in enumerate(options.online_replication_urls, 1)
    ]
    if inspection.main is None or inspection.uncertainties:
        gaps.append("Literal source-link coverage is incomplete in the selected source graph")
    for name in sorted(inspection.visited):
        cancellation_point()
        source = inspection.sources.get(name)
        if source is None:
            continue
        # Preserve URL argument bytes while using the existing comment/literal masking.
        text = re.sub(
            r"\\(url|nolinkurl)\b", lambda match: "\\" + match[1].capitalize(), source.text
        )
        try:
            commands = scan_commands(mask_literals(text))
        except ParseLimitExceeded:
            gaps.append(f"{name}: URL command arguments are incomplete")
            continue
        for command in commands:
            if command.name in {"Url", "Nolinkurl", "href"} and command.arguments:
                value = command.arguments[0].value
                if any(char in value for char in "{}\\") or command.depth:
                    gaps.append(f"{name}: an indirect or grouped URL command was not requested")
                    continue
                links.append(Reference("source link", name, command.line, "url", {"url": value}))
                if len(links) >= _MAX_REFERENCES:
                    gaps.append("Source-link scan exceeded its 2000-link bound")
                    return links, False
    return links, True


def _collect(
    root: Path, main: str | None, options: OnlineOptions
) -> tuple[list[Reference], list[Reference], list[str]]:
    if not root.is_dir():
        raise PreparationError("Online reference input must be a directory")
    gaps: list[str] = []
    # Both bibliography entries and source links are scoped by the selected graph.
    inspection = SourceGraph.build(root, main)
    references = (
        _bibliography_references(root, _bibliography_paths(root, inspection, gaps), gaps)
        if options.reads_bibliography
        else []
    )
    links: list[Reference] = []
    if options.online_replication_links:
        links, complete = _source_links(inspection, options, gaps)
        if not complete:
            return references, links, gaps
    return references, links, sorted(set(gaps))


def _finding(
    rule: str,
    reference: Reference,
    message: str,
    *,
    status: Status = "inconclusive",
    severity: Severity = "warning",
    evidence: EvidenceKind = EvidenceKind.DIRECT,
    incomplete: bool = False,
    **details: Any,
) -> Finding:
    return Finding(
        rule,
        message,
        severity=severity,
        status=status,
        path=reference.path,
        line=reference.line,
        evidence_kind=evidence,
        incomplete=incomplete,
        details={"citation_key": reference.key, **details},
    )


def _fetch_details(result: Fetch) -> dict[str, Any]:
    return {
        "outcome": result.outcome.value,
        "url": result.url,
        "http_status": result.response.status if result.response else None,
        "redirects": list(result.redirects),
    }


# Link availability is decided only by these outcomes; any other is inconclusive.
_LINK_STATUS: dict[FetchOutcome, Status] = {
    FetchOutcome.AVAILABLE: "passed",
    FetchOutcome.NOT_FOUND: "failed",
    FetchOutcome.GONE: "failed",
}


async def _health(client: _Client, reference: Reference, rule: str, url: str) -> Finding:
    result = await client.fetch(url)
    status = _LINK_STATUS.get(result.outcome, "inconclusive")
    return _finding(
        rule,
        reference,
        f"URL check: {result.outcome.replace('_', ' ')}. "
        "Availability does not establish identity or reproducibility.",
        status=status,
        severity="info" if status == "passed" else "warning",
        **_fetch_details(result),
    )


def _normalized(value: str) -> str:
    return " ".join(
        re.findall(
            r"\w+",
            unicodedata.normalize("NFKC", html.unescape(re.sub(r"<[^>]*>", "", value))).casefold(),
        )
    )


def _doi_url(doi: str) -> str:
    return "https://doi.org/" + quote(doi, safe="/")


def _strings(value: Any) -> list[str]:
    if (
        not isinstance(value, list)
        or len(value) > 20
        or any(not isinstance(item, str) or len(item) > 2048 for item in value)
    ):
        return []
    return value


def _authors(value: Any) -> list[str] | None:
    if not isinstance(value, list) or len(value) > 200:
        return None
    result = []
    for author in value:
        family = author.get("family") if isinstance(author, dict) else None
        if not isinstance(family, str) or not family or len(family) > 256:
            return None
        result.append(family)
    return result


def _years(work: dict[str, Any]) -> list[str]:
    years = set()
    for key in ("issued", "published", "published-print", "published-online"):
        value = work.get(key)
        parts = value.get("date-parts") if isinstance(value, dict) else None
        if (
            isinstance(parts, list)
            and parts
            and isinstance(parts[0], list)
            and parts[0]
            and type(parts[0][0]) is int
        ):
            years.add(str(parts[0][0]))
    return sorted(years)


async def _metadata(client: _Client, url: str, *, items: bool = False) -> tuple[Any, Fetch]:
    # The transport already bounds every body by online_max_response_bytes.
    fetched = await client.fetch(url, body=True)
    if fetched.outcome != FetchOutcome.AVAILABLE or fetched.response is None:
        return None, fetched
    try:
        data = json.loads(fetched.response.body)
        message = data["message"]
        if data.get("status") != "ok" or not isinstance(message, dict):
            raise ValueError
        if items and (
            not isinstance(message.get("items"), list)
            or len(message["items"]) > 20
            or any(not isinstance(item, dict) for item in message["items"])
        ):
            raise ValueError
        if not items and (
            not isinstance(message.get("DOI"), str) or _DOI.fullmatch(message["DOI"]) is None
        ):
            raise ValueError
        return message, fetched
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        return None, Fetch(
            FetchOutcome.MALFORMED,
            fetched.url,
            fetched.response,
            fetched.redirects,
            "Crossref returned malformed metadata",
        )


def _unavailable(rule: str, reference: Reference, fetched: Fetch) -> Finding:
    return _finding(
        rule,
        reference,
        f"Crossref lookup: {fetched.outcome.replace('_', ' ')}; "
        "no metadata conclusion is available.",
        provider="Crossref",
        **_fetch_details(fetched),
    )


# Status and message of each metadata comparison outcome; {fields} lists mismatches.
_METADATA_RESULTS: dict[str, tuple[Status, str]] = {
    "mismatch": ("failed", "Metadata differences require review: {fields}"),
    "incomplete": ("inconclusive", "Metadata comparison is incomplete."),
    "agreement": ("passed", "Supported literal metadata agrees."),
}


def _compare_metadata(reference: Reference, work: dict[str, Any], source: str) -> Finding:
    comparisons, missing = {}, []
    remote_authors = _authors(work.get("author")) or []
    local_author = reference.values.get("author")
    local_authors = (
        [
            name.split(",", 1)[0].strip() if "," in name else name.split()[-1]
            for name in re.split(r"\s+and\s+", local_author)
            if name.strip()
        ]
        if local_author
        else []
    )
    pairs = {
        "title": ([reference.values.get("title")], _strings(work.get("title"))),
        "authors": (local_authors, remote_authors),
        "year": ([reference.values.get("year")], _years(work)),
        "venue": (
            [reference.values.get("journal") or reference.values.get("booktitle")],
            _strings(work.get("container-title")),
        ),
    }
    for field, (local, remote) in pairs.items():
        if (
            not local
            or not remote
            or any(not isinstance(value, str) or not value for value in [*local, *remote])
        ):
            missing.append(field)
            continue
        left, right = (
            [_normalized(value) for value in local if isinstance(value, str)],
            [_normalized(value) for value in remote if isinstance(value, str)],
        )
        matches = left == right if field == "authors" else left[0] in right
        comparisons[field] = {"local": local, "remote": remote, "matches": matches}
    mismatch = [key for key, value in comparisons.items() if not value["matches"]]
    outcome = "mismatch" if mismatch else "incomplete" if missing else "agreement"
    status, message = _METADATA_RESULTS[outcome]
    return _finding(
        "online.metadata",
        reference,
        message.format(fields=", ".join(mismatch)),
        status=status,
        severity="info" if status == "passed" else "warning",
        evidence=EvidenceKind.HEURISTIC,
        # A field without comparable values on both sides is unmeasured.
        incomplete=bool(missing),
        provider="Crossref",
        source_url=source,
        outcome=outcome,
        comparisons=comparisons,
        unmeasured_fields=missing,
        scope=(
            "Normalized literal title, author family names in order, publication year and venue; "
            "no identity proof"
        ),
    )


_NO_CANDIDATE = (
    "Crossref returned no candidate in the bounded search; this does not establish absence."
)
_PUBLISHED_CANDIDATES = (
    "Review possible published-version candidates; title similarity does not establish identity."
)
_MISSING_DOI_CANDIDATES = "Review missing-DOI candidates; no bibliography was changed."


async def _candidates(
    client: _Client, reference: Reference, rule: str, *, published: bool = False
) -> Finding:
    title = reference.values.get("title")
    if not title or len(title) > 2048:
        return _finding(
            rule,
            reference,
            "A bounded literal title is required for candidate search.",
            outcome="input_unavailable",
        )
    url = _CROSSREF + "?" + urlencode({"query.bibliographic": title, "rows": 5})
    message, fetched = await _metadata(client, url, items=True)
    if message is None:
        return _unavailable(rule, reference, fetched)
    if len(message["items"]) > 5:
        return _unavailable(rule, reference, Fetch(FetchOutcome.MALFORMED, fetched.url))
    candidates: list[dict[str, Any]] = []
    for item in message["items"]:
        doi, titles = item.get("DOI"), _strings(item.get("title"))
        if not isinstance(doi, str) or _DOI.fullmatch(doi) is None or not titles:
            return _unavailable(rule, reference, Fetch(FetchOutcome.MALFORMED, fetched.url))
        if published and item.get("type") not in {
            "journal-article",
            "proceedings-article",
            "book-chapter",
        }:
            continue
        if reference.doi and doi.casefold() == reference.doi.casefold():
            continue
        authors = _authors(item.get("author", []))
        if authors is None:
            return _unavailable(rule, reference, Fetch(FetchOutcome.MALFORMED, fetched.url))
        candidates.append(
            {
                "doi": doi,
                "url": _doi_url(doi),
                "title": titles[0],
                "authors": authors,
                "years": _years(item),
                "title_similarity": round(
                    difflib.SequenceMatcher(
                        None, _normalized(title), _normalized(titles[0])
                    ).ratio(),
                    3,
                ),
            }
        )
    candidates.sort(key=lambda item: (-item["title_similarity"], item["doi"]))
    status: Status
    if not candidates:
        outcome, status, message = "no_match", "passed", _NO_CANDIDATE
    elif published:
        # A published-version candidate is never more than a lead to review.
        outcome = "ambiguous_candidates" if len(candidates) > 1 else "candidate"
        status, message = "inconclusive", _PUBLISHED_CANDIDATES
    elif len(candidates) > 1:
        outcome, status, message = "ambiguous_candidates", "inconclusive", _MISSING_DOI_CANDIDATES
    else:
        outcome, status, message = "candidate", "failed", _MISSING_DOI_CANDIDATES
    return _finding(
        rule,
        reference,
        message,
        status=status,
        severity="info" if not candidates else "warning",
        evidence=EvidenceKind.HEURISTIC,
        outcome=outcome,
        provider="Crossref",
        source_url=fetched.url,
        candidates=candidates,
        search_limit=5,
        identity_confirmed=False,
    )


def _preprint(reference: Reference) -> bool:
    return (
        reference.kind in {"unpublished", "preprint"}
        or (reference.values.get("archiveprefix") or "").casefold() == "arxiv"
        or "arxiv.org/" in (reference.values.get("url") or "").casefold()
        or "preprint" in (reference.values.get("note") or "").casefold()
    )


async def _notices(client: _Client, reference: Reference) -> Finding:
    url = _CROSSREF + "?" + urlencode({"filter": "updates:" + str(reference.doi), "rows": 20})
    message, fetched = await _metadata(client, url, items=True)
    if message is None:
        return _unavailable("online.notices", reference, fetched)
    notices: list[dict[str, Any]] = []
    for item in message["items"]:
        updates = item.get("update-to")
        doi = item.get("DOI")
        if (
            not isinstance(updates, list)
            or len(updates) > 20
            or not isinstance(doi, str)
            or _DOI.fullmatch(doi) is None
        ):
            return _unavailable(
                "online.notices", reference, Fetch(FetchOutcome.MALFORMED, fetched.url)
            )
        matched = False
        for update in updates:
            if (
                not isinstance(update, dict)
                or not isinstance(update.get("DOI"), str)
                or _DOI.fullmatch(update["DOI"]) is None
                or not isinstance(update.get("type"), str)
                or not re.fullmatch(r"[A-Za-z][A-Za-z -]{0,127}", update["type"])
            ):
                return _unavailable(
                    "online.notices", reference, Fetch(FetchOutcome.MALFORMED, fetched.url)
                )
            if update["DOI"].casefold() == str(reference.doi).casefold():
                if len(notices) >= 20:
                    return _unavailable(
                        "online.notices", reference, Fetch(FetchOutcome.RESPONSE_LIMIT, fetched.url)
                    )
                matched = True
                notices.append(
                    {
                        "type": update["type"],
                        "notice_doi": doi,
                        "notice_url": _doi_url(doi),
                        "updated_doi": reference.doi,
                    }
                )
        if not matched:
            return _unavailable(
                "online.notices", reference, Fetch(FetchOutcome.IDENTIFIER_MISMATCH, fetched.url)
            )
    truncated = isinstance(message.get("total-results"), int) and message["total-results"] > len(
        message["items"]
    )
    types = sorted({item["type"] for item in notices})
    return _finding(
        "online.notices",
        reference,
        "Crossref reports update notices: " + ", ".join(types) + ". Read each attributed notice."
        if notices
        else "No Crossref update notice was returned; absence outside this provider is unverified.",
        status="inconclusive" if truncated else "failed" if notices else "passed",
        severity="warning" if notices or truncated else "info",
        incomplete=truncated,
        provider="Crossref",
        source_url=fetched.url,
        outcome="notices" if notices else "no_notice_found",
        notices=notices,
        truncated=truncated,
        coverage=(
            "Crossref records returned by the updates DOI filter; "
            "not an exhaustive retraction registry"
        ),
    )


async def _check_doi_resolution(client: _Client, reference: Reference) -> Finding:
    if reference.doi:
        return await _health(client, reference, "online.doi_resolution", _doi_url(reference.doi))
    return _finding(
        "online.doi_resolution",
        reference,
        "DOI is invalid, ambiguous or nonliteral; no request was made.",
        outcome="input_unavailable",
    )


async def _crossref_work(client: _Client, reference: Reference) -> tuple[Any, Fetch]:
    """The Crossref record of the reference's DOI, or None with the failed fetch."""
    if not reference.doi:
        return None, Fetch(FetchOutcome.INPUT_UNAVAILABLE, "")
    work, fetched = await _metadata(client, _CROSSREF + "/" + quote(reference.doi, safe=""))
    if work is not None and work["DOI"].casefold() != reference.doi.casefold():
        return None, Fetch(FetchOutcome.IDENTIFIER_MISMATCH, fetched.url)
    return work, fetched


def _check_metadata(reference: Reference, work: Any, fetched: Fetch) -> Finding:
    if work is not None:
        return _compare_metadata(reference, work, fetched.url)
    if reference.doi:
        return _unavailable("online.metadata", reference, fetched)
    return _finding(
        "online.metadata",
        reference,
        "A supported literal DOI is required for metadata comparison.",
        outcome="input_unavailable",
    )


async def _check_published_version(
    client: _Client, reference: Reference, work: Any, fetched: Fetch
) -> Finding:
    relation = work.get("relation", {}) if work else {}
    related = relation.get("is-preprint-of", []) if isinstance(relation, dict) else []
    valid_related = (
        [
            item
            for item in related
            if isinstance(item, dict)
            and item.get("id-type") == "doi"
            and isinstance(item.get("id"), str)
            and _DOI.fullmatch(item["id"])
        ]
        if isinstance(related, list) and len(related) <= 20
        else []
    )
    if related and (not isinstance(related, list) or len(valid_related) != len(related)):
        return _unavailable(
            "online.published_version", reference, Fetch(FetchOutcome.MALFORMED, fetched.url)
        )
    if valid_related:
        return _finding(
            "online.published_version",
            reference,
            "Crossref declares a published-version relation; review before changing the citation.",
            status="failed",
            provider="Crossref",
            source_url=fetched.url,
            outcome="declared_relation",
            relations=[
                {
                    "doi": item["id"],
                    "url": _doi_url(item["id"]),
                    "asserted_by": item.get("asserted-by")
                    if item.get("asserted-by") in ("subject", "object")
                    else None,
                }
                for item in valid_related
            ],
        )
    if reference.doi and work is None:
        return _unavailable("online.published_version", reference, fetched)
    return await _candidates(client, reference, "online.published_version", published=True)


async def _check_notices(client: _Client, reference: Reference) -> Finding:
    if reference.doi:
        return await _notices(client, reference)
    return _finding(
        "online.notices",
        reference,
        "A supported literal DOI is required for notice lookup.",
        outcome="input_unavailable",
    )


async def _check_reference_link(client: _Client, reference: Reference) -> Finding:
    value = reference.values["url"]
    if value:
        return await _health(client, reference, "online.reference_link", value)
    return _finding(
        "online.reference_link",
        reference,
        "Reference URL is ambiguous or nonliteral; no request was made.",
        outcome="input_unavailable",
    )


async def _check_reference(client: _Client, reference: Reference) -> list[Finding]:
    """Run the selected checks for one entry, in a fixed order that spends requests."""
    options, result = client.options, []
    has_doi_field, preprint = "doi" in reference.values, _preprint(reference)
    if options.online_doi_resolution and has_doi_field:
        result.append(await _check_doi_resolution(client, reference))
    # One Crossref record serves both the metadata and the published-version checks.
    work, fetched = None, Fetch(FetchOutcome.INPUT_UNAVAILABLE, "")
    if options.online_metadata or (options.online_published_versions and preprint):
        work, fetched = await _crossref_work(client, reference)
    if options.online_metadata and has_doi_field:
        result.append(_check_metadata(reference, work, fetched))
    if options.online_missing_doi and not has_doi_field:
        result.append(await _candidates(client, reference, "online.missing_doi"))
    if options.online_published_versions and preprint:
        result.append(await _check_published_version(client, reference, work, fetched))
    if options.online_notices and has_doi_field:
        result.append(await _check_notices(client, reference))
    if options.online_reference_links and "url" in reference.values:
        result.append(await _check_reference_link(client, reference))
    return result


_DEFAULT_OPTIONS = OnlineOptions()


async def check_online_references(
    root: Path,
    main: str | None = None,
    *,
    options: OnlineOptions = _DEFAULT_OPTIONS,
    budget: ResourceBudget | None = None,
) -> list[Finding]:
    """Opt-in only: share DOI/URL identifiers and titles, never whole source files.

    Entries come from the bibliography resources the selected literal source graph
    declares, so spare copies outside the manuscript spend no request; a directory
    without any document root falls back to every local .bib file. Source links use
    the same selected graph. Transport availability and metadata agreement are
    separate checks. A request failure is advisory/inconclusive, never a fabricated
    metadata match, and the bounded request budget is reported with the findings.
    """
    enabled = options.selected_rules
    if not options.online:
        return [
            Finding(
                rule,
                "Selected online check was not run: network permission is disabled.",
                severity="info",
                status="skipped",
                suggestion="Use --online to allow the selected requests, or deselect this check.",
                details={"outcome": "offline", "requests": 0},
            )
            for rule in enabled
        ]
    if not enabled:
        return []
    sharing = Finding(
        "online.sharing",
        "Explicit online checks share DOI identifiers, URL destinations and bibliography titles "
        "with DOI/Crossref services and selected public destination hosts. "
        "No bibliography or manuscript file is uploaded.",
        severity="info",
        status="passed",
        details={
            "checks": enabled,
            "shared_fields": ["DOI", "URL", "bibliographic title"],
            "offline_default": True,
        },
    )
    reservation = (
        budget.lease("collect online reference inputs", memory_mb=128)
        if budget
        else contextlib.nullcontext()
    )
    async with reservation:
        references, links, gaps = await run_in_thread(_collect, root, main, options)
    client = _Client(options, budget)
    operations: list[Callable[[], Awaitable[list[Finding]]]] = [
        lambda reference=reference: _check_reference(client, reference) for reference in references
    ]

    async def link_check(reference: Reference) -> list[Finding]:
        return [
            await _health(
                client, reference, "online.replication_link", str(reference.values["url"])
            )
        ]

    operations.extend(lambda link=link: link_check(link) for link in links)

    async def run(operation: Callable[[], Awaitable[list[Finding]]]) -> list[Finding]:
        return await operation()

    collected = await bounded_map(
        operations,
        run,
        limit=min(options.online_jobs, budget.cpu_slots if budget else options.online_jobs),
    )
    findings = [sharing, *(finding for group in collected for finding in group)]
    for rule in enabled:
        if gaps:
            findings.append(
                Finding(
                    rule,
                    "Online input coverage is incomplete; uninspected inputs were not requested.",
                    severity="warning",
                    status="inconclusive",
                    details={"outcome": "input_coverage_incomplete", "reasons": gaps},
                )
            )
        elif not any(finding.rule == rule for finding in findings):
            findings.append(
                Finding(
                    rule,
                    "No applicable literal input was found for this selected check.",
                    severity="info",
                    status="skipped",
                    details={"outcome": "not_applicable"},
                )
            )
    findings.append(
        Finding(
            "online.requests",
            "Bounded online requests completed; observations are time-specific.",
            severity="info",
            status="passed",
            details={
                "requests": client.requests,
                "maximum": options.online_max_requests,
                "provider": "Crossref/DOI and explicitly selected public URLs",
            },
        )
    )
    return sorted(
        findings,
        key=lambda finding: (finding.path or "", finding.line or 0, finding.rule, finding.message),
    )
