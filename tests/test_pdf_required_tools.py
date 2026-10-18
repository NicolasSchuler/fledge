"""Setup diagnostics name every external tool that a configured PDF check runs.

Each adapter module states the tools of its configured checks, and ``fledge doctor``
unions them. These tests run every adapter with controlled tool output and compare the
commands it started with that statement, so a check that gains a tool cannot be missed.
"""

from __future__ import annotations

import tempfile
import unittest
from collections.abc import Awaitable
from pathlib import Path

from latexprep.pdf_artwork import (
    PdfArtworkOptions,
    PdfContrastSample,
    inspect_pdf_artwork,
    required_artwork_tools,
)
from latexprep.pdf_checks import (
    PdfCheckOptions,
    inspect_included_pdf_figures,
    inspect_pdf_details,
    required_pdf_check_tools,
)
from latexprep.pdf_comparison import COMPARISON_TOOLS, compare_pdfs
from latexprep.pdf_inspection import (
    PdfInspectionOptions,
    inspect_pdf,
    required_inspection_tools,
)
from latexprep.pdf_structure_checks import (
    HeadingExpectation,
    StructureOptions,
    inspect_pdf_structure,
    required_structure_tools,
)
from latexprep.pdf_support import PDF_TOOLS, PdfRegion
from tests.test_pdf import FakePdfRunner
from tests.test_pdf_artwork import ArtworkRunner
from tests.test_pdf_checks import DetailRunner

REGION = PdfRegion("Area", 1, 10, 10, 90, 90)
TEXT_TRACE = (
    '<document><page number="1" mediabox="0 0 100 100">'
    '<fill_text transform="1 0 0 1 0 0"><span><g unicode="A" x="10" y="10"/></span></fill_text>'
    "</page></document>"
)


def commands_started(calls: list[list[str]]) -> set[str]:
    """The tools of the commands a runner saw, without the version probes."""
    return {argv[0] for argv in calls if argv[-1] not in {"-v", "--version"}}


class RequiredToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.pdf = self.root / "document.pdf"
        self.pdf.write_bytes(b"%PDF-fixture")
        self.runs = 0

    def work(self) -> Path:
        self.runs += 1
        return self.root / f"work-{self.runs}"

    def assert_tools(self, used: set[str], required: set[str], *, label: str) -> None:
        self.assertLessEqual(used, required, f"{label}: a started tool is not required")
        self.assertLessEqual(required, used, f"{label}: a required tool was never started")
        self.assertLessEqual(required, PDF_TOOLS)

    async def test_pdf_check_tools_match_the_commands_each_option_starts(self) -> None:
        cases = {
            "text size": PdfCheckOptions(min_text_size_pt=8),
            "printable bounds": PdfCheckOptions(printable_margins_pt=(10, 10, 10, 10)),
            "text overlap": PdfCheckOptions(overlap_min_area_ratio=0.5),
            "sparse pages": PdfCheckOptions(min_page_ink_ratio=0.01),
            "metadata": PdfCheckOptions(required_metadata=("Title",)),
            "tagging": PdfCheckOptions(require_tagged=True),
            "language": PdfCheckOptions(require_language=True),
            "annotations": PdfCheckOptions(forbidden_annotation_types=("Link",)),
            "actions": PdfCheckOptions(forbidden_action_types=("JavaScript",)),
            "stroke width": PdfCheckOptions(min_stroke_width_pt=0.1),
            "colour spaces": PdfCheckOptions(allowed_color_spaces=("DeviceGray",)),
            "contrast": PdfCheckOptions(
                min_text_contrast=4.5, contrast_background_rgb=(1.0, 1.0, 1.0)
            ),
            "grayscale separation": PdfCheckOptions(min_grayscale_luminance_difference=0.1),
        }
        for label, options in cases.items():
            runner = DetailRunner()
            with self.subTest(label):
                await inspect_pdf_details(self.pdf, self.work(), runner, options=options)
                self.assert_tools(
                    commands_started([call[0] for call in runner.calls]),
                    required_pdf_check_tools(options),
                    label=label,
                )
        self.assertEqual(required_pdf_check_tools(PdfCheckOptions()), set())

    async def test_combined_options_need_the_union_of_their_tools(self) -> None:
        options = PdfCheckOptions(
            min_text_size_pt=8,
            overlap_min_area_ratio=0.5,
            min_page_ink_ratio=0.01,
            required_metadata=("Title",),
            require_language=True,
            min_stroke_width_pt=0.1,
        )
        runner = DetailRunner()
        await inspect_pdf_details(self.pdf, self.work(), runner, options=options)
        self.assertEqual(
            required_pdf_check_tools(options),
            {"pdfinfo", "pdftohtml", "pdftoppm", "qpdf", "mutool"},
        )
        self.assert_tools(
            commands_started([call[0] for call in runner.calls]),
            required_pdf_check_tools(options),
            label="combined",
        )

    async def test_input_figure_font_policies_need_pdffonts(self) -> None:
        options = PdfCheckOptions(forbid_type3_figure_fonts=True)
        runner = DetailRunner()
        await inspect_included_pdf_figures(
            (self.pdf,), self.root / "figures", runner, options=options
        )
        self.assert_tools(
            commands_started([call[0] for call in runner.calls]),
            required_pdf_check_tools(options),
            label="figure fonts",
        )

    async def test_artwork_tools_match_the_commands_each_option_starts(self) -> None:
        contrast = PdfContrastSample(
            "Sample", PdfRegion("Ink", 1, 10, 10, 20, 20), PdfRegion("Paper", 1, 80, 80, 90, 90), 3
        )
        cases = {
            "printer marks": PdfArtworkOptions(
                detect_printer_marks=True, trim_regions=(REGION,), printer_mark_max_length_pt=5
            ),
            "whitespace": PdfArtworkOptions(
                figure_regions=(REGION,), max_figure_whitespace_ratio=0.5
            ),
            "edge ink": PdfArtworkOptions(figure_regions=(REGION,), figure_edge_band_pt=2),
            "drawing bounds": PdfArtworkOptions(drawing_regions=(REGION,)),
            "clipping": PdfArtworkOptions(check_clipping=True),
            "composition": PdfArtworkOptions(classify_artwork=True),
            "category resolution": PdfArtworkOptions(
                artwork_category="figure", category_min_dpi=(("figure", 100.0),)
            ),
            "type 3": PdfArtworkOptions(classify_type3_glyphs=True),
            "stroke width": PdfArtworkOptions(min_stroke_width_pt=0.1),
            "raster regions": PdfArtworkOptions(
                raster_regions=(REGION,), min_raster_region_coverage=0.5
            ),
            "grayscale preview": PdfArtworkOptions(grayscale_preview=True),
            "contrast": PdfArtworkOptions(contrast_samples=(contrast,)),
            "colour space": PdfArtworkOptions(required_color_space="rgb"),
        }
        for label, options in cases.items():
            runner = ArtworkRunner(trace=TEXT_TRACE)
            with self.subTest(label):
                await inspect_pdf_artwork(self.pdf, self.work(), runner, options=options)
                self.assert_tools(
                    commands_started([call[0] for call in runner.calls]),
                    required_artwork_tools(options),
                    label=label,
                )
        self.assertEqual(required_artwork_tools(PdfArtworkOptions()), set())

    async def test_structure_tools_match_the_commands_each_option_starts(self) -> None:
        cases = {
            "headings": StructureOptions(heading_expectations=(HeadingExpectation(1, "A", "1"),)),
            "tables": StructureOptions(check_table_structure=True),
            "links": StructureOptions(check_link_structure=True),
            "figure alternatives": StructureOptions(require_figure_alt=True),
            "references": StructureOptions(check_structure_references=True),
            "structure tree": StructureOptions(require_structure_tree=True),
        }
        for label, options in cases.items():
            runner = DetailRunner(info="Pages: 1\n", text="1 A\f")
            with self.subTest(label):
                await inspect_pdf_structure(self.pdf, self.work(), runner, options)
                self.assert_tools(
                    commands_started([call[0] for call in runner.calls]),
                    required_structure_tools(options),
                    label=label,
                )
        self.assertEqual(required_structure_tools(StructureOptions()), set())

    async def test_inspection_and_comparison_tools_match_what_they_start(self) -> None:
        other = self.root / "other.pdf"
        other.write_bytes(b"%PDF-other")
        for label, action, required in (
            (
                "inspection",
                lambda runner: inspect_pdf(
                    self.pdf, self.work(), runner, options=PdfInspectionOptions()
                ),
                required_inspection_tools(),
            ),
            (
                "comparison",
                lambda runner: compare_pdfs(self.pdf, other, self.work(), runner),
                set(COMPARISON_TOOLS),
            ),
        ):
            runner = FakePdfRunner()
            with self.subTest(label):
                result: Awaitable[object] = action(runner)
                await result
                self.assert_tools(commands_started(runner.commands), required, label=label)


if __name__ == "__main__":
    unittest.main()
