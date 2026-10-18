"""Offline HTML report with escaped evidence and no executable or remote resources."""

from __future__ import annotations

import html
import json
import math
import unicodedata
from base64 import b64encode
from collections.abc import Mapping
from urllib.parse import quote

from .errors import PreparationError
from .finding_view import (
    RedactedReport,
    finding_views,
    not_run_summaries,
    redact_report,
    size_label,
)
from .findings import Finding
from .pdf_previews import MAX_PREVIEW_BYTES, png_dimensions
from .report import Report
from .review_policy import is_literal_relative_path

_STYLE = (
    "body{font:16px/1.5 system-ui,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem;"
    "color:#17202a;background:#fafafa}"
    "article,section{margin:1rem 0;padding:1rem;border:1px solid #bcc6cc;border-radius:.35rem;"
    "background:white}"
    "h1,h2,h3{line-height:1.2}"
    "pre,td{white-space:pre-wrap;overflow-wrap:anywhere}"
    "pre{overflow:auto}"
    "table{width:100%;border-collapse:collapse;table-layout:fixed}"
    "td,th{text-align:left;vertical-align:top;border:1px solid #cad1d6;padding:.4rem}"
    ".removed{background:#ffe8e8}"
    ".added{background:#e3f6e7}"
    ".context{background:#edf0f3}"
    ".review{border-left:4px solid #9a6200;padding:.5rem}"
    ".location{color:#42515e}"
    "svg{max-height:400px;max-width:100%;width:340px}"
    ".paper{fill:white;stroke:#526474;stroke-width:1}"
    ".region{fill:#eaa43b;fill-opacity:.3;stroke:#9a4900;stroke-width:1}"
    "figcaption{font-size:.85rem}"
    "a{color:#005599}"
    ".page-pair{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:1rem}"
    ".page-pair figure{margin:0;min-width:0}"
    ".page-pair img{display:block;width:100%;height:auto}"
    ".page-pair figcaption{font-weight:600;margin-bottom:.4rem}"
    ".page-navigation{display:flex;flex-wrap:wrap;gap:.5rem 1rem}"
    ".page-navigation a,summary{padding:.25rem 0}"
    "summary{cursor:pointer}"
    "a:focus-visible,summary:focus-visible{outline:3px solid #006b9a;outline-offset:3px}"
    ".difference-overlay{position:relative;isolation:isolate;background:black;max-width:700px}"
    ".difference-overlay img{display:block;width:100%;height:auto}"
    ".difference-overlay .difference-layer{position:absolute;inset:0;mix-blend-mode:difference}"
    "caption{text-align:left;font-weight:600;padding:.5rem 0}"
    "@media(max-width:650px){body{margin:1rem auto;padding:0 .5rem}"
    "article,section{padding:.65rem}"
    ".page-pair{grid-template-columns:1fr}"
    ".file-table{overflow-x:auto}"
    ".file-table table{min-width:480px}}"
)


def _visible_controls(value: object) -> str:
    rendered = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return "".join(
        f"\\u{ord(char):04x}"
        if unicodedata.category(char).startswith("C") and char not in "\n\t"
        else char
        for char in rendered
    )


def _escape_html(value: object) -> str:
    """Escape untrusted text for HTML content and attributes; controls become visible."""
    return html.escape(_visible_controls(value), quote=True)


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
    if not is_literal_relative_path(relative):
        return None
    return quote(path, safe="/") + f"#page={page}"


def _regions(finding: Finding, report: RedactedReport) -> str:
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
            if evidence.stage != finding.stage:
                continue
            if evidence.document != finding.document:
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
            + (
                f' <a href="{_escape_html(link)}">Open local PDF at page {page}</a>.'
                if link
                else ""
            )
            + " The diagram does not embed or modify the PDF.</figcaption></figure>"
        )
    return "".join(parts)


def _diff_html(diff: str) -> str:
    rows = []
    for line in diff.splitlines():
        if line.startswith(("---", "+++", "@@")):
            rows.append(f'<tr><td colspan="2" class="context">{_escape_html(line)}</td></tr>')
        elif line.startswith("-"):
            rows.append(f'<tr><td class="removed">{_escape_html(line[1:])}</td><td></td></tr>')
        elif line.startswith("+"):
            rows.append(f'<tr><td></td><td class="added">{_escape_html(line[1:])}</td></tr>')
        else:
            content = _escape_html(line[1:] if line.startswith(" ") else line)
            rows.append(f"<tr><td>{content}</td><td>{content}</td></tr>")
    return (
        '<table class="diff"><thead><tr><th>Before</th><th>After</th></tr></thead><tbody>'
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
            f"<section><h3>{_escape_html(package.get('document') or 'Submission')}</h3>"
            f"<p><strong>{_escape_html(package.get('status', 'incomplete'))}</strong> contents: "
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
            path = _escape_html(row.get("source", ""))
            if row.get("path") != row.get("source"):
                path += "<br>→ " + _escape_html(row.get("path", ""))
            reasons = row.get("reasons", [])
            reasons = reasons if isinstance(reasons, list) else [reasons]
            parts.append(
                f"<tr><td>{_escape_html(row.get('status'))}</td><td>{path}</td>"
                f"<td>{'<br>'.join(_escape_html(reason) for reason in reasons)}</td>"
                f"<td>{size_label(row.get('size_bytes'))}<br>"
                f"{size_label(row.get('compressed_bytes'))}</td></tr>"
            )
        parts.append("</tbody></table></div></section>")
    return "".join(parts)


def _preview_image(descriptor: Mapping[str, object], images: Mapping[str, bytes]) -> bytes | None:
    """Bounded PNG bytes for a descriptor's image, if the explicit store holds valid ones."""
    image_id = descriptor.get("image_id")
    data = images.get(image_id) if isinstance(image_id, str) else None
    if not isinstance(data, bytes) or len(data) > MAX_PREVIEW_BYTES:
        return None
    try:
        png_dimensions(data)
    except PreparationError:
        return None
    return data


def _page_comparison_html(
    page: Mapping[str, object], anchor: str, images: Mapping[str, bytes]
) -> list[str]:
    page_number = page.get("page", "?")
    sections = [
        f'<article id="{anchor}" class="page-comparison"><h4>Page {_escape_html(page_number)}</h4>'
    ]
    if page.get("text_only"):
        sections.append("<p>Extracted text differs although the rendered pages match.</p>")
    if page.get("page_dimensions_changed"):
        sections.append("<p>Page dimensions differ. Images retain their own proportions.</p>")
    if page.get("within_render_tolerance"):
        sections.append("<p>The measured visual difference is within the configured tolerance.</p>")
    sections.append('<div class="page-pair">')
    sources = {}
    for side, label in (("left", "Before"), ("right", "After")):
        descriptor = page.get(side, {})
        descriptor = descriptor if isinstance(descriptor, dict) else {}
        sections.append(f"<figure><figcaption>{label}</figcaption>")
        data = _preview_image(descriptor, images)
        if data is not None:
            uri = "data:image/png;base64," + b64encode(data).decode("ascii")
            sources[side] = uri
            sections.append(
                f'<img src="{uri}" alt="{label}, page {_escape_html(page_number)}" loading="lazy">'
            )
        elif descriptor.get("present") is False:
            sections.append("<p>This page does not exist in this PDF.</p>")
        else:
            status = descriptor.get("status", page.get("status", "unavailable"))
            if status == "captured":
                status = "image unavailable in this export"
            sections.append(
                f"<p>Preview {_escape_html(status)}. "
                f"{_escape_html(descriptor.get('error', ''))}</p>"
            )
        sections.append("</figure>")
    sections.append("</div>")
    if len(sources) == 2 and not page.get("page_dimensions_changed"):
        sections.append(
            "<details><summary>Difference overlay</summary>"
            "<p>Matching pixels are black; nonblack areas differ. "
            "The original images above "
            "remain the reference for interpreting the change.</p>"
            '<div class="difference-overlay" role="img" '
            'aria-label="Pixel difference overlay">'
            f'<img src="{sources["left"]}" alt="" loading="lazy">'
            f'<img class="difference-layer" src="{sources["right"]}" alt="" loading="lazy">'
            "</div></details>"
        )
    sections.append("</article>")
    return sections


def _pdf_previews_html(report: RedactedReport, images: Mapping[str, bytes]) -> str:
    summary = report.execution.get("pdf_previews")
    if not isinstance(summary, dict):
        return ""
    comparisons = summary.get("comparisons", [])
    if not isinstance(comparisons, list) or not comparisons:
        return ""
    parts = [
        '<h2 id="pdf-differences">PDF differences</h2>',
        "<p>Page images show manuscript content. Preview limits do not change "
        "the comparison result.</p>",
    ]
    navigation = []
    sections = []
    for index, comparison in enumerate(comparisons, 1):
        if not isinstance(comparison, dict):
            continue
        title = str(comparison.get("stage", "PDF comparison"))
        if comparison.get("document"):
            title = f"{comparison['document']}: {title}"
        sections.append(f"<section><h3>{_escape_html(title)}</h3>")
        pages = comparison.get("pages", [])
        if comparison.get("error"):
            sections.append(f"<p>Preview unavailable: {_escape_html(comparison['error'])}</p>")
        elif not pages:
            sections.append(
                f"<p>{_escape_html(comparison.get('status', 'incomplete')).capitalize()}.</p>"
            )
        omitted = comparison.get("omitted_pages", 0)
        if type(omitted) is int and omitted > 0:
            sections.append(
                f"<p>{omitted} affected page pair(s) omitted because the shared preview limit "
                "was reached. Their comparison results remain in the findings.</p>"
            )
        for page_index, page in enumerate(pages if isinstance(pages, list) else [], 1):
            if not isinstance(page, dict) or page.get("status") == "omitted":
                continue
            anchor = f"comparison-{index}-page-{page_index}"
            navigation.append(
                f'<a href="#{anchor}">{_escape_html(title)} · '
                f"page {_escape_html(page.get('page', '?'))}</a>"
            )
            sections.extend(_page_comparison_html(page, anchor, images))
        sections.append("</section>")
    if navigation:
        parts.append(
            '<nav class="page-navigation" aria-label="Changed pages">'
            + " ".join(navigation)
            + "</nav>"
        )
    parts.extend(sections)
    return "".join(parts)


def _findings_html(report: RedactedReport) -> list[str]:
    parts = ["<h2>Findings and actions</h2>"]
    for index, view in enumerate(finding_views(report, show_hidden=True), 1):
        finding = view.finding
        parts.extend(
            [
                f'<article id="finding-{index}"><h3>{_escape_html(view.code)} — '
                f"{_escape_html(view.title)}</h3><p>{_escape_html(finding.severity)} / "
                f"{_escape_html(view.status_label)}</p>",
                f'<p class="location">{_escape_html(view.location)}</p>',
                f"<p>{_escape_html(view.message)}</p>",
            ]
        )
        if view.evidence:
            parts.append(f"<p>Evidence: {_escape_html(view.evidence)}</p>")
        if view.review:
            parts.append(
                f'<p class="review">{_escape_html(view.review)}. Original result retained.</p>'
            )
        if view.next_step:
            parts.append(f"<p><strong>Next action:</strong> {_escape_html(view.next_step)}</p>")
        parts.append(_regions(finding, report))
        parts.append(
            "<details><summary>Recorded evidence</summary><pre>"
            + _escape_html(
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
            + "".join(f"<p>{_escape_html(line)}</p>" for line in not_run)
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
            f"<section><h3>{_escape_html(change.path)}</h3><p>{_escape_html(change.kind)}: "
            f"{_escape_html(change.reason)}</p>"
        )
        if change.destination:
            parts.append(f"<p>Destination: {_escape_html(change.destination)}</p>")
        if change.diff:
            parts.append(_diff_html(change.diff))
        parts.append("</section>")
    return parts


def render_html(report: Report, *, preview_images: Mapping[str, bytes] | None = None) -> str:
    """Render one offline document with escaped evidence and no executable resources."""
    return render_redacted_html(redact_report(report), preview_images=preview_images)


def render_redacted_html(
    report: RedactedReport, *, preview_images: Mapping[str, bytes] | None = None
) -> str:
    """Render a report that was already redacted together with other exported text."""
    parts = [
        '<!doctype html><html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
        "style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'\">",
        f"<title>Fledge report</title><style>{_STYLE}</style></head><body>",
        f"<h1>{_escape_html(report.outcome.replace('_', ' ').capitalize())}</h1>",
        f"<p>{_escape_html(report.scope)}</p>",
        "<p>Results cover the stated checks. Review decisions retain the original evidence; "
        "they do not prove compliance or supply missing verification.</p>",
    ]
    if report.main:
        parts.append(f"<p>Document: {_escape_html(report.main)}</p>")
    parts.append(_pdf_previews_html(report, preview_images or {}))
    parts.append(_package_contents_html(report))
    parts.extend(_findings_html(report))
    parts.extend(_changes_html(report))
    parts.append(
        "<details><summary>Run settings, stages, tools and artifacts</summary><pre>"
        + _escape_html(
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
        + "</pre></details></body></html>\n"
    )
    return "".join(parts)
