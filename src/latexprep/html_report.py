"""The offline HTML report: escaped evidence and no executable or remote resources.

Owns the page structure and the findings, changes and package sections. The PDF
previews and region diagrams are in ``html_report_previews`` and ``html_report_regions``,
the style sheet in ``report.css``. Every value shown passes through ``escape_html``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from functools import cache
from importlib import resources

from .finding_view import finding_views, not_run_summaries, size_label
from .html_report_previews import pdf_previews_html
from .html_report_regions import regions_html
from .html_text import escape_html
from .report import RedactedReport, Report, redact_report


@cache
def load_stylesheet() -> str:
    """The report's style sheet, packaged as ``report.css``."""
    return resources.files("latexprep").joinpath("report.css").read_text(encoding="utf-8")


def _diff_html(diff: str, path: str) -> str:
    """A before/after table of a unified diff; ``path`` names the file in its caption."""
    rows = []
    for line in diff.splitlines():
        if line.startswith(("---", "+++", "@@")):
            rows.append(f'<tr><td colspan="2" class="context">{escape_html(line)}</td></tr>')
        elif line.startswith("-"):
            rows.append(f'<tr><td class="removed">{escape_html(line[1:])}</td><td></td></tr>')
        elif line.startswith("+"):
            rows.append(f'<tr><td></td><td class="added">{escape_html(line[1:])}</td></tr>')
        else:
            content = escape_html(line[1:] if line.startswith(" ") else line)
            rows.append(f"<tr><td>{content}</td><td>{content}</td></tr>")
    return (
        f'<table class="diff"><caption>Changes to {escape_html(path)}</caption>'
        '<thead><tr><th scope="col">Before</th><th scope="col">After</th></tr></thead><tbody>'
        + "".join(rows)
        + "</tbody></table>"
    )


def _package_contents_html(report: RedactedReport) -> str:
    if not report.package_contents:
        return ""
    parts = ['<h2 id="package-contents">What is in the submission?</h2>']
    for package in report.package_contents:
        rows = package.get("files", [])
        rows = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
        included = sum(row.get("status") == "included" for row in rows)
        parts.append(
            f"<section><h3>{escape_html(package.get('document') or 'Submission')}</h3>"
            f"<p><strong>{escape_html(package.get('status', 'incomplete'))}</strong> contents: "
            f"{included} included, {len(rows) - included} excluded.</p>"
        )
        if package.get("status") == "proposed":
            parts.append(
                "<p>Initial selection and planned changes; final layout and ZIP not verified.</p>"
            )
        elif package.get("status") == "incomplete":
            parts.append(
                "<p>Selection is incomplete. These rows are not a verified package inventory.</p>"
            )
        elif package.get("status") == "prepared":
            parts.append("<p>Files were prepared; final package verification has not passed.</p>")
        if package.get("archive_bytes") is not None:
            parts.append(
                f"<p>ZIP: {size_label(package.get('archive_bytes'))}; "
                f"archive overhead: {size_label(package.get('overhead_bytes'))}.</p>"
            )
        parts.append(
            '<div class="file-table"><table><caption>Files and selection reasons</caption>'
            '<thead><tr><th scope="col">Status</th><th scope="col">Source → package</th>'
            '<th scope="col">Reason</th><th scope="col">File / compressed size</th>'
            "</tr></thead><tbody>"
        )
        for row in rows:
            path = escape_html(row.get("source", ""))
            if row.get("path") != row.get("source"):
                path += "<br>→ " + escape_html(row.get("path", ""))
            reasons = row.get("reasons", [])
            reasons = reasons if isinstance(reasons, list) else [reasons]
            parts.append(
                f"<tr><td>{escape_html(row.get('status'))}</td><td>{path}</td>"
                f"<td>{'<br>'.join(escape_html(reason) for reason in reasons)}</td>"
                f"<td>{size_label(row.get('size_bytes'))}<br>"
                f"{size_label(row.get('compressed_bytes'))}</td></tr>"
            )
        parts.append("</tbody></table></div></section>")
    return "".join(parts)


def _findings_html(report: RedactedReport) -> list[str]:
    parts = ["<h2>Findings and actions</h2>"]
    for index, view in enumerate(finding_views(report, show_hidden=True), 1):
        finding = view.finding
        parts.extend(
            [
                f'<article id="finding-{index}"><h3>{escape_html(view.code)} — '
                f"{escape_html(view.title)}</h3><p>{escape_html(finding.severity)} / "
                f"{escape_html(view.status_label)}</p>",
                f'<p class="location">{escape_html(view.location)}</p>',
                f"<p>{escape_html(view.message)}</p>",
            ]
        )
        if view.evidence:
            parts.append(f"<p>Evidence: {escape_html(view.evidence)}</p>")
        if view.review:
            parts.append(
                f'<p class="review">{escape_html(view.review)}. Original result retained.</p>'
            )
        if view.next_step:
            parts.append(f"<p><strong>Next action:</strong> {escape_html(view.next_step)}</p>")
        parts.append(regions_html(finding, report))
        parts.append(
            "<details><summary>Recorded evidence</summary><pre>"
            + escape_html(
                json.dumps(
                    {"evidence_kind": finding.evidence_kind, **finding.details},
                    indent=2,
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            + "</pre></details></article>"
        )
    if not_run := not_run_summaries(report):
        parts.append(
            "<section><h3>Checks that did not run</h3>"
            + "".join(f"<p>{escape_html(line)}</p>" for line in not_run)
            + "</section>"
        )
    if not report.findings:
        parts.append("<p>No findings were recorded within this scope.</p>")
    return parts


def _changes_html(report: RedactedReport) -> list[str]:
    if not report.changes:
        return []
    parts = [
        "<h2>Proposed or applied changes</h2><p>Diff excerpts show recorded changes; "
        "unchanged portions of each file may be omitted.</p>"
    ]
    for change in report.changes:
        parts.append(
            f"<section><h3>{escape_html(change.path)}</h3><p>{escape_html(change.kind)}: "
            f"{escape_html(change.reason)}</p>"
        )
        if change.destination:
            parts.append(f"<p>Destination: {escape_html(change.destination)}</p>")
        if change.diff:
            parts.append(_diff_html(change.diff, change.path))
        parts.append("</section>")
    return parts


def render_html(report: Report, *, preview_images: Mapping[str, bytes] | None = None) -> str:
    """Render one offline document with escaped evidence and no executable resources."""
    return render_redacted_html(redact_report(report), preview_images=preview_images)


def _page_title(report: RedactedReport) -> str:
    """The browser title: the command and, when known, the document the report is about."""
    title = f"Fledge {report.command} report"
    return f"{title}: {report.main}" if report.main else title


def _run_details_html(report: RedactedReport) -> str:
    return (
        "<details><summary>Run settings, stages, tools and artifacts</summary><pre>"
        + escape_html(
            json.dumps(
                {
                    "settings": report.settings,
                    "stages": report.stages,
                    "tools": report.tools,
                    "artifacts": report.artifacts,
                    "execution": report.execution,
                },
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        + "</pre></details>"
    )


def render_redacted_html(
    report: RedactedReport, *, preview_images: Mapping[str, bytes] | None = None
) -> str:
    """Render a report that was already redacted together with other exported text."""
    header = [
        '<!doctype html><html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
        "style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'\">",
        f"<title>{escape_html(_page_title(report))}</title>"
        f"<style>{load_stylesheet()}</style></head><body>",
        f"<header><h1>{escape_html(report.outcome.replace('_', ' ').capitalize())}</h1>",
        f"<p>{escape_html(report.scope)}</p>",
        "<p>Results cover the stated checks. Review decisions retain the original evidence; "
        "they do not prove compliance or supply missing verification.</p>",
    ]
    if report.main:
        header.append(f"<p>Document: {escape_html(report.main)}</p>")
    header.append("</header><main>")
    parts = [
        *header,
        pdf_previews_html(report, preview_images or {}),
        _package_contents_html(report),
        *_findings_html(report),
        *_changes_html(report),
        _run_details_html(report),
        "</main></body></html>\n",
    ]
    return "".join(parts)
