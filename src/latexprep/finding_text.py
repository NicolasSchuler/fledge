"""Wording and bookkeeping shared by the checks that build findings.

``check_status`` turns "violated" and "incomplete" into a status, ``list_locations``
and ``more_suffix`` name where a problem is without listing hundreds of places,
``plural`` counts regularly pluralized nouns, and ``redact_findings`` removes
discovered secrets from every string a finding reports. Which text a check reports is
that check's own; scanning files is ``file_scan``'s work.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace

from .cancellation import cancellation_point
from .findings import Finding, Status
from .redaction import LIMIT_REDACTION, redact_known_secrets


def check_status(*, failed: bool, incomplete: bool) -> Status:
    """A confirmed violation fails even when other evidence is incomplete."""
    if failed:
        return "failed"
    return "inconclusive" if incomplete else "passed"


def location_label(item: Mapping[str, object]) -> str:
    """``path:line`` for a reported item, ``path`` without a line, or nothing without a path."""
    path, line = item.get("path"), item.get("line")
    if not isinstance(path, str):
        return ""
    return f"{path}:{line}" if isinstance(line, int) else path


def more_suffix(hidden: int) -> str:
    """`` (+N more)`` for ``hidden`` items left out of a listing, or nothing."""
    return f" (+{hidden} more)" if hidden > 0 else ""


def plural(count: int, noun: str) -> str:
    """``1 float``, ``3 floats``: a count with its regularly pluralized noun."""
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def list_locations(items: Sequence[Mapping[str, object]], limit: int = 3) -> str:
    """Distinct item locations, the first ``limit`` named: ``a.tex:1, b.tex (+2 more)``."""
    places = list(dict.fromkeys(filter(None, map(location_label, items))))
    return ", ".join(places[:limit]) + more_suffix(len(places) - limit)


def redact_findings(findings: list[Finding], secrets: set[str]) -> list[Finding]:
    """Remove discovered secret values and secret-shaped text from every reported string.

    A finding too large to scan within the redaction bounds is replaced by one that
    reports only that its content was withheld.
    """
    redacted = []
    for item in findings:
        cancellation_point()
        cleaned = redact_known_secrets(
            {
                "path": item.path,
                "message": item.message,
                "suggestion": item.suggestion,
                "details": item.details,
            },
            secrets,
        )
        if isinstance(cleaned, dict):
            redacted.append(
                replace(
                    item,
                    path=cleaned["path"] or None,
                    message=cleaned["message"],
                    suggestion=cleaned["suggestion"] or None,
                    details=cleaned["details"],
                )
            )
        else:
            redacted.append(
                replace(item, path=None, message=LIMIT_REDACTION, suggestion=None, details={})
            )
    return redacted
