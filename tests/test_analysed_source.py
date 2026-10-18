"""One source analysis per project root and main file: built once, shared, never changed.

The checks of a job read the same literal graph and expanded document. These tests show
that each is built once however many checks (and threads) ask for it, that a check gives
the same result with a shared analysis as with its own, that a shared analysis is only
used for the root and main file it describes, and that a whole job builds one graph per
root and main file.
"""

from __future__ import annotations

import copy
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock, patch

from latexprep.analysed_source import AnalysedSource, AnalysedSourceCache, reuse_or_analyse
from latexprep.author_records import AuthorRecordOptions, check_author_records
from latexprep.bbl_checks import BblOptions
from latexprep.bibliography_checks import check_bibliography_coverage_and_fields
from latexprep.bibliography_integrity import check_bibliography_integrity
from latexprep.config import Settings
from latexprep.core import run_job
from latexprep.expanded_document import expand_document
from latexprep.job_context import JobRequest, pdf_check_settings
from latexprep.manuscript import ManuscriptOptions, check_manuscript
from latexprep.manuscript_check_options import ManuscriptCheckOptions
from latexprep.manuscript_checks import check_manuscript_policies
from latexprep.manuscript_metadata_checks import read_literal_metadata
from latexprep.online_inputs import collect_inputs
from latexprep.online_options import OnlineOptions
from latexprep.pdf_check_options import PdfCheckOptions
from latexprep.pdf_check_pipeline import inspect_configured_pdf_checks
from latexprep.resource_budget import ResourceBudget
from latexprep.runtime import ToolRunner
from latexprep.source_analysis import analyze_sources
from latexprep.source_graph import SourceGraph
from latexprep.submission_checks import check_submission
from latexprep.submission_options import SubmissionOptions
from latexprep.submission_readiness_checks import check_submission_readiness
from tests.support import fake_build_result, write_project

PAPER = {
    "main.tex": (
        "\\documentclass{article}\n\\usepackage{graphicx}\n"
        "\\title{A paper}\\author{Ada}\\email{ada@example.org}\n"
        "\\begin{document}\n\\begin{abstract}Some words.\\end{abstract}\n"
        "\\input{sections/intro}\n\\input{sections/results}\n"
        "\\bibliographystyle{plain}\\bibliography{references}\n\\end{document}\n"
    ),
    "sections/intro.tex": "\\section{Introduction}\nSee \\cite{known} and \\cite{missing}.\n",
    "sections/results.tex": (
        "\\section{Results}\n\\begin{figure}\\includegraphics{figures/plot.pdf}"
        "\\caption{Plot}\\label{fig:plot}\\end{figure}\nFigure~\\ref{fig:plot}.\n"
    ),
    "figures/plot.pdf": b"%PDF-1.4\n",
    "figures/unused.png": b"\x89PNG",
    "references.bib": (
        "@article{known, title={A reference title}, author={Smith, Ada}, year={2024}, "
        "journal={Example}}\n@article{spare, title={Spare}, author={Doe, Jo}, year={2020}}\n"
        "@article{spare, title={Spare again}, author={Doe, Jo}, year={2021}}\n"
    ),
}
# The same paper without a finding that blocks preparation.
CLEAN_PAPER = {
    **PAPER,
    "main.tex": PAPER["main.tex"].replace("\\email{ada", "\\affiliation{Lab}\\email{ada"),
    "sections/intro.tex": "\\section{Introduction}\nSee \\cite{known}.\n",
    "references.bib": (
        "@article{known, title={A reference title}, author={Smith, Ada}, year={2024}, "
        "journal={Example}}\n"
    ),
}
del CLEAN_PAPER["figures/unused.png"]


class _CountingGraphBuilds:
    """Counts ``SourceGraph.build`` calls while patched in, and keeps what each was asked."""

    def __init__(self, delay: float = 0.0) -> None:
        self.calls: list[tuple[Path, str | None, str]] = []
        self._real = SourceGraph.build
        self._delay = delay
        self._lock = threading.Lock()
        self._patch = patch.object(SourceGraph, "build", side_effect=self._build)

    def _build(self, root, main, *, scope="project"):
        with self._lock:
            self.calls.append((root, main, scope))
        time.sleep(self._delay)
        return self._real(root, main, scope=scope)

    def __enter__(self) -> _CountingGraphBuilds:
        self._patch.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._patch.stop()

    def project_scope(self) -> list[tuple[Path, str | None]]:
        return [(root, main) for root, main, scope in self.calls if scope == "project"]


class AnalysedSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = write_project(Path(self.temporary.name).resolve() / "paper", PAPER)

    def test_graph_and_document_are_built_lazily_and_only_once(self) -> None:
        with (
            _CountingGraphBuilds() as builds,
            patch(
                "latexprep.analysed_source.expand_document", side_effect=expand_document
            ) as expansion,
        ):
            analysed = AnalysedSource(self.root, "main.tex")
            self.assertEqual((builds.calls, expansion.call_count), ([], 0))
            graph = analysed.graph
            self.assertIs(analysed.graph, graph)
            document = analysed.expanded
            self.assertIs(analysed.expanded, document)
            self.assertIs(document.graph, graph)
            self.assertEqual(builds.calls, [(self.root, "main.tex", "project")])
            self.assertEqual(expansion.call_count, 1)

    def test_the_graph_alone_never_expands_the_document(self) -> None:
        with patch("latexprep.analysed_source.expand_document", side_effect=AssertionError):
            self.assertEqual(AnalysedSource(self.root, "main.tex").graph.main, "main.tex")

    def test_threads_asking_together_share_one_build(self) -> None:
        analysed = AnalysedSource(self.root, "main.tex")
        start = threading.Barrier(8)

        def ask(index: int):
            start.wait()
            return analysed.expanded if index % 2 else analysed.graph

        with _CountingGraphBuilds(delay=0.05) as builds, ThreadPoolExecutor(8) as pool:
            results = list(pool.map(ask, range(8)))
        self.assertEqual(len(builds.calls), 1)
        graphs = {id(getattr(result, "graph", result)) for result in results}
        self.assertEqual(len(graphs), 1)

    def test_a_failed_build_is_not_remembered(self) -> None:
        analysed = AnalysedSource(self.root, "main.tex")
        with patch.object(SourceGraph, "build", side_effect=OSError("unreadable")):
            with self.assertRaises(OSError):
                self.assertIsNotNone(analysed.graph)
        self.assertEqual(analysed.graph.main, "main.tex")

    def test_a_shared_analysis_is_refused_for_another_root_or_main_file(self) -> None:
        analysed = AnalysedSource(self.root, "main.tex")
        self.assertIs(reuse_or_analyse(self.root, "main.tex", analysed), analysed)
        fresh = reuse_or_analyse(self.root, "main.tex", None)
        self.assertIsNot(fresh, analysed)
        self.assertTrue(fresh.covers(self.root, "main.tex"))
        other_root = self.root.parent / "elsewhere"
        for root, main in ((self.root, None), (self.root, "other.tex"), (other_root, "main.tex")):
            with (
                self.subTest(root=root.name, main=main),
                self.assertRaisesRegex(ValueError, "Shared source analysis"),
            ):
                reuse_or_analyse(root, main, analysed)
        with self.assertRaises(ValueError):
            check_author_records(
                self.root,
                "other.tex",
                AuthorRecordOptions(required_author_fields=("email",)),
                analysed=analysed,
            )

    def test_the_cache_returns_one_analysis_per_root_and_main_file_until_cleared(self) -> None:
        cache = AnalysedSourceCache()
        first = cache.get(self.root, "main.tex")
        self.assertIs(cache.get(self.root, "main.tex"), first)
        self.assertIsNot(cache.get(self.root, None), first)
        self.assertIsNot(cache.get(self.root.parent, "main.tex"), first)
        self.assertEqual(first.graph.main, "main.tex")
        cache.clear()
        self.assertIsNot(cache.get(self.root, "main.tex"), first)
        # An analysis handed out before the cache was cleared stays valid.
        self.assertEqual(first.graph.main, "main.tex")

    def checks(self):
        """Each check that takes a shared analysis, as a function of that analysis (or None)."""
        root = self.root
        authors = AuthorRecordOptions(required_author_fields=("email", "affiliation"))
        return {
            "source analysis": lambda a: analyze_sources(root, "main.tex", analysed=a),
            "bibliography integrity": lambda a: check_bibliography_integrity(
                root, "main.tex", analysed=a
            ),
            "bibliography coverage": lambda a: check_bibliography_coverage_and_fields(
                root, "main.tex", analysed=a
            ),
            "manuscript": lambda a: check_manuscript(
                root, "main.tex", ManuscriptOptions(require_abstract=True), analysed=a
            ),
            "manuscript policies": lambda a: check_manuscript_policies(
                root,
                "main.tex",
                ManuscriptCheckOptions(required_metadata=("title", "author")),
                analysed=a,
            ),
            "author records": lambda a: check_author_records(root, "main.tex", authors, analysed=a),
            "literal metadata": lambda a: read_literal_metadata(
                root, "main.tex", ("title",), analysed=a
            ),
            "submission readiness": lambda a: check_submission_readiness(
                root, "main.tex", BblOptions(), required_color_space="rgb", analysed=a
            ),
            "unused assets": lambda a: check_submission(
                root, "main.tex", SubmissionOptions(report_unused_assets=True), analysed=a
            ),
            "online inputs": lambda a: collect_inputs(
                root, "main.tex", OnlineOptions(online=True, online_metadata=True), analysed=a
            ),
        }

    def test_every_check_gives_the_same_result_with_a_shared_analysis(self) -> None:
        analysed = AnalysedSource(self.root, "main.tex")
        for name, run in self.checks().items():
            with self.subTest(check=name):
                own = run(None)
                self.assertEqual(run(analysed), own)
                self.assertTrue(own, f"{name} should report something on this project")

    def test_a_main_file_left_to_the_project_gets_its_own_analysis(self) -> None:
        analysed = AnalysedSource(self.root, None)
        shared = analyze_sources(self.root, analysed=analysed)
        self.assertEqual(shared, analyze_sources(self.root))
        self.assertEqual(shared.main, "main.tex")
        with self.assertRaises(ValueError):
            analyze_sources(self.root, "main.tex", analysed=analysed)

    def test_checks_sharing_an_analysis_build_the_graph_once_and_do_not_change_it(self) -> None:
        analysed = AnalysedSource(self.root, "main.tex")

        def state():
            graph, document = analysed.graph, analysed.expanded
            return copy.deepcopy(
                (
                    graph.main,
                    graph.roots,
                    graph.findings,
                    graph.uncertainties,
                    graph.dependencies,
                    graph.references,
                    graph.search_arguments,
                    graph.system_references,
                    graph.visited,
                    graph.subfile_targets,
                    {name: (item.text, item.masked) for name, item in graph.sources.items()},
                    document.text,
                    document.segments,
                    document.commands,
                    document.graph_uncertainty,
                    document.presence_uncertainty,
                    document.conditionals,
                )
            )

        before = state()
        with _CountingGraphBuilds() as builds:
            for run in self.checks().values():
                run(analysed)
        self.assertEqual(builds.calls, [])
        self.assertEqual(state(), before)

    def test_a_standalone_check_builds_exactly_one_graph(self) -> None:
        with _CountingGraphBuilds() as builds:
            check_manuscript_policies(
                self.root,
                "main.tex",
                ManuscriptCheckOptions(required_metadata=("title",)),
            )
        self.assertEqual(builds.project_scope(), [(self.root, "main.tex")])

    def test_analysis_results_do_not_alias_the_shared_graph(self) -> None:
        analysed = AnalysedSource(self.root, "main.tex")
        result = analyze_sources(self.root, "main.tex", analysed=analysed)
        result.dependencies.add("injected.tex")
        result.roots.append("injected.tex")
        self.assertNotIn("injected.tex", analysed.graph.dependencies)
        self.assertNotIn("injected.tex", analysed.graph.roots)


class PdfCheckSharingTests(unittest.IsolatedAsyncioTestCase):
    async def test_source_metadata_and_figure_checks_share_one_graph(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            source = write_project(base / "paper", PAPER)
            settings = pdf_check_settings(
                Settings(
                    pdf_checks=PdfCheckOptions(require_embedded_figure_fonts=True),
                    match_source_pdf_metadata=("title",),
                )
            )

            async def run(**options):
                with (
                    patch(
                        "latexprep.pdf_check_pipeline.inspect_pdf_details",
                        new=AsyncMock(return_value=[]),
                    ),
                    patch(
                        "latexprep.pdf_check_pipeline.inspect_included_pdf_figures",
                        new=AsyncMock(return_value=[]),
                    ),
                ):
                    return await inspect_configured_pdf_checks(
                        base / "paper.pdf",
                        source,
                        "main.tex",
                        base / "work",
                        ToolRunner(),
                        settings,
                        ResourceBudget(2, 1024),
                        **options,
                    )

            with _CountingGraphBuilds() as alone:
                standalone = await run()
            shared_analysis = AnalysedSource(source, "main.tex")
            self.assertIsNotNone(shared_analysis.graph)  # built by an earlier check of the job
            with _CountingGraphBuilds() as shared:
                result = await run(analysed=shared_analysis)
            self.assertEqual(len(alone.calls), 1)
            self.assertEqual(shared.calls, [])
            self.assertEqual(result, standalone)


class JobSharingTests(unittest.IsolatedAsyncioTestCase):
    """A whole job builds one graph per project root and main file, whatever the checks."""

    def settings(self, **overrides) -> Settings:
        return Settings(
            main="main.tex",
            layout="preserve",
            jobs=3,
            manuscript=ManuscriptOptions(require_abstract=True),
            manuscript_checks=ManuscriptCheckOptions(
                required_metadata=("title", "author"), check_empty_sections=True
            ),
            author_records=AuthorRecordOptions(required_author_fields=("email", "affiliation")),
            submission_checks=SubmissionOptions(report_unused_assets=True),
            **overrides,
        )

    async def run_job_with(self, command: str, settings: Settings, source: Path, out: Path):
        request = JobRequest(command, source, settings, out if command == "prepare" else None)
        passed = AsyncMock(return_value=[])

        async def build(tree, main, work, _engine, _runner, **_options):
            # Report every project file as read, so preparation ships the whole paper.
            inputs = {
                path.relative_to(tree).as_posix() for path in tree.rglob("*") if path.is_file()
            }
            return fake_build_result(work, main, dependencies=inputs, submission_inputs=inputs)

        with (
            patch("latexprep.document_tasks.build_project", side_effect=build),
            patch("latexprep.document_tasks.inspect_pdf", new=passed),
            patch("latexprep.document_tasks.compare_pdfs", new=passed),
            patch("latexprep.pdf_check_pipeline.inspect_pdf", new=passed),
        ):
            return await run_job(request)

    def comparable(self, report):
        return sorted(repr(finding) for finding in report.findings)

    async def test_inspect_builds_one_graph_and_matches_unshared_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            source = write_project(base / "paper", PAPER)
            settings = self.settings()
            with _CountingGraphBuilds() as shared:
                report = await self.run_job_with("inspect", settings, source, base / "out")
            # Every check runs on a separate analysis, as before they shared one.
            with (
                _CountingGraphBuilds() as unshared,
                patch.object(
                    AnalysedSourceCache,
                    "get",
                    new=lambda _self, root, main: AnalysedSource(root, main),
                ),
            ):
                separate = await self.run_job_with("inspect", settings, source, base / "out")
            self.assertEqual(len(shared.project_scope()), 1)
            self.assertGreater(len(unshared.project_scope()), 3)
            self.assertEqual(self.comparable(report), self.comparable(separate))
            self.assertEqual(report.outcome, separate.outcome)

    async def test_the_bibliography_command_builds_one_graph_for_all_its_checks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            source = write_project(base / "paper", PAPER)
            with _CountingGraphBuilds() as builds:
                report = await run_job(JobRequest("bib", source, Settings(main="main.tex")))
            self.assertEqual(len(builds.project_scope()), 1)
            self.assertTrue(
                any(item.rule == "bibliography.duplicate_key" for item in report.findings)
            )

    async def test_check_builds_one_graph_for_the_imported_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            source = write_project(base / "paper", PAPER)
            with _CountingGraphBuilds() as builds:
                await self.run_job_with("check", self.settings(), source, base / "out")
            roots = builds.project_scope()
            self.assertEqual(len(roots), len(set(roots)), roots)
            self.assertEqual(len(roots), 1)

    async def test_prepare_builds_one_graph_for_each_tree_it_checks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            source = write_project(base / "paper", CLEAN_PAPER)
            with _CountingGraphBuilds() as builds:
                report = await self.run_job_with("prepare", self.settings(), source, base / "out")
            self.assertIn(report.outcome, {"passed", "passed_with_advisories"}, report.findings)
            roots = builds.project_scope()
            self.assertEqual(len(roots), len(set(roots)), roots)
            # The inputs selected for analysis, the shipped sources and the fresh extraction.
            self.assertEqual(
                sorted(root.name for root, _ in roots), ["extracted", "selected-inputs", "shipment"]
            )


if __name__ == "__main__":
    unittest.main()
