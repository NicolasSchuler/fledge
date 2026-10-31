"""Fakes for the network seams of the online checks: a resolver, a transport and replies.

The online client takes a ``resolver`` (host to addresses) and a ``transport`` (one
exchange with one address) so that tests never touch the network. Import from here
instead of redefining them.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from typing import Any

from latexprep.online_http_session import ExchangeRequest
from latexprep.online_http_wire import Response
from latexprep.online_options import OnlineOptions

PUBLIC_ADDRESS = "93.184.216.34"
PUBLIC_IPV6_ADDRESS = "2606:2800:220:1:248:1893:25c8:1946"


def options(**changes: Any) -> OnlineOptions:
    """Build online options with network permission and the shortest spacing between requests.

    Args:
        **changes: Further ``OnlineOptions`` fields.

    Returns:
        The online options.
    """
    return OnlineOptions(online=True, online_provider_interval_seconds=0.1, **changes)


def crossref_work(**changes: Any) -> dict[str, Any]:
    """Return the ``message`` of a Crossref record that agrees with the entries the tests write.

    Args:
        **changes: Fields that replace or extend those of the record.

    Returns:
        The record as a JSON-ready mapping.
    """
    return {
        "DOI": "10.1234/example",
        "title": ["A reference title"],
        "author": [{"family": "Smith", "given": "Ada"}],
        "issued": {"date-parts": [[2024]]},
        "container-title": ["Example Journal"],
        "type": "journal-article",
        **changes,
    }


def crossref_reply(message: Any) -> Response:
    """Return a successful Crossref reply carrying ``message``."""
    return Response(200, body=json.dumps({"status": "ok", "message": message}).encode())


async def public_resolver(host: str, port: int) -> tuple[str, ...]:
    """Name every host as one public address, ``PUBLIC_ADDRESS``; the port is ignored."""
    return (PUBLIC_ADDRESS,)


Answer = Response | BaseException | Callable[[ExchangeRequest], Response | BaseException]


class FakeTransport:
    """A transport that records each exchange and answers it.

    Args:
        answers: One answer used for every exchange, or any other iterable of answers (a
            list, a tuple, a generator) used in turn; the iterable is read in full when the
            transport is built. An answer is a response, an exception to raise, or a
            function of the request that returns either.

    Attributes:
        requests: Every request, in order.
    """

    def __init__(self, answers: Answer | Iterable[Answer]) -> None:
        self.requests: list[ExchangeRequest] = []
        single = isinstance(answers, Response | BaseException) or callable(answers)
        self._answers = None if single else iter(tuple(answers))
        self._single = answers if single else None

    async def __call__(self, request: ExchangeRequest) -> Response:
        self.requests.append(request)
        answer = next(self._answers) if self._answers is not None else self._single
        if callable(answer):
            answer = answer(request)
        if isinstance(answer, BaseException):
            raise answer
        assert isinstance(answer, Response)
        return answer
