"""Which URLs and network addresses an online check may request, and how they are shown.

This module owns the policy that keeps an online check from reaching anything but the
public internet: a URL must use http or https on the scheme's standard port, carry no
credentials or secret-looking query parameters, and name a public host; an address must
be globally routable. It also redacts a URL for findings. It resolves no name and opens
no connection (see ``pinned_dns`` and ``online_http_session``); the address policy is
applied again to every resolved answer before a connection is made.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import (
    SplitResult,
    parse_qsl,
    quote,
    urlencode,
    urlsplit,
    urlunsplit,
)

from .online_options import FetchOutcome, NetworkFailure

_SECRET_KEY = re.compile(
    r"token|secret|password|passwd|api.?key|authorization|signature|credential|session|jwt|bearer"
    r"|^(?:auth|key|sig|code)$",
    re.I,
)
_MAX_URL_CHARACTERS = 8192
_MAX_HOST_CHARACTERS = 253
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
# Names that never resolve on the public internet, or only inside a private network.
_RESERVED_SUFFIXES = (
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
_STANDARD_PORTS = {"http": 80, "https": 443}
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


@dataclass(frozen=True)
class Target:
    """A validated request target: its normalized URL, host, port and request path.

    Attributes:
        url: The normalized URL without credentials or fragment.
        scheme: ``http`` or ``https``.
        host: The lowercase ASCII host name or IP address, without a trailing dot.
        port: The scheme's standard port.
        path: The percent-encoded request path with its query, if any.
    """

    url: str
    scheme: str
    host: str
    port: int
    path: str


def _query_pairs(query: str) -> list[tuple[str, str]]:
    """Split ``query`` into key-value pairs; ``;`` separates like ``&`` and blanks are kept."""
    return parse_qsl(query.replace(";", "&"), keep_blank_values=True)


def redact_url(value: str) -> str:
    """Return ``value`` without credentials, fragment and secret-looking query values.

    A value that cannot be parsed becomes a fixed placeholder.
    """
    try:
        parts = urlsplit(value)
        host = parts.hostname or "invalid-target"
        authority = f"[{host}]" if ":" in host else host
        if parts.port:
            authority += f":{parts.port}"
        query = urlencode(
            [
                (key, "[redacted]" if _SECRET_KEY.search(key) else val)
                for key, val in _query_pairs(parts.query)
            ]
        )
        return urlunsplit((parts.scheme, authority, parts.path, query, ""))
    except ValueError:
        return "[invalid or credential-bearing URL]"


def require_public_ip(value: str) -> str:
    """Normalize ``value`` as an IP address that must be globally routable.

    Returns:
        The normalized address.

    Raises:
        NetworkFailure: ``value`` is not an IP address, or the address is private, local,
            reserved, multicast, special-use or carries a scope id.
    """
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


def _require_clean_text(url: str) -> None:
    """Refuse a URL that is too long or holds whitespace, control characters or backslashes.

    Raises:
        NetworkFailure: The URL is not clean text.
    """
    if (
        not isinstance(url, str)
        or len(url) > _MAX_URL_CHARACTERS
        or any(ord(char) < 33 or ord(char) == 127 for char in url)
        or "\\" in url
    ):
        raise NetworkFailure(
            FetchOutcome.UNSAFE_TARGET, "URL contains unsupported whitespace, controls or escapes"
        )


def _require_no_credentials(parts: SplitResult) -> None:
    """Refuse a URL with a user name, a password or a secret-looking query key.

    Raises:
        NetworkFailure: The URL carries credentials.
    """
    if parts.username is not None or parts.password is not None:
        raise NetworkFailure(
            FetchOutcome.UNSAFE_TARGET, "Credential-bearing URLs are never requested"
        )
    if any(_SECRET_KEY.search(key) for key, _ in _query_pairs(parts.query)):
        raise NetworkFailure(
            FetchOutcome.UNSAFE_TARGET,
            "Credential-bearing query parameters are never requested",
        )


def _is_dns_name(host: str) -> bool:
    """Return whether ``host`` is a dotted name of valid DNS labels within the length limit."""
    return (
        "." in host
        and len(host) <= _MAX_HOST_CHARACTERS
        and all(_DNS_LABEL.fullmatch(label) for label in host.split("."))
    )


def _public_host(hostname: str, parts: SplitResult) -> tuple[str, int]:
    """Return the ASCII host and the port of ``parts`` if both may be requested.

    Args:
        hostname: The host that ``parts`` names.
        parts: The split URL.

    Returns:
        The lowercase ASCII host without a trailing dot, and the port.

    Raises:
        NetworkFailure: The host is a reserved or local DNS name or a non-public address.
        ValueError: The port is not the scheme's standard port, or the host is neither a
            DNS name nor an IP address.
    """
    host = hostname.encode("idna").decode("ascii").lower().rstrip(".")
    if host.endswith(_RESERVED_SUFFIXES):
        raise NetworkFailure(
            FetchOutcome.UNSAFE_TARGET, "Reserved or local DNS names are forbidden"
        )
    standard = _STANDARD_PORTS[parts.scheme]
    port = parts.port or standard
    if port != standard or "%" in host:
        raise ValueError
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if not _is_dns_name(host):
            raise ValueError from None
    else:
        require_public_ip(str(address))
    return host, port


def parse_public_url(url: str) -> Target:
    """Validate ``url`` for a request, or raise a ``NetworkFailure`` that says why not.

    Returns:
        The validated target.

    Raises:
        NetworkFailure: The URL is not a clean public HTTP or HTTPS URL on its scheme's
            standard port, or it carries credentials.
    """
    _require_clean_text(url)
    try:
        parts = urlsplit(url)
        hostname = parts.hostname
        if parts.scheme not in _STANDARD_PORTS or not hostname:
            raise ValueError
        _require_no_credentials(parts)
        host, port = _public_host(hostname, parts)
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
