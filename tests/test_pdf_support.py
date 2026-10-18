"""Shared PDF tool plumbing: argument builders, preflight, text pages, memoization and policies."""

from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from latexprep.cancellation import cancellation_point
from latexprep.command_runner import CommandResult
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
from latexprep.runtime import ToolRunner
from tests.test_pdf_checks import DetailRunner

FONTS = (
    "name type encoding emb sub uni object ID\n"
    "---\n"
    "Embedded Type 1 Builtin yes yes no 3 0\n"
    "Loose TrueType WinAnsi no no no 4 0\n"
    "Glyphs Type 3 Custom yes no no 5 0\n"
    "Bare Type 3 Custom no no no 6 0\n"
)


class RecordingRunner(ToolRunner):
    """Answers pdfinfo and pdffonts; counts the commands and optionally fails them."""

    def __init__(
        self, *, info: str = "Pages: 2\n", fail: str | None = None, delay: float = 0
    ) -> None:
        super().__init__()
        self.info = info
        self.fail = fail
        self.delay = delay
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
        if argv[-1] in {"-v", "--version"}:
            return CommandResult(0, f"{argv[0]} version fixture\n", "", resolved_command=argv)
        self.commands.append(list(argv))
        await asyncio.sleep(self.delay)
        if argv[0] == self.fail:
            return CommandResult(1, "", "tool failed", resolved_command=argv)
        output = {"pdfinfo": self.info, "pdffonts": FONTS}.get(argv[0], f"{argv[0]} output\n")
        return CommandResult(0, output, "", resolved_command=argv)


class ArgumentBuilderTests(unittest.TestCase):
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


class FontPolicyTests(unittest.TestCase):
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


class QpdfEnvelopeTests(unittest.TestCase):
    @staticmethod
    def document(**changes: object) -> str:
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


class SessionTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.pdf = self.root / "document.pdf"
        self.pdf.write_bytes(b"%PDF-fixture")
        self.counter = 0

    def work(self) -> Path:
        self.counter += 1
        return self.root / f"work-{self.counter}"


class MeasurementMemoTests(SessionTestCase):
    async def test_an_identical_measurement_runs_its_tool_once(self) -> None:
        runner = RecordingRunner()
        session = await PdfToolSession.open(self.work(), {"document": self.pdf}, runner, None)
        parsed: list[str] = []
        for name in ("page-boxes", "raster-sizes"):
            await session.measure(name, ["pdffonts", DOCUMENT], lambda r: parsed.append(r.stdout))
        self.assertEqual(runner.commands, [["pdffonts", str(session.inputs["document"])]])
        # Parsing is repeated for every request, on the remembered output.
        self.assertEqual(parsed, [FONTS, FONTS])
        self.assertEqual(sorted(session.tool_versions()), ["pdffonts"])

    async def test_different_arguments_and_failures_are_not_shared(self) -> None:
        runner = RecordingRunner(fail="pdfinfo")
        session = await PdfToolSession.open(self.work(), {"document": self.pdf}, runner, None)
        # Each run needs its own command name: a name owns one new directory.
        for attempt in ("first", "second"):
            with self.assertRaises(PreparationError):
                await session.measure(attempt, pdfinfo_argv(), lambda r: r.stdout)
        self.assertEqual(len(runner.commands), 2)
        runner.fail = None
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
        runner = RecordingRunner(delay=0.01)
        session = await PdfToolSession.open(self.work(), {"document": self.pdf}, runner, None)
        results = await asyncio.gather(
            *(session.measure(f"copy-{n}", pdfinfo_argv(), lambda r: r.stdout) for n in range(3))
        )
        self.assertEqual(results, ["Pages: 2\n"] * 3)
        self.assertEqual(len(runner.commands), 1)

    async def test_a_cancelled_request_lets_the_waiting_one_run_the_tool(self) -> None:
        runner = RecordingRunner(delay=0.05)
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
        runner = RecordingRunner()
        for _ in range(2):
            session = await PdfToolSession.open(self.work(), {"document": self.pdf}, runner, None)
            await session.measure("properties", pdfinfo_argv(), lambda r: r.stdout)
        self.assertEqual(len(runner.commands), 2)


class SessionOpeningTests(SessionTestCase):
    async def test_measurements_default_to_the_runner_budget_unless_told_otherwise(self) -> None:
        runner = RecordingRunner()
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


class PreflightTests(SessionTestCase):
    async def test_a_readable_document_yields_its_session_properties_and_page_count(self) -> None:
        runner = RecordingRunner(info="Pages: 3\nTitle: Paper\n")
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
            "tool": (self.pdf, RecordingRunner(fail="pdfinfo")),
            "pages": (self.pdf, RecordingRunner(info="Title: none\n")),
            "ambiguous": (self.pdf, RecordingRunner(info="Pages: 2\nTitle: a\nTitle: b\n")),
            "input": (pdf_not_a_pdf, RecordingRunner()),
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
            await PdfPreflight.open(self.pdf, self.work(), RecordingRunner(fail="pdfinfo"), None)
        opened = await PdfPreflight.open(self.pdf, self.work(), RecordingRunner(), None)
        self.assertEqual(opened.pages, 2)


class TypedFontTests(unittest.TestCase):
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
    def test_wcag_ratio_runs_from_one_to_twenty_one_in_either_order(self) -> None:
        black, white = relative_luminance((0, 0, 0)), relative_luminance((1, 1, 1))
        self.assertAlmostEqual(wcag_contrast_ratio(black, white), 21.0)
        self.assertAlmostEqual(wcag_contrast_ratio(white, black), 21.0)
        self.assertEqual(wcag_contrast_ratio(0.3, 0.3), 1.0)
        # WCAG's published example: #777 on white is 4.48:1.
        gray = relative_luminance((0x77 / 255,) * 3)
        self.assertAlmostEqual(wcag_contrast_ratio(gray, white), 4.48, places=2)


class ToolMeasurementTests(SessionTestCase):
    async def session(self, runner: RecordingRunner) -> PdfToolSession:
        return await PdfToolSession.open(self.work(), {"document": self.pdf}, runner, None)

    @staticmethod
    def finding(rule: str, message: str) -> Finding:
        return Finding(rule, message, severity="info", status="passed")

    async def test_the_findings_of_a_readable_command_are_returned(self) -> None:
        session = await self.session(RecordingRunner())
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
            ("command", RecordingRunner(fail="pdffonts"), lambda _r: [], "pdffonts failed"),
            ("output", RecordingRunner(), unreadable, "Output could not be read"),
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
        session = await self.session(RecordingRunner())
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
        runner = RecordingRunner(delay=0.01)
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
