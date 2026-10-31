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
import logging
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

# Log lines name the host and the outcome only: a URL can carry a credential or a query
# secret, and the contact address of the user agent is never logged.
logger = logging.getLogger(__name__)

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
    """The TCP connection to one validated address failed; another address may work.

    The outcome is always ``FetchOutcome.UNAVAILABLE``.

    Args:
        message: Why the connection failed, in words for a report.
    """

    def __init__(self, message: str = "Could not connect to the server") -> None:
        super().__init__(FetchOutcome.UNAVAILABLE, message)


class ExchangeRequest(NamedTuple):
    """Everything one exchange needs: where to connect, what to ask, and the bounds.

    Attributes:
        target: The URL's host, port, scheme and path.
        address: The validated numeric address to connect to.
        method: The request method, ``GET`` or ``HEAD``.
        want_body: Whether to read the body of a 2xx response.
        max_body_bytes: The largest accepted body.
        user_agent: The ``User-Agent`` header value.
        connect_timeout: The longest a connection attempt may take, or ``None`` for no
            separate limit.
    """

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
    """Return the default verifying TLS context, built once; it checks certificate and host name."""
    return ssl.create_default_context()


async def open_pinned(
    target: Target,
    address: str,
    *,
    ssl_context: ssl.SSLContext | None,
    connect_timeout: float | None = None,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Connect to the numeric ``address``; TLS, if any, verifies the host name of ``target``.

    Args:
        target: The URL's host, which TLS verifies, and port.
        address: The numeric IPv4 or IPv6 address to connect to.
        ssl_context: The TLS context, or ``None`` for a plain connection.
        connect_timeout: The longest the TCP connection may take, or ``None``.

    Returns:
        The reader and writer of the connection.

    Raises:
        ConnectFailure: The connection failed or timed out, so the caller can try another
            address of the same host.
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
    """Send one request to its address and read the bounded response (the real transport).

    HTTPS uses the default verifying TLS context, and the connection is aborted
    afterwards.

    Args:
        request: The exchange to perform.

    Returns:
        The response, with its body when one was wanted and the status is 2xx.

    Raises:
        ConnectFailure: The connection failed or timed out.
        NetworkFailure: The address is not public, or the response is malformed or over a
            bound.
    """
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

    Attributes:
        outcome: How the fetch ended.
        url: The last URL asked, redacted.
        response: The final response, or ``None`` when the fetch failed.
        redirects: The redirect destinations followed, redacted.
        message: Why a fetch failed, in words for a report.
        host: The host of the last URL asked, or ``None``.
        elapsed_seconds: The time its network requests took.
        error_code: The operating-system or TLS code behind a failure (``ECONNRESET``,
            ``CERTIFICATE_VERIFY_FAILED``).
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
    """Classify a final HTTP status; an unlisted error status is unavailable."""
    if 200 <= status < 300:
        return FetchOutcome.AVAILABLE
    return _HTTP_OUTCOMES.get(status, FetchOutcome.UNAVAILABLE)


def _tls_reason(error: ssl.SSLError) -> str:
    """Explain a TLS failure, quoting the certificate verification message if there is one."""
    if isinstance(error, ssl.SSLCertVerificationError) and error.verify_message:
        return f"The server's certificate could not be verified ({error.verify_message})"
    return "The secure (TLS) connection could not be established"


def _transport_reason(error: Exception) -> str:
    """Explain a failed network connection by the error's type and system message."""
    detail = type(error).__name__
    if isinstance(error, OSError) and error.strerror:
        detail += f": {error.strerror}"
    return f"The network connection failed ({detail})"


def _error_code(error: BaseException) -> str | None:
    """Name the TLS reason or errno behind ``error`` or the exception it was raised from."""
    cause = error if isinstance(error, OSError) else error.__cause__
    if isinstance(cause, ssl.SSLError):
        # An error built by hand rather than by the ssl module has no reason.
        return getattr(cause, "reason", None)
    if isinstance(cause, OSError) and cause.errno is not None:
        return errno.errorcode.get(cause.errno)
    return None


class _BoundedCache:
    """Finished fetches by request; the oldest go first when entries or body bytes run over.

    Args:
        max_entries: The most fetches kept.
        max_body_bytes: The most response body bytes kept, over all fetches.
    """

    def __init__(self, max_entries: int, max_body_bytes: int) -> None:
        self._max_entries = max_entries
        self._max_body_bytes = max_body_bytes
        self._items: dict[tuple[str, bool], Fetch] = {}
        self._body_bytes = 0

    @staticmethod
    def _size(item: Fetch) -> int:
        """Return the body bytes a cached fetch holds."""
        return len(item.response.body) if item.response else 0

    def get(self, key: tuple[str, bool]) -> Fetch | None:
        """Return the cached fetch for ``key``, or ``None``."""
        return self._items.get(key)

    def values(self) -> list[Fetch]:
        """List the cached fetches, oldest first."""
        return list(self._items.values())

    def put(self, key: tuple[str, bool], item: Fetch) -> None:
        """Cache ``item`` under ``key``, then drop the oldest entries until within bounds.

        Args:
            key: The URL and whether a body was wanted.
            item: The finished fetch.
        """
        self._items[key] = item
        self._body_bytes += self._size(item)
        while len(self._items) > self._max_entries or self._body_bytes > self._max_body_bytes:
            removed = self._items.pop(next(iter(self._items)))
            self._body_bytes -= self._size(removed)


class _Allowance:
    """The network time one fetch may still use, shared by all of its requests.

    Args:
        seconds: The network time the fetch may use, the start of ``total`` and
            ``remaining``.

    Attributes:
        total: The seconds the fetch may use in all.
        remaining: The seconds still left.
    """

    def __init__(self, seconds: float) -> None:
        self.total = seconds
        self.remaining = seconds

    @property
    def spent(self) -> float:
        """The seconds used so far."""
        return self.total - self.remaining


class _Trail:
    """Where a fetch has got to: the URL being asked, the redirects so far and the host.

    Args:
        url: The URL the fetch starts with, the first ``current``.

    Attributes:
        current: The URL being asked.
        redirects: The redirect destinations so far, redacted.
        host: The host of the current URL, once it has been validated.
    """

    def __init__(self, url: str) -> None:
        self.current = url
        self.redirects: list[str] = []
        self.host: str | None = None


class OnlineClient:
    """Fetches URLs within one job's bounds; see the module documentation.

    Args:
        options: The online options, kept as ``options``.
        budget: The job's resource budget, kept as ``budget``.
        resolver: Names a host's addresses; defaults to the job's DNS resolver. Tests
            replace it to keep the network out.
        transport: Performs one exchange with one address; defaults to :func:`exchange`.
            Tests replace it to keep the network out.

    Attributes:
        options: The online options, which set the budget, timeouts, spacing and limits.
        budget: The job's resource budget that each request leases from, or ``None``.
        requests: The requests made so far.
        cache: The finished fetches kept for repeated URLs.
    """

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
        """Hold the lock of one fetch key, so that identical fetches run one at a time."""
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
        """Ask ``url`` with HEAD, or with GET and a bounded body when ``want_body`` is set.

        Concurrent identical fetches share one result, and finished results are cached.

        Args:
            url: The URL to fetch.
            want_body: Whether to read the response body.

        Returns:
            How the fetch ended; a failure is a result, never an exception.
        """
        key = (url, want_body)
        async with self._exclusive(key):
            cached = self.cache.get(key)
            if cached is not None:
                logger.debug("online %s: answered from the cache", "GET" if want_body else "HEAD")
                return cached
            result = await self._fetch_uncached(url, want_body=want_body)
            self.cache.put(key, result)
            return result

    async def _fetch_uncached(self, url: str, *, want_body: bool) -> Fetch:
        """Fetch ``url`` over the network, turning every network failure into a result.

        Args:
            url: The URL to fetch.
            want_body: Whether to read the response body.

        Returns:
            How the fetch ended.
        """
        trail = _Trail(url)
        allowance = _Allowance(self.options.online_timeout_seconds * FETCH_TIMEOUT_MULTIPLE)
        try:
            return await self._follow_redirects(trail, want_body=want_body, allowance=allowance)
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
        """Explain a timeout: one request too slow, or the whole fetch over its allowance."""
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
        """Log a failed fetch and describe it as a result.

        Args:
            trail: Where the fetch had got to.
            allowance: The fetch's network time, whose spent part is reported.
            outcome: How the fetch failed.
            reason: Why, in words for a report.
            error: The error behind the failure, which supplies the error code.

        Returns:
            The failed fetch.
        """
        logger.debug(
            "online %s: fetch failed as %s (%s) after %.2f s",
            trail.host or "unknown host",
            outcome,
            _error_code(error) or type(error).__name__,
            allowance.spent,
        )
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
        self, trail: _Trail, *, want_body: bool, allowance: _Allowance
    ) -> Fetch:
        """Ask the trail's URL and follow its redirects, up to the configured limit.

        Args:
            trail: Where the fetch has got to; it is updated with each redirect.
            want_body: Whether to read the final response body.
            allowance: The fetch's remaining network time.

        Returns:
            How the fetch ended at the first response that is not a redirect.

        Raises:
            NetworkFailure: A URL is unsafe, a redirect loops, is malformed or unsafe, the
                redirect limit or request budget is exhausted, the host throttled, or a
                response is malformed.
            TimeoutError: A request or the fetch ran out of time.
        """
        visited: set[str] = set()
        for _ in range(self.options.online_max_redirects + 1):
            target = parse_public_url(trail.current)
            trail.host = target.host
            if target.url in visited:
                raise NetworkFailure(FetchOutcome.REDIRECT_LOOP, "HTTP redirect loop detected")
            visited.add(target.url)
            response = await self._ask(target, want_body=want_body, allowance=allowance)
            if response.status not in _REDIRECT_STATUSES:
                outcome = _http_outcome(response.status)
                logger.debug(
                    "online %s: fetch ended as %s after %d redirect(s), %.2f s",
                    target.host,
                    outcome,
                    len(trail.redirects),
                    allowance.spent,
                )
                return Fetch(
                    outcome,
                    redact_url(target.url),
                    response,
                    tuple(trail.redirects),
                    host=target.host,
                    elapsed_seconds=allowance.spent,
                )
            trail.current = self._redirect_destination(target, response, want_body=want_body)
            trail.redirects.append(redact_url(trail.current))
        raise NetworkFailure(FetchOutcome.REDIRECT_LIMIT, "HTTP redirect limit exhausted")

    @staticmethod
    def _redirect_destination(target: Target, response: Response, *, want_body: bool) -> str:
        """Resolve and validate the destination of a redirect.

        Args:
            target: The URL that answered with the redirect.
            response: The redirect response.
            want_body: Whether a metadata body is wanted, which forbids a redirect from
                HTTPS to plain HTTP.

        Returns:
            The absolute destination URL.

        Raises:
            NetworkFailure: The redirect has no destination, the destination is not a
                public URL, or it downgrades a metadata request to plain HTTP.
        """
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

    async def _ask(self, target: Target, *, want_body: bool, allowance: _Allowance) -> Response:
        """Ask one URL: HEAD, or GET with a body, and a GET when the HEAD was refused."""
        method = "GET" if want_body else "HEAD"
        response = await self._one(target, method, want_body=want_body, allowance=allowance)
        if not want_body and response.status in _HEAD_RETRY_STATUSES:
            response = await self._one(target, "GET", want_body=False, allowance=allowance)
        return response

    def _budget_exhausted(self) -> NetworkFailure:
        """Build the failure for a used-up request budget."""
        return NetworkFailure(
            FetchOutcome.REQUEST_LIMIT,
            f"The budget of {self.options.online_max_requests} online requests is used up",
        )

    def _lease(self) -> contextlib.AbstractAsyncContextManager[object]:
        """Lease the resources of one request from the job's budget, if there is one."""
        if self.budget is None:
            return contextlib.nullcontext()
        return self.budget.lease("online request", memory_mb=DEFAULT_LEASE_MEMORY_MB)

    async def _one(
        self, target: Target, method: str, *, want_body: bool, allowance: _Allowance
    ) -> Response:
        """Make one request, spaced from the last request to the same host and counted.

        A host that answered HTTP 429 is not asked again.

        Args:
            target: The URL to ask.
            method: The request method.
            want_body: Whether to read the response body.
            allowance: The fetch's remaining network time.

        Returns:
            The response.

        Raises:
            NetworkFailure: The host throttled an earlier request, the request budget is
                used up, or the exchange failed.
            TimeoutError: The request or the fetch ran out of time.
        """
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
                started = time.monotonic()
                self._next_request[host] = started + interval
                response = await self._over_network(
                    target, method, want_body=want_body, allowance=allowance
                )
                logger.debug(
                    "online %s %s: HTTP %d in %.2f s (request %d of %d)",
                    method,
                    host,
                    response.status,
                    time.monotonic() - started,
                    self.requests,
                    self.options.online_max_requests,
                )
                if response.status == 429:
                    self._throttled.add(host)
                    logger.info("online %s: throttled the request (HTTP 429); not retried", host)
                return response

    async def _over_network(
        self, target: Target, method: str, *, want_body: bool, allowance: _Allowance
    ) -> Response:
        """Resolve the host and ask its first reachable public address within the time left.

        The time the request takes is deducted from ``allowance``.

        Args:
            target: The URL to ask.
            method: The request method.
            want_body: Whether to read the response body.
            allowance: The fetch's remaining network time.

        Returns:
            The response.

        Raises:
            TimeoutError: The request timeout or the fetch's allowance ran out.
            NetworkFailure: An address is not public, or the exchange failed.
        """
        limit = min(self.options.online_timeout_seconds, allowance.remaining)
        if limit <= 0:
            raise TimeoutError
        started = time.monotonic()
        try:
            async with asyncio.timeout(limit):
                addresses = await self._resolve(target.host, target.port)
                # Recheck injected/resolver answers; each request pins one validated address.
                addresses = tuple(require_public_ip(address) for address in addresses)
                return await self._first_reachable(target, addresses, method, want_body=want_body)
        finally:
            allowance.remaining -= time.monotonic() - started

    async def _first_reachable(
        self, target: Target, addresses: tuple[str, ...], method: str, *, want_body: bool
    ) -> Response:
        """Ask the first validated address that accepts a connection.

        Only a failed connection moves on to the next address; IPv4 goes first because
        a missing IPv6 route is the commoner failure. When there is more than one address,
        each connection attempt gets a share of the timeout so that a silent address cannot
        use all of it.

        Args:
            target: The URL to ask.
            addresses: The host's validated addresses; at most ``_MAX_ADDRESS_ATTEMPTS``
                are tried.
            method: The request method.
            want_body: Whether to read the response body.

        Returns:
            The response of the first address that accepted a connection.

        Raises:
            NetworkFailure: There are no addresses.
            ConnectFailure: No address accepted a connection.
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
