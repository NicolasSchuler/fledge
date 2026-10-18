"""MuPDF trace geometry: painted drawings with their placement, extent and active clip.

``mutool trace`` reports painted operations (images, paths, text) with affine
transforms, not semantic figure or table labels. This module turns a parsed trace into
``Drawing`` records whose boxes are in PDF points, and offers the geometry primitives
(points, matrices, boxes, straight path segments) the artwork measurements share.

Boxes use the trace's own frame; ``misaligned_frame_pages`` reports pages whose trace
frame disagrees with the page size that Poppler reported, so measurements can skip them.

Does not own: the findings built from drawings (``pdf_artwork_drawings``), which
operations a rule needs, or running the tool.
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, NamedTuple, Self

from .cancellation import cancellation_point
from .errors import PreparationError
from .pdf_check_options import PdfRegion
from .pdf_geometry import contains, finite_number
from .pdf_tool_output import COLOR_OPERATIONS, TracedDocument

# Painted operations whose extent is a transformed unit square (affine images).
IMAGE_OPERATIONS: Final = frozenset({"fill_image", "fill_image_mask"})
TEXT_OPERATIONS: Final = frozenset({"fill_text", "stroke_text"})
# Operations with a measurable painted extent: affine images and straight paths.
EXTENT_OPERATIONS: Final = frozenset({"fill_image", "fill_image_mask", "fill_path", "stroke_path"})
# Elements that carry no painting of their own and never make a page uncertain.
_PASSIVE_ELEMENTS: Final = frozenset(
    {"moveto", "lineto", "curveto", "closepath", "span", "g", "layer", "structure", "metatext"}
)
# Elements that nest or mask painting in ways the extents here cannot follow.
_UNCERTAIN_ELEMENTS: Final = frozenset({"group", "tile", "clip_mask"})
_MATRIX_VALUES: Final = 6
_BOX_VALUES: Final = 4
_UNIT_SQUARE_CORNERS: Final = ((0.0, 0.0), (1.0, 0.0), (0.0, 1.0), (1.0, 1.0))


class Point(NamedTuple):
    x: float
    y: float


class Matrix(NamedTuple):
    """A PDF affine transform ``[a b c d e f]`` mapping ``(x, y)`` to ``(ax+cy+e, bx+dy+f)``."""

    a: float
    b: float
    c: float
    d: float
    e: float
    f: float

    @classmethod
    def of_node(cls, node: ET.Element) -> Self:
        """The transform attribute of a trace element; raises when it is not a full matrix."""
        values = [finite_number(value) for value in node.get("transform", "").split()]
        if len(values) != _MATRIX_VALUES:
            raise PreparationError("Drawing operation has no complete affine transform")
        return cls(*values)

    @property
    def determinant(self) -> float:
        return self.a * self.d - self.b * self.c

    def apply(self, point: Point | tuple[float, float]) -> Point:
        x, y = point
        result = Point(self.a * x + self.c * y + self.e, self.b * x + self.d * y + self.f)
        if not all(math.isfinite(value) for value in result):
            raise PreparationError("Transformed drawing coordinate exceeds numerical limits")
        return result


class Box(NamedTuple):
    """An axis-aligned rectangle ``(left, top, right, bottom)`` in PDF points."""

    left: float
    top: float
    right: float
    bottom: float

    @classmethod
    def around(cls, points: Sequence[Point]) -> Self:
        if not points:
            raise PreparationError("Drawing path has no measured vertices")
        xs, ys = [point.x for point in points], [point.y for point in points]
        return cls(min(xs), min(ys), max(xs), max(ys))

    def clipped(self, clip: Box) -> Box:
        """The overlap with ``clip``; empty (see ``is_empty``) when they do not meet."""
        return Box(
            max(self.left, clip.left),
            max(self.top, clip.top),
            min(self.right, clip.right),
            min(self.bottom, clip.bottom),
        )

    @property
    def is_empty(self) -> bool:
        return self.left >= self.right or self.top >= self.bottom

    def contains(self, inner: tuple[float, ...], tolerance: float = 0) -> bool:
        """Whether ``inner`` lies inside this box, up to ``tolerance`` points."""
        return contains(self, inner, tolerance)


class Segment(NamedTuple):
    start: Point
    end: Point


class PathSegments(NamedTuple):
    """The straight segments of a path, and whether the path holds nothing but those."""

    segments: list[Segment]
    straight: bool


def path_segments(node: ET.Element) -> PathSegments:
    """Segments of a path made only of ``moveto``, ``lineto`` and ``closepath``.

    A curve or a line without a current point makes the path unsupported; the segments
    collected up to that point are returned with ``straight`` false.
    """
    result: list[Segment] = []
    start: Point | None = None
    previous: Point | None = None
    for child in node:
        if child.tag in {"moveto", "lineto"}:
            current = Point(finite_number(child.get("x")), finite_number(child.get("y")))
            if child.tag == "moveto":
                start = current
            elif previous is None:
                return PathSegments(result, False)
            else:
                result.append(Segment(previous, current))
            previous = current
        elif child.tag == "closepath" and previous is not None and start is not None:
            result.append(Segment(previous, start))
            previous = start
        else:
            return PathSegments(result, False)
    return PathSegments(result, bool(result))


def _path_box(node: ET.Element) -> Box | None:
    path = path_segments(node)
    if not path.straight:
        return None
    matrix = Matrix.of_node(node)
    return Box.around([matrix.apply(point) for segment in path.segments for point in segment])


def _rectangle(node: ET.Element) -> Box | None:
    """The box of a clip path that is an axis-aligned rectangle, else None."""
    path = path_segments(node)
    if (
        not path.straight
        or len(path.segments) != 4
        or path.segments[-1].end != path.segments[0].start
    ):
        return None
    matrix = Matrix.of_node(node)
    points = [matrix.apply(segment.start) for segment in path.segments]
    box = Box.around(points)
    corners = {
        (box.left, box.top),
        (box.right, box.top),
        (box.right, box.bottom),
        (box.left, box.bottom),
    }
    if set(points) != corners:
        return None
    for first, second in zip(points, points[1:] + points[:1], strict=True):
        if first.x != second.x and first.y != second.y:
            return None
    return box


@dataclass(frozen=True, slots=True)
class Drawing:
    """One painted operation: its page, its 1-based number on that page and its extent.

    ``box`` is the transformed extent of an image or straight filled path (None for
    anything else); ``clip`` is the active rectangular clip (None when the clip is not a
    supported rectangle); ``uncertain`` is set when the page already held content this
    module cannot follow, such as transparency groups.
    """

    page: int
    index: int
    node: ET.Element
    box: Box | None
    clip: Box | None
    uncertain: bool

    @property
    def kind(self) -> str:
        return self.node.tag


class DrawingTrace(NamedTuple):
    """Every painted operation of a document, its page frames and its uncertain pages."""

    drawings: list[Drawing]
    page_boxes: list[Box]
    trace_uncertain_pages: frozenset[int]


def _media_box(page: ET.Element) -> Box:
    values = [finite_number(value) for value in page.get("mediabox", "").split()]
    if len(values) != _BOX_VALUES or values[0] >= values[2] or values[1] >= values[3]:
        raise PreparationError("Trace page bounds are incomplete")
    return Box(*values)


def _painted_extent(node: ET.Element) -> Box | None:
    if node.tag in IMAGE_OPERATIONS:
        matrix = Matrix.of_node(node)
        return Box.around([matrix.apply(corner) for corner in _UNIT_SQUARE_CORNERS])
    if node.tag == "fill_path":
        return _path_box(node)
    return None


def _parse_page(number: int, page: ET.Element) -> tuple[list[Drawing], Box, bool]:
    """The drawings of one trace page, its frame, and whether the page is uncertain."""
    page_box = _media_box(page)
    drawings: list[Drawing] = []
    uncertain = False
    clips: list[Box | None] = [page_box]
    index = 0
    # Clip events are siblings in MuPDF XML. Transparency groups are nested.
    for node in page.iter():
        if node is page:
            continue
        if node.tag in _UNCERTAIN_ELEMENTS:
            uncertain = True
        elif node.tag.startswith("clip_"):
            clip = _rectangle(node) if node.tag == "clip_path" else None
            parent = clips[-1]
            clips.append(clip.clipped(parent) if clip is not None and parent is not None else None)
        elif node.tag == "pop_clip":
            if len(clips) == 1:
                raise PreparationError("Trace has an unmatched clipping restore")
            clips.pop()
        elif node.tag in COLOR_OPERATIONS or node.tag == "ignore_text":
            index += 1
            drawings.append(
                Drawing(number, index, node, _painted_extent(node), clips[-1], uncertain)
            )
        elif node.tag not in _PASSIVE_ELEMENTS:
            uncertain = True
    return drawings, page_box, uncertain or len(clips) != 1


def parse_drawing_trace(document: TracedDocument) -> DrawingTrace:
    """Drawings, page frames and uncertain pages of a validated MuPDF trace."""
    drawings: list[Drawing] = []
    page_boxes: list[Box] = []
    uncertain_pages: set[int] = set()
    for number, page in document.pages:
        cancellation_point()
        page_drawings, page_box, uncertain = _parse_page(number, page)
        drawings.extend(page_drawings)
        page_boxes.append(page_box)
        if uncertain:
            uncertain_pages.add(number)
    return DrawingTrace(drawings, page_boxes, frozenset(uncertain_pages))


def misaligned_frame_pages(
    page_boxes: Sequence[Box], sizes: Sequence[tuple[float, float]], tolerance: float
) -> frozenset[int]:
    """Pages whose trace frame is not the origin-anchored page Poppler reported.

    Measurements in the top-left frame are only meaningful when the trace's page box
    starts at the origin and matches the reported size within ``tolerance`` points.
    """
    return frozenset(
        number
        for number, (box, size) in enumerate(zip(page_boxes, sizes, strict=True), 1)
        if (box.left, box.top) != (0, 0)
        or any(
            abs(actual - expected) > tolerance
            for actual, expected in zip((box.right, box.bottom), size, strict=True)
        )
    )


@dataclass(frozen=True, slots=True)
class TracedArtwork:
    """Traced drawings, page frames and the pages their measurements must treat with care."""

    drawings: Sequence[Drawing]
    page_boxes: Sequence[Box]
    # Pages with trace content the extents here cannot follow (groups, masks, odd operators).
    trace_uncertain_pages: frozenset[int]
    # Pages whose coordinate frame cannot be aligned with the reported page size: rotated or
    # cropped pages, and pages whose trace frame disagrees with it.
    misaligned_frame_pages: frozenset[int]

    @property
    def unusable_pages(self) -> frozenset[int]:
        return self.trace_uncertain_pages | self.misaligned_frame_pages

    def drawings_by_page(self) -> dict[int, list[Drawing]]:
        """The drawings grouped by page, so per-region work never rescans every drawing."""
        pages: dict[int, list[Drawing]] = {}
        for drawing in self.drawings:
            pages.setdefault(drawing.page, []).append(drawing)
        return pages

    def region_is_measurable(self, region: PdfRegion) -> bool:
        """Whether the region lies on a traced page whose frame can be trusted."""
        return (
            region.page <= len(self.page_boxes)
            and region.page not in self.unusable_pages
            and self.page_boxes[region.page - 1].contains(region.box)
        )
