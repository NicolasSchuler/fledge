"""The published JSON Schema describes exactly the report fields the code writes."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from latexprep.findings import SEVERITIES, STATUSES, EvidenceKind, Finding
from latexprep.report import (
    REPORT_COMMANDS,
    REPORT_SCHEMA_VERSION,
    Change,
    ChangeKind,
    Outcome,
    Report,
)
from tests.support import schema_keyword_problems, schema_violations

SCHEMA = Path(__file__).resolve().parents[1] / "docs" / "_static" / "report.schema.json"


class ReportSchemaTests(unittest.TestCase):
    """The schema's fields and enumerations match a written report and the code."""

    def setUp(self) -> None:
        """Load the published schema and write a blocked report with one finding and change."""
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
        # "report" names no command; the withheld report keeps the command that was run.
        self.assertNotIn("report", REPORT_COMMANDS)
        self.assertEqual(set(properties["outcome"]["enum"]), set(Outcome))
        finding = self.schema["$defs"]["finding"]["properties"]
        self.assertEqual(set(finding["severity"]["enum"]), SEVERITIES)
        self.assertEqual(set(finding["status"]["enum"]), STATUSES)
        self.assertEqual(set(finding["evidence_kind"]["enum"]), set(EvidenceKind))
        change = self.schema["$defs"]["change"]["properties"]
        self.assertEqual(set(change["kind"]["enum"]), set(ChangeKind))


class SchemaValidatorTests(unittest.TestCase):
    """The small validator behind the report-conformance test checks what it claims to."""

    def violations(self, instance: object, schema: dict[str, object]) -> list[str]:
        """Return the violations of ``schema`` that the validator finds in ``instance``."""
        return schema_violations(instance, schema)

    def test_types_include_unions_and_exclude_booleans_from_numbers(self) -> None:
        self.assertEqual(self.violations(None, {"type": ["string", "null"]}), [])
        self.assertEqual(self.violations(3, {"type": "integer"}), [])
        self.assertEqual(self.violations(2.5, {"type": "number"}), [])
        self.assertTrue(self.violations(True, {"type": "integer"}))
        self.assertTrue(self.violations(True, {"type": "number"}))
        self.assertTrue(self.violations("3", {"type": "integer"}))
        self.assertTrue(self.violations(1, {"type": ["string", "null"]}))

    def test_constants_and_enumerations(self) -> None:
        self.assertEqual(self.violations(2, {"const": 2}), [])
        self.assertTrue(self.violations(3, {"const": 2}))
        self.assertEqual(self.violations("a", {"enum": ["a", "b"]}), [])
        self.assertTrue(self.violations("c", {"enum": ["a", "b"]}))

    def test_objects_check_required_declared_and_additional_properties(self) -> None:
        schema = {
            "type": "object",
            "required": ["name"],
            "properties": {"name": {"type": "string"}, "size": {"type": "integer"}},
            "additionalProperties": False,
        }
        self.assertEqual(self.violations({"name": "x", "size": 1}, schema), [])
        self.assertIn("missing required property 'name'", self.violations({}, schema)[0])
        self.assertTrue(self.violations({"name": 1}, schema))
        self.assertIn(
            "unexpected property 'other'", self.violations({"name": "x", "other": 1}, schema)[0]
        )
        typed = {"type": "object", "additionalProperties": {"type": "string"}}
        self.assertEqual(self.violations({"a": "b"}, typed), [])
        self.assertTrue(self.violations({"a": 1}, typed))

    def test_arrays_check_every_item_and_local_references_resolve(self) -> None:
        schema = {
            "type": "array",
            "items": {"$ref": "#/$defs/name"},
            "$defs": {"name": {"type": "string"}},
        }
        self.assertEqual(self.violations(["a", "b"], schema), [])
        problems = self.violations(["a", 2], schema)
        self.assertEqual(len(problems), 1)
        self.assertIn("$[1]", problems[0])

    def test_a_keyword_it_does_not_check_is_refused_rather_than_ignored(self) -> None:
        for keyword in ("pattern", "minimum", "oneOf", "allOf", "format"):
            with self.subTest(keyword=keyword), self.assertRaisesRegex(ValueError, keyword):
                self.violations("x", {"type": "string", keyword: "x"})
        nested = {"properties": {"a": {"items": {"minItems": 1}}}}
        with self.assertRaisesRegex(ValueError, "minItems"):
            self.violations({}, nested)
        # A property that is merely called like a keyword is not a keyword.
        named = {"properties": {"pattern": {"type": "string"}}}
        self.assertEqual(self.violations({"pattern": "x"}, named), [])

    def test_the_published_report_schema_only_uses_checked_keywords(self) -> None:
        self.assertEqual(
            schema_keyword_problems(json.loads(SCHEMA.read_text(encoding="utf-8"))), []
        )

    def test_a_report_that_breaks_the_schema_is_reported_with_its_location(self) -> None:
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        report = Report("check", findings=[Finding("rule.name", "message")]).to_dict()
        self.assertEqual(self.violations(report, schema), [])
        report["outcome"] = "unknown"
        report["findings"][0]["severity"] = "fatal"
        problems = self.violations(report, schema)
        self.assertEqual(len(problems), 2, problems)
        self.assertTrue(any(item.startswith("$.outcome") for item in problems))
        self.assertTrue(any(item.startswith("$.findings[0].severity") for item in problems))


if __name__ == "__main__":
    unittest.main()
