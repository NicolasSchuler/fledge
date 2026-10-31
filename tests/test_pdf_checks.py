"""The manuscript-PDF detail checks, measured from controlled and live PDF tool output.

The checks cover section budgets, text sizes and regions, printable bounds, overlaps, sparse
pages, metadata, language, tagging, annotation and action policies, figure fonts, stroke
widths, colour spaces, contrast and grayscale separation, together with their caps, the shared
resource budget, cancellation and the option validation.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from collections import Counter
from dataclasses import replace
from itertools import chain, combinations, islice
from pathlib import Path

from latexprep.command_runner import CommandResult, RuntimeLimits
from latexprep.errors import PreparationError
from latexprep.measurement_findings import MAX_SAMPLES
from latexprep.pdf_check_options import PdfCheckOptions, PdfRegion, PdfSectionBudget
from latexprep.pdf_check_pipeline import PdfCheckSettings, inspect_standalone_pdf
from latexprep.pdf_checks import (
    extract_pdf_evidence,
    inspect_included_pdf_figures,
    inspect_pdf_details,
)
from latexprep.pdf_geometry import (
    MAX_MEASUREMENT_PIXELS,
    MAX_PAIR_CHECKS,
    MIN_MEASUREMENT_DPI,
    relative_luminance,
)
from latexprep.pdf_inspection import PdfInspectionOptions
from latexprep.pdf_paint_checks import MAX_PAGE_PALETTE_COLORS
from latexprep.pdf_structure_checks import HeadingExpectation, StructureOptions
from latexprep.pdf_tool_output import MAX_PARSED_NODES
from latexprep.resource_budget import ResourceBudget
from latexprep.runtime import ToolRunner
from latexprep.submission_options import SubmissionOptions
from tests.pdf_fakes import (
    DetailRunner,
    geometry,
    gray_raster,
    object_json,
    text_xml,
    trace_xml,
)
from tests.support import live_test


def large_page_geometry(width: int, height: int) -> str:
    """Return the ``pdfinfo -box`` output of one unrotated page of the given size.

    Args:
        width: The page width in points, used for the size, MediaBox and CropBox.
        height: The page height in points, used for the size, MediaBox and CropBox.

    Returns:
        The page size, rotation, MediaBox and CropBox lines of page 1.
    """
    return (
        f"Page 1 size: {width} x {height} pts\nPage 1 rot: 0\n"
        f"Page 1 MediaBox: 0 0 {width} {height}\nPage 1 CropBox: 0 0 {width} {height}\n"
    )


def color_trace(
    *,
    text_color: str = "0 0 0",
    other_color: str = "1 1 1",
    space: str = "DeviceRGB",
    alpha: str = "1",
) -> str:
    """Return a mutool trace of one letter page with one filled glyph and one filled path.

    Args:
        text_color: The colour components of the glyph ``A``.
        other_color: The ``DeviceRGB`` colour components of the filled path.
        space: The colour space of the glyph.
        alpha: The alpha of the glyph.

    Returns:
        The trace XML of ``document.pdf``.
    """
    return (
        '<document filename="document.pdf"><page number="1" mediabox="0 0 612 792">'
        f'<fill_text colorspace="{space}" color="{text_color}" alpha="{alpha}" '
        'transform="1 0 0 1 0 0"><span font="Times" wmode="0" trm="12 0 0 12">'
        '<g unicode="A" glyph="A" x="20" y="30"/></span></fill_text>'
        f'<fill_path colorspace="DeviceRGB" color="{other_color}" transform="1 0 0 1 0 0">'
        '<moveto x="20" y="40"/><lineto x="50" y="40"/></fill_path></page></document>'
    )


def simple_pdf() -> bytes:
    """Return a one-page PDF with Helvetica body text, a language and Title and Author fields.

    The catalogue declares ``en-GB``, the page is letter-sized and shows "Body text", and the
    information dictionary names the title "Paper" and the author "Ada Lovelace".

    Returns:
        The bytes of the PDF, with a cross-reference table and a trailer.
    """
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


class CountingToolRunner(ToolRunner):
    """The real runner, counting every command that is not a version probe.

    Args:
        limits: The runtime limits, passed to :class:`ToolRunner`.

    Attributes:
        counts: How often each command line ran, apart from the version probes.
    """

    def __init__(self, limits: RuntimeLimits | None = None) -> None:
        super().__init__(limits)
        self.counts: Counter[tuple[str, ...]] = Counter()

    async def run(
        self,
        argv: list[str],
        cwd: Path,
        workspace: Path,
        *,
        readonly_inputs: tuple[Path, ...] = (),
        memory_mb: int | None = None,
    ) -> CommandResult:
        """Count the command unless it ends with ``-v`` or ``--version``, then run it.

        The arguments are those of :meth:`CommandRunner.run`; the result is that of the real
        runner.
        """
        if argv[-1] not in {"-v", "--version"}:
            self.counts[tuple(argv)] += 1
        return await super().run(
            argv, cwd, workspace, readonly_inputs=readonly_inputs, memory_mb=memory_mb
        )


class PdfDetailTests(unittest.IsolatedAsyncioTestCase):
    """The detail checks, the figure-font checks and the evidence helper on one PDF.

    Most tests answer the tool commands with :class:`DetailRunner`; the live tests run Poppler
    on :func:`simple_pdf`.
    """

    async def asyncSetUp(self) -> None:
        """Create a temporary directory holding a placeholder ``document.pdf``."""
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.pdf = self.root / "document.pdf"
        self.pdf.write_bytes(b"%PDF-fixture")
        self.sequence = 0

    async def asyncTearDown(self) -> None:
        """Remove the temporary directory."""
        self.temporary.cleanup()

    def work(self) -> Path:
        """Return a new, not yet created work directory path below the temporary root."""
        self.sequence += 1
        return self.root / f"inspect-{self.sequence}"

    async def check(self, options: PdfCheckOptions, runner: DetailRunner | None = None):
        """Run the detail checks on ``document.pdf`` in a new work directory.

        Args:
            options: The detail checks to run.
            runner: The runner that answers the tool commands; a default
                :class:`DetailRunner` when ``None``.

        Returns:
            The findings of :func:`inspect_pdf_details`.
        """
        return await inspect_pdf_details(
            self.pdf, self.work(), runner or DetailRunner(), options=options
        )

    async def assert_status(self, options: PdfCheckOptions, status: str, **outputs: object):
        """Assert that the checks produce exactly one finding, with the given status.

        Args:
            options: The detail checks to run.
            status: The expected status of the finding.
            **outputs: Tool outputs that replace the defaults of :class:`DetailRunner`.

        Returns:
            The one finding.
        """
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
        self.assertEqual(failure.evidence_kind, "heuristic")
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

    async def test_documents_beyond_the_rendering_cap_keep_non_rendering_checks(self) -> None:
        many = geometry(pages=301)
        result = await self.check(
            PdfCheckOptions(required_metadata=("Title",), min_page_ink_ratio=0.1),
            DetailRunner(info="Pages: 301\nTitle: Paper\n", geometry=many),
        )
        states = {finding.rule: finding.status for finding in result}
        self.assertEqual(states["pdf.required_metadata"], "passed")
        sparse = next(finding for finding in result if finding.rule == "pdf.sparse_pages")
        self.assertEqual(sparse.status, "inconclusive")
        self.assertIn("supports at most 300 pages", sparse.message)
        self.assertEqual(len([f for f in result if f.rule == "pdf.sparse_pages"]), 1)
        unreadable = await self.check(
            PdfCheckOptions(required_metadata=("Title",)),
            DetailRunner(info="Pages: 5001\nTitle: Paper\n"),
        )
        self.assertEqual([item.status for item in unreadable], ["inconclusive"])

    async def test_poppler_error_reports_with_or_without_offsets_make_evidence_inconclusive(
        self,
    ) -> None:
        options = PdfCheckOptions(required_metadata=("Title",))
        for stderr in (
            "Syntax Error (257): Unknown compression method in flate stream\n",
            "Syntax Error: Unknown operator 'BAD'\n",
            "I/O Error: Couldn't open file 'document.pdf'\n",
            "Fontconfig error: Cannot load default config file\n",
            "error: cannot find page tree\n",
            "Config Error: x\n",
            "Command Line Error: Wrong page range given\n",
            "Permission Error: Copying of text from this document is not allowed\n",
            "Internal Error: x\n",
            "pdftoppm: Error: x\n",
            "    error: cannot load page\n",
            "mutool: error: x\n",
            "Warning: harmless first line\nSyntax Error (257): x\n",
        ):
            with self.subTest(stderr=stderr):
                finding = await self.assert_status(options, "inconclusive", stderr=stderr)
                self.assertIn("damaged or incomplete", finding.message)
        # A repaired cross-reference table or a gap the tool worked around is not damage.
        for stderr in (
            "Syntax Warning: No valid XRef size in trailer\n",
            "Syntax Warning: x\n",
            "Unimplemented Feature: x\n",
            "warning: ignoring zero sized extgstate\n",
        ):
            with self.subTest(stderr=stderr):
                await self.assert_status(options, "passed", stderr=stderr)

    async def test_large_format_pages_keep_text_geometry_and_sparse_page_measurements(self) -> None:
        # About A1: valid for measurement, yet beyond the 144 DPI comparison render limits.
        width, height = 1684, 2384
        geometry_output = large_page_geometry(width, height)
        xml = text_xml().replace('height="792" width="612"', f'height="{height}" width="{width}"')
        await self.assert_status(
            PdfCheckOptions(min_text_size_pt=10),
            "passed",
            geometry=geometry_output,
            xml=xml,
        )
        sparse = await self.assert_status(
            PdfCheckOptions(min_page_ink_ratio=0.1),
            "passed",
            geometry=geometry_output,
            raster=gray_raster(width=842, height=1192),
        )
        self.assertIn(f"{MAX_MEASUREMENT_PIXELS:,}", sparse.details["scope"])

    async def test_page_beyond_the_measurement_raster_names_limit_and_setting(self) -> None:
        # A0 at the highest allowed DPI needs far more than the measurement pixel limit.
        options = PdfCheckOptions(min_page_ink_ratio=0.1, raster_dpi=144)
        finding = await self.assert_status(
            options, "inconclusive", geometry=large_page_geometry(2384, 3370)
        )
        self.assertIn(f"{MAX_MEASUREMENT_PIXELS:,}", finding.message)
        self.assertIn("raster_dpi", finding.message)
        self.assertEqual(finding.details["page"], 1)

    async def test_sparse_page_tool_failure_is_one_finding_naming_the_pages(self) -> None:
        options = PdfCheckOptions(min_page_ink_ratio=0.1)
        result = await self.check(
            options,
            DetailRunner(missing="pdftoppm", info="Pages: 3\n", geometry=geometry(pages=3)),
        )
        self.assertEqual(len(result), 1, result)
        self.assertEqual(result[0].status, "inconclusive")
        self.assertEqual(result[0].details["pages"], [1, 2, 3])
        self.assertIn("pages 1-3", result[0].message)
        self.assertNotIn("page", result[0].details)

    async def test_outcome_text_names_section_page_counts_and_budgets(self) -> None:
        options = PdfCheckOptions(section_budgets=(PdfSectionBudget("Main text", 1, 23, 20),))
        failure = await self.assert_status(options, "failed", info="Pages: 25\n")
        self.assertEqual(
            failure.message, "Main text spans 23 pages (pages 1-23), over its budget of 20."
        )
        self.assertEqual(
            failure.details["violations"],
            [
                {
                    "section": "Main text",
                    "pages": 23,
                    "maximum": 20,
                    "reason": "Main text: 23 pages against a budget of 20",
                }
            ],
        )
        outside = await self.assert_status(options, "inconclusive", info="Pages: 10\n")
        self.assertIn("only 10 pages", outside.message)
        within = await self.assert_status(
            PdfCheckOptions(section_budgets=(PdfSectionBudget("Main text", 1, 20, 20),)),
            "passed",
            info="Pages: 25\n",
        )
        self.assertEqual(within.message, "Main text spans 20 pages (pages 1-20); the budget is 20.")

    async def test_a_one_page_section_and_a_one_page_pdf_are_not_pluralized(self) -> None:
        single = await self.assert_status(
            PdfCheckOptions(section_budgets=(PdfSectionBudget("Abstract", 1, 1, 1),)),
            "passed",
            info="Pages: 4\n",
        )
        self.assertEqual(single.message, "Abstract spans 1 page (page 1); the budget is 1.")
        outside = await self.assert_status(
            PdfCheckOptions(section_budgets=(PdfSectionBudget("Appendix", 2, 3, 1),)),
            "inconclusive",
            info="Pages: 1\n",
        )
        self.assertIn("has only 1 page;", outside.message)

    async def test_sparse_page_outcome_states_measured_and_minimum_coverage(self) -> None:
        options = PdfCheckOptions(min_page_ink_ratio=0.1)
        failure = await self.assert_status(options, "failed", raster=gray_raster(blank=True))
        self.assertEqual(
            failure.message, "Page 1 has 0% inked pixels, below the configured minimum of 10%."
        )
        [violation] = failure.details["violations"]
        self.assertEqual((violation["page"], violation["ink_ratio"]), (1, 0))
        self.assertEqual(violation["reason"], "0% inked pixels against a minimum of 10%")
        passed = await self.assert_status(options, "passed")
        self.assertEqual(passed.message, "Page 1 has 25% inked pixels; the minimum is 10%.")

    async def test_language_violations_state_declared_and_expected_values(self) -> None:
        options = PdfCheckOptions(require_language=True, expected_language="en-GB")
        mismatch = await self.assert_status(
            options, "failed", objects=object_json(language="u:de-DE")
        )
        self.assertEqual(
            mismatch.details["violations"],
            ["Catalog language 'de-DE' differs from the configured language 'en-GB'."],
        )
        missing = await self.assert_status(options, "failed", objects=object_json(language=None))
        self.assertEqual(
            missing.details["violations"],
            ["The PDF catalog declares no language; the configured language is 'en-GB'."],
        )
        bare = await self.assert_status(
            PdfCheckOptions(require_language=True), "failed", objects=object_json(language=None)
        )
        self.assertEqual(bare.details["violations"], ["The PDF catalog declares no language."])

    async def test_object_count_beyond_the_bounded_limit_is_not_reported_as_malformed_json(
        self,
    ) -> None:
        objects = {f"obj:{number} 0 R": 0 for number in range(1, MAX_PARSED_NODES + 2)}
        document = json.dumps(
            {"version": 2, "pages": [{"object": "3 0 R"}], "qpdf": [{"jsonversion": 2}, objects]}
        )
        finding = await self.assert_status(
            PdfCheckOptions(forbidden_action_types=("JavaScript",)),
            "inconclusive",
            objects=document,
        )
        self.assertIn(f"{MAX_PARSED_NODES + 1:,} objects", finding.message)
        self.assertIn(f"{MAX_PARSED_NODES:,}", finding.message)
        self.assertNotIn("JSON", finding.message)

    async def test_overlap_comparisons_never_exceed_the_reported_cap(self) -> None:
        options = PdfCheckOptions(overlap_min_area_ratio=0.4)
        # 720 stacked boxes plus the base box form more pairs than the cap allows.
        extra = '<text top="72" left="72" width="100" height="12" font="0">Other</text>'
        result = await self.assert_status(options, "failed", xml=text_xml(extra=extra * 720))
        self.assertEqual(result.details["comparisons"], MAX_PAIR_CHECKS)
        self.assertEqual(result.details["violation_count"], MAX_PAIR_CHECKS)

    def test_raster_dpi_message_names_the_allowed_range(self) -> None:
        with self.assertRaises(PreparationError) as raised:
            PdfCheckOptions(raster_dpi=MIN_MEASUREMENT_DPI - 1)
        self.assertIn(str(MIN_MEASUREMENT_DPI), str(raised.exception))

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
        self.assertEqual(failure.evidence_kind, "heuristic")
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
            # pdffonts keeps the original spacing inside the type column.
            ("failed", "Font Type  3 Custom yes yes no 3 0\n"),
        ):
            with self.subTest(state=state, fonts=fonts):
                result = await inspect_included_pdf_figures(
                    (self.pdf,),
                    self.work(),
                    DetailRunner(fonts="name type encoding emb sub uni object ID\n---\n" + fonts),
                    options=options,
                )
                self.assertEqual(result[0].status, state)
                self.assertEqual(result[0].path, str(self.pdf))
                self.assertIn("attribution is not established", result[0].details["scope"])

    async def test_figure_without_font_resources_has_nothing_to_embed(self) -> None:
        for options in (
            PdfCheckOptions(require_embedded_figure_fonts=True),
            PdfCheckOptions(forbid_type3_figure_fonts=True),
            PdfCheckOptions(require_embedded_figure_fonts=True, forbid_type3_figure_fonts=True),
        ):
            with self.subTest(options=options):
                result = await inspect_included_pdf_figures(
                    (self.pdf,),
                    self.work(),
                    DetailRunner(fonts="name type encoding emb sub uni object ID\n---\n"),
                    options=options,
                )
                self.assertEqual(len(result), 1)
                finding = result[0]
                self.assertEqual((finding.severity, finding.status), ("info", "passed"))
                self.assertEqual(
                    finding.message, "No font resources in this figure; nothing to embed."
                )
                self.assertEqual(finding.details["font_count"], 0)
                self.assertFalse(finding.details["incomplete"])
                self.assertEqual(finding.path, str(self.pdf))

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
        self.assertEqual(failure.evidence_kind, "heuristic")
        self.assertIn("semantic color-only encoding", failure.details["scope"])
        await self.assert_status(options, "inconclusive", trace=color_trace(space="DeviceCMYK"))

    async def test_separation_compares_a_bounded_number_of_pairs_and_counts_every_close_one(
        self,
    ) -> None:
        pages, minimum = 9, 0.0005
        grays = [float(f"{value / 255:.6f}") for value in range(256)]
        trace = '<document filename="document.pdf">' + "".join(
            f'<page number="{page}" mediabox="0 0 612 792">'
            + "".join(
                f'<fill_path colorspace="DeviceGray" color="{value:.6f}" transform="1 0 0 1 0 0"/>'
                for value in grays
            )
            + "</page>"
            for page in range(1, pages + 1)
        )
        trace += "</document>"
        luminances = [relative_luminance((value,) * 3) for value in grays]
        pairs = islice(
            chain.from_iterable(combinations(luminances, 2) for _ in range(pages)), MAX_PAIR_CHECKS
        )
        expected = sum(abs(first - second) < minimum for first, second in pairs)
        self.assertGreater(expected, MAX_SAMPLES // 10)
        finding = await self.assert_status(
            PdfCheckOptions(min_grayscale_luminance_difference=minimum),
            "failed",
            trace=trace,
            info=f"Pages: {pages}\n",
            geometry=geometry(pages=pages),
        )
        self.assertEqual(finding.details["compared_pairs"], MAX_PAIR_CHECKS)
        self.assertEqual(finding.details["violation_count"], expected)
        self.assertEqual(len(finding.details["violations"]), min(expected, MAX_SAMPLES))
        # Pairs beyond the cap were never compared, so the measurement says it is partial.
        self.assertTrue(finding.details["incomplete"])
        self.assertEqual(finding.details["page_palette_entries"], pages * len(grays))

    async def test_a_page_palette_beyond_its_cap_is_only_partly_compared(self) -> None:
        colors = MAX_PAGE_PALETTE_COLORS + 44
        trace = (
            '<document filename="document.pdf"><page number="1" mediabox="0 0 612 792">'
            + "".join(
                f'<fill_path colorspace="DeviceGray" color="{value / colors:.6f}" '
                'transform="1 0 0 1 0 0"/>'
                for value in range(colors)
            )
            + "</page></document>"
        )
        finding = await self.assert_status(
            PdfCheckOptions(min_grayscale_luminance_difference=0.0000001),
            "inconclusive",
            trace=trace,
        )
        self.assertEqual(finding.details["page_palette_entries"], MAX_PAGE_PALETTE_COLORS)
        self.assertEqual(finding.details["violation_count"], 0)

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

    @live_test("Opt-in live PDF tools")
    async def test_live_poppler_geometry_metadata_and_raster_measurements(self) -> None:
        self.pdf.write_bytes(simple_pdf())
        runner = ToolRunner(RuntimeLimits(timeout_seconds=30, max_memory_mb=512))
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

    @live_test("Opt-in live PDF tools")
    async def test_live_poppler_groups_of_one_pdf_share_each_read_only_command(self) -> None:
        self.pdf.write_bytes(simple_pdf())
        runner = CountingToolRunner(RuntimeLimits(timeout_seconds=30, max_memory_mb=512))
        settings = PdfCheckSettings(
            max_pages=5,
            inspection=PdfInspectionOptions(require_embedded_fonts=True, forbid_attachments=True),
            details=PdfCheckOptions(
                min_text_size_pt=10, required_metadata=("Title",), min_page_ink_ratio=0.00001
            ),
            structure=StructureOptions(heading_expectations=(HeadingExpectation(1, "Body", ""),)),
            privacy=SubmissionOptions(identity_terms=("Nobody Atall",)),
        )
        findings = await inspect_standalone_pdf(
            self.pdf, self.work(), runner, settings, ResourceBudget(2, 512)
        )
        self.assertTrue(findings)
        self.assertFalse([item for item in findings if item.incomplete], findings)
        reads = {argv: count for argv, count in runner.counts.items() if argv[0] != "pdftoppm"}
        self.assertEqual(set(reads.values()), {1}, reads)
        # Properties, page boxes and text are each needed by several groups.
        self.assertEqual(sum(argv[0] == "pdfinfo" for argv in reads), 2)
        self.assertEqual(sum(argv[0] == "pdftotext" for argv in reads), 1)

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
