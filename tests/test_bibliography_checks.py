"""Behavior fixtures for scoped offline citation and bibliography metadata checks."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from latexprep.bibliography_checks import BibliographyOptions, check_bibliography_details
from latexprep.models import Finding, PreparationError, has_blockers


def listed(finding: Finding, key: str) -> list:
    """Read one list-valued detail of a finding without narrowing every element."""
    value = finding.details.get(key, [])
    assert isinstance(value, list), value
    return value


class BibliographyCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.options = BibliographyOptions()

    def write(self, name: str, source: str | bytes) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(source.encode("utf-8") if isinstance(source, str) else source)
        return path

    def manuscript(self, body: str = "", *, resources: str = "references") -> None:
        self.write(
            "main.tex",
            "\\documentclass{article}\n\\begin{document}\n"
            + body
            + "\n\\bibliography{"
            + resources
            + "}\n\\end{document}\n",
        )

    def check(self, **options: object) -> list[Finding]:
        return check_bibliography_details(self.root, "main.tex", replace(self.options, **options))

    def rows(self, suffix: str, **options: object) -> list[Finding]:
        return [item for item in self.check(**options) if item.rule == f"bibliography.{suffix}"]

    def test_citation_coverage_uses_selected_resources_and_inline_entries(self) -> None:
        self.manuscript(
            "\\input{chapter}\n\\citep[see][p.~2]{used,missing}\n"
            "\\begin{thebibliography}{9}\\bibitem{inline}Text\\end{thebibliography}\n"
            "\\cite{inline}\n% \\cite{comment}\n\\verb|\\cite{example}|"
        )
        self.write("chapter.tex", "A citation \\parencite{used}.")
        self.write("references.bib", "@article{used,title={A work}}")
        self.write("unselected.bib", "@misc{missing,title={Must not satisfy the citation}}")
        rows = self.rows("citation_coverage")
        self.assertEqual(
            [(row.details.get("key"), row.status) for row in rows], [("missing", "failed")]
        )
        self.assertEqual(rows[0].severity, "error")
        self.assertEqual(rows[0].code, "BIB101")
        self.assertEqual(rows[0].path, "main.tex")
        self.write("references.bib", "@article{used,title={A work}}\n@misc{missing}")
        self.assertEqual([row.status for row in self.rows("citation_coverage")], ["passed"])

    def test_dynamic_and_incomplete_coverage_is_inconclusive(self) -> None:
        cases = (
            ("\\ifdefined\\flag\\cite{missing}\\fi", "@misc{known}"),
            ("\\newcommand{\\mycite}[1]{\\cite{#1}}\\mycite{missing}", "@misc{known}"),
            ("\\cite{\\selectedkey}", "@misc{known}"),
            ("\\input{absent}\\cite{missing}", "@misc{known}"),
            ("\\cite{missing}", "@misc{known}\n@article{broken,title={"),
            ("\\autocites{known}{missing}{known}{known}{known}{known}{extra}", "@misc{known}"),
        )
        for source, bib in cases:
            with self.subTest(source=source):
                self.manuscript(source)
                self.write("references.bib", bib)
                rows = self.rows("citation_coverage")
                self.assertTrue(rows)
                self.assertTrue(all(row.status == "inconclusive" for row in rows))
                self.assertFalse(any(row.severity == "error" for row in rows))

    def test_all_roots_keeps_citation_resolution_per_document(self) -> None:
        self.manuscript("\\cite{only_other}")
        self.write("references.bib", "@misc{only_main}")
        self.write("other.tex", "\\documentclass{article}\\cite{only_other}\\bibliography{other}")
        self.write("other.bib", "@misc{only_other}")
        rows = self.rows("citation_coverage", all_roots=True)
        failed = [row for row in rows if row.status == "failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0].details["document"], "main.tex")
        self.assertEqual(failed[0].details["key"], "only_other")
        self.assertTrue(
            any(row.status == "passed" and row.details["document"] == "other.tex" for row in rows)
        )

    def test_uncited_entries_respect_nocite_aliases_and_relationships(self) -> None:
        original = (
            "@inproceedings{child,crossref={parent},related={related},xdata={data}}\n"
            "@proceedings{parent,title={Proceedings}}\n@misc{related}\n@xdata{data}\n"
            "@set{set,entryset={member}}\n@misc{member}\n"
            "@misc{primary,ids={alias}}\n@misc{unused}\n"
        )
        self.manuscript("\\cite{child,alias}\\nocite{set}")
        path = self.write("references.bib", original)
        rows = self.rows("uncited_entries")
        self.assertEqual([row.status for row in rows], ["failed"])
        self.assertEqual([item["entry"] for item in listed(rows[0], "entries")], ["unused"])
        self.assertEqual(rows[0].message, "1 of 8 entries have no scanned use: unused")
        self.assertEqual((rows[0].path, rows[0].line), ("references.bib", 8))
        self.assertEqual(rows[0].code, "BIB102")
        self.assertEqual(rows[0].severity, "warning")
        self.assertEqual(path.read_text(), original)
        self.manuscript("\\nocite{*}")
        self.assertEqual([row.status for row in self.rows("uncited_entries")], ["passed"])
        self.write("references.bib", "@set{collection,entryset={one,two}}\n@misc{one}\n@misc{two}")
        self.manuscript("\\cite{one}")
        self.assertEqual([row.status for row in self.rows("uncited_entries")], ["passed"])

    def test_uncited_candidates_are_inconclusive_for_filters_and_dynamic_relationships(
        self,
    ) -> None:
        cases = (
            ("\\cite{used}\\printbibliography[type=article]", "@misc{used}\n@misc{unused}"),
            ("\\cite{used}", "@misc{used,xdata=parents}\n@misc{unused}"),
            ("\\cite{used}", "@misc{used,crossref={missing}}\n@misc{unused}"),
            ("\\begin{refsection}\\cite{used}\\end{refsection}", "@misc{used}\n@misc{unused}"),
        )
        for source, bib in cases:
            with self.subTest(source=source, bib=bib):
                self.manuscript(source)
                self.write("references.bib", bib)
                rows = self.rows("uncited_entries")
                self.assertTrue(
                    any(
                        item["entry"] == "unused" for row in rows for item in listed(row, "entries")
                    )
                )
                self.assertTrue(all(row.status == "inconclusive" for row in rows))

    def test_ordinary_macro_and_conditional_definitions_keep_coverage_conclusive(self) -> None:
        self.write("references.bib", "@article{used,title={A work}}")
        self.manuscript(
            "\\newcommand{\\R}{\\mathbb{R}}\n\\def\\shorthand{text}\n\\let\\old\\relax\n"
            "\\renewcommand{\\arraystretch}{1.2}\n\\ifdefined\\flag\\relax\\fi\n\\cite{used}"
        )
        self.assertEqual([row.status for row in self.rows("citation_coverage")], ["passed"])
        self.assertEqual([row.status for row in self.rows("uncited_entries")], ["passed"])
        for body, reason in (
            ("\\ifdefined\\flag\\cite{used}\\fi", "conditional citation or bibliography"),
            ("\\newcommand{\\mycite}[1]{\\cite{#1}}\\mycite{used}", "macro definition of citation"),
            ("\\csname cite\\endcsname{used}", "category-code execution"),
        ):
            with self.subTest(body=body):
                self.manuscript(body)
                rows = self.rows("citation_coverage")
                self.assertTrue(rows)
                self.assertTrue(all(row.status == "inconclusive" for row in rows))
                self.assertTrue(
                    any(reason in str(item) for row in rows for item in listed(row, "uncertainty"))
                )

    def test_resolved_source_dependencies_do_not_make_every_check_inconclusive(self) -> None:
        # TeX's lookup order selects fig.pdf; the reported alternative is evidence,
        # not missing evidence, so it must not spread uncertainty to every rule.
        self.manuscript("\\includegraphics{fig}\n\\cite{used}")
        self.write("fig.pdf", b"%PDF-1.4")
        self.write("fig.png", b"\x89PNG\r\n\x1a\n")
        self.write("references.bib", "@article{used,pages={1--2},url={https://example.org/}}")
        rows = self.check()
        self.assertEqual(
            {row.rule: row.status for row in rows},
            {
                "bibliography.citation_coverage": "passed",
                "bibliography.uncited_entries": "passed",
                "bibliography.page_ranges": "passed",
                "bibliography.url_syntax": "passed",
            },
        )

    def test_many_uncited_entries_collapse_into_one_listed_finding(self) -> None:
        self.manuscript("\\cite{used}")
        self.write(
            "references.bib",
            "@misc{used}\n" + "".join(f"@misc{{spare{index}}}\n" for index in range(1, 6)),
        )
        rows = self.rows("uncited_entries")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].status, "failed")
        self.assertEqual(
            rows[0].message, "5 of 6 entries have no scanned use: spare1, spare2, spare3 (+2 more)"
        )
        self.assertEqual(
            [(item["entry"], item["path"], item["line"]) for item in listed(rows[0], "entries")],
            [(f"spare{index}", "references.bib", index + 1) for index in range(1, 6)],
        )

    def test_checks_without_supported_items_are_not_applicable_rather_than_passed(self) -> None:
        self.manuscript("\\cite{used}")
        self.write("references.bib", "@misc{used}")
        reported = {
            "bibliography.required_fields",
            "bibliography.missing_doi",
            "bibliography.page_ranges",
            "bibliography.url_syntax",
        }
        rows = [
            row
            for row in self.check(
                doi_entry_types=("article",), required_fields=(("article", ("author",)),)
            )
            if row.rule in reported
        ]
        self.assertEqual({row.rule for row in rows}, reported)
        self.assertTrue(all(row.status == "not_applicable" for row in rows))
        self.assertTrue(all(row.severity == "info" for row in rows))
        self.assertTrue(all(row.message == "No supported items to check." for row in rows))
        self.assertTrue(all(row.details["checked"] == 0 for row in rows))
        self.assertFalse(has_blockers(rows))

    def test_all_roots_unions_usage_without_importing_other_resources(self) -> None:
        self.manuscript("\\cite{first}")
        self.write("references.bib", "@misc{first}\n@misc{second}")
        self.write("other.tex", "\\documentclass{article}\\cite{second}\\bibliography{references}")
        self.write("unused-resource.bib", "@misc{unselected}")
        rows = self.rows("uncited_entries")
        self.assertEqual([item["entry"] for item in listed(rows[0], "entries")], ["second"])
        self.assertEqual(
            [row.status for row in self.rows("uncited_entries", all_roots=True)], ["passed"]
        )

    def test_required_fields_are_entry_type_specific_and_accept_alternatives(self) -> None:
        self.manuscript()
        self.write(
            "references.bib",
            "@article{missing,title={Known},year={2026}}\n"
            "@book{edited,title={Book},editor={A. Editor},date={2026-01}}\n"
            "@software{tool,title={{Tool}},version={1.0}}",
        )
        requirements = (
            ("*", ("title",)),
            ("article", ("author", "year|date")),
            ("book", ("author|editor", "year|date")),
        )
        rows = self.rows("required_fields", required_fields=requirements)
        self.assertEqual(
            [(row.details["entry"], row.details["requirement"]) for row in rows],
            [("missing", "author")],
        )
        self.assertEqual(rows[0].code, "BIB103")
        self.assertEqual(rows[0].severity, "error")
        self.assertTrue(has_blockers(rows))
        self.write(
            "references.bib",
            "@article{complete,title={A {GPU} title},author={A. Author},year=2026}",
        )
        self.assertEqual(
            [row.status for row in self.rows("required_fields", required_fields=requirements)],
            ["passed"],
        )
        self.assertFalse(self.rows("required_fields"))

    def test_required_fields_do_not_guess_macro_values_or_inheritance(self) -> None:
        self.manuscript()
        self.write(
            "references.bib",
            "@string{person={A. Author}}\n"
            "@article{macro,author=person}\n@article{child,crossref={parent}}\n"
            "@proceedings{parent,author={A. Author}}",
        )
        rows = self.rows("required_fields", required_fields=(("article", ("author",)),))
        self.assertEqual({row.details["entry"] for row in rows}, {"macro", "child"})
        self.assertTrue(all(row.status == "inconclusive" for row in rows))
        self.assertTrue(all(row.severity == "error" for row in rows))
        self.assertTrue(has_blockers(rows))
        self.write("references.bib", "@article{empty,author={{ }}}")
        self.assertEqual(
            [
                row.status
                for row in self.rows("required_fields", required_fields=(("article", ("author",)),))
            ],
            ["failed"],
        )

    def test_missing_doi_is_opt_in_advisory_and_type_specific(self) -> None:
        self.manuscript()
        self.write(
            "references.bib", "@article{missing,title={Article}}\n@software{tool,title={Tool}}"
        )
        self.assertFalse(self.rows("missing_doi"))
        rows = self.rows("missing_doi", doi_entry_types=("article",))
        self.assertEqual([row.details["entry"] for row in rows], ["missing"])
        self.assertEqual(rows[0].code, "BIB104")
        self.assertEqual(rows[0].severity, "warning")
        self.assertFalse(has_blockers(rows))
        self.write("references.bib", "@article{present,doi={10.1234/Work}}")
        self.assertEqual(
            [row.status for row in self.rows("missing_doi", doi_entry_types=("article",))],
            ["passed"],
        )

    def test_macro_and_inherited_doi_presence_is_inconclusive(self) -> None:
        self.manuscript()
        self.write(
            "references.bib",
            "@article{macro,doi=identifier}\n"
            "@article{child,xdata={parent}}\n@xdata{parent,doi={10.1234/Work}}",
        )
        rows = self.rows("missing_doi", doi_entry_types=("article",))
        self.assertEqual({row.details["entry"] for row in rows}, {"macro", "child"})
        self.assertTrue(all(row.status == "inconclusive" for row in rows))
        self.assertFalse(has_blockers(rows))

    def test_required_doi_uncertainty_blocks_while_doi_advisories_do_not(self) -> None:
        self.manuscript()
        self.write(
            "references.bib",
            "@article{macro,doi=identifier}\n"
            "@article{child,xdata={parent}}\n@xdata{parent,doi={10.1234/Work}}",
        )
        requirements = (("article", ("doi",)),)
        required = self.rows("required_fields", required_fields=requirements)
        advisory = self.rows("missing_doi", doi_entry_types=("article",))
        self.assertEqual({row.details["entry"] for row in required}, {"macro", "child"})
        self.assertTrue(all(row.status == "inconclusive" for row in required))
        self.assertTrue(all(row.severity == "error" for row in required))
        self.assertTrue(has_blockers(required))
        self.assertFalse(has_blockers(advisory))
        self.write("references.bib", "@article{missing}")
        required = self.rows("required_fields", required_fields=requirements)
        self.assertEqual([row.status for row in required], ["failed"])
        self.assertTrue(has_blockers(required))
        self.write("references.bib", "@article{present,doi={10.1234/Work}}")
        required = self.rows("required_fields", required_fields=requirements)
        self.assertEqual([row.status for row in required], ["passed"])
        self.assertFalse(has_blockers(required))

    def test_required_field_coverage_uncertainty_blocks_without_claiming_missing_metadata(
        self,
    ) -> None:
        for field in ("author", "doi"):
            requirements = (("article", (field,)),)
            for body, resource, bibliography in (
                ("", "missing", "@article{known}"),
                ("", "references", b"@article{known,title={Unreadable\xff}}"),
                ("", "references", "@article{broken,title={"),
                ("", "\\selectedresource", "@article{known}"),
                (
                    "\\ifdefined\\flag\\cite{known}\\fi",
                    "references",
                    "@article{known,author={A. Author},doi={10.1234/Work}}",
                ),
            ):
                with self.subTest(field=field, resource=resource, bibliography=bibliography):
                    self.manuscript(body, resources=resource)
                    self.write("references.bib", bibliography)
                    rows = self.rows("required_fields", required_fields=requirements)
                    self.assertTrue(rows)
                    self.assertTrue(all(row.status == "inconclusive" for row in rows))
                    self.assertTrue(all(row.severity == "error" for row in rows))
                    self.assertTrue(has_blockers(rows))

    def test_page_ranges_accept_single_prefixed_and_multiple_ranges(self) -> None:
        self.manuscript()
        self.write(
            "references.bib",
            "@misc{single,pages=12}\n@misc{prefixed,pages={S2--S10}}\n"
            "@misc{multiple,pages={1, 3--5, 9–12}}",
        )
        self.assertEqual([row.status for row in self.rows("page_ranges")], ["passed"])
        self.write("references.bib", "@misc{reversed,pages={20--10}}\n@misc{bad,pages={10---12}}")
        rows = self.rows("page_ranges")
        self.assertEqual({row.details["entry"] for row in rows}, {"reversed", "bad"})
        self.assertEqual({row.code for row in rows}, {"BIB105"})
        self.assertTrue(all(row.status == "failed" for row in rows))

    def test_page_range_unknown_notation_and_macros_are_inconclusive(self) -> None:
        self.manuscript()
        self.write(
            "references.bib",
            "@misc{roman,pages={iv--x}}\n@misc{macro,pages=pageRange}\n"
            "@misc{prefixes,pages={A1--B2}}",
        )
        rows = self.rows("page_ranges")
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row.status == "inconclusive" for row in rows))

    def test_url_syntax_distinguishes_valid_and_malformed_literals(self) -> None:
        self.manuscript()
        self.write(
            "references.bib",
            "@misc{url,url={https://example.org/a%20b?q=1#part}}\n"
            "@misc{ip,url={http://[2001:db8::1]:8080/a}}",
        )
        self.assertEqual([row.status for row in self.rows("url_syntax")], ["passed"])
        self.write(
            "references.bib",
            "@misc{host,url={https:///missing}}\n"
            "@misc{port,url={https://example.org:wrong/a}}\n"
            "@misc{escape,url={https://example.org/a%zz}}\n"
            "@misc{space,url={https://example.org/a b}}",
        )
        rows = self.rows("url_syntax")
        self.assertEqual(
            {row.details["entry"] for row in rows}, {"host", "port", "escape", "space"}
        )
        self.assertEqual({row.code for row in rows}, {"BIB106"})
        self.assertTrue(all(row.status == "failed" for row in rows))

    def test_url_macros_markup_and_other_schemes_are_inconclusive(self) -> None:
        self.manuscript()
        self.write(
            "references.bib",
            "@misc{macro,url=webaddress}\n"
            "@misc{markup,url={\\url{https://example.org}}}\n"
            "@misc{urn,url={urn:isbn:12345}}",
        )
        rows = self.rows("url_syntax")
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row.status == "inconclusive" for row in rows))

    def test_capitalization_is_opt_in_and_respects_nested_braces(self) -> None:
        self.manuscript()
        self.write(
            "references.bib",
            "@misc{risk,title={A GPU and iPhone study}}\n"
            "@misc{protected,title={A {GPU} and {{iPhone}} study}}\n"
            "@misc{ordinary,title={An ordinary study}}",
        )
        self.assertFalse(self.rows("capitalization"))
        rows = self.rows("capitalization", check_capitalization=True)
        self.assertEqual([row.details["entry"] for row in rows], ["risk"])
        self.assertEqual(rows[0].details["tokens"], ["GPU", "iPhone"])
        self.assertEqual(rows[0].code, "BIB107")
        self.assertEqual(rows[0].evidence, "heuristic")
        self.write("references.bib", "@misc{protected,title={A {GPU} study}}")
        self.assertEqual(
            [row.status for row in self.rows("capitalization", check_capitalization=True)],
            ["passed"],
        )

    def test_capitalization_macros_are_inconclusive(self) -> None:
        self.manuscript()
        self.write(
            "references.bib", "@misc{macro,title=titleText}\n@misc{command,title={A \\GPU{} study}}"
        )
        rows = self.rows("capitalization", check_capitalization=True)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row.status == "inconclusive" for row in rows))

    def test_fuzzy_duplicates_are_advisory_distinct_from_exact_dois(self) -> None:
        self.manuscript()
        original = (
            "@article{first,title={A bounded bibliography checker},"
            "author={A. Author},year=2026,doi={10.1234/A}}\n"
            "@article{candidate,title={A bounded bibliographic checker},"
            "author={A. Author},year=2026,doi={10.1234/B}}\n"
            "@article{other_author,title={A bounded bibliography checker},"
            "author={B. Author},year=2026}\n"
            "@article{other_year,title={A bounded bibliography checker},"
            "author={A. Author},year=2025}\n"
        )
        path = self.write("references.bib", original)
        self.assertFalse(self.rows("fuzzy_duplicates"))
        rows = self.rows(
            "fuzzy_duplicates", check_fuzzy_duplicates=True, fuzzy_duplicate_threshold=0.85
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].details["entry"], "candidate")
        self.assertEqual(rows[0].details["first_entry"], "first")
        self.assertEqual(rows[0].code, "BIB108")
        self.assertEqual((rows[0].severity, rows[0].evidence), ("warning", "heuristic"))
        self.assertEqual(path.read_text(), original)
        self.assertEqual(
            [
                row.status
                for row in self.rows(
                    "fuzzy_duplicates", check_fuzzy_duplicates=True, fuzzy_duplicate_threshold=1.0
                )
            ],
            ["passed"],
        )
        self.write("references.bib", original.replace("10.1234/B", "10.1234/A"))
        self.assertEqual(
            [
                row.status
                for row in self.rows(
                    "fuzzy_duplicates", check_fuzzy_duplicates=True, fuzzy_duplicate_threshold=0.85
                )
            ],
            ["passed"],
        )

    def test_fuzzy_duplicates_report_missing_metadata_and_budget_uncertainty(self) -> None:
        self.manuscript()
        self.write("references.bib", "@misc{unknown,title=titleText,author={A. Author},year=2026}")
        rows = self.rows("fuzzy_duplicates", check_fuzzy_duplicates=True)
        self.assertEqual([row.status for row in rows], ["inconclusive"])
        self.write(
            "references.bib",
            "@misc{a,title={First},author={A. Author},year=2026}\n"
            "@misc{b,title={Second},author={A. Author},year=2026}",
        )
        with patch("latexprep.bibliography_checks._MAX_FUZZY_PAIRS", 0):
            rows = self.rows("fuzzy_duplicates", check_fuzzy_duplicates=True)
        self.assertEqual([row.status for row in rows], ["inconclusive"])
        self.assertIn("comparison count", rows[0].message)

    def test_unselected_malformed_resources_and_content_after_end_are_ignored(self) -> None:
        self.manuscript("\\cite{used}")
        self.write("references.bib", "@misc{used,url={https://example.org}}")
        self.write("unused.bib", "@misc{broken,title={")
        with (self.root / "main.tex").open("a") as stream:
            stream.write("\\cite{ghost}\\bibliography{unused}")
        self.assertEqual([row.status for row in self.rows("citation_coverage")], ["passed"])
        self.assertEqual([row.status for row in self.rows("url_syntax")], ["passed"])

    def test_bibliography_only_scope_and_input_limits_report_uncertainty(self) -> None:
        self.write("references.bib", "@misc{known,url={https:///missing}}")
        rows = check_bibliography_details(self.root)
        self.assertTrue(
            any(row.rule == "bibliography.url_syntax" and row.status == "failed" for row in rows)
        )
        self.assertTrue(
            all(
                row.status == "inconclusive"
                for row in rows
                if row.rule in {"bibliography.citation_coverage", "bibliography.uncited_entries"}
            )
        )
        self.manuscript("\\cite{known}")
        with patch("latexprep.bibliography_checks._MAX_TOTAL_BYTES", 4):
            rows = self.check(required_fields=(("*", ("title",)),))
        self.assertTrue(rows)
        self.assertTrue(all(row.status == "inconclusive" for row in rows))
        self.assertTrue(any("byte budget" in row.message for row in rows))

    def test_all_root_discovery_and_conditional_metadata_do_not_claim_completeness(self) -> None:
        self.manuscript("\\nocite{*}")
        self.write("references.bib", "@misc{known}")
        self.write("unreadable.tex", b"\\documentclass{article}\xff")
        rows = self.check(all_roots=True)
        self.assertTrue(all(row.status == "inconclusive" for row in rows))
        self.assertTrue(any("All-root discovery" in row.message for row in rows))
        self.manuscript("\\ifdefined\\flag\\nocite{known}\\fi")
        rows = self.rows("required_fields", required_fields=(("*", ("title",)),))
        self.assertTrue(all(row.status == "inconclusive" for row in rows))
        self.assertTrue(all(row.severity == "error" for row in rows))
        self.assertTrue(has_blockers(rows))

    def test_extreme_page_numbers_and_single_letter_titles_remain_bounded(self) -> None:
        self.manuscript()
        digits = "9" * 5_000
        self.write("references.bib", "@misc{large,title={t},pages={1--" + digits + "}}")
        rows = self.check(required_fields=(("*", ("title",)),))
        self.assertEqual(
            [
                row.status
                for row in rows
                if row.rule in {"bibliography.required_fields", "bibliography.page_ranges"}
            ],
            ["passed", "passed"],
        )

    def test_option_validation_and_cooperative_cancellation(self) -> None:
        invalid = (
            {"check_urls": 1},
            {"fuzzy_duplicate_threshold": True},
            {"fuzzy_duplicate_threshold": 0},
            {"fuzzy_duplicate_threshold": float("nan")},
            {"doi_entry_types": ("Article",)},
            {"required_fields": (("article", ("author||editor",)),)},
            {"required_fields": (("article", ("title",)), ("article", ("year",)))},
        )
        for options in invalid:
            with self.subTest(options=options), self.assertRaises(PreparationError):
                replace(self.options, **options)
        self.manuscript()
        self.write("references.bib", "@misc{known}")
        with patch(
            "latexprep.bibliography_checks.cancellation_point",
            side_effect=PreparationError("Analysis cancelled"),
        ):
            with self.assertRaisesRegex(PreparationError, "Analysis cancelled"):
                self.check()


if __name__ == "__main__":
    unittest.main()
