"""Shared PDF tool plumbing: argument builders, preflight, text pages, memoization and policies."""

from __future__ import annotations

import asyncio
import json
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from latexprep.cancellation import cancellation_point
from latexprep.command_runner import CommandResult, RuntimeLimits
from latexprep.errors import PreparationError
from latexprep.findings import Finding
from latexprep.measurement_findings import EVIDENCE_LIMITATION_SUGGESTION, TOOL_FAILURE_SUGGESTION
from latexprep.pdf_check_options import PdfCheckOptions
from latexprep.pdf_checks import inspect_pdf_details
from latexprep.pdf_geometry import relative_luminance, wcag_contrast_ratio
from latexprep.pdf_qpdf_graph import parse_qpdf_json_v2
from latexprep.pdf_tool_output import (
    evaluate_font_policy,
    font_inventory,
    normalized_text_pages,
    parse_mupdf_trace,
    parse_tool_xml,
    pdfinfo_properties,
    refuse_reported_errors,
    split_text_pages,
)
from latexprep.pdf_tools import (
    DOCUMENT,
    PdfPreflight,
    PdfToolSession,
    ToolMeasurement,
    font_inventory_argv,
    layout_text_argv,
    open_pdf_session,
    pdf_tool_version_argv,
    pdfinfo_argv,
    preflight_pdf,
    qpdf_decoded_objects_argv,
    qpdf_objects_argv,
    raster_filename,
    render_page_argv,
    run_tool_measurements,
)
from latexprep.resource_budget import ResourceBudget
from tests.fakes import FakeCommandRunner, Reply, Script
from tests.pdf_fakes import DetailRunner
from tests.support import AsyncProjectTestCase

FONTS = (
    "name type encoding emb sub uni object ID\n"
    "---\n"
    "Embedded Type 1 Builtin yes yes no 3 0\n"
    "Loose TrueType WinAnsi no no no 4 0\n"
    "Glyphs Type 3 Custom yes no no 5 0\n"
    "Bare Type 3 Custom no no no 6 0\n"
)


def recording_runner(
    *, info: str = "Pages: 2\n", fail: str | None = None, delay: float = 0
) -> FakeCommandRunner:
    """Return a runner that answers pdfinfo and pdffonts, counts commands and may fail a tool.

    Every other tool prints its own name followed by " output".

    Args:
        info: What ``pdfinfo`` prints.
        fail: A tool whose commands exit with status 1 and print "tool failed" on stderr, or
            ``None``.
        delay: The seconds each command other than a version probe sleeps before it answers.

    Returns:
        The runner; ``pdffonts`` prints ``FONTS``.
    """
    failed = Reply("", "tool failed", 1)
    replies: dict[str, Script] = {"pdfinfo": info, "pdffonts": FONTS}
    if fail is not None:
        replies[fail] = failed
    return FakeCommandRunner(replies, default=lambda call: f"{call.tool} output\n", delay=delay)


class ArgumentBuilderTests(unittest.TestCase):
    """The canonical command lines of the PDF tools."""

    def test_pdfinfo_has_one_canonical_form(self) -> None:
        self.assertEqual(pdfinfo_argv(), ["pdfinfo", "-enc", "UTF-8", "-isodates", DOCUMENT])
        self.assertEqual(pdfinfo_argv("@left@")[-1], "@left@")

    def test_each_render_format_names_its_output_file(self) -> None:
        for raster, option, filename in (
            ("gray", "-gray", "page.pgm"),
            ("rgb", None, "page.ppm"),
            ("png", "-png", "page.png"),
        ):
            argv = render_page_argv(3, 72, raster)
            with self.subTest(raster):
                self.assertEqual(raster_filename(raster), filename)
                self.assertEqual(argv[0], "pdftoppm")
                self.assertEqual(argv[argv.index("-f") + 1], "3")
                self.assertEqual(argv[argv.index("-l") + 1], "3")
                self.assertEqual(argv[argv.index("-r") + 1], "72")
                self.assertIn("-singlefile", argv)
                self.assertEqual(argv[-2:], [DOCUMENT, "page"])
                formats = {"-gray", "-png"} & set(argv)
                self.assertEqual(formats, {option} if option else set())

    def test_the_comparison_sides_are_rendered_with_their_own_inputs(self) -> None:
        self.assertEqual(render_page_argv(1, 144, "rgb", "@right@")[-2:], ["@right@", "page"])

    def test_qpdf_decoded_objects_select_only_the_sorted_references(self) -> None:
        argv = qpdf_decoded_objects_argv({"12 0 R", "3 0 R", "7 1 R"})
        self.assertEqual(
            [item for item in argv if item.startswith("--json-object=")],
            ["--json-object=12,0", "--json-object=3,0", "--json-object=7,1"],
        )
        self.assertEqual(argv[:4], ["qpdf", "--json=2", "--json-key=qpdf", "--decode-level=all"])
        self.assertEqual(argv[-1], DOCUMENT)
        self.assertNotIn("--decode-level=all", qpdf_objects_argv())

    def test_remaining_builders_and_version_flags(self) -> None:
        self.assertEqual(font_inventory_argv(), ["pdffonts", DOCUMENT])
        self.assertEqual(layout_text_argv()[-3:], ["-layout", DOCUMENT, "-"])
        self.assertEqual(pdf_tool_version_argv("qpdf"), ["qpdf", "--version"])
        self.assertEqual(pdf_tool_version_argv("mutool"), ["mutool", "-v"])


class TextPageTests(unittest.TestCase):
    """Splitting extracted text into pages and normalizing its newlines."""

    def test_split_pages_require_exactly_one_form_feed_per_page(self) -> None:
        self.assertEqual(split_text_pages("a\n\fb \r\n\f", 2), ["a\n", "b \r\n"])
        self.assertEqual(split_text_pages("a\f\f  \n", 2), ["a", ""])
        for text in ("a\fb", "a\f", "a\fb\fc\f", "", "a\fb\fstray"):
            with self.subTest(text=text), self.assertRaisesRegex(PreparationError, "page boundary"):
                split_text_pages(text, 2)

    def test_normalized_pages_use_one_newline_policy(self) -> None:
        # Carriage returns, CRLF and trailing blanks never register as differences.
        self.assertEqual(
            normalized_text_pages("one  \r\ntwo\t\rthree\n\f\fx \f", 3),
            ["one\ntwo\nthree\n", "", "x"],
        )
        self.assertEqual(normalized_text_pages("a\r\nb\f", 1), normalized_text_pages("a\nb \f", 1))

    def test_normalized_pages_share_the_boundary_policy(self) -> None:
        with self.assertRaisesRegex(PreparationError, "page boundary"):
            normalized_text_pages("a\fb", 2)


# What pdfinfo (Poppler 26.11) prints for a file that declares PDF/X, from its Title on.
PDFX_INFO = (
    "Title:           A print-ready PDF\n"
    "Author:          A. Writer\n"
    "Tagged:          no\n"
    "Pages:           1\n"
    "PDF version:     1.7\n"
    "PDF subtype:    PDF/X-1:2001\n"
    "    Title:         ISO 15930 - Electronic document file format for prepress digital"
    " data exchange (PDF/X)\n"
    "    Abbreviation:  PDF/X-1:2001\n"
    "    Subtitle:      Part 1: Complete exchange using CMYK data (PDF/X-1 and PDF/X-1a)\n"
    "    Standard:      ISO 15930-1\n"
)


class PdfinfoPropertyTests(unittest.TestCase):
    """Reading the property lines of pdfinfo and refusing ambiguous ones."""

    def test_the_description_of_a_declared_standard_is_not_a_second_title(self) -> None:
        fields = pdfinfo_properties(PDFX_INFO)
        self.assertEqual(fields["Title"], "A print-ready PDF")
        self.assertEqual(fields["PDF subtype"], "PDF/X-1:2001")
        self.assertNotIn("Abbreviation", fields)
        self.assertNotIn("Standard", fields)

    def test_a_repeated_property_outside_that_description_is_still_refused(self) -> None:
        for output in (
            "Pages: 2\nTitle: a\nTitle: b\n",
            # Indented, but not under the subtype line.
            "Title: a\n    Title: b\nPages: 2\n",
            # After the description has ended.
            PDFX_INFO + "Title:           Another\n",
            PDFX_INFO + "Pages:           3\n",
            # A line without a colon ends the description.
            "Pages: 1\nTitle: a\nPDF subtype: PDF/X\ncontinued\n    Title: b\n",
        ):
            with (
                self.subTest(output=output),
                self.assertRaisesRegex(PreparationError, "Ambiguous duplicate"),
            ):
                pdfinfo_properties(output)


# What pdfimages (Poppler 26.11) prints for one JPEG 2000 image stored as a bare
# codestream, as in a 37 MiB paper of such figures: it reads the image as J2K and goes on.
RECOVERED_JPEG_2000 = (
    "Syntax Error: Malformed JP2 file format: first box must be JPEG 2000 signature box<0a>\n"
    "Syntax Warning: Unable to read header\n"
    "Syntax Warning: Did no succeed opening JPX Stream as JP2, trying as J2K.\n"
)


class ReportedErrorTests(unittest.TestCase):
    """Which lines of a tool's standard error mean damaged or incomplete input."""

    def test_an_error_the_tool_recovered_from_is_a_repair(self) -> None:
        for stderr in (
            "",
            "Syntax Warning: Invalid Font Weight\n",
            RECOVERED_JPEG_2000,
            RECOVERED_JPEG_2000 * 40,
            # Without the header warning, and with the spelling corrected.
            "Syntax Error: Malformed JP2 file format: first box must be JPEG 2000 signature "
            "box\nSyntax Warning: Did not succeed opening JPX Stream as JP2, trying as J2K.\n",
        ):
            with self.subTest(stderr=stderr[:60]):
                refuse_reported_errors(stderr, "pdfimages")

    def test_every_other_error_still_means_damaged_input(self) -> None:
        for stderr in (
            "Syntax Error (257): Illegal character\n",
            "error: cannot open document\n",
            # The second attempt failed too.
            RECOVERED_JPEG_2000 + "Syntax Error: Did not succeed opening JPX Stream.\n",
            # The error without the line that says the image was read another way.
            RECOVERED_JPEG_2000.splitlines(keepends=True)[0],
            RECOVERED_JPEG_2000.replace("trying as J2K", "giving up"),
            RECOVERED_JPEG_2000 + "Syntax Error: Couldn't find trailer dictionary\n",
        ):
            with (
                self.subTest(stderr=stderr[-60:]),
                self.assertRaisesRegex(PreparationError, "pdfimages reported damaged"),
            ):
                refuse_reported_errors(stderr, "pdfimages")


class FontPolicyTests(unittest.TestCase):
    """The font policy: observations always, violations only for the enabled switches."""

    def test_observations_ignore_the_switches_and_violations_follow_them(self) -> None:
        inventory = font_inventory(FONTS)
        names = {
            (require, forbid): [
                font.name
                for font in evaluate_font_policy(
                    inventory, require_embedded=require, forbid_type3=forbid
                ).violations
            ]
            for require in (False, True)
            for forbid in (False, True)
        }
        self.assertEqual(names[False, False], [])
        self.assertEqual(names[True, False], ["Loose", "Bare"])
        self.assertEqual(names[False, True], ["Glyphs", "Bare"])
        self.assertEqual(names[True, True], ["Loose", "Glyphs", "Bare"])
        outcome = evaluate_font_policy(inventory, require_embedded=False, forbid_type3=False)
        self.assertEqual([font.name for font in outcome.nonembedded], ["Loose", "Bare"])
        self.assertEqual([font.name for font in outcome.type3], ["Glyphs", "Bare"])

    def test_an_empty_inventory_violates_nothing(self) -> None:
        outcome = evaluate_font_policy([], require_embedded=True, forbid_type3=True)
        self.assertEqual((outcome.nonembedded, outcome.type3, outcome.violations), ([], [], []))


class ToolXmlTests(unittest.TestCase):
    """Tool XML is parsed with ``xml.etree``, so entity tricks must never reach the parser.

    Attributes:
        BILLION_LAUGHS: A document whose nested internal entities expand exponentially.
        EXTERNAL_ENTITY: A document whose entity reads a local file.
        EXTERNAL_PARAMETER_ENTITY: A document whose parameter entity loads a remote DTD.
    """

    BILLION_LAUGHS = (
        '<?xml version="1.0"?><!DOCTYPE pdf2xml ['
        '<!ENTITY a "aaaaaaaaaa">'
        '<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">'
        '<!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">'
        '<!ENTITY d "&c;&c;&c;&c;&c;&c;&c;&c;&c;&c;">'
        "]><pdf2xml>&d;</pdf2xml>"
    )
    EXTERNAL_ENTITY = (
        '<?xml version="1.0"?><!DOCTYPE pdf2xml ['
        '<!ENTITY secret SYSTEM "file:///etc/passwd">]><pdf2xml>&secret;</pdf2xml>'
    )
    EXTERNAL_PARAMETER_ENTITY = (
        '<!DOCTYPE pdf2xml [<!ENTITY % remote SYSTEM "http://example.invalid/e.dtd">%remote;]>'
        "<pdf2xml/>"
    )

    def test_entity_declarations_and_internal_subsets_are_rejected_before_parsing(self) -> None:
        rejected = {
            "billion laughs": self.BILLION_LAUGHS,
            "external entity": self.EXTERNAL_ENTITY,
            "external parameter entity": self.EXTERNAL_PARAMETER_ENTITY,
            "lower-case declaration": self.EXTERNAL_ENTITY.replace("ENTITY", "entity"),
            "internal subset without entities": (
                "<!DOCTYPE pdf2xml [<!ELEMENT pdf2xml ANY>]><pdf2xml/>"
            ),
        }
        for label, output in rejected.items():
            with (
                self.subTest(label),
                patch("latexprep.pdf_tool_output.ET.fromstring") as parser,
                self.assertRaisesRegex(PreparationError, "unsupported entity declarations"),
            ):
                parse_tool_xml(output, "pdf2xml")
            parser.assert_not_called()

    def test_the_trace_parser_applies_the_same_rejection(self) -> None:
        with self.assertRaisesRegex(PreparationError, "unsupported entity declarations"):
            parse_mupdf_trace(self.BILLION_LAUGHS.replace("pdf2xml", "document"), 1)

    def test_a_reference_to_an_undeclared_entity_is_malformed_not_fetched(self) -> None:
        output = (
            '<!DOCTYPE pdf2xml SYSTEM "http://example.invalid/pdf2xml.dtd"><pdf2xml>&x;</pdf2xml>'
        )
        with self.assertRaisesRegex(PreparationError, "malformed XML"):
            parse_tool_xml(output, "pdf2xml")

    def test_the_external_dtd_reference_pdftohtml_writes_still_parses(self) -> None:
        output = (
            '<?xml version="1.0"?><!DOCTYPE pdf2xml SYSTEM "pdf2xml.dtd"><pdf2xml><page/></pdf2xml>'
        )
        self.assertEqual(parse_tool_xml(output, "pdf2xml").tag, "pdf2xml")


class QpdfEnvelopeTests(unittest.TestCase):
    """The envelope of qpdf's JSON version 2 output and the defects that reject it."""

    @staticmethod
    def document(**changes: object) -> str:
        """Return a two-page qpdf JSON version 2 document with one object.

        Args:
            **changes: Top-level keys that replace those of the document; a key whose value
                is ``None`` is left out.

        Returns:
            The document as JSON text.
        """
        document: dict[str, object] = {
            "version": 2,
            "pages": [{}, {}],
            "qpdf": [{"jsonversion": 2}, {"obj:1 0 R": {"value": {}}}],
        }
        document.update(changes)
        return json.dumps({key: value for key, value in document.items() if value is not None})

    def test_a_complete_document_yields_its_objects_and_pages(self) -> None:
        parsed = parse_qpdf_json_v2(self.document(), "failed", pages=2)
        self.assertEqual(list(parsed.objects), ["obj:1 0 R"])
        self.assertEqual(len(parsed.pages), 2)

    def test_pages_are_only_required_when_requested(self) -> None:
        parsed = parse_qpdf_json_v2(self.document(pages=None), "failed")
        self.assertEqual(parsed.pages, [])
        with self.assertRaisesRegex(PreparationError, "needed pages"):
            parse_qpdf_json_v2(self.document(pages=None), "needed pages", pages=2)

    def test_every_envelope_defect_raises_the_callers_message(self) -> None:
        bad = {
            "not json": "{",
            "not a document": "[]",
            "version": self.document(version=1),
            "no version": self.document(version=None),
            "header version": self.document(qpdf=[{"jsonversion": 1}, {}]),
            "entry count": self.document(qpdf=[{"jsonversion": 2}]),
            "objects": self.document(qpdf=[{"jsonversion": 2}, []]),
            "no qpdf": self.document(qpdf=None),
            "page count": self.document(pages=[{}]),
            "page type": self.document(pages={}),
        }
        for label, output in bad.items():
            with self.subTest(label), self.assertRaisesRegex(PreparationError, "the message"):
                parse_qpdf_json_v2(output, "the message", pages=2)


class SessionTestCase(AsyncProjectTestCase):
    """A placeholder PDF in the project root and fresh work directories for tool sessions."""

    async def asyncSetUp(self) -> None:
        """Write a placeholder ``document.pdf`` into the project root."""
        self.pdf = self.root / "document.pdf"
        self.pdf.write_bytes(b"%PDF-fixture")
        self.counter = 0

    def work(self) -> Path:
        """Return a new, not yet created work directory path below the project root."""
        self.counter += 1
        return self.root / f"work-{self.counter}"


class MeasurementMemoTests(SessionTestCase):
    """Which measurements a tool session remembers and shares, and which it runs again."""

    async def test_an_identical_measurement_runs_its_tool_once(self) -> None:
        runner = recording_runner()
        session = await PdfToolSession.open(self.work(), {"document": self.pdf}, runner, None)
        parsed: list[str] = []
        for name in ("page-boxes", "raster-sizes"):
            await session.measure(name, ["pdffonts", DOCUMENT], lambda r: parsed.append(r.stdout))
        self.assertEqual(runner.commands, [["pdffonts", str(session.inputs["document"])]])
        # Parsing is repeated for every request, on the remembered output.
        self.assertEqual(parsed, [FONTS, FONTS])
        self.assertEqual(sorted(session.tool_versions()), ["pdffonts"])

    async def test_different_arguments_and_failures_are_not_shared(self) -> None:
        runner = recording_runner(fail="pdfinfo")
        session = await PdfToolSession.open(self.work(), {"document": self.pdf}, runner, None)
        # Each run needs its own command name: a name owns one new directory.
        for attempt in ("first", "second"):
            with self.assertRaises(PreparationError):
                await session.measure(attempt, pdfinfo_argv(), lambda r: r.stdout)
        self.assertEqual(len(runner.commands), 2)
        runner.replies["pdfinfo"] = "Pages: 2\n"
        self.assertEqual(
            await session.measure("third", pdfinfo_argv(), lambda r: r.stdout), "Pages: 2\n"
        )
        await session.measure("boxes", ["pdfinfo", "-box", DOCUMENT], lambda r: r.stdout)
        self.assertEqual(len(runner.commands), 4)

    async def test_text_geometry_and_sparse_pages_share_one_page_box_command(self) -> None:
        runner = DetailRunner()
        options = PdfCheckOptions(min_text_size_pt=8, min_page_ink_ratio=0.01)
        findings = await inspect_pdf_details(self.pdf, self.work(), runner, options=options)
        self.assertEqual(
            {item.rule for item in findings}, {"pdf.rendered_text_size", "pdf.sparse_pages"}
        )
        boxes = [argv for argv, _, _ in runner.calls if argv[:2] == ["pdfinfo", "-box"]]
        self.assertEqual(len(boxes), 1)

    async def test_concurrent_identical_measurements_run_the_tool_once(self) -> None:
        runner = recording_runner(delay=0.01)
        session = await PdfToolSession.open(self.work(), {"document": self.pdf}, runner, None)
        results = await asyncio.gather(
            *(session.measure(f"copy-{n}", pdfinfo_argv(), lambda r: r.stdout) for n in range(3))
        )
        self.assertEqual(results, ["Pages: 2\n"] * 3)
        self.assertEqual(len(runner.commands), 1)

    async def test_a_cancelled_request_lets_the_waiting_one_run_the_tool(self) -> None:
        runner = recording_runner(delay=0.05)
        session = await PdfToolSession.open(self.work(), {"document": self.pdf}, runner, None)
        first = asyncio.create_task(session.measure("first", pdfinfo_argv(), lambda r: r.stdout))
        await asyncio.sleep(0.01)
        second = asyncio.create_task(session.measure("second", pdfinfo_argv(), lambda r: r.stdout))
        await asyncio.sleep(0.01)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertEqual(await second, "Pages: 2\n")
        self.assertEqual(len(runner.commands), 2)

    async def test_separate_sessions_do_not_share_results(self) -> None:
        runner = recording_runner()
        for _ in range(2):
            session = await PdfToolSession.open(self.work(), {"document": self.pdf}, runner, None)
            await session.measure("properties", pdfinfo_argv(), lambda r: r.stdout)
        self.assertEqual(len(runner.commands), 2)


class SessionOpeningTests(SessionTestCase):
    """Which resource budget the measurements of an opened session use."""

    async def test_measurements_default_to_the_runner_budget_unless_told_otherwise(self) -> None:
        runner = recording_runner()
        runner.budget = ResourceBudget(1, 256)
        explicit = ResourceBudget(2, 512)
        defaulted = await open_pdf_session(self.pdf, self.work(), runner, None)
        self.assertIs(defaulted.budget, runner.budget)
        exact = await open_pdf_session(
            self.pdf, self.work(), runner, None, default_to_runner_budget=False
        )
        self.assertIsNone(exact.budget)
        for default in (True, False):
            supplied = await open_pdf_session(
                self.pdf, self.work(), runner, explicit, default_to_runner_budget=default
            )
            self.assertIs(supplied.budget, explicit)


class MemoryRecordingRunner(FakeCommandRunner):
    """A runner that records the memory cap of each command and the lease held for it.

    Args:
        limits: The runtime limits.
        budget: The runner's own budget, if any.
        watched: The budget whose held lease is recorded; the runner's own by default.
        exceed: Whether every command reports that it exceeded its cap, as the resource
            monitor words it.

    Attributes:
        caps: The ``memory_mb`` of each command other than a version probe.
        leases: The memory of the lease of ``watched`` held during each of those commands,
            or ``None``.
    """

    def __init__(
        self,
        limits: RuntimeLimits,
        budget: ResourceBudget | None = None,
        *,
        watched: ResourceBudget | None = None,
        exceed: bool = False,
    ) -> None:
        super().__init__(default="page num type\n---\n")
        self.limits = limits
        self.budget = budget
        self.watched = watched or budget
        self.exceed = exceed
        self.caps: list[int | None] = []
        self.leases: list[int | None] = []

    async def run(
        self,
        argv: list[str],
        cwd: Path,
        workspace: Path,
        *,
        readonly_inputs: tuple[Path, ...] = (),
        memory_mb: int | None = None,
    ) -> CommandResult:
        """Record the cap and the held lease, then answer as the base runner or as stopped.

        The arguments are those of :meth:`CommandRunner.run`.
        """
        result = await super().run(
            argv, cwd, workspace, readonly_inputs=readonly_inputs, memory_mb=memory_mb
        )
        if argv[-1] == "-v":
            return result
        lease = self.watched.current_lease if self.watched is not None else None
        self.caps.append(memory_mb)
        self.leases.append(lease.memory_mb if lease is not None else None)
        if self.exceed:
            exceeded = f"Aggregate tool memory exceeded {memory_mb} MiB"
            return CommandResult(-9, "", "", resolved_command=argv, resource_exceeded=exceeded)
        return result


class MeasurementMemoryTests(SessionTestCase):
    """The memory limit of a read-only measurement follows the size of the PDF it reads."""

    def large_pdf(self, mib: int) -> Path:
        """Write a PDF of ``mib`` MiB and return its path."""
        path = self.root / f"large-{mib}.pdf"
        path.write_bytes(b"%PDF-" + b"0" * (mib * 1024 * 1024 - 5))
        return path

    async def measure(self, pdf: Path, runner: MemoryRecordingRunner, budget=None) -> None:
        """Open a session over ``pdf`` and run one ``pdfimages -list`` measurement."""
        session = await PdfToolSession.open(self.work(), {"document": pdf}, runner, budget)
        await session.measure("images", ["pdfimages", "-list", DOCUMENT], lambda r: r.stdout)

    async def test_a_large_pdf_gets_more_memory_and_an_equal_lease(self) -> None:
        # pdfimages needed between 96 and 112 MiB for a 37 MiB paper and was stopped at 64.
        cases = ((self.pdf, 64), (self.large_pdf(10), 84), (self.large_pdf(37), 138))
        for pdf, expected in cases:
            budget = ResourceBudget(2, 2048)
            runner = MemoryRecordingRunner(RuntimeLimits(max_memory_mb=1024), watched=budget)
            await self.measure(pdf, runner, budget)
            with self.subTest(pdf=pdf.name):
                # The lease that admits the command holds exactly its cap.
                self.assertEqual((runner.caps, runner.leases), ([expected], [expected]))

    async def test_the_limit_stops_at_the_memory_of_one_command_and_at_the_held_lease(self) -> None:
        pdf = self.large_pdf(37)
        runner = MemoryRecordingRunner(RuntimeLimits(max_memory_mb=100))
        await self.measure(pdf, runner, ResourceBudget(2, 2048))
        self.assertEqual(runner.caps, [100])
        # A session without a budget runs under the lease its caller already holds.
        budget = ResourceBudget(2, 2048)
        runner = MemoryRecordingRunner(RuntimeLimits(max_memory_mb=1024), budget)
        async with budget.lease("caller", memory_mb=90):
            await self.measure(pdf, runner)
        self.assertEqual(runner.caps, [90])
        # ... or under a lease the runner takes from its own budget.
        own = ResourceBudget(1, 200, coordinator_memory_mb=128)
        runner = MemoryRecordingRunner(RuntimeLimits(max_memory_mb=1024), own)
        await self.measure(pdf, runner)
        self.assertEqual(runner.caps, [72])

    async def test_a_tool_over_its_limit_still_could_not_check_and_names_the_limit(self) -> None:
        runner = MemoryRecordingRunner(RuntimeLimits(max_memory_mb=1024), exceed=True)
        with self.assertRaises(PreparationError) as raised:
            await self.measure(self.large_pdf(10), runner, ResourceBudget(2, 2048))
        self.assertEqual(str(raised.exception), "Aggregate tool memory exceeded 84 MiB")


class PreflightTests(SessionTestCase):
    """The PDF preflight: its session, properties and page count, or its failure."""

    async def test_a_readable_document_yields_its_session_properties_and_page_count(self) -> None:
        runner = recording_runner(info="Pages: 3\nTitle: Paper\n")
        preflight = await preflight_pdf(self.pdf, self.work(), runner, None, ["pdf.any"])
        assert isinstance(preflight, PdfPreflight)
        self.assertEqual((preflight.pages, preflight.fields["Title"]), (3, "Paper"))
        # The properties are remembered, so a later request for them runs nothing.
        await preflight.session.measure("again", pdfinfo_argv(), lambda r: r.stdout)
        self.assertEqual(len(runner.commands), 1)

    async def test_any_failure_marks_every_rule_unavailable_with_the_callers_suggestion(
        self,
    ) -> None:
        pdf_not_a_pdf = self.root / "notes.txt"
        pdf_not_a_pdf.write_bytes(b"not a pdf")
        failures = {
            "tool": (self.pdf, recording_runner(fail="pdfinfo")),
            "pages": (self.pdf, recording_runner(info="Title: none\n")),
            "ambiguous": (self.pdf, recording_runner(info="Pages: 2\nTitle: a\nTitle: b\n")),
            "input": (pdf_not_a_pdf, recording_runner()),
        }
        for label, (pdf, runner) in failures.items():
            for suggestion in (EVIDENCE_LIMITATION_SUGGESTION, TOOL_FAILURE_SUGGESTION):
                with self.subTest(label, suggestion=suggestion):
                    findings = await preflight_pdf(
                        pdf,
                        self.work(),
                        runner,
                        None,
                        ["pdf.one", "pdf.two"],
                        suggestion=suggestion,
                    )
                    assert isinstance(findings, list)
                    self.assertEqual([item.rule for item in findings], ["pdf.one", "pdf.two"])
                    for finding in findings:
                        self.assertEqual(
                            (finding.status, finding.incomplete, finding.suggestion),
                            ("inconclusive", True, suggestion),
                        )

    async def test_the_raising_form_propagates_the_failure(self) -> None:
        with self.assertRaises(PreparationError):
            await PdfPreflight.open(self.pdf, self.work(), recording_runner(fail="pdfinfo"), None)
        opened = await PdfPreflight.open(self.pdf, self.work(), recording_runner(), None)
        self.assertEqual(opened.pages, 2)


class TypedFontTests(unittest.TestCase):
    """The typed rows of the font inventory and their report form."""

    def test_inventory_rows_are_typed_fonts_that_report_under_the_established_keys(self) -> None:
        loose, glyphs = font_inventory(FONTS)[1:3]
        self.assertEqual(
            (loose.name, loose.font_type, loose.embedded), ("Loose", "TrueType", False)
        )
        self.assertEqual((loose.is_type3, glyphs.is_type3), (False, True))
        self.assertEqual(
            glyphs.to_report(),
            {
                "name": "Glyphs",
                "type": "Type 3",
                "encoding": "Custom",
                "embedded": True,
                "subset": False,
                "unicode_map": False,
                "object": 5,
                "generation": 0,
            },
        )


class ColorContrastTests(unittest.TestCase):
    """The WCAG contrast ratio of two relative luminances."""

    def test_wcag_ratio_runs_from_one_to_twenty_one_in_either_order(self) -> None:
        black, white = relative_luminance((0, 0, 0)), relative_luminance((1, 1, 1))
        self.assertAlmostEqual(wcag_contrast_ratio(black, white), 21.0)
        self.assertAlmostEqual(wcag_contrast_ratio(white, black), 21.0)
        self.assertEqual(wcag_contrast_ratio(0.3, 0.3), 1.0)
        # WCAG's published example: #777 on white is 4.48:1.
        gray = relative_luminance((0x77 / 255,) * 3)
        self.assertAlmostEqual(wcag_contrast_ratio(gray, white), 4.48, places=2)


class ToolMeasurementTests(SessionTestCase):
    """A tool measurement's findings, its inconclusive outcomes and its worker thread."""

    async def session(self, runner: FakeCommandRunner) -> PdfToolSession:
        """Open a tool session over ``document.pdf`` without a budget, using ``runner``."""
        return await PdfToolSession.open(self.work(), {"document": self.pdf}, runner, None)

    @staticmethod
    def finding(rule: str, message: str) -> Finding:
        """Return a passed informational finding of ``rule`` with ``message``."""
        return Finding(rule, message, severity="info", status="passed")

    async def test_the_findings_of_a_readable_command_are_returned(self) -> None:
        session = await self.session(recording_runner())
        measurement = ToolMeasurement(
            "fonts",
            font_inventory_argv(),
            ("pdf.one",),
            lambda result: [self.finding("pdf.one", f"{len(font_inventory(result.stdout))} fonts")],
        )
        [finding] = await measurement.run(session)
        self.assertEqual((finding.rule, finding.message), ("pdf.one", "4 fonts"))

    async def test_a_failing_command_or_unreadable_output_leaves_each_rule_inconclusive(
        self,
    ) -> None:
        def unreadable(_result: CommandResult) -> list[Finding]:
            raise PreparationError("Output could not be read")

        for label, runner, evaluate, message in (
            ("command", recording_runner(fail="pdffonts"), lambda _r: [], "pdffonts failed"),
            ("output", recording_runner(), unreadable, "Output could not be read"),
        ):
            with self.subTest(label):
                session = await self.session(runner)
                measurement = ToolMeasurement(
                    "fonts",
                    font_inventory_argv(),
                    ("pdf.one", "pdf.two"),
                    evaluate,
                    suggestion=TOOL_FAILURE_SUGGESTION,
                )
                findings = await measurement.run(session)
                self.assertEqual([item.rule for item in findings], ["pdf.one", "pdf.two"])
                for finding in findings:
                    self.assertEqual(
                        (finding.status, finding.incomplete, finding.suggestion),
                        ("inconclusive", True, TOOL_FAILURE_SUGGESTION),
                    )
                    self.assertIn(message, finding.message)

    async def test_evaluation_runs_in_a_worker_thread_that_observes_cancellation(self) -> None:
        session = await self.session(recording_runner())
        threads: list[int] = []
        entered, stopped = threading.Event(), threading.Event()

        def evaluate(_result: CommandResult) -> list[Finding]:
            threads.append(threading.get_ident())
            entered.set()
            try:
                while True:
                    cancellation_point()
                    time.sleep(0.001)
            finally:
                stopped.set()

        measurement = ToolMeasurement("fonts", font_inventory_argv(), ("pdf.one",), evaluate)
        task = asyncio.create_task(measurement.run(session))
        while not entered.is_set():
            await asyncio.sleep(0.001)
        self.assertNotEqual(threads, [threading.get_ident()])
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        # The worker stopped before the cancellation reached the caller.
        self.assertTrue(stopped.is_set())

    async def test_measurements_run_concurrently_and_report_in_the_given_order(self) -> None:
        runner = recording_runner(delay=0.01)
        session = await self.session(runner)
        measurements = [
            ToolMeasurement(
                name,
                [tool, DOCUMENT],
                (f"pdf.{name}",),
                lambda result, name=name: [self.finding(f"pdf.{name}", result.stdout.strip())],
            )
            for name, tool in (
                ("first", "pdftotext"),
                ("second", "pdffonts"),
                ("third", "pdfimages"),
            )
        ]
        groups = await run_tool_measurements(session, measurements)
        self.assertEqual(
            [group[0].rule for group in groups], ["pdf.first", "pdf.second", "pdf.third"]
        )
        self.assertEqual(len(runner.commands), 3)


if __name__ == "__main__":
    unittest.main()
