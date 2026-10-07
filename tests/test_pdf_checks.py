from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from latexprep.models import PreparationError
from latexprep.pdf_checks import (
    PdfCheckOptions,
    PdfRegion,
    PdfSectionBudget,
    extract_pdf_evidence,
    inspect_included_pdf_figures,
    inspect_pdf_details,
)
from latexprep.runtime import CommandResult, RuntimeLimits, ToolRunner
from latexprep.scheduler import ResourceBudget


def geometry(*, pages: int = 1, rotation: int = 0, crop: str = "0 0 612 792") -> str:
    return "".join(
        f"Page {page} size: 612 x 792 pts\nPage {page} rot: {rotation}\n"
        f"Page {page} MediaBox: 0 0 612 792\nPage {page} CropBox: {crop}\n"
        for page in range(1, pages + 1)
    )


def text_xml(*, size: float = 12, left: float = 72, extra: str = "", text: str = "Body") -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE pdf2xml SYSTEM "pdf2xml.dtd">'
        '<pdf2xml producer="poppler" version="26.10.0">'
        '<page number="1" position="absolute" top="0" left="0" height="792" width="612">'
        f'<fontspec id="0" size="{size}" family="Times" color="#000000"/>'
        f'<text top="72" left="{left}" width="100" height="12" font="0">{text}</text>'
        f"{extra}</page></pdf2xml>"
    )


def object_json(
    *,
    language: object = "u:en-GB",
    annotations: object = None,
    action: object = None,
    orphan: object = None,
) -> str:
    catalog: dict[str, object] = {"/Type": "/Catalog", "/Pages": "2 0 R"}
    if language is not None:
        catalog["/Lang"] = language
    if action is not None:
        catalog["/OpenAction"] = "5 0 R"
    page: dict[str, object] = {"/Type": "/Page", "/Parent": "2 0 R"}
    objects: dict[str, object] = {
        "trailer": {"value": {"/Root": "1 0 R"}},
        "obj:1 0 R": {"value": catalog},
        "obj:2 0 R": {"value": {"/Type": "/Pages", "/Kids": ["3 0 R"], "/Count": 1}},
        "obj:3 0 R": {"value": page},
    }
    if annotations is not None:
        page["/Annots"] = ["4 0 R"]
        objects["obj:4 0 R"] = {"value": annotations}
    if action is not None:
        objects["obj:5 0 R"] = {"value": action}
    if orphan is not None:
        objects["obj:6 0 R"] = {"value": orphan}
    return json.dumps(
        {
            "version": 2,
            "pages": [{"object": "3 0 R", "pageposfrom1": 1}],
            "qpdf": [{"jsonversion": 2}, objects],
        }
    )


def trace_xml(
    *,
    width: str = "0.7",
    transform: str = "2 0 0 2 0 0",
    color: str = 'colorspace="DeviceGray" color="0"',
) -> str:
    return (
        '<document filename="document.pdf"><page number="1" mediabox="0 0 612 792">'
        f'<stroke_path linewidth="{width}" miterlimit="10" linecap="0,0,0" '
        f'linejoin="0" {color} transform="{transform}">'
        '<moveto x="30" y="30"/><lineto x="100" y="30"/></stroke_path>'
        "</page></document>"
    )


def gray_raster(*, blank: bool = False) -> bytes:
    pixels = 306 * 396
    ink = 0 if blank else pixels // 4
    return b"P5\n306 396\n255\n" + b"\0" * ink + b"\xff" * (pixels - ink)


def color_trace(
    *,
    text_color: str = "0 0 0",
    other_color: str = "1 1 1",
    space: str = "DeviceRGB",
    alpha: str = "1",
) -> str:
    return (
        '<document filename="document.pdf"><page number="1" mediabox="0 0 612 792">'
        f'<fill_text colorspace="{space}" color="{text_color}" alpha="{alpha}" '
        'transform="1 0 0 1 0 0"><span font="Times" wmode="0" trm="12 0 0 12">'
        '<g unicode="A" glyph="A" x="20" y="30"/></span></fill_text>'
        f'<fill_path colorspace="DeviceRGB" color="{other_color}" transform="1 0 0 1 0 0">'
        '<moveto x="20" y="40"/><lineto x="50" y="40"/></fill_path></page></document>'
    )


def simple_pdf() -> bytes:
    content = b"BT /F1 12 Tf 72 700 Td (Body text) Tj ET\n"
    objects = (
        b"<< /Type /Catalog /Pages 2 0 R /Lang (en-GB) >>",
        b"<< /Type /Pages /Count 1 /Kids [3 0 R] >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"endstream",
        b"<< /Title (Paper) /Author (Ada Lovelace) >>",
    )
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, value in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode() + value + b"\nendobj\n")
    start = len(output)
    output.extend(b"xref\n0 7\n0000000000 65535 f \n")
    output.extend(b"".join(f"{offset:010} 00000 n \n".encode() for offset in offsets[1:]))
    output.extend(
        f"trailer\n<< /Size 7 /Root 1 0 R /Info 6 0 R >>\nstartxref\n{start}\n%%EOF\n".encode()
    )
    return bytes(output)


class DetailRunner(ToolRunner):
    def __init__(self, **outputs: object):
        super().__init__()
        self.info = "Pages: 1\nTitle: Paper\nAuthor: Ada Lovelace\nTagged: yes\n"
        self.geometry = geometry()
        self.xml = text_xml()
        self.objects = object_json()
        self.trace = trace_xml()
        self.text = "Body text\n\f"
        self.raster = gray_raster()
        self.fonts = (
            "name type encoding emb sub uni object ID\n---\nFont Type 1 Builtin yes yes no 3 0\n"
        )
        self.missing: str | None = None
        self.failure: str | None = None
        self.calls: list[tuple[list[str], Path, tuple[Path, ...]]] = []
        self.expected_budget: ResourceBudget | None = None
        self.block: asyncio.Event | None = None
        self.started = asyncio.Event()
        for key, value in outputs.items():
            setattr(self, key, value)

    def _resolve_tool(self, name: str) -> Path:
        return Path("/test-tools") / name

    async def run(
        self,
        argv: list[str],
        cwd: Path,
        workspace: Path,
        *,
        readonly_inputs: tuple[Path, ...] = (),
        memory_mb: int | None = None,
    ) -> CommandResult:
        self._validate_readonly_inputs(readonly_inputs)
        if self.expected_budget is not None and self.expected_budget.current_lease is None:
            raise AssertionError("PDF command ran without its shared budget lease")
        self.calls.append((list(argv), workspace, readonly_inputs))
        if argv[0] == self.missing:
            raise PreparationError(f"Missing tool: {argv[0]}")
        if argv[-1] in {"-v", "--version"}:
            if argv[0] == "qpdf" and argv[-1] != "--version":
                raise AssertionError("qpdf requires --version")
            return CommandResult(0, argv[0] + " version fixture\n", "", False, argv)
        self.started.set()
        if self.block is not None:
            await self.block.wait()
        await asyncio.sleep(0)
        if argv[0] == "pdfinfo":
            output = self.geometry if "-box" in argv else self.info
        else:
            output = {
                "pdftohtml": self.xml,
                "qpdf": self.objects,
                "mutool": self.trace,
                "pdftotext": self.text,
                "pdffonts": self.fonts,
            }.get(argv[0], "")
        if argv[0] == "pdftoppm":
            (workspace / "page.pgm").write_bytes(self.raster)
        return CommandResult(
            0,
            output,
            "",
            self.failure == "timeout",
            argv,
            output_limited=self.failure == "output",
            resource_exceeded="Resource exceeded" if self.failure == "resource" else None,
        )


class PdfDetailTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.pdf = self.root / "document.pdf"
        self.pdf.write_bytes(b"%PDF-fixture")
        self.sequence = 0

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    def work(self) -> Path:
        self.sequence += 1
        return self.root / f"inspect-{self.sequence}"

    async def check(self, options: PdfCheckOptions, runner: DetailRunner | None = None):
        return await inspect_pdf_details(
            self.pdf, self.work(), runner or DetailRunner(), options=options
        )

    async def assert_status(self, options: PdfCheckOptions, status: str, **outputs: object):
        result = await self.check(options, DetailRunner(**outputs))
        self.assertEqual(len(result), 1, result)
        self.assertEqual(result[0].status, status, result)
        return result[0]

    async def test_section_budget_accepts_rejects_and_marks_outside_ranges_uncertain(self) -> None:
        base = PdfCheckOptions(section_budgets=(PdfSectionBudget("Main text", 1, 2, 2),))
        await self.assert_status(base, "passed", info="Pages: 3\n")
        await self.assert_status(
            replace(base, section_budgets=(PdfSectionBudget("References", 1, 3, 2),)),
            "failed",
            info="Pages: 3\n",
        )
        await self.assert_status(base, "inconclusive")

    async def test_text_size_threshold_rounding_and_unmeasured_text_are_distinct(self) -> None:
        options = PdfCheckOptions(min_text_size_pt=10)
        await self.assert_status(options, "passed")
        failure = await self.assert_status(options, "failed", xml=text_xml(size=8))
        self.assertEqual(failure.details["violations"][0]["page"], 1)
        await self.assert_status(options, "inconclusive", xml=text_xml(size=10))
        await self.assert_status(options, "inconclusive", xml=text_xml(text=""))
        await self.assert_status(
            options, "inconclusive", xml=text_xml().replace('font="0"', 'font="9"')
        )

    async def test_text_size_regions_apply_specific_minima_and_reject_partial_overlap(self) -> None:
        region = PdfRegion("Figure labels", 1, 60, 60, 180, 90, 14)
        options = PdfCheckOptions(min_text_size_pt=10, text_size_regions=(region,))
        await self.assert_status(options, "failed")
        await self.assert_status(
            replace(options, text_size_regions=(replace(region, min_text_size_pt=11),)), "passed"
        )
        await self.assert_status(
            replace(options, text_size_regions=(replace(region, right_pt=80),)), "inconclusive"
        )

    async def test_printable_bounds_accept_full_width_regions_and_flag_outside_text(self) -> None:
        options = PdfCheckOptions(printable_margins_pt=(72, 72, 72, 72))
        await self.assert_status(options, "passed")
        failure = await self.assert_status(options, "failed", xml=text_xml(left=60))
        self.assertEqual(failure.evidence, "heuristic")
        await self.assert_status(options, "inconclusive", geometry=geometry(rotation=90))
        await self.assert_status(options, "inconclusive", geometry=geometry(crop="10 10 600 780"))
        await self.assert_status(
            replace(
                options, printable_regions=(PdfRegion("Full width heading", 1, 30, 50, 580, 100),)
            ),
            "passed",
            xml=text_xml(left=60),
        )

    async def test_text_overlap_candidates_have_geometric_evidence_and_bounded_scope(self) -> None:
        options = PdfCheckOptions(overlap_min_area_ratio=0.4)
        await self.assert_status(options, "passed")
        extra = '<text top="74" left="100" width="100" height="12" font="0">Other</text>'
        failure = await self.assert_status(options, "failed", xml=text_xml(extra=extra))
        self.assertEqual(failure.severity, "warning")
        self.assertGreater(failure.details["violations"][0]["area_ratio"], 0.4)
        await self.assert_status(options, "inconclusive", xml=text_xml(text=""))
        await self.assert_status(options, "inconclusive", xml="<pdf2xml/>")

    async def test_overlap_evidence_is_capped_without_losing_total_count(self) -> None:
        options = PdfCheckOptions(overlap_min_area_ratio=0.4)
        extra = '<text top="72" left="72" width="100" height="12" font="0">Other</text>'
        failure = await self.assert_status(options, "failed", xml=text_xml(extra=extra * 100))
        self.assertEqual(len(failure.details["violations"]), 100)
        self.assertEqual(failure.details["violation_count"], 5050)

    async def test_geometry_disagreement_cannot_establish_printable_bounds(self) -> None:
        await self.assert_status(
            PdfCheckOptions(printable_margins_pt=(72, 72, 72, 72)),
            "inconclusive",
            xml=text_xml().replace('height="792"', 'height="400"'),
        )

    async def test_sparse_pages_measure_ink_and_distinguish_missing_raster(self) -> None:
        options = PdfCheckOptions(min_page_ink_ratio=0.1)
        await self.assert_status(options, "passed")
        blank = gray_raster(blank=True)
        failure = await self.assert_status(options, "failed", raster=blank)
        self.assertTrue(failure.details["last_page"])
        self.assertEqual(failure.details["ink_ratio"], 0)
        await self.assert_status(options, "inconclusive", raster=blank[:-1])
        self.assertEqual(await self.check(replace(options, sparse_page_exemptions=(1,))), [])

    async def test_raster_preflight_and_partial_output_cleanup_bound_artifacts(self) -> None:
        options = PdfCheckOptions(min_page_ink_ratio=0.1)
        runner = DetailRunner(raster=gray_raster()[:-1])
        work = self.work()
        result = await inspect_pdf_details(self.pdf, work, runner, options=options)
        self.assertEqual(result[0].status, "inconclusive")
        self.assertEqual(list(work.rglob("*.pgm")), [])
        runner = DetailRunner(geometry=geometry().replace("612 x 792", "100000 x 792"))
        result = await self.check(options, runner)
        self.assertEqual(result[0].status, "inconclusive")
        self.assertFalse(any(argv[0] == "pdftoppm" for argv, _, _ in runner.calls))

    async def test_required_metadata_handles_missing_fields_and_ambiguous_evidence(self) -> None:
        options = PdfCheckOptions(required_metadata=("Title", "Author"))
        await self.assert_status(options, "passed")
        failure = await self.assert_status(
            options, "failed", info="Pages: 1\nTitle: Paper\nAuthor: \n"
        )
        self.assertEqual(failure.details["violations"], ["Author"])
        await self.assert_status(
            options, "inconclusive", info="Pages: 1\nTitle: Paper\nTitle: Other\n"
        )

    async def test_metadata_agreement_preserves_case_and_author_order(self) -> None:
        options = PdfCheckOptions(
            expected_metadata=(("Title", "  Paper \n"), ("Author", "Ada Lovelace"))
        )
        await self.assert_status(options, "passed")
        await self.assert_status(
            options, "failed", info="Pages: 1\nTitle: paper\nAuthor: Lovelace Ada\n"
        )
        await self.assert_status(options, "inconclusive", missing="pdfinfo")

    async def test_tagging_presence_does_not_claim_semantic_accessibility(self) -> None:
        options = PdfCheckOptions(require_tagged=True)
        accepted = await self.assert_status(options, "passed")
        self.assertIn("reading order", accepted.details["scope"])
        await self.assert_status(options, "failed", info="Pages: 1\nTagged: no\n")
        await self.assert_status(options, "inconclusive", info="Pages: 1\n")

    async def test_catalog_language_uses_resolved_objects_and_explicit_expectation(self) -> None:
        options = PdfCheckOptions(require_language=True, expected_language="en-gb")
        await self.assert_status(options, "passed")
        await self.assert_status(options, "failed", objects=object_json(language="u:de-DE"))
        await self.assert_status(options, "failed", objects=object_json(language=None))
        await self.assert_status(options, "inconclusive", objects=object_json(language="b:656e"))
        await self.assert_status(options, "inconclusive", missing="qpdf")

    async def test_annotation_policy_reads_indirect_page_annotations(self) -> None:
        options = PdfCheckOptions(forbidden_annotation_types=("Text", "RichMedia"))
        await self.assert_status(
            options, "passed", objects=object_json(annotations={"/Subtype": "/Link"})
        )
        failure = await self.assert_status(
            options,
            "failed",
            objects=object_json(
                annotations={"/Subtype": "/Text", "/Contents": "u:Private review note"}
            ),
        )
        self.assertEqual(failure.details["violations"], [{"page": 1, "type": "Text"}])
        self.assertNotIn("Private review note", repr(failure))
        await self.assert_status(
            options, "inconclusive", objects=object_json(annotations={"/Contents": "u:Note"})
        )

    async def test_action_policy_scans_reachable_objects_without_echoing_payload(self) -> None:
        options = PdfCheckOptions(forbidden_action_types=("JavaScript", "Launch"))
        await self.assert_status(
            options, "passed", objects=object_json(orphan={"/S": "/JavaScript"})
        )
        failure = await self.assert_status(
            options,
            "failed",
            objects=object_json(
                action={"/S": "/JavaScript", "/JS": "u:Private executable payload"}
            ),
        )
        self.assertEqual(failure.evidence, "heuristic")
        self.assertNotIn("Private executable payload", repr(failure))
        await self.assert_status(
            options, "inconclusive", objects=object_json(action={"/Next": "9 0 R"})
        )

    async def test_unresolved_action_does_not_erase_completed_language_observation(self) -> None:
        result = await self.check(
            PdfCheckOptions(require_language=True, forbidden_action_types=("JavaScript",)),
            DetailRunner(objects=object_json(action={"/Next": "9 0 R"})),
        )
        states = {finding.rule: finding.status for finding in result}
        self.assertEqual(
            states, {"pdf.document_language": "passed", "pdf.action_policy": "inconclusive"}
        )

    async def test_included_figure_fonts_keep_source_attribution_and_unknown_coverage(self) -> None:
        options = PdfCheckOptions(
            require_embedded_figure_fonts=True, forbid_type3_figure_fonts=True
        )
        for state, fonts in (
            ("passed", "Font Type 1 Builtin yes yes no 3 0\n"),
            ("failed", "Font Type 3 Custom no no no 3 0\n"),
            ("inconclusive", ""),
        ):
            with self.subTest(state=state):
                result = await inspect_included_pdf_figures(
                    (self.pdf,),
                    self.work(),
                    DetailRunner(fonts="name type encoding emb sub uni object ID\n---\n" + fonts),
                    options=options,
                )
                self.assertEqual(result[0].status, state)
                self.assertEqual(result[0].path, str(self.pdf))
                self.assertIn("attribution is not established", result[0].details["scope"])

    async def test_stroke_width_applies_transform_and_rejects_hairline_or_anisotropic_measurements(
        self,
    ) -> None:
        options = PdfCheckOptions(min_stroke_width_pt=1)
        await self.assert_status(options, "passed")
        failure = await self.assert_status(options, "failed", trace=trace_xml(width="0.2"))
        self.assertEqual(failure.details["violations"][0]["effective_width_pt"], 0.4)
        await self.assert_status(options, "inconclusive", trace=trace_xml(width="0"))
        await self.assert_status(options, "inconclusive", trace=trace_xml(transform="2 0 0 1 0 0"))
        await self.assert_status(options, "inconclusive", missing="mutool")

    async def test_rendered_color_policy_handles_allowed_disallowed_and_unmeasured_spaces(
        self,
    ) -> None:
        options = PdfCheckOptions(allowed_color_spaces=("DeviceGray",))
        await self.assert_status(options, "passed")
        failure = await self.assert_status(
            options, "failed", trace=trace_xml(color='colorspace="DeviceRGB" color="1 0 0"')
        )
        self.assertEqual(failure.details["violations"][0]["color_space"], "DeviceRGB")
        await self.assert_status(options, "inconclusive", trace=trace_xml(color=""))

    async def test_unconfigured_checks_do_not_run_tools_or_report_policy_passes(self) -> None:
        runner = DetailRunner()
        self.assertEqual(await self.check(PdfCheckOptions(), runner), [])
        self.assertEqual(runner.calls, [])

    async def test_assumed_text_contrast_handles_high_low_and_unmeasured_colors(self) -> None:
        options = PdfCheckOptions(min_text_contrast=4.5, contrast_background_rgb=(1, 1, 1))
        accepted = await self.assert_status(options, "passed", trace=color_trace())
        self.assertEqual(accepted.details["assumed_background_rgb"], (1, 1, 1))
        failure = await self.assert_status(
            options, "failed", trace=color_trace(text_color="0.8 0.8 0.8")
        )
        self.assertLess(failure.details["violations"][0]["contrast_ratio"], 4.5)
        await self.assert_status(options, "inconclusive", trace=color_trace(space="DeviceCMYK"))
        await self.assert_status(options, "inconclusive", trace=color_trace(alpha="0.5"))

    async def test_grayscale_separation_flags_nominal_luminance_collisions_only(self) -> None:
        options = PdfCheckOptions(min_grayscale_luminance_difference=0.1)
        await self.assert_status(options, "passed", trace=color_trace())
        failure = await self.assert_status(
            options, "failed", trace=color_trace(text_color="1 0 0", other_color="0 0.58 0")
        )
        self.assertEqual(failure.evidence, "heuristic")
        self.assertIn("semantic color-only encoding", failure.details["scope"])
        await self.assert_status(options, "inconclusive", trace=color_trace(space="DeviceCMYK"))

    async def test_evidence_helper_bounds_text_and_retains_empty_page_evidence(self) -> None:
        text, properties = await extract_pdf_evidence(self.pdf, self.work(), DetailRunner())
        self.assertEqual(text, "Body text\n\f")
        self.assertEqual(properties["Title"], "Paper")
        text, _ = await extract_pdf_evidence(self.pdf, self.work(), DetailRunner(text="\f"))
        self.assertEqual(text, "\f")
        with self.assertRaises(PreparationError):
            await extract_pdf_evidence(self.pdf, self.work(), DetailRunner(text="Partial text"))

    async def test_shared_budget_frozen_inputs_and_separate_command_workspaces(self) -> None:
        budget = ResourceBudget(2, 512, render_jobs=1)
        runner = DetailRunner(expected_budget=budget)
        results = await inspect_pdf_details(
            self.pdf,
            self.work(),
            runner,
            options=PdfCheckOptions(
                min_text_size_pt=10,
                min_page_ink_ratio=0.1,
                expected_language="en-GB",
                min_stroke_width_pt=1,
            ),
            budget=budget,
        )
        self.assertTrue(all(item.status == "passed" for item in results), results)
        directories = [directory for _, directory, _ in runner.calls]
        self.assertEqual(len(directories), len(set(directories)))
        for argv, directory, inputs in runner.calls:
            if argv[-1] in {"-v", "--version"}:
                continue
            self.assertEqual(len(inputs), 1)
            self.assertNotEqual(inputs[0], self.pdf)
            self.assertNotIn(directory, inputs[0].parents)
            self.assertIn(str(inputs[0]), argv)
        self.assertIsNone(budget.current_lease)

    async def test_time_output_resource_limits_and_bad_xml_never_produce_passes(self) -> None:
        options = PdfCheckOptions(min_text_size_pt=10, require_tagged=True)
        for limit in ("timeout", "output", "resource"):
            result = await self.check(options, DetailRunner(failure=limit))
            self.assertTrue(all(item.status == "inconclusive" for item in result))
        await self.assert_status(
            PdfCheckOptions(min_text_size_pt=10),
            "inconclusive",
            xml=('<!DOCTYPE pdf2xml [<!ENTITY secret "sensitive">]><pdf2xml>&secret;</pdf2xml>'),
        )

    async def test_cancellation_releases_shared_budget(self) -> None:
        budget = ResourceBudget(1, 128)
        runner = DetailRunner(expected_budget=budget, block=asyncio.Event())
        task = asyncio.create_task(
            inspect_pdf_details(
                self.pdf,
                self.work(),
                runner,
                options=PdfCheckOptions(require_tagged=True),
                budget=budget,
            )
        )
        await asyncio.wait_for(runner.started.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        async with budget.lease("after cancellation", memory_mb=128):
            pass

    @unittest.skipUnless(
        os.environ.get("LATEX_PREP_RUN_INTEGRATION") == "1", "Opt-in live PDF tools"
    )
    async def test_live_poppler_geometry_metadata_and_raster_measurements(self) -> None:
        self.pdf.write_bytes(simple_pdf())
        runner = ToolRunner(RuntimeLimits(timeout_seconds=30, memory_mb=512))
        findings = await inspect_pdf_details(
            self.pdf,
            self.work(),
            runner,
            options=PdfCheckOptions(
                min_text_size_pt=10,
                printable_margins_pt=(70, 70, 70, 70),
                min_page_ink_ratio=0.00001,
                required_metadata=("Title",),
                expected_metadata=(("Author", "Ada Lovelace"),),
            ),
            budget=ResourceBudget(2, 512),
        )
        self.assertTrue(findings)
        self.assertTrue(all(item.status == "passed" for item in findings), findings)
        text, fields = await extract_pdf_evidence(self.pdf, self.work(), runner)
        self.assertIn("Body text", text)
        self.assertEqual(fields["Title"], "Paper")

    def test_options_reject_mutable_nonfinite_and_ambiguous_configuration(self) -> None:
        for kwargs in (
            {"min_text_size_pt": True},
            {"min_page_ink_ratio": 2},
            {"required_metadata": ["Title"]},
            {"printable_margins_pt": (0, 0, float("nan"), 0)},
            {"required_metadata": ("Rights",)},
            {"forbidden_action_types": ("/Launch",)},
            {"min_text_contrast": 4.5},
            {"min_text_contrast": 4.5, "contrast_background_rgb": (1, True, 1)},
            {"expected_metadata": (("Title", "One"), ("Title", "Two"))},
            {"text_size_regions": (PdfRegion("Region", 1, 0, 0, 10, 10),)},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(PreparationError):
                PdfCheckOptions(**kwargs)


if __name__ == "__main__":
    unittest.main()
