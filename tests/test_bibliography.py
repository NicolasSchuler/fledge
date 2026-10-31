"""Regression cases for bounded parsing and narrowly scoped bibliography edits."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from latexprep.bibliography_checks import check_bibliography_coverage_and_fields
from latexprep.bibliography_integrity import check_bibliography_integrity
from latexprep.bibliography_transform import (
    BibliographyTransformOptions,
    normalize_dois,
    plan_bibliography,
)
from latexprep.bibtex_syntax import BibFileReader
from latexprep.errors import PreparationError
from latexprep.findings import Finding


class BibliographyTests(unittest.TestCase):
    """Regression tests of bounded bibliography parsing, DOI normalization and integrity checks."""

    def setUp(self) -> None:
        """Create a temporary directory as ``self.root``."""
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def write(self, source: str | bytes, name: str = "references.bib") -> Path:
        """Write ``source`` (UTF-8 for text) below ``root``, creating parent directories.

        Args:
            source: The text or bytes of the file.
            name: The path of the file relative to ``root``.

        Returns:
            The path of the written file.
        """
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(source.encode("utf-8") if isinstance(source, str) else source)
        return path

    def test_prefix_edits_preserve_braces_comments_case_and_unknown_fields(self) -> None:
        original = (
            "\ufeff% private comment and capitalization must survive\r\n"
            '@comment{A comment with "unpaired quotes and nested {braces}.}\r\n'
            '@string{journal = "A {Journal}"}\r\n'
            '@preamble{"\\newcommand{\\name}{Example}"}\r\n'
            "@Article(KeepThisKEY,\r\n"
            "  title = {A {GPU} Study: \\{sets\\} and 100\\%},\r\n"
            '  author = {M{\\"u}ller, A.},\r\n'
            "  customField = {keep this field}, % preserve this too\r\n"
            "  journal = journal,\r\n"
            "  DOI = {{ https://dx.doi.org/10.1234/Mixed.Case }},\r\n"
            ")\r\n"
            '@misc{second, doi = "doi:10.5678/Other+Value"}\r\n'
        )
        source = self.write(original)
        expected = original.replace("https://dx.doi.org/", "").replace("doi:10.", "10.")

        proposed, changes, findings = normalize_dois(self.root)

        self.assertEqual(source.read_bytes(), original.encode("utf-8"))
        self.assertEqual(proposed, {"references.bib": expected.encode("utf-8")})
        self.assertEqual(len(changes), 1)
        self.assertIn("https://dx.doi.org/", changes[0].diff or "")
        self.assertEqual(findings, [])

    def test_normalization_is_idempotent_after_applying_proposed_bytes(self) -> None:
        path = self.write("@misc{k, doi={HTTP://DOI.ORG/10.1234/Case}}\n")
        proposed, _, _ = normalize_dois(self.root)
        path.write_bytes(proposed["references.bib"])

        self.assertEqual(normalize_dois(self.root), ({}, [], []))

    def test_macro_and_concatenated_dois_remain_unchanged(self) -> None:
        original = (
            '@string{prefix = "https://doi.org/"}\n'
            '@string{whole = "10.1234/Macro"}\n'
            "@misc{first, doi=prefix # {10.1234/Concatenated}}\n"
            "@misc{second, doi=whole}\n"
            "@misc{third, doi={https://doi.org/} # {10.1234/LiteralParts}}\n"
        )
        path = self.write(original)

        proposed, changes, findings = normalize_dois(self.root)

        self.assertEqual(proposed, {})
        self.assertEqual(changes, [])
        self.assertEqual(path.read_text(), original)
        self.assertEqual([item.rule for item in findings], ["bibliography.doi_unresolved"] * 3)
        self.assertTrue(all(item.status == "inconclusive" for item in findings))
        self.assertEqual({item.code for item in findings}, {"BIB006"})

    def test_duplicate_keys_and_dois_include_conflicts_without_merging(self) -> None:
        first = self.write("@article{same, title={First}, doi={10.1234/Same}}", "a.bib")
        second = self.write(
            "@article{same, title={Second}, DOI={https://doi.org/10.1234/Same}}",
            "nested/b.BIB",
        )

        findings = check_bibliography_integrity(self.root)

        self.assertEqual(
            {item.rule for item in findings},
            {"bibliography.duplicate_key", "bibliography.duplicate_doi"},
        )
        for finding in findings:
            self.assertEqual(finding.path, "nested/b.BIB")
            self.assertEqual(finding.details["first_path"], "a.bib")
            self.assertEqual(finding.details["conflicting_fields"], ["title"])
        self.assertEqual({item.code for item in findings}, {"BIB002", "BIB007"})
        self.assertTrue(first.exists() and second.exists())
        self.assertIn("https://doi.org/", second.read_text())

    def test_selected_main_limits_inspection_to_its_declared_resources(self) -> None:
        self.write(
            "\\documentclass{article}\n\\begin{document}\n\\cite{same}\n"
            "\\bibliography{references}\n\\end{document}\n",
            "main.tex",
        )
        self.write(
            "@article{same, title={First}, doi={https://doi.org/10.1234/Same}}\n", "references.bib"
        )
        backup = self.write(
            "@article{same, title={Second}, doi={https://doi.org/10.1234/Same}}\n",
            "old/references-backup.bib",
        )

        self.assertEqual(check_bibliography_integrity(self.root, "main.tex"), [])
        proposed, _, findings = normalize_dois(self.root, "main.tex")
        self.assertEqual(set(proposed), {"references.bib"})
        self.assertEqual(findings, [])

        self.assertEqual(
            {item.rule for item in check_bibliography_integrity(self.root)},
            {"bibliography.duplicate_key", "bibliography.duplicate_doi"},
        )
        self.assertIn("https://doi.org/", backup.read_text())

    def test_unresolved_resource_selection_keeps_the_whole_tree_scope(self) -> None:
        self.write("@misc{one, doi={https://doi.org/10.1234/One}}\n", "references.bib")
        self.write("@misc{two, doi={https://doi.org/10.1234/Two}}\n", "spare/extra.bib")
        for body in ("\\bibliography{\\dynamicname}", ""):
            with self.subTest(body=body):
                self.write(
                    "\\documentclass{article}\n\\begin{document}\n" + body + "\n\\end{document}\n",
                    "main.tex",
                )
                proposed, _, _ = normalize_dois(self.root, "main.tex")
                self.assertEqual(set(proposed), {"references.bib", "spare/extra.bib"})

    def test_repeated_doi_fields_are_not_chosen_or_normalized(self) -> None:
        self.write("@misc{k, doi={https://doi.org/10.1234/One}, DOI={doi:10.1234/Two}}")

        proposed, changes, findings = normalize_dois(self.root)

        self.assertEqual((proposed, changes), ({}, []))
        self.assertIn("bibliography.repeated_field", [item.rule for item in findings])
        self.assertIn("BIB003", {item.code for item in findings})

    def test_malformed_file_is_never_partially_normalized(self) -> None:
        cases = [
            "@misc{good, doi={https://doi.org/10.1234/Valid}}\n@article{broken, title={open",
            "@misc{k, title={One} year={2026}}",
            '@misc{k, title="unterminated}',
            "@misc{k, title={one} # }",
            "@misc{k, doi=10.1234/NeedsBraces}",
            '@string{a="one", b="two"}',
            "@comment{Unclosed",
        ]
        for source in cases:
            with self.subTest(source=source):
                path = self.write(source)
                proposed, changes, findings = normalize_dois(self.root)
                self.assertEqual((proposed, changes), ({}, []))
                self.assertTrue(any(item.rule == "bibliography.syntax" for item in findings))
                self.assertIn("BIB001", {item.code for item in findings})
                self.assertEqual(path.read_text(), source)

    def test_invalid_plain_dois_and_ambiguous_markup_are_distinct(self) -> None:
        self.write(
            "@misc{invalid, doi={https://doi.org/not-a-doi}}\n"
            "@misc{query, doi={https://doi.org/10.1234/Work?tracking=yes}}\n"
            "@misc{protected, doi={https://doi.org/10.1234/{ABC}}}\n"
            "@misc{escaped, doi={https://doi.org/10.1234/ABC\\_DEF}}\n"
            "@misc{encoded, doi={https://doi.org/10.1234/ABC%2FDEF}}\n"
        )

        proposed, _, findings = normalize_dois(self.root)

        self.assertEqual(proposed, {})
        self.assertEqual(sum(item.rule == "bibliography.invalid_doi" for item in findings), 1)
        self.assertEqual(sum(item.rule == "bibliography.doi_unresolved" for item in findings), 4)
        self.assertEqual({item.code for item in findings}, {"BIB006"})

    def test_literal_relationships_and_builtin_months_are_checked(self) -> None:
        self.write(
            '@string{venue = "Example"}\n'
            "@proceedings{parent, ids={alternate}, title=venue, month=jan}\n"
            "@inproceedings{child, crossref={alternate}}\n"
            "@misc{missing, xdata={parent, absent}, title=undefined}\n"
            "@misc{dynamic, crossref=venue}\n"
        )

        findings = check_bibliography_integrity(self.root)

        self.assertEqual(
            [item.rule for item in findings],
            [
                "bibliography.missing_relationship",
                "bibliography.undefined_macro",
                "bibliography.relationship_unresolved",
            ],
        )
        self.assertEqual(findings[0].line, 4)
        self.assertEqual(findings[-1].status, "inconclusive")
        self.assertEqual([item.code for item in findings], ["BIB005", "BIB004", "BIB005"])

    def test_a_string_macro_named_ids_defines_no_relationship_target(self) -> None:
        self.write('@string{ids = "target"}\n@misc{child, crossref={target}}\n')

        findings = check_bibliography_integrity(self.root)

        self.assertEqual([item.rule for item in findings], ["bibliography.missing_relationship"])

    def test_incomplete_parse_does_not_claim_macro_or_target_is_definitely_missing(self) -> None:
        self.write("@misc{k, title=maybe, crossref={later}}\n@misc{broken, title={")

        findings = check_bibliography_integrity(self.root)

        uncertain = [item for item in findings if item.rule != "bibliography.syntax"]
        self.assertEqual(len(uncertain), 2)
        self.assertTrue(all(item.status == "inconclusive" for item in uncertain))

    def test_comments_and_preamble_text_are_not_entries_or_doi_fields(self) -> None:
        source = (
            "% @misc{fake, doi={https://doi.org/10.1234/Fake}}\n"
            "@comment{doi={https://doi.org/10.1234/Comment}}\n"
            '@preamble{"doi={https://doi.org/10.1234/Preamble}"}\n'
            '@misc{k % a comment after the key\n, title="A {quoted \\"example\\"} title"}\n'
        )
        self.write(source)

        self.assertEqual(normalize_dois(self.root), ({}, [], []))

    def test_non_utf8_and_symbolic_link_inputs_are_not_modified(self) -> None:
        self.write(b"@misc{k,title={\xff}}", "legacy.bib")
        target = self.write("@misc{k,doi={https://doi.org/10.1234/Target}}", "target.txt")
        (self.root / "linked.bib").symlink_to(target)

        proposed, changes, findings = normalize_dois(self.root)

        self.assertEqual((proposed, changes), ({}, []))
        self.assertEqual(len(findings), 2)
        self.assertTrue(all(item.rule == "bibliography.input" for item in findings))
        self.assertIn("https://doi.org/", target.read_text())

    def test_nesting_limit_reports_failure_without_python_recursion(self) -> None:
        self.write("@misc{k,title={" + "{" * 300 + "x" + "}" * 301 + "}")

        findings = check_bibliography_integrity(self.root)

        self.assertEqual(len(findings), 1)
        self.assertIn("nesting", findings[0].message)

    def test_doi_case_is_not_collapsed_for_exact_duplicate_detection(self) -> None:
        self.write("@misc{a,doi={10.1234/UPPER}}\n@misc{b,doi={10.1234/upper}}")

        self.assertEqual(check_bibliography_integrity(self.root), [])

    def test_root_must_be_a_directory(self) -> None:
        path = self.write("@misc{k}")
        with self.assertRaises(PreparationError):
            check_bibliography_integrity(path)

    def test_every_bibliography_entry_point_words_a_missing_directory_alike(self) -> None:
        path = self.write("@misc{k}")
        calls = (
            lambda: check_bibliography_integrity(path),
            lambda: check_bibliography_coverage_and_fields(path),
            lambda: plan_bibliography(
                path, None, BibliographyTransformOptions(format_entries=True)
            ),
        )
        for call in calls:
            with self.assertRaises(PreparationError) as raised:
                call()
            message = str(raised.exception)
            self.assertTrue(message.startswith("Bibliography input must be a directory, but "))
            self.assertIn("Pass the project folder", message)

    def test_unreadable_encoding_names_the_byte_offset_without_codec_jargon(self) -> None:
        self.write(b"@misc{k,title={\xff}}", "legacy.bib")

        (finding,) = check_bibliography_integrity(self.root)

        self.assertEqual(
            finding.message,
            "Bibliography legacy.bib is not valid UTF-8 (first invalid byte at offset 15), "
            "so it was not inspected.",
        )
        self.assertNotIn("codec", finding.message)
        self.assertIn("UTF-8", finding.suggestion or "")

    def test_oversize_bibliography_states_the_limit_in_mebibytes(self) -> None:
        self.write("@misc{k}", "big.bib")
        with patch.object(BibFileReader, "read", return_value=None):
            (finding,) = check_bibliography_integrity(self.root)
        self.assertIn(
            "big.bib exceeds the bibliography byte budget (16 MiB per file)", finding.message
        )
        self.assertNotIn("16777216", finding.message)

    def macro_findings(self, body: str) -> list[Finding]:
        r"""Check a ``main.tex`` that cites ``k`` for undefined bibliography macros.

        Args:
            body: The text that follows ``\cite{k}`` in the document.

        Returns:
            The ``bibliography.undefined_macro`` findings of ``check_bibliography_integrity``.
        """
        self.write(
            "\\documentclass{article}\n\\begin{document}\\cite{k}\n" + body + "\n\\end{document}\n",
            "main.tex",
        )
        return [
            item
            for item in check_bibliography_integrity(self.root, "main.tex")
            if item.rule == "bibliography.undefined_macro"
        ]

    def test_macros_of_the_standard_styles_are_known_but_typos_still_fail(self) -> None:
        self.write("@article{k, journal = jacm, title = {T}}\n@article{j, journal = jaccm}")

        findings = self.macro_findings("\\bibliographystyle{plain}\\bibliography{references}")

        self.assertEqual([item.message.split("'")[1] for item in findings], ["jaccm"])
        self.assertEqual((findings[0].severity, findings[0].status), ("error", "failed"))

    def test_macros_of_a_style_that_is_not_available_are_inconclusive(self) -> None:
        self.write("@article{k, journal = csur}")

        findings = self.macro_findings(
            "\\bibliographystyle{ACM-Reference-Format}\\bibliography{references}"
        )

        (finding,) = findings
        self.assertEqual((finding.severity, finding.status), ("warning", "inconclusive"))
        self.assertIn("'ACM-Reference-Format' may define it", finding.message)
        self.assertIn("unless the style defines it", finding.suggestion or "")

    def test_macros_of_a_shipped_style_are_read_from_its_file(self) -> None:
        self.write("@article{k, journal = myjournal}\n@article{j, journal = hidden}")
        self.write(
            '% MACRO {hidden} {"commented out"}\nMACRO {MyJournal}\n  {"My Journal"}\n',
            "mystyle.bst",
        )

        findings = self.macro_findings("\\bibliographystyle{mystyle}\\bibliography{references}")

        # The whole style is read, so what it does not define is still a certain error.
        self.assertEqual([item.message.split("'")[1] for item in findings], ["hidden"])
        self.assertEqual((findings[0].severity, findings[0].status), ("error", "failed"))

    def test_macros_stay_errors_without_a_bibtex_style_and_uncertain_without_a_document(
        self,
    ) -> None:
        self.write("@article{k, journal = jacm}")

        biblatex = self.macro_findings("\\usepackage{biblatex}\\addbibresource{references.bib}")
        self.assertEqual([(i.severity, i.status) for i in biblatex], [("error", "failed")])

        bibliography_only = check_bibliography_integrity(self.root)
        self.assertEqual(
            [(i.severity, i.status) for i in bibliography_only], [("warning", "inconclusive")]
        )
        self.assertIn("style used with this file", bibliography_only[0].message)


if __name__ == "__main__":
    unittest.main()
