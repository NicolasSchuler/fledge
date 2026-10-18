"""One display view per finding, shared by every report renderer.

Titles, locations, status labels, evidence excerpts, stage de-duplication,
ordering and default visibility are derived here once, and so are the report-wide
summary lines (result counts, change counts, check selection). Renderers only escape
the text for their medium: terminal, compact, HTML or CI annotations. Views are built
only from a ``RedactedReport``.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import NamedTuple

from .execution_records import CheckSelectionRecord
from .findings import DetailKey, Finding
from .report import RedactedReport
from .review_policy import review_marker
from .rules import BY_NAME
from .task_findings import TASK_DETAIL, blocked_prerequisites


@dataclass(frozen=True, slots=True, kw_only=True)
class RenderOptions:
    """What a rendering lists in addition to unresolved findings.

    Terminal, compact and CI output honor ``show_passed``; ``show_diff`` and
    ``show_files`` apply to the terminal report. JSON and HTML list everything.
    """

    show_passed: bool = False
    show_diff: bool = False
    show_files: bool = False


DEFAULT_RENDER_OPTIONS = RenderOptions()

_STATUS_LABELS = {"skipped": "not checked", "not_applicable": "not applicable"}


# Findings that disclose what leaves the machine. They are passed notices, but a reader
# must see them without asking whenever the run sent anything.
ALWAYS_LISTED_RULES = frozenset({"online.sharing"})


def _hidden_by_default(finding: Finding) -> bool:
    # Passed results and scope disclaimers are counted but listed only on request.
    if finding.rule in ALWAYS_LISTED_RULES:
        return False
    return finding.status == "passed" or finding.is_scope_disclaimer


def size_label(value: object) -> str:
    """A byte count in binary units, or "not measured" when none was recorded."""
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return "not measured"
    if value < 1024:
        return f"{value} B"
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        value /= 1024
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
    raise AssertionError("size unit missing")


# Checkers keep their evidence in these detail lists behind a generic message.
_EVIDENCE_KEYS = (
    "matches",
    DetailKey.VIOLATIONS,
    "missing",
    "entries",
    DetailKey.CHANGED_PAGES,
    DetailKey.DIFFERING_PAGES,
    "nonembedded",
    "below_minimum",
    "candidates",
    "issues",
)
_EVIDENCE_PAGE_KEYS = frozenset({DetailKey.CHANGED_PAGES, DetailKey.DIFFERING_PAGES})
_EVIDENCE_ITEMS = 3
_EVIDENCE_LABEL_CHARS = 120
_EVIDENCE_NAME_KEYS = ("name", "key", "reference", "label", "doi", "reason")


def _clipped(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _evidence_label(item: object, *, pages: bool) -> str | None:
    label: object = None
    if isinstance(item, bool):
        return None
    if isinstance(item, int):
        label = f"page {item}" if pages else str(item)
    elif isinstance(item, str):
        label = item
    elif isinstance(item, Mapping):
        location = next(
            (item[key] for key in ("path", "file") if isinstance(item.get(key), str)), ""
        )
        if location and type(item.get("line")) is int:
            location = f"{location}:{item['line']}"
        if type(item.get(DetailKey.PAGE)) is int:
            page = f"page {item[DetailKey.PAGE]}"
            reason = item.get("reason")
            if location:
                location = f"{location} ({page})"
            elif isinstance(reason, str) and reason:
                # Without a file, the reason is the only thing that says what the page shows.
                suffix = f" ({page})"
                location = _clipped(reason, _EVIDENCE_LABEL_CHARS - len(suffix)) + suffix
            else:
                location = page
        label = location or next(
            (item[key] for key in _EVIDENCE_NAME_KEYS if isinstance(item.get(key), str)), ""
        )
    if not isinstance(label, str) or not label:
        return None
    return _clipped(label, _EVIDENCE_LABEL_CHARS)


def evidence_excerpt(finding: Finding) -> str:
    """A short, display-only excerpt of recorded evidence for an unresolved finding.

    Only the first few items of the first populated list among a fixed set of
    detail keys are summarized; the caller must sanitize the text for its medium.
    """
    if finding.status == "passed":
        return ""
    for key in _EVIDENCE_KEYS:
        items = finding.details.get(key)
        if not isinstance(items, (list, tuple)) or not items:
            continue
        hidden = 0
        count = finding.details.get(DetailKey.VIOLATION_COUNT)
        if key == DetailKey.VIOLATIONS and type(count) is int and count > len(items):
            hidden = count - len(items)
        labels = [_evidence_label(item, pages=key in _EVIDENCE_PAGE_KEYS) for item in items]
        shown = list(dict.fromkeys(label for label in labels if label is not None))[
            :_EVIDENCE_ITEMS
        ]
        if shown:
            hidden += sum(label not in shown for label in labels)
            return ", ".join(shown) + (f" (+{hidden} more)" if hidden else "")
    return ""


def _title(finding: Finding) -> str:
    definition = BY_NAME.get(finding.rule)
    return definition.title if definition else finding.rule.replace("_", " ").replace(".", " / ")


def _location(finding: Finding, stages: tuple[str, ...]) -> str:
    location = finding.path or "project"
    if document := finding.document:
        location = f"{document}: {location}"
    if finding.line is not None:
        location += f":{finding.line}"
    elif (input_line := finding.details.get(DetailKey.INPUT_LINE)) is not None:
        location += f" (reported input line {input_line}; file unresolved)"
    page = finding.page
    if page is not None:
        location += f" (page {page})"
    if stages:
        location += f" [{', '.join(stages)}]"
    elif DetailKey.STAGE in finding.details:
        location += f" [{finding.details[DetailKey.STAGE]}]"
    return location


def _priority(finding: Finding) -> int:
    if finding.status == "passed":
        return 4
    if finding.severity == "error":
        return 0 if finding.status == "failed" else 1
    return 2 if finding.severity == "warning" else 3


def _identity(finding: Finding) -> tuple[object, ...]:
    # The same result re-checked at another stage (baseline, transformed, archive
    # rebuild or final sources) is one issue for a reader; JSON keeps every stage.
    # Copies with different evidence or review decisions stay apart, so no result
    # hides distinct measurements or an unaccepted copy.
    details = finding.details
    return (
        finding.rule,
        finding.severity,
        finding.status,
        finding.message,
        str(details.get(DetailKey.DOCUMENT, "")),
        finding.path,
        finding.line,
        str(details.get(DetailKey.PAGE)),
        str(details.get(DetailKey.INPUT_LINE)),
        finding.evidence_kind,
        finding.incomplete,
        json.dumps(
            {key: value for key, value in details.items() if key != DetailKey.STAGE},
            sort_keys=True,
        ),
        review_marker(finding),
    )


class StagedFinding(NamedTuple):
    """A distinct finding and the stages its identical copies came from, if several."""

    finding: Finding
    stages: tuple[str, ...]


def merge_stage_copies(findings: Iterable[Finding]) -> list[StagedFinding]:
    """One entry per distinct finding with the stages its identical copies came from.

    A copy without a stage (the initial source analysis) represents the group and
    later identical re-checks are omitted; otherwise the stages are listed in the
    order the job reached them.
    """
    groups: dict[tuple[object, ...], list[Finding]] = {}
    for finding in findings:
        groups.setdefault(_identity(finding), []).append(finding)
    entries = []
    for members in groups.values():
        unstaged = next((item for item in members if DetailKey.STAGE not in item.details), None)
        if unstaged is not None or len(members) == 1:
            entries.append(StagedFinding(unstaged or members[0], ()))
            continue
        stages = tuple(dict.fromkeys(str(item.stage) for item in members))
        entries.append(StagedFinding(members[0], stages if len(stages) > 1 else ()))
    return entries


def not_run_summaries(report: RedactedReport) -> list[str]:
    """One line per failed prerequisite instead of one note per task it prevented."""
    groups: dict[tuple[str, tuple[str, ...]], list[str]] = {}
    for finding in report.findings:
        if prerequisites := blocked_prerequisites(finding):
            key = (finding.document or "", prerequisites)
            task = str(finding.details.get(TASK_DETAIL, finding.message))
            if task not in groups.setdefault(key, []):
                groups[key].append(task)
    lines = []
    for (document, prerequisites), tasks in groups.items():
        count = len(tasks)
        lines.append(
            (f"{document}: " if document else "")
            + f"{count} check{'s' if count != 1 else ''} did not run because "
            + f"{' and '.join(prerequisites)} failed: {', '.join(sorted(tasks))}"
        )
    return lines


def result_counts(report: RedactedReport) -> str:
    """The one-line tally of errors, warnings and unresolved or passed results."""
    findings = [entry.finding for entry in merge_stage_copies(report.findings)]
    errors = sum(item.severity == "error" and item.status != "passed" for item in findings)
    warnings = sum(item.severity == "warning" and item.status != "passed" for item in findings)
    statuses = Counter(item.status for item in findings)
    # Informational scope notes are neither failures nor missing evidence: count them apart.
    skipped_notes = sum(item.status == "skipped" and item.is_scope_disclaimer for item in findings)
    not_applicable = statuses["not_applicable"] + skipped_notes
    incomplete = statuses["inconclusive"] + statuses["skipped"] - skipped_notes
    return (
        f"{errors} error{'s' if errors != 1 else ''}, "
        f"{warnings} warning{'s' if warnings != 1 else ''}, {incomplete} incomplete, "
        f"{not_applicable} not applicable, "
        f"{statuses['passed']} passed result{'s' if statuses['passed'] != 1 else ''}"
    )


def change_summary(report: RedactedReport) -> str:
    """The changes counted by kind, most frequent first: ``exclude: 3, move: 1``."""
    kinds = Counter(change.kind for change in report.changes)
    ordered = sorted(kinds.items(), key=lambda item: (-item[1], item[0]))
    return ", ".join(f"{kind}: {count}" for kind, count in ordered)


def selection_description(selection: CheckSelectionRecord | None) -> str | None:
    """What the check selection restricts, or None when every check was selected."""
    if selection is None or (selection.select == ("ALL",) and not selection.ignore):
        return None
    return (
        f"Checks: select {', '.join(selection.select) or '(none)'}; "
        f"ignore {', '.join(selection.ignore) or '(none)'}; required validation retained"
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class FindingView:
    """Unescaped display text for one finding; each renderer escapes it for its medium.

    ``code`` is the check code, or the rule name of an uncoded diagnostic. Empty
    ``evidence``, ``next_step`` and ``review`` texts are omitted from output.
    """

    finding: Finding
    code: str
    title: str
    location: str
    message: str
    evidence: str
    next_step: str
    review: str
    status_label: str
    stages: tuple[str, ...]

    @classmethod
    def from_finding(cls, finding: Finding, stages: tuple[str, ...] = ()) -> FindingView:
        return cls(
            finding=finding,
            code=finding.code or finding.rule,
            title=_title(finding),
            location=_location(finding, stages),
            message=finding.message,
            evidence=evidence_excerpt(finding),
            next_step=finding.next_step or "",
            review=review_marker(finding),
            status_label=_STATUS_LABELS.get(finding.status, finding.status),
            stages=stages,
        )


def finding_views(report: RedactedReport, *, show_hidden: bool) -> list[FindingView]:
    """One view per distinct finding, the most urgent first.

    Passed results and scope disclaimers are listed only with ``show_hidden``, except
    the notices in ``ALWAYS_LISTED_RULES``.
    Tasks that never started because a prerequisite failed are left to
    :func:`not_run_summaries`.
    """
    order = {id(item): index for index, item in enumerate(report.sorted_findings())}
    entries = merge_stage_copies(
        item
        for item in report.findings
        if not blocked_prerequisites(item) and (show_hidden or not _hidden_by_default(item))
    )
    entries.sort(key=lambda entry: (_priority(entry.finding), order[id(entry.finding)]))
    return [FindingView.from_finding(entry.finding, entry.stages) for entry in entries]
