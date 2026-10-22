"""Opt-in live checks of PDF comparisons, accepted exceptions, previews and repeated baselines.

Each test builds real documents with TeX in the sandbox and measures them with Poppler, and
verifies one documented claim about how Fledge compares PDFs or keeps visual evidence: a
supplied reference PDF, an accepted comparison exception with HTML previews, grayscale page
previews, and repeated baseline builds.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from typing import Any, NamedTuple

from click.testing import CliRunner

from latexprep.cli import cli
from latexprep.config import Settings
from latexprep.core import run_job
from latexprep.job_context import JobRequest
from latexprep.pdf_artwork_options import PdfArtworkOptions
from latexprep.png_support import iter_png_chunks, read_png_header
from latexprep.report import Report
from latexprep.workflow_options import WorkflowOptions
from tests.support import live_test, write_file, write_project

EPOCH = 1_700_000_000
# Other live builds may share the machine, so a job gets the documented default deadline.
JOB_TIMEOUT_SECONDS = 600
FIXTURE = Path(__file__).resolve().parents[1] / "examples" / "nested-paper"
PASSING = frozenset({"passed", "passed_with_advisories"})

# The stages that comparison findings record, as reports and review decisions name them.
# Written out rather than imported, so that renaming a stage fails these tests.
REFERENCE_VERSUS_BASELINE = "supplied PDF versus baseline"
BASELINE_VERSUS_PREPARED = "baseline versus prepared"
PREPARED_VERSUS_ARCHIVE = "prepared versus archive rebuild"
FIRST_BASELINE_REPEAT = "baseline repeat 2"
# Comparison rules that record the comparison itself rather than a result of it.
_COMPARISON_RECORDS = frozenset({"compare.tools", "compare.baseline_stability"})
# The results of comparing two PDFs, by rule: page count (CMP001), extracted text (CMP002)
# and exact renders (CMP003).
ALL_MATCH = {
    "compare.page_count": ["passed"],
    "compare.text": ["passed"],
    "compare.rendering": ["passed"],
}
WORDING_CHANGED = {
    "compare.page_count": ["passed"],
    "compare.text": ["failed"],
    "compare.rendering": ["failed"],
}
_EMBEDDED_PNG = re.compile(r"data:image/png;base64,([A-Za-z0-9+/=]+)")
# The PNG file signature, written out rather than imported from the code under test.
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# More chunks than a page image of a renderer holds: pdftoppm splits its pixel data
# into many IDAT chunks.
_MAX_PNG_CHUNKS = 4096

# Settings for `fledge prepare`: a guarded edit of the cited entry's title, which changes the
# rendered reference list, so the baseline and prepared PDFs differ in text and pixels.
TITLE_EDIT = f"""\
source_date_epoch = {EPOCH}
job_timeout_seconds = {JOB_TIMEOUT_SECONDS}

[[bibliography_transform.field_edits]]
key = "knuth1984"
field = "title"
expected = "The {{TeXbook}}"
replacement = "The {{TeXbook}}, revised"
"""
REVISED_TITLE = "The {TeXbook}, revised"
# Two eligible decisions for that difference, and one for the archive rebuild comparison.
# That comparison passes, so its decision can change nothing and is reported as ineligible.
REVIEW_DECISIONS = """
[[reporting.accepted_exceptions]]
code = "CMP002"
stage = "baseline versus prepared"
reason = "Reviewed: the reference title was edited on purpose."

[[reporting.accepted_exceptions]]
code = "CMP003"
stage = "baseline versus prepared"
reason = "Reviewed: the reference title was edited on purpose."

[[reporting.accepted_exceptions]]
code = "CMP003"
stage = "prepared versus archive rebuild"
reason = "Targets the archive rebuild comparison, which passes."
"""

TWO_PAGES = (
    "\\documentclass{article}\n\\begin{document}\nFirst page.\n\\newpage\nSecond page.\n"
    "\\end{document}\n"
)


def _marker_document(marker: str) -> str:
    """A one-page document that prints ``marker``."""
    return (
        "\\documentclass{article}\n\\begin{document}\n"
        f"Run marker {marker}.\n\\end{{document}}\n"
    )


# pdfTeX seeds its random numbers from the clock, not from SOURCE_DATE_EPOCH, so every build
# of this document prints another number; the control prints a fixed one.
UNSTABLE_DOCUMENT = _marker_document("\\pdfuniformdeviate 1000000")
STABLE_DOCUMENT = _marker_document("123456")


def _settings(**options: Any) -> Settings:
    """Settings pinned for reproducible PDFs, with ``main.tex`` as the main file."""
    return Settings(
        main="main.tex",
        source_date_epoch=EPOCH,
        job_timeout_seconds=JOB_TIMEOUT_SECONDS,
        **options,
    )


def _run(request: JobRequest) -> Report:
    """Run one job to its end on a new event loop, as the command line does."""
    return asyncio.run(run_job(request))


def _problems(report: Report) -> list[tuple[str | None, str, str, str | None, str]]:
    """The findings that keep a run from passing cleanly, for assertion messages."""
    return [
        (item.code, item.rule, item.status, item.stage, item.message)
        for item in report.findings
        if item.severity != "info" and item.status != "passed"
    ]


def _comparison_results(report: Report, stage: str | None) -> dict[str, list[str]]:
    """The status of each comparison result recorded at ``stage``, by rule.

    ``None`` selects results without a stage, as the standalone PDF check records them. A
    comparison that could not run appears as ``compare.unavailable``.
    """
    results: dict[str, list[str]] = {}
    for item in report.findings:
        if (
            item.rule.startswith("compare.")
            and item.rule not in _COMPARISON_RECORDS
            and item.stage == stage
        ):
            results.setdefault(item.rule, []).append(item.status)
    return results


def _statuses(report: Report, code: str) -> list[str]:
    return [item.status for item in report.findings if item.code == code]


def _as_reported(path: Path) -> str:
    """How a shareable report shows ``path``, which lies in the temporary directory."""
    return "<tmp>/" + path.relative_to(Path(tempfile.gettempdir()).resolve()).as_posix()


def _png_chunk_kinds(data: bytes) -> list[bytes]:
    """The chunk types of a complete PNG, read after its signature and checksums verified."""
    return [chunk.kind for chunk in iter_png_chunks(data, max_chunks=_MAX_PNG_CHUNKS)]


class _CommandRun(NamedTuple):
    """What one in-process ``fledge prepare`` left behind."""

    exit_code: int
    output: Path
    report: dict[str, Any]
    html: str
    console: str


@live_test("Set FLEDGE_RUN_INTEGRATION=1 for live PDF comparison, review and preview checks")
class LiveComparisonTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="fledge-comparisons-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()

    def copy_fixture(self) -> Path:
        """A copy of examples/nested-paper, which cites ``knuth1984`` from a BibTeX file."""
        source = self.base / "paper"
        shutil.copytree(FIXTURE, source)
        return source

    def replace_once(self, path: Path, old: str, new: str) -> None:
        text = path.read_text(encoding="utf-8")
        self.assertEqual(text.count(old), 1, f"{path.name} must contain {old!r} once")
        path.write_text(text.replace(old, new), encoding="utf-8")

    def prepare(self, source: Path, name: str, settings: Settings | None = None) -> Report:
        """Prepare ``source`` into the new directory ``name``; the run must pass."""
        output = self.base / name
        report = _run(JobRequest("prepare", source, settings or _settings(), output))
        self.assertIn(report.outcome, PASSING, _problems(report))
        self.assertTrue((output / "submission.zip").is_file())
        return report

    def check(self, source: Path, reference: Path) -> Report:
        return _run(JobRequest("check", source, _settings(), reference_pdf=reference))

    def prepare_with_cli(self, source: Path, name: str, settings_text: str) -> _CommandRun:
        """Run ``fledge prepare`` in-process with HTML and JSON reports."""
        config = write_file(self.base, f"{name}.toml", settings_text)
        output = self.base / f"{name}-output"
        html, saved = self.base / f"{name}.html", self.base / f"{name}.json"
        result = CliRunner().invoke(
            cli,
            [
                *("prepare", str(source), "--main", "main.tex", "--config", str(config)),
                *("--output", str(output), "--html-report", str(html)),
                *("--json-report", str(saved), "--quiet"),
            ],
        )
        self.assertTrue(saved.is_file(), (result.output, result.exception))
        return _CommandRun(
            result.exit_code,
            output,
            json.loads(saved.read_text(encoding="utf-8")),
            html.read_text(encoding="utf-8"),
            result.output,
        )

    def assert_grayscale_pngs(self, paths: list[Path]) -> None:
        """Each file is a complete 8-bit grayscale PNG whose first chunk is IHDR."""
        self.assertTrue(paths)
        for path in paths:
            with self.subTest(preview=path.name):
                data = path.read_bytes()
                self.assertEqual(data[: len(PNG_SIGNATURE)], PNG_SIGNATURE)
                kinds = _png_chunk_kinds(data)
                self.assertEqual([kinds[0], kinds[-1]], [b"IHDR", b"IEND"])
                header = read_png_header(data)
                assert header is not None
                self.assertEqual((header.bit_depth, header.color_type), (8, 0))
                self.assertGreater(min(header.width, header.height), 0)

    def assert_previews_name(self, report: Report, exported: list[Path]) -> None:
        """Every PDF311 finding passed and together they name exactly the exported files."""
        previews = [item for item in report.findings if item.code == "PDF311"]
        self.assertEqual({item.status for item in previews}, {"passed"}, previews)
        named = sorted(name for item in previews for name in item.details["artifacts"])
        self.assertEqual(named, sorted(_as_reported(path) for path in exported))

    def assert_accepted_failures(self, report: dict[str, Any]) -> None:
        """The accepted comparison failures stay failed and carry their review decision."""
        reviewed = [
            finding
            for finding in report["findings"]
            if finding["code"] in {"CMP002", "CMP003"}
            and finding["details"].get("stage") == BASELINE_VERSUS_PREPARED
        ]
        self.assertEqual(
            sorted((finding["code"], finding["status"]) for finding in reviewed),
            [("CMP002", "failed"), ("CMP003", "failed")],
        )
        for finding in reviewed:
            with self.subTest(code=finding["code"]):
                actions = [entry["action"] for entry in finding["details"]["review"]]
                self.assertEqual(actions, ["accepted_exception"])

    def assert_archive_decision_ineligible(self, report: dict[str, Any]) -> None:
        """The decision that targets the passed archive comparison is reported as ineligible."""
        archive = [
            finding
            for finding in report["findings"]
            if finding["code"] == "CMP003"
            and finding["details"].get("stage") == PREPARED_VERSUS_ARCHIVE
        ]
        self.assertEqual([finding["status"] for finding in archive], ["passed"])
        self.assertNotIn("review", archive[0]["details"])
        diagnostics = [
            (
                finding["rule"],
                finding["details"]["requested_review"]["code"],
                finding["details"]["requested_review"]["stage"],
            )
            for finding in report["findings"]
            if finding["rule"].startswith("report.review_")
        ]
        # The other two decisions matched eligible failures, so they add no diagnostic.
        self.assertEqual(
            diagnostics, [("report.review_ineligible", "CMP003", PREPARED_VERSUS_ARCHIVE)]
        )

    def assert_previews_embedded(self, run: _CommandRun) -> None:
        """The HTML embeds before and after pages; the JSON describes them without bytes."""
        images = [base64.b64decode(data) for data in _EMBEDDED_PNG.findall(run.html)]
        self.assertGreaterEqual(len(images), 2)
        for image in images:
            self.assertEqual(image[: len(PNG_SIGNATURE)], PNG_SIGNATURE)
            kinds = _png_chunk_kinds(image)
            self.assertEqual([kinds[0], kinds[-1]], [b"IHDR", b"IEND"])
        summary = run.report["execution"]["pdf_previews"]
        self.assertGreaterEqual(summary["captured_page_pairs"], 1, summary)
        compared = [
            item for item in summary["comparisons"] if item["stage"] == BASELINE_VERSUS_PREPARED
        ]
        self.assertEqual(len(compared), 1, summary)
        pages = compared[0]["pages"]
        self.assertTrue(pages, compared)
        self.assertEqual({page["status"] for page in pages}, {"captured"}, pages)
        self.assertTrue(any(page["text_changed"] for page in pages), pages)

    def test_a_supplied_reference_pdf_must_match_the_build_exactly(self) -> None:
        """A supplied reference PDF is compared page by page, exactly, in check and pdf check.

        Documented in docs/preparation-options.md ("Reports and standalone PDF inspection")
        and docs/workflow-reference.md ("Supported source syntax"): ``--reference-pdf``
        compares page counts, extracted text and exact 144-DPI renders (CMP001 to CMP003);
        supplied reference PDFs are compared exactly, and a difference blocks the run.
        """
        # Each phase needs the PDFs prepared before it, so only the comparisons are subtests.
        source = self.copy_fixture()
        original = Path(self.prepare(source, "original").artifacts["pdf"])
        with self.subTest(phase="check against the PDF of the same sources"):
            report = self.check(source, original)
            self.assertEqual(
                _comparison_results(report, REFERENCE_VERSUS_BASELINE),
                ALL_MATCH,
                _problems(report),
            )
            self.assertIn(report.outcome, PASSING, _problems(report))

        self.replace_once(source / "main.tex", "A small submission", "A short submission")
        with self.subTest(phase="check after changing one word"):
            report = self.check(source, original)
            self.assertEqual(
                _comparison_results(report, REFERENCE_VERSUS_BASELINE), WORDING_CHANGED
            )
            self.assertEqual(report.outcome, "blocked")

        revised = Path(self.prepare(source, "revised").artifacts["pdf"])
        with self.subTest(phase="pdf check of two different PDFs"):
            report = _run(JobRequest("pdf", revised, _settings(), reference_pdf=original))
            self.assertEqual(_comparison_results(report, None), WORDING_CHANGED, _problems(report))
            self.assertEqual(report.outcome, "blocked")
        with self.subTest(phase="pdf check of one PDF against itself"):
            report = _run(JobRequest("pdf", original, _settings(), reference_pdf=original))
            self.assertEqual(_comparison_results(report, None), ALL_MATCH, _problems(report))
            self.assertIn(report.outcome, PASSING, _problems(report))

    def test_an_accepted_comparison_exception_releases_the_reviewed_edit(self) -> None:
        """Accepted exceptions release an intended change and keep its evidence visible.

        Documented in docs/preparation-options.md ("Preservation and explicit exceptions")
        and docs/reports.md ("Exit codes", "Visual PDF differences"): accepted exceptions
        keep the original failed result, a release with them reports
        ``accepted_exceptions`` (exit 2), and ineligible decisions are reported: here one
        decision targets the archive rebuild comparison, which passed, and is reported as
        ineligible. ``--html-report`` embeds before/after pages and JSON describes them in
        ``execution.pdf_previews``. Without the decisions the same edit blocks (exit 3) and
        nothing is written.
        """
        source = self.copy_fixture()
        with self.subTest(phase="with review decisions"):
            accepted = self.prepare_with_cli(source, "accepted", TITLE_EDIT + REVIEW_DECISIONS)
            self.assertEqual(accepted.exit_code, 2, accepted.console)
            self.assertEqual(accepted.report["outcome"], "accepted_exceptions")
            with zipfile.ZipFile(accepted.output / "submission.zip") as archive:
                bibliography = archive.read("references/library.bib").decode("utf-8")
            self.assertIn(REVISED_TITLE, bibliography)
            self.assert_accepted_failures(accepted.report)
            self.assert_archive_decision_ineligible(accepted.report)
            self.assert_previews_embedded(accepted)

        with self.subTest(phase="without review decisions"):
            refused = self.prepare_with_cli(source, "refused", TITLE_EDIT)
            self.assertEqual(refused.exit_code, 3, refused.console)
            self.assertEqual(refused.report["outcome"], "blocked")
            self.assertFalse(refused.output.exists())
            blocking = {
                (finding["code"], finding["status"])
                for finding in refused.report["findings"]
                if finding["details"].get("stage") == BASELINE_VERSUS_PREPARED
            }
            self.assertIn(("CMP002", "failed"), blocking)

    def test_grayscale_previews_are_saved_outside_the_submission_archive(self) -> None:
        """Grayscale previews are one PNG per page, saved beside the bundle, never inside it.

        Documented in docs/preparation-options.md ("Reports and standalone PDF inspection"):
        grayscale previews are PNG pages kept outside the source ZIP, in a new
        ``--preview-output`` directory for ``check`` and under ``previews/`` in a
        preparation output; PDF311 records each retained preview.
        """
        source = write_project(self.base / "two-pages", {"main.tex": TWO_PAGES})
        settings = _settings(rendered_pdf_artwork=PdfArtworkOptions(grayscale_preview=True))
        directory = self.base / "gray-pages"
        report = _run(JobRequest("check", source, settings, preview_output=directory))
        self.assertIn(report.outcome, PASSING, _problems(report))
        exported = sorted(directory.glob("*.png"))
        self.assertEqual(len(exported), 2, exported)
        self.assert_grayscale_pngs(exported)
        pages = sorted(item.details["page"] for item in report.findings if item.code == "PDF311")
        self.assertEqual(pages, [1, 2])
        self.assert_previews_name(report, exported)

        report = self.prepare(source, "prepared", settings)
        bundled = sorted((self.base / "prepared" / "previews").glob("*.png"))
        self.assert_grayscale_pngs(bundled)
        self.assert_previews_name(report, bundled)
        with zipfile.ZipFile(self.base / "prepared" / "submission.zip") as archive:
            names = archive.namelist()
        self.assertEqual([name for name in names if name.lower().endswith(".png")], [], names)

    def test_repeated_baselines_block_a_document_that_builds_differently(self) -> None:
        """Repeated baseline builds must match exactly, or preparation stops.

        Documented in docs/preparation-options.md ("Preservation and explicit exceptions"),
        docs/workflow-reference.md ("Supported source syntax") and the CMP101 catalogue
        entry: ``workflow.baseline_runs`` builds the unmodified sources again and requires
        identical page counts, text and exact renders before any transformation; a
        mismatch blocks the run (exit 3), so no bundle is written.
        """
        settings = _settings(workflow=WorkflowOptions(baseline_runs=2))
        unstable = write_project(self.base / "unstable", {"main.tex": UNSTABLE_DOCUMENT})
        output = self.base / "unstable-output"
        report = _run(JobRequest("prepare", unstable, settings, output))
        self.assertEqual(_statuses(report, "CMP101"), ["failed"], _problems(report))
        self.assertEqual(
            _comparison_results(report, FIRST_BASELINE_REPEAT)["compare.text"], ["failed"]
        )
        self.assertEqual(report.outcome, "blocked")
        self.assertFalse(output.exists())
        self.assertEqual(report.artifacts, {})

        stable = write_project(self.base / "stable", {"main.tex": STABLE_DOCUMENT})
        report = self.prepare(stable, "stable-output", settings)
        self.assertEqual(_statuses(report, "CMP101"), ["passed"], _problems(report))
        self.assertEqual(_comparison_results(report, FIRST_BASELINE_REPEAT), ALL_MATCH)


if __name__ == "__main__":
    unittest.main()
