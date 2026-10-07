"""Bounded credential redaction for complete, JSON/asdict-shaped report trees.

Discovery precedes replacement so a credential found in settings also disappears
from earlier logs, metadata, suggestions and diffs. This module performs no I/O
and does not import report models. Unsupported values are never stringified.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, unquote_plus

REDACTED = "[REDACTED]"
LIMIT_REDACTION = "[REDACTED: report exceeds redaction limits]"
MAX_TEXT_CHARS = 16 * 1024 * 1024
MAX_NODES = 100_000
MAX_DEPTH = 64
MAX_SECRETS = 1024
MAX_DISCOVERIES = 100_000
MAX_SECRET_CHARS = 1024 * 1024
MAX_REPLACEMENT_WORK = 128 * 1024 * 1024

# Shared with the optional source credential check. Detection remains heuristic;
# the report boundary additionally handles short contextual credentials below.
SECRET_PATTERNS = (
    ("access-key-shaped token", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("credential-shaped token", re.compile(r"\b(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{20,255}")),
    ("credential-shaped token", re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,255}")),
    ("credential-shaped token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,255}")),
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
_STANDALONE = re.compile(
    r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b|"
    r"\b(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{20,}|"
    r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}|"
    r"\bxox[baprs]-[A-Za-z0-9-]{20,}"
)
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
_QUERY = re.compile(r"(?:[?&#;])(?P<key>[^\s=&;#]{1,256})=(?P<value>[^\s&#;<>\"']*)")


class _Limit(Exception):
    """Fail closed without exposing the unprocessed report."""


def _variants(text: str) -> list[str]:
    result = [text]
    for _ in range(3):
        decoded = unquote(result[-1])
        if decoded == result[-1]:
            break
        result.append(decoded)
    if unquote(result[-1]) != result[-1]:
        raise _Limit
    return result


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
            raise _Limit
        for item in _variants(value):
            if not item or item.startswith("[REDACTED") or item.lower() == "[redacted]":
                continue
            if item not in self.secrets:
                self.secrets.add(item)
                self.secret_chars += len(item)
                if len(self.secrets) > MAX_SECRETS or self.secret_chars > MAX_SECRET_CHARS:
                    raise _Limit

    def inspect(self, text: str, sensitive: bool = False) -> None:
        self.text_chars += len(text)
        if self.text_chars > MAX_TEXT_CHARS:
            raise _Limit
        if sensitive:
            self.add(text)
        for value in _variants(text):
            for match in _STANDALONE.finditer(value):
                self.add(match.group())
            for match in _PRIVATE_BLOCK.finditer(value):
                self.add(match.group())
                # PEM body lines may also appear alone in tool output or a diff.
                for line in match.group("body").splitlines():
                    self.add(line.strip())
                    if line.startswith(("+", "-", " ")):
                        self.add(line[1:].strip())
            for match in _ASSIGNMENT.finditer(value):
                if _sensitive_key(match.group("key")):
                    candidate = match.group("value")
                    if candidate.startswith(('\\"', "\\'")) and candidate[-2:] == candidate[:2]:
                        candidate = candidate[2:-2]
                    elif candidate.startswith(('"', "'")) and candidate[-1:] == candidate[:1]:
                        if candidate.startswith('"'):
                            try:
                                self.add(json.loads(candidate))
                            except ValueError:
                                pass
                        candidate = candidate[1:-1]
                    elif candidate.startswith(('"', "'")):
                        raise _Limit
                    self.add(candidate)
            for match in _BEARER.finditer(value):
                self.add(match.group("value"))
            for pattern in (_USERINFO, _BARE_USERINFO):
                for match in pattern.finditer(value):
                    credential = match.group("value")
                    self.add(credential)
                    for part in credential.split(":", 1):
                        self.add(part)
            for match in _QUERY.finditer(value):
                if _query_sensitive(unquote_plus(match.group("key"))):
                    self.add(match.group("value"))
                    self.add(unquote_plus(match.group("value")))

    def copy(self, value: Any, depth: int = 0, sensitive: bool = False) -> Any:
        self.nodes += 1
        if self.nodes > MAX_NODES or depth > MAX_DEPTH:
            raise _Limit
        if isinstance(value, str):
            self.inspect(value, sensitive)
            return value
        if value is None or type(value) in {bool, int, float}:
            return value
        if type(value) not in {dict, list, tuple}:
            return "[REDACTED: unsupported value]"
        identity = id(value)
        if identity in self.ancestors:
            raise _Limit
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
                    copied[key] = self.copy(item, depth + 1, child_sensitive)
                return copied
            items = [self.copy(item, depth + 1, sensitive) for item in value]
            return tuple(items) if isinstance(value, tuple) else items
        finally:
            self.ancestors.remove(identity)


def redact_data(value: Any) -> Any:
    """Return a sanitized copy, or one fixed marker if complete scanning is unsafe.

    Strings with encoded credentials are decoded for replacement; other strings
    retain their original spelling. If any depth/size/work bound is exceeded, the
    entire value is withheld so credentials hidden beyond the bound cannot leak via
    an earlier field. Callers must never fall back to the unsanitized input.
    """
    scan = _Scan()
    try:
        copied = scan.copy(value)
        if scan.text_chars * max(1, len(scan.secrets)) > MAX_REPLACEMENT_WORK:
            raise _Limit
    except _Limit:
        return LIMIT_REDACTION
    secrets = sorted(scan.secrets, key=len, reverse=True)
    pattern = re.compile("|".join(map(re.escape, secrets))) if secrets else None
    long_secrets = [secret for secret in secrets if len(secret) >= 8]
    key_pattern = re.compile("|".join(map(re.escape, long_secrets))) if long_secrets else None

    def text(source: str, *, key: bool = False) -> str:
        matcher = key_pattern if key else pattern
        variants = _variants(source)
        if key and any(variant in scan.secrets for variant in variants):
            return REDACTED
        if matcher is None:
            return source
        result = source
        for variant in variants:
            if matcher.search(variant):
                result = variant
        return matcher.sub(REDACTED, result)

    def clean(item: Any) -> Any:
        if isinstance(item, str):
            return text(item)
        if isinstance(item, dict):
            return {
                text(key, key=True) if isinstance(key, str) else key: clean(part)
                for key, part in item.items()
            }
        if isinstance(item, (list, tuple)):
            values = [clean(part) for part in item]
            return tuple(values) if isinstance(item, tuple) else values
        return item

    return clean(copied)
