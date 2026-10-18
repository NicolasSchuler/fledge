"""The Crossref REST API as the online checks use it: request URLs and typed replies.

This module owns what is specific to Crossref and doi.org: how the lookup, search and
update-notice URLs are written, and how a JSON reply becomes a typed record. A reply
that is not in the expected shape, or exceeds a bound, raises ``CrossrefReplyError``
with a reason for the report; nothing partial is returned. It performs no request and
judges nothing about whether a record matches a bibliography entry (``online_checks``).

Crossref field and filter semantics:
https://github.com/Crossref/rest-api-doc/blob/master/api_format.md
https://www.crossref.org/documentation/retrieve-metadata/rest-api/rest-api-filters/
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, NamedTuple
from urllib.parse import quote, urlencode

from .bibtex_syntax import DOI_PATTERN
from .online_options import CROSSREF_HOST, FetchOutcome

CROSSREF_WORKS_URL = f"https://{CROSSREF_HOST}/works"
# Candidates asked for in a title search, and update notices asked for per DOI.
SEARCH_ROWS = 5
NOTICE_ROWS = 20
# Bounds on what a reply may contain; larger values make the reply unusable.
_MAX_ITEMS = 20
_MAX_STRINGS = 20
_MAX_STRING_CHARACTERS = 2048
_MAX_AUTHORS = 200
_MAX_FAMILY_NAME_CHARACTERS = 256
_MAX_RELATIONS = 20
_MAX_UPDATES = 20
_NOTICE_TYPE = re.compile(r"[A-Za-z][A-Za-z -]{0,127}")
_DATE_KEYS = ("issued", "published", "published-print", "published-online")
_ASSERTING_PARTIES = ("subject", "object")
_MALFORMED = "Crossref returned malformed metadata"


class CrossrefReplyError(Exception):
    """A reply that cannot be used, with the reason and the outcome to report."""

    def __init__(self, reason: str, outcome: FetchOutcome = FetchOutcome.MALFORMED) -> None:
        super().__init__(reason)
        self.reason = reason
        self.outcome = outcome


@dataclass(frozen=True, slots=True)
class PublishedVersion:
    """A DOI that Crossref declares to be the published version of a preprint."""

    doi: str
    asserted_by: str | None


@dataclass(frozen=True, slots=True)
class CrossrefWork:
    """The fields of one Crossref record that a bibliography entry is compared with.

    A list is empty when the record gives no usable value. ``published_versions`` is
    None when the record has a relation list that could not be read.
    """

    doi: str
    titles: list[str]
    authors: list[str]
    years: list[str]
    venues: list[str]
    published_versions: list[PublishedVersion] | None


@dataclass(frozen=True, slots=True)
class CrossrefCandidate:
    """One result of a title search; ``authors`` is None when its names were unusable."""

    doi: str
    titles: list[str]
    work_type: str | None
    authors: list[str] | None
    years: list[str]


@dataclass(frozen=True, slots=True)
class UpdateNotice:
    """A record that corrects, retracts or otherwise updates a work."""

    kind: str
    notice_doi: str


class NoticeReply(NamedTuple):
    """The update notices that name a DOI; ``truncated`` when Crossref holds more."""

    notices: list[UpdateNotice]
    truncated: bool


def doi_url(doi: str) -> str:
    """The doi.org address of ``doi``."""
    return "https://doi.org/" + quote(doi, safe="/")


def work_url(doi: str) -> str:
    """The Crossref lookup of one DOI."""
    return CROSSREF_WORKS_URL + "/" + quote(doi, safe="")


def search_url(title: str) -> str:
    """A bibliographic search for ``title`` returning at most ``SEARCH_ROWS`` results."""
    return CROSSREF_WORKS_URL + "?" + urlencode({"query.bibliographic": title, "rows": SEARCH_ROWS})


def notices_url(doi: str) -> str:
    """The records that update ``doi``, at most ``NOTICE_ROWS`` of them."""
    query = urlencode({"filter": "updates:" + doi, "rows": NOTICE_ROWS})
    return CROSSREF_WORKS_URL + "?" + query


def _strings(value: Any) -> list[str]:
    if (
        not isinstance(value, list)
        or len(value) > _MAX_STRINGS
        or any(not isinstance(item, str) or len(item) > _MAX_STRING_CHARACTERS for item in value)
    ):
        return []
    return value


def _family_names(value: Any) -> list[str] | None:
    if not isinstance(value, list) or len(value) > _MAX_AUTHORS:
        return None
    result = []
    for author in value:
        family = author.get("family") if isinstance(author, dict) else None
        if not isinstance(family, str) or not family or len(family) > _MAX_FAMILY_NAME_CHARACTERS:
            return None
        result.append(family)
    return result


def _years(record: dict[str, Any]) -> list[str]:
    years = set()
    for key in _DATE_KEYS:
        value = record.get(key)
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


def _message(body: bytes) -> dict[str, Any]:
    """The ``message`` object of a successful Crossref reply."""
    try:
        data = json.loads(body)
        message = data["message"]
        if data.get("status") != "ok" or not isinstance(message, dict):
            raise ValueError
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError) as error:
        raise CrossrefReplyError(_MALFORMED) from error
    return message


def _items(message: dict[str, Any]) -> list[dict[str, Any]]:
    items = message.get("items")
    if (
        not isinstance(items, list)
        or len(items) > _MAX_ITEMS
        or any(not isinstance(item, dict) for item in items)
    ):
        raise CrossrefReplyError(_MALFORMED)
    return items


def _published_versions(record: dict[str, Any]) -> list[PublishedVersion] | None:
    relation = record.get("relation", {})
    related = relation.get("is-preprint-of", []) if isinstance(relation, dict) else []
    if not related:
        return []
    if not isinstance(related, list) or len(related) > _MAX_RELATIONS:
        return None
    versions = []
    for item in related:
        if not (
            isinstance(item, dict)
            and item.get("id-type") == "doi"
            and isinstance(item.get("id"), str)
            and DOI_PATTERN.fullmatch(item["id"])
        ):
            return None
        asserted = item.get("asserted-by")
        versions.append(
            PublishedVersion(item["id"], asserted if asserted in _ASSERTING_PARTIES else None)
        )
    return versions


def parse_work_reply(body: bytes) -> CrossrefWork:
    """The record in a reply to a lookup of one DOI."""
    message = _message(body)
    doi = message.get("DOI")
    if not isinstance(doi, str) or DOI_PATTERN.fullmatch(doi) is None:
        raise CrossrefReplyError(_MALFORMED)
    return CrossrefWork(
        doi,
        _strings(message.get("title")),
        _family_names(message.get("author")) or [],
        _years(message),
        _strings(message.get("container-title")),
        _published_versions(message),
    )


def parse_search_reply(body: bytes) -> list[CrossrefCandidate]:
    """The candidates in a reply to a title search, in the order Crossref ranked them."""
    items = _items(_message(body))
    if len(items) > SEARCH_ROWS:
        raise CrossrefReplyError(f"Crossref returned more than the {SEARCH_ROWS} results asked for")
    candidates = []
    for item in items:
        doi, titles = item.get("DOI"), _strings(item.get("title"))
        if not isinstance(doi, str) or DOI_PATTERN.fullmatch(doi) is None or not titles:
            raise CrossrefReplyError("Crossref returned a result without a usable DOI or title")
        work_type = item.get("type")
        candidates.append(
            CrossrefCandidate(
                doi,
                titles,
                work_type if isinstance(work_type, str) else None,
                _family_names(item.get("author", [])),
                _years(item),
            )
        )
    return candidates


def _update_kinds(item: dict[str, Any], doi: str) -> list[str]:
    """The kinds of update that ``item`` makes to ``doi``; an item that makes none is wrong."""
    updates = item.get("update-to")
    if not isinstance(updates, list) or len(updates) > _MAX_UPDATES:
        raise CrossrefReplyError("Crossref returned an update record that could not be read")
    kinds = []
    for update in updates:
        if (
            not isinstance(update, dict)
            or not isinstance(update.get("DOI"), str)
            or DOI_PATTERN.fullmatch(update["DOI"]) is None
            or not isinstance(update.get("type"), str)
            or not _NOTICE_TYPE.fullmatch(update["type"])
        ):
            raise CrossrefReplyError("Crossref returned an update entry that could not be read")
        if update["DOI"].casefold() == doi.casefold():
            kinds.append(update["type"])
    if not kinds:
        raise CrossrefReplyError(
            "Crossref returned an update record that does not name this DOI",
            FetchOutcome.IDENTIFIER_MISMATCH,
        )
    return kinds


def parse_notice_reply(body: bytes, doi: str) -> NoticeReply:
    """The notices in a reply to an update-filter query for ``doi``."""
    message = _message(body)
    items = _items(message)
    notices: list[UpdateNotice] = []
    for item in items:
        notice_doi = item.get("DOI")
        if not isinstance(notice_doi, str) or DOI_PATTERN.fullmatch(notice_doi) is None:
            raise CrossrefReplyError("Crossref returned an update record that could not be read")
        for kind in _update_kinds(item, doi):
            if len(notices) >= NOTICE_ROWS:
                raise CrossrefReplyError(
                    f"Crossref listed more than {NOTICE_ROWS} notices", FetchOutcome.RESPONSE_LIMIT
                )
            notices.append(UpdateNotice(kind, notice_doi))
    total = message.get("total-results")
    return NoticeReply(notices, isinstance(total, int) and total > len(items))
