"""How the source graph finds a file: graphics paths, TeX lookup order and import contexts."""

from __future__ import annotations

import unittest

from latexprep.findings import has_blockers
from latexprep.flat_layout import plan_flatten
from latexprep.source_analysis import analyze_sources
from tests.support import ProjectTestCase


class SourceSearchPathTests(ProjectTestCase):
    """Finding files through graphics paths, the TeX lookup order and import contexts."""

    def assert_usable(self, plan) -> None:
        """Assert that the flattening ``plan`` is usable.

        The plan must hold no blocking finding, and no two of its target names may differ only in
        letter case; a failure lists the error findings.
        """
        self.assertFalse(
            has_blockers(plan.findings),
            [finding for finding in plan.findings if finding.severity == "error"],
        )
        self.assertEqual(
            len(plan.mapping), len({value.casefold() for value in plan.mapping.values()})
        )

    def test_graphicspath_is_applied_in_input_execution_order(self) -> None:
        self.write("main.tex", "\\documentclass{article}\n\\input{setup}\n\\input{sections/body}")
        self.write("setup.tex", "\\graphicspath{{images/}{plots/}}")
        self.write("sections/body.tex", "\\includegraphics{result}")
        self.write("plots/result.png", b"image")
        plan = plan_flatten(self.root, "main.tex")
        self.assert_usable(plan)
        self.assertEqual(plan.contents["sections/body.tex"], b"\\includegraphics{result.png}")
        self.assertEqual(plan.contents["setup.tex"], b"\\graphicspath{{./}}")

    def test_searchpath_declaration_after_use_does_not_resolve_earlier_image(self) -> None:
        self.write(
            "main.tex",
            "\\documentclass{article}\n\\includegraphics{result}\n\\graphicspath{{images/}}",
        )
        self.write("images/result.pdf", b"image")
        self.assertIn(
            "source.missing_dependency", {item.rule for item in analyze_sources(self.root).findings}
        )

    def test_compilation_cwd_is_not_included_file_parent(self) -> None:
        self.write("paper/main.tex", "\\documentclass{article}\n\\input{sections/intro}")
        self.write("paper/sections/intro.tex", "\\input{shared}\n\\includegraphics{../images/plot}")
        self.write("paper/shared.tex", "Correct input.")
        self.write("paper/sections/shared.tex", "Incorrect input.")
        self.write("images/plot.pdf", b"image")
        analysis = analyze_sources(self.root)
        self.assertIn("paper/shared.tex", analysis.dependencies)
        self.assertNotIn("paper/sections/shared.tex", analysis.dependencies)
        plan = plan_flatten(self.root, "paper/main.tex")
        self.assert_usable(plan)
        self.assertEqual(plan.mapping["paper/main.tex"], "main.tex")
        self.assertIn(
            f"\\input{{{plan.mapping['paper/shared.tex']}}}".encode(),
            plan.contents["paper/sections/intro.tex"],
        )

    def test_extensionless_graphics_with_multiple_extensions_are_ambiguous(self) -> None:
        self.write("main.tex", "\\documentclass{article}\n\\includegraphics{fig/result}")
        self.write("fig/result.pdf", b"pdf")
        self.write("fig/result.png", b"png")
        plan = plan_flatten(self.root, "main.tex")
        self.assert_usable(plan)
        finding = next(item for item in plan.findings if item.rule == "source.ambiguous_dependency")
        self.assertEqual(finding.code, "TEX008")
        self.assertEqual((finding.severity, finding.status), ("warning", "passed"))
        self.assertEqual(finding.details["selected"], "fig/result.pdf")
        self.assertEqual(finding.details["alternatives"], ["fig/result.png"])
        self.assertEqual(finding.details["resolved_by"], "TeX lookup order")
        # The rewritten reference names the file the lookup order selects.
        self.assertIn(b"\\includegraphics{result.pdf}", plan.contents["main.tex"])
        self.assertIn("fig/result.pdf", analyze_sources(self.root, "main.tex").dependencies)
        self.assertNotIn("fig/result.png", analyze_sources(self.root, "main.tex").dependencies)

    def test_searchpath_ambiguity_uses_tex_lookup_order(self) -> None:
        self.write(
            "main.tex",
            "\\documentclass{article}\n\\graphicspath{{a/}{b/}}\n\\includegraphics{result.pdf}",
        )
        self.write("a/result.pdf", b"a")
        self.write("b/result.pdf", b"b")
        plan = plan_flatten(self.root, "main.tex")
        self.assert_usable(plan)
        finding = next(item for item in plan.findings if item.rule == "source.ambiguous_dependency")
        self.assertEqual(finding.details["selected"], "a/result.pdf")
        self.assertEqual(finding.details["alternatives"], ["b/result.pdf"])
        self.assertIn(b"\\includegraphics{a-result.pdf}", plan.contents["main.tex"])

    def test_tex_lookup_order_prefers_the_build_directory_and_default_suffix(self) -> None:
        self.write(
            "main.tex",
            "\\documentclass{article}\n\\graphicspath{{images/}}\n"
            "\\includegraphics{plot.pdf}\n\\input{body}\n",
        )
        self.write("plot.pdf", b"cwd")
        self.write("images/plot.pdf", b"searched")
        self.write("body.tex", "Body.\n")
        self.write("body", "Not a TeX file.\n")
        plan = plan_flatten(self.root, "main.tex")
        self.assert_usable(plan)
        rewritten = plan.contents["main.tex"].decode()
        self.assertIn("\\includegraphics{plot.pdf}", rewritten)
        self.assertIn("\\input{body.tex}", rewritten)
        self.assertEqual(
            {
                item.details["reference"]: item.details["selected"]
                for item in plan.findings
                if item.rule == "source.ambiguous_dependency"
            },
            {"plot.pdf": "plot.pdf", "body": "body.tex"},
        )

    def test_repeated_input_in_different_search_contexts_is_blocked(self) -> None:
        self.write(
            "main.tex",
            "\\documentclass{article}\n\\graphicspath{{a/}}\n\\input{body}\n\\graphicspath{{b/}}\n\\input{body}",
        )
        self.write("body.tex", "\\includegraphics{result.pdf}")
        self.write("a/result.pdf", b"a")
        self.write("b/result.pdf", b"b")
        plan = plan_flatten(self.root, "main.tex")
        self.assertIn("source.context_ambiguity", {item.rule for item in plan.findings})

    def test_deferred_and_conditional_execution_cannot_select_the_wrong_image(self) -> None:
        for body in (
            "\\graphicspath{{a/}}\n\\newcommand{\\picture}{\\includegraphics{x}}\n"
            "\\graphicspath{{b/}}\n\\picture",
            "\\graphicspath{{a/}}\n\\ifdefined\\foo\n\\graphicspath{{b/}}\n"
            "\\fi\n\\includegraphics{x}",
        ):
            with self.subTest(body=body):
                self.write("main.tex", "\\documentclass{article}\n" + body)
                self.write("a/x.pdf", b"a")
                self.write("b/x.pdf", b"b")
                self.assertTrue(has_blockers(plan_flatten(self.root, "main.tex").findings))

    def test_graphicspath_without_trailing_separator_is_not_reinterpreted(self) -> None:
        self.write(
            "main.tex", "\\documentclass{article}\n\\graphicspath{{images}}\n\\includegraphics{x}"
        )
        self.write("images/x.pdf", b"image")
        self.assertTrue(has_blockers(plan_flatten(self.root, "main.tex").findings))

    def test_subfiles_parent_path_option_is_explicitly_unsupported(self) -> None:
        self.write("chapter.tex", "\\documentclass[../main.tex]{subfiles}\nText")
        result = plan_flatten(self.root, "chapter.tex")
        self.assertIn("source.path_option", {item.rule for item in result.findings})
        self.assertTrue(has_blockers(result.findings))

    def test_deep_input_chains_stop_before_python_recursion_limit(self) -> None:
        self.write("main.tex", "\\documentclass{article}\n\\input{part0}")
        for index in range(130):
            self.write(f"part{index}.tex", "\\input{part" + str(index + 1) + "}")
        self.write("part130.tex", "End")
        result = plan_flatten(self.root, "main.tex")
        self.assertIn("source.input_depth", {item.rule for item in result.findings})
        self.assertTrue(has_blockers(result.findings))

    def test_import_contexts_resolve_nested_inputs_and_restore_outer_paths(self) -> None:
        self.write(
            "paper/main.tex",
            "\\documentclass{article}\n\\usepackage{import}\n"
            "\\import{chapters/}{body}\n\\input{after}\n",
        )
        self.write("paper/chapters/body.tex", "\\input{inside}\n\\subimport{nested/}{part}\n")
        self.write("paper/chapters/inside.tex", "Inside.\n")
        self.write("paper/chapters/nested/part.tex", "\\includegraphics{image.pdf}\n")
        self.write("paper/chapters/nested/image.pdf", b"image")
        self.write("paper/after.tex", "After.\n")
        analysis = analyze_sources(self.root, "paper/main.tex")
        self.assertEqual(
            analysis.dependencies,
            {
                "paper/main.tex",
                "paper/chapters/body.tex",
                "paper/chapters/inside.tex",
                "paper/chapters/nested/part.tex",
                "paper/chapters/nested/image.pdf",
                "paper/after.tex",
            },
        )
        plan = plan_flatten(self.root, "paper/main.tex")
        self.assert_usable(plan)
        self.assertIn(b"\\import{./}{body.tex}", plan.contents["paper/main.tex"])
        self.assertIn(b"\\subimport{./}{part.tex}", plan.contents["paper/chapters/body.tex"])
        # LaTeX tries the plain filename before the import path.
        self.write("paper/inside.tex", "Shadows the imported input.\n")
        shadowed = plan_flatten(self.root, "paper/main.tex")
        self.assert_usable(shadowed)
        self.assertIn(b"\\input{paper-inside.tex}", shadowed.contents["paper/chapters/body.tex"])
        finding = next(
            item for item in shadowed.findings if item.rule == "source.ambiguous_dependency"
        )
        self.assertEqual(finding.details["selected"], "paper/inside.tex")

    def test_subfiles_bibliography_path_and_legacy_mode(self) -> None:
        main = "\\documentclass{article}\n\\usepackage{subfiles}\n\\subfile{parts/section}\n"
        self.write("main.tex", main)
        self.write(
            "parts/section.tex",
            "\\documentclass[../main.tex]{subfiles}\n"
            "\\begin{document}\n\\bibliography{references}\n\\end{document}\n",
        )
        self.write("parts/references.bib", "@article{key,title={Title}}")
        analysis = analyze_sources(self.root, "main.tex")
        self.assertIn("parts/references.bib", analysis.dependencies)
        self.assert_usable(plan_flatten(self.root, "main.tex"))
        self.write("main.tex", main.replace("usepackage{subfiles}", "usepackage[v1]{subfiles}"))
        self.assertTrue(has_blockers(plan_flatten(self.root, "main.tex").findings))


if __name__ == "__main__":
    unittest.main()
