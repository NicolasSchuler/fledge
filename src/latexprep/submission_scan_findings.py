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
    """How much one submission scan reports and reads."""

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
    """How one rule reports: its name, the scope sentence, its weight and its wording."""

    name: str
    scope: str
    advisory: bool = False
    describe: Describe | None = None


def short_name(value: str) -> str:
    """Shorten an over-long file name; it is the finding itself and the details keep it whole."""
    return value if len(value) <= _MAX_LABEL_CHARS else value[: _MAX_LABEL_CHARS - 1] + "…"


def listed(items: Sequence[str]) -> str:
    """The first few distinct items; the suffix counts hidden items, repeats included."""
    shown = list(dict.fromkeys(items))[:MAX_LISTED]
    return ", ".join(shown) + more_suffix(sum(item not in shown for item in items))


def match_count(matches: Sequence[object], limits: ScanLimits) -> str:
    """The number of matches, with ``+`` when collecting stopped at the limit."""
    return f"{len(matches)}{'+' if len(matches) == limits.max_matches else ''}"


def _match_label(match: Mapping[str, object]) -> str:
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
    """The finding for one scan: a violation fails it, an incomplete scan leaves it open."""
    status = check_status(failed=bool(matches), incomplete=bool(issues))
    weight = ADVISORY if rule.advisory else DIRECT_POLICY
    # Scans stop collecting at the reporting limit, so further matches may be unlisted.
    limited = len(matches) >= limits.max_matches
    return Finding(
        rule.name,
        _message(rule, matches, issues, limits),
        severity=weight.severity(status),
        status=status,
        evidence_kind=weight.evidence_kind,
        incomplete=bool(issues) or limited,
        details={
            "scope": rule.scope,
            "matches": [dict(item) for item in matches[: limits.max_matches]],
            "match_output_limited": limited,
            "issues": issues[: limits.max_matches],
            **(details or {}),
        },
    )
