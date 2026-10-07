from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from latexprep.manuscript import ManuscriptOptions, check_manuscript
from latexprep.models import Finding, PreparationError


class ManuscriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write(self, name: str, text: str) -> None:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def finding(self, text: str, options: ManuscriptOptions, rule: str) -> Finding:
        self.write("main.tex", text)
        findings = check_manuscript(self.root, "main.tex", options)
        return next(item for item in findings if item.rule == rule)

    def test_expected_document_class_matches_and_mismatch_blocks(self) -> None:
        options = ManuscriptOptions(expected_document_class="article")
        for name, status in (("article", "passed"), ("report", "failed")):
            with self.subTest(name=name):
                result = self.finding(
                    r"\documentclass{" + name + "}", options, "manuscript.document_class"
                )
                self.assertEqual(result.status, status)
                self.assertEqual(result.details["declarations"][0]["names"], [name])
                self.assertEqual(result.severity, "info" if status == "passed" else "error")
                self.assertEqual(result.code, "TEX101")

    def test_class_options_report_missing_and_forbidden_values(self) -> None:
        options = ManuscriptOptions(
            required_class_options=("twocolumn",), forbidden_class_options=("draft",)
        )
        good = self.finding(
            r"\documentclass[twocolumn, 10pt]{article}", options, "manuscript.class_options"
        )
        self.assertEqual(good.status, "passed")
        bad = self.finding(r"\documentclass[draft]{article}", options, "manuscript.class_options")
        self.assertEqual(bad.status, "failed")
        self.assertEqual(bad.severity, "error")
        self.assertEqual(bad.details["missing"], ["twocolumn"])
        self.assertEqual(bad.details["present_forbidden"], ["draft"])
        self.assertEqual(bad.code, "TEX102")

    def test_forbidden_packages_check_direct_local_graph_declarations(self) -> None:
        self.write("helper.sty", r"\RequirePackage{todonotes}")
        options = ManuscriptOptions(forbidden_packages=("todonotes",))
        good = self.finding(
            r"\documentclass{article}\usepackage{graphicx}",
            options,
            "manuscript.forbidden_packages",
        )
        self.assertEqual(good.status, "passed")
        bad = self.finding(
            r"\documentclass{article}\usepackage{helper}", options, "manuscript.forbidden_packages"
        )
        self.assertEqual(bad.status, "failed")
        self.assertEqual(bad.details["matches"][0]["path"], "helper.sty")
        self.assertIn("transitive", bad.details["scope"])
        self.assertEqual(bad.code, "TEX103")

    def test_required_abstract_rejects_missing_and_empty_content(self) -> None:
        options = ManuscriptOptions(require_abstract=True)
        for body, status in (
            ("", "failed"),
            (r"\begin{abstract} \end{abstract}", "failed"),
            (r"\begin{abstract}Some findings.\end{abstract}", "passed"),
        ):
            with self.subTest(body=body):
                result = self.finding(
                    r"\documentclass{article}" + body, options, "manuscript.abstract_required"
                )
                self.assertEqual(result.status, status)
                self.assertEqual(result.code, "TEX104")

    def test_invisible_abstract_commands_do_not_establish_nonempty_content(self) -> None:
        for body in (
            r"\label{abs}",
            r"\nocite{key}",
            r"\emph{\label{abs}}\par\nocite{key}",
            r"\textbf{}",
        ):
            with self.subTest(body=body):
                result = self.finding(
                    r"\documentclass{article}\begin{abstract}" + body + r"\end{abstract}",
                    ManuscriptOptions(require_abstract=True),
                    "manuscript.abstract_required",
                )
                self.assertEqual(result.code, "TEX104")
                self.assertEqual(result.status, "failed")
                self.assertEqual(result.severity, "error")
                self.assertFalse(result.details["abstracts"][0]["nonempty_literal_text"])
                self.assertEqual(result.details["abstracts"][0]["words"], 0)
                self.assertIsNone(result.suggestion)
                self.assertIn("Add a nonempty abstract", result.next_step)
        visible = self.finding(
            r"\documentclass{article}\abstract{\label{abs}Actual findings.\nocite{key}}",
            ManuscriptOptions(require_abstract=True),
            "manuscript.abstract_required",
        )
        self.assertEqual(visible.status, "passed")
        self.assertTrue(visible.details["abstracts"][0]["nonempty_literal_text"])

    def test_math_only_abstract_presence_is_inconclusive_but_word_count_is_explicit(self) -> None:
        self.write("main.tex", r"\documentclass{article}\abstract{$x + y$}")
        findings = {
            item.rule: item
            for item in check_manuscript(
                self.root,
                "main.tex",
                ManuscriptOptions(
                    require_abstract=True, abstract_min_words=0, abstract_max_words=0
                ),
            )
        }
        presence = findings["manuscript.abstract_required"]
        self.assertEqual(presence.status, "inconclusive")
        self.assertEqual(presence.severity, "error")
        self.assertIn("mathematics", presence.message)
        self.assertIsNotNone(presence.suggestion)
        count = findings["manuscript.abstract_words"]
        self.assertEqual(count.status, "passed")
        self.assertEqual(count.details["abstracts"][0]["words"], 0)

    def test_abstract_word_limits_use_documented_math_and_markup_counting(self) -> None:
        source = (
            r"\documentclass{article}\begin{abstract}We test \emph{well-known methods} "
            r"with $x + y$ and \LaTeX{} \cite{key}.\end{abstract}"
        )
        for minimum, maximum, status in ((7, 7, "passed"), (8, 12, "failed"), (0, 6, "failed")):
            with self.subTest(minimum=minimum, maximum=maximum):
                result = self.finding(
                    source,
                    ManuscriptOptions(abstract_min_words=minimum, abstract_max_words=maximum),
                    "manuscript.abstract_words",
                )
                self.assertEqual(result.details["abstracts"][0]["words"], 7)
                self.assertEqual(result.status, status)
                self.assertIn("math", result.details["counting_method"])
                self.assertEqual(result.code, "TEX105")

    def test_keyword_limits_count_phrases_and_common_environments(self) -> None:
        for body in (
            r"\keywords{machine learning, testing; verification}",
            r"\begin{keyword}machine learning\sep testing\sep verification\end{keyword}",
            r"\begin{IEEEkeywords}machine learning, testing, verification\end{IEEEkeywords}",
        ):
            with self.subTest(body=body):
                source = r"\documentclass{article}" + body
                good = self.finding(
                    source,
                    ManuscriptOptions(keywords_min_count=3, keywords_max_count=3),
                    "manuscript.keyword_count",
                )
                self.assertEqual(good.details["count"], 3)
                self.assertEqual(good.status, "passed")
                bad = self.finding(
                    source, ManuscriptOptions(keywords_max_count=2), "manuscript.keyword_count"
                )
                self.assertEqual(bad.status, "failed")
                self.assertEqual(bad.code, "TEX106")
        absent = self.finding(
            r"\documentclass{article}",
            ManuscriptOptions(keywords_min_count=1),
            "manuscript.keyword_count",
        )
        self.assertEqual(absent.status, "failed")
        self.assertEqual(absent.details["count"], 0)

    def test_required_sections_match_literal_headings_not_body_claims(self) -> None:
        options = ManuscriptOptions(required_sections=("Data availability", "Funding"))
        result = self.finding(
            (
                r"\documentclass{article}\section*{DATA \emph{availability}}"
                "Funding is discussed in the body."
            ),
            options,
            "manuscript.required_sections",
        )
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.details["missing"], ["Funding"])
        self.assertIn("adequacy is not assessed", result.message)
        self.assertEqual(result.code, "TEX107")
        present = self.finding(
            r"\documentclass{article}\section{Data availability}\subsection[Short]{Funding}",
            options,
            "manuscript.required_sections",
        )
        self.assertEqual(present.status, "passed")

    def test_selected_dependency_graph_excludes_unrelated_document_roots(self) -> None:
        self.write(
            "other.tex",
            (
                r"\documentclass{report}\usepackage{todonotes}\begin{abstract}Other text."
                r"\end{abstract}\section{Funding}"
            ),
        )
        self.write("paper/main.tex", r"\documentclass{article}\input{sections/body}")
        self.write(
            "paper/sections/body.tex",
            r"\begin{abstract}Selected text.\end{abstract}\section{Results}",
        )
        self.write("paper/sections/other.tex", r"\section{Funding}")
        options = ManuscriptOptions(
            expected_document_class="article",
            forbidden_packages=("todonotes",),
            require_abstract=True,
            required_sections=("Funding",),
        )
        results = {
            item.rule: item for item in check_manuscript(self.root, "paper/main.tex", options)
        }
        self.assertEqual(results["manuscript.document_class"].status, "passed")
        self.assertEqual(results["manuscript.forbidden_packages"].status, "passed")
        self.assertEqual(results["manuscript.abstract_required"].status, "passed")
        self.assertEqual(results["manuscript.required_sections"].status, "failed")
        self.assertEqual(
            results["manuscript.inventory"].details["content_sources"],
            ["paper/main.tex", "paper/sections/body.tex"],
        )

    def test_comments_code_examples_and_text_after_document_end_do_not_count(self) -> None:
        source = r"""\documentclass{article}
% \usepackage{todonotes}
\begin{verbatim}
\begin{abstract}Example only.\end{abstract}
\section{Funding}
\usepackage{todonotes}
\end{verbatim}
\verb|\keywords{example}|
\end{document}
\section{Funding}
\begin{abstract}Unreachable.\end{abstract}
"""
        self.write("main.tex", source)
        options = ManuscriptOptions(
            require_abstract=True,
            forbidden_packages=("todonotes",),
            required_sections=("Funding",),
            keywords_min_count=1,
        )
        results = {item.rule: item for item in check_manuscript(self.root, "main.tex", options)}
        self.assertEqual(results["manuscript.abstract_required"].status, "failed")
        self.assertEqual(results["manuscript.forbidden_packages"].status, "passed")
        self.assertEqual(results["manuscript.required_sections"].status, "failed")
        self.assertEqual(results["manuscript.keyword_count"].status, "failed")
        self.assertEqual((self.root / "main.tex").read_text(), source)

    def test_conditional_and_macro_generated_declarations_are_inconclusive(self) -> None:
        cases = (
            (
                r"\ifdefined\foo\documentclass{article}\fi",
                ManuscriptOptions(expected_document_class="article"),
                "manuscript.document_class",
            ),
            (
                r"\documentclass[\opts]{article}",
                ManuscriptOptions(required_class_options=("twocolumn",)),
                "manuscript.class_options",
            ),
            (
                r"\documentclass{article}\newcommand{\loadit}{\usepackage{todonotes}}",
                ManuscriptOptions(forbidden_packages=("todonotes",)),
                "manuscript.forbidden_packages",
            ),
            (
                r"\documentclass{article}\newcommand{\ab}{\begin{abstract}Text.\end{abstract}}",
                ManuscriptOptions(require_abstract=True),
                "manuscript.abstract_required",
            ),
            (
                r"\documentclass{article}\begin{abstract}Known \unknown.\end{abstract}",
                ManuscriptOptions(abstract_max_words=100),
                "manuscript.abstract_words",
            ),
            (
                r"\documentclass{article}\keywords{\terms}",
                ManuscriptOptions(keywords_max_count=4),
                "manuscript.keyword_count",
            ),
            (
                r"\documentclass{article}\section{\statement}",
                ManuscriptOptions(required_sections=("Funding",)),
                "manuscript.required_sections",
            ),
        )
        for source, options, rule in cases:
            with self.subTest(rule=rule):
                result = self.finding(source, options, rule)
                self.assertEqual(result.status, "inconclusive")
                self.assertEqual(result.severity, "error")

    def test_multiple_or_unclosed_abstracts_are_inconclusive(self) -> None:
        for body in (
            r"\begin{abstract}One.\end{abstract}\abstract{Two.}",
            r"\begin{abstract}Never closed.",
            r"\begin{abstract}\emph{\end{abstract}}",
        ):
            result = self.finding(
                r"\documentclass{article}" + body,
                ManuscriptOptions(require_abstract=True),
                "manuscript.abstract_required",
            )
            self.assertEqual(result.status, "inconclusive")

    def test_missing_or_dynamic_source_inputs_prevent_false_pass(self) -> None:
        for target in ("missing", r"\generated"):
            result = self.finding(
                r"\documentclass{article}\input{" + target + "}",
                ManuscriptOptions(forbidden_packages=("todonotes",)),
                "manuscript.forbidden_packages",
            )
            self.assertEqual(result.status, "inconclusive")
            self.assertTrue(result.details["uncertainty"])

    def test_default_options_do_not_emit_compliance_passes_or_read_input(self) -> None:
        self.assertEqual(
            check_manuscript(self.root / "absent", "main.tex", ManuscriptOptions()), []
        )

    def test_empty_class_options_are_literal_and_definitions_are_not_content(self) -> None:
        self.write("custom.cls", r"\newcommand{\example}{\abstract{Only a definition.}}")
        result = self.finding(
            r"\documentclass[]{custom}",
            ManuscriptOptions(forbidden_class_options=("draft",)),
            "manuscript.class_options",
        )
        self.assertEqual(result.status, "passed")
        absent = self.finding(
            r"\documentclass{custom}",
            ManuscriptOptions(require_abstract=True),
            "manuscript.abstract_required",
        )
        self.assertEqual(absent.status, "failed")
        self.assertEqual(absent.details["abstracts"], [])

    def test_repeated_inputs_and_nested_abstracts_are_not_definitive_counts(self) -> None:
        self.write("body.tex", r"\abstract{Repeated abstract.}")
        repeated = self.finding(
            r"\documentclass{article}\input{body}\input{body}",
            ManuscriptOptions(abstract_min_words=1),
            "manuscript.abstract_words",
        )
        self.assertEqual(repeated.status, "inconclusive")
        self.assertIn("repeated source inclusion", repeated.message)
        nested = self.finding(
            r"\documentclass{article}\begin{abstract}\begin{abstract}Text."
            r"\end{abstract}\end{abstract}",
            ManuscriptOptions(abstract_min_words=1),
            "manuscript.abstract_words",
        )
        self.assertEqual(nested.status, "inconclusive")
        self.assertTrue(all(item["uncertainty"] for item in nested.details["abstracts"]))

    def test_commands_after_document_end_do_not_select_another_root(self) -> None:
        self.write("other.tex", r"\documentclass{report}\usepackage{todonotes}")
        result = self.finding(
            r"\documentclass{article}\end{document}\input{other}",
            ManuscriptOptions(expected_document_class="article"),
            "manuscript.document_class",
        )
        self.assertEqual(result.status, "passed")
        self.assertEqual(len(result.details["declarations"]), 1)

    def test_endinput_stops_heading_and_dependency_search_in_included_file(self) -> None:
        self.write("funding.tex", r"\section{Funding}")
        self.write("body.tex", "\\endinput\n\\section{Funding}\n\\input{funding}\n")
        self.write("main.tex", "\\documentclass{article}\n\\input{body}\n\\section{Results}\n")
        options = ManuscriptOptions(required_sections=("Funding", "Results"))
        findings = {item.rule: item for item in check_manuscript(self.root, "main.tex", options)}
        missing = findings["manuscript.required_sections"]
        self.assertEqual(missing.code, "TEX107")
        self.assertEqual(missing.status, "failed")
        self.assertEqual(missing.details["missing"], ["Funding"])
        self.assertEqual([item["heading"] for item in missing.details["headings"]], ["Results"])
        self.assertNotIn("funding.tex", findings["manuscript.inventory"].details["content_sources"])
        self.write("body.tex", "\\section{Funding}\n\\endinput\n")
        present = next(
            item
            for item in check_manuscript(self.root, "main.tex", options)
            if item.rule == "manuscript.required_sections"
        )
        self.assertEqual(present.status, "passed")

    def test_endinput_same_line_conditional_and_macro_contexts_are_inconclusive(self) -> None:
        for source in (
            "\\endinput\\section{Funding}\n",
            "\\iffalse\\endinput\n\\fi\\section{Funding}\n",
            r"\def\stop{\endinput}\stop\section{Funding}",
        ):
            with self.subTest(source=source):
                self.write("body.tex", source)
                result = self.finding(
                    "\\documentclass{article}\n\\input{body}\n",
                    ManuscriptOptions(required_sections=("Funding",)),
                    "manuscript.required_sections",
                )
                self.assertEqual(result.status, "inconclusive")
                self.assertEqual(result.severity, "error")
                self.assertIn("endinput", result.message)

    def test_invalid_count_ranges_and_conflicting_class_options_are_rejected(self) -> None:
        for values in (
            {"abstract_min_words": -1},
            {"keywords_min_count": 3, "keywords_max_count": 2},
            {"required_class_options": ("draft",), "forbidden_class_options": ("draft",)},
            {"require_abstract": "yes"},
            {"required_sections": ("",)},
        ):
            with self.subTest(values=values), self.assertRaises(PreparationError):
                ManuscriptOptions(**values)


if __name__ == "__main__":
    unittest.main()
