"""Findings for the matches of a submission scan, and the wording they share.

Every submission scan (identity terms, secrets, file-name policies, deliverables, the
archive) ends with a list of matches and a list of reasons the scan was incomplete.
``scan_finding`` turns that into one finding: a status from the two lists, a message
with a count and the first few places, and details that carry up to
``ScanLimits.max_matches`` matches. A rule may supply its own message wording; the
default wording ("N matches: location") never repeats matched text, so identity terms
and secrets stay out of the report. This module scans nothing itself.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import NamedTuple

from .file_scan import MAX_SCAN_MATCHES
from .finding_text import check_status, more_suffix
from .finding_weight import ADVISORY, DIRECT_POLICY
from .findings import Finding
from .tex_lexer import MAX_TOTAL_SOURCE_BYTES

# Matches named in a message; the details list every match up to ``max_matches``.
MAX_LISTED = 3
# Longer file names are shortened in messages; the details keep them whole.
_MAX_LABEL_CHARS = 120


class ScanLimits(NamedTuple):
    """How much one submission scan reports and reads.

    Attributes:
        max_matches: The most matches one rule collects and reports.
        max_pdf_evidence_bytes: The most extracted PDF text and metadata read in total.
    """

    # Matches one rule reports before it stops collecting; a full list means "at least".
    max_matches: int = MAX_SCAN_MATCHES
    # Extracted PDF text and metadata read in total, in UTF-8 bytes.
    max_pdf_evidence_bytes: int = MAX_TOTAL_SOURCE_BYTES


DEFAULT_SCAN_LIMITS = ScanLimits()

# Renders the matches of one rule for its message, given their count text (``5`` or
# ``200+``). Rules without one use the generic "N matches: location" wording.
Describe = Callable[[Sequence[Mapping[str, object]], str], str]


@dataclass(frozen=True, slots=True)
class ScanRule:
    """How one rule reports: its name, the scope sentence, its weight and its wording.

    Attributes:
        name: The rule name of the finding.
        scope: The sentence that states what the scan covers, kept in the details.
        advisory: Whether the finding has the advisory weight instead of the direct-policy
            weight.
        describe: The rule's own message wording for its matches, or ``None`` for the
            generic "N matches: location" wording.
    """

    name: str
    scope: str
    advisory: bool = False
    describe: Describe | None = None


def short_name(value: str) -> str:
    """Shorten an over-long file name; it is the finding itself and the details keep it whole.

    Args:
        value: The file name or label to shorten.

    Returns:
        ``value`` itself when it is at most ``_MAX_LABEL_CHARS`` characters long, otherwise
        its start followed by an ellipsis.
    """
    return value if len(value) <= _MAX_LABEL_CHARS else value[: _MAX_LABEL_CHARS - 1] + "…"


def listed(items: Sequence[str]) -> str:
    """Return the first few distinct items; the suffix counts hidden items, repeats included.

    Args:
        items: The items to list, in order.

    Returns:
        At most ``MAX_LISTED`` distinct items joined by commas, followed by a count of the
        items not shown.
    """
    shown = list(dict.fromkeys(items))[:MAX_LISTED]
    return ", ".join(shown) + more_suffix(sum(item not in shown for item in items))


def _limited(matches: Sequence[object], limits: ScanLimits) -> bool:
    """Return whether the matches reach the reporting limit, so further ones may be unlisted.

    Args:
        matches: The matches one rule collected.
        limits: The limits of the scan, whose ``max_matches`` is the reporting limit.

    Returns:
        ``True`` when there are at least ``limits.max_matches`` matches.
    """
    return len(matches) >= limits.max_matches


def match_count(matches: Sequence[object], limits: ScanLimits) -> str:
    """Return the number of reported matches, with ``+`` when they reach the limit.

    A finding reports at most ``limits.max_matches`` matches, and reaching that many
    means further matches may be unlisted. A rule that collected more than the limit, by
    adding several matches at once or by not stopping at all, is therefore counted as the
    limit followed by ``+``, as many as the details keep.

    Args:
        matches: The matches one rule collected.
        limits: The limits of the scan, whose ``max_matches`` is the reporting limit.

    Returns:
        The count as text, such as ``5`` or ``200+``.
    """
    if _limited(matches, limits):
        return f"{limits.max_matches}+"
    return str(len(matches))


def _match_label(match: Mapping[str, object]) -> str:
    """Return the message label of one match: its location and reason.

    The location is the path, or the channel when there is no path, with the line when the
    match has one; a match without either is an "unspecified location".

    Args:
        match: One match of a scan.

    Returns:
        The label, with an over-long file name shortened.
    """
    # Matches hold only locations and generated reasons, never matched text or terms.
    path, line, reason = match.get("path"), match.get("line"), match.get("reason")
    label = path if isinstance(path, str) else match.get("channel")
    label = label if isinstance(label, str) else ""
    if label and type(line) is int:
        label += f":{line}"
    label = short_name(label)
    if isinstance(reason, str):
        label = f"{label} ({reason})" if label else reason
    return label or "unspecified location"


def _message(
    rule: ScanRule,
    matches: Sequence[Mapping[str, object]],
    issues: list[str],
    limits: ScanLimits,
) -> str:
    """Return the finding message for one scan's matches and issues.

    Args:
        rule: The rule, whose own wording replaces the generic one when it has one.
        matches: The matches of the scan.
        issues: The reasons the scan was incomplete.
        limits: The limits of the scan.

    Returns:
        The match count and first locations, or a statement that nothing matched, with the
        first issue appended when the scan was incomplete.
    """
    if not matches:
        if not issues:
            return "No matches in the stated scope."
        return f"The configured scan is incomplete: {issues[0]}{more_suffix(len(issues) - 1)}"
    count = match_count(matches, limits)
    if rule.describe is not None:
        message = rule.describe(matches, count)
    else:
        labels = [_match_label(match) for match in matches]
        message = f"{count} match{'es' if len(matches) != 1 else ''}: {listed(labels)}"
    if issues:
        message += f"; the scan was also incomplete: {issues[0]}"
    return message


def scan_finding(
    rule: ScanRule,
    matches: Sequence[Mapping[str, object]],
    issues: list[str],
    limits: ScanLimits,
    *,
    details: dict[str, object] | None = None,
) -> Finding:
    """Return the finding for one scan: a violation fails it, an incomplete scan leaves it open.

    The message and the details report the first ``limits.max_matches`` matches. A scan
    that reached that many counts as incomplete, because further matches may be unlisted,
    and its count carries a ``+``.

    Args:
        rule: How the rule reports.
        matches: The matches of the scan; the message and the details report up to
            ``limits.max_matches``.
        issues: The reasons the scan was incomplete; the details keep up to
            ``limits.max_matches``.
        limits: The limits of the scan.
        details: Further details to add to the finding, or ``None``.

    Returns:
        The finding of the scan.
    """
    status = check_status(failed=bool(matches), incomplete=bool(issues))
    weight = ADVISORY if rule.advisory else DIRECT_POLICY
    # Scans stop collecting at the reporting limit, so further matches may be unlisted.
    limited = _limited(matches, limits)
    reported = matches[: limits.max_matches]
    return Finding(
        rule.name,
        _message(rule, reported, issues, limits),
        severity=weight.severity(status),
        status=status,
        evidence_kind=weight.evidence_kind,
        incomplete=bool(issues) or limited,
        details={
            "scope": rule.scope,
            "matches": [dict(item) for item in reported],
            "match_output_limited": limited,
            "issues": issues[: limits.max_matches],
            **(details or {}),
        },
    )
