"""The colour space of raster figures against the family a venue requires (``PDF313``).

Opt-in ``required_color_space`` (``rgb`` or ``cmyk``) compares the colour type in each
literal PNG header and the component count in each literal JPEG header with that
family; grayscale satisfies either. PDF figures are measured on the built document by
the artwork trace, and EPS or PostScript figures are vector formats with no raster
colour space to read, so both are listed as not applicable. A figure whose header
cannot be read leaves the check inconclusive rather than passing.

User-facing prose says "colour", as the other figure checks do. The setting, the rule
name and the details keys spell it "color"; they are stable identifiers.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import NamedTuple

from .cancellation import cancellation_point
from .file_safety import read_file_prefix
from .file_scan import MAX_SCAN_MATCHES
from .finding_text import list_locations
from .finding_weight import DIRECT_POLICY
from .findings import Finding, Status
from .limits import MIB
from .png_support import read_png_header
from .source_graph import SourceGraph

FIGURE_COLOR_SPACES = ("rgb", "cmyk")
_RULE = "figure.color_space"
# Figures named in a message; the details list every one.
_MAX_LISTED_FIGURES = 3
# Enough of a file to hold a PNG header, or the JPEG segments before its frame header.
_MAX_IMAGE_HEADER_BYTES = 4 * MIB
# Vector figures have no raster colour space to read from a header. PDF figures are
# measured on the built document; EPS and PostScript are not measured at all.
_VECTOR_FIGURE_SUFFIXES = {".pdf", ".eps", ".ps"}
_RASTER_FIGURE_SUFFIXES = {".png", ".jpg", ".jpeg"}
# PNG IHDR colour types: gray, truecolour, palette, gray with alpha, truecolour with alpha.
_PNG_COLOR_FAMILIES = {0: "gray", 2: "rgb", 3: "rgb", 4: "gray", 6: "rgb"}
# JPEG frame component counts: one gray channel, three (YCbCr or RGB), four (CMYK or YCCK).
_JPEG_COLOR_FAMILIES = {1: "gray", 3: "rgb", 4: "cmyk"}

_JPEG_SIGNATURE = b"\xff\xd8"
_JPEG_MARKER_PREFIX = 0xFF
_JPEG_MARKER_BYTES = 2  # the 0xFF prefix and the marker code
_JPEG_LENGTH_BYTES = 2  # a segment's length counts these two bytes as well as its payload
_JPEG_SEGMENT_HEADER_BYTES = _JPEG_MARKER_BYTES + _JPEG_LENGTH_BYTES
# Markers that stand alone, without a length: TEM and the restart markers RST0 to RST7.
_JPEG_STANDALONE_MARKERS = frozenset({0x01, *range(0xD0, 0xD8)})
# Start-of-frame markers SOF0 to SOF15, except the table and arithmetic-coding markers
# DHT (0xC4), JPG (0xC8) and DAC (0xCC) that share that range.
_JPEG_FRAME_MARKERS = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}
_JPEG_ADOBE_MARKER = 0xEE  # APP14, which Adobe writers use to record the colour transform
_JPEG_END_MARKERS = frozenset({0xD9, 0xDA})  # end of image, start of scan: no frame follows
_ADOBE_SIGNATURE = b"Adobe"
_ADOBE_TRANSFORM_OFFSET = 11  # in the APP14 payload: "Adobe", version, two flag words, transform
_ADOBE_PAYLOAD_BYTES = 12
_FRAME_COMPONENTS_OFFSET = 5  # in a frame payload: precision, height, width, component count
_FRAME_PAYLOAD_BYTES = 6


class JpegColor(NamedTuple):
    """The colour family of a JPEG frame and the Adobe transform flag, when either is known."""

    family: str | None
    adobe_transform: int | None


class FigureColor(NamedTuple):
    """The measured colour family of one raster figure."""

    path: str
    color_space: str
    adobe_transform: int | None = None

    def as_dict(self) -> dict[str, object]:
        """The measurement as report details."""
        record: dict[str, object] = {"path": self.path, "color_space": self.color_space}
        if self.adobe_transform is not None:
            record["adobe_transform"] = self.adobe_transform
        return record


def png_color_family(data: bytes) -> str | None:
    """``gray``, ``rgb`` or ``None`` from the IHDR colour type at the start of a PNG."""
    header = read_png_header(data)
    return None if header is None else _PNG_COLOR_FAMILIES.get(header.color_type)


def jpeg_color_family(data: bytes) -> JpegColor:
    """The colour family from the frame's component count, and the Adobe APP14 transform.

    This walker reads a bounded file prefix and stops quietly at the first segment
    it cannot follow. The image-metadata scan in ``metadata_privacy`` walks whole
    files strictly and names each structural defect, so the two stay separate.
    """
    if not data.startswith(_JPEG_SIGNATURE):
        return JpegColor(None, None)
    cursor = len(_JPEG_SIGNATURE)
    transform = None
    while cursor + _JPEG_SEGMENT_HEADER_BYTES <= len(data):
        if data[cursor] != _JPEG_MARKER_PREFIX:
            break
        marker = data[cursor + 1]
        if marker == _JPEG_MARKER_PREFIX:  # fill byte before the next marker
            cursor += 1
            continue
        if marker in _JPEG_STANDALONE_MARKERS:
            cursor += _JPEG_MARKER_BYTES
            continue
        length = int.from_bytes(
            data[cursor + _JPEG_MARKER_BYTES : cursor + _JPEG_SEGMENT_HEADER_BYTES], "big"
        )
        payload = data[cursor + _JPEG_SEGMENT_HEADER_BYTES : cursor + _JPEG_MARKER_BYTES + length]
        if length < _JPEG_LENGTH_BYTES or len(payload) != length - _JPEG_LENGTH_BYTES:
            break
        if (
            marker == _JPEG_ADOBE_MARKER
            and payload.startswith(_ADOBE_SIGNATURE)
            and len(payload) >= _ADOBE_PAYLOAD_BYTES
        ):
            transform = payload[_ADOBE_TRANSFORM_OFFSET]
        elif marker in _JPEG_FRAME_MARKERS and len(payload) >= _FRAME_PAYLOAD_BYTES:
            components = payload[_FRAME_COMPONENTS_OFFSET]
            return JpegColor(_JPEG_COLOR_FAMILIES.get(components), transform)
        elif marker in _JPEG_END_MARKERS:
            break
        cursor += _JPEG_MARKER_BYTES + length
    return JpegColor(None, transform)


def _measure(root: Path, name: str, suffix: str) -> FigureColor | str:
    """The colour record of one raster figure, or the reason it cannot be read."""
    try:
        data = read_file_prefix(root / name, _MAX_IMAGE_HEADER_BYTES)
    except OSError:
        return f"{name}: figure is unreadable or linked"
    transform = None
    if suffix == ".png":
        space = png_color_family(data)
    else:
        space, transform = jpeg_color_family(data)
    if space is None:
        return f"{name}: no supported colour header was found"
    return FigureColor(name, space, transform)


def _finding(
    status: Status,
    message: str,
    path: str,
    details: dict[str, object],
) -> Finding:
    return Finding(
        _RULE,
        message,
        severity=DIRECT_POLICY.severity(status),
        status=status,
        path=path,
        evidence_kind=DIRECT_POLICY.evidence_kind,
        details=details,
    )


def color_space_finding(
    root: Path, graph: SourceGraph, figures: list[str], required: str, main: str
) -> Finding:
    """Compare each literal raster figure's colour family with ``required``."""
    violations: list[FigureColor] = []
    measured: list[FigureColor] = []
    vector: list[str] = []
    uncertainty: list[str] = []
    if graph.uncertainties:
        uncertainty.append("the literal figure graph has unresolved or uncertain inputs")
    for name in figures:
        cancellation_point()
        suffix = PurePosixPath(name).suffix.lower()
        if suffix in _VECTOR_FIGURE_SUFFIXES:
            vector.append(name)
            continue
        if suffix not in _RASTER_FIGURE_SUFFIXES:
            uncertainty.append(
                f"{name}: the colour space of {suffix or 'an extensionless'} figure file "
                "cannot be read here; check it manually or use a PNG or JPEG file"
            )
            continue
        outcome = _measure(root, name, suffix)
        if isinstance(outcome, str):
            uncertainty.append(outcome)
            continue
        measured.append(outcome)
        if outcome.color_space not in {"gray", required}:
            violations.append(outcome)
    details: dict[str, object] = {
        "required": required,
        "measured": [record.as_dict() for record in measured[:MAX_SCAN_MATCHES]],
        "violations": [record.as_dict() for record in violations[:MAX_SCAN_MATCHES]],
        "uncertainty": uncertainty[:MAX_SCAN_MATCHES],
        "not_applicable_vector_figures": vector[:MAX_SCAN_MATCHES],
        "scope": "PNG IHDR colour type and JPEG frame component count (Adobe APP14 recorded) "
        "of literal raster figures; grayscale satisfies either policy. PDF figures are "
        "measured separately by the included_figure_artwork input trace and EPS or PS "
        "figures are vector formats that are not measured; embedded ICC profiles are not "
        "interpreted.",
    }
    if violations:
        places = list_locations([record.as_dict() for record in violations], _MAX_LISTED_FIGURES)
        return _finding(
            "failed",
            f"Raster figures that are not {required.upper()} or grayscale "
            f"({len(violations)}): {places}.",
            violations[0].path if len(violations) == 1 else main,
            details,
        )
    if uncertainty:
        return _finding(
            "inconclusive",
            f"Raster figure colour spaces could not all be checked: {uncertainty[0]}.",
            main,
            details,
        )
    message = (
        f"Raster figures that are {required.upper()} or grayscale: {len(measured)}."
        if measured
        else "No raster figures to check; PDF, EPS and PS figures are not read for colour here."
    )
    return _finding("passed", message, main, details)
