from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from latexprep.command_runner import CommandResult, RuntimeLimits
from latexprep.errors import PreparationError
from latexprep.pdf_comparison import ComparisonPolicy, PageDifference, compare_pdfs
from latexprep.pdf_geometry import COMPARISON_DPI, require_comparison_raster_fit
from latexprep.pdf_inspection import PdfInspectionOptions, inspect_pdf
from latexprep.pdf_tool_output import is_type3, page_count, parse_page_sizes
from latexprep.report import Report
from latexprep.resource_budget import ResourceBudget
from latexprep.runtime import ToolRunner
from latexprep.workflow_options import ComparisonOptions
from tests.pdf_fakes import FakePdfRunner
from tests.support import live_test


class PdfTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.left, self.right = self.root / "a.pdf", self.root / "b.pdf"
        self.left.write_bytes(b"%PDF-test-left")
        self.right.write_bytes(b"%PDF-test-right")

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    async def test_rendered_change_is_detected_when_text_matches(self) -> None:
        findings = await compare_pdfs(
            self.left, self.right, self.root / "compare", FakePdfRunner(raster_changed=True)
        )
        by_rule = {item.rule: item for item in findings}
        self.assertEqual(by_rule["compare.text"].status, "passed")
        self.assertEqual(by_rule["compare.rendering"].status, "failed")
        self.assertEqual(by_rule["compare.rendering"].details["changed_pages"], [1])
        self.assertEqual(by_rule["compare.rendering"].code, "CMP003")
        self.assertEqual(self.left.read_bytes(), b"%PDF-test-left")

    async def test_extracted_text_change_is_detected_when_raster_matches(self) -> None:
        findings = await compare_pdfs(
            self.left, self.right, self.root / "compare", FakePdfRunner(text_changed=True)
        )
        by_rule = {item.rule: item for item in findings}
        self.assertEqual(by_rule["compare.text"].status, "failed")
        self.assertEqual(by_rule["compare.text"].code, "CMP002")
        self.assertEqual(by_rule["compare.rendering"].status, "passed")

    async def test_page_count_difference_identifies_missing_pages(self) -> None:
        findings = await compare_pdfs(
            self.left, self.right, self.root / "compare", FakePdfRunner(pages_right=2)
        )
        by_rule = {item.rule: item for item in findings}
        self.assertEqual(by_rule["compare.page_count"].status, "failed")
        self.assertEqual(by_rule["compare.page_count"].code, "CMP001")
        self.assertEqual(by_rule["compare.rendering"].details["changed_pages"], [2])

    async def test_missing_renderer_is_inconclusive_and_blocking(self) -> None:
        findings = await compare_pdfs(
            self.left, self.right, self.root / "compare", FakePdfRunner(missing="pdftoppm")
        )
        unavailable = next(item for item in findings if item.rule == "compare.unavailable")
        self.assertEqual(unavailable.status, "inconclusive")
        self.assertEqual(unavailable.severity, "error")
        self.assertNotIn("compare.rendering", {item.rule for item in findings})

    async def test_matching_comparison_states_exact_tolerance_and_limits(self) -> None:
        findings = await compare_pdfs(self.left, self.right, self.root / "compare", FakePdfRunner())
        self.assertTrue(all(item.status == "passed" for item in findings))
        rendered = next(item for item in findings if item.rule == "compare.rendering")
        self.assertEqual(rendered.details["dpi"], 144)
        self.assertEqual(rendered.details["max_changed_pixel_ratio"], 0)
        self.assertEqual(rendered.details["channel_tolerance"], 0)
        self.assertFalse(rendered.details["semantic_equivalence_proven"])

    async def test_rendering_message_states_the_comparison_resolution_in_use(self) -> None:
        with patch("latexprep.pdf_comparison.COMPARISON_DPI", 96):
            findings = await compare_pdfs(
                self.left, self.right, self.root / "compare", FakePdfRunner()
            )
        rendered = next(item for item in findings if item.rule == "compare.rendering")
        self.assertEqual(rendered.details["dpi"], 96)
        self.assertIn("96 DPI", rendered.message)
        self.assertNotIn("144", rendered.message)

    def test_policy_names_its_tolerances_and_a_difference_reports_only_measured_fields(
        self,
    ) -> None:
        exact = ComparisonPolicy.of(None)
        self.assertFalse(exact.tolerant)
        self.assertEqual(
            exact.to_details(),
            {
                "dpi": COMPARISON_DPI,
                "max_changed_pixel_ratio": 0,
                "channel_tolerance": 0,
                "text_normalization": "line endings and trailing horizontal whitespace only",
                "metadata_compared": False,
                "semantic_equivalence_proven": False,
            },
        )
        options = ComparisonOptions(channel_tolerance=2, max_changed_pixel_ratio=0.25)
        tolerant = ComparisonPolicy.of(options)
        self.assertTrue(tolerant.tolerant)
        self.assertEqual(tolerant.to_details()["max_changed_pixel_ratio"], 0.25)
        unmeasured = PageDifference(2, (10, 12), (10, 12), False)
        self.assertEqual(
            unmeasured.to_report(),
            {
                "page": 2,
                "left_pixels": (10, 12),
                "right_pixels": (10, 12),
                "page_dimensions_changed": False,
            },
        )
        self.assertTrue(unmeasured.counts_as_changed)
        counted = PageDifference(2, (10, 12), (10, 12), False, 3, 0.025, True)
        self.assertFalse(counted.counts_as_changed)
        self.assertEqual(counted.to_report()["changed_pixel_ratio"], 0.025)
        self.assertIs(counted.to_report()["within_tolerance"], True)

    async def test_pdf_inspection_reports_scope_gaps(self) -> None:
        findings = await inspect_pdf(self.left, self.root / "inspect", FakePdfRunner())
        by_rule = {item.rule: item for item in findings}
        self.assertEqual(by_rule["pdf.fonts"].details["nonembedded"], [])
        scope = by_rule["pdf.annotation_media_scope"]
        self.assertEqual((scope.severity, scope.status), ("info", "skipped"))
        self.assertIn("absence is unverified", scope.message)
        self.assertEqual(by_rule["pdf.attachments"].status, "passed")

    async def test_clean_inspection_has_no_advisory_or_blocking_findings(self) -> None:
        findings = await inspect_pdf(self.left, self.root / "inspect", FakePdfRunner())
        self.assertEqual(
            [item.rule for item in findings if item.severity != "info" and item.status != "passed"],
            [],
        )

    async def test_comparison_rejects_documents_beyond_the_rendering_cap(self) -> None:
        findings = await compare_pdfs(
            self.left, self.right, self.root / "compare", FakePdfRunner(pages_right=301)
        )
        rules = {item.rule: item for item in findings}
        self.assertNotIn("compare.rendering", rules)
        unavailable = rules["compare.unavailable"]
        self.assertEqual((unavailable.severity, unavailable.status), ("error", "inconclusive"))
        self.assertIn("Rendered comparison supports at most 300 pages", unavailable.message)

    def test_page_count_bounds_are_explicit_per_purpose(self) -> None:
        self.assertEqual(page_count({"Pages": "5000"}), 5000)
        self.assertEqual(page_count({"Pages": "300"}, maximum=300), 300)
        for fields, maximum in (({"Pages": "5001"}, 5000), ({"Pages": "301"}, 300)):
            with self.subTest(pages=fields["Pages"]), self.assertRaises(PreparationError):
                page_count(fields, maximum=maximum)
        for fields in ({"Pages": "0"}, {"Pages": "many"}, {}):
            with self.subTest(fields=fields), self.assertRaises(PreparationError):
                page_count(fields)

    def test_type3_font_detection_is_shared_and_exact(self) -> None:
        for value in ("Type 3", "Type3", "type  3", "TYPE\t3"):
            self.assertTrue(is_type3(value), value)
        for value in ("Type 1", "Type 1C", "Type 30", "CID Type 3C", "TrueType", ""):
            self.assertFalse(is_type3(value), value)

    async def test_inspection_supports_documents_beyond_the_rendering_cap(self) -> None:
        findings = await inspect_pdf(
            self.left, self.root / "inspect", ConfigurablePdfRunner(pages=301)
        )
        by_rule = {item.rule: item for item in findings}
        self.assertEqual(by_rule["pdf.pages"].details["pages"], 301)
        self.assertNotIn("pdf.inspection_unavailable", by_rule)
        self.assertEqual(by_rule["pdf.text"].status, "passed")

    async def test_missing_pdf_inspection_tool_is_blocking(self) -> None:
        findings = await inspect_pdf(
            self.left, self.root / "inspect", FakePdfRunner(missing="pdffonts")
        )
        failure = next(item for item in findings if item.rule == "pdf.fonts_unavailable")
        self.assertEqual(failure.severity, "error")
        self.assertEqual(failure.status, "inconclusive")

    async def test_missing_pdfinfo_serializes_page_limit_as_incomplete(self) -> None:
        findings = await inspect_pdf(
            self.left, self.root / "inspect", FakePdfRunner(missing="pdfinfo"), max_pages=1
        )
        serialized = json.loads(json.dumps(Report("check", findings=findings).to_dict()))
        page_limit = next(item for item in serialized["findings"] if item["code"] == "PDF001")
        self.assertEqual(page_limit["status"], "inconclusive")
        self.assertIs(page_limit["incomplete"], True)
        self.assertIn("Missing tool: pdfinfo", page_limit["message"])

    def test_pathological_page_dimensions_are_rejected_before_rendering(self) -> None:
        with self.assertRaises(PreparationError):
            parse_page_sizes("Page 1 size: 1e308 x 792 pts", 1)
        with self.assertRaises(PreparationError):
            parse_page_sizes("Page 1 size: 1000000 x 1000000 pts", 1)
        with self.assertRaises(PreparationError):
            parse_page_sizes("Page 1 size: 612 x 792 pts", 2)
        with self.assertRaises(PreparationError):
            parse_page_sizes("Page 1 size: 612 x 792 pts\nPage 1 size: 700 x 800 pts", 1)
        with self.assertRaises(PreparationError):
            parse_page_sizes("Page 1 size: .e x 792 pts", 1)


def stencil_mask_pdf() -> bytes:
    """One page painting an 8 x 8 one-bit image mask, which pdfimages lists as a stencil."""
    content = b"q 100 0 0 100 10 10 cm /M Do Q\n"
    objects = (
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] "
        b"/Resources << /XObject << /M 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /XObject /Subtype /Image /Width 8 /Height 8 /ImageMask true "
        b"/BitsPerComponent 1 /Length 8 >>\nstream\n" + bytes([0b10101010] * 8) + b"\nendstream",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"endstream",
    )
    output = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, value in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode() + value + b"\nendobj\n")
    start = len(output)
    output.extend(b"xref\n0 6\n0000000000 65535 f \n")
    output.extend(b"".join(f"{offset:010} 00000 n \n".encode() for offset in offsets))
    output.extend(f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode())
    return bytes(output)


class ConfigurablePdfRunner(FakePdfRunner):
    def __init__(
        self, *, pages: int = 1, outputs: dict[str, str] | None = None, missing: str | None = None
    ) -> None:
        super().__init__(missing=missing)
        self.pages = pages
        self.outputs = outputs or {}

    async def run(
        self,
        argv: list[str],
        cwd: Path,
        workspace: Path,
        *,
        readonly_inputs: tuple[Path, ...] = (),
        memory_mb: int | None = None,
    ) -> CommandResult:
        result = await super().run(
            argv, cwd, workspace, readonly_inputs=readonly_inputs, memory_mb=memory_mb
        )
        if argv[-1] == "-v":
            return result
        key = "geometry" if argv[0] == "pdfinfo" and "-box" in argv else argv[0]
        defaults = {
            "pdfinfo": f"Pages: {self.pages}\nEncrypted: no\nForm: none\nJavaScript: no\n",
            "geometry": "".join(
                f"Page {page} size: 612 x 792 pts\n" for page in range(1, self.pages + 1)
            ),
            "pdftotext": "Text\n\f" * self.pages,
        }
        output = self.outputs.get(key, defaults.get(key, result.stdout))
        return CommandResult(0, output, "", resolved_command=argv)


class PdfConstraintTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.pdf = self.root / "input.pdf"
        self.pdf.write_bytes(b"%PDF-test")
        self.counter = 0

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    async def inspect(self, options=None, *, runner=None, max_pages=None):
        self.counter += 1
        findings = await inspect_pdf(
            self.pdf,
            self.root / f"inspection-{self.counter}",
            runner or ConfigurablePdfRunner(),
            max_pages,
            options=options,
        )
        return {item.rule: item for item in findings}

    async def test_huge_finite_dimensions_produce_incomplete_required_measurements(self) -> None:
        findings = await self.inspect(
            PdfInspectionOptions(expected_page_width_pt=612, expected_page_height_pt=792),
            runner=ConfigurablePdfRunner(outputs={"geometry": "Page 1 size: 1e308 x 792 pts\n"}),
        )
        self.assertEqual(findings["pdf.page_dimensions"].code, "PDF101")
        self.assertEqual(findings["pdf.page_dimensions"].status, "inconclusive")
        self.assertEqual(findings["pdf.page_dimensions"].severity, "error")
        self.assertIn("exceed", findings["pdf.page_dimensions"].message)
        self.assertNotIn("raster", findings["pdf.page_dimensions"].message)

    async def test_large_format_pages_are_inventoried_without_comparison_render_limits(
        self,
    ) -> None:
        # About A1: valid geometry that the 144 DPI comparison renderer cannot raster.
        a1 = "Page 1 size: 1684 x 2384 pts\n"
        findings = await self.inspect(
            PdfInspectionOptions(
                expected_page_width_pt=1684,
                expected_page_height_pt=2384,
                require_consistent_page_size=True,
            ),
            runner=ConfigurablePdfRunner(outputs={"geometry": a1}),
        )
        self.assertNotIn("pdf.geometry_unavailable", findings)
        self.assertEqual(findings["pdf.page_geometry"].status, "passed")
        self.assertEqual(findings["pdf.page_dimensions"].status, "passed")
        self.assertEqual(findings["pdf.page_size_consistency"].status, "passed")
        # The comparison path still refuses pages it cannot render, and says why.
        with self.assertRaisesRegex(PreparationError, f"{COMPARISON_DPI} DPI"):
            require_comparison_raster_fit(parse_page_sizes(a1, 1))

    async def test_page_limit_boundary_and_overflow(self) -> None:
        at_limit = await self.inspect(max_pages=2, runner=ConfigurablePdfRunner(pages=2))
        self.assertEqual(at_limit["pdf.page_limit"].status, "passed")
        overflow = await self.inspect(max_pages=1, runner=ConfigurablePdfRunner(pages=2))
        finding = overflow["pdf.page_limit"]
        self.assertEqual(finding.status, "failed")
        self.assertEqual(finding.severity, "error")
        self.assertEqual(finding.details["pages"], 2)
        self.assertEqual(finding.details["maximum"], 1)
        self.assertEqual(finding.code, "PDF001")
        self.assertEqual(overflow["pdf.pages"].status, "passed")
        unavailable = await self.inspect(
            max_pages=2, runner=ConfigurablePdfRunner(outputs={"pdfinfo": "Pages: 0\n"})
        )
        self.assertEqual(unavailable["pdf.page_limit"].status, "inconclusive")
        self.assertEqual(unavailable["pdf.page_limit"].severity, "error")

    async def test_expected_dimensions_measure_every_page_with_pdf_point_tolerance(self) -> None:
        options = PdfInspectionOptions(
            expected_page_width_pt=612, expected_page_height_pt=792, page_size_tolerance_pt=1
        )
        exact = await self.inspect(
            options,
            runner=ConfigurablePdfRunner(
                pages=2,
                outputs={"geometry": "Page 1 size: 611 x 793 pts\nPage 2 size: 613 x 791 pts\n"},
            ),
        )
        self.assertEqual(exact["pdf.page_dimensions"].status, "passed")
        wrong = await self.inspect(
            options,
            runner=ConfigurablePdfRunner(
                pages=2,
                outputs={"geometry": "Page 1 size: 612 x 792 pts\nPage 2 size: 792 x 612 pts\n"},
            ),
        )
        finding = wrong["pdf.page_dimensions"]
        self.assertEqual(finding.status, "failed")
        self.assertEqual(finding.details["differing_pages"], [2])
        self.assertIn("1/72", finding.details["unit"])
        self.assertEqual(finding.code, "PDF101")
        missing = await self.inspect(
            options,
            runner=ConfigurablePdfRunner(
                pages=2, outputs={"geometry": "Page 1 size: 612 x 792 pts\n"}
            ),
        )
        self.assertEqual(missing["pdf.page_dimensions"].status, "inconclusive")

    async def test_page_consistency_uses_full_range_not_just_first_page(self) -> None:
        options = PdfInspectionOptions(require_consistent_page_size=True, page_size_tolerance_pt=1)
        same = await self.inspect(options, runner=ConfigurablePdfRunner(pages=3))
        self.assertEqual(same["pdf.page_size_consistency"].status, "passed")
        changed = await self.inspect(
            options,
            runner=ConfigurablePdfRunner(
                pages=3,
                outputs={
                    "geometry": (
                        "Page 1 size: 612 x 792 pts\nPage 2 size: 611 x 792 pts\n"
                        "Page 3 size: 613 x 792 pts\n"
                    )
                },
            ),
        )
        finding = changed["pdf.page_size_consistency"]
        self.assertEqual(finding.status, "failed")
        self.assertEqual(finding.details["range_pt"], (2.0, 0.0))
        self.assertEqual(finding.code, "PDF102")

    async def test_image_resolution_checks_both_axes_and_repeated_placements(self) -> None:
        options = PdfInspectionOptions(min_image_dpi=300)
        header = (
            "page num type width height color comp bpc enc interp object ID "
            "x-ppi y-ppi size ratio\n---\n"
        )
        boundary = header + "1 0 image 600 600 rgb 3 8 image no 4 0 300 300 10K 2%\n"
        good = await self.inspect(
            options, runner=ConfigurablePdfRunner(outputs={"pdfimages": boundary})
        )
        self.assertEqual(good["pdf.image_resolution"].status, "passed")
        unequal = boundary + "2 1 image 600 600 rgb 3 8 image no 4 0 600 299 10K 2%\n"
        bad = await self.inspect(
            options, runner=ConfigurablePdfRunner(pages=2, outputs={"pdfimages": unequal})
        )
        finding = bad["pdf.image_resolution"]
        self.assertEqual(finding.status, "failed")
        self.assertEqual(finding.details["below_minimum"][0]["page"], 2)
        self.assertEqual(finding.details["below_minimum"][0]["y_dpi"], 299)
        self.assertEqual(finding.details["inspected_placements"], 2)
        self.assertEqual(finding.code, "PDF103")
        unknown = await self.inspect(
            options,
            runner=ConfigurablePdfRunner(outputs={"pdfimages": boundary.replace("300 300", "0 0")}),
        )
        self.assertEqual(unknown["pdf.image_resolution"].status, "inconclusive")
        vector_only = await self.inspect(
            options, runner=ConfigurablePdfRunner(outputs={"pdfimages": header})
        )
        self.assertEqual(vector_only["pdf.image_resolution"].status, "passed")
        self.assertEqual(vector_only["pdf.image_resolution"].details["inspected_placements"], 0)

    async def test_stencil_masks_are_measured_like_other_raster_placements(self) -> None:
        header = (
            "page num type width height color comp bpc enc interp object ID "
            "x-ppi y-ppi size ratio\n---\n"
        )
        # A row copied from pdfimages -list for a 1-bit /ImageMask image.
        stencil = "1 0 stencil 8 8 - 1 1 image no 4 0 6 6 8B 100%\n"
        for options in (PdfInspectionOptions(), PdfInspectionOptions(min_image_dpi=300)):
            findings = await self.inspect(
                options, runner=ConfigurablePdfRunner(outputs={"pdfimages": header + stencil})
            )
            with self.subTest(options=options):
                self.assertNotIn("pdf.images_unavailable", findings)
                self.assertEqual(findings["pdf.images"].details["images"][0]["type"], "stencil")
        low = (
            await self.inspect(
                PdfInspectionOptions(min_image_dpi=300),
                runner=ConfigurablePdfRunner(outputs={"pdfimages": header + stencil}),
            )
        )["pdf.image_resolution"]
        self.assertEqual(low.status, "failed")
        self.assertEqual(low.details["below_minimum"][0]["type"], "stencil")

    @live_test("Opt-in live PDF tools")
    async def test_live_pdfimages_inventory_accepts_a_real_stencil_mask(self) -> None:
        self.pdf.write_bytes(stencil_mask_pdf())
        findings = await inspect_pdf(
            self.pdf,
            self.root / "live-stencil",
            ToolRunner(RuntimeLimits(timeout_seconds=30, max_memory_mb=512)),
            options=PdfInspectionOptions(min_image_dpi=1),
            budget=ResourceBudget(2, 512),
        )
        rules = {item.rule: item for item in findings}
        self.assertNotIn("pdf.images_unavailable", rules)
        self.assertEqual(rules["pdf.images"].details["images"][0]["type"], "stencil")
        self.assertEqual(rules["pdf.image_resolution"].status, "passed")

    async def test_font_embedding_violation_changes_status_and_zero_fonts_are_inconclusive(
        self,
    ) -> None:
        options = PdfInspectionOptions(require_embedded_fonts=True)
        good = await self.inspect(options)
        self.assertEqual(good["pdf.font_embedding"].status, "passed")
        header = "name type encoding emb sub uni object ID\n---\n"
        bad = await self.inspect(
            options,
            runner=ConfigurablePdfRunner(
                outputs={"pdffonts": header + "Test Type 1 Builtin no no no 3 0\n"}
            ),
        )
        finding = bad["pdf.font_embedding"]
        self.assertEqual(finding.status, "failed")
        self.assertEqual(finding.details["nonembedded"], ["Test"])
        self.assertEqual(finding.code, "PDF104")
        self.assertEqual(bad["pdf.fonts"].status, "failed")
        self.assertEqual(bad["pdf.fonts"].severity, "warning")
        empty = await self.inspect(
            options, runner=ConfigurablePdfRunner(outputs={"pdffonts": header, "pdftotext": "\f"})
        )
        self.assertEqual(empty["pdf.font_embedding"].status, "inconclusive")
        self.assertEqual(empty["pdf.text"].status, "inconclusive")
        self.assertEqual(empty["pdf.text"].details["pages_without_text"], [1])

    async def test_forbidden_type3_font_policy_detects_only_type3(self) -> None:
        options = PdfInspectionOptions(forbid_type3_fonts=True)
        good = await self.inspect(options)
        self.assertEqual(good["pdf.type3_fonts"].status, "passed")
        fonts = (
            "name type encoding emb sub uni object ID\n---\nBitmap Type 3 Custom yes no no 7 0\n"
        )
        bad = await self.inspect(options, runner=ConfigurablePdfRunner(outputs={"pdffonts": fonts}))
        finding = bad["pdf.type3_fonts"]
        self.assertEqual(finding.status, "failed")
        self.assertEqual(finding.details["type3_fonts"][0]["name"], "Bitmap")
        self.assertEqual(finding.code, "PDF105")

    async def test_encryption_policy_uses_explicit_reported_feature(self) -> None:
        options = PdfInspectionOptions(forbid_encryption=True)
        good = await self.inspect(options)
        self.assertEqual(good["pdf.encryption_policy"].status, "passed")
        bad = await self.inspect(
            options,
            runner=ConfigurablePdfRunner(
                outputs={"pdfinfo": "Pages: 1\nEncrypted: yes (print:yes copy:no)\n"}
            ),
        )
        finding = bad["pdf.encryption_policy"]
        self.assertEqual(finding.status, "failed")
        self.assertEqual(finding.severity, "error")
        self.assertIn("copy:no", finding.details["value"])
        self.assertEqual(finding.code, "PDF106")

    async def test_forms_policy_detects_acroform_and_xfa(self) -> None:
        options = PdfInspectionOptions(forbid_forms=True)
        good = await self.inspect(options)
        self.assertEqual(good["pdf.forms_policy"].status, "passed")
        for form in ("AcroForm", "XFA"):
            bad = await self.inspect(
                options,
                runner=ConfigurablePdfRunner(outputs={"pdfinfo": f"Pages: 1\nForm: {form}\n"}),
            )
            finding = bad["pdf.forms_policy"]
            self.assertEqual(finding.status, "failed")
            self.assertEqual(finding.details["value"], form)
            self.assertEqual(finding.code, "PDF107")

    async def test_javascript_policy_requires_explicit_reported_value(self) -> None:
        options = PdfInspectionOptions(forbid_javascript=True)
        good = await self.inspect(options)
        self.assertEqual(good["pdf.javascript_policy"].status, "passed")
        bad = await self.inspect(
            options,
            runner=ConfigurablePdfRunner(outputs={"pdfinfo": "Pages: 1\nJavaScript: yes\n"}),
        )
        finding = bad["pdf.javascript_policy"]
        self.assertEqual(finding.status, "failed")
        self.assertEqual(finding.details["value"], "yes")
        self.assertEqual(finding.code, "PDF108")
        unknown = await self.inspect(
            options,
            runner=ConfigurablePdfRunner(outputs={"pdfinfo": "Pages: 1\nJavaScript: unknown\n"}),
        )
        self.assertEqual(unknown["pdf.javascript_policy"].status, "inconclusive")
        self.assertEqual(unknown["pdf.javascript_policy"].severity, "error")

    async def test_attachments_policy_counts_embedded_files(self) -> None:
        options = PdfInspectionOptions(forbid_attachments=True)
        good = await self.inspect(options)
        self.assertEqual(good["pdf.attachments_policy"].status, "passed")
        bad = await self.inspect(
            options,
            runner=ConfigurablePdfRunner(
                outputs={"pdfdetach": "1 embedded files\n1: supplement.dat\n"}
            ),
        )
        finding = bad["pdf.attachments_policy"]
        self.assertEqual(finding.status, "failed")
        self.assertEqual(finding.details["count"], 1)
        self.assertEqual(finding.code, "PDF109")
        incomplete = await self.inspect(
            options, runner=ConfigurablePdfRunner(outputs={"pdfdetach": "1 embedded files\n"})
        )
        self.assertEqual(incomplete["pdf.attachments_policy"].status, "inconclusive")

    async def test_each_configured_policy_is_inconclusive_when_pdfinfo_is_unavailable(self) -> None:
        options = PdfInspectionOptions(
            expected_page_width_pt=612,
            expected_page_height_pt=792,
            require_consistent_page_size=True,
            min_image_dpi=300,
            require_embedded_fonts=True,
            forbid_type3_fonts=True,
            forbid_encryption=True,
            forbid_forms=True,
            forbid_javascript=True,
            forbid_attachments=True,
        )
        findings = await self.inspect(
            options, max_pages=2, runner=ConfigurablePdfRunner(missing="pdfinfo")
        )
        rules = {
            "pdf.page_limit",
            "pdf.page_dimensions",
            "pdf.page_size_consistency",
            "pdf.image_resolution",
            "pdf.font_embedding",
            "pdf.type3_fonts",
            "pdf.encryption_policy",
            "pdf.forms_policy",
            "pdf.javascript_policy",
            "pdf.attachments_policy",
        }
        for rule in rules:
            with self.subTest(rule=rule):
                self.assertEqual(findings[rule].status, "inconclusive")
                self.assertEqual(findings[rule].severity, "error")
        self.assertNotIn("pdf.pages", findings)

    async def test_empty_or_malformed_inventory_output_is_not_a_policy_pass(self) -> None:
        for tool, options, rule in (
            ("pdffonts", PdfInspectionOptions(require_embedded_fonts=True), "pdf.font_embedding"),
            ("pdfimages", PdfInspectionOptions(min_image_dpi=300), "pdf.image_resolution"),
            ("pdfdetach", PdfInspectionOptions(forbid_attachments=True), "pdf.attachments_policy"),
        ):
            for output in ("", "unrecognized rows\n"):
                with self.subTest(tool=tool, output=output):
                    result = await self.inspect(
                        options, runner=ConfigurablePdfRunner(outputs={tool: output})
                    )
                    self.assertEqual(result[rule].status, "inconclusive")
                    self.assertEqual(result[rule].severity, "error")

    async def test_unconfigured_constraints_do_not_report_compliance(self) -> None:
        findings = await self.inspect()
        self.assertNotIn("pdf.page_limit", findings)
        self.assertNotIn("pdf.font_embedding", findings)
        self.assertNotIn("pdf.image_resolution", findings)
        self.assertNotIn("pdf.forms_policy", findings)
        self.assertEqual(findings["pdf.fonts"].status, "passed")

    async def test_partial_text_extraction_is_not_a_readability_pass(self) -> None:
        findings = await self.inspect(
            runner=ConfigurablePdfRunner(pages=2, outputs={"pdftotext": "Only one page\f"})
        )
        self.assertEqual(findings["pdf.text_unavailable"].status, "inconclusive")
        self.assertNotIn("pdf.text", findings)

    def test_invalid_pdf_constraint_values_are_rejected(self) -> None:
        for values in (
            {"expected_page_width_pt": 612},
            {"min_image_dpi": 0},
            {"min_image_dpi": float("nan")},
            {"page_size_tolerance_pt": -1},
            {"page_size_tolerance_pt": float("inf")},
            {"forbid_forms": "yes"},
        ):
            with self.subTest(values=values), self.assertRaises(PreparationError):
                PdfInspectionOptions(**values)


if __name__ == "__main__":
    unittest.main()
