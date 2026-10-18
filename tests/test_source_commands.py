"""How file-reading commands and their arguments are recognised, and where recognition stops."""

from __future__ import annotations

import unittest

from latexprep.findings import has_blockers
from latexprep.flat_layout import plan_flatten
from latexprep.source_analysis import analyze_selected_sources, analyze_sources
from tests.support import ProjectTestCase


class SourceCommandTests(ProjectTestCase):
    def assert_usable(self, plan) -> None:
        self.assertFalse(
            has_blockers(plan.findings),
            [finding for finding in plan.findings if finding.severity == "error"],
        )
        self.assertEqual(
            len(plan.mapping), len({value.casefold() for value in plan.mapping.values()})
        )

    def test_dynamic_and_unsupported_commands_are_flat_blockers(self) -> None:
        commands = (
            "\\input{\\mydirectory/chapter}",
            "\\import{\\directory/}{chapter}",
            "\\subimport{a/}{\\chapter}",
            "\\subfile{\\chapter}",
            "\\externaldocument{other/main}",
            "\\pgfplotstableread{\\tablefile}",
            "\\customreader{data/table.csv}",
            "\\IfFileExists{missing.tex}{}{}",
            "\\setmainfont[Path=fonts/,BoldFont=local-bold.otf]{local.otf}",
        )
        for command in commands:
            with self.subTest(command=command):
                self.write("main.tex", "\\documentclass{article}\n" + command)
                plan = plan_flatten(self.root, "main.tex")
                self.assertTrue(has_blockers(plan.findings))
                self.assertTrue(
                    any(
                        item.rule == "flatten.unsupported_source" and item.line == 2
                        for item in plan.findings
                    )
                )

    def test_path_macros_in_local_styles_are_not_silently_supported(self) -> None:
        self.write("main.tex", "\\documentclass{article}\n\\usepackage{styles/helper}")
        self.write(
            "styles/helper.sty", "\\ProvidesPackage{helper}\n\\customdatareader{data/table.csv}"
        )
        plan = plan_flatten(self.root, "main.tex")
        self.assertTrue(
            any(
                item.rule == "flatten.unsupported_source" and item.path == "styles/helper.sty"
                for item in plan.findings
            )
        )

    def test_literal_examples_comments_and_path_prose_are_not_dependencies(self) -> None:
        self.write(
            "main.tex",
            "\\documentclass{article}\n% TODO \\input{absent}\n\\verb|\\input{absent}|\n\\begin{minted}{latex}\n\\input{absent}\n\\end{minted}\n\\texttt{data/absent.csv}\n\\url{https://example.test/a.pdf}\n",
        )
        plan = plan_flatten(self.root, "main.tex")
        self.assert_usable(plan)
        self.assertEqual(plan.contents, {})
        self.assertEqual(plan.findings, [])

    def test_markers_labels_and_refs_are_heuristics_not_blockers(self) -> None:
        self.write(
            "main.tex",
            "\\documentclass{article}\n\\label{x}\n\\input{body}\n"
            "TODO \\todo{Fix me} \\ref{missing}\n"
            "% FIXME \\label{x}\n\\verb|XXX \\label{x}|",
        )
        self.write("body.tex", "\\label{x}")
        result = analyze_sources(self.root)
        rules = [item.rule for item in result.findings]
        self.assertEqual(rules.count("source.duplicate_label"), 2)
        self.assertEqual(rules.count("source.edit_marker"), 1)
        self.assertIn("source.edit_command", rules)
        self.assertIn("source.unresolved_reference", rules)
        self.assertEqual(
            {item.code for item in result.findings}, {"TEX001", "TEX002", "TEX003", "TEX004"}
        )
        self.assertTrue(all(item.evidence_kind == "heuristic" for item in result.findings))
        self.assertFalse(has_blockers(result.findings))
        self.write("body.tex", "\\label{body}")
        self.write("main.tex", "\\documentclass{article}\\input{body}\\ref{body}")
        self.assertEqual(analyze_sources(self.root).findings, [])

    def test_comments_between_command_and_argument_are_preserved(self) -> None:
        self.write(
            "main.tex", "\\documentclass{article}\n\\input% keep this comment\n{sections/body}\n"
        )
        self.write("sections/body.tex", "Text")
        plan = plan_flatten(self.root, "main.tex")
        self.assert_usable(plan)
        self.assertIn(b"\\input% keep this comment\n{body.tex}", plan.contents["main.tex"])

    def test_comments_inside_paths_block_instead_of_being_deleted(self) -> None:
        self.write("main.tex", "\\documentclass{article}\n\\input{sections/%keep\nbody}")
        self.write("sections/body.tex", "Text")
        plan = plan_flatten(self.root, "main.tex")
        self.assertTrue(has_blockers(plan.findings))
        self.assertNotIn("main.tex", plan.contents)

    def test_unbraced_input_has_a_bounded_filename_token(self) -> None:
        self.write("main.tex", "\\documentclass{article}\n\\input sections/body\nText")
        self.write("sections/body.tex", "Body")
        plan = plan_flatten(self.root, "main.tex")
        self.assert_usable(plan)
        self.assertIn(b"\\input body.tex\nText", plan.contents["main.tex"])

    def test_custom_extensionless_file_argument_is_not_ignored(self) -> None:
        self.write("main.tex", "\\documentclass{article}\n\\customreader{data/table}")
        self.write("data/table", "data")
        self.assertTrue(has_blockers(plan_flatten(self.root, "main.tex").findings))

    def test_plain_fraction_is_not_misidentified_as_custom_path(self) -> None:
        self.write("main.tex", "\\documentclass{article}\n$\\frac{a/b}{c}$")
        self.assert_usable(plan_flatten(self.root, "main.tex"))

    def test_unmatched_bracket_does_not_hide_the_rest_of_a_file(self) -> None:
        self.write(
            "main.tex",
            "\\documentclass{article}\nLet $t \\in [0, 1)$.\n\\input{sections/body}\n",
        )
        self.write("sections/body.tex", "Body.\n")
        for analyze in (analyze_sources, analyze_selected_sources):
            with self.subTest(analyze=analyze.__name__):
                result = analyze(self.root, "main.tex")
                self.assertTrue(result.complete, result.findings)
                self.assertEqual(result.dependencies, {"main.tex", "sections/body.tex"})

    def test_argument_collection_stops_at_a_paragraph_break(self) -> None:
        self.write("data/table.csv", "a,b\n1,2\n")
        for separator, expected in (("\n", True), ("\n\n", False)):
            with self.subTest(separator=separator):
                self.write(
                    "main.tex",
                    "\\documentclass{article}\n\\mymacro{x}" + separator + "{data/table.csv}\n",
                )
                rules = {item.rule for item in analyze_sources(self.root, "main.tex").findings}
                self.assertEqual("source.custom_path" in rules, expected)

    def test_biblatex_bbl_version_guard_is_not_a_file_reference(self) -> None:
        self.write(
            "paper/main.tex",
            "\\documentclass{article}\n\\usepackage{biblatex}\n\\addbibresource{refs.bib}\n"
            "\\begin{document}\\cite{a}\\printbibliography\\end{document}\n",
        )
        self.write("paper/refs.bib", "@misc{a,title={A}}\n")
        # The guard biber writes at the top of every biblatex .bbl.
        self.write(
            "paper/main.bbl",
            "\\begingroup\n\\makeatletter\n\\@ifundefined{ver@biblatex.sty}\n"
            "  {\\@latex@error{Missing 'biblatex' package}{}\\aftergroup\\endinput}\n"
            "  {}\n\\endgroup\n",
        )
        rules = {item.rule for item in analyze_sources(self.root, "paper/main.tex").findings}
        self.assertNotIn("source.custom_path", rules)
        plan = plan_flatten(self.root, "paper/main.tex")
        self.assert_usable(plan)
        self.assertEqual(plan.mapping["paper/main.bbl"], "main.bbl")
        self.write("paper/main.tex", "\\documentclass{article}\n\\mymacro{refs.bib}\n")
        rules = {item.rule for item in analyze_sources(self.root, "paper/main.tex").findings}
        self.assertIn("source.custom_path", rules)

    def test_conditional_uncertainty_covers_only_enclosed_project_references(self) -> None:
        self.write(
            "local/helper.sty",
            "\\ProvidesPackage{helper}\n\\if@twocolumn\\RequirePackage{multicol}\\fi\n",
        )
        self.write("figure.pdf", b"image")
        preamble = "\\documentclass{article}\n\\newif\\ifanonymous\n\\usepackage{local/helper}\n"
        self.write("main.tex", preamble + "\\includegraphics{figure.pdf}\n")
        self.assert_usable(plan_flatten(self.root, "main.tex"))
        self.write("main.tex", preamble + "\\ifanonymous\n\\includegraphics{figure.pdf}\n\\fi\n")
        findings = [
            item
            for item in analyze_sources(self.root, "main.tex").findings
            if item.rule == "source.conditional_dependency"
        ]
        self.assertEqual([(item.path, item.line) for item in findings], [("main.tex", 5)])
        self.assertTrue(findings[0].suggestion.startswith("Make the dependency unconditional"))
        self.assertTrue(has_blockers(plan_flatten(self.root, "main.tex").findings))

    def test_formatting_commands_and_template_internals_are_not_advisories(self) -> None:
        self.write(
            "main.tex",
            "\\documentclass{article}\n\\usepackage{local/template}\n"
            "\\textcolor{red}{Result} \\color{blue}\n\\label{intro}\n",
        )
        self.write(
            "local/template.sty",
            "\\ProvidesPackage{template}\n% TODO internal note\n\\hl{draft}\n\\label{intro}\n",
        )
        self.assertEqual(analyze_sources(self.root, "main.tex").findings, [])

    def test_unclosed_and_oversized_arguments_are_bounded_and_explicit(self) -> None:
        for argument in ("unclosed", "x" * (64 * 1024 + 1) + "}"):
            with self.subTest(length=len(argument)):
                self.write("main.tex", "\\documentclass{article}\n\\input{" + argument)
                plan = plan_flatten(self.root, "main.tex")
                self.assertTrue(has_blockers(plan.findings))
                self.assertIn("source.argument_limit", {item.rule for item in plan.findings})

    def test_tikz_path_commands_are_not_taken_for_literal_text(self) -> None:
        self.write(
            "main.tex",
            "\\documentclass{article}\n\\begin{document}\n\\begin{tikzpicture}\n"
            "\\path[draw] (0,0)\n  -- (1,1);\n\\end{tikzpicture}\n\\input{body}\n"
            "\\end{document}\n",
        )
        self.write("body.tex", "Body.\n")
        analysis = analyze_sources(self.root, "main.tex")
        self.assertNotIn("source.literal_unclosed", {item.rule for item in analysis.findings})
        self.assertTrue(analysis.complete, analysis.findings)
        self.assert_usable(plan_flatten(self.root, "main.tex"))

    def test_comment_between_a_command_and_its_argument_does_not_hide_the_argument(self) -> None:
        self.write("main.tex", "\\documentclass{article}\n\\input\n% see the next file\n{body}\n")
        self.write("body.tex", "Body.\n")
        selected = analyze_selected_sources(self.root, "main.tex")
        self.assertTrue(selected.complete, selected.findings)
        self.assertEqual(selected.dependencies, {"main.tex", "body.tex"})
        self.assert_usable(plan_flatten(self.root, "main.tex"))


if __name__ == "__main__":
    unittest.main()
