"""The published JSON Schema describes exactly the report fields the code writes."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from latexprep.models import (
    REPORT_COMMANDS,
    REPORT_SCHEMA_VERSION,
    SEVERITIES,
    STATUSES,
    Change,
    ChangeKind,
    EvidenceKind,
    Finding,
    Outcome,
    Report,
)

SCHEMA = Path(__file__).resolve().parents[1] / "docs" / "_static" / "report.schema.json"


class ReportSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        report = Report(
            "check",
            outcome=Outcome.BLOCKED,
            main="main.tex",
            findings=[
                Finding(
                    "bibliography.citation_coverage",
                    "Citation key 'knuth1948' has no entry.",
                    severity="error",
                    path="main.tex",
                    line=5,
                )
            ],
            changes=[
                Change("main.tex", kind=ChangeKind.REWRITE, reason="Removed private comments")
            ],
        )
        self.report = report.to_dict()

    def test_schema_fields_match_the_written_report(self) -> None:
        self.assertEqual(next(iter(self.report)), "schema_version")
        self.assertEqual(self.report["schema_version"], REPORT_SCHEMA_VERSION)
        self.assertEqual(
            self.schema["properties"]["schema_version"]["const"], REPORT_SCHEMA_VERSION
        )
        self.assertEqual(set(self.schema["required"]), set(self.report))
        self.assertEqual(set(self.schema["properties"]), set(self.report))
        definitions = self.schema["$defs"]
        findings = self.report["findings"]
        changes = self.report["changes"]
        assert isinstance(findings, list) and isinstance(changes, list)
        self.assertEqual(set(definitions["finding"]["required"]), set(findings[0]))
        self.assertEqual(set(definitions["finding"]["properties"]), set(findings[0]))
        self.assertEqual(set(definitions["change"]["required"]), set(changes[0]))
        self.assertEqual(set(definitions["change"]["properties"]), set(changes[0]))

    def test_schema_enumerations_match_the_code(self) -> None:
        properties = self.schema["properties"]
        self.assertEqual(set(properties["command"]["enum"]), REPORT_COMMANDS)
        self.assertEqual(set(properties["outcome"]["enum"]), set(Outcome))
        finding = self.schema["$defs"]["finding"]["properties"]
        self.assertEqual(set(finding["severity"]["enum"]), SEVERITIES)
        self.assertEqual(set(finding["status"]["enum"]), STATUSES)
        self.assertEqual(set(finding["evidence_kind"]["enum"]), set(EvidenceKind))
        change = self.schema["$defs"]["change"]["properties"]
        self.assertEqual(set(change["kind"]["enum"]), set(ChangeKind))


if __name__ == "__main__":
    unittest.main()
