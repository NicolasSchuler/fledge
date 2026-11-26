"""Clean cases: input that a check must accept, for checks whose linked tests only show a hit.

A check that is tested only with input it rejects cannot be told from one that rejects
everything. Each test here runs a check on input that is allowed, such as a matching case, a
closed construct or a value inside its limit, and asserts that no finding of the check appears.
``@clean_case`` marks the test for the catalogue check in ``tests/test_rules.py``, which lists
the checks that have no clean case yet.
"""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from latexprep.bbl_checks import BblOptions
from latexprep.bibliography_checks import check_bibliography_coverage_and_fields
from latexprep.bibliography_integrity import check_bibliography_integrity
from latexprep.build_log import bibliography_log_findings, log_findings
from latexprep.build_result import BuildResult
from latexprep.command_runner import CommandResult
from latexprep.config import Settings
from latexprep.core import run_job
from latexprep.findings import Finding, has_blockers
from latexprep.flat_layout import plan_flatten
from latexprep.job_context import JobRequest
from latexprep.latexmk_build import build_project
from latexprep.pdf_comparison import compare_pdfs
from latexprep.project import import_project
from latexprep.review_policy import (
    ReportingOptions,
    ReviewRule,
    has_unaccepted_blockers,
    reviewed_findings,
)
from latexprep.source_analysis import SourceAnalysis, analyze_sources
from latexprep.submission_readiness_checks import check_submission_readiness
from tests.fakes import FakeBuildRunner, fake_formatter
from tests.pdf_fakes import FakePdfRunner
from tests.support import (
    AsyncProjectTestCase,
    ProjectTestCase,
    clean_case,
    controlled_backends,
    format_with,
    sectioned_project,
    write_project,
)

DOCUMENT = "\\documentclass{article}\n"


def rules_of(findings: list[Finding]) -> set[str]:
    """Return the rule names of ``findings``."""
    return {item.rule for item in findings}


class SourceGraphCleanCases(ProjectTestCase):
    """Source constructs that the graph follows without any finding."""

    def analyse(self, main: str = "main.tex") -> SourceAnalysis:
        """Return the analysis of ``main``, which must be selected and have a complete graph."""
        analysis = analyze_sources(self.root, main)
        self.assertEqual(analysis.main, main)
        self.assertTrue(analysis.complete, analysis.findings)
        return analysis

    @clean_case("TEX001", "TEX002", "TEX003", "TEX004")
    def test_markers_in_comments_and_literals_and_resolved_labels_raise_nothing(self) -> None:
        self.write(
            "main.tex",
            DOCUMENT + "% TODO FIXME written in a comment\n\\label{one}\n\\verb|XXX \\todo{x}|\n"
            "See \\ref{one}.\n",
        )
        self.assertEqual(self.analyse().findings, [])

    @clean_case("TEX005", "TEX006")
    def test_present_relative_dependencies_are_neither_missing_nor_external(self) -> None:
        self.write("main.tex", DOCUMENT + "\\input{sections/body}\n")
        self.write("sections/body.tex", "Body.\n")
        analysis = self.analyse()
        self.assertEqual(analysis.dependencies, {"main.tex", "sections/body.tex"})
        self.assertEqual(rules_of(analysis.findings), set())

    @clean_case("TEX007")
    def test_a_path_spelled_with_the_case_of_the_file_is_not_a_case_mismatch(self) -> None:
        self.write("main.tex", DOCUMENT + "\\input{Body}\n")
        self.write("Body.tex", "Text")
        analysis = self.analyse()
        self.assertIn("Body.tex", analysis.dependencies)
        self.assertNotIn("source.case_mismatch", rules_of(analysis.findings))

    @clean_case("TEX303")
    def test_a_regular_style_file_is_read_and_not_reported_as_a_link(self) -> None:
        self.write("main.tex", DOCUMENT + "\\usepackage{helper}\n")
        self.write("helper.sty", "\\ProvidesPackage{helper}")
        analysis = self.analyse()
        self.assertIn("helper.sty", analysis.dependencies)
        self.assertNotIn("source.link", rules_of(analysis.findings))

    @clean_case("TEX307")
    def test_a_macro_outside_the_file_argument_does_not_make_the_path_dynamic(self) -> None:
        self.write(
            "main.tex",
            DOCUMENT + "\\newcommand{\\chosen}{body}\n\\emph{\\chosen}\n\\input{body}\n",
        )
        self.write("body.tex", "Body.\n")
        analysis = self.analyse()
        self.assertIn("body.tex", analysis.dependencies)
        self.assertNotIn("source.dynamic_path", rules_of(analysis.findings))

    @clean_case("TEX312")
    def test_a_font_resolved_outside_the_project_and_a_commented_lookup_read_nothing(self) -> None:
        self.write("body.ttf", b"font")
        self.write(
            "main.tex",
            DOCUMENT + "\\usepackage{fontspec}\n\\setmainfont{Times New Roman}\n"
            "\\setmonofont{body.ttf}\n% \\IfFileExists{x.tex}{}{}\n"
            "\\verb|\\directlua{require('x')}|\n",
        )
        analysis = self.analyse()
        self.assertEqual(analysis.dependencies, {"main.tex", "body.ttf"})
        self.assertNotIn("source.unsupported_command", rules_of(analysis.findings))
        # A family whose files the project holds may load them at run time, which is reported.
        self.write("Body-Bold.otf", b"font")
        self.write(
            "main.tex", DOCUMENT + "\\usepackage{fontspec}\n\\setmainfont{Body}[Extension=.otf]\n"
        )
        analysis = analyze_sources(self.root, "main.tex")
        self.assertIn("source.unsupported_command", rules_of(analysis.findings))

    @clean_case("TEX308")
    def test_a_comment_after_a_path_argument_does_not_touch_the_path(self) -> None:
        self.write("main.tex", DOCUMENT + "\\input{body} % the body of the paper\n% \\input{x}\n")
        self.write("body.tex", "Body.\n")
        analysis = self.analyse()
        self.assertIn("body.tex", analysis.dependencies)
        self.assertNotIn("source.path_literal_context", rules_of(analysis.findings))

    @clean_case("TEX309")
    def test_a_file_command_at_the_top_level_and_a_macro_without_one_are_not_nested(self) -> None:
        self.write("main.tex", DOCUMENT + "\\newcommand{\\note}[1]{\\emph{#1}}\n\\input{body}\n")
        self.write("body.tex", "Body.\n")
        analysis = self.analyse()
        self.assertIn("body.tex", analysis.dependencies)
        self.assertNotIn("source.nested_file_command", rules_of(analysis.findings))

    @clean_case("TEX310")
    def test_an_unconditional_reference_is_not_a_conditional_dependency(self) -> None:
        self.write("figure.pdf", b"image")
        self.write(
            "main.tex",
            DOCUMENT + "\\newif\\ifanonymous\n\\includegraphics{figure.pdf}\n\\ifanonymous\n"
            "Anonymous text.\n\\fi\n",
        )
        analysis = analyze_sources(self.root, "main.tex")
        self.assertIn("figure.pdf", analysis.dependencies)
        self.assertNotIn("source.conditional_dependency", rules_of(analysis.findings))

    @clean_case("TEX313", "TEX315")
    def test_a_subfile_of_a_selected_parent_is_supported_structure(self) -> None:
        self.write(
            "main.tex",
            DOCUMENT + "\\usepackage{subfiles}\n\\begin{document}\n\\subfile{chapter}\n"
            "\\end{document}\n",
        )
        self.write(
            "chapter.tex",
            "\\documentclass[main.tex]{subfiles}\n\\begin{document}\nText\n\\end{document}\n",
        )
        analysis = self.analyse()
        self.assertEqual(analysis.dependencies, {"main.tex", "chapter.tex"})
        self.assertFalse(
            rules_of(analysis.findings) & {"source.subfile_structure", "source.path_option"}
        )

    @clean_case("TEX314")
    def test_a_font_option_of_the_supported_set_is_not_reported(self) -> None:
        self.write("body.ttf", b"font")
        self.write("main.tex", DOCUMENT + "\\setmainfont{body.ttf}[Ligatures=TeX]\n")
        analysis = analyze_sources(self.root, "main.tex")
        self.assertIn("body.ttf", analysis.dependencies)
        self.assertNotIn("source.font_options", rules_of(analysis.findings))

    @clean_case("TEX320")
    def test_a_chain_of_inputs_without_a_return_is_not_a_cycle(self) -> None:
        self.write("main.tex", DOCUMENT + "\\input{first}\n")
        self.write("first.tex", "\\input{second}\n")
        self.write("second.tex", "End.\n")
        analysis = self.analyse()
        self.assertEqual(analysis.dependencies, {"main.tex", "first.tex", "second.tex"})
        self.assertNotIn("source.input_cycle", rules_of(analysis.findings))

    @clean_case("TEX321")
    def test_a_modest_chain_of_inputs_stays_within_the_depth_limit(self) -> None:
        self.write("main.tex", DOCUMENT + "\\input{part0}\n")
        for index in range(12):
            self.write(f"part{index}.tex", f"\\input{{part{index + 1}}}\n")
        self.write("part12.tex", "End.\n")
        analysis = self.analyse()
        self.assertIn("part12.tex", analysis.dependencies)
        self.assertNotIn("source.input_depth", rules_of(analysis.findings))

    @clean_case("TEX322")
    def test_a_traversal_within_its_command_budget_is_not_reported(self) -> None:
        self.write("main.tex", DOCUMENT + "\\input{body}\n" * 3)
        self.write("body.tex", "Body.\n")
        with patch("latexprep.source_graph.MAX_COMMANDS", 100):
            analysis = self.analyse()
        self.assertNotIn("source.expansion_limit", rules_of(analysis.findings))

    @clean_case("TEX323")
    def test_the_same_file_in_one_search_context_is_not_ambiguous(self) -> None:
        self.write("main.tex", DOCUMENT + "\\graphicspath{{a/}}\n\\input{body}\n\\input{body}\n")
        self.write("body.tex", "\\includegraphics{result.pdf}")
        self.write("a/result.pdf", b"a")
        plan = plan_flatten(self.root, "main.tex")
        self.assertNotIn("source.context_ambiguity", rules_of(plan.findings))
        self.assertFalse(has_blockers(plan.findings), plan.findings)

    @clean_case("TEX301", "TEX302", "TEX328")
    def test_a_utf8_source_inside_the_size_limit_is_read(self) -> None:
        self.write("main.tex", DOCUMENT + "\\input{body}\n")
        self.write("body.tex", "Caf\u00e9 \u00fcber na\u00efve.\n".encode())
        analysis = self.analyse()
        self.assertIn("body.tex", analysis.dependencies)
        self.assertFalse(
            rules_of(analysis.findings)
            & {"source.encoding", "source.foreign_encoding", "source.size_limit"}
        )

    @clean_case("TEX304", "TEX305")
    def test_closed_arguments_and_few_commands_stay_within_the_lexical_bounds(self) -> None:
        self.write("main.tex", DOCUMENT + "\\input{body}\n")
        self.write("body.tex", "One \\emph{closed} and \\textbf{two \\emph{nested}} arguments.\n")
        analysis = self.analyse()
        self.assertFalse(
            rules_of(analysis.findings) & {"source.argument_limit", "source.command_limit"}
        )

    @clean_case("TEX306", "TEX316", "TEX317", "TEX318", "TEX319", "TEX326")
    def test_closed_literals_plain_names_and_a_graphics_path_in_its_supported_form(self) -> None:
        self.write(
            "main.tex",
            DOCUMENT + "\\usepackage{styles}\n\\ProvidesPackage{name}\n"
            "\\graphicspath{{figures/}}\n\\begin{document}\n\\includegraphics{plot}\n"
            "\\verb|closed|\n\\end{document}\n",
        )
        self.write("styles.sty", "\\ProvidesPackage{styles}\n")
        self.write("figures/plot.pdf", b"image")
        analysis = self.analyse()
        self.assertIn("figures/plot.pdf", analysis.dependencies)
        self.assertEqual(rules_of(analysis.findings), set())

    @clean_case("TEX325", "TEX327")
    def test_a_single_document_root_needs_no_choice_and_an_existing_main_is_valid(self) -> None:
        self.write("main.tex", DOCUMENT)
        self.write("notes.tex", "Notes without a document class.\n")
        for main in (None, "main.tex"):
            with self.subTest(main=main):
                analysis = analyze_sources(self.root, main)
                self.assertEqual(analysis.roots, ["main.tex"])
                self.assertEqual(analysis.main, "main.tex")
                self.assertFalse(
                    rules_of(analysis.findings) & {"source.root_ambiguous", "source.main_invalid"}
                )


class FlatLayoutCleanCases(ProjectTestCase):
    """Projects that the flat layout can move without a finding."""

    @clean_case("PKG006")
    def test_literal_file_commands_are_followed_and_rewritten_without_a_blocker(self) -> None:
        self.write(
            "main.tex",
            DOCUMENT + "\\usepackage{local/good}\n\\input{sections/body}\n"
            "\\includegraphics{figures/plot.pdf}\n",
        )
        self.write("local/good.sty", "\\ProvidesPackage{good}")
        self.write("sections/body.tex", "Body.\n")
        self.write("figures/plot.pdf", b"image")
        plan = plan_flatten(self.root, "main.tex")
        self.assertEqual(plan.mapping["sections/body.tex"], "body.tex")
        self.assertEqual(plan.mapping["figures/plot.pdf"], "plot.pdf")
        self.assertNotIn("flatten.unsupported_source", rules_of(plan.findings))
        self.assertFalse(has_blockers(plan.findings), plan.findings)

    @clean_case("PKG007")
    def test_a_document_body_that_the_main_file_reads_is_not_another_document_root(self) -> None:
        self.write("main.tex", DOCUMENT + "\\input{body}\n")
        self.write("body.tex", "\\begin{document}\nBody.\n\\end{document}\n")
        plan = plan_flatten(self.root, "main.tex")
        self.assertIn("body.tex", plan.mapping)
        self.assertNotIn("flatten.multiple_roots", rules_of(plan.findings))

    @clean_case("PKG008")
    def test_an_unvisited_source_without_file_references_needs_no_compilation_context(
        self,
    ) -> None:
        self.write("main.tex", DOCUMENT)
        self.write("orphan.tex", "Text that names no other file.\n")
        plan = plan_flatten(self.root, "main.tex")
        self.assertNotIn("flatten.unresolved_context", rules_of(plan.findings))
        self.assertFalse(has_blockers(plan.findings), plan.findings)

    @clean_case("PKG001")
    def test_local_styles_with_different_names_do_not_collide(self) -> None:
        self.write(
            "main.tex", DOCUMENT + "\\usepackage{a/firstlocal}\n\\usepackage{b/secondlocal}\n"
        )
        self.write("a/firstlocal.sty", "\\ProvidesPackage{firstlocal}")
        self.write("b/secondlocal.sty", "\\ProvidesPackage{secondlocal}")
        plan = plan_flatten(self.root, "main.tex")
        self.assertEqual(plan.mapping["a/firstlocal.sty"], "firstlocal.sty")
        self.assertEqual(plan.mapping["b/secondlocal.sty"], "secondlocal.sty")
        self.assertNotIn("flatten.identity_collision", rules_of(plan.findings))

    @clean_case("PKG002")
    def test_a_style_file_name_without_protected_characters_is_moved_unchanged(self) -> None:
        self.write("main.tex", DOCUMENT + "\\usepackage{local/good}\n")
        self.write("local/good.sty", "\\ProvidesPackage{good}")
        plan = plan_flatten(self.root, "main.tex")
        self.assertEqual(plan.mapping["local/good.sty"], "good.sty")
        self.assertNotIn("flatten.identity_name", rules_of(plan.findings))

    @clean_case("PKG003")
    def test_a_relocated_style_with_an_unused_name_shadows_no_system_style(self) -> None:
        self.write("main.tex", DOCUMENT + "\\usepackage{local/uniquelocalstyle}\n")
        self.write("local/uniquelocalstyle.sty", "\\ProvidesPackage{uniquelocalstyle}")
        plan = plan_flatten(self.root, "main.tex")
        self.assertEqual(plan.mapping["local/uniquelocalstyle.sty"], "uniquelocalstyle.sty")
        self.assertNotIn("flatten.system_shadowing", rules_of(plan.findings))

    @clean_case("PKG004")
    def test_a_sidecar_stays_beside_the_only_file_it_describes(self) -> None:
        self.write("main.tex", DOCUMENT + "\\includegraphics{a/plot.pdf}\n")
        self.write("a/plot.pdf", b"image")
        self.write("a/plot.bb", "%% Bounding box")
        plan = plan_flatten(self.root, "main.tex")
        self.assertEqual(plan.mapping["a/plot.pdf"], "plot.pdf")
        self.assertEqual(plan.mapping["a/plot.bb"], "plot.bb")
        self.assertNotIn("flatten.companion_identity", rules_of(plan.findings))


class ImportCleanCases(unittest.TestCase):
    """Archives that import without a finding."""

    @clean_case("PKG005")
    def test_portable_file_names_in_a_zip_raise_no_warning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            archive = base / "input.zip"
            with zipfile.ZipFile(archive, "w") as packaged:
                for name in ("main.tex", "figures/plot.pdf", "sections/intro-1.tex"):
                    packaged.writestr(name, b"data")
            result = import_project(archive, base / "import", unwrap=False)
        self.assertEqual(result.files, ["figures/plot.pdf", "main.tex", "sections/intro-1.tex"])
        self.assertEqual(result.findings, [])


class FormattingCleanCases(unittest.IsolatedAsyncioTestCase):
    """Sources that the formatter handles without an unformattable finding."""

    async def format_source(self, content: bytes, results: list[CommandResult]) -> list[Finding]:
        """Format ``main.tex`` with a fake formatter that returns ``results``; keep the source."""
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "source").mkdir()
            (base / "source" / "main.tex").write_bytes(content)
            with patch("latexprep.formatting.shutil.which", return_value="/fake/tex-fmt"):
                result = await format_with(
                    base / "source", base / "work", fake_formatter(results), Settings()
                )
            self.assertEqual((base / "source" / "main.tex").read_bytes(), content)
        return result.findings

    @clean_case("FMT002")
    async def test_a_formatter_that_repeats_its_output_is_idempotent(self) -> None:
        output = CommandResult(0, "b\n", "", resolved_command=[])
        findings = await self.format_source(b"a\n", [output, output])
        self.assertEqual(
            [(item.code, item.status) for item in findings if item.code == "FMT001"],
            [("FMT001", "failed")],
        )
        self.assertNotIn("source.format_idempotence", rules_of(findings))

    @clean_case("FMT003", "FMT004")
    async def test_a_small_utf8_source_is_formatted(self) -> None:
        output = CommandResult(0, "Caf\u00e9\n", "", resolved_command=[])
        findings = await self.format_source("Caf\u00e9\n".encode(), [output, output])
        self.assertEqual([item.status for item in findings if item.code == "FMT001"], ["passed"])
        self.assertFalse(rules_of(findings) & {"source.format_encoding", "source.format_size"})


class BibliographyCleanCases(unittest.TestCase):
    """Bibliographies that raise no integrity or source finding."""

    @clean_case("BIB002", "BIB006", "BIB007")
    def test_distinct_keys_and_well_formed_dois_are_neither_duplicates_nor_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.bib").write_text("@article{one, title={First}, doi={10.1234/One}}\n")
            (root / "b.bib").write_text(
                "@article{two, title={Second}, doi={https://doi.org/10.1234/Two}}\n"
            )
            findings = check_bibliography_integrity(root)
        self.assertEqual(findings, [])

    @clean_case("BIB008")
    def test_a_utf8_bibliography_is_read_and_not_reported_as_unreadable(self) -> None:
        entry = "@article{one, author={J. M\u00fcller}, title={Caf\u00e9}}\n"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "refs.bib").write_bytes(entry.encode("utf-8"))
            self.assertEqual(check_bibliography_integrity(root), [])
            # The same entry in Latin-1 is what the check reports, so it did run above.
            (root / "refs.bib").write_bytes(entry.encode("latin-1"))
            findings = check_bibliography_integrity(root)
        self.assertEqual(rules_of(findings), {"bibliography.input"})

    @clean_case("BIB001", "BIB003", "BIB004", "BIB005")
    def test_a_well_formed_file_with_macros_months_and_crossrefs_raises_no_finding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "refs.bib").write_text(
                '@string{ej = "Example Journal"}\n'
                "@article{parent, author={A. B. Alpha}, title={Parent}, journal=ej, month=jan,"
                " year={2020}}\n"
                "@incollection{child, author={C. Gamma}, title={Child}, crossref={parent},"
                " year={2021}}\n"
            )
            findings = check_bibliography_integrity(root)
        self.assertEqual(findings, [])

    @clean_case("BIB009")
    def test_a_complete_project_yields_no_source_error_note(self) -> None:
        main = (
            "\\documentclass{article}\n\\begin{document}\nA \\cite{knuth}.\n"
            "\\bibliographystyle{plain}\n\\bibliography{refs}\n\\end{document}\n"
        )
        bibliography = "@book{knuth, author={Donald Knuth}, title={The TeXbook}, year={1984}}\n"
        with tempfile.TemporaryDirectory() as directory:
            project = write_project(
                Path(directory) / "paper", {"main.tex": main, "refs.bib": bibliography}
            )
            findings = check_bibliography_coverage_and_fields(
                project, "main.tex", Settings().bibliography_checks
            )
        self.assertTrue(any(item.code == "BIB101" and item.status == "passed" for item in findings))
        self.assertNotIn("bibliography.source_error", rules_of(findings))


class ComparisonCleanCases(unittest.IsolatedAsyncioTestCase):
    """PDF comparisons that find no difference."""

    @clean_case("CMP001", "CMP002", "CMP003")
    async def test_equal_page_counts_text_and_renders_all_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            left, right = base / "a.pdf", base / "b.pdf"
            left.write_bytes(b"%PDF-test-left")
            right.write_bytes(b"%PDF-test-right")
            findings = await compare_pdfs(left, right, base / "compare", FakePdfRunner())
        by_rule = {item.rule: item for item in findings}
        for rule in ("compare.page_count", "compare.text", "compare.rendering"):
            self.assertEqual(by_rule[rule].status, "passed", rule)


class ReviewCleanCases(unittest.TestCase):
    """Reviews that match a finding."""

    @clean_case("RPT101", "RPT102")
    def test_a_review_that_matches_a_waivable_finding_is_neither_unmatched_nor_ineligible(
        self,
    ) -> None:
        failed = Finding(
            "pdf.rendered_text_size",
            "Caption text is small",
            severity="error",
            status="failed",
            path="paper.pdf",
            details={"stage": "final", "page": 4},
        )
        options = ReportingOptions(
            accepted_exceptions=(ReviewRule("PDF202", "Reviewed size", page_start=4, page_end=4),)
        )
        self.assertFalse(has_unaccepted_blockers([failed], options))
        reviewed = reviewed_findings([failed], options)
        self.assertEqual((reviewed[0].status, reviewed[0].rule), ("failed", failed.rule))
        decisions = reviewed[0].details["review"]
        assert isinstance(decisions, list)
        self.assertEqual(decisions[0]["action"], "accepted_exception")
        self.assertNotIn("report.review_unmatched", rules_of(reviewed))
        self.assertNotIn("report.review_ineligible", rules_of(reviewed))


class BuildLogCleanCases(unittest.TestCase):
    """A TeX log that only looks like the messages the build checks search for."""

    CLEAN_LOG = """\
This is pdfTeX, Version 3.141592653-2.6-1.40.26 (TeX Live 2024) (preloaded format=pdflatex)
entering extended mode
(./main.tex
LaTeX2e <2024-06-01> patch level 2
(/usr/share/texlive/texmf-dist/tex/latex/base/article.cls
Document Class: article 2024/02/08 v1.4n Standard LaTeX document class)
Package natbib Info: Citation `knuth' is defined on input line 7.
Package hyperref Info: Option `colorlinks' set `true' on input line 3.
LaTeX Info: Redefining \\cite on input line 8.
LaTeX Font Info:    Checking defaults for OT1/cmr/m/n on input line 9.
(./sections/body.tex)
Output written on main.pdf (1 page, 12345 bytes).
Transcript written on main.log.
"""

    @clean_case(
        "BLD001",
        "BLD002",
        "BLD003",
        "BLD004",
        "BLD005",
        "BLD006",
        "BLD007",
        "BLD008",
        "BLD009",
        "BLD011",
        "BLD012",
        "BLD013",
        "BLD014",
    )
    def test_an_ordinary_log_raises_none_of_the_log_checks(self) -> None:
        self.assertEqual(log_findings(self.CLEAN_LOG, "main.tex"), [])
        self.assertEqual(len(log_findings("Missing character: There is no x\n", "main.tex")), 1)

    @clean_case("BLD015")
    def test_a_bibliography_log_without_warnings_raises_no_note(self) -> None:
        bibtex = (
            "This is BibTeX, Version 0.99e (TeX Live 2026)\n"
            "The top-level auxiliary file: main.aux\n"
            "The style file: plain.bst\n"
            "Database file #1: refs.bib\n"
        )
        biber = (
            "[0] Config.pm:308> INFO - This is Biber 2.22\n"
            "[57] Biber.pm:4140> INFO - Found 3 citekeys in bib section 0\n"
            "[90] Biber.pm:131> INFO - Output to main.bbl\n"
        )
        self.assertEqual(bibliography_log_findings(bibtex, "main.tex"), [])
        self.assertEqual(bibliography_log_findings(biber, "main.tex"), [])
        warned = bibtex + "Warning--empty journal in knuth1984\n"
        self.assertEqual(len(bibliography_log_findings(warned, "main.tex")), 1)


class BuildCleanCases(AsyncProjectTestCase):
    """Builds, with a faked latexmk, that raise no finding of the build checks."""

    async def build(
        self, runner: FakeBuildRunner, main: str = "main.tex", work: str = "build"
    ) -> BuildResult:
        """Build ``main`` of the project in ``source`` with ``runner`` into ``work``."""
        return await build_project(self.root / "source", main, self.root / work, "pdflatex", runner)

    @clean_case("BLD010")
    async def test_an_input_from_a_declared_toolchain_root_is_not_an_external_dependency(
        self,
    ) -> None:
        self.write("source/main.tex", "source content")
        toolchain = self.write("toolchain/article.cls", "\\ProvidesClass{article}\n").parent
        trace = (
            f"INPUT {self.root / 'build/project/main.tex'}\nINPUT {toolchain / 'article.cls'}\n"
            f"OUTPUT {self.root / 'build/output/main.pdf'}\n"
        )
        result = await self.build(FakeBuildRunner(trace=trace, resource_roots=[toolchain]))
        self.assertNotIn("build.external_dependency", rules_of(result.findings))
        # The same trace without the declared root is what the check reports.
        result = await self.build(FakeBuildRunner(trace=trace), work="undeclared")
        self.assertIn("build.external_dependency", rules_of(result.findings))

    @clean_case("BLD107")
    async def test_a_latexmkrc_outside_the_folders_of_the_main_file_is_not_reported(self) -> None:
        self.write("source/paper/main.tex", "source content")
        self.write("source/notes/latexmkrc", "$pdf_mode = 5;\n")
        result = await self.build(FakeBuildRunner(), main="paper/main.tex")
        self.assertNotIn("build.latexmk_configuration", rules_of(result.findings))
        # The same file in the folder of the main file is what the check reports.
        self.write("source/paper/latexmkrc", "$pdf_mode = 5;\n")
        result = await self.build(FakeBuildRunner(), main="paper/main.tex", work="configured")
        self.assertIn("build.latexmk_configuration", rules_of(result.findings))


class SubmissionReadinessCleanCases(ProjectTestCase):
    """Source trees that the submission-readiness advisories accept."""

    @clean_case("TEX011")
    def test_an_eps_figure_beside_a_pdf_of_the_same_name_is_not_included(self) -> None:
        self.write(
            "main.tex",
            DOCUMENT + "\\usepackage{graphicx}\n\\begin{document}\n"
            "\\includegraphics{figs/plot}\n\\end{document}\n",
        )
        self.write("figs/plot.eps", "%!PS-Adobe-3.0 EPSF-3.0\n")
        self.write("figs/plot.pdf", b"%PDF-1.5\n")
        findings = check_submission_readiness(self.root, "main.tex", BblOptions())
        self.assertNotIn("source.eps_figures", rules_of(findings))
        # Without the PDF, the search finds the EPS figure, which the check reports.
        (self.root / "figs/plot.pdf").unlink()
        findings = check_submission_readiness(self.root, "main.tex", BblOptions())
        self.assertIn("source.eps_figures", rules_of(findings))


class JobCleanCases(AsyncProjectTestCase):
    """Jobs, with backends that start no tool, that raise no finding of the workflow checks."""

    @clean_case("PKG303")
    async def test_an_archive_within_the_import_limit_is_released(self) -> None:
        sectioned_project(self.root / "input")
        result = await run_job(
            JobRequest(
                "prepare", self.root / "input", Settings(layout="flat"), self.root / "output"
            ),
            backends=controlled_backends(),
        )
        self.assertNotIn("package.archive_limit", rules_of(result.findings))
        self.assertTrue((self.root / "output").exists(), result.findings)

    @clean_case("TEX324")
    async def test_a_document_body_that_the_main_file_reads_leaves_one_main_file(self) -> None:
        self.write("input/main.tex", DOCUMENT + "\\input{body}\n")
        self.write("input/body.tex", "\\begin{document}\nText.\n\\end{document}\n")
        request = JobRequest("inspect", self.root / "input", Settings())
        result = await run_job(request, backends=controlled_backends())
        self.assertNotIn("project.main_selection", rules_of(result.findings))
        # A second file that declares a document class is another candidate, which is reported.
        self.write("input/other.tex", DOCUMENT)
        result = await run_job(request, backends=controlled_backends())
        self.assertIn("project.main_selection", rules_of(result.findings))


if __name__ == "__main__":
    unittest.main()
