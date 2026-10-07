"""Explicit scoped review, offline reports and unverified diagnostic exports.

Review metadata is display evidence, never authorization. Gate decisions always
evaluate the explicitly supplied frozen options against the original finding.
"""

from __future__ import annotations

import html
import io
import json
import math
import os
import stat
import unicodedata
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from .models import Finding, PreparationError, Report
from .rules import BY_CODE, BY_NAME

# Explicitly enumerated presentation/content policies. New checks are ineligible
# until their evidence and safety contract have been reviewed here.
REVIEWABLE_CODES = frozenset(
    {
        "TEX001",
        "TEX002",
        "TEX003",
        "TEX004",
        "TEX101",
        "TEX102",
        "TEX103",
        "TEX104",
        "TEX105",
        "TEX106",
        "TEX107",
        "BIB006",
        "BIB007",
        "BIB102",
        "BIB103",
        "BIB104",
        "BIB105",
        "BIB106",
        "BIB107",
        "BIB108",
        "BLD005",
        "BLD006",
        "BLD007",
        "BLD103",
        "BLD104",
        "MAN001",
        "MAN002",
        "MAN003",
        "MAN004",
        "MAN005",
        "MAN006",
        "MAN007",
        "MAN008",
        "MAN009",
        "MAN010",
        "MAN011",
        "MAN012",
        "MAN013",
        "MAN014",
        "MAN015",
        "MAN016",
        "PDF001",
        "PDF101",
        "PDF102",
        "PDF103",
        "PDF104",
        "PDF105",
        "PDF201",
        "PDF202",
        "PDF203",
        "PDF204",
        "PDF205",
        "PDF206",
        "PDF207",
        "PDF208",
        "PDF209",
        "PDF212",
        "PDF213",
        "PDF214",
        "PDF215",
        "PDF216",
        "PKG101",
        "PKG102",
        "PKG103",
        "PKG104",
        "PKG105",
        "PKG106",
        "PKG107",
        "PRV001",
        "PRV002",
        "PRV003",
        "PRV004",
        "PRV005",
        "NET001",
        "NET002",
        "NET003",
        "NET004",
        "NET005",
        "NET006",
        "NET007",
        "CMP001",
        "CMP002",
        "CMP003",
        "FMT001",
    }
)
_COMPARISON_CODES = frozenset({"CMP001", "CMP002", "CMP003"})
_REVIEW_DIAGNOSTICS = {"report.review_unmatched", "report.review_ineligible"}
_TEXT_SUFFIXES = frozenset(
    {".log", ".txt", ".tex", ".bib", ".diff", ".json", ".out", ".err", ".sty", ".cls"}
)
_MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
_MAX_ARTIFACT_TOTAL = 8 * 1024 * 1024


def _safe_relative(path: str) -> bool:
    return (
        bool(path)
        and not path.startswith("/")
        and "\\" not in path
        and ":" not in path
        and all(part not in {"", ".", ".."} for part in path.split("/"))
        and not any(unicodedata.category(char).startswith("C") for char in path)
    )


@dataclass(frozen=True)
class ReviewRule:
    """An exact diagnostic code and optional conjunctive location constraints.

    Paths are literal project-relative paths, never globs. Endpoints are inclusive;
    a start without an end selects one line/page. Missing evidence never matches a
    constrained location, and a sampled aggregate cannot establish complete scope.
    """

    code: str
    reason: str
    path: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    page_start: int | None = None
    page_end: int | None = None
    stage: str | None = None
    document: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or self.code not in BY_CODE:
            raise PreparationError("Review decisions require an exact registered check code")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise PreparationError("Every review decision requires a nonempty reason")
        if len(self.reason) > 4096:
            raise PreparationError("Review reasons must be at most 4096 characters")
        for name in ("path", "document"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not _safe_relative(value)):
                raise PreparationError(f"Review {name} must be a literal safe relative path")
        if self.stage is not None and (
            not isinstance(self.stage, str)
            or not self.stage.strip()
            or any(unicodedata.category(char).startswith("C") for char in self.stage)
        ):
            raise PreparationError("Review stage must be nonempty text without control characters")
        for kind in ("line", "page"):
            start, end = getattr(self, f"{kind}_start"), getattr(self, f"{kind}_end")
            if any(
                value is not None and (type(value) is not int or value < 1)
                for value in (start, end)
            ):
                raise PreparationError(f"Review {kind} endpoints must be positive integers")
            if end is not None and (start is None or end < start):
                raise PreparationError(f"Review {kind} range requires an ordered start and end")
        if self.line_start is not None and self.path is None:
            raise PreparationError("Review line scope requires an exact path")


@dataclass(frozen=True)
class ReportingOptions:
    suppressions: tuple[ReviewRule, ...] = ()
    accepted_exceptions: tuple[ReviewRule, ...] = ()

    def __post_init__(self) -> None:
        for name in ("suppressions", "accepted_exceptions"):
            values = getattr(self, name)
            if not isinstance(values, tuple) or any(not isinstance(v, ReviewRule) for v in values):
                raise PreparationError(f"reporting.{name} must be immutable review rules")
            if len(values) > 1024 or len(set(values)) != len(values):
                raise PreparationError(f"reporting.{name} requires at most 1024 unique decisions")


def _incomplete(details: object) -> bool:
    if isinstance(details, dict):
        for key, value in details.items():
            if key in {
                "incomplete",
                "unavailable",
                "uncertain",
                "uncertainty",
                "content_uncertainty",
                "truncated",
                "unsupported",
                "issues",
                "match_output_limited",
                "output_limited",
            }:
                if value:
                    return True
            if key in {
                "unsupported_pages",
                "unavailable_pages",
                "unmeasured",
                "unmeasured_fields",
                "unknown",
                "unknown_fields",
            }:
                if value:
                    return True
            if key in {"complete", "recorder_complete", "coverage_complete", "input_complete"}:
                if value is not True:
                    return True
            if _incomplete(value):
                return True
    elif isinstance(details, (list, tuple)):
        return any(_incomplete(item) for item in details)
    return False


def _affected_pages(finding: Finding) -> set[int] | None:
    details = finding.details
    direct = details.get("page")
    if type(direct) is int and direct > 0:
        return {direct}
    changed = details.get("changed_pages")
    if isinstance(changed, (tuple, list)) and changed:
        if all(type(value) is int and value > 0 for value in changed):
            return set(changed)
        return None
    violations = details.get("violations")
    if isinstance(violations, (tuple, list)) and violations:
        # A bounded sample cannot authorize the unlisted violations.
        count = details.get("violation_count")
        if type(count) is not int or count != len(violations):
            return None
        pages: set[int] = set()
        for item in violations:
            value = item.get("page") if isinstance(item, dict) else None
            if type(value) is not int or value < 1:
                return None
            pages.add(value)
        return pages
    return None


def _matches(finding: Finding, review: ReviewRule) -> bool:
    if finding.code != review.code:
        return False
    for key in ("stage", "document"):
        value = getattr(review, key)
        if value is not None and finding.details.get(key) != value:
            return False
    if review.path is not None and finding.path != review.path:
        return False
    if review.line_start is not None:
        if type(finding.line) is not int or not (
            review.line_start <= finding.line <= (review.line_end or review.line_start)
        ):
            return False
    if review.page_start is not None:
        pages = _affected_pages(finding)
        if pages is None or not all(
            review.page_start <= page <= (review.page_end or review.page_start) for page in pages
        ):
            return False
    return True


def _eligible(finding: Finding, action: str) -> bool:
    if (
        finding.code not in REVIEWABLE_CODES
        or finding.status != "failed"
        or _incomplete(finding.details)
        or (action == "suppressed" and finding.severity == "error")
    ):
        return False
    if finding.code in _COMPARISON_CODES:
        return action == "accepted_exception" and (
            finding.details.get("stage") == "baseline versus prepared"
        )
    return True


def _decisions(options: ReportingOptions) -> list[tuple[str, ReviewRule]]:
    return [("accepted_exception", rule) for rule in options.accepted_exceptions] + [
        ("suppressed", rule) for rule in options.suppressions
    ]


def has_unaccepted_blockers(findings: Sequence[Finding], options: ReportingOptions) -> bool:
    """Evaluate explicit current decisions; stored review metadata grants no authority."""
    return any(
        finding.severity == "error"
        and finding.status in {"failed", "inconclusive", "skipped"}
        and not any(
            _matches(finding, rule) and _eligible(finding, "accepted_exception")
            for rule in options.accepted_exceptions
        )
        for finding in findings
    )


def reviewed_findings(
    findings: Sequence[Finding], options: ReportingOptions, *, diagnostics: bool = True
) -> list[Finding]:
    """Add visible decisions without rewriting the original result or its evidence."""
    result: list[Finding] = []
    decisions = _decisions(options)
    matched = [False] * len(decisions)
    ineligible = [False] * len(decisions)
    for finding in findings:
        if finding.rule in _REVIEW_DIAGNOSTICS:
            continue
        details = dict(finding.details)
        details.pop("review", None)
        clean = replace(finding, details=details)
        applied = []
        for index, (action, decision) in enumerate(decisions):
            if not _matches(clean, decision):
                continue
            matched[index] = True
            if _eligible(clean, action):
                applied.append(
                    {"action": action, "reason": decision.reason, "scope": asdict(decision)}
                )
            else:
                ineligible[index] = True
        if applied:
            details["review"] = applied
        result.append(replace(clean, details=details))
    if diagnostics:
        for index, (action, decision) in enumerate(decisions):
            if matched[index] and not ineligible[index]:
                continue
            result.append(
                Finding(
                    "report.review_ineligible" if ineligible[index] else "report.review_unmatched",
                    "A requested review decision cannot change this result."
                    if ineligible[index]
                    else "No finding matches this review decision's full scope.",
                    "warning",
                    "inconclusive",
                    path=decision.path,
                    line=decision.line_start,
                    details={"action": action, "requested_review": asdict(decision)},
                )
            )
    return result


def apply_reviews(report: Report, options: ReportingOptions) -> Report:
    """Return a report with visible decisions, preserving the workflow's outcome.

    A caller may label a successfully completed workflow ``accepted_exceptions``.
    This function never upgrades a blocked, cancelled, incomplete or errored run.
    """
    findings = reviewed_findings(report.findings, options)
    outcome = report.outcome
    if outcome in {"passed", "passed_with_advisories", "accepted_exceptions"}:
        if has_unaccepted_blockers(findings, options):
            outcome = "blocked"
        elif any(
            _matches(finding, decision) and _eligible(finding, "accepted_exception")
            for finding in findings
            for decision in options.accepted_exceptions
        ):
            outcome = "accepted_exceptions"
        elif outcome == "passed" and any(
            finding.severity == "warning" and finding.status != "passed" for finding in findings
        ):
            outcome = "passed_with_advisories"
    return replace(report, findings=findings, outcome=outcome)


def review_marker(finding: Finding) -> str:
    """A display-only marker; never use it for acceptance or execution decisions."""
    entries = finding.details.get("review")
    if not isinstance(entries, (list, tuple)):
        return ""
    labels = {"suppressed": "Suppressed advisory", "accepted_exception": "Accepted exception"}
    return "; ".join(
        f"{labels[entry['action']]}: {entry['reason']}"
        for entry in entries
        if isinstance(entry, dict)
        and entry.get("action") in labels
        and isinstance(entry.get("reason"), str)
    )


def is_scope_disclaimer(finding: Finding) -> bool:
    """Informational notes that a check was out of scope; shown only on request."""
    return finding.severity == "info" and finding.status in {"skipped", "not_applicable"}


# Checkers keep their evidence in these detail lists behind a generic message.
_EVIDENCE_KEYS = (
    "matches",
    "violations",
    "missing",
    "entries",
    "changed_pages",
    "differing_pages",
    "nonembedded",
    "below_minimum",
    "candidates",
)
_EVIDENCE_PAGE_KEYS = frozenset({"changed_pages", "differing_pages"})
_EVIDENCE_ITEMS = 3
_EVIDENCE_LABEL_CHARS = 120
_EVIDENCE_NAME_KEYS = ("name", "key", "reference", "label", "reason")


def _evidence_label(item: object, pages: bool) -> str | None:
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
        if type(item.get("page")) is int:
            location = f"{location} (page {item['page']})" if location else f"page {item['page']}"
        label = location or next(
            (item[key] for key in _EVIDENCE_NAME_KEYS if isinstance(item.get(key), str)), ""
        )
    if not isinstance(label, str) or not label:
        return None
    return (
        label if len(label) <= _EVIDENCE_LABEL_CHARS else label[: _EVIDENCE_LABEL_CHARS - 1] + "…"
    )


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
        count = finding.details.get("violation_count")
        if key == "violations" and type(count) is int and count > len(items):
            hidden = count - len(items)
        labels = [_evidence_label(item, key in _EVIDENCE_PAGE_KEYS) for item in items]
        shown = list(dict.fromkeys(label for label in labels if label is not None))[
            :_EVIDENCE_ITEMS
        ]
        if shown:
            hidden += sum(label not in shown for label in labels)
            return ", ".join(shown) + (f" (+{hidden} more)" if hidden else "")
    return ""


def _text(value: object) -> str:
    rendered = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return "".join(
        f"\\u{ord(char):04x}"
        if unicodedata.category(char).startswith("C") and char not in "\n\t"
        else char
        for char in rendered
    )


def _escape(value: object) -> str:
    return html.escape(_text(value), quote=True)


def _location(finding: Finding) -> str:
    parts = [finding.path or "project"]
    if finding.line is not None:
        parts.append(f"line {finding.line}")
    for key in ("page", "stage", "document"):
        if key in finding.details:
            parts.append(f"{key}: {finding.details[key]}")
    return " · ".join(parts)


def _boxes(value: object) -> list[tuple[float, float, float, float]]:
    if not isinstance(value, (tuple, list)):
        return []
    if len(value) == 4 and all(type(v) in {int, float} and math.isfinite(v) for v in value):
        left, top, right, bottom = map(float, value)
        if 0 <= left < right <= 1_000_000 and 0 <= top < bottom <= 1_000_000:
            return [(left, top, right, bottom)]
        return []
    return [box for item in value[:100] for box in _boxes(item)]


def _pdf_link(path: object, page: int) -> str | None:
    if not isinstance(path, str) or not path.lower().endswith(".pdf"):
        return None
    relative = path[1:] if path.startswith("/") and not path.startswith("//") else path
    if not _safe_relative(relative):
        return None
    return quote(path, safe="/") + f"#page={page}"


def _regions(finding: Finding, report: Report) -> str:
    candidates = [finding.details]
    violations = finding.details.get("violations")
    if isinstance(violations, (list, tuple)):
        candidates += [item for item in violations[:100] if isinstance(item, dict)]
    parts = []
    for item in candidates:
        page = item.get("page")
        if type(page) is not int or page < 1:
            continue
        boxes = _boxes(item.get("bounds_pt", item.get("bbox")))
        if not boxes:
            continue
        width, height = max(box[2] for box in boxes), max(box[3] for box in boxes)
        known_size = False
        # Geometry from another document/stage must not be borrowed for this page.
        for evidence in report.findings:
            if evidence.details.get("stage") != finding.details.get("stage"):
                continue
            if evidence.details.get("document") != finding.details.get("document"):
                continue
            sizes = evidence.details.get("dimensions_pt")
            if isinstance(sizes, (list, tuple)) and len(sizes) >= page:
                size = sizes[page - 1]
                if (
                    isinstance(size, (list, tuple))
                    and len(size) == 2
                    and all(
                        type(v) in {int, float} and math.isfinite(v) and 0 < v <= 1_000_000
                        for v in size
                    )
                ):
                    width, height = map(float, size)
                    known_size = True
                    break
        regions = "".join(
            f'<rect x="{left:g}" y="{top:g}" width="{right - left:g}" '
            f'height="{bottom - top:g}" class="region"/>'
            for left, top, right, bottom in boxes
        )
        link = _pdf_link(finding.path, page) or _pdf_link(report.artifacts.get("pdf"), page)
        parts.append(
            f'<figure><svg role="img" aria-label="Page {page} reported regions" '
            f'viewBox="0 0 {width:g} {height:g}" xmlns="http://www.w3.org/2000/svg">'
            f'<rect width="{width:g}" height="{height:g}" class="paper"/>{regions}</svg>'
            f"<figcaption>Page {page}: reported regions in PDF points (top-left origin). "
            + (
                "Page size recorded."
                if known_size
                else "Coordinate extent only; page size unavailable."
            )
            + (f' <a href="{_escape(link)}">Open local PDF at page {page}</a>.' if link else "")
            + " The diagram does not embed or modify the PDF.</figcaption></figure>"
        )
    return "".join(parts)


def _diff_html(diff: str) -> str:
    rows = []
    for line in diff.splitlines():
        if line.startswith(("---", "+++", "@@")):
            rows.append(f'<tr><td colspan="2" class="context">{_escape(line)}</td></tr>')
        elif line.startswith("-"):
            rows.append(f'<tr><td class="removed">{_escape(line[1:])}</td><td></td></tr>')
        elif line.startswith("+"):
            rows.append(f'<tr><td></td><td class="added">{_escape(line[1:])}</td></tr>')
        else:
            content = _escape(line[1:] if line.startswith(" ") else line)
            rows.append(f"<tr><td>{content}</td><td>{content}</td></tr>")
    return (
        '<table class="diff"><thead><tr><th>Before</th><th>After</th></tr></thead><tbody>'
        + "".join(rows)
        + "</tbody></table>"
    )


def render_html(report: Report) -> str:
    """Render one offline document with escaped evidence and no executable resources."""
    public = report.redacted_copy()
    parts = [
        '<!doctype html><html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
        "style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'\">",
        "<title>Fledge report</title><style>",
        "body{font:16px/1.5 system-ui,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem;"
        "color:#17202a;background:#fafafa}article,section{margin:1rem 0;padding:1rem;"
        "border:1px solid "
        "#bcc6cc;border-radius:.35rem;background:white}h1,h2,h3{line-height:1.2}pre,td{white-space:"
        "pre-wrap;overflow-wrap:anywhere}pre{overflow:auto}table{width:100%;border-collapse:collapse;"
        "table-layout:fixed}td,th{text-align:left;vertical-align:top;border:1px solid #cad1d6;"
        "padding:.4rem}.removed{background:#ffe8e8}.added{background:#e3f6e7}.context{background:"
        "#edf0f3}.review{border-left:4px solid #9a6200;padding:.5rem}.location{color:#42515e}"
        "svg{max-height:400px;max-width:100%;width:340px}.paper{fill:white;stroke:#526474;"
        "stroke-width:1}.region{fill:#eaa43b;fill-opacity:.3;stroke:#9a4900;stroke-width:1}"
        "figcaption{font-size:.85rem}a{color:#005599}</style></head><body>",
        f"<h1>{_escape(public.outcome.replace('_', ' ').capitalize())}</h1>",
        f"<p>{_escape(public.scope)}</p>",
        "<p>Results cover the stated checks. Review decisions retain the original evidence; "
        "they do not prove compliance or supply missing verification.</p>",
    ]
    if public.main:
        parts.append(f"<p>Document: {_escape(public.main)}</p>")
    parts.append("<h2>Findings and actions</h2>")
    for index, finding in enumerate(public.sorted_findings(), 1):
        definition = BY_NAME.get(finding.rule)
        title = definition.title if definition else finding.rule
        parts.extend(
            [
                f'<article id="finding-{index}"><h3>{_escape(finding.code or finding.rule)} — '
                f"{_escape(title)}</h3><p>{_escape(finding.severity)} / "
                f"{_escape(finding.status)}</p>",
                f'<p class="location">{_escape(_location(finding))}</p>',
                f"<p>{_escape(finding.message)}</p>",
            ]
        )
        if excerpt := evidence_excerpt(finding):
            parts.append(f"<p>Evidence: {_escape(excerpt)}</p>")
        marker = review_marker(finding)
        if marker:
            parts.append(f'<p class="review">{_escape(marker)}. Original result retained.</p>')
        if finding.next_step:
            parts.append(f"<p><strong>Next action:</strong> {_escape(finding.next_step)}</p>")
        parts.append(_regions(finding, public))
        parts.append(
            "<details><summary>Recorded evidence</summary><pre>"
            + _escape(
                json.dumps(
                    {"evidence": finding.evidence, **finding.details},
                    indent=2,
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            + "</pre></details></article>"
        )
    if not public.findings:
        parts.append("<p>No findings were recorded within this scope.</p>")
    if public.changes:
        parts.append(
            "<h2>Proposed or applied changes</h2><p>Diff excerpts show recorded changes; "
            "unchanged portions of each file may be omitted.</p>"
        )
        for change in public.changes:
            parts.append(
                f"<section><h3>{_escape(change.path)}</h3><p>{_escape(change.kind)}: "
                f"{_escape(change.reason)}</p>"
            )
            if change.destination:
                parts.append(f"<p>Destination: {_escape(change.destination)}</p>")
            if change.diff:
                parts.append(_diff_html(change.diff))
            parts.append("</section>")
    parts.append(
        "<details><summary>Run settings, stages, tools and artifacts</summary><pre>"
        + _escape(
            json.dumps(
                {
                    "settings": public.settings,
                    "stages": public.stages,
                    "tools": public.tools,
                    "artifacts": public.artifacts,
                    "execution": public.execution,
                },
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        + "</pre></details></body></html>\n"
    )
    return "".join(parts)


def _ci_escape(value: str, *, property_value: bool = False) -> str:
    result = value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    result = "".join(
        f"%{ord(char):02X}"
        if ord(char) < 32 or ord(char) == 127
        else f"\\u{ord(char):04x}"
        if unicodedata.category(char).startswith("C")
        else char
        for char in result
    )
    return result.replace(":", "%3A").replace(",", "%2C") if property_value else result


def render_ci_annotations(report: Report, *, show_passed: bool = False) -> str:
    """GitHub Actions annotations; each untrusted finding occupies one physical line."""
    public = report.redacted_copy()
    lines = []
    for finding in public.sorted_findings():
        if not show_passed and (finding.status == "passed" or is_scope_disclaimer(finding)):
            continue
        marker = review_marker(finding)
        level = (
            "notice"
            if marker or finding.status == "passed"
            else {"error": "error", "warning": "warning"}.get(finding.severity, "notice")
        )
        fields = ["title=" + _ci_escape(finding.code or finding.rule, property_value=True)]
        if finding.path and _safe_relative(finding.path):
            fields.append("file=" + _ci_escape(finding.path, property_value=True))
            if type(finding.line) is int and finding.line > 0:
                fields.append(f"line={finding.line}")
        message = f"{finding.severity}/{finding.status}: {finding.message}"
        if excerpt := evidence_excerpt(finding):
            message += f" | Evidence: {excerpt}"
        if marker:
            message += f" | {marker}; original result retained"
        if finding.next_step:
            message += f" | Next: {finding.next_step}"
        lines.append(f"::{level} {','.join(fields)}::{_ci_escape(message)}")
    return "\n".join(lines)


def _scratch_text(root: Path, relative: str) -> str:
    if not _safe_relative(relative) or PurePosixPath(relative).suffix.lower() not in _TEXT_SUFFIXES:
        raise PreparationError("Diagnostic artifacts require allowlisted relative text-file paths")
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    descriptor: int | None = None
    try:
        components = relative.split("/")
        for component in components[:-1]:
            child = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
            )
            os.close(directory)
            directory = child
        descriptor = os.open(
            components[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_ARTIFACT_BYTES:
            raise PreparationError("Diagnostic artifact is not a bounded regular text file")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            data = stream.read(_MAX_ARTIFACT_BYTES + 1)
        if len(data) > _MAX_ARTIFACT_BYTES or b"\0" in data:
            raise PreparationError("Diagnostic artifact is oversized or not supported text")
        return data.decode("utf-8")
    except (OSError, UnicodeError) as error:
        raise PreparationError(
            "Diagnostic artifact cannot be read safely as local UTF-8 text"
        ) from error
    finally:
        os.close(directory)
        if descriptor is not None:
            os.close(descriptor)


def export_diagnostic_bundle(
    report: Report,
    destination: Path,
    *,
    scratch_root: Path | None = None,
    artifacts: Sequence[str] = (),
    logs: Mapping[str, str] | None = None,
) -> Path:
    """Create an exclusive ZIP of sanitized, explicitly unverified diagnostic text.

    No submission sources/PDFs are copied implicitly. Explicit scratch artifacts
    must be regular UTF-8 files with allowlisted suffixes; links and binary files
    are rejected. All text is redacted together before any archive is written.
    """
    if destination.exists() or destination.is_symlink():
        raise PreparationError("Diagnostic destination already exists")
    if isinstance(artifacts, str) or any(not isinstance(item, str) for item in artifacts):
        raise PreparationError("Diagnostic artifacts must be a sequence of text paths")
    if len(artifacts) > 100 or len(set(artifacts)) != len(artifacts):
        raise PreparationError("Choose at most 100 distinct diagnostic artifacts")
    if artifacts and scratch_root is None:
        raise PreparationError("Explicit diagnostic artifacts require a scratch root")
    if logs is not None and any(
        not isinstance(k, str) or not isinstance(v, str) for k, v in logs.items()
    ):
        raise PreparationError("Diagnostic logs require text names and contents")
    selected = []
    total = 0
    for index, relative in enumerate(artifacts, 1):
        if not isinstance(relative, str) or scratch_root is None:
            raise PreparationError("Diagnostic artifact paths must be text")
        content = _scratch_text(scratch_root, relative)
        total += len(content.encode("utf-8"))
        if total > _MAX_ARTIFACT_TOTAL:
            raise PreparationError("Diagnostic text artifacts exceed the total size limit")
        selected.append(
            {
                "entry": f"scratch/{index:04d}{PurePosixPath(relative).suffix.lower()}",
                "source": relative,
                "content": content,
            }
        )
    envelope = replace(
        report,
        outcome="blocked",
        scope="Unverified diagnostic export; this is not a submission package",
        findings=[
            *report.findings,
            Finding(
                "report.diagnostic_bundle",
                "This diagnostic export is unverified and cannot be used as a submission package.",
                "info",
                "inconclusive",
                details={"source_outcome": report.outcome},
            ),
        ],
        execution={
            **report.execution,
            "diagnostic_export": {
                "source_outcome": report.outcome,
                "source_scope": report.scope,
                "logs": dict(logs or {}),
                "scratch": selected,
            },
        },
    ).redacted_copy()
    diagnostic = envelope.execution.get("diagnostic_export")
    if not isinstance(diagnostic, dict):
        raise PreparationError("Diagnostic data exceeded safe report redaction limits")
    entries = {
        "README.txt": "UNVERIFIED DIAGNOSTIC BUNDLE\n\nThis archive contains sanitized diagnostic "
        "material. It is not a submission package and has not been rebuilt or verified. "
        "Credential detection is bounded and heuristic; review content before sharing.\n",
        "report.json": json.dumps(envelope.to_dict(), indent=2, ensure_ascii=False) + "\n",
        "report.html": render_html(envelope),
        "logs/stages.json": json.dumps(envelope.stages, indent=2, ensure_ascii=False) + "\n",
        "logs/explicit.json": json.dumps(diagnostic.get("logs", {}), indent=2, ensure_ascii=False)
        + "\n",
    }
    for index, change in enumerate(envelope.changes, 1):
        if change.diff:
            entries[f"diffs/{index:04d}.diff"] = change.diff
    clean_scratch = diagnostic.get("scratch")
    if not isinstance(clean_scratch, list) or len(clean_scratch) != len(selected):
        raise PreparationError("Diagnostic artifacts could not be safely sanitized")
    for index, item in enumerate(clean_scratch):
        if not isinstance(item, dict) or not isinstance(item.get("content"), str):
            raise PreparationError("Diagnostic artifact could not be safely sanitized")
        # Archive names come from generated control data, never redacted source names.
        entries[selected[index]["entry"]] = item["content"]
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, contents in sorted(entries.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, contents.encode("utf-8"))
    try:
        with destination.open("xb") as stream:
            stream.write(output.getvalue())
    except OSError as error:
        raise PreparationError(
            "Cannot write diagnostic bundle to the requested destination"
        ) from error
    return destination
