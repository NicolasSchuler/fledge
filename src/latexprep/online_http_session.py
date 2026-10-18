"""Bounded requests to public hosts: connection, pacing, budget, redirects and caching.

This module owns everything between "fetch this URL" and a finished ``Fetch``: connecting
to one validated address with TLS that checks the original host name (``open_pinned``),
one request/response exchange (``exchange``), and ``OnlineClient``, which spends the
job's request budget, spaces requests to one host, follows a bounded number of
redirects, asks again with GET when HEAD is refused, and caches finished results. No
failure escapes as an exception; each becomes a ``Fetch`` with an outcome, a reason and
the diagnostics (host, elapsed time, error code) that explain it.

Two seams keep the network out of tests: the ``resolver`` that names a host's addresses
and the ``transport`` that performs one exchange with one address. The wire format is in
``online_http_wire`` and the address policy in ``public_url``; the meaning of an outcome
for a finding is ``online_checks``'s.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import socket
import ssl
import time
from collections import defaultdict
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass
from functools import cache
from typing import NamedTuple
from urllib.parse import urljoin

from .limits import DEFAULT_LEASE_MEMORY_MB, IO_CHUNK_BYTES, MIB
from .online_http_wire import Response, build_request, read_response
from .online_options import DEFAULT_USER_AGENT, FetchOutcome, NetworkFailure, OnlineOptions
from .pinned_dns import JobResolver, Resolver
from .public_url import Target, parse_public_url, redact_url, require_public_ip
from .resource_budget import ResourceBudget

# A HEAD answer with one of these may only reflect a server that handles HEAD badly,
# so the same URL is asked again with GET before the result is reported.
_HEAD_RETRY_STATUSES = frozenset({403, 404, 405, 501})
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_HTTP_OUTCOMES = {
    401: FetchOutcome.AUTHENTICATION_REQUIRED,
    403: FetchOutcome.ACCESS_DENIED,
    404: FetchOutcome.NOT_FOUND,
    410: FetchOutcome.GONE,
    429: FetchOutcome.RATE_LIMITED,
}
# Validated addresses tried in turn for one request; with more than one, an attempt to
# connect may use at most this share of the request's timeout.
_MAX_ADDRESS_ATTEMPTS = 4
_CONNECT_TIMEOUT_SHARE = 0.5
# All requests of one fetch (redirect hops and the GET after a refused HEAD) together may
# use this multiple of the timeout of a single request.
FETCH_TIMEOUT_MULTIPLE = 3
# Finished results kept for repeated URLs: at most this many, holding this many body bytes.
_CACHE_MAX_ENTRIES = 128
_CACHE_MAX_BODY_BYTES = 4 * MIB


class ConnectFailure(NetworkFailure):
    """The TCP connection to one validated address failed; another address may work."""

    def __init__(self, message: str = "Could not connect to the server") -> None:
        super().__init__(FetchOutcome.UNAVAILABLE, message)


class ExchangeRequest(NamedTuple):
    """Everything one exchange needs: where to connect, what to ask, and the bounds."""

    target: Target
    address: str
    method: str
    want_body: bool
    max_body_bytes: int
    user_agent: str = DEFAULT_USER_AGENT
    connect_timeout: float | None = None


Transport = Callable[[ExchangeRequest], Awaitable[Response]]


@cache
def default_ssl_context() -> ssl.SSLContext:
    """The default verifying TLS context, built once: certificate and host name are checked."""
    return ssl.create_default_context()


async def open_pinned(
    target: Target,
    address: str,
    *,
    ssl_context: ssl.SSLContext | None,
    connect_timeout: float | None = None,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Connect to the numeric ``address``; TLS, if any, verifies the host name of ``target``.

    A failed or timed-out connection raises :class:`ConnectFailure`, so the caller can
    try another address of the same host.
    """
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setblocking(False)
    try:
        try:
            async with asyncio.timeout(connect_timeout):
                await asyncio.get_running_loop().sock_connect(sock, (address, target.port))
        except (OSError, TimeoutError) as error:
            raise ConnectFailure from error
        return await asyncio.open_connection(
            sock=sock,
            ssl=ssl_context,
            server_hostname=target.host if ssl_context else None,
            limit=IO_CHUNK_BYTES,
        )
    except BaseException:
        sock.close()
        raise


async def exchange(request: ExchangeRequest) -> Response:
    """Send one request to its address and read the bounded response (the real transport)."""
    address = require_public_ip(request.address)
    context = default_ssl_context() if request.target.scheme == "https" else None
    reader, writer = await open_pinned(
        request.target, address, ssl_context=context, connect_timeout=request.connect_timeout
    )
    try:
        writer.write(build_request(request.method, request.target, request.user_agent))
        await writer.drain()
        head_only = not request.want_body or request.method == "HEAD"
        return await read_response(reader, head_only=head_only, limit=request.max_body_bytes)
    finally:
        writer.close()
        # Closing the transport is synchronous; do not let peer shutdown stall cleanup.
        if writer.transport is not None:
            writer.transport.abort()


@dataclass(frozen=True)
class Fetch:
    """How one URL fetch ended.

    ``message`` says why a fetch failed, in words for a report; ``error_code`` is the
    operating-system or TLS code behind it (``ECONNRESET``, ``CERTIFICATE_VERIFY_FAILED``)
    and ``elapsed_seconds`` the time its network requests took.
    """

    outcome: FetchOutcome
    url: str
    response: Response | None = None
    redirects: tuple[str, ...] = ()
    message: str = ""
    host: str | None = None
    elapsed_seconds: float = 0.0
    error_code: str | None = None


def _http_outcome(status: int) -> FetchOutcome:
    if 200 <= status < 300:
        return FetchOutcome.AVAILABLE
    return _HTTP_OUTCOMES.get(status, FetchOutcome.UNAVAILABLE)


def _tls_reason(error: ssl.SSLError) -> str:
    if isinstance(error, ssl.SSLCertVerificationError) and error.verify_message:
        return f"The server's certificate could not be verified ({error.verify_message})"
    return "The secure (TLS) connection could not be established"


def _transport_reason(error: Exception) -> str:
    detail = type(error).__name__
    if isinstance(error, OSError) and error.strerror:
        detail += f": {error.strerror}"
    return f"The network connection failed ({detail})"


def _error_code(error: BaseException) -> str | None:
    """The TLS reason or errno name behind ``error`` or the exception it was raised from."""
    cause = error if isinstance(error, OSError) else error.__cause__
    if isinstance(cause, ssl.SSLError):
        # An error built by hand rather than by the ssl module has no reason.
        return getattr(cause, "reason", None)
    if isinstance(cause, OSError) and cause.errno is not None:
        return errno.errorcode.get(cause.errno)
    return None


class _BoundedCache:
    """Finished fetches by request; the oldest go first when entries or body bytes run over."""

    def __init__(self, max_entries: int, max_body_bytes: int) -> None:
        self._max_entries = max_entries
        self._max_body_bytes = max_body_bytes
        self._items: dict[tuple[str, bool], Fetch] = {}
        self._body_bytes = 0

    @staticmethod
    def _size(item: Fetch) -> int:
        return len(item.response.body) if item.response else 0

    def get(self, key: tuple[str, bool]) -> Fetch | None:
        return self._items.get(key)

    def values(self) -> list[Fetch]:
        """The cached fetches, oldest first."""
        return list(self._items.values())

    def put(self, key: tuple[str, bool], item: Fetch) -> None:
        self._items[key] = item
        self._body_bytes += self._size(item)
        while len(self._items) > self._max_entries or self._body_bytes > self._max_body_bytes:
            removed = self._items.pop(next(iter(self._items)))
            self._body_bytes -= self._size(removed)


class _Allowance:
    """The network time one fetch may still use, shared by all of its requests."""

    def __init__(self, seconds: float) -> None:
        self.total = seconds
        self.remaining = seconds

    @property
    def spent(self) -> float:
        return self.total - self.remaining


class _Trail:
    """Where a fetch has got to: the URL being asked, the redirects so far and the host."""

    def __init__(self, url: str) -> None:
        self.current = url
        self.redirects: list[str] = []
        self.host: str | None = None


class OnlineClient:
    """Fetches URLs within one job's bounds; see the module documentation."""

    def __init__(
        self,
        options: OnlineOptions,
        budget: ResourceBudget | None = None,
        *,
        resolver: Resolver | None = None,
        transport: Transport | None = None,
    ) -> None:
        self.options = options
        self.budget = budget
        self.requests = 0
        self.cache = _BoundedCache(_CACHE_MAX_ENTRIES, _CACHE_MAX_BODY_BYTES)
        self._resolve: Resolver = resolver or JobResolver()
        self._transport: Transport = transport or exchange
        # One lock per provider host serializes its requests and spacing.
        self._host_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._next_request: dict[str, float] = {}
        self._throttled: set[str] = set()
        # Concurrent identical fetches share one result: later ones wait on the lock of
        # the first. A lock lives only while some fetch uses it.
        self._in_flight: dict[tuple[str, bool], tuple[asyncio.Lock, int]] = {}

    @property
    def in_flight(self) -> int:
        """The number of distinct fetches in progress; zero when the client is idle."""
        return len(self._in_flight)

    @contextlib.asynccontextmanager
    async def _exclusive(self, key: tuple[str, bool]) -> AsyncGenerator[None]:
        lock, users = self._in_flight.get(key, (asyncio.Lock(), 0))
        self._in_flight[key] = (lock, users + 1)
        try:
            async with lock:
                yield
        finally:
            lock, users = self._in_flight[key]
            if users == 1:
                del self._in_flight[key]
            else:
                self._in_flight[key] = (lock, users - 1)

    async def fetch(self, url: str, *, want_body: bool = False) -> Fetch:
        """Ask ``url`` with HEAD, or with GET and a bounded body when ``want_body`` is set."""
        key = (url, want_body)
        async with self._exclusive(key):
            cached = self.cache.get(key)
            if cached is not None:
                return cached
            result = await self._fetch_uncached(url, want_body)
            self.cache.put(key, result)
            return result

    async def _fetch_uncached(self, url: str, want_body: bool) -> Fetch:
        trail = _Trail(url)
        allowance = _Allowance(self.options.online_timeout_seconds * FETCH_TIMEOUT_MULTIPLE)
        try:
            return await self._follow_redirects(trail, want_body, allowance)
        except NetworkFailure as error:
            return self._failed(trail, allowance, error.outcome, str(error), error)
        except TimeoutError as error:
            return self._failed(
                trail, allowance, FetchOutcome.TIMEOUT, self._timeout_reason(allowance), error
            )
        except ssl.SSLError as error:
            return self._failed(
                trail, allowance, FetchOutcome.TLS_FAILURE, _tls_reason(error), error
            )
        except (OSError, ValueError, asyncio.IncompleteReadError) as error:
            return self._failed(
                trail, allowance, FetchOutcome.UNAVAILABLE, _transport_reason(error), error
            )

    def _timeout_reason(self, allowance: _Allowance) -> str:
        seconds = self.options.online_timeout_seconds
        if allowance.remaining <= 0:
            return (
                f"The redirects and retries of this address used more than "
                f"{allowance.total:g} seconds in total"
            )
        return f"No complete answer arrived within {seconds} seconds"

    @staticmethod
    def _failed(
        trail: _Trail,
        allowance: _Allowance,
        outcome: FetchOutcome,
        reason: str,
        error: BaseException,
    ) -> Fetch:
        return Fetch(
            outcome,
            redact_url(trail.current),
            redirects=tuple(trail.redirects),
            message=reason,
            host=trail.host,
            elapsed_seconds=allowance.spent,
            error_code=_error_code(error),
        )

    async def _follow_redirects(
        self, trail: _Trail, want_body: bool, allowance: _Allowance
    ) -> Fetch:
        visited: set[str] = set()
        for _ in range(self.options.online_max_redirects + 1):
            target = parse_public_url(trail.current)
            trail.host = target.host
            if target.url in visited:
                raise NetworkFailure(FetchOutcome.REDIRECT_LOOP, "HTTP redirect loop detected")
            visited.add(target.url)
            response = await self._ask(target, want_body, allowance)
            if response.status not in _REDIRECT_STATUSES:
                return Fetch(
                    _http_outcome(response.status),
                    redact_url(target.url),
                    response,
                    tuple(trail.redirects),
                    host=target.host,
                    elapsed_seconds=allowance.spent,
                )
            trail.current = self._redirect_destination(target, response, want_body)
            trail.redirects.append(redact_url(trail.current))
        raise NetworkFailure(FetchOutcome.REDIRECT_LIMIT, "HTTP redirect limit exhausted")

    @staticmethod
    def _redirect_destination(target: Target, response: Response, want_body: bool) -> str:
        location = response.header("location")
        if not location:
            raise NetworkFailure(FetchOutcome.MALFORMED, "Redirect omitted its destination")
        destination = urljoin(target.url, location)
        # Validate even the final over-budget redirect without contacting it.
        checked = parse_public_url(destination)
        if want_body and target.scheme == "https" and checked.scheme == "http":
            raise NetworkFailure(
                FetchOutcome.UNSAFE_TARGET,
                "A metadata request was redirected from HTTPS to plain HTTP",
            )
        return destination

    async def _ask(self, target: Target, want_body: bool, allowance: _Allowance) -> Response:
        """One URL: HEAD, or GET with a body, and a GET when the HEAD was refused."""
        method = "GET" if want_body else "HEAD"
        response = await self._one(target, method, want_body, allowance)
        if not want_body and response.status in _HEAD_RETRY_STATUSES:
            response = await self._one(target, "GET", False, allowance)
        return response

    def _budget_exhausted(self) -> NetworkFailure:
        return NetworkFailure(
            FetchOutcome.REQUEST_LIMIT,
            f"The budget of {self.options.online_max_requests} online requests is used up",
        )

    def _lease(self) -> contextlib.AbstractAsyncContextManager[object]:
        if self.budget is None:
            return contextlib.nullcontext()
        return self.budget.lease("online request", memory_mb=DEFAULT_LEASE_MEMORY_MB)

    async def _one(
        self, target: Target, method: str, want_body: bool, allowance: _Allowance
    ) -> Response:
        host = target.host
        async with self._host_locks[host]:
            if host in self._throttled:
                raise NetworkFailure(
                    FetchOutcome.RATE_LIMITED,
                    "Provider throttled an earlier request; no automatic retry",
                )
            await asyncio.sleep(max(0.0, self._next_request.get(host, 0.0) - time.monotonic()))
            if self.requests >= self.options.online_max_requests:
                raise self._budget_exhausted()
            async with self._lease():
                # Different providers may have waited for the same shared resource slot.
                # Admission and increment must not be separated by an await.
                if self.requests >= self.options.online_max_requests:
                    raise self._budget_exhausted()
                self.requests += 1
                interval = self.options.online_provider_interval_seconds
                self._next_request[host] = time.monotonic() + interval
                response = await self._over_network(target, method, want_body, allowance)
                if response.status == 429:
                    self._throttled.add(host)
                return response

    async def _over_network(
        self, target: Target, method: str, want_body: bool, allowance: _Allowance
    ) -> Response:
        limit = min(self.options.online_timeout_seconds, allowance.remaining)
        if limit <= 0:
            raise TimeoutError
        started = time.monotonic()
        try:
            async with asyncio.timeout(limit):
                addresses = await self._resolve(target.host, target.port)
                # Recheck injected/resolver answers; each request pins one validated address.
                addresses = tuple(require_public_ip(address) for address in addresses)
                return await self._first_reachable(target, addresses, method, want_body)
        finally:
            allowance.remaining -= time.monotonic() - started

    async def _first_reachable(
        self, target: Target, addresses: tuple[str, ...], method: str, want_body: bool
    ) -> Response:
        """Ask the first validated address that accepts a connection.

        Only a failed connection moves on to the next address; IPv4 goes first because
        a missing IPv6 route is the commoner failure, and the first connection attempt
        gets a share of the timeout so that a silent address cannot use all of it.
        """
        ordered = sorted(addresses, key=lambda address: ":" in address)[:_MAX_ADDRESS_ATTEMPTS]
        connect_timeout = (
            self.options.online_timeout_seconds * _CONNECT_TIMEOUT_SHARE
            if len(ordered) > 1
            else None
        )
        failure: NetworkFailure | None = None
        for address in ordered:
            request = ExchangeRequest(
                target,
                address,
                method,
                want_body,
                self.options.online_max_response_bytes,
                self.options.user_agent_for(target.host),
                connect_timeout,
            )
            try:
                return await self._transport(request)
            except ConnectFailure as error:
                failure = error
        if failure is None:
            raise NetworkFailure(FetchOutcome.UNAVAILABLE, "DNS returned no public addresses")
        if len(ordered) > 1:
            raise ConnectFailure(
                f"Could not connect to any of the server's {len(ordered)} addresses"
            ) from failure
        raise failure
