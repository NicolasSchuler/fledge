"""Settings of the online checks, and the vocabulary that every online module shares.

This module owns ``OnlineOptions`` (every remote check is opt-in and bounded), the
``FetchOutcome`` that names how a request ended, and ``NetworkFailure``, the way a
transport layer says why a request cannot continue. It performs no request. A failure
never escapes the online modules as an exception: ``online_http_session`` turns it into
a ``Fetch`` and ``online_checks`` into a finding.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar

from . import __version__
from .option_config import Bounds, require_text, require_unique_strings, validate_scalars

# The only host that receives the contact address; Crossref's polite pool needs it there.
CROSSREF_HOST = "api.crossref.org"
_EMAIL = re.compile(
    r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
    r"@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+"
)
# Crossref's polite pool keys on a contact address inside the product comment.
# What a report shows in place of a configured contact address.
CONTACT_EMAIL_PLACEHOLDER = "<set>"
PRODUCT = f"fledge/{__version__}"
DEFAULT_USER_AGENT = f"{PRODUCT} (explicit reference checks)"
# Inclusive (low, high) bounds of the integer options, also quoted in messages.
OPTION_BOUNDS = {
    "online_max_requests": (1, 1000),
    "online_timeout_seconds": (1, 60),
    "online_max_redirects": (0, 10),
    "online_max_response_bytes": (1024, 4_194_304),
    "online_jobs": (1, 8),
}
# Seconds between requests to one provider.
_PROVIDER_INTERVAL_BOUNDS = Bounds(0.1, 60)
_MAX_REPLICATION_URLS = 1000
# The longest email address that SMTP can route (RFC 5321).
_MAX_EMAIL_CHARACTERS = 254
# Each selectable check's option and rule, in catalogue order (NET001-NET007).
CHECK_RULES = {
    "online_doi_resolution": "online.doi_resolution",
    "online_metadata": "online.metadata",
    "online_missing_doi": "online.missing_doi",
    "online_published_versions": "online.published_version",
    "online_notices": "online.notices",
    "online_reference_links": "online.reference_link",
    "online_replication_links": "online.replication_link",
}
# The only check that reads configured and source links instead of bibliography entries.
SOURCE_LINK_CHECK = "online_replication_links"


def option_maximum(option: str) -> int:
    """Return the largest value ``option`` accepts, for messages that say how to raise it."""
    return OPTION_BOUNDS[option][1]


class FetchOutcome(StrEnum):
    """How one bounded request ended; the values are the reported outcome strings.

    Attributes:
        AVAILABLE: The server answered with a 2xx status.
        AUTHENTICATION_REQUIRED: The server answered 401.
        ACCESS_DENIED: The server answered 403.
        NOT_FOUND: The server answered 404.
        GONE: The server answered 410.
        RATE_LIMITED: The server answered 429, asking for fewer requests.
        UNAVAILABLE: Another error status, or the provider or network could not be reached.
        UNSAFE_TARGET: The request or a redirect led to a target that may not be contacted.
        MALFORMED: The reply could not be used.
        RESPONSE_LIMIT: The reply was larger than ``online_max_response_bytes``.
        REQUEST_LIMIT: The job's request budget was spent before this request.
        REDIRECT_LOOP: A redirect leads back to an address already visited.
        REDIRECT_LIMIT: The redirects exceeded ``online_max_redirects``.
        TIMEOUT: The provider did not answer in time.
        HOST_UNRESOLVED: The host name could not be resolved.
        TLS_FAILURE: The secure connection failed.
        IDENTIFIER_MISMATCH: The reply concerns a different identifier than the one asked for.
        INPUT_UNAVAILABLE: The entry's input cannot be used, so no request was made.
    """

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
    HOST_UNRESOLVED = "host_unresolved"
    TLS_FAILURE = "tls_failure"
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

    ``online_contact_email`` is sent only in the User-Agent request header to
    api.crossref.org, for Crossref's polite pool; every other host receives the plain
    product User-Agent. It never enters a finding, detail or report field. The report
    records the setting as ``CONTACT_EMAIL_PLACEHOLDER`` when an address is set, so a
    shared report, HTML file or diagnostics bundle shows that one was used but not whose.

    Construction raises ``SettingsError`` when a setting has the wrong type or lies outside
    its bounds, the replication links are not distinct nonempty strings or are too many, or
    the contact address is not one simple email address.

    Attributes:
        FIELD_BOUNDS: The bounds :func:`validate_scalars` applies to the integer limits and
            the provider interval.
        online: Whether network access is allowed; it selects no check.
        online_doi_resolution: Whether the ``online.doi_resolution`` check is selected.
        online_metadata: Whether the ``online.metadata`` check is selected.
        online_missing_doi: Whether the ``online.missing_doi`` check is selected.
        online_published_versions: Whether the ``online.published_version`` check is selected.
        online_notices: Whether the ``online.notices`` check is selected.
        online_reference_links: Whether the ``online.reference_link`` check is selected.
        online_replication_links: Whether the ``online.replication_link`` check is selected;
            it reads configured and source links instead of bibliography entries.
        online_replication_urls: The configured links of the replication link check.
        online_contact_email: The contact address for Crossref's polite pool, or ``None``.
        online_max_requests: The request budget of the whole job.
        online_timeout_seconds: The request timeout, in seconds.
        online_max_redirects: The most redirects one request follows.
        online_max_response_bytes: The largest reply accepted, in bytes.
        online_jobs: How many references and links are checked at once, at most.
        online_provider_interval_seconds: The seconds between requests to one provider.
    """

    FIELD_BOUNDS: ClassVar[dict[str, Bounds]] = {
        **{name: Bounds(low, high) for name, (low, high) in OPTION_BOUNDS.items()},
        "online_provider_interval_seconds": _PROVIDER_INTERVAL_BOUNDS,
    }

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
        validate_scalars(self)
        require_unique_strings(
            "online_replication_urls",
            self.online_replication_urls,
            max_items=_MAX_REPLICATION_URLS,
            allow_surrounding_whitespace=True,
        )
        require_text(
            "online_contact_email",
            self.online_contact_email,
            expected="one simple email address",
            pattern=_EMAIL,
            max_length=_MAX_EMAIL_CHARACTERS,
            allow_none=True,
        )

    @property
    def user_agent(self) -> str:
        """The User-Agent header value for Crossref; the only place a contact address is used."""
        email = self.online_contact_email
        return f"{PRODUCT} (mailto:{email})" if email else DEFAULT_USER_AGENT

    def user_agent_for(self, host: str) -> str:
        """Return the User-Agent for ``host``: the contact address goes to Crossref's API alone."""
        return self.user_agent if host == CROSSREF_HOST else DEFAULT_USER_AGENT

    @property
    def selected_rules(self) -> list[str]:
        """Rules of the selected checks, whether or not network access is allowed."""
        return [rule for name, rule in CHECK_RULES.items() if getattr(self, name)]

    @property
    def reads_bibliography(self) -> bool:
        """Whether a selected check requests bibliography entries."""
        return any(getattr(self, name) for name in CHECK_RULES if name != SOURCE_LINK_CHECK)


class NetworkFailure(Exception):
    """A bounded, redacted failure; never contains provider bodies or credentials.

    Args:
        message: Why the request cannot continue; it becomes the exception's text.

    Attributes:
        outcome: How the request ended.
    """

    def __init__(self, outcome: FetchOutcome, message: str) -> None:
        super().__init__(message)
        self.outcome = outcome
