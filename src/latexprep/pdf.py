"""Bounded PDF inspection and exact rendered/text comparison using Poppler."""

from __future__ import annotations

import math
import re
import shutil
from collections.abc import Awaitable, Callable, Sequence
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TypeVar

from .models import Finding, PreparationError
from .runtime import CommandResult, ToolRunner
from .scheduler import ResourceBudget, bounded_map, cancellation_point, run_in_thread
from .workflow_options import ComparisonOptions

_MAX_INSPECTION_PAGES = 5000
_MAX_RENDERED_PAGES = 300
_DPI = 144
_MAX_PIXELS = 16_000_000
_MAX_DIMENSION = 8192
_RENDER_HEADROOM_MB = 128
_PIXEL_CHUNK = 3 * 1024  # a whole number of RGB pixels
_Result = TypeVar("_Result")


async def _new_workspace(
    work: Path, documents: dict[str, Path], runner: ToolRunner, budget: ResourceBudget | None
) -> tuple[Path, dict[str, Path]]:
    async with (
        budget.lease(f"{work.name}: freeze PDF inputs", memory_mb=64)
        if budget is not None
        else nullcontext()
    ):
        if work.exists():
            raise PreparationError("PDF checks require a new, separate workspace")
        for document in documents.values():
            if not document.is_file() or document.is_symlink():
                raise PreparationError("PDF check input must be a regular, non-symlink file")
            if document.stat().st_size > runner.limits.max_file_bytes:
                raise PreparationError("PDF input exceeds the configured file size limit")
        work.mkdir(parents=True)
        work = work.resolve()
        frozen: dict[str, Path] = {}
        for name, document in documents.items():
            frozen[name] = runner.freeze_input(document, work / "inputs")
            with frozen[name].open("rb") as stream:
                if stream.read(5) != b"%PDF-":
                    raise PreparationError("PDF input has no PDF header")
        return work, frozen


@dataclass
class _PdfWork:
    work: Path
    documents: dict[str, Path]
    runner: ToolRunner
    budget: ResourceBudget | None
    versions: dict[str, object] = field(default_factory=dict)

    async def run(
        self, name: str, argv: list[str], *, leased: bool = False, memory_mb: int | None = None
    ) -> CommandResult:
        """Give each probe and command its own writable tool-state directory."""
        inputs = tuple(
            dict.fromkeys(self.documents[arg[2:]] for arg in argv if arg.startswith("./"))
        )
        argv = [str(self.documents[arg[2:]]) if arg.startswith("./") else arg for arg in argv]

        async def invoke() -> CommandResult:
            leaf = self.work / name
            version_work, command_work = leaf / "version", leaf / "command"
            version_work.mkdir(parents=True)
            command_work.mkdir()
            version = await self.runner.tool_version([argv[0], "-v"], version_work)
            self.versions[argv[0]] = deepcopy(version)
            result = await self.runner.run(
                argv,
                cwd=command_work,
                workspace=command_work,
                readonly_inputs=inputs,
                memory_mb=memory_mb,
            )
            _require_complete(result, argv[0])
            return result

        if self.budget is None or leased:
            return await invoke()
        async with self.budget.lease(f"{self.work.name}: PDF {name}", memory_mb=64):
            return await invoke()

    def tools(self) -> dict[str, object]:
        return {name: deepcopy(self.versions[name]) for name in sorted(self.versions)}


async def _measurements(
    operations: Sequence[Callable[[], Awaitable[_Result]]], budget: ResourceBudget | None
) -> list[_Result | PreparationError | OSError]:
    """Keep completed evidence when a sibling fails, and drain on cancellation."""

    async def measure(
        operation: Callable[[], Awaitable[_Result]],
    ) -> _Result | PreparationError | OSError:
        try:
            return await operation()
        except (PreparationError, OSError) as error:
            # Exception tracebacks can otherwise retain the failed worker's
            # large raster buffers until the complete comparison is collected.
            return PreparationError(str(error))

    return await bounded_map(operations, measure, limit=budget.jobs if budget is not None else 1)


def _completed(result: _Result | PreparationError | OSError) -> _Result:
    if isinstance(result, (PreparationError, OSError)):
        raise result
    return result


def _require_complete(result: CommandResult, tool: str) -> None:
    if result.timed_out:
        raise PreparationError(f"{tool} exceeded the configured time limit")
    if result.output_limited:
        raise PreparationError(f"{tool} exceeded the diagnostic output limit")
    if result.resource_exceeded:
        raise PreparationError(result.resource_exceeded)
    if result.returncode:
        raise PreparationError(f"{tool} failed ({result.returncode}): {result.stderr[:1000]}")
    if re.search(r"(?:Syntax|Internal|Fontconfig) Error:|Error:", result.stderr, re.IGNORECASE):
        raise PreparationError(
            f"{tool} reported damaged or incomplete input: {result.stderr[:1000]}"
        )


def _fields(output: str) -> dict[str, str]:
    return {
        key.strip(): value.strip()
        for key, value in (line.split(":", 1) for line in output.splitlines() if ":" in line)
    }


def _page_count(fields: dict[str, str], maximum: int = _MAX_INSPECTION_PAGES) -> int:
    try:
        pages = int(fields["Pages"])
    except (KeyError, ValueError) as error:
        raise PreparationError("pdfinfo did not return a valid page count") from error
    if pages < 1 or pages > maximum:
        raise PreparationError(f"PDF has {pages} pages; this implementation supports 1–{maximum}")
    return pages


def _require_rendered_pages(pages: int, subject: str = "Rendered comparison") -> None:
    """Rendering costs one process and raster per page, so it keeps a smaller cap."""
    if pages > _MAX_RENDERED_PAGES:
        raise PreparationError(
            f"{subject} supports at most {_MAX_RENDERED_PAGES} pages; this PDF has {pages}"
        )


def _is_type3(font_type: object) -> bool:
    """Shared Type 3 test for pdffonts type cells; spacing and case are not significant."""
    return re.fullmatch(r"Type\s*3", str(font_type), re.IGNORECASE) is not None


def _page_sizes(output: str, pages: int) -> list[tuple[float, float]]:
    sizes: dict[int, tuple[float, float]] = {}
    for match in re.finditer(
        r"^Page\s+(?:(\d+)\s+)?size:\s*([\d.eE+-]+)\s+x\s+([\d.eE+-]+)\s+pts",
        output,
        re.MULTILINE,
    ):
        index = int(match[1]) if match[1] else 1
        if index in sizes:
            raise PreparationError("pdfinfo returned duplicate page dimension rows")
        try:
            sizes[index] = (float(match[2]), float(match[3]))
        except ValueError as error:
            raise PreparationError("pdfinfo returned invalid page dimensions") from error
    if set(sizes) != set(range(1, pages + 1)):
        raise PreparationError("PDF page dimensions were not available for every page")
    for width, height in sizes.values():
        if not math.isfinite(width) or not math.isfinite(height) or min(width, height) <= 0:
            raise PreparationError("PDF contains invalid page dimensions")
        if max(width, height) > _MAX_DIMENSION * 72 / _DPI:
            raise PreparationError("PDF page exceeds safe raster dimensions at 144 DPI")
        pixels = (math.ceil(width * _DPI / 72), math.ceil(height * _DPI / 72))
        if max(pixels) > _MAX_DIMENSION or math.prod(pixels) > _MAX_PIXELS:
            raise PreparationError("PDF page exceeds safe raster dimensions at 144 DPI")
    return [sizes[page] for page in range(1, pages + 1)]


def _unavailable(rule: str, error: Exception) -> Finding:
    return Finding(
        rule,
        str(error),
        "error",
        "inconclusive",
        suggestion="Resolve the tool, resource, or isolation failure and repeat this check.",
    )


@dataclass(frozen=True)
class PdfOptions:
    expected_page_width_pt: float | None = None
    expected_page_height_pt: float | None = None
    page_size_tolerance_pt: float = 1.0
    require_consistent_page_size: bool = False
    min_image_dpi: float | None = None
    require_embedded_fonts: bool = False
    forbid_type3_fonts: bool = False
    forbid_encryption: bool = False
    forbid_forms: bool = False
    forbid_javascript: bool = False
    forbid_attachments: bool = False

    def __post_init__(self) -> None:
        for value in (
            self.require_consistent_page_size,
            self.require_embedded_fonts,
            self.forbid_type3_fonts,
            self.forbid_encryption,
            self.forbid_forms,
            self.forbid_javascript,
            self.forbid_attachments,
        ):
            if type(value) is not bool:
                raise PreparationError("PDF policy switches must be booleans")
        if (self.expected_page_width_pt is None) != (self.expected_page_height_pt is None):
            raise PreparationError("Expected PDF width and height must be supplied together")
        for value in (
            self.expected_page_width_pt,
            self.expected_page_height_pt,
            self.min_image_dpi,
        ):
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise PreparationError(
                    "PDF dimensions and image resolution must be positive finite numbers"
                )
        if (
            isinstance(self.page_size_tolerance_pt, bool)
            or not isinstance(self.page_size_tolerance_pt, (int, float))
            or not math.isfinite(self.page_size_tolerance_pt)
            or self.page_size_tolerance_pt < 0
        ):
            raise PreparationError("PDF page-size tolerance must be a nonnegative finite number")


def _configured_pdf_rules(options: PdfOptions, max_pages: int | None) -> list[str]:
    values = {
        "pdf.page_limit": max_pages is not None,
        "pdf.page_dimensions": options.expected_page_width_pt is not None,
        "pdf.page_size_consistency": options.require_consistent_page_size,
        "pdf.image_resolution": options.min_image_dpi is not None,
        "pdf.font_embedding": options.require_embedded_fonts,
        "pdf.type3_fonts": options.forbid_type3_fonts,
        "pdf.encryption_policy": options.forbid_encryption,
        "pdf.forms_policy": options.forbid_forms,
        "pdf.javascript_policy": options.forbid_javascript,
        "pdf.attachments_policy": options.forbid_attachments,
    }
    return [rule for rule, enabled in values.items() if enabled]


def _constraint(rule: str, message: str, violated: bool, details: dict[str, object]) -> Finding:
    suggestions = {
        "pdf.page_limit": "Review the total page budget, including references and appendices.",
        "pdf.page_dimensions": "Check the configured paper size and PDF page boxes.",
        "pdf.page_size_consistency": "Use a consistent page size, including inserted PDF pages.",
        "pdf.image_resolution": "Use higher-resolution inputs or reduce the rendered raster size.",
        "pdf.font_embedding": "Embed the listed font resources when generating the PDF or figures.",
        "pdf.type3_fonts": "Replace the listed Type 3 font resources with an allowed font type.",
        "pdf.encryption_policy": "Export an unencrypted PDF when this policy is required.",
        "pdf.forms_policy": "Remove form fields from a separate submission PDF copy.",
        "pdf.javascript_policy": "Remove JavaScript from a separate submission PDF copy.",
        "pdf.attachments_policy": "Remove embedded files from a separate submission PDF copy.",
    }
    return Finding(
        rule,
        message,
        "error" if violated else "info",
        "failed" if violated else "passed",
        evidence="derived",
        details=details,
        suggestion=suggestions.get(rule) if violated else None,
    )


def _table_rows(output: str, header: Sequence[str], tool: str) -> list[str]:
    lines = output.splitlines()
    if len(lines) < 2 or lines[0].split() != header or re.fullmatch(r"[-\s]+", lines[1]) is None:
        raise PreparationError(f"{tool} did not return a recognized complete inventory header")
    return [line for line in lines[2:] if line.strip()]


def _font_inventory(output: str) -> list[dict[str, object]]:
    inventory: list[dict[str, object]] = []
    for line in _table_rows(output, "name type encoding emb sub uni object ID".split(), "pdffonts"):
        match = re.fullmatch(
            r"(\S+)\s+(.+?)\s+(\S+)\s+(yes|no)\s+(yes|no)\s+(yes|no)\s+(\d+)\s+(\d+)\s*", line
        )
        if not match:
            raise PreparationError("pdffonts returned an unrecognized inventory row")
        inventory.append(
            {
                "name": match[1],
                "type": match[2],
                "encoding": match[3],
                "embedded": match[4] == "yes",
                "subset": match[5] == "yes",
                "unicode_map": match[6] == "yes",
                "object": int(match[7]),
                "generation": int(match[8]),
            }
        )
    return inventory


def _image_inventory(output: str, pages: int) -> list[dict[str, object]]:
    inventory: list[dict[str, object]] = []
    header = (
        "page num type width height color comp bpc enc interp object ID x-ppi y-ppi size ratio"
    ).split()
    for line in _table_rows(output, header, "pdfimages"):
        values = line.split()
        if len(values) != 16:
            raise PreparationError("pdfimages returned an unrecognized image inventory row")
        try:
            page, number, width, height = (
                int(values[0]),
                int(values[1]),
                int(values[3]),
                int(values[4]),
            )
            x_dpi, y_dpi = float(values[12]), float(values[13])
        except ValueError as error:
            raise PreparationError("pdfimages returned invalid image measurements") from error
        if (
            page not in range(1, pages + 1)
            or number < 0
            or min(width, height) < 1
            or values[2] not in {"image", "mask", "smask"}
            or not all(math.isfinite(dpi) and dpi >= 0 for dpi in (x_dpi, y_dpi))
        ):
            raise PreparationError("pdfimages returned invalid image measurements")
        inventory.append(
            {
                "page": page,
                "number": number,
                "type": values[2],
                "width_px": width,
                "height_px": height,
                "x_dpi": x_dpi,
                "y_dpi": y_dpi,
            }
        )
    return inventory


def _feature_presence(name: str, value: str | None) -> bool | None:
    if value is None:
        return None
    value = value.lower()
    if name == "Form":
        return False if value == "none" else True if value in {"acroform", "xfa"} else None
    return False if value == "no" else True if re.match(r"yes(?:\s|$)", value) else None


async def inspect_pdf(
    pdf: Path,
    work: Path,
    runner: ToolRunner,
    max_pages: int | None = None,
    *,
    options: PdfOptions | None = None,
    budget: ResourceBudget | None = None,
) -> list[Finding]:
    """Measure a PDF and evaluate only explicitly configured acceptance constraints."""
    options = options or PdfOptions()
    findings: list[Finding] = []
    try:
        work, documents = await _new_workspace(work, {"document.pdf": pdf}, runner, budget)
        session = _PdfWork(work, documents, runner, budget)
        info = await session.run("properties", ["pdfinfo", "-isodates", "./document.pdf"])
        fields = _fields(info.stdout)
        pages = _page_count(fields)
    except (PreparationError, OSError) as error:
        return [
            _unavailable(rule, error)
            for rule in ["pdf.inspection_unavailable", *_configured_pdf_rules(options, max_pages)]
        ]
    operations = {
        "geometry": ["pdfinfo", "-box", "-f", "1", "-l", str(pages), "./document.pdf"],
        "fonts": ["pdffonts", "./document.pdf"],
        "text": ["pdftotext", "-enc", "UTF-8", "-layout", "./document.pdf", "-"],
        "attachments": ["pdfdetach", "-list", "./document.pdf"],
        "images": ["pdfimages", "-list", "./document.pdf"],
    }
    measurements = dict(
        zip(
            operations,
            await _measurements(
                [
                    lambda name=name, argv=argv: session.run(name, argv)
                    for name, argv in operations.items()
                ],
                budget,
            ),
            strict=True,
        )
    )
    findings.append(
        Finding(
            "pdf.pages",
            f"PDF contains {pages} pages.",
            "info",
            "passed",
            details={"pages": pages, "scope": "Total pages, including references and appendices."},
        )
    )
    if max_pages is not None:
        findings.append(
            _constraint(
                "pdf.page_limit",
                f"PDF has {pages} total pages; configured maximum: {max_pages}.",
                pages > max_pages,
                {
                    "pages": pages,
                    "maximum": max_pages,
                    "scope": "All pages, including references and appendices.",
                },
            )
        )
    findings.append(
        Finding(
            "pdf.metadata",
            "PDF document properties were read.",
            "info",
            "passed",
            details={"properties": fields},
        )
    )
    try:
        geometry = _completed(measurements["geometry"])
        sizes = _page_sizes(geometry.stdout, pages)
        findings.append(
            Finding(
                "pdf.page_geometry",
                "PDF page dimensions and boxes were read for every page.",
                "info",
                "passed",
                details={
                    "dimensions_pt": sizes,
                    "boxes": geometry.stdout,
                    "unit": "PDF points (1/72 inch)",
                },
            )
        )
        if (
            options.expected_page_width_pt is not None
            and options.expected_page_height_pt is not None
        ):
            expected = (options.expected_page_width_pt, options.expected_page_height_pt)
            differing = [
                page
                for page, size in enumerate(sizes, 1)
                if any(
                    abs(value - target) > options.page_size_tolerance_pt
                    for value, target in zip(size, expected, strict=True)
                )
            ]
            findings.append(
                _constraint(
                    "pdf.page_dimensions",
                    f"Expected {expected[0]:g} × {expected[1]:g} PDF points "
                    f"within {options.page_size_tolerance_pt:g} points; "
                    f"differing pages: {differing or 'none'}.",
                    bool(differing),
                    {
                        "expected_pt": expected,
                        "tolerance_pt": options.page_size_tolerance_pt,
                        "dimensions_pt": sizes,
                        "differing_pages": differing,
                        "unit": "PDF points (1/72 inch)",
                        "orientation": (
                            "Width and height are ordered as reported by pdfinfo; "
                            "not swapped automatically."
                        ),
                    },
                )
            )
        if options.require_consistent_page_size:
            # Max-minus-min enforces the tolerance for every pair of pages, not
            # just against a reference page that could hide opposite deviations.
            spread = tuple(
                max(size[axis] for size in sizes) - min(size[axis] for size in sizes)
                for axis in (0, 1)
            )
            findings.append(
                _constraint(
                    "pdf.page_size_consistency",
                    f"Page width/height ranges span {spread[0]:g}/{spread[1]:g} PDF points; "
                    f"maximum permitted spread: {options.page_size_tolerance_pt:g} points.",
                    any(value > options.page_size_tolerance_pt for value in spread),
                    {
                        "dimensions_pt": sizes,
                        "range_pt": spread,
                        "tolerance_pt": options.page_size_tolerance_pt,
                        "unit": "PDF points (1/72 inch)",
                    },
                )
            )
    except (PreparationError, OSError) as error:
        findings.append(_unavailable("pdf.geometry_unavailable", error))
        for rule in ("pdf.page_dimensions", "pdf.page_size_consistency"):
            if rule in _configured_pdf_rules(options, max_pages):
                findings.append(_unavailable(rule, error))
    for name, required, rule in (
        ("Encrypted", options.forbid_encryption, "pdf.encryption_policy"),
        ("Form", options.forbid_forms, "pdf.forms_policy"),
        ("JavaScript", options.forbid_javascript, "pdf.javascript_policy"),
    ):
        value = fields.get(name)
        present = _feature_presence(name, value)
        findings.append(
            Finding(
                f"pdf.{name.lower()}",
                f"{name}: {value}."
                if value is not None
                else f"{name} was not reported by pdfinfo.",
                "info" if present is not None else "warning",
                "passed" if present is not None else "inconclusive",
                details={"value": value, "scope": "pdfinfo document-level feature reporting"},
            )
        )
        if required:
            if present is None:
                findings.append(
                    _unavailable(
                        rule, PreparationError(f"pdfinfo did not report a recognized {name} value")
                    )
                )
            else:
                findings.append(
                    _constraint(
                        rule,
                        f"{name}: {value}; the configured policy requires absence.",
                        present,
                        {
                            "value": value,
                            "scope": (
                                "pdfinfo document-level feature reporting; "
                                "arbitrary media/annotations are not covered."
                            ),
                        },
                    )
                )
    try:
        fonts = _completed(measurements["fonts"])
        inventory = _font_inventory(fonts.stdout)
        missing = [font["name"] for font in inventory if not font["embedded"]]
        findings.append(
            Finding(
                "pdf.fonts",
                f"Inspected {len(inventory)} font resources; {len(missing)} are not embedded.",
                "warning" if missing else "info",
                "failed" if missing else "passed",
                details={
                    "fonts": inventory,
                    "nonembedded": missing,
                    "scope": (
                        "Font resources reported by pdffonts; "
                        "source-figure attribution is not established."
                    ),
                },
            )
        )
        if options.require_embedded_fonts:
            if not inventory:
                findings.append(
                    _unavailable(
                        "pdf.font_embedding",
                        PreparationError(
                            "No font resources were reported; an embedding requirement cannot "
                            "establish text/font coverage for outlined or image-only content"
                        ),
                    )
                )
            else:
                findings.append(
                    _constraint(
                        "pdf.font_embedding",
                        f"Unembedded fonts: {', '.join(str(name) for name in missing) or 'none'}; "
                        "all reported font resources must be embedded.",
                        bool(missing),
                        {"nonembedded": missing, "fonts": inventory},
                    )
                )
        if options.forbid_type3_fonts:
            type3 = [font for font in inventory if _is_type3(font["type"])]
            findings.append(
                _constraint(
                    "pdf.type3_fonts",
                    "Type 3 fonts: "
                    f"{', '.join(str(font['name']) for font in type3) or 'none'}; "
                    "the configured policy forbids Type 3.",
                    bool(type3),
                    {"type3_fonts": type3, "inspected_fonts": len(inventory)},
                )
            )
    except (PreparationError, OSError) as error:
        findings.append(_unavailable("pdf.fonts_unavailable", error))
        for rule, required in (
            ("pdf.font_embedding", options.require_embedded_fonts),
            ("pdf.type3_fonts", options.forbid_type3_fonts),
        ):
            if required:
                findings.append(_unavailable(rule, error))
    try:
        text = _completed(measurements["text"])
        text_pages = _normalized_pages(text.stdout, pages)
        empty = [page for page, value in enumerate(text_pages, 1) if not value.strip()]
        findings.append(
            Finding(
                "pdf.text",
                "Extractable text is present on every page."
                if not empty
                else (
                    "Some pages have no extractable text; image-only, blank, or outlined "
                    "content may be intentional."
                ),
                "warning" if empty else "info",
                "inconclusive" if empty else "passed",
                details={
                    "extracted_characters": len(text.stdout),
                    "pages_without_text": empty,
                    "scope": "Text extraction, not OCR or semantic readability.",
                },
            )
        )
    except (PreparationError, OSError) as error:
        findings.append(_unavailable("pdf.text_unavailable", error))
    try:
        attachments = _completed(measurements["attachments"])
        match = re.search(r"^(\d+) embedded files\s*$", attachments.stdout, re.MULTILINE)
        if match is None:
            raise PreparationError("pdfdetach did not report its attachment count")
        count = int(match[1])
        entries = re.findall(r"^(\d+):\s*\S.*$", attachments.stdout, re.MULTILINE)
        if [int(index) for index in entries] != list(range(1, count + 1)):
            raise PreparationError("pdfdetach did not return a complete attachment inventory")
        findings.append(
            Finding(
                "pdf.attachments",
                f"PDF contains {count} embedded files.",
                "info",
                "passed",
                details={"count": count, "inventory": attachments.stdout},
            )
        )
        if options.forbid_attachments:
            findings.append(
                _constraint(
                    "pdf.attachments_policy",
                    f"PDF contains {count} embedded files; configured maximum: 0.",
                    count > 0,
                    {"count": count, "inventory": attachments.stdout},
                )
            )
    except (PreparationError, OSError) as error:
        findings.append(_unavailable("pdf.attachments_unavailable", error))
        if options.forbid_attachments:
            findings.append(_unavailable("pdf.attachments_policy", error))
    try:
        images = _completed(measurements["images"])
        image_inventory = _image_inventory(images.stdout, pages)
        findings.append(
            Finding(
                "pdf.images",
                f"Measured {len(image_inventory)} reported raster placements, including masks.",
                "info",
                "passed",
                evidence="derived",
                details={
                    "images": image_inventory,
                    "scope": (
                        "Effective x/y PPI from pdfimages for each reported placement, "
                        "including masks; vector artwork has no raster-DPI test."
                    ),
                },
            )
        )
        if options.min_image_dpi is not None:
            unknown = [item for item in image_inventory if item["x_dpi"] == 0 or item["y_dpi"] == 0]
            low = [
                item
                for item in image_inventory
                if isinstance(item["x_dpi"], float)
                and isinstance(item["y_dpi"], float)
                and min(item["x_dpi"], item["y_dpi"]) > 0
                and min(item["x_dpi"], item["y_dpi"]) < options.min_image_dpi
            ]
            if unknown and not low:
                findings.append(
                    Finding(
                        "pdf.image_resolution",
                        "No usable DPI for raster placements on pages "
                        f"{', '.join(sorted({str(item['page']) for item in unknown}))}; "
                        f"configured minimum: {options.min_image_dpi:g} DPI.",
                        "error",
                        "inconclusive",
                        evidence="derived",
                        suggestion="Inspect the listed raster placements for usable DPI.",
                        details={"minimum_dpi": options.min_image_dpi, "unmeasured": unknown},
                    )
                )
            else:
                findings.append(
                    _constraint(
                        "pdf.image_resolution",
                        f"Minimum {options.min_image_dpi:g} DPI on both axes; "
                        f"{len(image_inventory)} raster placements inspected. Below minimum: "
                        + (
                            "; ".join(
                                f"page {item['page']} image {item['number']}: "
                                f"{item['x_dpi']} × {item['y_dpi']} DPI"
                                for item in low[:8]
                            )
                            or "none"
                        )
                        + (f"; plus {len(low) - 8} more" if len(low) > 8 else ""),
                        bool(low),
                        {
                            "minimum_dpi": options.min_image_dpi,
                            "below_minimum": low,
                            "unmeasured": unknown,
                            "inspected_placements": len(image_inventory),
                            "includes_masks": True,
                        },
                    )
                )
    except (PreparationError, OSError) as error:
        findings.append(_unavailable("pdf.images_unavailable", error))
        if options.min_image_dpi is not None:
            findings.append(_unavailable("pdf.image_resolution", error))
    findings.append(
        Finding(
            "pdf.annotation_media_scope",
            (
                "Arbitrary annotations and active media are not inspected by this "
                "Poppler adapter; their absence is unverified."
            ),
            # A scope disclaimer, not an observation: it must not make an otherwise
            # clean inspection an advisory outcome.
            "info",
            "skipped",
            suggestion="Use a structural PDF checker before relying on these objects being absent.",
        )
    )
    findings.append(
        Finding(
            "pdf.tools",
            "PDF tool versions were recorded.",
            "info",
            "passed",
            details={"tools": session.tools()},
        )
    )
    return findings


def _normalized_pages(text: str, pages: int) -> list[str]:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    extracted = text.split("\f")
    if extracted and not extracted[-1].strip():
        extracted.pop()
    if len(extracted) != pages:
        raise PreparationError("Extracted text did not preserve the expected page boundaries")
    return ["\n".join(line.rstrip(" \t") for line in page.split("\n")) for page in extracted]


def _read_raster(path: Path, *, max_pixels: int = _MAX_PIXELS) -> tuple[tuple[int, int], bytes]:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 3 * max_pixels + 1024:
        raise PreparationError("Renderer did not produce a bounded raster page")
    with path.open("rb") as stream:
        # Validate the header before allocating pixel buffers; reading the entire
        # file and then slicing it transiently duplicates a full page in memory.
        header = re.match(rb"P6\s+(\d+)\s+(\d+)\s+255(?:\r\n|\s)", stream.read(1024))
        if header is None:
            raise PreparationError("Renderer output is not a supported RGB PPM page")
        width, height = int(header[1]), int(header[2])
        if (
            min(width, height) < 1
            or max(width, height) > _MAX_DIMENSION
            or width * height > min(max_pixels, _MAX_PIXELS)
        ):
            raise PreparationError("Renderer output has invalid or oversized raster dimensions")
        stream.seek(header.end())
        pixels = stream.read(width * height * 3 + 1)
    if len(pixels) != width * height * 3:
        raise PreparationError("Renderer output has invalid or oversized raster dimensions")
    return (width, height), pixels


@dataclass
class _ComparisonSide:
    pages: int
    dimensions: list[tuple[float, float]] | None = None
    text: list[str] | None = None
    errors: list[str] = field(default_factory=list)


async def _comparison_side(session: _PdfWork, name: str) -> _ComparisonSide:
    document = f"./{name}.pdf"
    info = await session.run(f"{name}/properties", ["pdfinfo", document])
    pages = _page_count(_fields(info.stdout))
    _require_rendered_pages(pages)
    side = _ComparisonSide(pages)
    geometry, text = await _measurements(
        [
            lambda: session.run(
                f"{name}/geometry", ["pdfinfo", "-box", "-f", "1", "-l", str(pages), document]
            ),
            lambda: session.run(
                f"{name}/text", ["pdftotext", "-enc", "UTF-8", "-layout", document, "-"]
            ),
        ],
        session.budget,
    )
    try:
        side.dimensions = _page_sizes(_completed(geometry).stdout, pages)
    except (PreparationError, OSError) as error:
        side.errors.append(f"{name} page geometry: {error}")
    try:
        side.text = _normalized_pages(_completed(text).stdout, pages)
    except (PreparationError, OSError) as error:
        side.errors.append(f"{name} text extraction: {error}")
    return side


def _raster_pixel_count(size: tuple[float, float]) -> int:
    return math.ceil(size[0] * _DPI / 72) * math.ceil(size[1] * _DPI / 72)


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


async def _render_pair(
    session: _PdfWork,
    page: int,
    dimensions: dict[str, list[tuple[float, float]]],
    options: ComparisonOptions | None = None,
) -> dict[str, object] | None:
    name = f"page-{page:04d}"
    memory_mb = _pair_memory_mb(dimensions["left"][page - 1], dimensions["right"][page - 1])

    async def render() -> dict[str, object] | None:
        rasters: list[tuple[tuple[int, int], bytes]] = []
        try:
            for side in ("left", "right"):
                await session.run(
                    f"{name}/{side}",
                    [
                        "pdftoppm",
                        "-r",
                        str(_DPI),
                        "-f",
                        str(page),
                        "-l",
                        str(page),
                        "-singlefile",
                        f"./{side}.pdf",
                        "page",
                    ],
                    leased=True,
                    memory_mb=_RENDER_HEADROOM_MB,
                )
                path = session.work / name / side / "command/page.ppm"
                rasters.append(
                    _read_raster(path, max_pixels=_raster_pixel_count(dimensions[side][page - 1]))
                )
                path.unlink()
            (size_left, pixels_left), (size_right, pixels_right) = rasters
            dimensions_changed = dimensions["left"][page - 1] != dimensions["right"][page - 1]
            if size_left != size_right or pixels_left != pixels_right or dimensions_changed:
                measurement: dict[str, object] = {
                    "page": page,
                    "left_pixels": size_left,
                    "right_pixels": size_right,
                    "page_dimensions_changed": dimensions_changed,
                }
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
            return None
        finally:
            rasters.clear()
            # Failed and cancelled renderers can leave partial rasters. The runner
            # has reaped the process before returning control to this cleanup.
            pair_work = session.work / name
            if pair_work.exists():
                shutil.rmtree(pair_work)

    if session.budget is None:
        return await render()
    async with session.budget.lease(
        f"{session.work.name}: compare PDF page {page}", memory_mb=memory_mb, kind="render"
    ):
        return await render()


async def compare_pdfs(
    left: Path,
    right: Path,
    work: Path,
    runner: ToolRunner,
    *,
    budget: ResourceBudget | None = None,
    options: ComparisonOptions | None = None,
) -> list[Finding]:
    findings: list[Finding] = []
    policy: dict[str, object] = {
        "dpi": _DPI,
        "pixel_tolerance": options.max_changed_pixel_ratio if options else 0,
        "channel_tolerance": options.channel_tolerance if options else 0,
        "text_normalization": "line endings and trailing horizontal whitespace only",
        "metadata_compared": False,
        "semantic_equivalence_proven": False,
    }
    try:
        work, documents = await _new_workspace(
            work, {"left.pdf": left, "right.pdf": right}, runner, budget
        )
    except (PreparationError, OSError) as error:
        return [_unavailable("compare.unavailable", error)]
    session = _PdfWork(work, documents, runner, budget)
    prepared = await _measurements(
        [lambda: _comparison_side(session, "left"), lambda: _comparison_side(session, "right")],
        budget,
    )
    sides: dict[str, _ComparisonSide] = {}
    errors: list[str] = []
    for name, result in zip(("left", "right"), prepared, strict=True):
        if isinstance(result, (PreparationError, OSError)):
            errors.append(f"{name} PDF preflight: {result}")
        else:
            sides[name] = result
            errors.extend(result.errors)
    render_evidence: dict[str, object] = {}
    if len(sides) == 2:
        page_counts = {name: side.pages for name, side in sides.items()}
        same_count = page_counts["left"] == page_counts["right"]
        findings.append(
            Finding(
                "compare.page_count",
                "Page counts match." if same_count else "Page counts changed.",
                "info" if same_count else "error",
                "passed" if same_count else "failed",
                details={**policy, **page_counts},
            )
        )
        common = min(page_counts.values())
        missing_pages = list(range(common + 1, max(page_counts.values()) + 1))
        left_text, right_text = sides["left"].text, sides["right"].text
        if left_text is not None and right_text is not None:
            changed_text = [
                page for page in range(1, common + 1) if left_text[page - 1] != right_text[page - 1]
            ] + missing_pages
            findings.append(
                Finding(
                    "compare.text",
                    "Normalized extracted text matches."
                    if not changed_text
                    else f"Extracted text differs on {len(changed_text)} pages.",
                    "error" if changed_text else "info",
                    "failed" if changed_text else "passed",
                    details={**policy, "changed_pages": changed_text},
                )
            )
        dimensions = {
            name: side.dimensions for name, side in sides.items() if side.dimensions is not None
        }
        if len(dimensions) == 2:
            results = await _measurements(
                [
                    lambda page=page: (
                        _render_pair(session, page, dimensions, options)
                        if options is not None
                        else _render_pair(session, page, dimensions)
                    )
                    for page in range(1, common + 1)
                ],
                budget,
            )
            changed_rasters: list[dict[str, object]] = []
            completed_pages: list[int] = []
            failed_pages: list[int] = []
            for page, result in enumerate(results, 1):
                if isinstance(result, (PreparationError, OSError)):
                    errors.append(f"Page {page} rendering: {result}")
                    failed_pages.append(page)
                else:
                    completed_pages.append(page)
                    if result is not None:
                        changed_rasters.append(result)
            changed_pages = [
                item["page"] for item in changed_rasters if not item.get("within_tolerance")
            ] + missing_pages
            if failed_pages:
                render_evidence = {
                    "completed_pages": completed_pages,
                    "unavailable_pages": failed_pages,
                    "known_changed_pages": changed_pages,
                    "measurements": changed_rasters,
                }
            else:
                findings.append(
                    Finding(
                        "compare.rendering",
                        (
                            "Rendered pages match within the explicit pixel tolerances at 144 DPI."
                            if options
                            and (options.channel_tolerance or options.max_changed_pixel_ratio)
                            else "Rendered pages match exactly at 144 DPI."
                        )
                        if not changed_pages
                        else f"Rendered output differs on {len(changed_pages)} pages.",
                        "error" if changed_pages else "info",
                        "failed" if changed_pages else "passed",
                        details={
                            **policy,
                            "changed_pages": changed_pages,
                            "measurements": changed_rasters,
                        },
                    )
                )
    if errors:
        unavailable = _unavailable("compare.unavailable", PreparationError("; ".join(errors)))
        findings.append(replace(unavailable, details={**policy, **render_evidence}))
    if session.versions:
        findings.append(
            Finding(
                "compare.tools",
                "Comparison tool versions were recorded.",
                "info",
                "passed",
                details={"tools": session.tools()},
            )
        )
    return findings
