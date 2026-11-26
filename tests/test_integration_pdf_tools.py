r"""Live qpdf and MuPDF checks: the real tools in the sandbox, on a PDF written by the test.

The tests need Poppler's ``pdfinfo`` and, for each case, qpdf or MuPDF's ``mutool``
(Homebrew's ``qpdf`` and ``mupdf-tools``; Debian's and Ubuntu's ``qpdf`` and
``mupdf-tools``). They run ``fledge pdf check`` as a user types it, so no TeX is needed.
The PDF has one page whose catalog declares the language ``en-US`` and which carries a
link annotation, a 0.1 pt stroke and a 2 pt stroke, both in DeviceRGB.

Each test enables one opt-in check that passes and one that fails, so a finding can only
have its expected status if the tool ran and its output was read; a missing or failing
tool would make both findings inconclusive. Without opt-in checks the PDF passes with
advisories, because its page has no extractable text.
"""

from __future__ import annotations

import json
from typing import Any

from click.testing import CliRunner

from latexprep.cli import cli
from latexprep.report import Outcome
from tests.live_support import LiveProjectTestCase, blocking_summary
from tests.support import live_test

# The page draws a red 0.1 pt line and a blue 2 pt line, both in DeviceRGB.
_PAGE_CONTENT = b"1 0 0 RG 0.1 w 20 20 m 180 20 l S 0 0 1 RG 2 w 20 100 m 180 100 l S\n"
_OBJECTS = (
    b"<< /Type /Catalog /Pages 2 0 R /Lang (en-US) >>",
    b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
    b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents 4 0 R /Annots [5 0 R] >>",
    b"<< /Length %d >>\nstream\n%sendstream" % (len(_PAGE_CONTENT), _PAGE_CONTENT),
    b"<< /Type /Annot /Subtype /Link /Rect [20 140 180 160] /Border [0 0 0]"
    b" /A << /S /URI /URI (https://example.org/) >> >>",
)


def one_page_pdf() -> bytes:
    """Return the bytes of the test's one-page PDF, with a correct cross-reference table."""
    document = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(_OBJECTS, 1):
        offsets.append(len(document))
        document += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    table = len(document)
    document += b"xref\n0 %d\n0000000000 65535 f \n" % (len(_OBJECTS) + 1)
    document += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    document += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(_OBJECTS) + 1,
        table,
    )
    return bytes(document)


def findings_by_code(report: dict[str, Any], *codes: str) -> dict[str, dict[str, Any]]:
    """Return the one finding of each of ``codes`` in a JSON report, by code."""
    found = [item for item in report["findings"] if item["code"] in codes]
    return {item["code"]: item for item in found}


@live_test("Set FLEDGE_RUN_INTEGRATION=1 for the live qpdf and MuPDF checks")
class LivePdfToolTests(LiveProjectTestCase):
    """The real qpdf and mutool measure a PDF in the sandbox, and their findings decide."""

    def setUp(self) -> None:
        """Skip unless Poppler's ``pdfinfo``, which counts the pages, is installed."""
        super().setUp()
        self.require_tools("pdfinfo")

    def check_pdf(self, settings: str, outcome: Outcome) -> dict[str, Any]:
        """Run ``fledge pdf check`` on the test PDF with ``settings`` and read its JSON report.

        Also asserts that the command exited with the code of its outcome, and that the
        outcome is ``outcome``.

        Args:
            settings: The text of the settings file.
            outcome: The expected outcome.

        Returns:
            The parsed JSON report.
        """
        pdf = self.base / "paper.pdf"
        pdf.write_bytes(one_page_pdf())
        configuration = self.base / "fledge.toml"
        configuration.write_text(settings, encoding="utf-8")
        result = CliRunner().invoke(
            cli,
            [
                "pdf",
                "check",
                str(pdf),
                "--config",
                str(configuration),
                "--json",
                "--quiet",
                "--job-timeout-seconds",
                "600",
            ],
        )
        try:
            report = json.loads(result.stdout)
        except json.JSONDecodeError:
            self.fail(f"No JSON report (exit code {result.exit_code}): {result.output}")
        self.assertEqual(
            result.exit_code, Outcome(report["outcome"]).exit_code, blocking_summary(report)
        )
        self.assert_outcome(report, outcome)
        return report

    def test_qpdf_reads_the_catalog_language_and_the_link_annotation(self) -> None:
        self.require_tools("qpdf")
        report = self.check_pdf(
            '[pdf_checks]\nexpected_language = "en-US"\nforbidden_annotation_types = ["Link"]\n',
            Outcome.BLOCKED,
        )
        found = findings_by_code(report, "PDF209", "PDF210")
        self.assertEqual(
            {code: item["status"] for code, item in found.items()},
            {"PDF209": "passed", "PDF210": "failed"},
            blocking_summary(report),
        )
        self.assertEqual(found["PDF209"]["details"]["declared_language"], "en-US")
        self.assertEqual(found["PDF210"]["severity"], "error")
        self.assertEqual(found["PDF210"]["details"]["annotation_count"], 1)
        self.assertEqual(found["PDF210"]["details"]["violations"], [{"page": 1, "type": "Link"}])
        self.assertIn("qpdf", " ".join(report["tools"]["qpdf"]["version"]))

    def test_mutool_traces_the_stroke_widths_and_color_spaces(self) -> None:
        self.require_tools("mutool")
        report = self.check_pdf(
            '[pdf_checks]\nmin_stroke_width_pt = 0.5\nallowed_color_spaces = ["DeviceRGB"]\n',
            Outcome.PASSED_WITH_ADVISORIES,
        )
        found = findings_by_code(report, "PDF213", "PDF214")
        self.assertEqual(
            {code: item["status"] for code, item in found.items()},
            {"PDF213": "failed", "PDF214": "passed"},
            blocking_summary(report),
        )
        # A thin stroke is an advisory: it warns instead of blocking the PDF.
        self.assertEqual(found["PDF213"]["severity"], "warning")
        strokes = found["PDF213"]["details"]
        self.assertEqual(strokes["stroke_count"], 2)
        self.assertEqual(
            [(item["page"], item["source_width"]) for item in strokes["violations"]], [(1, 0.1)]
        )
        self.assertAlmostEqual(strokes["violations"][0]["effective_width_pt"], 0.1, places=6)
        self.assertEqual(found["PDF214"]["details"]["measured_operations"], 2)
        self.assertIn("mutool", report["tools"])
