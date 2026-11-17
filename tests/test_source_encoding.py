"""Sources that are not valid UTF-8: read for the checks, never rewritten (``TEX328``).

A Latin-1 paper is an ordinary paper. Its commands are ASCII, so the files, references,
packages, floats and metadata can be checked; its text does not encode back to its bytes,
so no transformation may touch it, and the scans that search its wording stay incomplete.
"""

from __future__ import annotations

import unittest

from latexprep.bibliography_transform import BibliographyTransformOptions, plan_bibliography
from latexprep.errors import PreparationError
from latexprep.findings import Finding, has_blockers
from latexprep.flat_layout import plan_flatten
from latexprep.manuscript import ManuscriptOptions, check_manuscript
from latexprep.manuscript_check_options import ManuscriptCheckOptions
from latexprep.manuscript_checks import check_manuscript_policies
from latexprep.source_analysis import analyze_selected_sources
from latexprep.source_graph import SourceGraph
from latexprep.source_transform import SourceTransformOptions, plan_source_transforms
from latexprep.submission_checks import check_submission
from latexprep.submission_options import SubmissionOptions
from latexprep.submission_text import read_submission_text
from tests.support import ProjectTestCase

FOREIGN = "source.foreign_encoding"
POLICIES = ManuscriptCheckOptions(
    required_metadata=("title", "author"),
    check_orcid=True,
    require_float_captions=True,
    require_float_labels=True,
    check_float_label_order=True,
    require_float_references=True,
)
# A paper in Latin-1, as pdfLaTeX with inputenc reads it: accents in the title, the author,
# a comment and a caption are single bytes above 0x7F. An accented letter in front of a
# blank ("qualité est") shows that the text is in no double-byte encoding.
LATIN1_PAPER = (
    b"\\documentclass{article}\n\\usepackage[latin1]{inputenc}\n"
    b"\\title{Une \xe9tude de la qualit\xe9}\n\\author{Ren\xe9e M\xfcller}\n"
    b"\\begin{document}\n\\maketitle\nLa qualit\xe9 est \xe9lev\xe9e.\n"
    b"Voir la figure~\\ref{fig:a}. % TODO remarque priv\xe9e\n"
    b"\\begin{figure}\\centering\\rule{1cm}{1cm}\\caption{R\xe9sultats}\\label{fig:a}"
    b"\\end{figure}\n\\end{document}\n"
)


class ForeignEncodingTests(ProjectTestCase):
    """A source that is not valid UTF-8 is read as text and kept as bytes."""

    def policies(self) -> dict[str, Finding]:
        """Return the manuscript policy findings of the project, by rule without its prefix."""
        findings = check_manuscript_policies(self.root, "main.tex", POLICIES)
        return {item.rule.removeprefix("manuscript."): item for item in findings}

    def note(self, name: str = "main.tex") -> Finding:
        """Return the note that ``name`` is not UTF-8."""
        findings = analyze_selected_sources(self.root, "main.tex").findings
        return next(item for item in findings if item.rule == FOREIGN and item.path == name)

    def test_the_commands_of_a_latin1_source_are_read(self) -> None:
        self.write("main.tex", LATIN1_PAPER)
        graph = SourceGraph.build(self.root, "main.tex")
        source = graph.sources["main.tex"]
        # The text reads as written, and the source is marked as not UTF-8.
        self.assertIn("\\author{Renée Müller}", source.text)
        self.assertTrue(source.foreign)
        note = self.note()
        self.assertEqual(
            (note.code, note.status, note.severity), ("TEX328", "inconclusive", "warning")
        )
        self.assertIn("never changed", note.message)
        # The note names the line of the first byte that is not UTF-8 and a conversion
        # that reads the file as Fledge does.
        self.assertEqual(note.line, 3)
        self.assertIn("iconv -f cp1252 -t utf-8", note.suggestion or "")
        self.assertNotIn("source.encoding", {item.rule for item in graph.findings})
        # Title, author, caption, label and reference were found: every policy is decided.
        policies = self.policies()
        self.assertEqual(len(policies), 6)
        for rule, finding in policies.items():
            with self.subTest(rule=rule):
                self.assertEqual(finding.status, "passed", finding.message)
        constraints = check_manuscript(
            self.root, "main.tex", ManuscriptOptions(forbidden_packages=("showframe",))
        )
        self.assertEqual({item.status for item in constraints}, {"passed"})

    def test_a_violation_in_such_a_source_is_still_found(self) -> None:
        # Counter-tests: reading the file must not turn into trusting it.
        self.write("main.tex", LATIN1_PAPER.replace(b"\\caption{R\xe9sultats}", b""))
        self.assertEqual(self.policies()["float_captions"].status, "failed")
        self.write("main.tex", LATIN1_PAPER.replace(b"\\author{Ren\xe9e M\xfcller}", b"\\author{}"))
        self.assertEqual(self.policies()["required_metadata"].status, "failed")
        self.write(
            "main.tex",
            LATIN1_PAPER.replace(b"\\usepackage[latin1]{inputenc}", b"\\usepackage{showframe}"),
        )
        constraints = check_manuscript(
            self.root, "main.tex", ManuscriptOptions(forbidden_packages=("showframe",))
        )
        self.assertIn("failed", {item.status for item in constraints})
        self.write("main.tex", LATIN1_PAPER.replace(b"\\maketitle", b"\\maketitle\\input{absent}"))
        findings = analyze_selected_sources(self.root, "main.tex").findings
        self.assertIn("source.missing_dependency", {item.rule for item in findings})

    def test_one_stray_byte_leaves_the_rest_of_a_utf8_source_as_written(self) -> None:
        # A UTF-8 paper with one Latin-1 byte: only that byte is read otherwise.
        text = "\\documentclass{article}\n\\title{Qualité}\n\\begin{document}\n".encode()
        self.write("main.tex", text + b"Un caf\xe9 au lait.\n\\end{document}\n")
        source = SourceGraph.build(self.root, "main.tex").sources["main.tex"]
        self.assertIn("\\title{Qualité}", source.text)
        self.assertIn("Un café au lait.", source.text)
        # The five bytes that Windows-1252 leaves undefined become U+FFFD, not line breaks.
        self.write("main.tex", b"\\documentclass{article}\n% a\x81b\x85c\n\\begin{document}\n")
        source = SourceGraph.build(self.root, "main.tex").sources["main.tex"]
        self.assertEqual(source.text.count("\n"), len(source.text.splitlines()))
        self.assertIn("a\N{REPLACEMENT CHARACTER}b…c", source.text)

    def test_text_that_holds_nul_bytes_is_still_unreadable(self) -> None:
        # UTF-16 has no ASCII command in it for a byte-wise reader.
        self.write("main.tex", "\\documentclass{article}\n\\input{body}\n")
        self.write("body.tex", "\\section{Titre}\n".encode("utf-16"))
        graph = SourceGraph.build(self.root, "main.tex")
        self.assertNotIn("body.tex", graph.sources)
        rules = {item.rule for item in graph.findings if item.path == "body.tex"}
        self.assertEqual(rules, {"source.encoding"})

    def test_text_that_may_be_double_byte_is_not_read(self) -> None:
        # Review: in Shift-JIS the character 表 ends in a backslash byte. Read byte by
        # byte, 表\input{appendix} hid the command, and the missing file went unreported.
        self.write("main.tex", "\\documentclass{article}\n\\input{body}\n")
        cases = {
            "Shift-JIS": "\\section{表}\n表\\input{appendix}\n".encode("shift_jis"),
            "Big5": "\\section{許功蓋}\n".encode("big5"),
            # One accented letter in front of a brace and nothing that rules out GBK: the
            # brace may be half of a character, so the file is left unread.
            "ambiguous": b"\\section{A}\\label{sec:Poincar\xe9}\n",
        }
        for name, body in cases.items():
            with self.subTest(encoding=name):
                self.write("body.tex", body)
                graph = SourceGraph.build(self.root, "main.tex")
                self.assertNotIn("body.tex", graph.sources)
                rules = {item.rule for item in graph.findings if item.path == "body.tex"}
                self.assertEqual(rules, {"source.encoding"})
        # In GB 2312 both bytes of a character are above 0x7F, so no brace can be one.
        self.write("body.tex", "\\section{中文}\n这是中文。\n".encode("gb2312"))
        graph = SourceGraph.build(self.root, "main.tex")
        self.assertEqual([c.name for c in graph.sources["body.tex"].commands], ["section"])
        self.assertEqual(self.note("body.tex").code, "TEX328")

    def test_citation_keys_in_such_a_source_are_not_renamed(self) -> None:
        # Review: a key rename rewrote the Latin-1 file as UTF-8 and shipped it.
        paper = LATIN1_PAPER.replace(
            b"\\end{document}", b"\\cite{oldKey}\n\\bibliography{refs}\n\\end{document}"
        )
        self.write("main.tex", paper)
        self.write("refs.bib", "@misc{oldKey, title = {A title}, year = {2020}}\n")
        options = BibliographyTransformOptions(key_renames=(("oldKey", "newKey"),))
        proposal = plan_bibliography(self.root, "main.tex", options)
        self.assertEqual((proposal.files, proposal.changes), ({}, []))
        (finding,) = proposal.findings
        self.assertTrue(finding.is_blocking)
        self.assertIn("main.tex is not valid UTF-8 (TEX328)", finding.message)
        self.assertEqual((self.root / "main.tex").read_bytes(), paper)

    def test_a_source_that_is_not_utf8_is_never_rewritten(self) -> None:
        self.write("main.tex", LATIN1_PAPER)
        source = SourceGraph.build(self.root, "main.tex").sources["main.tex"]
        for text in (None, source.text, source.text + "% added\n"):
            with self.assertRaisesRegex(PreparationError, "cannot be rewritten"):
                source.encode(text)
        self.assertFalse(SourceGraph.build(self.root, "main.tex").is_complete)
        cases = {
            "comment removal": {"comment_policy": "all"},
            "private comment removal": {"comment_policy": "private"},
            "included-PDF information": {"suppress_included_pdf_information": True},
            "input merging": {"merge_inputs": True},
        }
        for name, options in cases.items():
            with self.subTest(transformation=name):
                plan = plan_source_transforms(
                    self.root, "main.tex", SourceTransformOptions(**options)
                )
                self.assertTrue(has_blockers(plan.findings), plan.findings)
                self.assertEqual((plan.contents, plan.changes), ({}, []))
        flat = plan_flatten(self.root, "main.tex")
        self.assertTrue(has_blockers(flat.findings))
        self.assertEqual(flat.contents, {})
        self.assertEqual((self.root / "main.tex").read_bytes(), LATIN1_PAPER)
        # A flat layout would have to rewrite the path of an input in a folder: the plan
        # is refused, and the main file gets no rewritten content.
        nested = LATIN1_PAPER.replace(b"\\maketitle", b"\\maketitle\\input{sections/intro}")
        self.write("main.tex", nested)
        self.write("sections/intro.tex", "Text.\n")
        flat = plan_flatten(self.root, "main.tex")
        self.assertTrue(has_blockers(flat.findings))
        self.assertNotIn("main.tex", flat.contents)

    def test_a_package_that_is_not_utf8_does_not_stop_comment_removal_elsewhere(self) -> None:
        # The algorithm.sty of venue kits has one Latin-1 byte; it is copied as it is, and
        # the comments of the paper itself are still removed.
        self.write(
            "main.tex",
            "\\documentclass{article}\n\\usepackage{algorithm}\n% private note\n"
            "\\begin{document}\nText.\n\\end{document}\n",
        )
        package = b"%% Copyright (C) Rog\xe9rio\n\\ProvidesPackage{algorithm}\n"
        self.write("algorithm.sty", package)
        plan = plan_source_transforms(
            self.root, "main.tex", SourceTransformOptions(comment_policy="all")
        )
        self.assertFalse(has_blockers(plan.findings), plan.findings)
        self.assertEqual(set(plan.contents), {"main.tex"})
        self.assertNotIn(b"private note", plan.contents["main.tex"])
        self.assertEqual(self.note("algorithm.sty").code, "TEX328")

    def test_the_scans_of_its_wording_stay_incomplete(self) -> None:
        # The private note and the name are in the file, in bytes that are not UTF-8: a scan
        # that cannot be sure of the text must not report it clean.
        self.write("main.tex", LATIN1_PAPER)
        options = SubmissionOptions(scan_private_comments=True, identity_terms=("Renée Müller",))
        findings = {item.rule: item for item in check_submission(self.root, "main.tex", options)}
        for rule in ("submission.private_comments", "submission.identity_terms"):
            with self.subTest(rule=rule):
                self.assertNotEqual(findings[rule].status, "passed", findings[rule].message)
        # No field of such a paper is offered as ready to paste into a submission form.
        fields = read_submission_text(self.root, "main.tex")
        self.assertEqual(fields["title"].status, "unresolved")
        self.assertTrue(any("not valid UTF-8" in issue for issue in fields["title"].issues))


if __name__ == "__main__":
    unittest.main()
