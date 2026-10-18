"""Check selection reaches the adapters: online, PDF detail, artwork and structure checks."""

from __future__ import annotations

import unittest
from dataclasses import replace
from unittest.mock import AsyncMock, patch

from latexprep.check_selection import CheckSelection
from latexprep.config import Settings, effective_settings
from latexprep.findings import Finding
from latexprep.online_checks import check_online_references
from latexprep.online_http_wire import Response
from latexprep.online_options import OnlineOptions
from latexprep.pdf_artwork import inspect_artwork_inputs, inspect_pdf_artwork
from latexprep.pdf_artwork_options import PdfArtworkOptions
from latexprep.pdf_check_options import PdfCheckOptions, PdfRegion, PdfSectionBudget
from latexprep.pdf_checks import inspect_included_pdf_figures, inspect_pdf_details
from latexprep.pdf_inspection import PdfInspectionOptions, configured_policy_rules
from latexprep.pdf_structure_checks import (
    HeadingExpectation,
    StructureOptions,
    inspect_pdf_structure,
)
from latexprep.rules import code_for
from tests.pdf_fakes import ArtworkRunner, DetailRunner, contrast_sample
from tests.support import AsyncProjectTestCase


def selection(*codes: str) -> CheckSelection:
    return CheckSelection(select=codes)


def codes(findings: list[Finding]) -> set[str]:
    return {finding.code for finding in findings if finding.code is not None}


def configured_artwork() -> PdfArtworkOptions:
    region = PdfRegion("Figure", 1, 5, 5, 95, 95)
    return PdfArtworkOptions(
        detect_printer_marks=True,
        trim_regions=(region,),
        printer_mark_max_length_pt=10,
        printer_mark_exemptions=(2,),
        figure_regions=(region,),
        max_figure_whitespace_ratio=0.8,
        figure_edge_band_pt=2,
        drawing_regions=(region,),
        check_clipping=True,
        classify_artwork=True,
        artwork_category="line",
        category_min_dpi=(("line", 600),),
        classify_type3_glyphs=True,
        forbid_bitmap_type3_glyphs=True,
        min_stroke_width_pt=0.3,
        raster_regions=(region,),
        min_raster_region_coverage=0.8,
        grayscale_preview=True,
        contrast_samples=(contrast_sample(),),
        raster_dpi=72,
    )


def configured_pdf_details() -> PdfCheckOptions:
    return PdfCheckOptions(
        section_budgets=(PdfSectionBudget("Body", 1, 1, 2),),
        min_text_size_pt=10,
        text_size_regions=(PdfRegion("Body", 1, 0, 0, 500, 700, 10),),
        printable_margins_pt=(20, 20, 20, 20),
        printable_regions=(PdfRegion("Body", 1, 20, 20, 500, 700),),
        overlap_min_area_ratio=0.2,
        min_page_ink_ratio=0.1,
        sparse_page_exemptions=(2,),
        required_metadata=("Title",),
        expected_metadata=(("Title", "Paper"),),
        require_tagged=True,
        require_language=True,
        expected_language="en-GB",
        forbidden_annotation_types=("Text",),
        forbidden_action_types=("JavaScript",),
        min_stroke_width_pt=0.3,
        allowed_color_spaces=("DeviceRGB",),
        min_text_contrast=4.5,
        contrast_background_rgb=(1.0, 1.0, 1.0),
        min_grayscale_luminance_difference=0.1,
        require_embedded_figure_fonts=True,
        forbid_type3_figure_fonts=True,
    )


class CheckPolicyAdapterTests(AsyncProjectTestCase):
    async def asyncSetUp(self) -> None:
        self.pdf = self.root / "paper.pdf"
        self.pdf.write_bytes(b"%PDF-fixture")

    async def test_all_seven_online_switches_project_without_granting_network(self) -> None:
        toggles = {
            "NET001": "online_doi_resolution",
            "NET002": "online_metadata",
            "NET003": "online_missing_doi",
            "NET004": "online_published_versions",
            "NET005": "online_notices",
            "NET006": "online_reference_links",
            "NET007": "online_replication_links",
        }
        options = OnlineOptions(
            **dict.fromkeys(toggles.values(), True),
            online_replication_urls=("https://example.org/code",),
        )
        self.assertEqual(
            codes(await check_online_references(self.root, options=options)), set(toggles)
        )
        for code in toggles:
            with self.subTest(code=code):
                effective = effective_settings(
                    Settings(checks=selection(code), online_checks=options)
                )
                with patch(
                    "latexprep.online_checks.collect_inputs",
                    side_effect=AssertionError("offline scan"),
                ):
                    findings = await check_online_references(
                        self.root, options=effective.online_checks
                    )
                self.assertEqual(codes(findings), {code})
                self.assertEqual(findings[0].status, "skipped")
                self.assertFalse(effective.online_checks.online)
                self.assertEqual(
                    effective.online_checks.online_replication_urls, options.online_replication_urls
                )

    async def test_ignored_online_check_performs_no_scan_dns_or_http(self) -> None:
        (self.root / "references.bib").write_text("@article{key, doi={10.1234/example}}\n")
        original = Settings(online_checks=OnlineOptions(online=True, online_doi_resolution=True))
        dns = AsyncMock(return_value=("93.184.216.34",))
        http = AsyncMock(return_value=Response(200))
        findings = await check_online_references(
            self.root, options=original.online_checks, resolver=dns, transport=http
        )
        # The check and the notice of what the requests shared (NET008).
        self.assertEqual(codes(findings), {"NET001", "NET008"})
        self.assertGreater(dns.await_count, 0)
        self.assertGreater(http.await_count, 0)
        effective = effective_settings(replace(original, checks=CheckSelection(ignore=("NET",))))
        with patch("latexprep.online_checks.collect_inputs", side_effect=AssertionError("scan")):
            self.assertEqual(
                await check_online_references(
                    self.root,
                    options=effective.online_checks,
                    resolver=AsyncMock(side_effect=AssertionError("DNS")),
                    transport=AsyncMock(side_effect=AssertionError("HTTP")),
                ),
                [],
            )

    async def test_each_pdf_detail_disables_other_adapters_and_keeps_paired_arguments_valid(
        self,
    ) -> None:
        original = Settings(
            pdf_checks=configured_pdf_details(), match_source_pdf_metadata=("author",)
        )
        expected = {f"PDF{number}" for number in range(201, 217)}
        before = await inspect_pdf_details(
            self.pdf, self.root / "details-all", DetailRunner(), options=original.pdf_checks
        )
        self.assertEqual(codes(before), expected - {"PDF212"})
        for code in expected - {"PDF212"}:
            with self.subTest(code=code):
                effective = effective_settings(replace(original, checks=selection(code)))
                runner = DetailRunner()
                findings = await inspect_pdf_details(
                    self.pdf, self.root / code, runner, options=effective.pdf_checks
                )
                self.assertEqual(codes(findings), {code})
                self.assertTrue(runner.calls)
                self.assertEqual(
                    effective.match_source_pdf_metadata,
                    original.match_source_pdf_metadata if code == "PDF207" else (),
                )
        disabled = effective_settings(replace(original, checks=selection()))
        runner = DetailRunner()
        self.assertEqual(
            await inspect_pdf_details(
                self.pdf, self.root / "details-off", runner, options=disabled.pdf_checks
            ),
            [],
        )
        self.assertEqual(
            await inspect_included_pdf_figures(
                (self.pdf,), self.root / "figures-off", runner, options=disabled.pdf_checks
            ),
            [],
        )
        self.assertEqual(runner.calls, [])
        fonts = effective_settings(replace(original, checks=selection("PDF212")))
        findings = await inspect_included_pdf_figures(
            (self.pdf,), self.root / "figures-on", runner, options=fonts.pdf_checks
        )
        self.assertEqual(codes(findings), {"PDF212"})
        self.assertIn("pdffonts", {call[0][0] for call in runner.calls})

    async def test_base_pdf_policy_constraints_are_removed_together(self) -> None:
        original = Settings(
            max_pages=10,
            pdf=PdfInspectionOptions(
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
            ),
        )
        expected = {"PDF001", *(f"PDF{number}" for number in range(101, 110))}
        self.assertEqual(
            {code_for(name) for name in configured_policy_rules(original.pdf, original.max_pages)},
            expected,
        )
        for code in expected:
            effective = effective_settings(replace(original, checks=selection(code)))
            self.assertEqual(
                {
                    code_for(name)
                    for name in configured_policy_rules(effective.pdf, effective.max_pages)
                },
                {code},
            )
        disabled = effective_settings(replace(original, checks=selection()))
        self.assertEqual(configured_policy_rules(disabled.pdf, disabled.max_pages), [])

    async def test_artwork_regions_and_rendering_stay_only_when_their_check_is_selected(
        self,
    ) -> None:
        original = Settings(
            rendered_pdf_artwork=configured_artwork(), included_figure_artwork=configured_artwork()
        )
        expected = {f"PDF{number}" for number in range(301, 313)}
        before = await inspect_pdf_artwork(
            self.pdf,
            self.root / "artwork-all",
            ArtworkRunner(),
            options=original.rendered_pdf_artwork,
        )
        self.assertEqual(codes(before), expected)
        for code in expected:
            with self.subTest(code=code):
                effective = effective_settings(replace(original, checks=selection(code)))
                self.assertEqual(effective.included_figure_artwork, effective.rendered_pdf_artwork)
                runner = ArtworkRunner()
                findings = await inspect_pdf_artwork(
                    self.pdf, self.root / code, runner, options=effective.rendered_pdf_artwork
                )
                self.assertEqual(codes(findings), {code})
                if code in {"PDF302", "PDF303"}:
                    self.assertEqual(
                        effective.rendered_pdf_artwork.figure_regions,
                        original.rendered_pdf_artwork.figure_regions,
                    )
                else:
                    self.assertEqual(effective.rendered_pdf_artwork.figure_regions, ())
                if code == "PDF308":
                    self.assertNotIn("pdftoppm", {call[0][0] for call in runner.calls})
        disabled = effective_settings(replace(original, checks=CheckSelection(ignore=("PDF3",))))
        runner = ArtworkRunner()
        self.assertEqual(
            await inspect_pdf_artwork(
                self.pdf, self.root / "artwork-off", runner, options=disabled.rendered_pdf_artwork
            ),
            [],
        )
        self.assertEqual(
            await inspect_artwork_inputs(
                (self.pdf,),
                self.root / "inputs-off",
                runner,
                options=disabled.included_figure_artwork,
            ),
            [],
        )
        self.assertEqual(runner.calls, [])

    async def test_structure_inspection_stops_before_tools_and_retains_other_structure_checks(
        self,
    ) -> None:
        original = Settings(
            structure_checks=StructureOptions(
                heading_expectations=(HeadingExpectation(1, "Introduction", "1"),),
                require_structure_tree=True,
                check_table_structure=True,
                check_link_structure=True,
                require_figure_alt=True,
                check_structure_references=True,
            )
        )
        expected = {f"PDF{number}" for number in range(401, 406)}
        before = await inspect_pdf_structure(
            self.pdf, self.root / "structure-all", DetailRunner(), original.structure_checks
        )
        self.assertEqual(codes(before), expected)
        for code in expected:
            effective = effective_settings(replace(original, checks=selection(code)))
            findings = await inspect_pdf_structure(
                self.pdf, self.root / code, DetailRunner(), effective.structure_checks
            )
            self.assertEqual(codes(findings), {code})
        disabled = effective_settings(replace(original, checks=selection()))
        runner = DetailRunner()
        self.assertEqual(
            await inspect_pdf_structure(
                self.pdf, self.root / "structure-off", runner, disabled.structure_checks
            ),
            [],
        )
        self.assertEqual(runner.calls, [])


if __name__ == "__main__":
    unittest.main()
