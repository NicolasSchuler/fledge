"""Source analysis: root discovery, selected analyses, graph completeness and error messages."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from latexprep.findings import has_blockers
from latexprep.flat_layout import plan_flatten
from latexprep.source_analysis import analyze_selected_sources, analyze_sources, discover_roots
from latexprep.source_graph import SourceGraph
from latexprep.tex_lexer import MAX_ARGUMENT_CHARS, mask_literals
from tests.support import ProjectTestCase


class SourceAnalysisTests(ProjectTestCase):
    """Analysing sources: roots, selected closures, dependencies, limits and error messages."""

    def test_roots_ignore_comments_and_literal_examples(self) -> None:
        self.write(
            "draft.tex",
            "% \\documentclass{article}\n\\verb|\\documentclass{book}|\n"
            "\\begin{verbatim}\n\\documentclass{book}\n\\end{verbatim}",
        )
        self.write("paper/main.tex", "\\documentclass{article}\n\\begin{document}Hi\\end{document}")
        self.assertEqual(discover_roots(self.root), ["paper/main.tex"])
        self.assertEqual(analyze_sources(self.root).main, "paper/main.tex")

    def test_a_body_the_main_file_inputs_is_not_a_second_root(self) -> None:
        # From the public reproduction ``body-input``: main.tex holds the preamble and
        # reads body.tex, which opens and closes the document environment.
        self.write("paper/main.tex", "\\documentclass{article}\n\\input{body}\n")
        self.write("paper/body.tex", "\\begin{document}\nText.\n\\end{document}\n")
        self.assertEqual(discover_roots(self.root), ["paper/main.tex"])
        graph = SourceGraph.build(self.root, None)
        self.assertEqual((graph.main, graph.roots), ("paper/main.tex", ["paper/main.tex"]))
        self.assertEqual(SourceGraph.build(self.root, "paper/main.tex").other_roots(), [])

    def test_a_header_the_main_file_inputs_is_not_the_root(self) -> None:
        self.write("main.tex", "\\input{header.tex}\n\\begin{document}\nText.\n\\end{document}\n")
        self.write("header.tex", "\\documentclass{article}\n")
        self.assertEqual(discover_roots(self.root), ["main.tex"])

    def test_documents_that_do_not_read_each_other_stay_separate_roots(self) -> None:
        self.write("paper.tex", "\\documentclass{article}\n\\input{body}\n")
        self.write("body.tex", "\\begin{document}\nText.\n\\end{document}\n")
        self.write("supplement.tex", "\\documentclass{article}\n\\input{other/body}\n")
        self.write("response.tex", "\\begin{document}\nA reply.\n\\end{document}\n")
        self.assertEqual(discover_roots(self.root), ["paper.tex", "response.tex", "supplement.tex"])
        # Two candidates that read each other leave the choice to the user.
        self.write("a.tex", "\\documentclass{article}\n\\input{b}\n")
        self.write("b.tex", "\\documentclass{article}\n\\input{a}\n")
        for name in ("paper.tex", "body.tex", "supplement.tex", "response.tex"):
            (self.root / name).unlink()
        self.assertEqual(discover_roots(self.root), ["a.tex", "b.tex"])

    def test_a_document_read_only_by_an_input_that_may_not_run_stays_a_root(self) -> None:
        # Review: \iffalse\input{supplement}\fi merged a second document into the paper,
        # which was then blamed for the supplement's packages.
        supplement = "\\documentclass{article}\n\\begin{document}\nMore.\n\\end{document}\n"
        for reading in (
            "\\iffalse\n\\input{supplement}\n\\fi\n",
            "\\newcommand\\withsupplement{\\input{supplement}}\n",
            "\\maybe{\\input{supplement}}\n",
            "% \\input{supplement}\n",
        ):
            with self.subTest(reading=reading):
                self.write(
                    "paper.tex",
                    "\\documentclass{article}\n"
                    + reading
                    + "\\begin{document}\nText.\n\\end{document}\n",
                )
                self.write("supplement.tex", supplement)
                self.assertEqual(discover_roots(self.root), ["paper.tex", "supplement.tex"])
        # An input in a bare group or a typeset argument runs where it stands.
        self.write(
            "paper.tex",
            "\\documentclass{article}\n\\begin{document}\n{\\small\\input{supplement}}\n"
            "\\end{document}\n",
        )
        self.assertEqual(discover_roots(self.root), ["paper.tex"])

    def test_a_main_file_that_is_not_utf8_is_still_found(self) -> None:
        # From the public reproduction ``latin1-label``: one Latin-1 byte in a label.
        self.write(
            "main.tex",
            b"\\documentclass{article}\n\\begin{document}\n\\section{A}\\label{sec:Poincar\xe9}\n"
            b"See \\ref{sec:Poincar\xe9}.\n\\end{document}\n",
        )
        self.assertEqual(discover_roots(self.root), ["main.tex"])
        # The byte stands in front of a brace and nothing rules out a double-byte
        # encoding, in which the brace would be half of a character: the file is the main
        # file, but its text is left unread (TEX301).
        result = analyze_sources(self.root, "main.tex")
        self.assertEqual(result.main, "main.tex")
        self.assertIn("source.encoding", {item.rule for item in result.findings})
        self.assertFalse(result.complete)
        # With a Latin-1 letter in front of a blank the file can only be single-byte text:
        # it is read, its label and reference are found, and TEX328 notes that it is not
        # UTF-8 and keeps the graph from counting as complete, so nothing rewrites it.
        self.write(
            "main.tex",
            b"\\documentclass{article}\n\\begin{document}\n\\section{A}\\label{sec:a}\n"
            b"Henri Poincar\xe9 in \\ref{sec:a}.\n\\end{document}\n",
        )
        result = analyze_sources(self.root, "main.tex")
        rules = {item.rule for item in result.findings}
        self.assertIn("source.foreign_encoding", rules)
        self.assertFalse(rules & {"source.encoding", "source.unresolved_reference"})
        self.assertFalse(result.complete)

    def test_a_file_without_a_document_class_is_not_a_root_in_any_encoding(self) -> None:
        self.write("notes.tex", b"\\section{Poincar\xe9}\nText.\n")
        self.write("chapter.tex", "\\section{Plain}\n")
        self.assertEqual(discover_roots(self.root), [])

    def test_multiple_roots_need_explicit_selection(self) -> None:
        self.write("a.tex", "\\documentclass{article}")
        self.write("b.tex", "\\documentclass{article}")
        result = analyze_sources(self.root)
        self.assertIsNone(result.main)
        self.assertTrue(has_blockers(result.findings))
        self.assertEqual(result.findings[0].details["candidates"], ["a.tex", "b.tex"])
        self.assertEqual(analyze_sources(self.root, "b.tex").main, "b.tex")
        self.assertTrue(has_blockers(plan_flatten(self.root, "b.tex").findings))

    def test_selected_analysis_does_not_read_unrelated_sources_or_spend_their_budget(self) -> None:
        self.write("main.tex", "\\documentclass{article}\n\\input{body}\n")
        self.write("body.tex", "Selected body.\n")
        self.write("a-malformed.tex", "\\input{")
        self.write("b-dynamic.tex", "\\directlua{require('private')}")
        self.write("c-huge.tex", "x" * 129)
        self.write("d-budget.tex", "x" * 110)
        self.write("other.tex", "\\documentclass{book}")
        read_bytes = Path.read_bytes

        def read_selected(path: Path) -> bytes:
            self.assertIn(path.relative_to(self.root).as_posix(), {"main.tex", "body.tex"})
            return read_bytes(path)

        with (
            patch.object(Path, "read_bytes", read_selected),
            patch("latexprep.source_loading.MAX_SOURCE_BYTES", 128),
            patch("latexprep.source_loading.MAX_TOTAL_SOURCE_BYTES", 128),
        ):
            selected = analyze_selected_sources(self.root, "main.tex")
        self.assertTrue(selected.complete, selected.findings)
        self.assertEqual(selected.dependencies, {"main.tex", "body.tex"})
        self.assertEqual(selected.roots, ["main.tex"])
        self.assertEqual(selected.findings, [])

        # Reading every file to discover roots must not attribute their lexical
        # uncertainties to the selected document.
        inspected = analyze_sources(self.root, "main.tex")
        self.assertTrue(inspected.complete, inspected.findings)
        self.assertIn("other.tex", inspected.roots)
        self.assertNotIn("source.argument_limit", {item.rule for item in inspected.findings})
        flattened = plan_flatten(self.root, "main.tex")
        self.assertIn("other.tex", flattened.mapping)
        self.assertIn("flatten.multiple_roots", {item.rule for item in flattened.findings})
        self.assertIn(
            "source.argument_limit",
            {item.details.get("source_rule") for item in flattened.findings},
        )

    def test_selected_analysis_keeps_transitive_local_inputs_and_resources(self) -> None:
        sources = {
            "main.tex": "\\documentclass{styles/custom}\n\\bibliography{bib/references}\n"
            "\\bibliographystyle{styles/custom}\n",
            "styles/custom.cls": "\\ProvidesClass{custom}\n\\LoadClass{article}\n"
            "\\RequirePackage{styles/helper}\n",
            "styles/helper.sty": "\\ProvidesPackage{helper}\n\\input{parts/body}\n",
            "parts/body.tex": "\\newcommand{\\titleword}{Plain}\n"
            "\\pgfplotstableread{data/values.csv}\\datatable\n"
            "\\includegraphics{figures/plot.pdf}\n"
            "\\setmainfont[Path={fonts/}]{local.otf}\n",
            "data/values.csv": "a,b\n1,2\n",
            "figures/plot.pdf": "image",
            "fonts/local.otf": "font",
            "bib/references.bib": "@misc{reference,title={Reference}}",
            "styles/custom.bst": "ENTRY {}{}{}",
        }
        for name, contents in sources.items():
            self.write(name, contents)
        self.write("unrelated.tex", "\\documentclass{book}")
        selected = analyze_selected_sources(self.root, "main.tex")
        self.assertTrue(selected.complete, selected.findings)
        self.assertEqual(selected.dependencies, set(sources))
        self.assertEqual(selected.roots, ["main.tex"])

    def test_selected_analysis_keeps_subfile_roots_in_its_loaded_closure(self) -> None:
        self.write(
            "main.tex",
            "\\documentclass{article}\n\\usepackage{subfiles}\n\\subfile{parts/body}\n",
        )
        self.write(
            "parts/body.tex",
            "\\documentclass[../main.tex]{subfiles}\n"
            "\\begin{document}\n\\input{details}\n\\end{document}\n",
        )
        self.write("parts/details.tex", "Details.\n")
        self.write("unrelated.tex", "\\documentclass{book}")
        selected = analyze_selected_sources(self.root, "main.tex")
        self.assertTrue(selected.complete, selected.findings)
        self.assertEqual(selected.roots, ["main.tex", "parts/body.tex"])
        self.assertEqual(selected.dependencies, {"main.tex", "parts/body.tex", "parts/details.tex"})

    def test_selected_analysis_reports_unsupported_or_unresolved_dependencies_as_incomplete(
        self,
    ) -> None:
        self.write("data/table.csv", "data")
        for command, rule in (
            (r"\input{\chosen}", "source.dynamic_path"),
            (r"\input{missing}", "source.missing_dependency"),
            (r"\customreader{data/table.csv}", "source.custom_path"),
            (r"\IfFileExists{data/table.csv}{}{}", "source.unsupported_command"),
            (r"\directlua{require('reader')}", "source.unsupported_command"),
            (r"\luadirect{require('reader')}", "source.unsupported_command"),
            (r"\luaexec{require('reader')}", "source.unsupported_command"),
        ):
            with self.subTest(command=command):
                self.write("main.tex", "\\documentclass{article}\n" + command)
                selected = analyze_selected_sources(self.root, "main.tex")
                self.assertFalse(selected.complete)
                self.assertIn(rule, {item.rule for item in selected.findings})

    def test_selected_analysis_requires_readable_sources_and_rejects_linked_dependencies(
        self,
    ) -> None:
        self.write("main.tex", "\\documentclass{article}\n\\input{body}\n")
        for body, rule in (
            (b"caf\xe9", "source.foreign_encoding"),
            (b"caf\x00\xe9", "source.encoding"),
            (b"\\input{", "source.argument_limit"),
        ):
            with self.subTest(rule=rule):
                self.write("body.tex", body)
                selected = analyze_selected_sources(self.root, "main.tex")
                self.assertFalse(selected.complete)
                self.assertIn(rule, {item.rule for item in selected.findings})
        self.write("main.tex", "\\documentclass{article}\n\\usepackage{helper}\n")
        self.write("actual.sty", "\\ProvidesPackage{helper}")
        (self.root / "helper.sty").symlink_to("actual.sty")
        selected = analyze_selected_sources(self.root, "main.tex")
        self.assertFalse(selected.complete)
        self.assertIn("source.link", {item.rule for item in selected.findings})
        missing = analyze_selected_sources(self.root, "missing.tex")
        self.assertFalse(missing.complete)
        self.assertIsNone(missing.main)

    def test_source_advisories_do_not_make_literal_dependencies_incomplete(self) -> None:
        self.write("main.tex", "\\documentclass{article}\nTODO \\ref{unresolved}\n")
        for analyze in (analyze_sources, analyze_selected_sources):
            with self.subTest(analyze=analyze.__name__):
                result = analyze(self.root, "main.tex")
                self.assertTrue(result.complete, result.findings)
                self.assertEqual(
                    {item.rule for item in result.findings},
                    {"source.edit_marker", "source.unresolved_reference"},
                )

    def test_labels_of_separate_documents_are_not_compared_without_a_main_file(self) -> None:
        paper = "\\documentclass{article}\n\\section{A}\\label{sec:a}\nTODO \\ref{sec:a}\n"
        self.write("paper.tex", paper)
        self.write("paper-diff.tex", paper + "\\ref{sec:only-in-the-paper}\n")
        rules = [item.rule for item in analyze_sources(self.root).findings]
        self.assertNotIn("source.duplicate_label", rules)
        self.assertNotIn("source.unresolved_reference", rules)
        # Editing markers do not depend on the document, so they are still reported.
        self.assertEqual(rules.count("source.edit_marker"), 2)
        selected = {item.rule for item in analyze_sources(self.root, "paper-diff.tex").findings}
        self.assertIn("source.unresolved_reference", selected)

    def test_every_reference_command_resolves_its_labels(self) -> None:
        # cleveref ranges and lists, cpageref, subcaption and zref-clever refer to labels too.
        references = (
            r"\crefrange{a}{b}\Crefrange{a}{b}\labelcref{a,b}\cpageref{a,b}\Cpageref{b}"
            r"\subref{a}\zcref[S]{a,b}\ref{a}"
        )
        self.write(
            "main.tex", "\\documentclass{article}\n\\label{a}\\label{b}\n" + references + "\n"
        )
        self.assertEqual(analyze_sources(self.root, "main.tex").findings, [])
        self.write("main.tex", "\\documentclass{article}\n\\label{a}\n" + references + "\n")
        missing = [
            item.line
            for item in analyze_sources(self.root, "main.tex").findings
            if item.rule == "source.unresolved_reference"
        ]
        # b is missing from \crefrange, \Crefrange, \labelcref, \cpageref, \Cpageref, \zcref.
        self.assertEqual(missing, [3] * 6)

    def test_all_literals_keep_offsets_and_escaped_percent_is_text(self) -> None:
        source = (
            "% TODO \\input{bad}\n"
            "\\verb|TODO \\input{bad}| \\lstinline[language=TeX]!\\input{bad}!\n"
            "\\mintinline{tex}{\\input{bad}}\n"
            "\\begin{lstlisting}\n\\input{bad}\n\\end{lstlisting}\n"
            "\\begin{minted}{tex}\nTODO \\input{bad}\n\\end{minted}\n"
            "\\begin% comment\n{verbatim}\n\\input{bad}\n\\end{verbatim}\n"
            "\\url{https://example.test/a%20b/\\input{bad}}\n"
            "Text \\% TODO\n"
        )
        masked = mask_literals(source)
        self.assertEqual(len(masked), len(source))
        self.assertEqual(
            [offset for offset, char in enumerate(source) if char == "\n"],
            [offset for offset, char in enumerate(masked) if char == "\n"],
        )
        self.assertNotIn("\\input", masked)
        self.assertEqual(masked.count("TODO"), 1)

    def test_absolute_outside_and_missing_dependencies_are_errors(self) -> None:
        for reference in ("/tmp/secret", "../secret", "C:/secret", "absent"):
            with self.subTest(reference=reference):
                self.write("main.tex", "\\documentclass{article}\n\\input{" + reference + "}")
                result = analyze_sources(self.root)
                self.assertTrue(has_blockers(result.findings))
                code = "TEX005" if reference == "absent" else "TEX006"
                self.assertIn(code, {item.code for item in result.findings})
        self.write("main.tex", "\\documentclass{article}\\input{present}")
        self.write("present.tex", "Included text.")
        self.assertEqual(analyze_sources(self.root).findings, [])

    def test_case_mismatch_is_reported_and_not_guessed(self) -> None:
        self.write("main.tex", "\\documentclass{article}\n\\input{Body}")
        self.write("body.tex", "Text")
        result = analyze_sources(self.root)
        self.assertIn("source.case_mismatch", {item.rule for item in result.findings})
        self.assertIn("TEX007", {item.code for item in result.findings})

    def test_unsupported_encoding_and_large_files_are_explicit(self) -> None:
        self.write("main.tex", "\\documentclass{article}\n\\input{body}")
        # A file that is not UTF-8 is read but cannot be rewritten; one with NUL bytes is
        # not read at all. Flat layout refuses both.
        for body, rule in (
            (b"caf\xe9", "source.foreign_encoding"),
            (b"\x00\xe9", "source.encoding"),
        ):
            with self.subTest(rule=rule):
                self.write("body.tex", body)
                plan = plan_flatten(self.root, "main.tex")
                self.assertIn(rule, {item.rule for item in plan.findings})
                self.assertTrue(has_blockers(plan.findings))
                self.assertNotIn("body.tex", plan.contents)
        self.write("body.tex", b"x" * (8 * 1024 * 1024 + 1))
        plan = plan_flatten(self.root, "main.tex")
        self.assertIn("source.size_limit", {item.rule for item in plan.findings})
        self.assertTrue(has_blockers(plan.findings))

    def test_invalid_main_does_not_read_external_file(self) -> None:
        self.write("main.tex", "\\documentclass{article}")
        for main in ("../secret.tex", "/tmp/secret.tex", "absent.tex"):
            with self.subTest(main=main):
                result = analyze_sources(self.root, main)
                self.assertIsNone(result.main)
                self.assertTrue(has_blockers(result.findings))

    def finding(self, findings, rule: str):
        """Return the first of ``findings`` whose rule is ``rule``.

        Args:
            findings: The findings to search.
            rule: The rule to look for.

        Returns:
            The first finding with that rule.

        Raises:
            StopIteration: No finding has that rule.
        """
        return next(item for item in findings if item.rule == rule)

    def test_read_failures_and_encoding_failures_are_told_apart_without_local_paths(self) -> None:
        self.write("main.tex", "\\documentclass{article}\n\\input{body}\n")
        # UTF-16: not text that a byte-wise reader finds commands in.
        self.write("body.tex", "café".encode("utf-16"))
        encoding = self.finding(
            analyze_selected_sources(self.root, "main.tex").findings, "source.encoding"
        )
        self.assertIn("not text in an encoding that can be read", encoding.message)
        self.write("body.tex", "Readable once permissions are fixed.\n")
        (self.root / "body.tex").chmod(0)
        self.addCleanup((self.root / "body.tex").chmod, 0o600)
        if os.access(self.root / "body.tex", os.R_OK):
            self.skipTest("file permissions are not enforced for this user")
        unreadable = self.finding(
            analyze_selected_sources(self.root, "main.tex").findings, "source.encoding"
        )
        self.assertIn("could not be read (permission denied)", unreadable.message)
        self.assertNotIn("UTF-8", unreadable.message)
        self.assertNotIn(str(self.root), unreadable.message)

    def test_lexical_bounds_report_their_cause_and_line(self) -> None:
        self.write("main.tex", "\\documentclass{article}\n\\input{body}\n")
        cases = (
            ("Text.\n\\emph{open\n", "source.argument_limit", "never closed", 2),
            (
                "A\n\\emph{" + "x" * (MAX_ARGUMENT_CHARS + 1) + "}\n",
                "source.argument_limit",
                f"longer than {MAX_ARGUMENT_CHARS // 1024} KiB",
                2,
            ),
        )
        for body, rule, cause, line in cases:
            with self.subTest(cause=cause):
                self.write("body.tex", body)
                item = self.finding(analyze_selected_sources(self.root, "main.tex").findings, rule)
                self.assertIn(cause, item.message)
                self.assertEqual((item.path, item.line), ("body.tex", line))
                self.assertTrue(item.suggestion)
        self.write("body.tex", "One \\a \\b \\c \\d \\e \\f.\n")
        with (
            patch("latexprep.tex_lexer.MAX_COMMANDS", 5),
            patch("latexprep.source_loading.MAX_COMMANDS", 5),
        ):
            item = self.finding(
                analyze_selected_sources(self.root, "main.tex").findings, "source.command_limit"
            )
        self.assertIn("more than 5 TeX commands", item.message)
        self.assertEqual((item.path, item.line), ("body.tex", 1))

    def test_root_discovery_skips_files_beyond_the_lexical_bounds(self) -> None:
        self.write("big.tex", "\\documentclass{article}\n" + "\\relax\n" * 10)
        self.write("small.tex", "\\documentclass{article}\n")
        with patch("latexprep.tex_lexer.MAX_COMMANDS", 5):
            self.assertEqual(discover_roots(self.root), ["small.tex"])
        self.assertEqual(discover_roots(self.root), ["big.tex", "small.tex"])

    def test_dependency_errors_name_what_was_tried_and_the_next_step(self) -> None:
        self.write("Body.TEX", "x")
        self.write(
            "main.tex",
            "\\documentclass{article}\n\\input{missing}\n\\input{/tmp/outside}\n"
            "\\input{../outside}\n\\input{body.TEX}\n\\includegraphics{figures/nothing}\n",
        )
        findings = analyze_sources(self.root, "main.tex").findings
        by_reference = {
            item.details["reference"]: item
            for item in findings
            if item.rule.startswith("source.") and "reference" in item.details
        }
        missing = by_reference["missing"]
        self.assertEqual(missing.rule, "source.missing_dependency")
        self.assertIn("looked for missing.tex, missing", missing.message)
        self.assertIn("\\input", missing.suggestion)
        graphics = by_reference["figures/nothing"]
        self.assertIn("figures/nothing.pdf", graphics.message)
        self.assertRegex(graphics.message, r"\(\+\d+ more\)\.$")
        self.assertNotRegex(graphics.message, r"and \d+ more")
        for reference in ("/tmp/outside", "../outside"):
            with self.subTest(reference=reference):
                self.assertEqual(by_reference[reference].rule, "source.external_path")
                self.assertIn("Copy the file into the project", by_reference[reference].suggestion)
        mismatch = by_reference["body.TEX"]
        self.assertEqual(mismatch.rule, "source.case_mismatch")
        self.assertIn("Body.TEX", mismatch.suggestion)

    def test_main_file_and_root_selection_errors_list_what_exists(self) -> None:
        self.write("a.tex", "\\documentclass{article}")
        self.write("b.tex", "\\documentclass{article}")
        ambiguous = self.finding(analyze_sources(self.root).findings, "source.root_ambiguous")
        self.assertIn("a.tex, b.tex", ambiguous.message)
        self.assertIn("--main", ambiguous.suggestion)
        absent = self.finding(analyze_sources(self.root, "c.tex").findings, "source.main_invalid")
        self.assertIn("'c.tex' was not found", absent.message)
        self.assertIn("a.tex, b.tex", absent.message)
        self.assertIn("--main", absent.suggestion)
        escaping = self.finding(
            analyze_sources(self.root, "../a.tex").findings, "source.main_invalid"
        )
        self.assertIn("relative to the project folder", escaping.message)

    def test_graph_completeness_and_other_roots_describe_the_walk(self) -> None:
        self.write("main.tex", "\\documentclass{article}\n\\input{body}\n")
        self.write("body.tex", "Body.\n")
        self.write("other.tex", "\\documentclass{article}\n")
        graph = SourceGraph.build(self.root, "main.tex")
        self.assertTrue(graph.is_complete)
        self.assertEqual(graph.other_roots(), ["other.tex"])
        self.write("body.tex", "\\input{\\chosen}\n")
        self.assertFalse(SourceGraph.build(self.root, "main.tex").is_complete)
        self.write("body.tex", "Body.\n")
        self.write("main.tex", "\\documentclass{article}\n\\input{missing}\n")
        self.assertFalse(SourceGraph.build(self.root, "main.tex").is_complete)

    def test_imported_metadata_is_visible_and_subfile_content_is_explicitly_uncertain(self) -> None:
        from latexprep.manuscript import ManuscriptOptions, check_manuscript
        from latexprep.manuscript_check_options import ManuscriptCheckOptions
        from latexprep.manuscript_checks import check_manuscript_policies

        self.write("main.tex", "\\documentclass{article}\n\\import{parts/}{metadata}\n")
        self.write(
            "parts/metadata.tex",
            "\\begin{abstract}A supported abstract.\\end{abstract}\n\\title{Imported title}\n",
        )
        findings = check_manuscript(self.root, "main.tex", ManuscriptOptions(require_abstract=True))
        self.assertEqual(
            next(f for f in findings if f.rule == "manuscript.abstract_required").status, "passed"
        )
        detailed = check_manuscript_policies(
            self.root, "main.tex", ManuscriptCheckOptions(required_metadata=("title",))
        )
        self.assertFalse(has_blockers(detailed))


if __name__ == "__main__":
    unittest.main()
