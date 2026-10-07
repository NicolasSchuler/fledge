from __future__ import annotations

import tempfile
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from unittest.mock import patch

from latexprep.manuscript_checks import (
    ManuscriptCheckOptions,
    check_manuscript_details,
    read_literal_metadata,
)
from latexprep.models import Finding, PreparationError


class ManuscriptDetailTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def write(self, name: str, text: str) -> None:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def results(self, body: str, options: ManuscriptCheckOptions) -> list[Finding]:
        self.write("main.tex", "\\documentclass{article}\n" + body)
        return check_manuscript_details(self.root, "main.tex", options)

    def result(self, body: str, options: ManuscriptCheckOptions, suffix: str) -> Finding:
        return next(
            finding
            for finding in self.results(body, options)
            if finding.rule == "manuscript." + suffix
        )

    def policy(
        self,
        suffix: str,
        code: str,
        options: ManuscriptCheckOptions,
        valid: str,
        violating: str,
        *,
        advisory: bool = False,
    ) -> None:
        good = self.result(valid, options, suffix)
        self.assertEqual(good.status, "passed", good.details)
        self.assertEqual(good.severity, "info")
        bad = self.result(violating, options, suffix)
        self.assertEqual(bad.status, "failed", bad.details)
        self.assertEqual(bad.code, code)
        self.assertEqual(bad.severity, "warning" if advisory else "error")
        self.assertEqual(bad.evidence, "heuristic" if advisory else "derived")
        self.assertTrue(bad.details["matches"])
        uncertain = self.result("\\iftrue\n" + violating + "\n\\fi", options, suffix)
        self.assertEqual(uncertain.status, "inconclusive", uncertain.details)
        self.assertTrue(uncertain.details["uncertainty"])

    def test_package_allowlist(self) -> None:
        self.policy(
            "package_allowlist",
            "MAN001",
            ManuscriptCheckOptions(allowed_packages=("graphicx",)),
            r"\usepackage{graphicx}",
            r"\usepackage{unknownstyle}",
        )
        empty = self.result(
            r"\usepackage{graphicx}",
            ManuscriptCheckOptions(allowed_packages=()),
            "package_allowlist",
        )
        self.assertEqual(empty.status, "failed")
        self.write("local.sty", "\\RequirePackage{graphicx}\n")
        inventory = self.result(
            r"\usepackage[option]{local}",
            ManuscriptCheckOptions(inventory_packages=True),
            "package_inventory",
        )
        declarations = inventory.details["declarations"]
        assert isinstance(declarations, list)
        self.assertEqual(len(declarations), 2)
        self.assertIn("transitive system", str(inventory.details["scope"]))

    def test_layout_manipulation(self) -> None:
        self.policy(
            "layout_manipulation",
            "MAN002",
            ManuscriptCheckOptions(check_layout_manipulation=True),
            "Regular prose.\n% \\vspace{-1cm}\n\\verb|\\newpage|",
            r"\vspace{-1cm}\newpage\setlength{\textwidth}{1in}",
            advisory=True,
        )
        positive = self.result(
            r"\vspace{1cm}",
            ManuscriptCheckOptions(check_layout_manipulation=True),
            "layout_manipulation",
        )
        self.assertEqual(positive.status, "passed")
        forced = self.result(
            r"\begin{figure}[!ht]Image.\end{figure}",
            ManuscriptCheckOptions(check_layout_manipulation=True),
            "layout_manipulation",
        )
        self.assertEqual(forced.status, "failed")

    def test_abstract_citations(self) -> None:
        self.policy(
            "abstract_citations",
            "MAN003",
            ManuscriptCheckOptions(forbid_abstract_citations=True),
            r"\begin{abstract}Findings.\end{abstract}",
            r"\begin{abstract}Findings \cite{key}.\end{abstract}",
        )
        nested = self.result(
            r"\abstract{Findings \citep[see][p. 1]{key}.}",
            ManuscriptCheckOptions(forbid_abstract_citations=True),
            "abstract_citations",
        )
        self.assertEqual(nested.status, "failed")

    def test_abstract_abbreviations(self) -> None:
        self.policy(
            "abstract_abbreviations",
            "MAN004",
            ManuscriptCheckOptions(abstract_abbreviation_policy="define"),
            r"\abstract{A central processing unit (CPU) runs the code. CPU usage falls.}",
            r"\abstract{CPU usage falls.}",
            advisory=True,
        )
        forbidden = self.result(
            r"\abstract{A central processing unit (CPU) runs the code.}",
            ManuscriptCheckOptions(abstract_abbreviation_policy="forbid"),
            "abstract_abbreviations",
        )
        self.assertEqual(forbidden.status, "failed")
        exempt = self.result(
            r"\abstract{CPU usage falls.}",
            ManuscriptCheckOptions(
                abstract_abbreviation_policy="forbid", abstract_abbreviation_exceptions=("CPU",)
            ),
            "abstract_abbreviations",
        )
        self.assertEqual(exempt.status, "passed")

    def test_heading_depth(self) -> None:
        self.policy(
            "heading_depth",
            "MAN005",
            ManuscriptCheckOptions(max_heading_depth=1),
            r"\section{Results}Findings.",
            r"\subsection{Results}Findings.",
        )

    def test_empty_sections(self) -> None:
        self.policy(
            "empty_sections",
            "MAN006",
            ManuscriptCheckOptions(check_empty_sections=True),
            r"\section{Results}\subsection{Details}Findings.",
            r"\section{Results}\label{sec:results}\section{Discussion}Findings.",
            advisory=True,
        )
        included = ManuscriptCheckOptions(check_empty_sections=True)
        self.write("body.tex", "Findings.")
        result = self.result(r"\section{Results}\input{body}", included, "empty_sections")
        self.assertEqual(result.status, "passed")

    def test_hardcoded_references(self) -> None:
        self.policy(
            "hardcoded_references",
            "MAN007",
            ManuscriptCheckOptions(check_hardcoded_references=True),
            r"Figure~\ref{fig:x}. \verb|Table 2| \url{Figure 3}",
            "Figure 2 and Table 4 provide details.",
            advisory=True,
        )

    def test_required_metadata(self) -> None:
        required = ("title", "author", "affiliation", "email", "corresponding_author")
        self.policy(
            "required_metadata",
            "MAN008",
            ManuscriptCheckOptions(required_metadata=required),
            r"\title{Study}\author{Jane Doe}\affiliation{Example University}"
            r"\email{jane@example.org}\correspondingauthor{Jane Doe}",
            r"\title{Study}\author{Author name}",
        )
        mapped, uncertainty = read_literal_metadata(self.root, "main.tex")
        self.assertTrue(uncertainty)
        self.assertNotIn("author", mapped)

    def test_orcid(self) -> None:
        self.policy(
            "orcid",
            "MAN009",
            ManuscriptCheckOptions(check_orcid=True),
            r"\orcid{0000-0002-1825-0097}",
            r"\orcid{0000-0002-1825-0098}",
        )
        for value in ("https://orcid.org/0000-0002-1825-0097", "0000-0002-1694-233X"):
            result = self.result(
                "\\orcidlink{" + value + "}", ManuscriptCheckOptions(check_orcid=True), "orcid"
            )
            self.assertEqual(result.status, "passed")
        invalid = self.result(r"\orcid{123}", ManuscriptCheckOptions(check_orcid=True), "orcid")
        self.assertEqual(invalid.status, "failed")
        dynamic = self.result(r"\orcid{\id}", ManuscriptCheckOptions(check_orcid=True), "orcid")
        self.assertEqual(dynamic.status, "inconclusive")

    def test_required_declarations(self) -> None:
        options = ManuscriptCheckOptions(required_declarations=("Data Availability", "Funding"))
        self.policy(
            "required_declarations",
            "MAN010",
            options,
            r"\section*{Data Availability}Data are available."
            r"\declaration{Funding}{No external funding.}",
            r"\section*{Data Availability}\section{Results}Findings.",
        )
        empty = self.result(
            r"\declaration{Data Availability}{\label{data}}\declaration{Funding}{TBD}",
            options,
            "required_declarations",
        )
        self.assertEqual(empty.status, "failed")

    def test_float_captions(self) -> None:
        self.policy(
            "float_captions",
            "MAN011",
            ManuscriptCheckOptions(require_float_captions=True),
            r"\begin{figure}\caption{Results}\end{figure}\begin{table*}\caption{Data}\end{table*}",
            r"\begin{figure}\caption{}\end{figure}\begin{table}Data.\end{table}",
        )

    def test_float_labels(self) -> None:
        self.policy(
            "float_labels",
            "MAN012",
            ManuscriptCheckOptions(require_float_labels=True),
            r"\begin{figure}\label{fig:x}\end{figure}\begin{table*}\label{tab:x}\end{table*}",
            r"\begin{figure}Image.\end{figure}",
        )

    def test_float_label_order(self) -> None:
        self.policy(
            "float_label_order",
            "MAN013",
            ManuscriptCheckOptions(check_float_label_order=True),
            r"\begin{figure}\caption{Results}\label{fig:x}\end{figure}",
            r"\begin{figure}\label{fig:x}\caption{Results}\end{figure}",
        )

    def test_float_references(self) -> None:
        self.policy(
            "float_references",
            "MAN014",
            ManuscriptCheckOptions(require_float_references=True),
            r"See \ref{fig:x}.\begin{figure}\label{fig:x}\end{figure}",
            r"\begin{figure}\label{fig:x}\caption{See \ref{fig:x}}\end{figure}",
        )

    def test_float_reference_order(self) -> None:
        floats = r"\begin{figure}\label{fig:a}\end{figure}\begin{figure}\label{fig:b}\end{figure}"
        self.policy(
            "float_reference_order",
            "MAN015",
            ManuscriptCheckOptions(check_float_reference_order=True),
            r"See \ref{fig:a}, then \ref{fig:b}." + floats,
            r"See \ref{fig:b}, then \ref{fig:a}." + floats,
        )
        missing = self.result(
            floats,
            ManuscriptCheckOptions(check_float_reference_order=True),
            "float_reference_order",
        )
        self.assertEqual(missing.status, "inconclusive")

    def test_figure_descriptions(self) -> None:
        self.policy(
            "figure_descriptions",
            "MAN016",
            ManuscriptCheckOptions(require_figure_descriptions=True),
            r"\begin{figure}\Description{A line increases from left to right.}\end{figure}",
            r"\begin{figure}\Description{TODO}\end{figure}",
        )
        missing = self.result(
            r"\begin{figure}Image.\end{figure}",
            ManuscriptCheckOptions(require_figure_descriptions=True),
            "figure_descriptions",
        )
        self.assertEqual(missing.status, "failed")
        self.assertIn("human review", str(missing.details["description_quality"]))

    def test_float_spanning_literal_include_boundaries(self) -> None:
        self.write("start.tex", "\\begin{figure}\n\\caption{Results}\n")
        self.write("middle.tex", "\\label{fig:x}\n\\Description{A line rises.}\n")
        self.write("end.tex", "\\end{figure}\n")
        options = ManuscriptCheckOptions(
            require_float_captions=True,
            require_float_labels=True,
            check_float_label_order=True,
            require_float_references=True,
            require_figure_descriptions=True,
        )
        results = self.results(r"See \ref{fig:x}.\input{start}\input{middle}\input{end}", options)
        self.assertTrue(all(result.status == "passed" for result in results), results)
        floats = results[0].details["floats"]
        assert isinstance(floats, list)
        inventory = floats[0]
        self.assertEqual(inventory["path"], "start.tex")
        self.assertEqual(inventory["captions"][0]["line"], 2)
        self.assertEqual(inventory["labels"], ["fig:x"])

    def test_unknown_constructs_make_their_own_region_inconclusive(self) -> None:
        # An unexpanded construct is an interpretation gap for the region it sits
        # in, not for every check in the document.
        cases = (
            (r"\begin{figure}\generatedcaption\end{figure}", "float_captions"),
            (r"\begin{figure}\begin{customfloat}\end{customfloat}\end{figure}", "float_captions"),
            (r"\section{Results}\generatedbody", "empty_sections"),
            (r"\newcommand{\x}{\title{Generated}}\x", "required_metadata"),
            (r"\title{\generatedtitle}", "required_metadata"),
            (r"\begin{abstract}\generatedprose\end{abstract}", "abstract_citations"),
        )
        options = ManuscriptCheckOptions(
            require_float_captions=True,
            required_metadata=("title",),
            check_empty_sections=True,
            forbid_abstract_citations=True,
        )
        for body, suffix in cases:
            with self.subTest(body=body):
                result = self.result(body, options, suffix)
                self.assertEqual(result.status, "inconclusive", result.details)
                self.assertTrue(result.details["uncertainty"])

    def test_unrelated_uncertainty_never_hides_a_confirmed_violation(self) -> None:
        options = ManuscriptCheckOptions(require_float_captions=True, check_empty_sections=True)
        result = self.result(
            r"\newcommand{\R}{\mathbb{R}}\generatedprose"
            r"\begin{figure}Plot.\end{figure}",
            options,
            "float_captions",
        )
        self.assertEqual(result.status, "failed", result.details)
        self.assertEqual(result.message, "1 of 1 floats lack a caption: main.tex:2.")
        self.assertTrue(result.details["matches"])
        definitions = self.result(
            r"\newcommand{\R}{\mathbb{R}}\def\ours{Method}"
            r"\begin{figure}\caption{Shown}\end{figure}",
            options,
            "float_captions",
        )
        self.assertEqual(definitions.status, "passed", definitions.details)

    def test_preamble_declarations_are_not_body_constructs(self) -> None:
        self.write(
            "main.tex",
            "\\documentclass{article}\n"
            "\\newcommand{\\R}{\\mathbb{R}}\n"
            "\\usepackage{graphicx}\n"
            "\\begin{document}\n"
            "\\section{Results}Findings for $\\R$.\n"
            "\\bibliographystyle{plain}\\bibliography{refs}\n"
            "\\end{document}\n",
        )
        (self.root / "refs.bib").write_text("@misc{a, title={A}}\n")
        results = check_manuscript_details(
            self.root,
            "main.tex",
            ManuscriptCheckOptions(check_empty_sections=True, require_float_captions=True),
        )
        self.assertTrue(all(result.status == "passed" for result in results), results)

    def test_failure_messages_report_counts_and_first_locations(self) -> None:
        self.write("extra.tex", "\\begin{figure}\\end{figure}\n\\begin{figure}\\end{figure}\n")
        result = self.result(
            r"\begin{figure}\caption{One}\end{figure}\begin{figure}\end{figure}\input{extra}",
            ManuscriptCheckOptions(require_float_captions=True),
            "float_captions",
        )
        self.assertEqual(result.status, "failed")
        self.assertEqual(
            result.message, "3 of 4 floats lack a caption: main.tex:2, extra.tex:1, extra.tex:2."
        )
        self.write("many.tex", "\\begin{figure}\\end{figure}\n" * 5)
        many = self.result(
            r"\input{many}", ManuscriptCheckOptions(require_float_captions=True), "float_captions"
        )
        self.assertEqual(
            many.message,
            "5 of 5 floats lack a caption: many.tex:1, many.tex:2, many.tex:3, +2 more.",
        )

    def test_comments_literal_examples_unselected_sources_and_end_document_excluded(self) -> None:
        self.write("unused.tex", r"\documentclass{article}\begin{figure}\end{figure}")
        options = ManuscriptCheckOptions(require_float_captions=True)
        result = self.result(
            "% \\begin{figure}\\end{figure}\n"
            r"\verb|\begin{figure}\end{figure}|"
            r"\end{document}\begin{figure}\end{figure}",
            options,
            "float_captions",
        )
        self.assertEqual(result.status, "passed")
        self.assertFalse(result.details["floats"])

    def test_malformed_and_ambiguous_floats_are_inconclusive(self) -> None:
        options = ManuscriptCheckOptions(require_float_captions=True, require_float_references=True)
        for body in (
            r"\begin{figure}Unclosed.",
            r"\begin{figure}\begin{table}\end{figure}\end{table}",
        ):
            with self.subTest(body=body):
                results = self.results(body, options)
                references = next(
                    item for item in results if item.rule.endswith("float_references")
                )
                self.assertEqual(references.status, "inconclusive")
        # Duplicate labels make reference attribution ambiguous, but with no
        # reference anywhere in the document the absence itself is confirmed.
        duplicated = self.result(
            r"\begin{figure}\label{same}\end{figure}\begin{figure}\label{same}\end{figure}",
            options,
            "float_references",
        )
        self.assertEqual(duplicated.status, "failed")
        self.assertIn("ambiguous reference targets", str(duplicated.details["uncertainty"]))

    def test_input_cycle_missing_input_and_bounds_are_inconclusive(self) -> None:
        options = ManuscriptCheckOptions(require_float_captions=True)
        for body in (r"\input{main}", r"\input{missing}"):
            result = self.result(body, options, "float_captions")
            self.assertEqual(result.status, "inconclusive")
        with patch("latexprep.manuscript_checks.MAX_TOTAL_SOURCE_BYTES", 16):
            result = self.result("Text.", options, "float_captions")
        self.assertEqual(result.status, "inconclusive")

    def test_metadata_reader_returns_only_unique_supported_literal_values(self) -> None:
        self.write(
            "main.tex", r"\documentclass{article}\title{A \emph{clear} title}\author{Jane Doe}"
        )
        values, reasons = read_literal_metadata(self.root, "main.tex")
        self.assertEqual(values, {"title": "A clear title", "author": "Jane Doe"})
        self.assertEqual(reasons, [])
        self.write("main.tex", r"\documentclass{article}\title{First}\title{Second}")
        values, reasons = read_literal_metadata(self.root, "main.tex")
        self.assertNotIn("title", values)
        self.assertTrue(reasons)

    def test_metadata_reader_scopes_ambiguity_to_requested_fields(self) -> None:
        self.write(
            "main.tex",
            r"\documentclass{article}\title{A paper}\author{Alice}\author{Bob}",
        )
        values, reasons = read_literal_metadata(self.root, "main.tex", ("title",))
        self.assertEqual(values, {"title": "A paper"})
        self.assertEqual(reasons, [])
        values, reasons = read_literal_metadata(self.root, "main.tex", ("author",))
        self.assertEqual(values, {})
        self.assertEqual(
            reasons, ["multiple author declarations have unresolved combination semantics"]
        )
        values, reasons = read_literal_metadata(self.root, "main.tex")
        self.assertEqual(values, {"title": "A paper"})
        self.assertTrue(reasons)

    def test_metadata_reader_preserves_graph_uncertainty_for_selected_fields(self) -> None:
        self.write("main.tex", r"\documentclass{article}\title{A paper}\input{missing}")
        values, reasons = read_literal_metadata(self.root, "main.tex", ("title",))
        self.assertEqual(values, {"title": "A paper"})
        self.assertTrue(reasons)

    def test_duplicate_metadata_empty_label_and_multi_reference_order(self) -> None:
        metadata = self.result(
            r"\title{Study}\title{}",
            ManuscriptCheckOptions(required_metadata=("title",)),
            "required_metadata",
        )
        self.assertEqual(metadata.status, "inconclusive")
        labels = self.result(
            r"\begin{figure}\label{}\end{figure}",
            ManuscriptCheckOptions(require_float_labels=True),
            "float_labels",
        )
        self.assertEqual(labels.status, "failed")
        ordered = self.result(
            r"See \cref{fig:b,fig:a}."
            r"\begin{figure}\label{fig:a}\end{figure}"
            r"\begin{figure}\label{fig:b}\end{figure}",
            ManuscriptCheckOptions(check_float_reference_order=True),
            "float_reference_order",
        )
        self.assertEqual(ordered.status, "failed")

    def test_other_supported_citations_and_negative_register_spacing(self) -> None:
        result = self.result(
            r"\abstract{Findings \autocite{key}.}",
            ManuscriptCheckOptions(forbid_abstract_citations=True),
            "abstract_citations",
        )
        self.assertEqual(result.status, "failed")
        result = self.result(
            r"\vspace{-\baselineskip}",
            ManuscriptCheckOptions(check_layout_manipulation=True),
            "layout_manipulation",
        )
        self.assertEqual(result.status, "failed")

    def test_no_selected_main_and_nested_abstracts_are_inconclusive(self) -> None:
        options = ManuscriptCheckOptions(forbid_abstract_citations=True)
        self.write("first.tex", r"\documentclass{article}")
        self.write("second.tex", r"\documentclass{article}")
        results = check_manuscript_details(self.root, None, options)
        self.assertEqual(results[0].status, "inconclusive")
        result = self.result(
            r"\begin{abstract}Outer\begin{abstract}Inner\end{abstract}\end{abstract}",
            options,
            "abstract_citations",
        )
        self.assertEqual(result.status, "inconclusive")

    def test_input_terminators_and_trailing_groups_preserve_source_evidence(self) -> None:
        self.write("body.tex", "Some text.\n")
        bare = self.result(
            r"\input body\subsection{Deep}More text.",
            ManuscriptCheckOptions(max_heading_depth=1),
            "heading_depth",
        )
        self.assertEqual(bare.status, "failed")
        grouped = self.result(
            r"\input{body}{\generatedcontent}",
            ManuscriptCheckOptions(require_float_captions=True),
            "float_captions",
        )
        self.assertEqual(grouped.status, "inconclusive")

    def test_duplicate_label_outside_float_prevents_reference_target_claim(self) -> None:
        result = self.result(
            r"\section{Results}\label{fig:x}See \ref{fig:x}."
            r"\begin{figure}\caption{Results}\label{fig:x}\end{figure}",
            ManuscriptCheckOptions(require_float_references=True),
            "float_references",
        )
        self.assertEqual(result.status, "inconclusive")

    def test_defaults_validation_and_immutability(self) -> None:
        self.assertEqual(self.results("Text.", ManuscriptCheckOptions()), [])
        for update in (
            {"check_orcid": 1},
            {"allowed_packages": ["x"]},
            {"allowed_packages": ("a,b",)},
            {"abstract_abbreviation_policy": "automatic"},
            {"max_heading_depth": True},
            {"max_heading_depth": 6},
            {"required_metadata": ("publisher",)},
            {"required_declarations": ("A", "A")},
        ):
            with self.subTest(update=update), self.assertRaises(PreparationError):
                replace(ManuscriptCheckOptions(), **update)
        options = ManuscriptCheckOptions()
        attribute = "check_orcid"
        with self.assertRaises(FrozenInstanceError):
            setattr(options, attribute, True)


if __name__ == "__main__":
    unittest.main()
