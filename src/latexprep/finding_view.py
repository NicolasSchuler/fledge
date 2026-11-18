"""One display view per finding, shared by every report renderer.

Titles, locations, status labels, evidence excerpts, stage de-duplication, ordering,
default visibility and which result follows from which are derived here once, and so
are the report-wide lines (result counts, change counts, check selection, the settings
in effect and the closing verdict). Renderers only escape the text for their medium:
terminal, compact, HTML or CI annotations. Views are built only from a
``RedactedReport``.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import NamedTuple

from .citation_graph import NO_MAIN_SOURCE
from .execution_records import CONFIG_PATH_KEY, CheckSelectionRecord, read_execution_records
from .findings import (
    DOCUMENT_SUMMARY_RULE,
    DetailKey,
    Finding,
    follows_stopped_build,
    stopped_build_stages,
)
from .operational_titles import OPERATIONAL_TITLES, build_stage_title
from .report import Outcome, RedactedReport
from .review_policy import review_marker
from .rules import BY_NAME
from .task_findings import TASK_DETAIL, blocked_prerequisites, cancelled_task


@dataclass(frozen=True, slots=True, kw_only=True)
class RenderOptions:
    """What a rendering lists in addition to unresolved findings, and how it names places.

    JSON and HTML list everything.

    Attributes:
        show_passed: Whether passed results and scope disclaimers are listed as well;
            terminal, compact and CI output honor it.
        show_diff: Whether every change is listed with its diff; the terminal report
            honors it.
        show_files: Whether every file of each package is listed; the terminal report
            honors it.
        settings_file: The settings file as the terminal and compact headers name it, a
            path the user can open from the working directory; ``None`` names the file the
            report records, with local directories generalized.
        annotation_prefix: The project's directory relative to the repository root, which
            CI annotations put before each file path; empty when the paths of the findings
            are already relative to it.
    """

    show_passed: bool = False
    show_diff: bool = False
    show_files: bool = False
    settings_file: str | None = None
    annotation_prefix: str = ""


DEFAULT_RENDER_OPTIONS = RenderOptions()

_STATUS_LABELS = {"skipped": "not checked", "not_applicable": "not applicable"}


# Findings that disclose what leaves the machine. They are passed notices, but a reader
# must see them without asking whenever the run sent anything.
ALWAYS_LISTED_RULES = frozenset({"online.sharing"})


def _hidden_by_default(finding: Finding) -> bool:
    """Return whether ``finding`` is listed only on request."""
    # Passed results and scope disclaimers are counted but listed only on request.
    if finding.rule in ALWAYS_LISTED_RULES:
        return False
    return finding.status == "passed" or finding.is_scope_disclaimer


def size_label(value: object) -> str:
    """Format a byte count in binary units, or "not measured" when none was recorded."""
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
    """Shorten ``text`` to ``limit`` characters, ending in an ellipsis when it was cut."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _evidence_label(item: object, *, pages: bool) -> str | None:
    """Name one evidence item in a few words.

    A mapping is named by its file and line and its page, by its reason and page when it
    has no file, or else by the first of ``_EVIDENCE_NAME_KEYS`` it holds.

    Args:
        item: The evidence item: a number, a text or a mapping.
        pages: Whether a number is a page number.

    Returns:
        The label, clipped to ``_EVIDENCE_LABEL_CHARS`` characters, or ``None`` when the item
        names nothing.
    """
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
    """Excerpt the recorded evidence of an unresolved finding, for display only.

    Only the first few items of the first populated list among a fixed set of
    detail keys are summarized; the caller must sanitize the text for its medium.

    Returns:
        The distinct item labels joined by commas, with a count of the items left out,
        or an empty string for a passed finding or one without such evidence.
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
    """Return the rule's title: the catalogue's, an operational result's or a readable name."""
    definition = BY_NAME.get(finding.rule)
    if definition is not None:
        return definition.title
    if (title := OPERATIONAL_TITLES.get(finding.rule)) is not None:
        return title
    if finding.is_build_follow_on:
        return build_stage_title(finding.rule.partition(".")[2])
    return finding.rule.replace("_", " ").replace(".", " / ")


def _place(finding: Finding) -> str:
    """Name the file and line of a finding, or ``project`` when it has no file."""
    place = finding.path or "project"
    return f"{place}:{finding.line}" if finding.line is not None else place


def _location(finding: Finding, stages: tuple[str, ...], project: str = "project") -> str:
    """Describe where a finding is: document, file and line or input line, page and stages.

    Args:
        finding: The finding to locate; one without a path is in ``project``.
        stages: The stages its identical copies came from; without them, the finding's
            own stage is named.
        project: What a finding without a path is in; empty when the run examined no
            project, as setup diagnostics without one do.

    Returns:
        The location text, empty for a finding of no project without a stage.
    """
    location = finding.path or project
    if document := finding.document:
        location = f"{document}: {location}"
    if finding.line is not None:
        location += f":{finding.line}"
    elif (input_line := finding.details.get(DetailKey.INPUT_LINE)) is not None:
        location += f" (reported input line {input_line}; file unresolved)"
    page = finding.page
    if page is not None:
        location += f" (page {page})"
    stage = ", ".join(stages) if stages else finding.details.get(DetailKey.STAGE)
    if stage is not None:
        location = f"{location} [{stage}]" if location else str(stage)
    return location


def _project_label(report: RedactedReport) -> str:
    """Name what a finding without a path is in: the project, or nothing without one.

    Setup diagnostics run without a project unless one is named, and record that as
    ``execution.project``.
    """
    if report.command == "doctor" and not report.execution.get("project"):
        return ""
    return "project"


def _priority(finding: Finding) -> int:
    """Rank a finding for display: errors, warnings, then notes, each failed before undecided.

    Passed results come last.
    """
    if finding.status == "passed":
        return 6
    rank = {"error": 0, "warning": 2}.get(finding.severity, 4)
    return rank if finding.status == "failed" else rank + 1


def _identity(finding: Finding) -> tuple[object, ...]:
    """Return what makes copies of a finding from different stages identical."""
    # The same result re-checked at another stage (baseline, transformed, archive
    # rebuild or final sources) is one issue for a reader; JSON keeps every stage.
    # Copies with different evidence or review decisions stay apart, so no result
    # hides distinct measurements or an unaccepted copy. How many bytes a scan read is
    # bookkeeping: the prepared copy holds a few more bytes and the same matches.
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
            {
                key: value
                for key, value in details.items()
                if key not in {DetailKey.STAGE, DetailKey.BYTES_READ}
            },
            sort_keys=True,
        ),
        review_marker(finding),
    )


class StagedFinding(NamedTuple):
    """A distinct finding and the stages its identical copies came from, if several.

    Attributes:
        finding: The finding that represents the group.
        stages: The stages of the copies; empty for a single copy or a group with an
            unstaged copy.
    """

    finding: Finding
    stages: tuple[str, ...]


def merge_stage_copies(findings: Iterable[Finding]) -> list[StagedFinding]:
    """Merge identical copies of a finding into one entry with the stages they came from.

    A copy without a stage (the initial source analysis) represents the group and
    later identical re-checks are omitted; otherwise the stages are listed in the
    order the job reached them.

    Returns:
        One entry per distinct finding, in the order the groups first appear.
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


def _task_summary(tasks: Sequence[str], reason: str, document: str) -> str:
    """Word one line about the tasks that one reason kept from running or finishing."""
    count = len(tasks)
    return (
        f"{document}: " if document else ""
    ) + f"{count} check{'s' if count != 1 else ''} {reason}: {', '.join(sorted(tasks))}"


def not_run_summaries(report: RedactedReport) -> list[str]:
    """Summarise tasks that did not run or finish in one line per cause, not one per task.

    Tasks skipped because a prerequisite failed get one line per document and set of
    failed prerequisites; tasks the user cancelled get one line per document.

    Returns:
        The lines, naming the tasks each cause affected.
    """
    groups: dict[tuple[str, str], list[str]] = {}
    for finding in report.findings:
        if prerequisites := blocked_prerequisites(finding):
            reason = f"did not run because {' and '.join(prerequisites)} failed"
        elif cancelled_task(finding) is not None:
            reason = "did not finish because the run was cancelled"
        else:
            continue
        task = str(finding.details.get(TASK_DETAIL, finding.message))
        names = groups.setdefault((finding.document or "", reason), [])
        if task not in names:
            names.append(task)
    return [_task_summary(tasks, reason, document) for (document, reason), tasks in groups.items()]


def _plural(count: int, noun: str) -> str:
    """Write ``count`` with ``noun``, adding an ``s`` unless the count is one."""
    return f"{count} {noun}{'s' if count != 1 else ''}"


def _count_group(finding: Finding) -> str:
    """Name the one group of the result counts that ``finding`` belongs to."""
    if finding.status == "passed":
        return "passed"
    if finding.is_suggestion:
        return "suggestion"
    if finding.status == "not_applicable" or finding.is_scope_disclaimer:
        return "not_applicable"
    if finding.status == "failed":
        return {"error": "error", "warning": "warning"}.get(finding.severity, "note")
    return "incomplete"


def result_counts(report: RedactedReport) -> str:
    """Tally the results in one line, each in exactly one group.

    Errors, warnings and notes are failed checks; incomplete results reached no decision,
    whatever their severity; informational scope notes count as not applicable. Notes
    and suggestions are named only when there are any. Identical copies from different
    stages count once.
    """
    findings = [entry.finding for entry in merge_stage_copies(report.findings)]
    counts = Counter(_count_group(item) for item in findings)
    parts = [_plural(counts["error"], "error"), _plural(counts["warning"], "warning")]
    if counts["note"]:
        parts.append(_plural(counts["note"], "note"))
    parts += [
        f"{counts['incomplete']} incomplete",
        f"{counts['not_applicable']} not applicable",
        _plural(counts["passed"], "passed result"),
    ]
    if counts["suggestion"]:
        parts.append(_plural(counts["suggestion"], "suggestion"))
    return ", ".join(parts)


def change_summary(report: RedactedReport) -> str:
    """Count the changes by kind, most frequent first: ``exclude: 3, move: 1``."""
    kinds = Counter(change.kind for change in report.changes)
    ordered = sorted(kinds.items(), key=lambda item: (-item[1], item[0]))
    return ", ".join(f"{kind}: {count}" for kind, count in ordered)


def selection_description(selection: CheckSelectionRecord | None) -> str | None:
    """Describe what the check selection changes, or ``None`` when it changes nothing.

    That is which checks were selected or ignored, and which were reported as warnings
    (``checks.advisory``) although their settings switch them on. Neither reaches the
    always-enforced guards, and the line says so wherever a selector could suggest that
    it does.
    """
    if selection is None:
        return None
    parts = []
    restricted = selection.select != ("ALL",) or bool(selection.ignore)
    if restricted:
        parts.append(
            f"select {', '.join(selection.select) or '(none)'}; "
            f"ignore {', '.join(selection.ignore) or '(none)'}"
        )
    if selection.advisory:
        parts.append(f"reported as warnings instead of errors: {', '.join(selection.advisory)}")
    if restricted:
        parts.append("required validation retained")
    elif any(
        selector == "ALL" or code.startswith(selector)
        for selector in selection.advisory
        for code in selection.always_enforced
    ):
        parts.append("always-enforced guards still block")
    return "Checks: " + "; ".join(parts) if parts else None


def settings_description(report: RedactedReport, options: RenderOptions) -> str | None:
    """Name the settings file and preset a run used, or say that the defaults applied.

    Args:
        report: The redacted report.
        options: The rendering options; ``settings_file`` names the file in place of the
            report's own record.

    Returns:
        The line, or ``None`` when the report does not record where its settings came
        from.
    """
    if CONFIG_PATH_KEY not in report.execution:
        return None
    records = read_execution_records(report.execution)
    sources = [path] if (path := options.settings_file or records.config_path) else []
    if records.preset:
        sources.append(f"preset {records.preset}")
    return f"Settings: {' + '.join(sources) or 'defaults (no settings file)'}"


@dataclass(frozen=True, slots=True, kw_only=True)
class FindingView:
    """Unescaped display text for one finding; each renderer escapes it for its medium.

    Empty ``evidence``, ``next_step``, ``review`` and ``cause`` texts are omitted from
    output.

    Attributes:
        finding: The finding shown.
        code: The check code, or the rule name of an uncoded diagnostic.
        title: The rule's title.
        location: Where the finding is, with its document, page and stages.
        message: The finding's message.
        evidence: An excerpt of the recorded evidence.
        next_step: What to do next.
        review: The marker of a review decision that applies to the finding.
        status_label: The status as readers see it, such as ``not checked``.
        stages: The stages the finding's identical copies came from.
        cause: For a result that follows from another one shown before it, that result's
            code and place, such as ``TEX005 sections/figs.tex:2``.
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
    cause: str = ""

    @classmethod
    def from_finding(
        cls, finding: Finding, stages: tuple[str, ...] = (), project: str = "project"
    ) -> FindingView:
        """Derive the display text of ``finding``, whose identical copies came from ``stages``.

        Args:
            finding: The finding to show.
            stages: The stages its identical copies came from.
            project: What a finding without a path is in, as :func:`_location` takes it.

        Returns:
            The view.
        """
        return cls(
            finding=finding,
            code=finding.code or finding.rule,
            title=_title(finding),
            location=_location(finding, stages, project),
            message=finding.message,
            evidence=evidence_excerpt(finding),
            next_step=finding.next_step or "",
            review=review_marker(finding),
            status_label=_STATUS_LABELS.get(finding.status, finding.status),
            stages=stages,
        )

    @property
    def place(self) -> str:
        """The file and line of the finding, or ``project``."""
        return _place(self.finding)


# The closing lines TeX writes after the error that stopped it; they restate that error.
_TEX_STOP_TRAILER = re.compile(r"==> Fatal error occurred|Emergency stop|no output PDF file")
_TEX_ERROR_RULE = "build.tex_error"
_BUILD_FAILED_RULE = "build.failed"
# Build-log results that a build which stopped on a TeX error could not establish.
_UNRESOLVED_AFTER_STOP_RULES = frozenset({"build.undefined_citation", "build.undefined_reference"})
# Results that say the input selects no single document; a missing main source names them.
_MAIN_SELECTION_RULES = frozenset(
    {
        "project.main_selection",
        "source.root_ambiguous",
        "source.root_missing",
        "source.main_invalid",
    }
)


def _is_build_aftermath(finding: Finding) -> bool:
    """Return whether ``finding`` only restates the failure that stopped a build.

    That is a build result of ``Finding.is_build_follow_on``, TeX's closing lines after the
    error, or a citation or reference result the stopped build could not establish.
    """
    if finding.status == "passed":
        return False
    return (
        finding.is_build_follow_on
        or (finding.rule == _TEX_ERROR_RULE and bool(_TEX_STOP_TRAILER.search(finding.message)))
        or (finding.rule in _UNRESOLVED_AFTER_STOP_RULES and finding.status == "inconclusive")
    )


def _build_cause(
    findings: Sequence[Finding], indices: Sequence[int], causes: Mapping[int, int]
) -> int | None:
    """Choose the result that explains a failed build, if the build failed.

    A tool problem such as a timeout explains a stopped build. Otherwise the first
    substantive TeX error explains it, through the source error at the same place when
    there is one, or the failed build itself.

    Args:
        findings: The findings in display order.
        indices: The findings from one document's build stage.
        causes: The causes found so far, by the index of the finding they explain.

    Returns:
        The index of the explaining finding, or ``None`` when no build failed or nothing
        explains it.
    """
    for index in indices:
        finding = findings[index]
        if finding.is_blocking and finding.prevents_checking:
            return index
    for index in indices:
        finding = findings[index]
        if (
            finding.rule == _TEX_ERROR_RULE
            and finding.status == "failed"
            and not _TEX_STOP_TRAILER.search(finding.message)
        ):
            return causes.get(index, index)
    return next(
        (
            index
            for index in indices
            if findings[index].rule == _BUILD_FAILED_RULE and findings[index].status != "passed"
        ),
        None,
    )


def _build_follow_ons(findings: Sequence[Finding], causes: Mapping[int, int]) -> dict[int, int]:
    """Find follow-ons to stopped builds within the same document and build stage."""
    builds: dict[tuple[str | None, str | None], list[int]] = {}
    for index, finding in enumerate(findings):
        if finding.rule.startswith("build."):
            builds.setdefault((finding.document, finding.stage), []).append(index)
    stopped = stopped_build_stages(findings)
    result = {}
    for indices in builds.values():
        cause = _build_cause(findings, indices, causes)
        if cause is None:
            continue
        for index in indices:
            finding = findings[index]
            if index == cause or index in causes or finding.status == "passed":
                continue
            if _is_build_aftermath(finding) or follows_stopped_build(finding, stopped):
                result[index] = cause
    return result


def _same_place_causes(findings: Sequence[Finding]) -> dict[int, int]:
    """Map each TeX error to the source error at the same file and line, which it restates.

    The build names the document it built and a source check may not, so two findings
    are at the same place when their file and line agree and their documents do not
    differ.

    Args:
        findings: The findings in display order.

    Returns:
        The cause of each such TeX error, by index.
    """
    located: dict[tuple[str, int], list[int]] = {}
    for index, finding in enumerate(findings):
        if (
            finding.is_blocking
            and finding.path
            and finding.line is not None
            and not finding.rule.startswith("build.")
        ):
            located.setdefault((finding.path, finding.line), []).append(index)
    causes = {}
    for index, finding in enumerate(findings):
        if finding.rule != _TEX_ERROR_RULE or finding.status == "passed" or not finding.path:
            continue
        candidates = located.get((finding.path, finding.line or 0), [])
        cause = next(
            (
                item
                for item in candidates
                if None in {findings[item].document, finding.document}
                or findings[item].document == finding.document
            ),
            None,
        )
        if cause is not None:
            causes[index] = cause
    return causes


def _restated_reasons(finding: Finding) -> tuple[str, ...]:
    """Return the reasons a result lists for its incomplete evidence, as it words them."""
    reasons = finding.details.get("uncertainty")
    if finding.status == "passed" or not isinstance(reasons, (list, tuple)):
        return ()
    return tuple(item for item in reasons if isinstance(item, str))


def _restated_source_causes(
    findings: Sequence[Finding], causes: Mapping[int, int]
) -> dict[int, int]:
    """Map each result whose evidence is incomplete because of a source error to that error.

    A bibliography result lists the source errors it restates as ``path: message``, or
    the missing main source, which the result that selects no document explains.

    Args:
        findings: The findings in display order.
        causes: The causes found so far; a finding already explained is never a cause.

    Returns:
        The cause of each such result, by index.
    """
    by_reason: dict[tuple[str | None, str], int] = {}
    for index, finding in enumerate(findings):
        if index in causes or not finding.is_blocking:
            continue
        if finding.path:
            by_reason.setdefault((finding.document, f"{finding.path}: {finding.message}"), index)
        if finding.rule in _MAIN_SELECTION_RULES:
            by_reason.setdefault((finding.document, NO_MAIN_SOURCE), index)
    restated = {}
    for index, finding in enumerate(findings):
        reasons = [] if index in causes else _restated_reasons(finding)
        keys = ((finding.document, reason) for reason in reasons)
        cause = next((by_reason[key] for key in keys if key in by_reason), None)
        if cause is not None and cause != index:
            restated[index] = cause
    return restated


def _document_summary_causes(
    findings: Sequence[Finding], causes: Mapping[int, int]
) -> dict[int, int]:
    """Map the failed summary of several document packages to the first document's problem.

    The summary fails whenever a document package failed, for the reasons that the
    document's own findings give, so it is never the problem to start with.

    Args:
        findings: The findings in display order.
        causes: The causes found so far; a finding already explained is never a cause.

    Returns:
        The cause of the summary, by index; nothing when no document has a problem.
    """
    first = next(
        (
            index
            for index, finding in enumerate(findings)
            if finding.document and finding.is_blocking and index not in causes
        ),
        None,
    )
    if first is None:
        return {}
    return {
        index: first
        for index, finding in enumerate(findings)
        if finding.rule == DOCUMENT_SUMMARY_RULE and finding.is_blocking
    }


def follow_on_causes(findings: Sequence[Finding]) -> dict[int, int]:
    """Map the index of each result that follows from another one to that result's index.

    Four kinds of result follow from another reported beside them: a TeX error at the
    place of a source error, which names the same problem; the aftermath of a failed
    build (see :func:`_is_build_aftermath`), including every result of the truncated log
    of a build that a tool, time or resource problem stopped, which follows what stopped
    the build; a result whose evidence is incomplete because of a source error that it
    names; and the failed summary of several document packages, which follows the first
    problem of a document. A cause is never itself a follow-on: chains resolve to their
    first cause.

    Args:
        findings: The findings in display order.

    Returns:
        The cause of each follow-on, by index.
    """
    causes = _same_place_causes(findings)
    causes.update(_build_follow_ons(findings, causes))
    causes.update(_restated_source_causes(findings, causes))
    causes.update(_document_summary_causes(findings, causes))
    resolved = {}
    for index, cause in causes.items():
        seen = {index}
        while cause in causes and cause not in seen:
            seen.add(cause)
            cause = causes[cause]
        if cause not in seen:
            resolved[index] = cause
    return resolved


def _views_with_causes(entries: Sequence[StagedFinding], project: str) -> list[FindingView]:
    """Build the views in display order, each cause followed by the results it explains.

    Args:
        entries: The distinct findings, in display order.
        project: What a finding without a path is in, as :func:`_location` takes it.

    Returns:
        The views, with each follow-on moved behind its cause.
    """
    views = [FindingView.from_finding(entry.finding, entry.stages, project) for entry in entries]
    causes = follow_on_causes([entry.finding for entry in entries])
    ordered = []
    for index, view in enumerate(views):
        if index in causes:
            continue
        ordered.append(view)
        cause = f"{view.code} {view.place}"
        # What names the cause again, such as the TeX error at its place, comes before the
        # aftermath of a failed build.
        followers = sorted(
            (follower for follower, source in causes.items() if source == index),
            key=lambda item: (_is_build_aftermath(views[item].finding), item),
        )
        ordered.extend(replace(views[item], cause=cause) for item in followers)
    return ordered


def _distinct_entries(report: RedactedReport, findings: Iterable[Finding]) -> list[StagedFinding]:
    """Merge the stage copies of ``findings`` and order them, the most urgent first."""
    order = {id(item): index for index, item in enumerate(report.sorted_findings())}
    entries = merge_stage_copies(findings)
    entries.sort(key=lambda entry: (_priority(entry.finding), order[id(entry.finding)]))
    return entries


def finding_views(report: RedactedReport, *, show_hidden: bool) -> list[FindingView]:
    """Build one view per distinct finding other than suggestions, the most urgent first.

    Each cause is followed by the results that follow from it (``FindingView.cause``).
    Tasks that never started because a prerequisite failed, or that the user cancelled,
    are left to :func:`not_run_summaries`, and suggestions to :func:`suggestion_views`.

    Args:
        report: The redacted report.
        show_hidden: Whether passed results and scope disclaimers are listed; the
            notices in ``ALWAYS_LISTED_RULES`` are listed regardless.

    Returns:
        The views, ordered by urgency and then by the report's order of findings, with
        follow-ons moved behind their causes.
    """
    return _views_with_causes(
        _distinct_entries(
            report,
            (
                item
                for item in report.findings
                if not item.is_suggestion
                and not blocked_prerequisites(item)
                and cancelled_task(item) is None
                and (show_hidden or not _hidden_by_default(item))
            ),
        ),
        _project_label(report),
    )


def suggestion_views(report: RedactedReport, *, show_hidden: bool) -> list[FindingView]:
    """Build one view per distinct suggestion, in the report's order.

    Suggestions are optional advice; they never change the outcome or the exit code.

    Args:
        report: The redacted report.
        show_hidden: Whether suggestions that passed are listed too.

    Returns:
        The views of the suggestions.
    """
    entries = _distinct_entries(
        report,
        (
            item
            for item in report.findings
            if item.is_suggestion and (show_hidden or item.status != "passed")
        ),
    )
    project = _project_label(report)
    return [FindingView.from_finding(entry.finding, entry.stages, project) for entry in entries]


def _problems(views: Sequence[FindingView]) -> list[FindingView]:
    """Return the views of open blocking findings that no other result explains."""
    return [view for view in views if not view.cause and view.finding.is_blocking]


def _published(report: RedactedReport) -> str:
    """Describe the artifacts that this run actually wrote."""
    if report.submission_written:
        return " The submission was written."
    if any(name.startswith("preview.") for name in report.artifacts):
        return " Previews were written."
    if report.command == "prepare" and not report.outcome.passed:
        return " Nothing was published."
    return ""


def interrupted_tasks(report: RedactedReport) -> list[str]:
    """Return the tasks that were queued or running when the run was cancelled."""
    names = (
        str(item.details.get(TASK_DETAIL, item.message))
        for item in report.findings
        if cancelled_task(item) == "running"
    )
    return list(dict.fromkeys(names))


def cancellation_line(report: RedactedReport) -> str:
    """Say during which tasks the run was cancelled, and that nothing was published."""
    if tasks := interrupted_tasks(report):
        return f"Cancelled during {', '.join(tasks)}; nothing was published."
    return "Cancelled; nothing was published."


def _blocked_verdict(report: RedactedReport, views: Sequence[FindingView]) -> str:
    """Count the problems of a blocked run and name the one to start with."""
    problems = _problems(views)
    if not problems:
        return f"Blocked: a required check did not pass.{_published(report)}"
    first = problems[0]
    return (
        f"Blocked: {_plural(len(problems), 'problem')} to fix. Start with {first.code} "
        f"({first.title}) at {first.place}.{_published(report)}"
    )


def _error_verdict(report: RedactedReport, views: Sequence[FindingView]) -> str:
    """Name what kept Fledge from checking the paper: the rejected request or the first cause."""
    problems = _problems(views)
    cause = next((view for view in problems if view.finding.prevents_checking), None)
    if report.is_request_error and cause is not None:
        return f"Error: {cause.message}"
    if cause is None and any(view.finding.rule == "report.write" for view in problems):
        return f"Could not write all requested reports.{_published(report)}"
    reason = cause.title if cause else "the run did not finish"
    return f"Could not check: {reason}.{_published(report)}"


def verdict_line(report: RedactedReport, views: Sequence[FindingView]) -> str:
    """Write the one line that closes a report: the result and where to start.

    Args:
        report: The redacted report.
        views: The finding views of the report, as :func:`finding_views` returns them.

    Returns:
        The verdict, such as ``Blocked: 2 problems to fix. Start with TEX005 (Missing
        source dependencies) at sections/figs.tex:2. Nothing was published.``
    """
    open_warnings = sum(
        not view.cause and view.finding.severity == "warning" and view.finding.status != "passed"
        for view in views
    )
    written = _published(report)
    match report.outcome:
        case Outcome.BLOCKED:
            return _blocked_verdict(report, views)
        case Outcome.ERROR:
            return _error_verdict(report, views)
        case Outcome.INTERNAL_ERROR:
            return (
                "Internal error: a defect in Fledge stopped the run; your files are not at "
                f"fault. Rerun with --debug and report it.{written}"
            )
        case Outcome.CANCELLED:
            return cancellation_line(report)
        case Outcome.PLANNED:
            return "Planned: a dry run; nothing was verified or written."
        case Outcome.PASSED_WITH_ADVISORIES:
            return f"Passed with {_plural(open_warnings, 'warning')} to review.{written}"
        case Outcome.ACCEPTED_EXCEPTIONS:
            return f"Passed because of accepted exceptions in your settings.{written}"
        case _:
            return f"Passed: no problems found within this command's scope.{written}"
