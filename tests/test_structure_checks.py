from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from latexprep.models import PreparationError
from latexprep.option_config import read_options
from latexprep.scheduler import ResourceBudget
from latexprep.structure_checks import (
    HeadingExpectation,
    StructureOptions,
    check_author_records,
    inspect_pdf_structure,
)
from tests.test_pdf_checks import DetailRunner


def structured_pdf_objects() -> dict[str, object]:
    def entry(value: dict[str, object]) -> dict[str, object]:
        return {"value": value}

    objects = {
        "trailer": entry({"/Root": "1 0 R"}),
        "obj:1 0 R": entry({"/Type": "/Catalog", "/Pages": "2 0 R", "/StructTreeRoot": "4 0 R"}),
        "obj:2 0 R": entry({"/Type": "/Pages", "/Kids": ["3 0 R"], "/Count": 1}),
        "obj:3 0 R": entry(
            {"/Type": "/Page", "/Parent": "2 0 R", "/StructParents": 0, "/Annots": ["10 0 R"]}
        ),
        "obj:4 0 R": entry({"/Type": "/StructTreeRoot", "/K": ["5 0 R"], "/ParentTree": "11 0 R"}),
        "obj:5 0 R": entry(
            {
                "/Type": "/StructElem",
                "/S": "/Document",
                "/P": "4 0 R",
                "/Pg": "3 0 R",
                "/K": ["6 0 R", "9 0 R", "12 0 R"],
            }
        ),
        "obj:6 0 R": entry(
            {"/Type": "/StructElem", "/S": "/Table", "/P": "5 0 R", "/K": ["7 0 R"]}
        ),
        "obj:7 0 R": entry({"/Type": "/StructElem", "/S": "/TR", "/P": "6 0 R", "/K": ["8 0 R"]}),
        "obj:8 0 R": entry({"/Type": "/StructElem", "/S": "/TH", "/P": "7 0 R", "/K": 0}),
        "obj:9 0 R": entry(
            {
                "/Type": "/StructElem",
                "/S": "/Figure",
                "/P": "5 0 R",
                "/K": 1,
                "/Alt": "u:Measured result chart",
            }
        ),
        "obj:10 0 R": entry(
            {
                "/Type": "/Annot",
                "/Subtype": "/Link",
                "/StructParent": 1,
                "/A": {"/S": "/URI", "/URI": "u:https://example.org"},
            }
        ),
        "obj:11 0 R": entry({"/Nums": [0, ["8 0 R", "9 0 R", "12 0 R"], 1, "12 0 R"]}),
        "obj:12 0 R": entry(
            {
                "/Type": "/StructElem",
                "/S": "/Link",
                "/P": "5 0 R",
                "/K": [2, {"/Type": "/OBJR", "/Pg": "3 0 R", "/Obj": "10 0 R"}],
            }
        ),
    }
    return {
        "version": 2,
        "pages": [{"object": "3 0 R", "pageposfrom1": 1}],
        "qpdf": [{"jsonversion": 2}, objects],
    }


def object_value(document: dict[str, object], number: int) -> dict[str, object]:
    return document["qpdf"][1][f"obj:{number} 0 R"]["value"]


class AuthorRecordTests(unittest.TestCase):
    def check(self, preamble: str, options: StructureOptions | None = None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "main.tex").write_text(
                "\\documentclass{article}\n"
                + preamble
                + "\n\\begin{document}Body.\\end{document}\n"
            )
            return check_author_records(
                root,
                "main.tex",
                options or StructureOptions(required_author_fields=("affiliation", "email")),
            )

    def test_each_author_needs_its_own_fields(self) -> None:
        complete = (
            "\\author{Ada}\\affiliation{Lab A}\\email{ada@example.org}\n"
            "\\author{Grace}\\affiliation{Lab B}\\email{grace@example.org}"
        )
        passed = self.check(complete)[0]
        self.assertEqual(passed.code, "MAN201")
        self.assertEqual(passed.status, "passed")
        self.assertEqual(len(passed.details["records"]), 2)
        failed = self.check(complete.replace("\\email{grace@example.org}", ""))[0]
        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.details["violations"][0]["author_index"], 2)
        self.assertEqual(failed.details["violations"][0]["missing_fields"], ["email"])
        custom = self.check(
            "\\newcommand{\\person}[1]{#1}\n"
            "\\person{Ada}\\contact{ada@example.org}\n% \\person{Ignored}\n",
            StructureOptions(author_command="person", required_author_fields=("contact",)),
        )[0]
        self.assertEqual(custom.status, "passed")
        self.assertEqual(len(custom.details["records"]), 1)
        self.assertEqual(self.check("\\email{orphan@example.org}\\author{Ada}")[0].status, "failed")

    def test_grouped_and_dynamic_records_are_inconclusive(self) -> None:
        for source in (
            "\\author{Ada}\\affiliation{Lab}\\box{\\email{ada@example.org}}",
            "\\author{\\hiddenname}\\affiliation{Lab}\\email{ada@example.org}",
            "\\iftrue\\author{Ada}\\affiliation{Lab}\\email{ada@example.org}\\fi",
        ):
            with self.subTest(source=source):
                self.assertEqual(self.check(source)[0].status, "inconclusive")

    def test_defined_conditional_names_do_not_block_author_records(self) -> None:
        result = self.check(
            "\\newif\\ifanonymous\\anonymousfalse\n"
            "\\author{Ada}\\affiliation{Lab A}\\email{ada@example.org}"
        )[0]
        self.assertEqual(result.status, "passed", result.details["uncertainty"])

    def test_failed_author_records_report_counts_and_locations(self) -> None:
        result = self.check("\\author{Ada}\\affiliation{Lab A}\n\\author{Grace}")[0]
        self.assertEqual(result.status, "failed")
        self.assertEqual(
            result.message,
            "2 of 2 literal author records lack a configured field: main.tex:2, main.tex:3.",
        )

    def test_option_schema_is_explicit_and_frozen(self) -> None:
        parsed = read_options(
            StructureOptions,
            {
                "required_author_fields": ["email"],
                "heading_expectations": [{"page": 1, "title": "Methods", "number": "2"}],
            },
            "structure_checks",
        )
        self.assertEqual(parsed.heading_expectations, (HeadingExpectation(1, "Methods", "2"),))
        for values in (
            {"author_command": "\\author"},
            {"required_author_fields": ("email", "email")},
            {"required_author_fields": ("author",)},
            {"require_structure_tree": 1},
            {
                "heading_expectations": (
                    HeadingExpectation(1, "Methods", "2"),
                    HeadingExpectation(1, "Methods", "3"),
                )
            },
        ):
            with self.subTest(values=values), self.assertRaises(PreparationError):
                StructureOptions(**values)
        with self.assertRaises(PreparationError):
            HeadingExpectation(True, "Methods", "2")


class PdfStructureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.pdf = self.root / "document.pdf"
        self.pdf.write_bytes(b"%PDF-controlled-test")
        self.count = 0

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    async def check(self, options: StructureOptions, document=None, **outputs):
        self.count += 1
        runner = DetailRunner(
            info="Pages: 1\n",
            objects=json.dumps(document or structured_pdf_objects()),
            text="2 Methods\nBody.\f",
            **outputs,
        )
        result = await inspect_pdf_structure(
            self.pdf, self.root / f"inspect-{self.count}", runner, options
        )
        return result

    async def test_heading_expectations_match_differ_and_remain_uncertain(self) -> None:
        options = StructureOptions(heading_expectations=(HeadingExpectation(1, "Methods", "2"),))
        passed = (await self.check(options))[0]
        self.assertEqual(passed.code, "PDF401")
        self.assertEqual(passed.status, "passed")
        wrong = replace(options, heading_expectations=(HeadingExpectation(1, "Methods", "3"),))
        self.assertEqual((await self.check(wrong))[0].status, "failed")
        outside = replace(options, heading_expectations=(HeadingExpectation(2, "Methods", "2"),))
        self.assertEqual((await self.check(outside))[0].status, "inconclusive")
        for text in ("\f", "2 Methods\n2 Methods\f", "2 Methods without page boundary"):
            self.count += 1
            result = await inspect_pdf_structure(
                self.pdf,
                self.root / f"inspect-{self.count}",
                DetailRunner(info="Pages: 1\n", text=text),
                options,
            )
            self.assertEqual(result[0].status, "inconclusive")

    async def test_tagged_tables_have_rows_and_cells(self) -> None:
        options = StructureOptions(check_table_structure=True)
        passed = (await self.check(options))[0]
        self.assertEqual(passed.code, "PDF402")
        self.assertEqual(passed.status, "passed")
        self.assertEqual(passed.details["tables"][0]["header_cells"], 1)
        document = structured_pdf_objects()
        object_value(document, 8)["/S"] = "/P"
        self.assertEqual((await self.check(options, document))[0].status, "failed")
        document = structured_pdf_objects()
        object_value(document, 6)["/K"] = []
        self.assertEqual((await self.check(options, document))[0].status, "failed")

    async def test_link_tags_match_real_page_annotations(self) -> None:
        options = StructureOptions(check_link_structure=True)
        passed = (await self.check(options))[0]
        self.assertEqual(passed.code, "PDF403")
        self.assertEqual(passed.status, "passed")
        for change in (
            "association",
            "target",
            "empty_uri",
            "empty_destination",
            "wrong_page",
            "repeated",
        ):
            document = structured_pdf_objects()
            if change == "association":
                object_value(document, 12)["/K"] = 2
            elif change == "target":
                object_value(document, 10).pop("/A")
            elif change == "empty_uri":
                object_value(document, 10)["/A"] = {"/S": "/URI", "/URI": "u:"}
            elif change == "empty_destination":
                object_value(document, 10)["/A"] = {"/S": "/GoTo", "/D": None}
            elif change == "wrong_page":
                object_value(document, 12)["/K"] = [
                    2,
                    {"/Type": "/OBJR", "/Pg": "90 0 R", "/Obj": "10 0 R"},
                ]
            else:
                object_value(document, 3)["/Annots"] = ["10 0 R", "10 0 R"]
            with self.subTest(change=change):
                self.assertEqual((await self.check(options, document))[0].status, "failed")
        for destination, status in (
            (["3 0 R", "/Fit"], "passed"),
            (["90 0 R", "/Fit"], "failed"),
            ("u:named-destination", "inconclusive"),
        ):
            document = structured_pdf_objects()
            object_value(document, 10)["/A"] = {"/S": "/GoTo", "/D": destination}
            with self.subTest(destination=destination):
                self.assertEqual((await self.check(options, document))[0].status, status)

    async def test_figure_alt_presence_and_unreadable_strings(self) -> None:
        options = StructureOptions(require_figure_alt=True)
        passed = (await self.check(options))[0]
        self.assertEqual(passed.code, "PDF404")
        self.assertEqual(passed.status, "passed")
        for alt, status in ((None, "failed"), ("u:  ", "failed"), ("b:FEFF0041", "inconclusive")):
            document = structured_pdf_objects()
            object_value(document, 9)["/Alt"] = alt
            self.assertEqual((await self.check(options, document))[0].status, status)
        document = structured_pdf_objects()
        object_value(document, 9)["/S"] = "/Picture"
        self.assertEqual((await self.check(options, document))[0].status, "inconclusive")
        object_value(document, 4)["/RoleMap"] = {"/Picture": "/Figure"}
        self.assertEqual((await self.check(options, document))[0].status, "passed")

    async def test_structure_references_reject_duplicate_missing_and_invalid_mcid_links(
        self,
    ) -> None:
        options = StructureOptions(require_structure_tree=True, check_structure_references=True)
        passed = (await self.check(options))[0]
        self.assertEqual(passed.code, "PDF405")
        self.assertEqual(passed.status, "passed")
        self.assertEqual(passed.details["marked_content_count"], 3)
        for mutation in (
            "duplicate",
            "missing_mcid",
            "missing_parent",
            "wrong_parent",
            "invalid_page",
        ):
            document = structured_pdf_objects()
            if mutation == "duplicate":
                object_value(document, 9)["/K"] = [1, 1]
            elif mutation == "missing_mcid":
                object_value(document, 9)["/K"] = {"/Type": "/MCR", "/Pg": "3 0 R"}
            elif mutation == "missing_parent":
                object_value(document, 3).pop("/StructParents")
            elif mutation == "wrong_parent":
                object_value(document, 11)["/Nums"] = [0, ["9 0 R", "9 0 R", "12 0 R"], 1, "12 0 R"]
            else:
                object_value(document, 9)["/Pg"] = "90 0 R"
            with self.subTest(mutation=mutation):
                self.assertEqual((await self.check(options, document))[0].status, "failed")

    async def test_missing_or_malformed_structure_and_tools_are_not_passes(self) -> None:
        options = StructureOptions(require_structure_tree=True, require_figure_alt=True)
        document = structured_pdf_objects()
        object_value(document, 1).pop("/StructTreeRoot")
        result = {finding.code: finding for finding in await self.check(options, document)}
        self.assertEqual(result["PDF405"].status, "failed")
        self.assertEqual(result["PDF404"].status, "inconclusive")
        document = structured_pdf_objects()
        object_value(document, 5)["/K"] = ["5 0 R"]
        self.assertTrue(
            all(f.status == "inconclusive" for f in await self.check(options, document))
        )
        document = structured_pdf_objects()
        object_value(document, 11)["/Nums"] = [part for key in range(10001) for part in (key, [])]
        self.assertTrue(
            all(f.status == "inconclusive" for f in await self.check(options, document))
        )
        options = replace(options, heading_expectations=(HeadingExpectation(1, "Methods", "2"),))
        self.count += 1
        runner = DetailRunner(info="Pages: 1\n", text="2 Methods\f")
        runner.missing = "qpdf"
        result = await inspect_pdf_structure(
            self.pdf, self.root / f"inspect-{self.count}", runner, options
        )
        findings = {finding.code: finding for finding in result}
        self.assertEqual(findings["PDF401"].status, "passed")
        self.assertEqual(findings["PDF405"].status, "inconclusive")
        self.assertEqual(findings["PDF404"].status, "inconclusive")

    async def test_structure_probes_keep_frozen_inputs_and_shared_resource_lease(self) -> None:
        budget = ResourceBudget(2, 256)
        runner = DetailRunner(info="Pages: 1\n", objects=json.dumps(structured_pdf_objects()))
        runner.expected_budget = budget
        options = StructureOptions(require_structure_tree=True, check_table_structure=True)
        results = await inspect_pdf_structure(
            self.pdf, self.root / "leased", runner, options, budget
        )
        self.assertTrue(all(finding.status == "passed" for finding in results))
        inputs = [
            paths for command, _, paths in runner.calls if command[-1] not in {"-v", "--version"}
        ]
        self.assertTrue(inputs)
        self.assertTrue(all(len(paths) == 1 for paths in inputs))
        self.assertEqual(len({paths[0] for paths in inputs}), 1)
        self.assertNotEqual(inputs[0][0], self.pdf)


if __name__ == "__main__":
    unittest.main()
