"""Review decisions in the preparation workflow: what may be accepted, and what may not."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from latexprep.config import Settings
from latexprep.core import run_job
from latexprep.findings import Finding
from latexprep.job_context import JobRequest
from latexprep.review_policy import BASELINE_VERSUS_PREPARED_STAGE, ReportingOptions, ReviewRule
from tests.support import controlled_backends, minimal_project


class WorkflowReviewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.source = self.base / "input"
        minimal_project(self.source)

    async def test_reviewed_page_policy_keeps_failure_visible_and_marks_output(self):
        settings = Settings(
            reporting=ReportingOptions(
                accepted_exceptions=(ReviewRule("PDF001", "Approved extra page"),)
            )
        )
        failure = Finding("pdf.page_limit", "Too many pages", severity="error")
        backends = controlled_backends(inspect_pdf=[failure])
        result = await run_job(
            JobRequest("prepare", self.source, settings, self.base / "out"), backends=backends
        )
        self.assertEqual(result.outcome, "accepted_exceptions", result.to_dict())
        findings = [item for item in result.findings if item.code == "PDF001"]
        self.assertTrue(findings)
        self.assertTrue(
            all(item.status == "failed" and item.details.get("review") for item in findings)
        )
        saved = json.loads((self.base / "out/report.json").read_text())
        self.assertEqual(saved["outcome"], "accepted_exceptions")

    async def test_baseline_difference_is_accepted_at_the_stage_the_workflow_records(self):
        settings = Settings(
            reporting=ReportingOptions(
                accepted_exceptions=(
                    ReviewRule(
                        "CMP003", "Intended new appearance", stage=BASELINE_VERSUS_PREPARED_STAGE
                    ),
                )
            )
        )
        changed = Finding("compare.rendering", "Pixels differ", severity="error")

        async def compare(_left, _right, work, *_args, **_options):
            return [changed] if work.name == "compare-prepared" else []

        backends = controlled_backends(compare_pdfs=compare)
        result = await run_job(
            JobRequest("prepare", self.source, settings, self.base / "out"), backends=backends
        )
        stages = {item.details.get("stage") for item in result.findings if item.code == "CMP003"}
        self.assertEqual(stages, {BASELINE_VERSUS_PREPARED_STAGE})
        self.assertEqual(result.outcome, "accepted_exceptions", result.to_dict())

    async def test_archive_mismatch_cannot_be_waived(self):
        settings = Settings(
            reporting=ReportingOptions(
                accepted_exceptions=(
                    ReviewRule(
                        "CMP003", "Intended new appearance", stage=BASELINE_VERSUS_PREPARED_STAGE
                    ),
                )
            )
        )
        changed = Finding("compare.rendering", "Pixels differ", severity="error")
        backends = controlled_backends(compare_pdfs=[changed])
        result = await run_job(
            JobRequest("prepare", self.source, settings, self.base / "out"), backends=backends
        )
        self.assertEqual(result.outcome, "blocked")
        self.assertFalse((self.base / "out").exists())
        self.assertTrue(
            any(
                item.details.get("stage") == "prepared versus archive rebuild"
                and item.status == "failed"
                for item in result.findings
            )
        )


if __name__ == "__main__":
    unittest.main()
