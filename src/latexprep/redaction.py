"""Bounded credential redaction for complete, JSON/asdict-shaped report trees.

Discovery precedes replacement so a credential found in settings also disappears
from earlier logs, metadata, suggestions and diffs. This module performs no file I/O
and does not import report models. Unsupported values are never stringified.
Local directory names are generalized separately by :func:`scrub_local_paths`.
"""

from __future__ import annotations

import json
import math
import re
import tempfile
from collections import Counter
from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, unquote_plus

REDACTED = "[REDACTED]"
HOME_LABEL = "~"
TEMPORARY_LABEL = "<tmp>"
LIMIT_REDACTION = "[REDACTED: report exceeds redaction limits]"
MAX_TEXT_CHARS = 16 * 1024 * 1024
# Leave room for the default 10,000 imported entries, each represented by a
# selection/change record and its final inventory row. Text and replacement-work
# limits still independently bound scanning and credential substitution.
MAX_NODES = 250_000
MAX_DEPTH = 64
MAX_SECRETS = 1024
MAX_DISCOVERIES = 100_000
MAX_SECRET_CHARS = 1024 * 1024
MAX_REPLACEMENT_WORK = 128 * 1024 * 1024

_ACCESS_KEY = r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"
# Prefix and alphabet of each credential-shaped token; the caller adds the length.
_TOKEN_PREFIXES = (
    r"\b(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]",
    r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]",
    r"\bxox[baprs]-[A-Za-z0-9-]",
)
# Shared with the optional source credential check. Detection remains heuristic;
# the report boundary additionally handles short contextual credentials below.
SECRET_PATTERNS = (
    ("access-key-shaped token", re.compile(_ACCESS_KEY)),
    *(("credential-shaped token", re.compile(prefix + "{20,255}")) for prefix in _TOKEN_PREFIXES),
    ("private-key header", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    (
        "secret assignment",
        re.compile(
            r"(?i)\b(?:api[_-]?key|access[_-]?token|client[_-]?secret|password|secret[_-]?key)"
            r"[\s\"']{0,8}[:=][\s\"']{0,8}(?P<secret>[A-Za-z0-9_./+~-]{12,256})"
        ),
    ),
    (
        "URL credential",
        re.compile(r"(?i)https?://[^\s/@:{}]{1,128}:(?P<secret>[^\s/@{}]{8,256})@"),
    ),
)
# Report redaction discovers each whole token however long it is, so no part of
# a long credential survives; the source check above bounds its matches instead.
_STANDALONE = re.compile("|".join([_ACCESS_KEY, *(prefix + "{20,}" for prefix in _TOKEN_PREFIXES)]))
_PRIVATE_BLOCK = re.compile(
    r"-----BEGIN (?P<kind>(?:(?:RSA|EC|OPENSSH|DSA|ENCRYPTED) )?PRIVATE KEY)-----"
    r"(?P<body>.*?)(?:-----END (?P=kind)-----|\Z)",
    re.S,
)
_SENSITIVE_END = re.compile(
    r"(?:password|passwd|pwd|passphrase|apikey|accesskey|token|secret|secretkey|privatekey|"
    r"credential|authorization|signature)s?$",
    re.I,
)
_ASSIGNMENT = re.compile(
    r"(?i)(?<![\w])(?P<key>[a-z][a-z0-9_.-]{0,95})"
    r"(?:\\?[\"'])?\s*[:=]\s*"
    r"(?P<value>\\?\"(?:\\.|[^\"\\])*\\?\"|"
    r"\\?'(?:\\.|[^'\\])*\\?'|[^\s,;&}\]<>\"']+|[\"'][^\r\n]*)"
)
_BEARER = re.compile(r"(?i)\bBearer\s+(?P<value>[A-Za-z0-9._~+/=-]+)")
# Scan from delimiters instead of retrying an unbounded optional scheme at every
# character. Bare credentials start at token boundaries; query keys are bounded.
_USERINFO = re.compile(r"//(?P<value>[^\s/?#<>]+)@")
_BARE_USERINFO = re.compile(r"(?<![^\s/<>])(?P<value>[^\s/:@<>]+:[^\s/@<>]+)@")
# A bare ``scheme:address@host`` URI names a recipient, not a user and password.
# Authorities (``//user:password@host``) never receive this exemption.
_NON_CREDENTIAL_SCHEMES = frozenset(
    {"mailto", "sip", "sips", "xmpp", "acct", "im", "pres", "mid", "cid", "news", "nntp", "urn"}
)
# Secrets shorter than this are only replaced as whole tokens, so a short password
# such as "git" or "alice" cannot corrupt unrelated words in a report.
_MIN_SUBSTRING_SECRET = 8
_TOKEN_BEFORE = r"(?<![A-Za-z0-9])"
_TOKEN_AFTER = r"(?![A-Za-z0-9])"
# A password-less username is secret only when it looks like a bearer token.
_TOKEN_USERNAME_MIN_CHARS = 16
_TOKEN_ENTROPY_MIXED_CASE = 3.5
_TOKEN_ENTROPY_SINGLE_CASE = 4.0
_QUERY = re.compile(r"(?:[?&#;])(?P<key>[^\s=&;#]{1,256})=(?P<value>[^\s&#;<>\"']*)")


class _RedactionLimitExceeded(Exception):
    """Fail closed without exposing the unprocessed report."""


def _decoded_json_string(value: str) -> str | None:
    """The text a double-quoted JSON string literal denotes, or None if it is not one."""
    if not value.startswith('"'):
        return None
    try:
        decoded = json.loads(value)
    except ValueError:
        return None
    return decoded if isinstance(decoded, str) else None


def _variants(text: str) -> list[str]:
    result = [text]
    for _ in range(3):
        decoded = unquote(result[-1])
        if decoded == result[-1]:
            break
        result.append(decoded)
    if unquote(result[-1]) != result[-1]:
        raise _RedactionLimitExceeded
    return result


def _token_like(value: str) -> bool:
    """Whether a long username is more likely a bearer token than a name."""
    if len(value) < _TOKEN_USERNAME_MIN_CHARS:
        return False
    if any(char.isdigit() for char in value):
        return True
    counts = Counter(value)
    entropy = -sum(n / len(value) * math.log2(n / len(value)) for n in counts.values())
    mixed_case = any(char.isupper() for char in value) and any(char.islower() for char in value)
    return entropy >= (_TOKEN_ENTROPY_MIXED_CASE if mixed_case else _TOKEN_ENTROPY_SINGLE_CASE)


def _userinfo_secrets(userinfo: str, *, authority: bool) -> tuple[str, ...]:
    """Secrets in ``user:password`` or, for an authority, a token-like lone username.

    Short names such as ``git`` or ``oauth2`` and recipients such as
    ``mailto:alice`` are not credentials; neither is ever added on its own.
    """
    user, _, password = userinfo.partition(":")
    if password and (authority or user.lower() not in _NON_CREDENTIAL_SCHEMES):
        return userinfo, password
    return (user,) if authority and _token_like(user) else ()


def _sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", _variants(key)[-1].lower())
    return _SENSITIVE_END.search(normalized) is not None


def _query_sensitive(key: str) -> bool:
    return _sensitive_key(key) or key.lower() in {"key", "auth", "sig"}


@dataclass
class _Scan:
    nodes: int = 0
    text_chars: int = 0
    secret_chars: int = 0
    discoveries: int = 0
    secrets: set[str] = field(default_factory=set)
    ancestors: set[int] = field(default_factory=set)

    def add(self, value: str) -> None:
        self.discoveries += 1
        if self.discoveries > MAX_DISCOVERIES:
            raise _RedactionLimitExceeded
        for item in _variants(value):
            if not item or item.startswith("[REDACTED") or item.lower() == "[redacted]":
                continue
            if item not in self.secrets:
                self.secrets.add(item)
                self.secret_chars += len(item)
                if len(self.secrets) > MAX_SECRETS or self.secret_chars > MAX_SECRET_CHARS:
                    raise _RedactionLimitExceeded

    def inspect(self, text: str, sensitive: bool = False) -> None:
        """Record every secret found in ``text``; a ``sensitive`` text is one secret itself."""
        self.text_chars += len(text)
        if self.text_chars > MAX_TEXT_CHARS:
            raise _RedactionLimitExceeded
        if sensitive:
            self.add(text)
        for value in _variants(text):
            self._add_standalone_tokens(value)
            self._add_private_keys(value)
            self._add_assigned_secrets(value)
            self._add_authorization_secrets(value)
            self._add_query_secrets(value)

    def _add_standalone_tokens(self, value: str) -> None:
        for match in _STANDALONE.finditer(value):
            self.add(match.group())

    def _add_private_keys(self, value: str) -> None:
        for match in _PRIVATE_BLOCK.finditer(value):
            self.add(match.group())
            # PEM body lines may also appear alone in tool output or a diff.
            for line in match.group("body").splitlines():
                self.add(line.strip())
                if line.startswith(("+", "-", " ")):
                    self.add(line[1:].strip())

    def _add_assigned_secrets(self, value: str) -> None:
        """Values assigned to a sensitive name, such as ``password = "..."``."""
        for match in _ASSIGNMENT.finditer(value):
            if _sensitive_key(match.group("key")):
                self._add_assigned_value(match.group("value"))

    def _add_assigned_value(self, candidate: str) -> None:
        """Record an assigned value without its quotes; a quote never closed is unsafe."""
        if candidate.startswith(('\\"', "\\'")) and candidate[-2:] == candidate[:2]:
            candidate = candidate[2:-2]
        elif candidate.startswith(('"', "'")) and candidate[-1:] == candidate[:1]:
            if (decoded := _decoded_json_string(candidate)) is not None:
                self.add(decoded)
            candidate = candidate[1:-1]
        elif candidate.startswith(('"', "'")):
            raise _RedactionLimitExceeded
        self.add(candidate)

    def _add_authorization_secrets(self, value: str) -> None:
        """Bearer tokens and the credentials in a URL's user information."""
        for match in _BEARER.finditer(value):
            self.add(match.group("value"))
        for pattern, authority in ((_USERINFO, True), (_BARE_USERINFO, False)):
            for match in pattern.finditer(value):
                for secret in _userinfo_secrets(match.group("value"), authority=authority):
                    self.add(secret)

    def _add_query_secrets(self, value: str) -> None:
        for match in _QUERY.finditer(value):
            if _query_sensitive(unquote_plus(match.group("key"))):
                self.add(match.group("value"))
                self.add(unquote_plus(match.group("value")))

    def collect_secrets_and_copy(self, value: Any, depth: int = 0, sensitive: bool = False) -> Any:
        self.nodes += 1
        if self.nodes > MAX_NODES or depth > MAX_DEPTH:
            raise _RedactionLimitExceeded
        if isinstance(value, str):
            self.inspect(value, sensitive)
            return value
        if value is None or type(value) in {bool, int, float}:
            return value
        if type(value) not in {dict, list, tuple}:
            return "[REDACTED: unsupported value]"
        identity = id(value)
        if identity in self.ancestors:
            raise _RedactionLimitExceeded
        self.ancestors.add(identity)
        try:
            if isinstance(value, dict):
                copied = {}
                for key, item in value.items():
                    if isinstance(key, str):
                        self.inspect(key)
                        child_sensitive = sensitive or _sensitive_key(key)
                    elif key is None or type(key) in {bool, int, float}:
                        child_sensitive = sensitive
                    else:
                        key = "[REDACTED: unsupported key]"
                        child_sensitive = True
                    copied[key] = self.collect_secrets_and_copy(item, depth + 1, child_sensitive)
                return copied
            items = [self.collect_secrets_and_copy(item, depth + 1, sensitive) for item in value]
            return tuple(items) if isinstance(value, tuple) else items
        finally:
            self.ancestors.remove(identity)


def _replacement_patterns(
    secrets: set[str],
) -> tuple[re.Pattern[str] | None, re.Pattern[str] | None]:
    """The patterns that replace ``secrets`` in text and in dictionary keys.

    A short secret such as a password "git" is replaced only as a whole token, so it cannot
    corrupt unrelated words; keys are matched only by secrets long enough to be unique.
    """
    ordered = sorted(secrets, key=len, reverse=True)
    long_secrets = [secret for secret in ordered if len(secret) >= _MIN_SUBSTRING_SECRET]
    short_secrets = [secret for secret in ordered if len(secret) < _MIN_SUBSTRING_SECRET]
    alternatives = []
    if long_secrets:
        alternatives.append("|".join(map(re.escape, long_secrets)))
    if short_secrets:
        alternatives.append(
            _TOKEN_BEFORE + "(?:" + "|".join(map(re.escape, short_secrets)) + ")" + _TOKEN_AFTER
        )
    pattern = re.compile("|".join(alternatives)) if alternatives else None
    key_pattern = re.compile("|".join(map(re.escape, long_secrets))) if long_secrets else None
    return pattern, key_pattern


@dataclass(frozen=True)
class _Replacer:
    """Replaces every discovered secret in a copied report tree."""

    secrets: frozenset[str]
    pattern: re.Pattern[str] | None
    key_pattern: re.Pattern[str] | None

    def text(self, source: str, *, key: bool = False) -> str:
        matcher = self.key_pattern if key else self.pattern
        variants = _variants(source)
        if key and any(variant in self.secrets for variant in variants):
            return REDACTED
        if matcher is None:
            return source
        result = source
        for variant in variants:
            if matcher.search(variant):
                result = variant
        return matcher.sub(REDACTED, result)

    def clean(self, item: Any) -> Any:
        if isinstance(item, str):
            return self.text(item)
        if isinstance(item, dict):
            return {
                self.text(key, key=True) if isinstance(key, str) else key: self.clean(part)
                for key, part in item.items()
            }
        if isinstance(item, (list, tuple)):
            values = [self.clean(part) for part in item]
            return tuple(values) if isinstance(item, tuple) else values
        return item


def redact_data(value: Any) -> Any:
    """Return a sanitized copy, or one fixed marker if complete scanning is unsafe.

    Strings with encoded credentials are decoded for replacement; other strings
    retain their original spelling. If any depth/size/work bound is exceeded, the
    entire value is withheld so credentials hidden beyond the bound cannot leak via
    an earlier field. Callers must never fall back to the unsanitized input.
    """
    scan = _Scan()
    try:
        copied = scan.collect_secrets_and_copy(value)
        if scan.text_chars * max(1, len(scan.secrets)) > MAX_REPLACEMENT_WORK:
            raise _RedactionLimitExceeded
    except _RedactionLimitExceeded:
        return LIMIT_REDACTION
    pattern, key_pattern = _replacement_patterns(scan.secrets)
    return _Replacer(frozenset(scan.secrets), pattern, key_pattern).clean(copied)


def redact_secrets(value: str) -> str:
    """Replace every secret-shaped substring of one string with ``[REDACTED]``."""
    for _, pattern in SECRET_PATTERNS:
        value = pattern.sub(REDACTED, value)
    return value


class _KnownSecretScan:
    """One bounded pass that replaces known secret values and secret-shaped text."""

    def __init__(self, secrets: Collection[str]) -> None:
        if len(secrets) > MAX_SECRETS:
            raise _RedactionLimitExceeded
        self.secrets = sorted((secret for secret in secrets if secret), key=len, reverse=True)
        self.nodes = 0
        self.text_chars = 0

    def clean(self, value: Any, depth: int = 0) -> Any:
        self.nodes += 1
        if self.nodes > MAX_NODES or depth > MAX_DEPTH:
            raise _RedactionLimitExceeded
        if isinstance(value, str):
            self.text_chars += len(value)
            if self.text_chars * max(1, len(self.secrets)) > MAX_REPLACEMENT_WORK:
                raise _RedactionLimitExceeded
            for secret in self.secrets:
                value = value.replace(secret, REDACTED)
            return redact_secrets(value)
        if isinstance(value, dict):
            return {
                str(self.clean(key, depth + 1)): self.clean(item, depth + 1)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            values = [self.clean(item, depth + 1) for item in value]
            return tuple(values) if isinstance(value, tuple) else values
        return value


def redact_known_secrets(value: Any, secrets: Collection[str]) -> Any:
    """Return a copy of a JSON-shaped value with ``secrets`` and secret-shaped text replaced.

    ``secrets`` are values already discovered elsewhere, such as credentials found in a
    project's files; each is replaced wherever it appears, longest first, and
    ``redact_secrets`` then replaces anything that merely looks like a credential.
    Like ``redact_data`` this is bounded in nodes, depth and work, and when a bound is
    exceeded the whole value is withheld as ``LIMIT_REDACTION``, never returned as it was.
    Values that are not strings, lists, tuples or dictionaries are kept unchanged.
    """
    try:
        return _KnownSecretScan(secrets).clean(value)
    except _RedactionLimitExceeded:
        return LIMIT_REDACTION


def local_directory_labels() -> dict[str, str]:
    """The home and temporary directories of this machine, each with its generic label.

    Both the spelled and the symlink-resolved form are listed, because tools report either.
    ``/tmp`` counts as a temporary directory although macOS keeps the real one elsewhere.
    """
    labels: dict[str, str] = {}
    for label, directory in (
        (HOME_LABEL, Path.home),
        (TEMPORARY_LABEL, tempfile.gettempdir),
        (TEMPORARY_LABEL, lambda: "/tmp"),
    ):
        try:
            spelled = Path(directory())
            candidates = {str(spelled), str(spelled.resolve())}
        except (OSError, RuntimeError):
            continue
        for candidate in candidates:
            candidate = candidate.rstrip("/")
            # The filesystem root says nothing about a person.
            if len(candidate) > 1:
                labels.setdefault(candidate, label)
    return labels


def scrub_local_paths(value: Any, labels: dict[str, str] | None = None) -> Any:
    """Return a copy of ``value`` with machine-specific directories shown generically.

    The home directory becomes ``~`` and the temporary directory ``<tmp>``, so a shared
    report does not reveal a user name or an institution's directory layout. Paths
    relative to a project are unchanged, and so is any text that merely starts like a
    directory name (``/home/ann`` does not match ``/home/anna``). Only strings are
    rewritten; dictionary keys keep their names.
    """
    generic = local_directory_labels() if labels is None else labels
    if not generic:
        return value
    pattern = re.compile(
        r"(?<![\w.~-])(?:"
        + "|".join(re.escape(directory) for directory in sorted(generic, key=len, reverse=True))
        + r")(?![\w-]|\.\w)"
    )

    def scrub_text(text: str) -> str:
        return pattern.sub(lambda match: generic[match.group()], text)

    def scrub(item: Any) -> Any:
        if isinstance(item, str):
            return scrub_text(item)
        if isinstance(item, dict):
            return {key: scrub(part) for key, part in item.items()}
        if isinstance(item, (list, tuple)):
            values = [scrub(part) for part in item]
            return tuple(values) if isinstance(item, tuple) else values
        return item

    return scrub(value)
