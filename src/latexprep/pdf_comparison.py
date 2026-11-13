"""Bounded rendered and extracted-text comparison of two PDFs, with optional previews.

A comparison reports the page counts, the extracted text per page (line ends and trailing
blanks normalized) and the rendered pages at one resolution, ``COMPARISON_DPI`` unless a page
is too large for it (``comparison_dpi``), each side measured independently so one failure never
hides the others. It proves neither semantic equivalence nor equality of metadata. With a
preview collector, the pages whose text, rendering or size differ are also captured as PNG
images, within the shared preview limits; preview failures never change the findings.

Each side is measured through the session of its PDF, which within a job is shared with every
other look at the same PDF, and its pages are rendered and compared by ``pdf_page_renders``.

Does not own: the preview budget and descriptors (``pdf_previews``), the tool session and
parsers (``pdf_tool_output``), the sessions and digests a job shares (``pdf_job_sessions``),
how pages are rendered (``pdf_page_renders``) or how comparisons are scheduled
(``pdf_check_pipeline``).
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import Final, NamedTuple, TypeVar

from .command_runner import CommandResult, CommandRunner
from .errors import PreparationError
from .file_safety import FileTooLargeError, read_regular_file
from .findings import Finding
from .measurement_findings import TOOL_FAILURE_SUGGESTION, unavailable_finding
from .pdf_geometry import COMPARISON_DPI, comparison_dpi
from .pdf_job_sessions import JobPdf, PageDigests, job_pdf_sessions
from .pdf_page_renders import (
    RENDER_HEADROOM_MB,
    PageDifference,
    PixelTolerance,
    RenderOutcome,
    RenderRequest,
    RenderSide,
    pair_memory_mb,
    raster_pixel_count,
    render_page_pairs,
)
from .pdf_previews import (
    PdfPreviewCollector,
    PdfPreviewStore,
    PreviewLimitError,
    PreviewPage,
    PreviewSide,
    PreviewStatus,
    preview_dimensions,
)
from .pdf_tool_output import (
    normalized_text_pages,
    page_count,
    parse_page_sizes,
    pdfinfo_fields,
    require_rendered_pages,
)
from .pdf_tools import (
    PdfToolSession,
    SharedPdfSession,
    gather_measurements,
    layout_text_argv,
    page_boxes_argv,
    pdfinfo_argv,
    raster_filename,
    render_page_argv,
)
from .png_support import MIN_PNG_BYTES
from .resource_budget import ResourceBudget
from .workflow_options import ComparisonOptions

_T = TypeVar("_T")
# Page counts and geometry (pdfinfo), text (pdftotext) and renders (pdftoppm).
COMPARISON_TOOLS: Final = frozenset({"pdfinfo", "pdftotext", "pdftoppm"})
_SIDES: Final = ("left", "right")
_TEXT_NORMALIZATION: Final = "line endings and trailing horizontal whitespace only"


def _unavailable(rule: str, error: Exception, details: dict[str, object] | None = None) -> Finding:
    """Report ``rule`` as unavailable evidence, with the suggestion for a tool failure.

    Args:
        rule: The rule that could not be measured.
        error: The cause.
        details: The details of the finding, or ``None``.

    Returns:
        The unavailable finding.
    """
    return unavailable_finding(rule, error, suggestion=TOOL_FAILURE_SUGGESTION, details=details)


@dataclass(frozen=True, slots=True)
class ComparisonPolicy:
    """The parameters a comparison ran with, reported with each of its findings.

    Attributes:
        dpi: The resolution of the renders.
        max_changed_pixel_ratio: The largest share of a page's pixels that may differ before
            the page counts as changed.
        channel_tolerance: The largest per-channel difference (0 to 255) that still counts
            as the same color.
    """

    dpi: int
    max_changed_pixel_ratio: float
    channel_tolerance: int

    @classmethod
    def of(cls, options: ComparisonOptions | None) -> ComparisonPolicy:
        """Return the policy of explicit tolerances, or the exact comparison when there are none."""
        if options is None:
            return cls(COMPARISON_DPI, 0, 0)
        return cls(COMPARISON_DPI, options.max_changed_pixel_ratio, options.channel_tolerance)

    @property
    def tolerant(self) -> bool:
        """Whether any tolerance is set."""
        return bool(self.channel_tolerance or self.max_changed_pixel_ratio)

    def to_details(self) -> dict[str, object]:
        """Return the policy as finding details, with what the comparison does not establish."""
        return {
            "dpi": self.dpi,
            "max_changed_pixel_ratio": self.max_changed_pixel_ratio,
            "channel_tolerance": self.channel_tolerance,
            "text_normalization": _TEXT_NORMALIZATION,
            "metadata_compared": False,
            "semantic_equivalence_proven": False,
        }


@dataclass(frozen=True, slots=True)
class _ComparisonSide:
    """One preflighted comparison input.

    Attributes:
        pages: The number of pages.
        dimensions: The size of every page in points, or ``None`` when unavailable.
        text: The normalized text of every page, or ``None`` when unavailable.
        errors: Why some of the evidence is unavailable.
    """

    pages: int
    dimensions: list[tuple[float, float]] | None
    text: list[str] | None
    errors: tuple[str, ...]


class _SidePair(NamedTuple):
    """Both preflighted comparison inputs, aligned page by page.

    Attributes:
        left: The left input.
        right: The right input.
    """

    left: _ComparisonSide
    right: _ComparisonSide

    def named(self) -> Iterator[tuple[str, _ComparisonSide]]:
        """Return each side with its name, left first."""
        return zip(_SIDES, self, strict=True)

    @property
    def common_pages(self) -> int:
        """The number of pages that both sides have."""
        return min(self.left.pages, self.right.pages)

    @property
    def missing_pages(self) -> list[int]:
        """The pages that only the longer side has."""
        return list(range(self.common_pages + 1, max(self.left.pages, self.right.pages) + 1))

    @property
    def dimensions_known(self) -> bool:
        """Whether the page sizes of both sides are known."""
        return self.left.dimensions is not None and self.right.dimensions is not None

    @property
    def dpi(self) -> int:
        """The resolution at which both sides are rendered: the highest one every page fits."""
        return min(comparison_dpi(side.dimensions or []) for side in (self.left, self.right))


def _side_evidence(
    outcome: CommandResult | PreparationError,
    parse: Callable[[str, int], _T],
    pages: int,
    subject: str,
    errors: list[str],
) -> _T | None:
    """Parse one measurement of a side, recording a failure in ``errors`` instead of raising.

    Args:
        outcome: The result of the measurement's command, or the error it ended with.
        parse: Parses the command's output for the page count.
        pages: The page count of the side.
        subject: Names the measurement in ``errors``.
        errors: The errors of the side, extended in place.

    Returns:
        The parsed evidence, or ``None`` when it is unavailable.
    """
    if isinstance(outcome, PreparationError):
        errors.append(f"{subject}: {outcome}")
        return None
    try:
        return parse(outcome.stdout, pages)
    except (PreparationError, OSError) as error:
        errors.append(f"{subject}: {error}")
        return None


def _comparable_page_sizes(output: str, pages: int) -> list[tuple[float, float]]:
    """Parse the page sizes in points, refusing pages too large to compare.

    A page must fit the raster limits at some comparison resolution (``comparison_dpi``).

    Args:
        output: The page box output of pdfinfo.
        pages: The number of pages.

    Returns:
        The width and height of every page.

    Raises:
        PreparationError: The sizes cannot be parsed, or a page is too large.
    """
    sizes = parse_page_sizes(output, pages)
    comparison_dpi(sizes)
    return sizes


class _Views(NamedTuple):
    """The sessions that measure and render the two compared PDFs, and their page digests.

    Both sessions write below the comparison's workspace, under names that carry the side.

    Attributes:
        left: The view of the left PDF's session.
        right: The view of the right PDF's session.
        left_digests: The digests of the left PDF's rendered pages.
        right_digests: The digests of the right PDF's rendered pages.
    """

    left: PdfToolSession
    right: PdfToolSession
    left_digests: PageDigests
    right_digests: PageDigests

    def named(self) -> Iterator[tuple[str, PdfToolSession]]:
        """Return each side's session with the side's name, left first."""
        return zip(_SIDES, (self.left, self.right), strict=True)

    def session(self, side: str) -> PdfToolSession:
        """Return the session of the side named ``side``."""
        return self.left if side == "left" else self.right

    def tool_versions(self) -> dict[str, object]:
        """Return the version of each tool either side ran or relied on, as report data."""
        return dict(sorted({**self.left.tool_versions(), **self.right.tool_versions()}.items()))


def _result(result: CommandResult) -> CommandResult:
    """Return a measurement's result as it is; the comparison parses it per side."""
    return result


async def _prepare_side(session: PdfToolSession, name: str) -> _ComparisonSide:
    """Preflight one comparison input: its page count, page sizes and text.

    The measurements are the ones every PDF check makes, remembered by the PDF's shared
    session, so a PDF that was inspected before is not measured again.

    Args:
        session: The side's session.
        name: The side, ``left`` or ``right``.

    Returns:
        The side; a failed geometry or text measurement is recorded among its errors.

    Raises:
        PreparationError: The page count cannot be read or exceeds the rendered page limit.
    """
    info = await session.measure(f"{name}/properties", pdfinfo_argv(), _result)
    pages = page_count(pdfinfo_fields(info.stdout))
    require_rendered_pages(pages)
    geometry, text = await gather_measurements(
        [
            partial(session.measure, f"{name}/geometry", page_boxes_argv(pages), _result),
            partial(session.measure, f"{name}/text", layout_text_argv(), _result),
        ],
        session.budget,
    )
    errors: list[str] = []
    dimensions = _side_evidence(
        geometry, _comparable_page_sizes, pages, f"{name} page geometry", errors
    )
    text_pages = _side_evidence(
        text, normalized_text_pages, pages, f"{name} text extraction", errors
    )
    return _ComparisonSide(pages, dimensions, text_pages, tuple(errors))


async def _prepare_sides(views: _Views) -> tuple[_SidePair | None, list[str]]:
    """Preflight both comparison inputs concurrently.

    Returns:
        The pair, or ``None`` when either side failed its preflight, and every error.
    """
    prepared = await gather_measurements(
        [partial(_prepare_side, session, name) for name, session in views.named()],
        views.left.budget,
    )
    sides: list[_ComparisonSide] = []
    errors: list[str] = []
    for name, result in zip(_SIDES, prepared, strict=True):
        if isinstance(result, PreparationError):
            errors.append(f"{name} PDF preflight: {result}")
        else:
            sides.append(result)
            errors.extend(result.errors)
    return (_SidePair(*sides) if len(sides) == 2 else None), errors


def _page_count_finding(pair: _SidePair, policy: ComparisonPolicy) -> Finding:
    """Compare the page counts of both sides."""
    page_counts = {name: side.pages for name, side in pair.named()}
    same_count = pair.left.pages == pair.right.pages
    return Finding(
        "compare.page_count",
        "Page counts match." if same_count else "Page counts changed.",
        severity="info" if same_count else "error",
        status="passed" if same_count else "failed",
        details={**policy.to_details(), **page_counts},
    )


def _changed_text_pages(pair: _SidePair) -> list[int] | None:
    """Return the pages whose normalized text differs (pages missing on one side included).

    Returns:
        The pages, or ``None`` when the text of either side could not be extracted.
    """
    left_text, right_text = pair.left.text, pair.right.text
    if left_text is None or right_text is None:
        return None
    changed = [
        page
        for page in range(1, pair.common_pages + 1)
        if left_text[page - 1] != right_text[page - 1]
    ]
    return changed + pair.missing_pages


def _text_finding(changed_text: list[int], policy: ComparisonPolicy) -> Finding:
    """Report the pages whose extracted text differs."""
    return Finding(
        "compare.text",
        "Normalized extracted text matches."
        if not changed_text
        else f"Extracted text differs on {len(changed_text)} pages.",
        severity="error" if changed_text else "info",
        status="failed" if changed_text else "passed",
        details={**policy.to_details(), "changed_pages": changed_text},
    )


def _rendering_finding(
    changed_pages: list[int], differences: Sequence[PageDifference], policy: ComparisonPolicy
) -> Finding:
    """Report the pages whose rendering differs.

    Args:
        changed_pages: The pages that differ beyond the tolerances or exist on one side only.
        differences: The measured difference of every page pair that differs.
        policy: The comparison policy.

    Returns:
        The finding.
    """
    if changed_pages:
        message = f"Rendered output differs on {len(changed_pages)} pages."
    elif policy.tolerant:
        message = f"Rendered pages match within the explicit pixel tolerances at {policy.dpi} DPI."
    else:
        message = f"Rendered pages match exactly at {policy.dpi} DPI."
    return Finding(
        "compare.rendering",
        message,
        severity="error" if changed_pages else "info",
        status="failed" if changed_pages else "passed",
        details={
            **policy.to_details(),
            "changed_pages": changed_pages,
            "measurements": [item.to_report() for item in differences],
        },
    )


@dataclass(slots=True)
class _Comparison:
    """What comparing two prepared PDFs established, before it becomes findings.

    Attributes:
        findings: The findings so far.
        errors: Why some evidence is unavailable.
        text_changes: The pages whose text differs, or ``None`` when text was not compared.
        renders: The render results, or ``None`` when the pages were not rendered.
        render_evidence: What the renders established when some pages failed, or ``None``.
    """

    findings: list[Finding]
    errors: list[str]
    text_changes: list[int] | None = None
    renders: RenderOutcome | None = None
    render_evidence: dict[str, object] | None = None


async def _compare_prepared(
    views: _Views,
    pair: _SidePair,
    errors: list[str],
    policy: ComparisonPolicy,
    *,
    count_changes: bool,
) -> _Comparison:
    """Compare the page counts, text and renders of both prepared sides.

    Args:
        views: The sessions of the two sides.
        pair: The prepared sides.
        errors: The errors so far, extended in place.
        policy: The comparison policy.
        count_changes: Whether changed pixels are counted.

    Returns:
        What the comparison established.
    """
    comparison = _Comparison([_page_count_finding(pair, policy)], errors)
    comparison.text_changes = _changed_text_pages(pair)
    if comparison.text_changes is not None:
        comparison.findings.append(_text_finding(comparison.text_changes, policy))
    if pair.left.dimensions is None or pair.right.dimensions is None:
        return comparison
    request = RenderRequest(
        RenderSide("left", views.left, pair.left.dimensions, views.left_digests, policy.dpi),
        RenderSide("right", views.right, pair.right.dimensions, views.right_digests, policy.dpi),
        PixelTolerance(policy.max_changed_pixel_ratio, policy.channel_tolerance)
        if count_changes
        else None,
    )
    outcome = await render_page_pairs(request, pair.common_pages)
    comparison.renders = outcome
    errors.extend(outcome.errors)
    changed_pages = outcome.changed_pages + pair.missing_pages
    if outcome.failed_pages:
        comparison.render_evidence = {
            "completed_pages": outcome.completed_pages,
            "unavailable_pages": outcome.failed_pages,
            "known_changed_pages": changed_pages,
            "measurements": [item.to_report() for item in outcome.differences],
        }
    else:
        comparison.findings.append(_rendering_finding(changed_pages, outcome.differences, policy))
    return comparison


def _job_pdfs(
    left: Path, right: Path, work: Path, runner: CommandRunner, budget: ResourceBudget | None
) -> tuple[JobPdf, JobPdf, bool]:
    """Return the session and digests of both PDFs, shared with the job when there is one.

    Outside a job scope each PDF gets sessions of its own, frozen beside ``work`` because the
    comparison's own workspace must not exist before its first command.

    Args:
        left: The first PDF.
        right: The second PDF.
        work: The workspace of the comparison.
        runner: Runs the PDF tools.
        budget: The resource budget, or ``None``.

    Returns:
        The left and the right PDF, and whether the caller owns them and must release them.
    """
    registry = job_pdf_sessions(runner, budget)
    if registry is not None:
        return registry.pdf(left), registry.pdf(right), False

    def own(pdf: Path, side: str) -> JobPdf:
        """Open sessions of this comparison's own for one side."""
        frozen = work.with_name(f"{work.name}-{side}-input")
        return JobPdf(SharedPdfSession(pdf, frozen, runner, budget), PageDigests())

    return own(left, "left"), own(right, "right"), True


async def _open_views(left: JobPdf, right: JobPdf, work: Path) -> _Views:
    """Open the views of both PDFs' sessions, which write below the comparison's ``work``.

    Raises:
        PreparationError: A PDF could not be frozen, or ``work`` exists.
        OSError: A PDF could not be frozen.
    """
    left_view = await left.session.view(left.session.pdf, work)
    right_view = await right.session.view(right.session.pdf, work)
    return _Views(left_view, right_view, left.digests, right.digests)


async def compare_pdfs(
    left: Path,
    right: Path,
    work: Path,
    runner: CommandRunner,
    *,
    budget: ResourceBudget | None = None,
    options: ComparisonOptions | None = None,
    previews: PdfPreviewCollector | None = None,
) -> list[Finding]:
    """Compare two PDFs by page count, text and render; capture previews if a collector is given.

    Within a job, each PDF's frozen copy, remembered measurements and page digests are the
    job's (see ``pdf_job_sessions``), so a PDF that was inspected or compared before is not
    measured or rendered again. Elsewhere the comparison opens sessions of its own.

    Args:
        left: The first PDF, such as the reference.
        right: The second PDF.
        work: The workspace of the comparison.
        runner: Runs the PDF tools.
        budget: The resource budget, or ``None``.
        options: The tolerances, or ``None`` for an exact comparison.
        previews: Collects the previews of the affected pages, or ``None``.

    Returns:
        The findings.
    """
    left_pdf, right_pdf, owned = _job_pdfs(left, right, work, runner, budget)
    try:
        return await _compare_open_pdfs(
            left_pdf, right_pdf, work, ComparisonPolicy.of(options), options, previews
        )
    finally:
        if owned:
            left_pdf.session.release()
            right_pdf.session.release()


async def _compare_open_pdfs(
    left: JobPdf,
    right: JobPdf,
    work: Path,
    policy: ComparisonPolicy,
    options: ComparisonOptions | None,
    previews: PdfPreviewCollector | None,
) -> list[Finding]:
    """Compare two PDFs through their sessions and report the findings.

    Args:
        left: The first PDF's sessions and digests.
        right: The second PDF's sessions and digests.
        work: The workspace of the comparison.
        policy: The comparison policy.
        options: The tolerances, or ``None`` for an exact comparison.
        previews: Collects the previews of the affected pages, or ``None``.

    Returns:
        The findings.
    """
    try:
        views = await _open_views(left, right, work)
    except (PreparationError, OSError) as error:
        if previews is not None:
            previews.unavailable(
                "PDF comparison inputs were unavailable; no previews were captured."
            )
        return [_unavailable("compare.unavailable", error)]
    pair, errors = await _prepare_sides(views)
    comparison = _Comparison([], errors)
    if pair is not None and pair.dimensions_known:
        # Large pages, such as an A0 poster, are compared at a lower resolution.
        policy = replace(policy, dpi=min(policy.dpi, pair.dpi))
    if pair is not None:
        comparison = await _compare_prepared(
            views, pair, errors, policy, count_changes=options is not None
        )
    findings, errors = comparison.findings, comparison.errors
    if errors:
        findings.append(
            _unavailable(
                "compare.unavailable",
                PreparationError("; ".join(errors)),
                details={**policy.to_details(), **(comparison.render_evidence or {})},
            )
        )
    versions = views.tool_versions()
    if versions:
        findings.append(
            Finding(
                "compare.tools",
                "Comparison tool versions were recorded.",
                severity="info",
                status="passed",
                details={"tools": versions},
            )
        )
    if previews is not None:
        evidence = (
            None if pair is None else _ChangeEvidence.of(pair, comparison, incomplete=bool(errors))
        )
        await _capture_previews(views, pair, evidence, previews, policy.dpi)
    return findings


class _PageSide(NamedTuple):
    """One side of a page pair: which page, which PDF and how large the page is in points.

    Attributes:
        page: The page number.
        side: The side, ``left`` or ``right``.
        size: The width and height of the page, in points.
    """

    page: int
    side: str
    size: tuple[float, float]


async def _capture_png(
    session: PdfToolSession, target: _PageSide, store: PdfPreviewStore, slot: int, dpi: int
) -> tuple[str, tuple[int, int]]:
    """Render one optional image without retaining any scratch-file references.

    Args:
        session: The session of the side that ``target`` names.
        target: The side of the page to render.
        store: The shared preview budget, which keeps the image.
        slot: The preview slot of the page pair.
        dpi: The resolution of the comparison.

    Returns:
        The image id and the width and height of the image.

    Raises:
        PreviewLimitError: A preview or tool file byte limit is reached.
        PreparationError: The renderer produced no regular PNG within the geometry
            reservation.
    """
    page, side, size = target
    name = f"preview-{page:04d}-{side}"
    async with session.lease(
        f"preview PDF page {page} {side}", memory_mb=pair_memory_mb(size, size, dpi), kind="render"
    ):
        try:
            maximum = min(session.runner.limits.max_file_bytes, store.remaining_bytes)
            if maximum < MIN_PNG_BYTES:
                raise PreviewLimitError("Insufficient PNG preview or tool file byte allowance")
            await session.run_without_lease(
                name,
                render_page_argv(page, dpi, "png"),
                memory_mb=RENDER_HEADROOM_MB,
            )
            path = session.work / name / "command" / raster_filename("png")
            try:
                data = read_regular_file(path, maximum)
            except FileTooLargeError:
                raise PreviewLimitError(
                    "PNG image exceeds the preview or tool file byte limit"
                ) from None
            except OSError:
                raise PreparationError(
                    "Preview renderer did not produce a regular PNG image"
                ) from None
            pixels = preview_dimensions(data)
            if pixels[0] * pixels[1] > raster_pixel_count(size, dpi):
                raise PreparationError("Preview PNG dimensions exceed the geometry reservation")
            return store.add_image(slot, side, data), pixels
        finally:
            preview_work = session.work / name
            if preview_work.exists():
                shutil.rmtree(preview_work)


@dataclass(frozen=True, slots=True)
class _ChangeEvidence:
    """What the comparison established about each page of a side pair.

    Attributes:
        text_changes: The pages whose text differs, or ``None`` when text was not compared.
        differences: The rendering differences, by page.
        completed: The pages whose pair was rendered.
        missing_pages: The pages that only one side has.
        dimensions_changed: The pages whose size differs between the sides.
        incomplete: Whether some comparison evidence is unavailable.
    """

    text_changes: frozenset[int] | None
    differences: Mapping[int, PageDifference]
    completed: frozenset[int]
    missing_pages: frozenset[int]
    dimensions_changed: frozenset[int]
    incomplete: bool

    @classmethod
    def of(cls, pair: _SidePair, comparison: _Comparison, *, incomplete: bool) -> _ChangeEvidence:
        """Collect what ``comparison`` established about each page of ``pair``.

        Args:
            pair: The prepared sides.
            comparison: What the comparison established.
            incomplete: Whether some comparison evidence is unavailable.

        Returns:
            The evidence.
        """
        renders = comparison.renders
        left, right = pair.left.dimensions, pair.right.dimensions
        return cls(
            text_changes=(
                None if comparison.text_changes is None else frozenset(comparison.text_changes)
            ),
            differences={item.page: item for item in renders.differences} if renders else {},
            completed=frozenset(renders.completed_pages) if renders else frozenset(),
            missing_pages=frozenset(pair.missing_pages),
            dimensions_changed=frozenset(
                page
                for page in range(1, pair.common_pages + 1)
                if left is not None and right is not None and left[page - 1] != right[page - 1]
            ),
            incomplete=incomplete,
        )

    @property
    def affected_pages(self) -> list[int]:
        """The pages that differ in any way, in order."""
        return sorted(
            (self.text_changes or frozenset())
            | self.differences.keys()
            | self.missing_pages
            | self.dimensions_changed
        )


def _preview_page(page: int, pair: _SidePair, evidence: _ChangeEvidence) -> PreviewPage:
    """Return a page's change summary with both of its sides pending capture.

    Args:
        page: The page number.
        pair: The prepared sides.
        evidence: What the comparison established about each page.

    Returns:
        The descriptor of the page.
    """
    sides = []
    for _, side in pair.named():
        present = page <= side.pages
        sizes = side.dimensions
        sides.append(
            PreviewSide(
                present,
                sizes[page - 1] if present and sizes is not None else None,
                "pending" if present else "missing",
            )
        )
    difference = evidence.differences.get(page)
    missing = page in evidence.missing_pages
    text_changed = evidence.text_changes is not None and page in evidence.text_changes
    return PreviewPage(
        page=page,
        text_changed=text_changed if evidence.text_changes is not None else None,
        rendering_changed=(
            (difference is not None or missing) if page in evidence.completed or missing else None
        ),
        text_only=(
            text_changed
            and page in evidence.completed
            and difference is None
            and page not in evidence.dimensions_changed
        ),
        page_dimensions_changed=(
            page in evidence.dimensions_changed
            if page <= pair.common_pages and pair.dimensions_known
            else None
        ),
        page_count_changed=missing,
        within_render_tolerance=None if difference is None else difference.within_tolerance,
        left=sides[0],
        right=sides[1],
    )


async def _capture_page(
    views: _Views,
    pair: _SidePair,
    preview: PreviewPage,
    store: PdfPreviewStore,
    slot: int,
    dpi: int,
) -> None:
    """Capture the preview of each present side of a page, recording failures on it.

    Args:
        views: The sessions of the two sides.
        pair: The prepared sides.
        preview: The descriptor of the page, updated in place.
        store: The shared preview budget.
        slot: The preview slot of the page pair.
        dpi: The resolution of the comparison.
    """
    for (name, descriptor), (_, side) in zip(preview.sides(), pair.named(), strict=True):
        if not descriptor.present:
            continue
        if side.dimensions is None:
            descriptor.status = "unavailable"
            descriptor.error = "Page dimensions were unavailable."
            continue
        try:
            target = _PageSide(preview.page, name, side.dimensions[preview.page - 1])
            image_id, pixels = await _capture_png(views.session(name), target, store, slot, dpi)
            descriptor.status, descriptor.image_id, descriptor.pixels = "captured", image_id, pixels
        except PreviewLimitError:
            descriptor.status = "omitted"
            descriptor.error = "The PNG preview or tool file byte limit was reached."
        except (PreparationError, OSError):
            # Raw tool diagnostics can name scratch files. Keep only a
            # safe, actionable capture status in the descriptor store.
            descriptor.status = "unavailable"
            descriptor.error = "The PNG preview renderer was unavailable."


def _combined_status(preview: PreviewPage) -> PreviewStatus:
    """Return the status shared by the present sides of a page pair, else partial."""
    statuses = {side.status for _, side in preview.sides() if side.present}
    return next(iter(statuses)) if len(statuses) == 1 else "partial"


def _omit(preview: PreviewPage) -> None:
    """Mark a page and its present sides as omitted because a shared limit was reached."""
    preview.status = "omitted"
    preview.omission_reason = "The shared preview page or byte limit was reached."
    for _, side in preview.sides():
        if side.present:
            side.status = "omitted"


async def _capture_previews(
    views: _Views,
    pair: _SidePair | None,
    evidence: _ChangeEvidence | None,
    collector: PdfPreviewCollector,
    dpi: int,
) -> None:
    """Capture only identified differences; preview failures never change findings.

    Args:
        views: The sessions of the two sides.
        pair: The prepared sides, or ``None`` when the preflight failed.
        evidence: What the comparison established, or ``None`` when the preflight failed.
        collector: Collects the previews of this comparison.
        dpi: The resolution of the comparison, at which the previews are rendered.
    """
    if pair is None or evidence is None:
        collector.unavailable(
            "PDF comparison preflight was unavailable; affected pages are unknown."
        )
        return
    affected = evidence.affected_pages
    store = collector.store
    async with store.exclusive():
        for page in affected:
            preview = _preview_page(page, pair, evidence)
            collector.pages.append(preview)
            slot = store.reserve_page()
            if slot is None:
                _omit(preview)
                continue
            await _capture_page(views, pair, preview, store, slot, dpi)
            preview.status = _combined_status(preview)
        collector.finished = True
        if not affected and evidence.incomplete:
            collector.unavailable("Comparison evidence was incomplete; affected pages are unknown.")
