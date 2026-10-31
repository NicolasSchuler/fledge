"""The PDF-differences section of the HTML report: before and after page images.

Reads the preview summary the job recorded in ``execution.pdf_previews`` into typed
records, and embeds the bounded PNG images of the explicit preview store as data URIs.
Page images show manuscript content; this module adds no remote resource.
"""

from __future__ import annotations

from base64 import b64encode
from collections.abc import Mapping
from dataclasses import dataclass

from .errors import PreparationError
from .execution_records import PDF_PREVIEWS_KEY
from .html_text import escape_html
from .pdf_previews import MAX_PREVIEW_BYTES, preview_dimensions
from .report import RedactedReport


@dataclass(frozen=True, slots=True)
class PreviewSide:
    """One side (before or after) of a page pair, as the preview store described it.

    Attributes:
        image_id: The id of the side's image in the preview store, or ``None``.
        page_absent: Whether the page does not exist in this side's PDF.
        status: The preview status the store recorded for this side.
        error: The error the store recorded for this side.
    """

    image_id: str | None
    page_absent: bool
    status: object
    error: object


@dataclass(frozen=True, slots=True)
class PreviewPage:
    """A compared page pair.

    Attributes:
        number: The page as the comparison recorded it.
        omitted: Whether the pair was left out of the previews.
        text_only: Whether only the extracted text differs; the rendered pages match.
        dimensions_changed: Whether the page dimensions differ.
        within_tolerance: Whether the measured visual difference is within the configured
            tolerance.
        status: The preview status of the pair.
        left: The page before.
        right: The page after.
    """

    number: object
    omitted: bool
    text_only: bool
    dimensions_changed: bool
    within_tolerance: bool
    status: object
    left: PreviewSide
    right: PreviewSide


@dataclass(frozen=True, slots=True)
class PreviewComparison:
    """The preview summary of one PDF comparison, titled by document and stage.

    Attributes:
        title: The stage, preceded by the document when there is one.
        status: The preview status of the comparison.
        error: Why the previews are unavailable, if they are.
        omitted_pages: How many affected page pairs the shared preview limit left out.
        pages: The compared page pairs.
    """

    title: str
    status: object
    error: object
    omitted_pages: int
    pages: tuple[PreviewPage, ...]


def _read_side(descriptor: object) -> PreviewSide:
    """Read one side of a recorded page pair; a malformed descriptor reads as empty."""
    data = descriptor if isinstance(descriptor, dict) else {}
    image_id = data.get("image_id")
    return PreviewSide(
        image_id=image_id if isinstance(image_id, str) else None,
        page_absent=data.get("present") is False,
        status=data.get("status"),
        error=data.get("error", ""),
    )


def _read_page(page: dict) -> PreviewPage:
    """Read one recorded page pair, with defaults for missing entries."""
    return PreviewPage(
        number=page.get("page", "?"),
        omitted=page.get("status") == "omitted",
        text_only=bool(page.get("text_only")),
        dimensions_changed=bool(page.get("page_dimensions_changed")),
        within_tolerance=bool(page.get("within_render_tolerance")),
        status=page.get("status", "unavailable"),
        left=_read_side(page.get("left", {})),
        right=_read_side(page.get("right", {})),
    )


def read_preview_comparisons(execution: Mapping[str, object]) -> tuple[PreviewComparison, ...]:
    """Read the comparisons in ``execution.pdf_previews``; malformed entries are skipped."""
    summary = execution.get(PDF_PREVIEWS_KEY)
    comparisons = summary.get("comparisons", []) if isinstance(summary, dict) else []
    if not isinstance(comparisons, list):
        return ()
    result = []
    for comparison in comparisons:
        if not isinstance(comparison, dict):
            continue
        title = str(comparison.get("stage", "PDF comparison"))
        if comparison.get("document"):
            title = f"{comparison['document']}: {title}"
        pages = comparison.get("pages", [])
        omitted = comparison.get("omitted_pages", 0)
        result.append(
            PreviewComparison(
                title=title,
                status=comparison.get("status", "incomplete"),
                error=comparison.get("error"),
                omitted_pages=omitted if type(omitted) is int else 0,
                pages=tuple(_read_page(page) for page in pages if isinstance(page, dict))
                if isinstance(pages, list)
                else (),
            )
        )
    return tuple(result)


def _preview_image(side: PreviewSide, images: Mapping[str, bytes]) -> bytes | None:
    """Return the bounded PNG bytes of a side's image, if the explicit store holds valid ones.

    Args:
        side: The side whose image is looked up.
        images: The explicit preview store, by image id.

    Returns:
        The image, or ``None`` when it is missing, larger than ``MAX_PREVIEW_BYTES`` or not a
        valid preview image.
    """
    data = images.get(side.image_id) if side.image_id is not None else None
    if not isinstance(data, bytes) or len(data) > MAX_PREVIEW_BYTES:
        return None
    try:
        preview_dimensions(data)
    except PreparationError:
        return None
    return data


def _page_notes(page: PreviewPage) -> list[str]:
    """Explain text-only differences, changed dimensions and differences within tolerance."""
    notes = []
    if page.text_only:
        notes.append("<p>Extracted text differs although the rendered pages match.</p>")
    if page.dimensions_changed:
        notes.append("<p>Page dimensions differ. Images retain their own proportions.</p>")
    if page.within_tolerance:
        notes.append("<p>The measured visual difference is within the configured tolerance.</p>")
    return notes


def _side_html(
    side: PreviewSide, label: str, page: PreviewPage, images: Mapping[str, bytes]
) -> tuple[str, str | None]:
    """Draw the figure of one side, and return the data URI of its image if it has one.

    Args:
        side: The side to draw.
        label: The caption, ``Before`` or ``After``.
        page: The page pair the side belongs to.
        images: The explicit preview store, by image id.

    Returns:
        The figure's HTML and the data URI of its image, or ``None`` without an image.
    """
    parts = [f"<figure><figcaption>{label}</figcaption>"]
    uri = None
    data = _preview_image(side, images)
    if data is not None:
        uri = "data:image/png;base64," + b64encode(data).decode("ascii")
        parts.append(
            f'<img src="{uri}" alt="{label}, page {escape_html(page.number)}" loading="lazy">'
        )
    elif side.page_absent:
        parts.append("<p>This page does not exist in this PDF.</p>")
    else:
        status = side.status if side.status is not None else page.status
        if status == "captured":
            status = "image unavailable in this export"
        parts.append(f"<p>Preview {escape_html(status)}. {escape_html(side.error)}</p>")
    parts.append("</figure>")
    return "".join(parts), uri


def _difference_overlay(left_uri: str, right_uri: str) -> str:
    """Overlay the after image on the before image so that differing pixels stand out."""
    return (
        "<details><summary>Difference overlay</summary>"
        "<p>Matching pixels are black; nonblack areas differ. "
        "The original images above "
        "remain the reference for interpreting the change.</p>"
        '<div class="difference-overlay" role="img" '
        'aria-label="Pixel difference overlay">'
        f'<img src="{left_uri}" alt="" loading="lazy">'
        f'<img class="difference-layer" src="{right_uri}" alt="" loading="lazy">'
        "</div></details>"
    )


def _page_comparison_html(page: PreviewPage, anchor: str, images: Mapping[str, bytes]) -> str:
    """Show one page pair side by side.

    A difference overlay follows when both sides have an image and the page dimensions
    did not change.

    Args:
        page: The page pair.
        anchor: The ``id`` of the page's article, the target of the navigation link.
        images: The explicit preview store, by image id.

    Returns:
        The HTML of the page pair.
    """
    sections = [
        f'<article id="{anchor}" class="page-comparison"><h4>Page {escape_html(page.number)}</h4>',
        *_page_notes(page),
        '<div class="page-pair">',
    ]
    uris = {}
    for side, label in ((page.left, "Before"), (page.right, "After")):
        figure, uri = _side_html(side, label, page, images)
        sections.append(figure)
        if uri is not None:
            uris[label] = uri
    sections.append("</div>")
    if len(uris) == 2 and not page.dimensions_changed:
        sections.append(_difference_overlay(uris["Before"], uris["After"]))
    sections.append("</article>")
    return "".join(sections)


def _comparison_html(
    index: int, comparison: PreviewComparison, images: Mapping[str, bytes]
) -> tuple[list[str], list[str]]:
    """Show the section of one comparison and the navigation links to its pages.

    Args:
        index: The comparison's position, counted from 1, which makes page anchors unique.
        comparison: The comparison to show.
        images: The explicit preview store, by image id.

    Returns:
        The HTML parts of the section, and one navigation link per page pair shown.
    """
    sections = [f"<section><h3>{escape_html(comparison.title)}</h3>"]
    if comparison.error:
        sections.append(f"<p>Preview unavailable: {escape_html(comparison.error)}</p>")
    elif not comparison.pages:
        sections.append(f"<p>{escape_html(comparison.status).capitalize()}.</p>")
    if comparison.omitted_pages > 0:
        sections.append(
            f"<p>{comparison.omitted_pages} affected page pair(s) omitted because the shared "
            "preview limit was reached. Their comparison results remain in the findings.</p>"
        )
    navigation = []
    for page_index, page in enumerate(comparison.pages, 1):
        if page.omitted:
            continue
        anchor = f"comparison-{index}-page-{page_index}"
        navigation.append(
            f'<a href="#{anchor}">{escape_html(comparison.title)} · '
            f"page {escape_html(page.number)}</a>"
        )
        sections.append(_page_comparison_html(page, anchor, images))
    sections.append("</section>")
    return sections, navigation


def pdf_previews_html(report: RedactedReport, images: Mapping[str, bytes]) -> str:
    """Show the PDF-differences section, or an empty string when no previews were recorded.

    Args:
        report: The redacted report, whose execution record holds the preview summary.
        images: The explicit preview store, by image id.

    Returns:
        The HTML of the section.
    """
    comparisons = read_preview_comparisons(report.execution)
    if not comparisons:
        return ""
    parts = [
        '<h2 id="pdf-differences">PDF differences</h2>',
        "<p>Page images show manuscript content. Preview limits do not change "
        "the comparison result.</p>",
    ]
    navigation: list[str] = []
    sections: list[str] = []
    for index, comparison in enumerate(comparisons, 1):
        body, links = _comparison_html(index, comparison, images)
        sections.extend(body)
        navigation.extend(links)
    if navigation:
        parts.append(
            '<nav class="page-navigation" aria-label="Changed pages">'
            + " ".join(navigation)
            + "</nav>"
        )
    parts.extend(sections)
    return "".join(parts)
