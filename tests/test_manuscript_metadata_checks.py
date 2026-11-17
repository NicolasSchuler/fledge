r"""Required author metadata and ORCID checks next to constructs Fledge does not read.

A title or author with literal text satisfies the required-metadata check whatever stands
beside it; an unread construct matters only where the field has no literal text without
it. The ORCID check reads identifiers wherever a command carries one, and a part of the
project that was not read stays a gap only while no author was found. Every relaxation is
paired with a case in which the same kind of construct hides or carries a violation.
"""

from __future__ import annotations

import unittest

from latexprep.findings import Finding
from latexprep.manuscript_check_options import ManuscriptCheckOptions
from latexprep.manuscript_checks import check_manuscript_policies
from latexprep.submission_checks import check_submission
from latexprep.submission_options import SubmissionOptions
from tests.support import ProjectTestCase, clean_case

CAMERA_READY = ManuscriptCheckOptions(required_metadata=("title", "author"), check_orcid=True)
# A missing input file: a part of the project that was not read.
UNREAD_INPUT = "\\input{missing}\n"


class AuthorMetadataTests(ProjectTestCase):
    """The required-metadata (``MAN008``) and ORCID (``MAN009``) policies on source fixtures."""

    def findings(
        self, preamble: str, options: ManuscriptCheckOptions = CAMERA_READY
    ) -> dict[str, Finding]:
        r"""Check an ``article`` with ``preamble`` and an empty body.

        Args:
            preamble: The source between the class line and ``\begin{document}``.
            options: The policies to check.

        Returns:
            Every finding, by its rule name without ``manuscript.``.
        """
        self.write(
            "main.tex",
            "\\documentclass{article}\n" + preamble + "\\begin{document}\n\\end{document}\n",
        )
        return {
            item.rule.removeprefix("manuscript."): item
            for item in check_manuscript_policies(self.root, "main.tex", options)
        }

    def metadata(self, preamble: str) -> Finding:
        """Return the required-metadata finding for ``preamble``."""
        return self.findings(preamble)["required_metadata"]

    def orcid(self, preamble: str) -> Finding:
        """Return the ORCID finding for ``preamble``."""
        return self.findings(preamble)["orcid"]

    def assert_status(self, finding: Finding, status: str) -> None:
        """Assert the status of a finding and show its details when it differs."""
        self.assertEqual(finding.status, status, finding.details)

    @clean_case("MAN008")
    def test_formatting_and_class_commands_beside_the_names_leave_the_fields_present(
        self,
    ) -> None:
        # From the NeurIPS, AACL, WACV, Elsevier and LNCS author blocks of the corpus.
        cases = {
            "spaces and font switches": "\\author{\\mdseries Ada Example$^{1}$\\quad "
            "Bo Sample$^{2}$\\qquad Cy Third}\n",
            "orcidlink and line spacing": "\\author{Ada Example$^{1}$\\,\\orcidlink"
            "{0000-0002-1825-0097} \\qquad Bo Sample\\\\[4pt] Example University}\n",
            "corresponding-author mark": "\\author[1]{Ada Example\\corref{cor1}}\n",
            "unknown class command": "\\author{Ada Example\\customauthormark{1}}\n",
        }
        for name, author in cases.items():
            with self.subTest(name=name):
                finding = self.metadata("\\title{A Study}\n" + author)
                self.assert_status(finding, "passed")
                self.assertEqual(finding.details["uncertainty"], [])
        spaced = self.metadata("\\title{A Study}\n" + cases["orcidlink and line spacing"])
        author = spaced.details["metadata"]["author"][0]
        self.assertNotIn("unbalanced math delimiters", author["uncertainty"])

    def test_a_title_with_a_pdf_alternative_or_a_project_word_is_present(self) -> None:
        for title in (
            "\\title{Graphs\\texorpdfstring{\\\\}{ } in Practice}\n",
            # The CVPR kit names the conference with its own macro.
            "\\def\\confName{CVPR}\n\\title{\\LaTeX\\ Guidelines for \\confName~Proceedings}\n",
        ):
            with self.subTest(title=title):
                self.assert_status(self.metadata(title + "\\author{Ada Example}\n"), "passed")

    def test_a_project_macro_that_typesets_its_argument_in_every_branch_keeps_the_names(
        self,
    ) -> None:
        # The NeurIPS paper marks camera-ready changes with \rev{…}.
        finding = self.metadata(
            "\\newif\\ifshowrev\n"
            "\\newcommand{\\rev}[1]{\\ifshowrev{\\color{blue}#1}\\else#1\\fi}\n"
            "\\title{A Study}\n\\author{\\rev{Ada Example\\quad Bo Sample}}\n"
        )
        self.assert_status(finding, "passed")

    def test_a_project_macro_that_may_drop_its_argument_leaves_the_author_open(self) -> None:
        # Counter-tests: the argument is not typeset in some branch, by an alias, after an
        # optional argument, or by a definition Fledge cannot read.
        cases = {
            "one branch": "\\newcommand{\\rev}[1]{\\ifshowrev #1\\fi}\n",
            "gobbling alias": "\\makeatletter\\let\\rev\\@gobble\\makeatother\n",
            "unused braced argument": "\\newcommand{\\rev}[2][x]{#1}\n",
            "nested macro that drops": "\\newcommand{\\hide}[1]{}\n"
            "\\newcommand{\\rev}[1]{\\hide{#1}}\n",
            "unread definition": "\\NewDocumentCommand{\\rev}{m}{}\n",
            "later definition that drops": "\\newcommand{\\rev}[1]{#1}\n"
            "\\renewcommand{\\rev}[1]{}\n",
            "recursive definitions": "\\newcommand{\\rev}[1]{\\other{#1}}\n"
            "\\newcommand{\\other}[1]{\\rev{#1}}\n",
        }
        for name, definition in cases.items():
            with self.subTest(name=name):
                finding = self.metadata(definition + "\\title{A}\n\\author{\\rev{Ada Example}}\n")
                self.assert_status(finding, "inconclusive")
        # The braced argument after an optional one is #2.
        kept = self.metadata(
            "\\newcommand{\\rev}[2][x]{#2}\n\\title{A}\n\\author{\\rev{Ada Example}}\n"
        )
        self.assert_status(kept, "passed")

    def test_a_command_that_selects_among_its_arguments_keeps_no_name(self) -> None:
        # Review: \iftoggle{blind}{Anonymous Author(s)}{#1} printed the placeholder, and
        # the check passed on the name it was given.
        cases = {
            "etoolbox toggle": "\\newcommand\\anon[1]{\\iftoggle{blind}{Anonymous Author(s)}{#1}}\n"
            "\\author{\\anon{Ada Example}}\n",
            "ifthen": "\\newcommand\\anon[1]{\\ifthenelse{\\boolean{blind}}{Anonymous}{#1}}\n"
            "\\author{\\anon{Ada Example}}\n",
            "kernel gobble": "\\makeatletter\\newcommand\\anon[1]{\\@gobble{#1}}\\makeatother\n"
            "\\author{\\anon{Ada Example}}\n",
            "phantom": "\\newcommand\\anon[1]{\\phantom{#1}}\n\\author{\\anon{Ada Example}}\n",
            "unknown command in the definition": "\\newcommand\\anon[1]{\\maybe{#1}}\n"
            "\\author{\\anon{Ada Example}}\n",
            "toggle in the declaration": "\\author{\\iftoggle{blind}{}{Ada Example}}\n",
            "name built in the declaration": "\\author{\\csname @gobble\\endcsname{Ada Example}}\n",
        }
        for name, author in cases.items():
            with self.subTest(name=name):
                self.assert_status(self.metadata("\\title{A Study}\n" + author), "inconclusive")
        # A macro that typesets its argument in a bare group or through a formatting
        # command keeps the name, and so does a name beside a toggle.
        for author in (
            "\\newcommand\\rev[1]{{\\color{blue}#1}}\n\\author{\\rev{Ada Example}}\n",
            "\\newcommand\\rev[1]{\\textcolor{blue}{\\textbf{#1}}}\n\\author{\\rev{Ada Example}}\n",
            "\\author{Ada Example\\iftoggle{blind}{}{\\thanks{Funded.}}}\n",
        ):
            with self.subTest(author=author):
                self.assert_status(self.metadata("\\title{A Study}\n" + author), "passed")

    def test_an_author_that_is_only_an_unread_macro_stays_inconclusive(self) -> None:
        for author in (
            "\\newcommand{\\names}{Ada Example}\n\\author{\\names}\n",
            "\\author{\\generatedauthors}\n",
            # An identifier or an author mark beside the macro names no one.
            "\\author{\\names\\qquad\\orcidlink{0000-0002-1825-0097}}\n",
            "\\author{\\names\\corref{cor1}}\n",
            "\\author{\\names\\inst{1}}\n",
        ):
            with self.subTest(author=author):
                finding = self.metadata("\\title{A Study}\n" + author)
                self.assert_status(finding, "inconclusive")

    def test_an_author_in_a_conditional_or_beside_a_stray_branch_word_stays_open(self) -> None:
        for author in (
            "\\author{\\ifcameraready Ada Example\\else Anonymous\\fi}\n",
            "\\author{\\ifcameraready Ada Example\\fi}\n",
            "\\newcommand{\\hideauthors}{\\iffalse}\n\\author{\\hideauthors Ada Example\\fi}\n",
            # Only one branch declares the author under camera-ready, as before.
            "\\ifcameraready\\author{Ada Example\\qquad\\orcidlink{0000-0002-1825-0097}}\\fi\n",
        ):
            with self.subTest(author=author):
                finding = self.metadata("\\title{A Study}\n" + author)
                self.assert_status(finding, "inconclusive")

    def test_empty_or_formatting_only_authors_still_fail(self) -> None:
        for author in ("\\author{}\n", "\\author{\\qquad\\mdseries}\n", "\\author{Author name}\n"):
            with self.subTest(author=author):
                finding = self.metadata("\\title{A Study}\n" + author)
                self.assert_status(finding, "failed")

    @clean_case("MAN008", "MAN009")
    def test_a_construct_the_scan_only_suspects_does_not_hide_metadata_that_was_found(
        self,
    ) -> None:
        # A name built at run time is a gap of the source graph; it holds no identifier.
        declared = "\\title{A Study}\n\\author{Ada Example\\orcidlink{0000-0002-1825-0097}}\n"
        findings = self.findings(declared + "\\csname generated\\endcsname\n")
        for rule in ("required_metadata", "orcid"):
            with self.subTest(rule=rule):
                self.assert_status(findings[rule], "passed")
        # A file that was not read does not take the author away, but it may hold another
        # identifier (review: an invalid ORCID in such a file passed).
        findings = self.findings(declared + UNREAD_INPUT)
        self.assert_status(findings["required_metadata"], "passed")
        self.assert_status(findings["orcid"], "inconclusive")

    def test_an_unread_part_of_the_project_may_hold_metadata_that_was_not_found(self) -> None:
        # Counter-test: without an author, the unread file may declare it, and its ORCID.
        findings = self.findings("\\title{A Study}\n\\orcid{0000-0002-1825-0097}\n" + UNREAD_INPUT)
        for rule in ("required_metadata", "orcid"):
            with self.subTest(rule=rule):
                self.assert_status(findings[rule], "inconclusive")
                self.assertTrue(findings[rule].details["uncertainty"])

    def test_icml_title_and_author_commands_declare_the_fields(self) -> None:
        icml = (
            "\\icmltitlerunning{Short}\n\\twocolumn[\n\\icmltitle{A Study \\\\ of Things}\n"
            "\\begin{icmlauthorlist}\n\\icmlauthor{Ada Example}{inst}\n"
            "\\icmlauthor{Bo Sample}{inst}\n\\end{icmlauthorlist}\n"
            "\\icmlaffiliation{inst}{Example University}\n]\n"
        )
        self.assert_status(self.metadata(icml), "passed")
        # Counter-test: an empty ICML author is still missing.
        empty = icml.replace("Ada Example", "").replace("Bo Sample", "")
        self.assert_status(self.metadata(empty), "failed")
        asme = "\\title{A Study}\n\\SetAuthors{Ada Example\\affil{1}, Bo Sample\\affil{2}}\n"
        self.assert_status(self.metadata(asme), "passed")

    def test_identifiers_inside_orcidlink_are_checked_beside_unread_commands(self) -> None:
        # Counter-tests: a placeholder or a wrong checksum still fails, also when a part of
        # the project was not read.
        for identifier in ("0000-0002-1825-0098", "1234-5678-9012", "0000-0000-0000-0000X"):
            for unread in ("", UNREAD_INPUT):
                with self.subTest(identifier=identifier, unread=bool(unread)):
                    finding = self.orcid(
                        "\\title{A}\n\\author{Ada Example$^{1}$\\,\\orcidlink{"
                        + identifier
                        + "}\\qquad Bo Sample}\n"
                        + unread
                    )
                    self.assert_status(finding, "failed")

    def test_an_identifier_given_by_a_macro_or_parameter_is_a_gap_not_a_checksum(self) -> None:
        for preamble in (
            "\\author{Ada Example\\orcidlink{\\adaorcid}}\n",
            # A macro that passes its argument on: its uses carry the identifiers.
            "\\newcommand{\\oid}[1]{\\orcidlink{#1}}\n\\author{Ada Example\\oid{0000}}\n",
        ):
            with self.subTest(preamble=preamble):
                self.assert_status(self.orcid(preamble), "inconclusive")

    def test_the_identity_terms_of_a_review_are_found_in_such_an_author_block(self) -> None:
        # The anonymous-review identity check reads the files, not this reading.
        self.write(
            "main.tex",
            "\\documentclass{article}\n\\newcommand{\\rev}[1]{#1}\n"
            "\\author{\\mdseries\\rev{Ada Example}\\,\\orcidlink{0000-0002-1825-0097}"
            "\\qquad Bo Sample\\\\[4pt] Example University}\n"
            "\\begin{document}\n\\maketitle\n\\end{document}\n",
        )
        options = SubmissionOptions(identity_terms=("Ada Example", "Example University"))
        finding = next(
            item
            for item in check_submission(self.root, "main.tex", options)
            if item.rule == "submission.identity_terms"
        )
        self.assertEqual((finding.code, finding.status), ("PRV001", "failed"))
        self.assertEqual(len(finding.details["matches"]), 2)


if __name__ == "__main__":
    unittest.main()
