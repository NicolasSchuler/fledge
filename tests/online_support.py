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
    """Online options with network permission and the shortest spacing between requests."""
    return OnlineOptions(online=True, online_provider_interval_seconds=0.1, **changes)


def crossref_work(**changes: Any) -> dict[str, Any]:
    """The ``message`` of a Crossref record that agrees with the entries the tests write."""
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
    """A successful Crossref reply carrying ``message``."""
    return Response(200, body=json.dumps({"status": "ok", "message": message}).encode())


async def public_resolver(host: str, port: int) -> tuple[str, ...]:
    """Name every host as one public address."""
    return (PUBLIC_ADDRESS,)


Answer = Response | BaseException | Callable[[ExchangeRequest], Response | BaseException]


class FakeTransport:
    """Records each exchange and answers it.

    ``answers`` is one answer used for every exchange, or an iterable of answers used in
    turn. An answer is a response, an exception to raise, or a function of the request
    that returns either.
    """

    def __init__(self, answers: Answer | Iterable[Answer]) -> None:
        self.requests: list[ExchangeRequest] = []
        self._answers = iter(answers) if isinstance(answers, list | tuple) else None
        self._single = None if self._answers is not None else answers

    async def __call__(self, request: ExchangeRequest) -> Response:
        self.requests.append(request)
        answer = next(self._answers) if self._answers is not None else self._single
        if callable(answer):
            answer = answer(request)
        if isinstance(answer, BaseException):
            raise answer
        assert isinstance(answer, Response)
        return answer
