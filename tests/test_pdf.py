from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from latexprep.models import PreparationError
from latexprep.pdf import (
    PdfOptions,
    _changed_pixels,
    _is_type3,
    _page_count,
    _page_sizes,
    compare_pdfs,
    inspect_pdf,
)
from latexprep.runtime import CommandResult, ToolRunner


class FakePdfRunner(ToolRunner):
    def __init__(
        self,
        *,
        text_changed: bool = False,
        raster_changed: bool = False,
        pages_right: int = 1,
        missing: str | None = None,
    ) -> None:
        super().__init__()
        self.text_changed = text_changed
        self.raster_changed = raster_changed
        self.pages_right = pages_right
        self.missing = missing
        self.commands: list[list[str]] = []

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
        self.commands.append(argv)
        if argv[0] == self.missing:
            raise PreparationError(f"Missing tool: {argv[0]}")
        if argv[-1] == "-v":
            return CommandResult(
                99 if argv[0] == "pdfdetach" else 0,
                f"{argv[0]} version test\n",
                "",
                False,
                argv,
            )
        right = any(b"right" in path.read_bytes() for path in readonly_inputs)
        pages = self.pages_right if right else 1
        output = ""
        if argv[0] == "pdfinfo":
            output = f"Pages: {pages}\nEncrypted: no\nForm: none\nJavaScript: no\n"
            if "-box" in argv:
                output += "".join(
                    f"Page {page} size: 612 x 792 pts\n" for page in range(1, pages + 1)
                )
        elif argv[0] == "pdftotext":
            output = (("Changed\n" if right and self.text_changed else "Text\n") + "\f") * pages
        elif argv[0] == "pdftoppm":
            pixel = b"\0\0\0" if right and self.raster_changed else b"\xff\xff\xff"
            (workspace / (argv[-1] + ".ppm")).write_bytes(b"P6\n1 1\n255\n" + pixel)
        elif argv[0] == "pdffonts":
            output = (
                "name type encoding emb sub uni object ID\n---\n"
                "Test Type 1 Builtin yes yes no 3 0\n"
            )
        elif argv[0] == "pdfdetach":
            output = "0 embedded files\n"
        elif argv[0] == "pdfimages":
            output = (
                "page num type width height color comp bpc enc interp object ID "
                "x-ppi y-ppi size ratio\n---\n"
            )
        return CommandResult(0, output, "", False, argv)


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
        self.assertEqual(rendered.details["pixel_tolerance"], 0)
        self.assertFalse(rendered.details["semantic_equivalence_proven"])

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
        self.assertEqual(_page_count({"Pages": "5000"}), 5000)
        self.assertEqual(_page_count({"Pages": "300"}, maximum=300), 300)
        for fields, maximum in (({"Pages": "5001"}, 5000), ({"Pages": "301"}, 300)):
            with self.subTest(pages=fields["Pages"]), self.assertRaises(PreparationError):
                _page_count(fields, maximum=maximum)
        for fields in ({"Pages": "0"}, {"Pages": "many"}, {}):
            with self.subTest(fields=fields), self.assertRaises(PreparationError):
                _page_count(fields)

    def test_type3_font_detection_is_shared_and_exact(self) -> None:
        for value in ("Type 3", "Type3", "type  3", "TYPE\t3"):
            self.assertTrue(_is_type3(value), value)
        for value in ("Type 1", "Type 1C", "Type 30", "CID Type 3C", "TrueType", ""):
            self.assertFalse(_is_type3(value), value)

    def test_changed_pixels_matches_a_per_pixel_reference_across_chunk_boundaries(self) -> None:
        def reference(left: bytes, right: bytes, tolerance: int) -> int:
            return sum(
                any(abs(left[i + c] - right[i + c]) > tolerance for c in range(3))
                for i in range(0, len(left), 3)
            )

        # More than two 1024-pixel chunks, ending inside a partial chunk.
        pixels = 2 * 1024 + 517
        left = bytes((index * 7) % 256 for index in range(3 * pixels))
        right = bytearray(left)
        # Differences straddling chunk edges, in a lone channel, and within tolerance.
        for pixel, channel, delta in (
            (0, 0, 9),
            (1023, 2, 1),
            (1024, 1, 5),
            (1025, 0, 2),
            (2047, 1, 200),
            (2048, 2, 3),
            (pixels - 1, 0, 40),
        ):
            offset = 3 * pixel + channel
            right[offset] = (right[offset] + delta) % 256
        right = bytes(right)
        for tolerance in (0, 1, 2, 5, 255):
            with self.subTest(tolerance=tolerance):
                self.assertEqual(
                    _changed_pixels(left, right, tolerance), reference(left, right, tolerance)
                )
        self.assertEqual(_changed_pixels(left, left, 0), 0)
        self.assertEqual(_changed_pixels(b"", b"", 0), 0)

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

    def test_pathological_page_dimensions_are_rejected_before_rendering(self) -> None:
        with self.assertRaises(PreparationError):
            _page_sizes("Page 1 size: 1e308 x 792 pts", 1)
        with self.assertRaises(PreparationError):
            _page_sizes("Page 1 size: 1000000 x 1000000 pts", 1)
        with self.assertRaises(PreparationError):
            _page_sizes("Page 1 size: 612 x 792 pts", 2)
        with self.assertRaises(PreparationError):
            _page_sizes("Page 1 size: 612 x 792 pts\nPage 1 size: 700 x 800 pts", 1)
        with self.assertRaises(PreparationError):
            _page_sizes("Page 1 size: .e x 792 pts", 1)


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
        return CommandResult(0, output, "", False, argv)


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
            PdfOptions(expected_page_width_pt=612, expected_page_height_pt=792),
            runner=ConfigurablePdfRunner(outputs={"geometry": "Page 1 size: 1e308 x 792 pts\n"}),
        )
        self.assertEqual(findings["pdf.page_dimensions"].code, "PDF101")
        self.assertEqual(findings["pdf.page_dimensions"].status, "inconclusive")
        self.assertEqual(findings["pdf.page_dimensions"].severity, "error")
        self.assertIn("safe raster dimensions", findings["pdf.page_dimensions"].message)

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
        options = PdfOptions(
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
        options = PdfOptions(require_consistent_page_size=True, page_size_tolerance_pt=1)
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
        options = PdfOptions(min_image_dpi=300)
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

    async def test_font_embedding_violation_changes_status_and_zero_fonts_are_inconclusive(
        self,
    ) -> None:
        options = PdfOptions(require_embedded_fonts=True)
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
        options = PdfOptions(forbid_type3_fonts=True)
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
        options = PdfOptions(forbid_encryption=True)
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
        options = PdfOptions(forbid_forms=True)
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
        options = PdfOptions(forbid_javascript=True)
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
        options = PdfOptions(forbid_attachments=True)
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
        options = PdfOptions(
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
            ("pdffonts", PdfOptions(require_embedded_fonts=True), "pdf.font_embedding"),
            ("pdfimages", PdfOptions(min_image_dpi=300), "pdf.image_resolution"),
            ("pdfdetach", PdfOptions(forbid_attachments=True), "pdf.attachments_policy"),
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
                PdfOptions(**values)


if __name__ == "__main__":
    unittest.main()
