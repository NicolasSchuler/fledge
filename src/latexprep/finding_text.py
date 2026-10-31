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
    """Turn a confirmed violation and incomplete evidence into a status.

    A confirmed violation fails even when other evidence is incomplete.

    Args:
        failed: Whether a violation was confirmed.
        incomplete: Whether the evidence is incomplete.

    Returns:
        ``failed`` for a confirmed violation, otherwise ``inconclusive`` for incomplete
        evidence and ``passed`` for complete evidence.
    """
    if failed:
        return "failed"
    return "inconclusive" if incomplete else "passed"


def location_label(item: Mapping[str, object]) -> str:
    """Label the location of a reported item.

    Returns:
        ``path:line`` for an item with a path and a line, ``path`` without a line, or an
        empty string without a path.
    """
    path, line = item.get("path"), item.get("line")
    if not isinstance(path, str):
        return ""
    return f"{path}:{line}" if isinstance(line, int) else path


def more_suffix(hidden: int) -> str:
    """Return `` (+N more)`` for ``hidden`` items left out of a listing, or nothing."""
    return f" (+{hidden} more)" if hidden > 0 else ""


def plural(count: int, noun: str) -> str:
    """Write a count with its regularly pluralized noun: ``1 float``, ``3 floats``."""
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def list_locations(items: Sequence[Mapping[str, object]], limit: int = 3) -> str:
    """List the distinct locations of ``items``, naming the first ``limit``.

    Args:
        items: Reported items with optional ``path`` and ``line`` keys; items without a
            path are left out.
        limit: How many locations to name before the rest are only counted.

    Returns:
        The locations joined by commas, such as ``a.tex:1, b.tex (+2 more)``.
    """
    places = list(dict.fromkeys(filter(None, map(location_label, items))))
    return ", ".join(places[:limit]) + more_suffix(len(places) - limit)


def redact_findings(findings: list[Finding], secrets: set[str]) -> list[Finding]:
    """Remove discovered secret values and secret-shaped text from every reported string.

    A finding too large to scan within the redaction bounds is replaced by one that
    reports only that its content was withheld.

    Args:
        findings: The findings whose path, message, suggestion and details are redacted.
        secrets: The discovered secret values.

    Returns:
        The redacted findings, in the order given.
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
