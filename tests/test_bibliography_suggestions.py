"""The bibliography suggestions: preprints, URLs that repeat the DOI, and venue variants.

Each suggestion is run through ``check_bibliography_integrity``, which offers them with
the integrity findings of the same files; the last class runs whole jobs to show that they
are reported only when selected, never change the outcome, and can be silenced by a
comment in the .bib file.
"""

from __future__ import annotations

import time
import unittest
from dataclasses import replace

from latexprep.bibliography_integrity import check_bibliography_integrity
from latexprep.bibliography_suggestions import MAX_VENUE_GROUPS
from latexprep.bibliography_venues import venue_name, venue_spelling
from latexprep.check_selection import CheckSelection
from latexprep.config import Settings
from latexprep.core import run_job
from latexprep.findings import Finding
from latexprep.job_context import JobRequest
from tests.support import AsyncProjectTestCase, ProjectTestCase, clean_case

# Long enough that a backtracking pattern takes seconds; a linear scan needs milliseconds.
_LONG_VALUE_CHARACTERS = 200_000
_LINEAR_TIME_LIMIT_SECONDS = 1.0
_MANUSCRIPT = (
    "\\documentclass{article}\n\\begin{document}\n\\nocite{*}\n"
    "\\bibliography{references}\n\\end{document}\n"
)
_SUGGESTIONS = CheckSelection(select=("ALL", "BIB111", "BIB112", "BIB113"))


class SuggestionTestCase(ProjectTestCase):
    """A project whose ``main.tex`` declares ``references.bib``, and the check that reads it."""

    def setUp(self) -> None:
        """Write the manuscript; each test writes the bibliography."""
        super().setUp()
        self.write("main.tex", _MANUSCRIPT)

    def suggestions(self, bibliography: str, rule: str) -> list[Finding]:
        """Write ``references.bib`` and return the suggestions of ``rule`` about it.

        Args:
            bibliography: The text of the bibliography.
            rule: The rule name after ``bibliography.``.

        Returns:
            The findings of the rule, each checked to be a suggestion.
        """
        self.write("references.bib", bibliography)
        findings = [
            item
            for item in check_bibliography_integrity(self.root, "main.tex")
            if item.rule == f"bibliography.{rule}"
        ]
        for finding in findings:
            self.assertEqual((finding.severity, finding.status), ("suggestion", "failed"))
            self.assertEqual(finding.path, "references.bib")
        return findings


class PreprintSuggestionTests(SuggestionTestCase):
    """``bibliography.preprint`` (BIB111) and the preprint forms of common exports."""

    def test_every_export_form_is_named_as_a_preprint(self) -> None:
        forms = {
            "Google Scholar": (
                "@article{key, title={Attention}, journal={arXiv preprint arXiv:1706.03762}, "
                "year={2017}}",
                "journal field names arXiv",
            ),
            "Semantic Scholar": (
                "@article{key, title={T}, journal={ArXiv}, volume={abs/2101.00001}}",
                "journal field names arXiv",
            ),
            "DBLP": (
                "@article{key, title={T}, journal={CoRR}, volume={abs/1706.03762}, "
                "eprinttype={arXiv}, eprint={1706.03762}}",
                "CoRR with an abs/ volume",
            ),
            "arXiv DOI": (
                "@misc{key, title={T}, doi={10.48550/ARXIV.1706.03762}}",
                "arXiv DOI",
            ),
            "arXiv export": (
                "@misc{key, title={T}, eprint={1706.03762}, archivePrefix={arXiv}, "
                "primaryClass={cs.CL}}",
                "archiveprefix field names arXiv",
            ),
            "biblatex": (
                "@online{key, title={T}, eprinttype={arxiv}, eprint={1706.03762}}",
                "eprinttype field names arXiv",
            ),
            "biblatex journaltitle": (
                "@article{key, title={T}, journaltitle={{arXiv} e-prints}}",
                "journaltitle field names arXiv",
            ),
            "howpublished": (
                "@misc{key, title={T}, howpublished={arXiv:1706.03762}}",
                "howpublished field names arXiv",
            ),
            "unpublished": ("@unpublished{key, title={T}, note={Manuscript}}", "@unpublished"),
            "biblatex preprint": ("@preprint{key, title={T}}", "@preprint"),
            "arXiv URL": (
                "@misc{key, title={T}, url={https://arxiv.org/abs/1706.03762}}",
                "arxiv.org",
            ),
            "note": ("@misc{key, title={T}, note={Preprint}}", "note calls it a preprint"),
        }
        for form, (entry, reason) in forms.items():
            with self.subTest(form=form):
                (finding,) = self.suggestions(entry + "\n", "preprint")
                self.assertEqual(finding.code, "BIB111")
                self.assertIn(reason, finding.message)
                self.assertEqual((finding.line, finding.details["entry"]), (1, "key"))
                self.assertIn("NET004", finding.next_step or "")

    @clean_case("BIB111")
    def test_published_works_with_an_archived_copy_are_not_preprints(self) -> None:
        entries = {
            "published article with its arXiv copy": (
                "@article{key, title={T}, journal={Physical Review D}, doi={10.1103/x}, "
                "eprint={2101.00001}, archivePrefix={arXiv}}"
            ),
            "conference paper with an arXiv URL": (
                "@inproceedings{key, title={T}, booktitle={ICLR}, "
                "url={https://arxiv.org/abs/2101.00001}}"
            ),
            "journal macro with an arXiv eprint type": (
                "@string{apj={Astrophysical Journal}}\n"
                "@article{key, title={T}, journal=apj, eprinttype={arxiv}}"
            ),
            "CoRR without an abs volume": "@article{key, title={T}, journal={CoRR}}",
            "ordinary article": "@article{key, title={T}, journal={Nature}, year={2020}}",
            "arXiv inside a journal name": "@article{key, title={T}, journal={On arXiv usage}}",
        }
        for case, entry in entries.items():
            with self.subTest(case=case):
                self.assertEqual(self.suggestions(entry + "\n", "preprint"), [])


class UrlRepeatsDoiTests(SuggestionTestCase):
    """``bibliography.url_repeats_doi`` (BIB112): a url field that is the entry's DOI."""

    def test_a_doi_org_url_of_the_entrys_own_doi_is_named(self) -> None:
        cases = {
            "DBLP, letter case differs": (
                "doi={10.48550/ARXIV.2303.08774}, url={https://doi.org/10.48550/arXiv.2303.08774}"
            ),
            "dx.doi.org over HTTP": "doi={10.1145/123.456}, url={http://dx.doi.org/10.1145/123.456}",
            "percent-encoded": (
                "doi={10.1002/(SICI)1097-4571}, url={https://doi.org/10.1002/%28SICI%291097-4571}"
            ),
            "DOI written as a URL": (
                "doi={https://doi.org/10.1234/x}, url={https://doi.org/10.1234/x}"
            ),
        }
        for case, fields in cases.items():
            with self.subTest(case=case):
                (finding,) = self.suggestions(
                    f"@article{{key, title={{T}}, {fields}}}\n", "url_repeats_doi"
                )
                self.assertEqual(finding.code, "BIB112")
                self.assertIn("repeats the DOI", finding.message)
                self.assertEqual(finding.evidence_kind, "derived")

    @clean_case("BIB112")
    def test_other_addresses_and_unreadable_fields_are_not_named(self) -> None:
        cases = {
            "publisher page": "doi={10.1234/x}, url={https://example.org/paper}",
            "another DOI": "doi={10.1234/x}, url={https://doi.org/10.1234/y}",
            "query string": "doi={10.1234/x}, url={https://doi.org/10.1234/x?download=1}",
            "credentials": "doi={10.1234/x}, url={https://user:secret@doi.org/10.1234/x}",
            "port": "doi={10.1234/x}, url={https://doi.org:8443/10.1234/x}",
            "no DOI": "url={https://doi.org/10.1234/x}",
            "repeated url": (
                "doi={10.1234/x}, url={https://doi.org/10.1234/x}, url={https://doi.org/10.1234/x}"
            ),
            "macro url": "doi={10.1234/x}, url=link",
            "invalid DOI": "doi={not a doi}, url={https://doi.org/not a doi}",
            "other scheme": "doi={10.1234/x}, url={ftp://doi.org/10.1234/x}",
        }
        for case, fields in cases.items():
            with self.subTest(case=case):
                entry = f"@string{{link={{x}}}}\n@article{{key, title={{T}}, {fields}}}\n"
                self.assertEqual(self.suggestions(entry, "url_repeats_doi"), [])


class VenueVariantTests(SuggestionTestCase):
    """``bibliography.venue_variants`` (BIB113): one venue spelled several ways in a file."""

    def test_one_venue_spelled_several_ways_is_one_group(self) -> None:
        bibliography = (
            "@inproceedings{a, title={A}, booktitle={Advances in Neural Information Processing "
            "Systems}, year={2019}}\n"
            "@inproceedings{b, title={B}, booktitle={Advances in Neural Information Processing "
            "Systems}, year={2020}}\n"
            "@article{c, title={C}, journal={Advances in neural information processing systems}}\n"
            "@inproceedings{d, title={D}, booktitle={Proc. of the {IEEE} Conf. on Computer Vision "
            "\\& Pattern Recognition}}\n"
            "@inproceedings{e, title={E}, booktitle={Proceedings of the IEEE Conference on "
            "Computer Vision and Pattern Recognition, 2018}}\n"
        )
        neurips, cvpr = self.suggestions(bibliography, "venue_variants")
        self.assertEqual(neurips.code, "BIB113")
        self.assertEqual((neurips.line, neurips.details["entry"]), (3, "c"))
        self.assertIn("spelled 2 ways in references.bib", neurips.message)
        self.assertIn(
            "'Advances in Neural Information Processing Systems' (2 entries)", neurips.message
        )
        self.assertIn(
            "'Advances in neural information processing systems' (1 entry)", neurips.message
        )
        rows = neurips.details["variants"]
        assert isinstance(rows, list)
        self.assertEqual([row["entries"] for row in rows], [["a", "b"], ["c"]])
        self.assertEqual([row["fields"] for row in rows], [["booktitle"], ["journal"]])
        self.assertEqual(neurips.evidence_kind, "heuristic")
        self.assertIn("@string", neurips.next_step or "")
        self.assertEqual(cvpr.details["entry"], "e")
        self.assertIn("'Proc. of the IEEE Conf. on Computer Vision & Pattern", cvpr.message)

    @clean_case("BIB113")
    def test_years_editions_and_distinct_venues_are_not_variants(self) -> None:
        bibliography = (
            "@inproceedings{a, booktitle={Proceedings of the 34th International Conference on "
            "Machine Learning}}\n"
            "@inproceedings{b, booktitle={Proceedings of the 35th International Conference on "
            "Machine Learning}}\n"
            "@inproceedings{c, booktitle={CVPR 2017}}\n"
            "@inproceedings{d, booktitle={CVPR, 2018}}\n"
            "@inproceedings{e, booktitle={CVPR'19}}\n"
            "@article{f, journal={{IEEE} Transactions on Software Engineering}}\n"
            "@article{g, journal={IEEE Transactions on Software Engineering}}\n"
            "@inproceedings{h, booktitle={Proceedings}}\n"
            "@inproceedings{i, booktitle={Proc.}}\n"
            "@article{j, journal={Journal of Physics A}}\n"
            "@article{k, journal={Journal of Physics B}}\n"
            "@string{nature={Nature}}\n"
            "@article{l, journal=nature}\n"
            "@article{m, journal={nature}}\n"
            '@article{n, journal={Z. f{\\"u}r Physik}}\n'
            '@article{o, journal={Z. f\\"ur Physik}}\n'
        )
        self.assertEqual(self.suggestions(bibliography, "venue_variants"), [])
        # The same venue spelled differently in two files is not one file's inconsistency.
        self.write("main.tex", _MANUSCRIPT.replace("{references}", "{references,more}"))
        self.write("more.bib", "@article{z, journal={nature}}\n")
        self.assertEqual(self.suggestions("@article{y, journal={Nature}}\n", "venue_variants"), [])

    def test_the_number_of_reported_venues_is_capped(self) -> None:
        venues = MAX_VENUE_GROUPS + 2
        bibliography = "".join(
            f"@article{{a{index}, journal={{Venue {chr(65 + index)} Letters}}}}\n"
            f"@article{{b{index}, journal={{Venue {chr(65 + index)} Lett.}}}}\n"
            for index in range(venues)
        )
        findings = self.suggestions(bibliography, "venue_variants")
        self.assertEqual(len(findings), MAX_VENUE_GROUPS)
        self.assertEqual([item.details["omitted_venues"] for item in findings[-2:]], [0, 2])
        self.assertIn("2 more such venue(s) are not listed", findings[-1].message)
        self.assertNotIn("not listed", findings[0].message)

    def test_names_ignore_case_punctuation_years_and_function_words(self) -> None:
        same = (
            "Proc. of the 2019 Int. Conf. on Machine Learning",
            "Proceedings International Conference on Machine Learning (2020)",
            "proceedings of the international conference on machine learning",
            "PROC. INTL. CONF. MACH. LEARN.",
            "Proceedings of the 1st International Conference & Machine Learning",
        )
        names = {venue_name(venue_spelling(value)) for value in same}
        self.assertEqual(
            names, {("proceedings", "international", "conference", "machine", "learning")}
        )
        self.assertEqual(venue_spelling("CVPR'17, 2018"), ("CVPR",))
        self.assertEqual(venue_spelling("Proc. {IEEE} \\& ACM"), ("Proc.", "IEEE", "&", "ACM"))
        for generic in ("Proceedings", "Proc. Int. Conf.", "Journal", "2019", "34th Annual"):
            with self.subTest(generic=generic):
                self.assertIsNone(venue_name(venue_spelling(generic)))
        # An abbreviation of several possible words is left as written.
        self.assertNotEqual(
            venue_name(venue_spelling("Comput. Linguist.")),
            venue_name(venue_spelling("Computational Linguistics")),
        )

    def test_long_adversarial_venue_values_are_read_in_linear_time(self) -> None:
        fragments = ("'1", "a.", "&", "x'99", "1st", "2019", "_", " ", "a" * 50 + ".")
        for fragment in fragments:
            value = (fragment * (_LONG_VALUE_CHARACTERS // len(fragment) + 1))[
                :_LONG_VALUE_CHARACTERS
            ]
            with self.subTest(fragment=fragment):
                started = time.perf_counter()
                venue_name(venue_spelling(value))
                self.assertLess(time.perf_counter() - started, _LINEAR_TIME_LIMIT_SECONDS)


class SuggestionReportingTests(AsyncProjectTestCase):
    """Whole jobs: the suggestions are optional advice, selected by code and silenced in place."""

    def setUp(self) -> None:
        """Write a manuscript whose bibliography triggers each of the three suggestions."""
        super().setUp()
        self.project = self.write_project(
            {
                "main.tex": _MANUSCRIPT,
                "references.bib": (
                    "@article{preprint, title={T}, journal={arXiv preprint arXiv:2101.00001}, "
                    "year={2021}}\n"
                    "@article{linked, title={U}, journal={Nature}, doi={10.1038/x}, "
                    "url={https://doi.org/10.1038/x}, year={2020}}\n"
                    "@article{lower, title={V}, journal={nature}, year={2019}}\n"
                ),
            }
        )

    async def run_bib(self, command: str = "bib", **settings: object):
        """Run ``command`` on the project and return its report.

        Args:
            command: The job command, ``bib`` or ``inspect``.
            **settings: Fields of the ``Settings`` beyond the main file.

        Returns:
            The report of the job.
        """
        return await run_job(
            JobRequest(command, self.project, Settings(main="main.tex", **settings))
        )

    def codes(self, report) -> list[str]:
        """Return the codes of the report's suggestions, in report order."""
        return [item.code for item in report.findings if item.is_suggestion and item.code]

    async def test_suggestions_are_reported_only_when_selected_and_never_change_the_outcome(
        self,
    ) -> None:
        default = await self.run_bib()
        self.assertEqual(self.codes(default), [])
        selected = await self.run_bib(checks=_SUGGESTIONS)
        self.assertEqual(sorted(self.codes(selected)), ["BIB111", "BIB112", "BIB113"])
        self.assertEqual(selected.outcome, default.outcome)
        # A family selector selects the suggestions too; ALL alone does not.
        family = await self.run_bib(checks=CheckSelection(select=("BIB",)))
        self.assertEqual(sorted(self.codes(family)), ["BIB111", "BIB112", "BIB113"])
        ignored = await self.run_bib(checks=replace(_SUGGESTIONS, ignore=("BIB112",)))
        self.assertEqual(sorted(self.codes(ignored)), ["BIB111", "BIB113"])

    async def test_a_comment_before_the_entry_silences_its_suggestion(self) -> None:
        bibliography = self.project / "references.bib"
        bibliography.write_text(
            "% fledge: ignore BIB111\n" + bibliography.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        for command in ("bib", "inspect"):
            with self.subTest(command=command):
                report = await self.run_bib(command, checks=_SUGGESTIONS)
                self.assertNotIn("BIB111", self.codes(report))
                self.assertIn("BIB112", self.codes(report))
                self.assertEqual(
                    report.execution["silenced_suggestions"],
                    [{"code": "BIB111", "path": "references.bib", "line": 2}],
                )


if __name__ == "__main__":
    unittest.main()
