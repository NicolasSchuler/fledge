"""Bounded rendered and extracted-text comparison of two PDFs, with optional previews.

A comparison reports the page counts, the extracted text per page (line ends and trailing
blanks normalized) and the rendered pages at ``COMPARISON_DPI``, each side measured
independently so one failure never hides the others. It proves neither semantic
equivalence nor equality of metadata. With a preview collector, the pages whose text,
rendering or size differ are also captured as PNG images, within the shared preview
limits; preview failures never change the findings.

Does not own: the preview budget and descriptors (``pdf_previews``), the tool session and
parsers (``pdf_tool_output``) or how comparisons are scheduled (``pdf_check_pipeline``).
"""

from __future__ import annotations

import math
import shutil
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Final, NamedTuple, TypeVar

from .cancellation import run_in_thread
from .command_runner import CommandResult, CommandRunner
from .errors import PreparationError
from .file_safety import FileTooLargeError, read_regular_file
from .findings import Finding
from .limits import MIB
from .measurement_findings import TOOL_FAILURE_SUGGESTION, unavailable_finding
from .pdf_geometry import COMPARISON_DPI, measurement_raster_size, require_comparison_raster_fit
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
    gather_measurements,
    layout_text_argv,
    page_boxes_argv,
    pdf_input,
    pdfinfo_argv,
    raster_filename,
    render_page_argv,
)
from .png_support import MIN_PNG_BYTES
from .raster_pixels import RasterImage, count_changed_pixels, read_pnm_raster
from .resource_budget import ResourceBudget
from .workflow_options import ComparisonOptions

_T = TypeVar("_T")
# Memory a render needs beyond its two rasters: the renderer process and its buffers.
_RENDER_HEADROOM_MB: Final = 128
_RGB_BYTES_PER_PIXEL: Final = 3
# Page counts and geometry (pdfinfo), text (pdftotext) and renders (pdftoppm).
COMPARISON_TOOLS: Final = frozenset({"pdfinfo", "pdftotext", "pdftoppm"})
_SIDES: Final = ("left", "right")
_TEXT_NORMALIZATION: Final = "line endings and trailing horizontal whitespace only"


def _unavailable(rule: str, error: Exception, details: dict[str, object] | None = None) -> Finding:
    return unavailable_finding(rule, error, suggestion=TOOL_FAILURE_SUGGESTION, details=details)


@dataclass(frozen=True, slots=True)
class ComparisonPolicy:
    """The parameters a comparison ran with, reported with each of its findings."""

    dpi: int
    # Largest share of a page's pixels that may differ before the page counts as changed.
    max_changed_pixel_ratio: float
    # Largest per-channel difference (0 to 255) that still counts as the same color.
    channel_tolerance: int

    @classmethod
    def of(cls, options: ComparisonOptions | None) -> ComparisonPolicy:
        """The policy of explicit tolerances, or the exact comparison when there are none."""
        if options is None:
            return cls(COMPARISON_DPI, 0, 0)
        return cls(COMPARISON_DPI, options.max_changed_pixel_ratio, options.channel_tolerance)

    @property
    def tolerant(self) -> bool:
        return bool(self.channel_tolerance or self.max_changed_pixel_ratio)

    def to_details(self) -> dict[str, object]:
        return {
            "dpi": self.dpi,
            "max_changed_pixel_ratio": self.max_changed_pixel_ratio,
            "channel_tolerance": self.channel_tolerance,
            "text_normalization": _TEXT_NORMALIZATION,
            "metadata_compared": False,
            "semantic_equivalence_proven": False,
        }


@dataclass(frozen=True, slots=True)
class PageDifference:
    """How the renders of one page pair differ; absent pairs are identical and not recorded."""

    page: int
    left_pixels: tuple[int, int]
    right_pixels: tuple[int, int]
    page_dimensions_changed: bool
    # Set only when the pixels were counted: both renders have one size and the page
    # sizes agree, and tolerances were configured.
    changed_pixels: int | None = None
    changed_pixel_ratio: float | None = None
    within_tolerance: bool | None = None

    @property
    def counts_as_changed(self) -> bool:
        """Whether the page differs beyond the configured tolerances (or none were measured)."""
        return not self.within_tolerance

    def to_report(self) -> dict[str, object]:
        row: dict[str, object] = {
            "page": self.page,
            "left_pixels": self.left_pixels,
            "right_pixels": self.right_pixels,
            "page_dimensions_changed": self.page_dimensions_changed,
        }
        if self.changed_pixels is not None:
            row.update(
                changed_pixels=self.changed_pixels,
                changed_pixel_ratio=self.changed_pixel_ratio,
                within_tolerance=self.within_tolerance,
            )
        return row


@dataclass(frozen=True, slots=True)
class _ComparisonSide:
    pages: int
    dimensions: list[tuple[float, float]] | None
    text: list[str] | None
    errors: tuple[str, ...]


class _SidePair(NamedTuple):
    """Both preflighted comparison inputs, aligned page by page."""

    left: _ComparisonSide
    right: _ComparisonSide

    def named(self) -> Iterator[tuple[str, _ComparisonSide]]:
        return zip(_SIDES, self, strict=True)

    @property
    def common_pages(self) -> int:
        return min(self.left.pages, self.right.pages)

    @property
    def missing_pages(self) -> list[int]:
        return list(range(self.common_pages + 1, max(self.left.pages, self.right.pages) + 1))

    @property
    def dimensions_known(self) -> bool:
        return self.left.dimensions is not None and self.right.dimensions is not None


def _side_evidence(
    outcome: CommandResult | PreparationError,
    parse: Callable[[str, int], _T],
    pages: int,
    subject: str,
    errors: list[str],
) -> _T | None:
    if isinstance(outcome, PreparationError):
        errors.append(f"{subject}: {outcome}")
        return None
    try:
        return parse(outcome.stdout, pages)
    except (PreparationError, OSError) as error:
        errors.append(f"{subject}: {error}")
        return None


def _comparable_page_sizes(output: str, pages: int) -> list[tuple[float, float]]:
    """Page sizes in points, refusing pages too large to render at the comparison resolution."""
    sizes = parse_page_sizes(output, pages)
    require_comparison_raster_fit(sizes)
    return sizes


async def _prepare_side(session: PdfToolSession, name: str) -> _ComparisonSide:
    document = pdf_input(name)
    info = await session.run(f"{name}/properties", pdfinfo_argv(document))
    pages = page_count(pdfinfo_fields(info.stdout))
    require_rendered_pages(pages)
    geometry, text = await gather_measurements(
        [
            partial(session.run, f"{name}/geometry", page_boxes_argv(pages, document)),
            partial(session.run, f"{name}/text", layout_text_argv(document)),
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


async def _prepare_sides(session: PdfToolSession) -> tuple[_SidePair | None, list[str]]:
    prepared = await gather_measurements(
        [partial(_prepare_side, session, name) for name in _SIDES], session.budget
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


def raster_pixel_count(size: tuple[float, float]) -> int:
    """Pixels of a page of ``size`` points rendered at the comparison resolution."""
    return math.prod(measurement_raster_size(size, COMPARISON_DPI))


def pair_memory_mb(left: tuple[float, float], right: tuple[float, float]) -> int:
    """Memory to reserve for holding both renders of a page pair, in MiB."""
    pixels = raster_pixel_count(left) + raster_pixel_count(right)
    return math.ceil(_RGB_BYTES_PER_PIXEL * pixels / MIB) + _RENDER_HEADROOM_MB


@dataclass(frozen=True, slots=True)
class _PageRenders:
    """What every page-pair render of one comparison shares."""

    session: PdfToolSession
    dimensions: dict[str, list[tuple[float, float]]]
    policy: ComparisonPolicy
    count_changes: bool


async def _read_render(renders: _PageRenders, name: str, side: str, page: int) -> RasterImage:
    """Render one side of a page pair and read its raster, removing the file afterwards."""
    await renders.session.run_without_lease(
        f"{name}/{side}",
        render_page_argv(page, COMPARISON_DPI, "rgb", pdf_input(side)),
        memory_mb=_RENDER_HEADROOM_MB,
    )
    path = renders.session.work / name / side / "command" / raster_filename("rgb")
    image = read_pnm_raster(
        path, channels=3, max_pixels=raster_pixel_count(renders.dimensions[side][page - 1])
    )
    path.unlink()
    return image


async def _rendered_difference(renders: _PageRenders, page: int) -> PageDifference | None:
    session = renders.session
    name = f"page-{page:04d}"
    try:
        left = await _read_render(renders, name, "left", page)
        right = await _read_render(renders, name, "right", page)
        sizes_changed = (
            renders.dimensions["left"][page - 1] != renders.dimensions["right"][page - 1]
        )
        if left == right and not sizes_changed:
            return None
        difference = PageDifference(
            page, (left.width, left.height), (right.width, right.height), sizes_changed
        )
        if (
            not renders.count_changes
            or sizes_changed
            or difference.left_pixels != difference.right_pixels
        ):
            return difference
        changed = await run_in_thread(
            count_changed_pixels, left, right, renders.policy.channel_tolerance
        )
        ratio = changed / (left.width * left.height)
        return PageDifference(
            page,
            difference.left_pixels,
            difference.right_pixels,
            sizes_changed,
            changed_pixels=changed,
            changed_pixel_ratio=ratio,
            within_tolerance=ratio <= renders.policy.max_changed_pixel_ratio,
        )
    finally:
        # Failed and cancelled renderers can leave partial rasters. The runner
        # has reaped the process before returning control to this cleanup.
        pair_work = session.work / name
        if pair_work.exists():
            shutil.rmtree(pair_work)


async def _render_pair(renders: _PageRenders, page: int) -> PageDifference | None:
    memory_mb = pair_memory_mb(
        renders.dimensions["left"][page - 1], renders.dimensions["right"][page - 1]
    )
    async with renders.session.lease(
        f"compare PDF page {page}", memory_mb=memory_mb, kind="render"
    ):
        return await _rendered_difference(renders, page)


@dataclass(slots=True)
class _RenderOutcome:
    """Page-pair render results of one comparison, collected in page order."""

    differences: list[PageDifference]
    completed_pages: list[int]
    failed_pages: list[int]
    errors: list[str]

    @property
    def changed_pages(self) -> list[int]:
        return [item.page for item in self.differences if item.counts_as_changed]


async def _render_pages(renders: _PageRenders, pages: int) -> _RenderOutcome:
    results = await gather_measurements(
        [partial(_render_pair, renders, page) for page in range(1, pages + 1)],
        renders.session.budget,
    )
    outcome = _RenderOutcome([], [], [], [])
    for page, result in enumerate(results, 1):
        if isinstance(result, PreparationError):
            outcome.errors.append(f"Page {page} rendering: {result}")
            outcome.failed_pages.append(page)
        else:
            outcome.completed_pages.append(page)
            if result is not None:
                outcome.differences.append(result)
    return outcome


def _page_count_finding(pair: _SidePair, policy: ComparisonPolicy) -> Finding:
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
    """Pages whose normalized text differs (pages missing on one side included).

    None when the text of either side could not be extracted.
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
    """What comparing two prepared PDFs established, before it becomes findings."""

    findings: list[Finding]
    errors: list[str]
    text_changes: list[int] | None = None
    renders: _RenderOutcome | None = None
    render_evidence: dict[str, object] | None = None


async def _compare_prepared(
    session: PdfToolSession,
    pair: _SidePair,
    errors: list[str],
    policy: ComparisonPolicy,
    count_changes: bool,
) -> _Comparison:
    comparison = _Comparison([_page_count_finding(pair, policy)], errors)
    comparison.text_changes = _changed_text_pages(pair)
    if comparison.text_changes is not None:
        comparison.findings.append(_text_finding(comparison.text_changes, policy))
    if pair.left.dimensions is None or pair.right.dimensions is None:
        return comparison
    renders = _PageRenders(
        session,
        {"left": pair.left.dimensions, "right": pair.right.dimensions},
        policy,
        count_changes,
    )
    outcome = await _render_pages(renders, pair.common_pages)
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
    """Compare two PDFs by page count, text and render; capture previews if a collector is given."""
    policy = ComparisonPolicy.of(options)
    try:
        session = await PdfToolSession.open(work, {"left": left, "right": right}, runner, budget)
    except (PreparationError, OSError) as error:
        if previews is not None:
            previews.unavailable(
                "PDF comparison inputs were unavailable; no previews were captured."
            )
        return [_unavailable("compare.unavailable", error)]
    pair, errors = await _prepare_sides(session)
    comparison = _Comparison([], errors)
    if pair is not None:
        comparison = await _compare_prepared(session, pair, errors, policy, options is not None)
    findings, errors = comparison.findings, comparison.errors
    if errors:
        findings.append(
            _unavailable(
                "compare.unavailable",
                PreparationError("; ".join(errors)),
                details={**policy.to_details(), **(comparison.render_evidence or {})},
            )
        )
    if session.versions:
        findings.append(
            Finding(
                "compare.tools",
                "Comparison tool versions were recorded.",
                severity="info",
                status="passed",
                details={"tools": session.tool_versions()},
            )
        )
    if previews is not None:
        evidence = (
            None if pair is None else _ChangeEvidence.of(pair, comparison, incomplete=bool(errors))
        )
        await _capture_previews(session, pair, evidence, previews)
    return findings


class _PageSide(NamedTuple):
    """One side of a page pair: which page, which PDF and how large the page is in points."""

    page: int
    side: str
    size: tuple[float, float]


async def _capture_png(
    session: PdfToolSession, target: _PageSide, store: PdfPreviewStore, slot: int
) -> tuple[str, tuple[int, int]]:
    """Render one optional image without retaining any scratch-file references."""
    page, side, size = target
    name = f"preview-{page:04d}-{side}"
    async with session.lease(
        f"preview PDF page {page} {side}", memory_mb=pair_memory_mb(size, size), kind="render"
    ):
        try:
            maximum = min(session.runner.limits.max_file_bytes, store.remaining_bytes)
            if maximum < MIN_PNG_BYTES:
                raise PreviewLimitError("Insufficient PNG preview or tool file byte allowance")
            await session.run_without_lease(
                name,
                render_page_argv(page, COMPARISON_DPI, "png", pdf_input(side)),
                memory_mb=_RENDER_HEADROOM_MB,
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
            if pixels[0] * pixels[1] > raster_pixel_count(size):
                raise PreparationError("Preview PNG dimensions exceed the geometry reservation")
            return store.add_image(slot, side, data), pixels
        finally:
            preview_work = session.work / name
            if preview_work.exists():
                shutil.rmtree(preview_work)


@dataclass(frozen=True, slots=True)
class _ChangeEvidence:
    """What the comparison established about each page of a side pair."""

    # Pages whose text differs (None: text was not compared) and the rendering differences.
    text_changes: frozenset[int] | None
    differences: Mapping[int, PageDifference]
    completed: frozenset[int]
    missing_pages: frozenset[int]
    dimensions_changed: frozenset[int]
    incomplete: bool

    @classmethod
    def of(cls, pair: _SidePair, comparison: _Comparison, *, incomplete: bool) -> _ChangeEvidence:
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
        return sorted(
            (self.text_changes or frozenset())
            | self.differences.keys()
            | self.missing_pages
            | self.dimensions_changed
        )


def _preview_page(page: int, pair: _SidePair, evidence: _ChangeEvidence) -> PreviewPage:
    """A page's change summary with both of its sides pending capture."""
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
    session: PdfToolSession,
    pair: _SidePair,
    preview: PreviewPage,
    store: PdfPreviewStore,
    slot: int,
) -> None:
    for (name, descriptor), (_, side) in zip(preview.sides(), pair.named(), strict=True):
        if not descriptor.present:
            continue
        if side.dimensions is None:
            descriptor.status = "unavailable"
            descriptor.error = "Page dimensions were unavailable."
            continue
        try:
            target = _PageSide(preview.page, name, side.dimensions[preview.page - 1])
            image_id, pixels = await _capture_png(session, target, store, slot)
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
    """The status shared by the present sides of a page pair, else partial."""
    statuses = {side.status for _, side in preview.sides() if side.present}
    return next(iter(statuses)) if len(statuses) == 1 else "partial"


def _omit(preview: PreviewPage) -> None:
    preview.status = "omitted"
    preview.omission_reason = "The shared preview page or byte limit was reached."
    for _, side in preview.sides():
        if side.present:
            side.status = "omitted"


async def _capture_previews(
    session: PdfToolSession,
    pair: _SidePair | None,
    evidence: _ChangeEvidence | None,
    collector: PdfPreviewCollector,
) -> None:
    """Capture only identified differences; preview failures never change findings."""
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
            await _capture_page(session, pair, preview, store, slot)
            preview.status = _combined_status(preview)
        collector.finished = True
        if not affected and evidence.incomplete:
            collector.unavailable("Comparison evidence was incomplete; affected pages are unknown.")
