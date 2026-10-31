"""Resolving a host name to public addresses without letting a slow resolver stall a job.

System name resolution cannot be cancelled from inside a thread, so each lookup runs in
a disposable child interpreter that is killed when the lookup is cancelled or times
out. This module owns that child, the validation of every address it returns against
the public-address policy, and ``JobResolver``, which remembers the validated answers
of one job so that a host is looked up once. It opens no connection and speaks no HTTP;
``online_http_session`` connects to one of the returned addresses and keeps to it.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import sys
from collections.abc import Awaitable, Callable

from .cancellation import complete_despite_cancellation
from .online_options import FetchOutcome, NetworkFailure
from .process_control import kill_unfinished, start_process
from .public_url import require_public_ip

Resolver = Callable[[str, int], Awaitable[tuple[str, ...]]]

# Exit status 3 tells the parent that the name does not resolve, as opposed to a
# resolver that did not answer or output that was too large.
DNS_UNRESOLVED_EXIT = 3
# The answer is a JSON list of at most this many addresses in at most this many bytes.
_MAX_ADDRESSES = 32
_MAX_ANSWER_BYTES = 8192
DNS_SCRIPT = f"""import json,socket,sys
try: rows=socket.getaddrinfo(sys.argv[1],int(sys.argv[2]),type=socket.SOCK_STREAM)
except socket.gaierror: raise SystemExit({DNS_UNRESOLVED_EXIT})
addresses=sorted({{row[4][0] for row in rows}})
if not addresses or len(addresses)>{_MAX_ADDRESSES}: raise SystemExit(2)
print(json.dumps(addresses))
"""


async def _read_answer(process: asyncio.subprocess.Process) -> bytes:
    """Read the child's output, bounded; a longer answer is refused rather than buffered.

    Returns:
        The output.

    Raises:
        NetworkFailure: The child has no output stream, or its answer exceeds the byte
            limit.
    """
    stream = process.stdout
    if stream is None:
        raise NetworkFailure(FetchOutcome.UNAVAILABLE, "Public DNS resolution did not complete")
    data = bytearray()
    while chunk := await stream.read(_MAX_ANSWER_BYTES + 1 - len(data)):
        data.extend(chunk)
        if len(data) > _MAX_ANSWER_BYTES:
            raise NetworkFailure(FetchOutcome.UNAVAILABLE, "DNS response exceeded its byte limit")
    return bytes(data)


async def resolve_public(host: str, port: int) -> tuple[str, ...]:
    """Resolve ``host`` to public addresses; any non-public answer makes the whole lookup unsafe.

    An IP address literal is checked without a lookup. Otherwise the lookup runs in a child
    interpreter, which is killed if it has not finished when the lookup ends or is
    cancelled.

    Args:
        host: The host name or IP address literal.
        port: The port passed to ``getaddrinfo`` with the name.

    Returns:
        The validated addresses; those of a lookup are sorted.

    Raises:
        NetworkFailure: The name does not resolve, the lookup did not complete or gave a
            malformed or oversized answer, or an address is not public.
    """
    try:
        return (require_public_ip(str(ipaddress.ip_address(host))),)
    except ValueError:
        pass
    process = None
    try:
        process = await start_process(
            sys.executable,
            "-I",
            "-c",
            DNS_SCRIPT,
            host,
            str(port),
            stop=kill_unfinished,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={"LANG": "C"},
            close_fds=True,
        )
        data = await _read_answer(process)
        await process.wait()
        if process.returncode == DNS_UNRESOLVED_EXIT:
            raise NetworkFailure(
                FetchOutcome.HOST_UNRESOLVED, f"The host name {host} could not be resolved"
            )
        if process.returncode:
            raise NetworkFailure(FetchOutcome.UNAVAILABLE, "Public DNS resolution did not complete")
        addresses = json.loads(data)
        if not isinstance(addresses, list) or not 1 <= len(addresses) <= _MAX_ADDRESSES:
            raise ValueError
        return tuple(require_public_ip(value) for value in addresses)
    except (ValueError, OSError) as error:
        raise NetworkFailure(
            FetchOutcome.UNAVAILABLE, "Public DNS resolution did not complete"
        ) from error
    finally:
        if process is not None:
            kill_unfinished(process)
            await complete_despite_cancellation(process.wait())


class JobResolver:
    """Looks each host up once per job; only validated answers are remembered.

    The remembered answers are bounded by the job's request budget, because every
    lookup serves a request. A failed lookup is not remembered, so a name that failed
    for a transient reason is tried again by the next request.

    Args:
        resolve: Looks up the public addresses of a host and port; by default
            :func:`resolve_public`.
    """

    def __init__(self, resolve: Resolver = resolve_public) -> None:
        self._resolve = resolve
        self._answers: dict[tuple[str, int], tuple[str, ...]] = {}

    async def __call__(self, host: str, port: int) -> tuple[str, ...]:
        """Return the validated addresses of ``host``, looking them up on first use.

        Args:
            host: The host name or IP address literal.
            port: The port to resolve for.

        Returns:
            The validated addresses.

        Raises:
            NetworkFailure: The lookup failed; the failure is not remembered.
        """
        key = (host, port)
        if key not in self._answers:
            self._answers[key] = await self._resolve(host, port)
        return self._answers[key]
