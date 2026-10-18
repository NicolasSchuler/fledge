"""Bounded rendered and extracted-text comparison of two PDFs, with optional previews."""

from __future__ import annotations

import math
import shutil
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, NamedTuple, TypeVar, cast

from .models import Finding, PreparationError
from .pdf_previews import (
    MIN_PNG_BYTES,
    PdfPreviewCollector,
    PdfPreviewStore,
    PreviewLimitError,
    png_dimensions,
)
from .pdf_support import (
    COMPARISON_DPI,
    TOOL_FAILURE_SUGGESTION,
    PdfToolSession,
    gather_measurements,
    layout_text_argv,
    normalized_text_pages,
    page_boxes_argv,
    page_count,
    parse_page_sizes,
    pdf_input,
    pdfinfo_fields,
    read_rgb_raster,
    require_comparison_raster_fit,
    require_rendered_pages,
    unavailable_finding,
)
from .runtime import CommandResult, CommandRunner
from .scheduler import ResourceBudget, cancellation_point, run_in_thread
from .workflow_options import ComparisonOptions

_T = TypeVar("_T")
_RENDER_HEADROOM_MB = 128
_PIXEL_CHUNK = 3 * 1024  # a whole number of RGB pixels
_SIDES = ("left", "right")


def _unavailable(rule: str, error: Exception, details: dict[str, object] | None = None) -> Finding:
    return unavailable_finding(rule, error, suggestion=TOOL_FAILURE_SUGGESTION, details=details)


def _render_argv(page: int, side: str, *output_options: str) -> list[str]:
    return [
        "pdftoppm",
        *output_options,
        "-r",
        str(COMPARISON_DPI),
        "-f",
        str(page),
        "-l",
        str(page),
        "-singlefile",
        pdf_input(side),
        "page",
    ]


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
    info = await session.run(f"{name}/properties", ["pdfinfo", document])
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


def _raster_pixel_count(size: tuple[float, float]) -> int:
    return math.ceil(size[0] * COMPARISON_DPI / 72) * math.ceil(size[1] * COMPARISON_DPI / 72)


def _pair_memory_mb(left: tuple[float, float], right: tuple[float, float]) -> int:
    pixels = _raster_pixel_count(left) + _raster_pixel_count(right)
    return math.ceil(3 * pixels / 1_048_576) + _RENDER_HEADROOM_MB


def _changed_pixels(left: bytes, right: bytes, channel_tolerance: int) -> int:
    count = 0
    for start in range(0, len(left), _PIXEL_CHUNK):
        cancellation_point()
        stop = min(len(left), start + _PIXEL_CHUNK)
        if left[start:stop] == right[start:stop]:
            # Identical bytes cannot exceed any tolerance; scan only differing chunks.
            continue
        for offset in range(start, stop, 3):
            if any(
                abs(left[offset + channel] - right[offset + channel]) > channel_tolerance
                for channel in range(3)
            ):
                count += 1
    return count


@dataclass(frozen=True, slots=True)
class _PageRenders:
    """What every page-pair render of one comparison shares."""

    session: PdfToolSession
    dimensions: dict[str, list[tuple[float, float]]]
    options: ComparisonOptions | None


async def _rendered_difference(renders: _PageRenders, page: int) -> dict[str, object] | None:
    session = renders.session
    name = f"page-{page:04d}"
    rasters: list[tuple[tuple[int, int], bytes]] = []
    try:
        for side in _SIDES:
            await session.run_without_lease(
                f"{name}/{side}", _render_argv(page, side), memory_mb=_RENDER_HEADROOM_MB
            )
            path = session.work / name / side / "command/page.ppm"
            rasters.append(
                read_rgb_raster(
                    path, max_pixels=_raster_pixel_count(renders.dimensions[side][page - 1])
                )
            )
            path.unlink()
        (size_left, pixels_left), (size_right, pixels_right) = rasters
        dimensions_changed = (
            renders.dimensions["left"][page - 1] != renders.dimensions["right"][page - 1]
        )
        if size_left == size_right and pixels_left == pixels_right and not dimensions_changed:
            return None
        measurement: dict[str, object] = {
            "page": page,
            "left_pixels": size_left,
            "right_pixels": size_right,
            "page_dimensions_changed": dimensions_changed,
        }
        options = renders.options
        if options is not None and size_left == size_right and not dimensions_changed:
            changed_pixels = await run_in_thread(
                _changed_pixels, pixels_left, pixels_right, options.channel_tolerance
            )
            ratio = changed_pixels / (size_left[0] * size_left[1])
            measurement.update(
                changed_pixels=changed_pixels,
                changed_pixel_ratio=ratio,
                within_tolerance=ratio <= options.max_changed_pixel_ratio,
            )
        return measurement
    finally:
        rasters.clear()
        # Failed and cancelled renderers can leave partial rasters. The runner
        # has reaped the process before returning control to this cleanup.
        pair_work = session.work / name
        if pair_work.exists():
            shutil.rmtree(pair_work)


async def _render_pair(renders: _PageRenders, page: int) -> dict[str, object] | None:
    memory_mb = _pair_memory_mb(
        renders.dimensions["left"][page - 1], renders.dimensions["right"][page - 1]
    )
    async with renders.session.lease(
        f"compare PDF page {page}", memory_mb=memory_mb, kind="render"
    ):
        return await _rendered_difference(renders, page)


@dataclass(slots=True)
class _RenderOutcome:
    """Page-pair render results of one comparison, collected in page order."""

    changed_rasters: list[dict[str, object]]
    completed_pages: list[int]
    failed_pages: list[int]
    errors: list[str]

    @property
    def changed_pages(self) -> list[object]:
        return [item["page"] for item in self.changed_rasters if not item.get("within_tolerance")]


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
                outcome.changed_rasters.append(result)
    return outcome


def _comparison_policy(options: ComparisonOptions | None) -> dict[str, object]:
    return {
        "dpi": COMPARISON_DPI,
        "pixel_tolerance": options.max_changed_pixel_ratio if options is not None else 0,
        "channel_tolerance": options.channel_tolerance if options is not None else 0,
        "text_normalization": "line endings and trailing horizontal whitespace only",
        "metadata_compared": False,
        "semantic_equivalence_proven": False,
    }


def _page_count_finding(pair: _SidePair, policy: dict[str, object]) -> Finding:
    page_counts = {name: side.pages for name, side in pair.named()}
    same_count = pair.left.pages == pair.right.pages
    return Finding(
        "compare.page_count",
        "Page counts match." if same_count else "Page counts changed.",
        severity="info" if same_count else "error",
        status="passed" if same_count else "failed",
        details={**policy, **page_counts},
    )


def _text_finding(pair: _SidePair, policy: dict[str, object]) -> Finding | None:
    left_text, right_text = pair.left.text, pair.right.text
    if left_text is None or right_text is None:
        return None
    changed_text = [
        page
        for page in range(1, pair.common_pages + 1)
        if left_text[page - 1] != right_text[page - 1]
    ] + pair.missing_pages
    return Finding(
        "compare.text",
        "Normalized extracted text matches."
        if not changed_text
        else f"Extracted text differs on {len(changed_text)} pages.",
        severity="error" if changed_text else "info",
        status="failed" if changed_text else "passed",
        details={**policy, "changed_pages": changed_text},
    )


def _rendering_finding(
    changed_pages: list[object],
    measurements: list[dict[str, object]],
    policy: dict[str, object],
    options: ComparisonOptions | None,
) -> Finding:
    tolerant = options is not None and bool(
        options.channel_tolerance or options.max_changed_pixel_ratio
    )
    if changed_pages:
        message = f"Rendered output differs on {len(changed_pages)} pages."
    elif tolerant:
        message = (
            f"Rendered pages match within the explicit pixel tolerances at {COMPARISON_DPI} DPI."
        )
    else:
        message = f"Rendered pages match exactly at {COMPARISON_DPI} DPI."
    return Finding(
        "compare.rendering",
        message,
        severity="error" if changed_pages else "info",
        status="failed" if changed_pages else "passed",
        details={**policy, "changed_pages": changed_pages, "measurements": measurements},
    )


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
    policy = _comparison_policy(options)
    try:
        session = await PdfToolSession.open(work, {"left": left, "right": right}, runner, budget)
    except (PreparationError, OSError) as error:
        if previews is not None:
            previews.unavailable(
                "PDF comparison inputs were unavailable; no previews were captured."
            )
        return [_unavailable("compare.unavailable", error)]
    pair, errors = await _prepare_sides(session)
    findings: list[Finding] = []
    render_evidence: dict[str, object] = {}
    if pair is not None:
        findings.append(_page_count_finding(pair, policy))
        if (text := _text_finding(pair, policy)) is not None:
            findings.append(text)
        if pair.left.dimensions is not None and pair.right.dimensions is not None:
            renders = _PageRenders(
                session, {"left": pair.left.dimensions, "right": pair.right.dimensions}, options
            )
            outcome = await _render_pages(renders, pair.common_pages)
            errors.extend(outcome.errors)
            changed_pages = outcome.changed_pages + pair.missing_pages
            if outcome.failed_pages:
                render_evidence = {
                    "completed_pages": outcome.completed_pages,
                    "unavailable_pages": outcome.failed_pages,
                    "known_changed_pages": changed_pages,
                    "measurements": outcome.changed_rasters,
                }
            else:
                findings.append(
                    _rendering_finding(changed_pages, outcome.changed_rasters, policy, options)
                )
    if errors:
        findings.append(
            _unavailable(
                "compare.unavailable",
                PreparationError("; ".join(errors)),
                details={**policy, **render_evidence},
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
        await _capture_previews(session, pair, findings, previews)
    return findings


async def _capture_png(
    session: PdfToolSession,
    page: int,
    side: str,
    size: tuple[float, float],
    store: PdfPreviewStore,
    slot: int,
) -> tuple[str, tuple[int, int]]:
    """Render one optional image without retaining any scratch-file references."""
    name = f"preview-{page:04d}-{side}"
    async with session.lease(
        f"preview PDF page {page} {side}", memory_mb=_pair_memory_mb(size, size), kind="render"
    ):
        try:
            maximum = min(session.runner.limits.max_file_bytes, store.remaining_bytes)
            if maximum < MIN_PNG_BYTES:
                raise PreviewLimitError("Insufficient PNG preview or tool file byte allowance")
            await session.run_without_lease(
                name, _render_argv(page, side, "-png"), memory_mb=_RENDER_HEADROOM_MB
            )
            path = session.work / name / "command/page.png"
            if not path.is_file() or path.is_symlink():
                raise PreparationError("Preview renderer did not produce a regular PNG image")
            if path.stat().st_size > maximum:
                raise PreviewLimitError("PNG image exceeds the preview or tool file byte limit")
            with path.open("rb") as stream:
                data = stream.read(maximum + 1)
            if len(data) > maximum:
                raise PreviewLimitError("PNG image exceeds the preview or tool file byte limit")
            pixels = png_dimensions(data)
            if pixels[0] * pixels[1] > _raster_pixel_count(size):
                raise PreparationError("Preview PNG dimensions exceed the geometry reservation")
            return store.add_image(slot, side, data), pixels
        finally:
            preview_work = session.work / name
            if preview_work.exists():
                shutil.rmtree(preview_work)


@dataclass(frozen=True, slots=True)
class _ChangeEvidence:
    """What the comparison findings established about each page of a side pair."""

    text_compared: bool
    text_changes: set[int]
    measurements: dict[int, dict[str, Any]]
    completed: set[int]
    missing_pages: set[int]
    dimensions_changed: set[int]

    @property
    def affected_pages(self) -> list[int]:
        return sorted(
            self.text_changes
            | self.measurements.keys()
            | self.missing_pages
            | self.dimensions_changed
        )


def _change_evidence(pair: _SidePair, by_rule: dict[str, Finding]) -> _ChangeEvidence:
    text_finding = by_rule.get("compare.text")
    common = pair.common_pages
    rendering = by_rule.get("compare.rendering") or by_rule.get("compare.unavailable")
    if rendering is None:
        measurements: dict[int, dict[str, Any]] = {}
        completed: set[int] = set()
    else:
        measurements = {
            item["page"]: item
            for item in cast(list[dict[str, Any]], rendering.details.get("measurements", []))
        }
        completed = (
            set(range(1, common + 1))
            if "compare.rendering" in by_rule
            else set(cast(list[int], rendering.details.get("completed_pages", [])))
        )
    left, right = pair.left.dimensions, pair.right.dimensions
    return _ChangeEvidence(
        text_compared=text_finding is not None,
        text_changes=(
            set(cast(list[int], text_finding.details["changed_pages"]))
            if text_finding is not None
            else set()
        ),
        measurements=measurements,
        completed=completed,
        missing_pages=set(pair.missing_pages),
        dimensions_changed={
            page
            for page in range(1, common + 1)
            if left is not None and right is not None and left[page - 1] != right[page - 1]
        },
    )


def _page_descriptor(
    page: int, pair: _SidePair, evidence: _ChangeEvidence
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """A page's change summary and its per-side capture descriptors, both pending."""
    sides: dict[str, dict[str, Any]] = {}
    for name, side in pair.named():
        present = page <= side.pages
        sides[name] = {
            "present": present,
            "dimensions_pt": list(side.dimensions[page - 1])
            if present and side.dimensions is not None
            else None,
            "image_id": None,
            "status": "pending" if present else "missing",
        }
    measured = page in evidence.measurements
    missing = page in evidence.missing_pages
    descriptor: dict[str, Any] = {
        "page": page,
        "text_changed": page in evidence.text_changes if evidence.text_compared else None,
        "rendering_changed": measured or missing if page in evidence.completed or missing else None,
        "text_only": page in evidence.text_changes
        and page in evidence.completed
        and not measured
        and page not in evidence.dimensions_changed,
        "page_dimensions_changed": page in evidence.dimensions_changed
        if page <= pair.common_pages and pair.dimensions_known
        else None,
        "page_count_changed": missing,
        "within_render_tolerance": evidence.measurements.get(page, {}).get("within_tolerance"),
        "status": "pending",
        **sides,
    }
    return descriptor, sides


async def _capture_page(
    session: PdfToolSession,
    pair: _SidePair,
    page: int,
    sides: dict[str, dict[str, Any]],
    store: PdfPreviewStore,
    slot: int,
) -> None:
    for name, side in pair.named():
        side_descriptor = sides[name]
        if not side_descriptor["present"]:
            continue
        if side.dimensions is None:
            side_descriptor.update(status="unavailable", error="Page dimensions were unavailable.")
            continue
        try:
            image_id, pixels = await _capture_png(
                session, page, name, side.dimensions[page - 1], store, slot
            )
            side_descriptor.update(status="captured", image_id=image_id, pixels=list(pixels))
        except PreviewLimitError:
            side_descriptor.update(
                status="omitted",
                error="The PNG preview or tool file byte limit was reached.",
            )
        except (PreparationError, OSError):
            # Raw tool diagnostics can name scratch files. Keep only a
            # safe, actionable capture status in the descriptor store.
            side_descriptor.update(
                status="unavailable", error="The PNG preview renderer was unavailable."
            )


async def _capture_previews(
    session: PdfToolSession,
    pair: _SidePair | None,
    findings: list[Finding],
    collector: PdfPreviewCollector,
) -> None:
    """Capture only identified differences; preview failures never change findings."""
    if pair is None:
        collector.unavailable(
            "PDF comparison preflight was unavailable; affected pages are unknown."
        )
        return
    by_rule = {finding.rule: finding for finding in findings}
    evidence = _change_evidence(pair, by_rule)
    affected = evidence.affected_pages
    store = collector.store
    async with store.exclusive():
        for page in affected:
            descriptor, sides = _page_descriptor(page, pair, evidence)
            collector.pages.append(descriptor)
            slot = store.reserve_page()
            if slot is None:
                descriptor["status"] = "omitted"
                descriptor["omission_reason"] = "The shared preview page or byte limit was reached."
                for side_descriptor in sides.values():
                    if side_descriptor["present"]:
                        side_descriptor["status"] = "omitted"
                continue
            await _capture_page(session, pair, page, sides, store, slot)
            statuses = {
                side_descriptor["status"]
                for side_descriptor in sides.values()
                if side_descriptor["present"]
            }
            descriptor["status"] = next(iter(statuses)) if len(statuses) == 1 else "partial"
        collector.finished = True
        if not affected and "compare.unavailable" in by_rule:
            collector.unavailable("Comparison evidence was incomplete; affected pages are unknown.")
