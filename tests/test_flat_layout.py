"""The flat layout: how files are renamed, collisions resolved and dependencies rewritten."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from latexprep.findings import has_blockers
from latexprep.flat_layout import plan_flatten
from latexprep.source_analysis import analyze_sources
from tests.support import ProjectTestCase


class FlatLayoutTests(ProjectTestCase):
    """Flat layout plans: renamed files, rewritten references, collisions and advice."""

    def assert_usable(self, plan) -> None:
        """Assert that ``plan`` has no blocking finding and maps no two files to one name.

        Names that differ only in case count as one name.
        """
        self.assertFalse(
            has_blockers(plan.findings),
            [finding for finding in plan.findings if finding.severity == "error"],
        )
        self.assertEqual(
            len(plan.mapping), len({value.casefold() for value in plan.mapping.values()})
        )

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
        self.assertIn("flatten.identity_collision", {item.rule for item in plan.findings})
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
        self.assertIn("flatten.system_shadowing", {item.rule for item in plan.findings})
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

    def test_sidecar_relationship_breakage_is_blocked(self) -> None:
        self.write("main.tex", "\\documentclass{article}\n\\includegraphics{a/plot.pdf}")
        self.write("a/plot.pdf", b"image")
        self.write("b/plot.pdf", b"other")
        self.write("a/plot.bb", "%% Bounding box")
        self.assertIn(
            "PKG004", {item.code for item in plan_flatten(self.root, "main.tex").findings}
        )
        self.assertIn(
            "flatten.companion_identity",
            {item.rule for item in plan_flatten(self.root, "main.tex").findings},
        )

    def finding(self, findings, rule: str):
        """Return the first of ``findings`` whose rule is ``rule``.

        Args:
            findings: The findings to search.
            rule: The rule name to find.

        Returns:
            The first matching finding.

        Raises:
            StopIteration: No finding has the rule.
        """
        return next(item for item in findings if item.rule == rule)

    def test_advice_names_the_layout_setting_only_where_it_applies(self) -> None:
        self.write(
            "main.tex",
            "\\documentclass{article}\n\\input{\\chosen}\n\\IfFileExists{x.tex}{}{}\n"
            "\\customreader{data/table.csv}\n\\ProvidesPackage{dir/name}\n",
        )
        self.write("data/table.csv", "data")
        analysis = analyze_sources(self.root, "main.tex")
        plan = plan_flatten(self.root, "main.tex")
        advice = [
            item.suggestion for item in (*analysis.findings, *plan.findings) if item.suggestion
        ]
        self.assertTrue(advice)
        self.assertFalse([text for text in advice if "preserve layout" in text.lower()], advice)
        for item in analysis.findings:
            if item.suggestion and "layout" in item.suggestion:
                self.assertIn('layout = "', item.suggestion)
        unsupported = [item for item in plan.findings if item.rule == "flatten.unsupported_source"]
        self.assertTrue(unsupported)
        self.assertTrue(all('layout = "preserve"' in item.suggestion for item in unsupported))

    def test_flat_layout_with_other_roots_explains_the_limit_in_plain_words(self) -> None:
        self.write("main.tex", "\\documentclass{article}\n")
        self.write("other.tex", "\\documentclass{article}\n")
        roots = self.finding(plan_flatten(self.root, "main.tex").findings, "flatten.multiple_roots")
        self.assertEqual(roots.details["other_roots"], ["other.tex"])
        self.assertNotIn("initial", roots.message.lower())
        self.assertIn("Flat layout supports one document root per package", roots.message)

    def test_long_unicode_collisions_keep_counter_and_byte_limit(self) -> None:
        self.write("main.tex", "\\documentclass{article}")
        basename = "é" * 100 + ".csv"
        self.write("a/" + basename, "one")
        self.write("b/" + basename, "two")
        self.write("a/long." + "e" * 230, "extension")
        plan = plan_flatten(self.root, "main.tex")
        self.assert_usable(plan)
        self.assertTrue(all(len(name.encode("utf-8")) <= 200 for name in plan.mapping.values()))

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


if __name__ == "__main__":
    unittest.main()
