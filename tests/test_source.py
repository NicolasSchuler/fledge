from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from latexprep.models import has_blockers
from latexprep.source import analyze_sources, discover_roots, mask_literals, plan_flatten


class SourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def write(self, path: str, text: str | bytes) -> None:
        destination = self.root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(text.encode() if isinstance(text, str) else text)

    def assert_usable(self, plan) -> None:
        self.assertFalse(
            has_blockers(plan.findings),
            [finding for finding in plan.findings if finding.severity == "error"],
        )
        self.assertEqual(
            len(plan.mapping), len(set(value.casefold() for value in plan.mapping.values()))
        )

    def test_roots_ignore_comments_and_literal_examples(self) -> None:
        self.write(
            "draft.tex",
            "% \\documentclass{article}\n\\verb|\\documentclass{book}|\n"
            "\\begin{verbatim}\n\\documentclass{book}\n\\end{verbatim}",
        )
        self.write("paper/main.tex", "\\documentclass{article}\n\\begin{document}Hi\\end{document}")
        self.assertEqual(discover_roots(self.root), ["paper/main.tex"])
        self.assertEqual(analyze_sources(self.root).main, "paper/main.tex")

    def test_multiple_roots_need_explicit_selection(self) -> None:
        self.write("a.tex", "\\documentclass{article}")
        self.write("b.tex", "\\documentclass{article}")
        result = analyze_sources(self.root)
        self.assertIsNone(result.main)
        self.assertTrue(has_blockers(result.findings))
        self.assertEqual(result.findings[0].details["candidates"], ["a.tex", "b.tex"])
        self.assertEqual(analyze_sources(self.root, "b.tex").main, "b.tex")
        self.assertTrue(has_blockers(plan_flatten(self.root, "b.tex").findings))

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
            [i for i, c in enumerate(source) if c == "\n"],
            [i for i, c in enumerate(masked) if c == "\n"],
        )
        self.assertNotIn("\\input", masked)
        self.assertEqual(masked.count("TODO"), 1)

    def test_nested_paths_and_collisions_are_rewritten_not_globally_replaced(self) -> None:
        main = (
            "\\documentclass{article}\n"
            "% \\includegraphics{figures/results}\n"
            "\\input{sections/method}\n"
            "\\includegraphics{figures/results}\n"
            "\\includegraphics[width=.5\\textwidth]{appendix/results.pdf}\n"
            "\\bibliography{bib/references}\n"
            "\\bibliographystyle{plain}\n"
            "\\texttt{figures/results.pdf} \\url{https://example.test/figures/results.pdf}\n"
            "\\begin{verbatim}\\input{sections/method}\\end{verbatim}\n"
        )
        self.write("main.tex", main)
        self.write("sections/method.tex", "Method text.\n")
        self.write("figures/results.pdf", b"one")
        self.write("appendix/results.pdf", b"two")
        self.write("bib/references.bib", "@article{key, title={Test}}")
        plan = plan_flatten(self.root, "main.tex")
        self.assert_usable(plan)
        self.assertEqual(plan.mapping["figures/results.pdf"], "figures-results.pdf")
        self.assertEqual(plan.mapping["appendix/results.pdf"], "appendix-results.pdf")
        rewritten = plan.contents["main.tex"].decode()
        self.assertIn("\\input{method.tex}", rewritten)
        self.assertIn("\\includegraphics{figures-results.pdf}", rewritten)
        self.assertIn("\\bibliography{references}", rewritten)
        self.assertIn("% \\includegraphics{figures/results}", rewritten)
        self.assertIn("\\texttt{figures/results.pdf}", rewritten)
        self.assertIn("\\begin{verbatim}\\input{sections/method}\\end{verbatim}", rewritten)
        self.assertEqual((self.root / "main.tex").read_text(), main)

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
            "source-missing-dependency", {item.rule for item in analyze_sources(self.root).findings}
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
        self.assertTrue(has_blockers(plan.findings))
        finding = next(item for item in plan.findings if item.rule == "source-ambiguous-dependency")
        self.assertEqual(finding.code, "TEX008")
        self.assertEqual(finding.details["candidates"], ["fig/result.pdf", "fig/result.png"])

    def test_searchpath_ambiguity_is_blocked(self) -> None:
        self.write(
            "main.tex",
            "\\documentclass{article}\n\\graphicspath{{a/}{b/}}\n\\includegraphics{result.pdf}",
        )
        self.write("a/result.pdf", b"a")
        self.write("b/result.pdf", b"b")
        self.assertTrue(has_blockers(plan_flatten(self.root, "main.tex").findings))

    def test_repeated_input_in_different_search_contexts_is_blocked(self) -> None:
        self.write(
            "main.tex",
            "\\documentclass{article}\n\\graphicspath{{a/}}\n\\input{body}\n\\graphicspath{{b/}}\n\\input{body}",
        )
        self.write("body.tex", "\\includegraphics{result.pdf}")
        self.write("a/result.pdf", b"a")
        self.write("b/result.pdf", b"b")
        plan = plan_flatten(self.root, "main.tex")
        self.assertIn("source-context-ambiguity", {item.rule for item in plan.findings})

    def test_include_and_bibliography_keep_naming_contract(self) -> None:
        self.write(
            "main.tex",
            "\\documentclass{article}\n\\include{sections/chapter}\n"
            "\\bibliography{bib/one, bib/two}\n"
            "\\addbibresource[location=local]{bib/three.bib}\n"
            "\\bibliographystyle{styles/custom}",
        )
        self.write("sections/chapter.tex", "text")
        for name in ("one", "two", "three"):
            self.write(f"bib/{name}.bib", "")
        self.write("styles/custom.bst", "ENTRY {}{}{}")
        plan = plan_flatten(self.root, "main.tex")
        self.assert_usable(plan)
        output = plan.contents["main.tex"].decode()
        self.assertIn("\\include{chapter}", output)
        self.assertIn("\\bibliography{one, two}", output)
        self.assertIn("\\addbibresource[location=local]{three.bib}", output)
        self.assertIn("\\bibliographystyle{custom}", output)

    def test_class_and_package_identities_are_preserved(self) -> None:
        self.write("main.tex", "\\documentclass{styles/custom}\n\\usepackage{styles/helper}")
        self.write("styles/custom.cls", "\\ProvidesClass{custom}\n\\LoadClass{article}")
        self.write("styles/helper.sty", "\\ProvidesPackage{helper}")
        plan = plan_flatten(self.root, "main.tex")
        self.assert_usable(plan)
        self.assertEqual(plan.mapping["styles/custom.cls"], "custom.cls")
        self.assertEqual(plan.mapping["styles/helper.sty"], "helper.sty")
        self.assertEqual(
            plan.contents["main.tex"], b"\\documentclass{custom}\n\\usepackage{helper}"
        )

    def test_local_style_collision_is_blocked(self) -> None:
        self.write("main.tex", "\\documentclass{article}\n\\usepackage{a/helper}")
        self.write("a/helper.sty", "\\ProvidesPackage{helper}")
        self.write("b/helper.sty", "\\ProvidesPackage{helper}")
        plan = plan_flatten(self.root, "main.tex")
        self.assertIn("flatten-identity-collision", {item.rule for item in plan.findings})
        self.assertIn("PKG001", {item.code for item in plan.findings})
        self.assertTrue(has_blockers(plan.findings))

    def test_protected_filename_cannot_be_sanitized(self) -> None:
        self.write("main.tex", "\\documentclass{article}")
        self.write("local/bad name.sty", "\\ProvidesPackage{bad name}")
        result = plan_flatten(self.root, "main.tex")
        finding = next(item for item in result.findings if item.code == "PKG002")
        self.assertEqual(finding.path, "local/bad name.sty")
        self.assertEqual(finding.status, "failed")
        self.assertTrue(has_blockers(result.findings))
        (self.root / "local/bad name.sty").rename(self.root / "local/good.sty")
        result = plan_flatten(self.root, "main.tex")
        self.assertNotIn("PKG002", {item.code for item in result.findings})
        self.assertEqual(result.mapping["local/good.sty"], "good.sty")

    def test_relocated_style_cannot_shadow_system_style(self) -> None:
        self.write("main.tex", "\\documentclass{article}\n\\usepackage{helper}")
        self.write("unused/helper.sty", "\\ProvidesPackage{helper}")
        plan = plan_flatten(self.root, "main.tex")
        self.assertIn("flatten-system-shadowing", {item.rule for item in plan.findings})
        self.assertIn("PKG003", {item.code for item in plan.findings})

    def test_main_basename_and_bbl_jobname_are_preserved(self) -> None:
        self.write("paper/main.tex", "\\documentclass{article}\n\\input{../other/main}")
        self.write("paper/main.bbl", "References.")
        self.write("other/main.tex", "Body.")
        plan = plan_flatten(self.root, "paper/main.tex")
        self.assert_usable(plan)
        self.assertEqual(plan.mapping["paper/main.tex"], "main.tex")
        self.assertEqual(plan.mapping["paper/main.bbl"], "main.bbl")
        self.assertNotEqual(plan.mapping["other/main.tex"], "main.tex")

    def test_sanitization_and_generated_name_collisions_are_deterministic(self) -> None:
        self.write("main.tex", "\\documentclass{article}")
        for path in (
            "a/a.pdf",
            "b/a.pdf",
            "a-a.pdf",
            "a+b/plot.png",
            "a b/plot.png",
            "C/RESULT.pdf",
            "D/result.pdf",
            "data/CON.txt",
            "u/café.csv",
            "v/cafe\u0301.csv",
        ):
            self.write(path, b"data")
        first = plan_flatten(self.root, "main.tex")
        second = plan_flatten(self.root, "main.tex")
        self.assert_usable(first)
        self.assertEqual(first.mapping, second.mapping)
        self.assertEqual(first.mapping["a-a.pdf"], "a-a.pdf")
        self.assertNotEqual(first.mapping["a/a.pdf"], "a-a.pdf")
        self.assertEqual(first.mapping["data/CON.txt"], "file-CON.txt")
        self.assertTrue(
            all("/" not in filename and " " not in filename for filename in first.mapping.values())
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
                        item.rule == "flatten-unsupported-source" and item.line == 2
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
                item.rule == "flatten-unsupported-source" and item.path == "styles/helper.sty"
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
        self.assertEqual(rules.count("source-duplicate-label"), 2)
        self.assertEqual(rules.count("source-edit-marker"), 1)
        self.assertIn("source-edit-command", rules)
        self.assertIn("source-unresolved-reference", rules)
        self.assertEqual(
            {item.code for item in result.findings}, {"TEX001", "TEX002", "TEX003", "TEX004"}
        )
        self.assertTrue(all(item.evidence == "heuristic" for item in result.findings))
        self.assertFalse(has_blockers(result.findings))
        self.write("body.tex", "\\label{body}")
        self.write("main.tex", "\\documentclass{article}\\input{body}\\ref{body}")
        self.assertEqual(analyze_sources(self.root).findings, [])

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
        self.assertIn("source-case-mismatch", {item.rule for item in result.findings})
        self.assertIn("TEX007", {item.code for item in result.findings})

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

    def test_flattening_is_idempotent_and_keeps_utf8_bom_crlf(self) -> None:
        original = b"\xef\xbb\xbf\\documentclass{article}\r\n\\input{sections/body}\r\n"
        self.write("main.tex", original)
        self.write("sections/body.tex", "Body")
        first = plan_flatten(self.root, "main.tex")
        self.assert_usable(first)
        self.assertTrue(first.contents["main.tex"].startswith(b"\xef\xbb\xbf"))
        self.assertEqual(first.contents["main.tex"].count(b"\r\n"), 2)
        with tempfile.TemporaryDirectory() as temporary:
            flat = Path(temporary)
            for name, output in first.mapping.items():
                (flat / output).write_bytes(
                    first.contents.get(name, (self.root / name).read_bytes())
                )
            second = plan_flatten(flat, "main.tex")
            self.assert_usable(second)
            self.assertEqual(second.contents, {})
            self.assertEqual(second.changes, [])

    def test_unsupported_encoding_and_large_files_are_explicit(self) -> None:
        self.write("main.tex", "\\documentclass{article}\n\\input{body}")
        self.write("body.tex", b"caf\xe9")
        plan = plan_flatten(self.root, "main.tex")
        self.assertIn("source-encoding", {item.rule for item in plan.findings})
        self.assertTrue(has_blockers(plan.findings))
        self.write("body.tex", b"x" * (8 * 1024 * 1024 + 1))
        plan = plan_flatten(self.root, "main.tex")
        self.assertIn("source-size-limit", {item.rule for item in plan.findings})
        self.assertTrue(has_blockers(plan.findings))

    def test_invalid_main_does_not_read_external_file(self) -> None:
        self.write("main.tex", "\\documentclass{article}")
        for main in ("../secret.tex", "/tmp/secret.tex", "absent.tex"):
            with self.subTest(main=main):
                result = analyze_sources(self.root, main)
                self.assertIsNone(result.main)
                self.assertTrue(has_blockers(result.findings))

    def test_sidecar_relationship_breakage_is_blocked(self) -> None:
        self.write("main.tex", "\\documentclass{article}\n\\includegraphics{a/plot.pdf}")
        self.write("a/plot.pdf", b"image")
        self.write("b/plot.pdf", b"other")
        self.write("a/plot.bb", "%% Bounding box")
        self.assertIn(
            "PKG004", {item.code for item in plan_flatten(self.root, "main.tex").findings}
        )
        self.assertIn(
            "flatten-companion-identity",
            {item.rule for item in plan_flatten(self.root, "main.tex").findings},
        )

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

    def test_custom_extensionless_file_argument_is_not_ignored(self) -> None:
        self.write("main.tex", "\\documentclass{article}\n\\customreader{data/table}")
        self.write("data/table", "data")
        self.assertTrue(has_blockers(plan_flatten(self.root, "main.tex").findings))

    def test_plain_fraction_is_not_misidentified_as_custom_path(self) -> None:
        self.write("main.tex", "\\documentclass{article}\n$\\frac{a/b}{c}$")
        self.assert_usable(plan_flatten(self.root, "main.tex"))

    def test_unclosed_and_oversized_arguments_are_bounded_and_explicit(self) -> None:
        for argument in ("unclosed", "x" * (64 * 1024 + 1) + "}"):
            with self.subTest(length=len(argument)):
                self.write("main.tex", "\\documentclass{article}\n\\input{" + argument)
                plan = plan_flatten(self.root, "main.tex")
                self.assertTrue(has_blockers(plan.findings))
                self.assertIn("source-argument-limit", {item.rule for item in plan.findings})

    def test_long_unicode_collisions_keep_counter_and_byte_limit(self) -> None:
        self.write("main.tex", "\\documentclass{article}")
        basename = "é" * 100 + ".csv"
        self.write("a/" + basename, "one")
        self.write("b/" + basename, "two")
        self.write("a/long." + "e" * 230, "extension")
        plan = plan_flatten(self.root, "main.tex")
        self.assert_usable(plan)
        self.assertTrue(all(len(name.encode("utf-8")) <= 200 for name in plan.mapping.values()))

    def test_graphicspath_without_trailing_separator_is_not_reinterpreted(self) -> None:
        self.write(
            "main.tex", "\\documentclass{article}\n\\graphicspath{{images}}\n\\includegraphics{x}"
        )
        self.write("images/x.pdf", b"image")
        self.assertTrue(has_blockers(plan_flatten(self.root, "main.tex").findings))

    def test_subfiles_parent_path_option_is_explicitly_unsupported(self) -> None:
        self.write("chapter.tex", "\\documentclass[../main.tex]{subfiles}\nText")
        result = plan_flatten(self.root, "chapter.tex")
        self.assertIn("source-path-option", {item.rule for item in result.findings})
        self.assertTrue(has_blockers(result.findings))

    def test_deep_input_chains_stop_before_python_recursion_limit(self) -> None:
        self.write("main.tex", "\\documentclass{article}\n\\input{part0}")
        for index in range(130):
            self.write(f"part{index}.tex", "\\input{part" + str(index + 1) + "}")
        self.write("part130.tex", "End")
        result = plan_flatten(self.root, "main.tex")
        self.assertIn("source-input-depth", {item.rule for item in result.findings})
        self.assertTrue(has_blockers(result.findings))

    def test_truncated_unicode_names_keep_distinct_collision_suffixes(self) -> None:
        self.write("main.tex", "\\documentclass{article}")
        for ending in "abc":
            self.write("漢" * 75 + ending + ".csv", ending)
        self.write("a." + "x" * 210, "long extension")
        result = plan_flatten(self.root, "main.tex")
        self.assert_usable(result)
        outputs = [result.mapping["漢" * 75 + ending + ".csv"] for ending in "abc"]
        self.assertEqual(len(set(outputs)), 3)
        self.assertTrue(outputs[1].endswith("-2.csv"))
        self.assertTrue(outputs[2].endswith("-3.csv"))
        self.assertTrue(
            all(len(output.encode("utf-8")) <= 200 for output in result.mapping.values())
        )

    def test_colliding_long_extensions_do_not_exhaust_counter_budget(self) -> None:
        self.write("main.tex", "\\documentclass{article}")
        paths = ["a." + "x" * 210 + ending for ending in "12"]
        for path in paths:
            self.write(path, "data")
        result = plan_flatten(self.root, "main.tex")
        self.assert_usable(result)
        self.assertNotEqual(result.mapping[paths[0]], result.mapping[paths[1]])
        self.assertTrue(result.mapping[paths[1]].endswith("-2"))
        self.assertTrue(all(len(name.encode("utf-8")) <= 200 for name in result.mapping.values()))

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
        self.write("paper/inside.tex", "Ambiguous root candidate.\n")
        self.assertTrue(has_blockers(plan_flatten(self.root, "paper/main.tex").findings))

    def test_literal_subfile_body_and_parent_reference_are_flattened(self) -> None:
        self.write(
            "main.tex",
            "\\documentclass{article}\n\\usepackage{subfiles}\n"
            "\\begin{document}\n\\subfile{parts/section}\n\\end{document}\n",
        )
        self.write(
            "parts/section.tex",
            "\\documentclass[../main.tex]{subfiles}\n"
            "\\begin{document}\n\\input{details}\n\\end{document}\n",
        )
        self.write("parts/details.tex", "Details.\n")
        plan = plan_flatten(self.root, "main.tex")
        self.assert_usable(plan)
        self.assertIn(b"\\subfile{section.tex}", plan.contents["main.tex"])
        self.assertIn(b"\\documentclass[main.tex]{subfiles}", plan.contents["parts/section.tex"])
        self.assertIn("parts/details.tex", analyze_sources(self.root, "main.tex").dependencies)
        self.write(
            "parts/section.tex",
            "\\documentclass[../other.tex]{subfiles}\n\\begin{document}Body.\\end{document}\n",
        )
        self.assertTrue(has_blockers(plan_flatten(self.root, "main.tex").findings))

    def test_literal_data_and_font_dependencies_are_rewritten(self) -> None:
        self.write(
            "main.tex",
            "\\documentclass{article}\n"
            "\\pgfplotstableread{data/table.csv}\\datatable\n"
            "\\DTLloaddb{measurements}{data/values.csv}\n"
            "\\inputminted{python}{scripts/example.py}\n"
            "\\setmainfont[Path={fonts/},Ligatures=TeX]{local.otf}\n",
        )
        for path in ("data/table.csv", "data/values.csv", "scripts/example.py", "fonts/local.otf"):
            self.write(path, b"data")
        plan = plan_flatten(self.root, "main.tex")
        self.assert_usable(plan)
        rewritten = plan.contents["main.tex"]
        self.assertIn(b"\\DTLloaddb{measurements}{values.csv}", rewritten)
        self.assertIn(b"\\inputminted{python}{example.py}", rewritten)
        self.assertIn(b"Path={./}", rewritten)
        self.assertEqual(len(analyze_sources(self.root, "main.tex").dependencies), 5)
        self.write("main.tex", "\\documentclass{article}\n\\setmainfont{Some System Family}")
        self.assertTrue(has_blockers(plan_flatten(self.root, "main.tex").findings))

    def test_literal_reader_adapters_preserve_nonfile_arguments(self) -> None:
        readers = (
            (r"\pgfplotstabletypeset{data/values.csv}", r"\pgfplotstabletypeset{values.csv}"),
            (r"\csvreader{data/values.csv}{}{value}", r"\csvreader{values.csv}{}{value}"),
            (r"\csvautotabular{data/values.csv}", r"\csvautotabular{values.csv}"),
            (r"\lstinputlisting{data/values.csv}", r"\lstinputlisting{values.csv}"),
            (r"\verbatiminput{data/values.csv}", r"\verbatiminput{values.csv}"),
            (r"\VerbatimInput{data/values.csv}", r"\VerbatimInput{values.csv}"),
            (
                r"\includepdf[pages={1,2}]{figures/pages.pdf}",
                r"\includepdf[pages={1,2}]{pages.pdf}",
            ),
        )
        self.write("data/values.csv", "a,b\n1,2\n")
        self.write("figures/pages.pdf", b"pdf")
        for command, expected in readers:
            with self.subTest(command=command):
                self.write("main.tex", "\\documentclass{article}\n" + command)
                plan = plan_flatten(self.root, "main.tex")
                self.assert_usable(plan)
                self.assertIn(expected.encode(), plan.contents["main.tex"])

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

    def test_filename_overrides(self) -> None:
        self.write("main.tex", "\\documentclass{article}\n\\includegraphics{figures/plot.pdf}\n")
        self.write("figures/plot.pdf", b"image")
        plan = plan_flatten(
            self.root, "main.tex", filename_overrides=(("figures/plot.pdf", "figure-1.pdf"),)
        )
        self.assert_usable(plan)
        self.assertEqual(plan.mapping["figures/plot.pdf"], "figure-1.pdf")
        self.assertIn(b"\\includegraphics{figure-1.pdf}", plan.contents["main.tex"])
        self.assertEqual(
            next(f for f in plan.findings if f.rule == "flatten.filename_overrides").status,
            "passed",
        )
        for override, status in (
            ((("missing.pdf", "other.pdf"),), "failed"),
            ((("figures/plot.pdf", "../escape.pdf"),), "failed"),
            ((("main.tex", "paper.tex"),), "inconclusive"),
        ):
            with self.subTest(override=override):
                blocked = plan_flatten(self.root, "main.tex", filename_overrides=override)
                self.assertTrue(has_blockers(blocked.findings))
                self.assertIn(
                    status,
                    [f.status for f in blocked.findings if f.rule == "flatten.filename_overrides"],
                )

    def test_imported_metadata_is_visible_and_subfile_content_is_explicitly_uncertain(self) -> None:
        from latexprep.manuscript import ManuscriptOptions, check_manuscript
        from latexprep.manuscript_checks import ManuscriptCheckOptions, check_manuscript_details

        self.write("main.tex", "\\documentclass{article}\n\\import{parts/}{metadata}\n")
        self.write(
            "parts/metadata.tex",
            "\\begin{abstract}A supported abstract.\\end{abstract}\n\\title{Imported title}\n",
        )
        findings = check_manuscript(self.root, "main.tex", ManuscriptOptions(require_abstract=True))
        self.assertEqual(
            next(f for f in findings if f.rule == "manuscript.abstract_required").status, "passed"
        )
        detailed = check_manuscript_details(
            self.root, "main.tex", ManuscriptCheckOptions(required_metadata=("title",))
        )
        self.assertFalse(has_blockers(detailed))


if __name__ == "__main__":
    unittest.main()
