"""Opt-in live preparation of inputs in the shape authors hand them over.

A ZIP compressed on macOS, a working directory full of local build output, editor backups
and repository metadata, folder names with spaces, quotes, parentheses and accents, and
relative command-line arguments must each give the verified package a clean folder gives.
The archive must also stay the same across repeated runs, a run on a prepared output,
serial and parallel scheduling, and two jobs sharing one event loop.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any

from latexprep.config import Settings
from latexprep.core import run_job
from latexprep.job_context import JobRequest
from latexprep.package_inventory import UNUSED_REASON
from latexprep.project_cleanup import cleanup_reason
from latexprep.report import ChangeKind, Report
from latexprep.source_transform import SourceTransformOptions
from tests import SOURCE_ROOT
from tests.support import directory_contents, live_test, write_project

EPOCH = 1_700_000_000
# Generous, because other builds may share the host; no assertion depends on timing.
JOB_TIMEOUT_SECONDS = 600
# A command-line run may use the whole job deadline, plus interpreter start and reporting.
CLI_TIMEOUT_SECONDS = JOB_TIMEOUT_SECONDS + 120
# How often the temporary directory of a running command-line job is listed. A job keeps
# its workspace for its whole run, seconds at the least, so no listing interval misses it.
LISTING_INTERVAL_SECONDS = 0.05
NESTED_PAPER = SOURCE_ROOT.parent / "examples" / "nested-paper"
RELEASED = frozenset({"passed", "passed_with_advisories"})
# The exit code of each outcome that releases a bundle (docs/reports.md, "Exit codes").
RELEASED_EXIT_CODES = {0: "passed", 1: "passed_with_advisories"}
# Edits that preparing an already prepared output must not make again.
SOURCE_EDITS = frozenset({ChangeKind.REWRITE, ChangeKind.FORMAT, ChangeKind.MOVE})
WRAPPER = "paper-export"
# The magic number and filler of an AppleDouble file, as macOS writes under __MACOSX.
APPLE_DOUBLE = b"\x00\x05\x16\x07\x00\x02\x00\x00Mac OS X        "

BIBLIOGRAPHY = """@book{knuth1984,
  author = {Donald E. Knuth},
  title = {The {TeXbook}},
  year = {1984},
  publisher = {Addison-Wesley}
}
"""

INTERNAL_COMMENT = "internal: compare these numbers with the earlier draft"
PRIVATE_ASIDE = "private aside for the co-authors"
# A section with two private comments and list items that tex-fmt indents.
ANNOTATED_RESULTS = "\n".join(
    (
        r"\section{Results}",
        r"\label{sec:results}",
        f"% {INTERNAL_COMMENT}",
        "A source project can preserve its structure while a separate submission copy",
        "uses a flat directory. The underlying typesetting system is described",
        r"by Knuth~\cite{knuth1984}.",
        r"\begin{itemize}",
        r"\item The first finding.",
        rf"\item The second finding. % {PRIVATE_ASIDE}",
        r"\end{itemize}",
        "",
    )
)

CITING_MAIN = r"""\documentclass{article}
\begin{document}
\section{Introduction}\label{sec:intro}
Section~\ref{sec:intro} cites \cite{knuth1984}.
\bibliographystyle{plain}
\bibliography{refs}
\end{document}
"""

BIBLATEX_MAIN = r"""\documentclass{article}
\usepackage[backend=biber]{biblatex}
\addbibresource{refs.bib}
\begin{document}
See \cite{knuth1984}.
\printbibliography
\end{document}
"""

# The tools of the author's own build, which runs on the host outside Fledge's sandbox.
LOCAL_BUILD_TOOLS = ("latexmk", "pdflatex", "bibtex")
# What `latexmk -pdf` leaves beside main.tex, and what it writes only when configured to.
LOCAL_BUILD_OUTPUT = (
    "main.aux",
    "main.bbl",
    "main.blg",
    "main.fdb_latexmk",
    "main.fls",
    "main.log",
    "main.pdf",
)
OPTIONAL_LOCAL_BUILD_OUTPUT = ("main.synctex.gz",)
# Stale: it names a label that the current source no longer defines.
STALE_LABEL = "\\newlabel{ghost}{{9}{9}}\n"
# Repository metadata, Finder files and editor backups that an author's folder collects.
WORKING_DIRECTORY_DEBRIS: dict[str, str | bytes] = {
    ".git/HEAD": "ref: refs/heads/main\n",
    ".git/config": "[core]\n\trepositoryformatversion = 0\n",
    ".DS_Store": b"\x00\x00\x00\x01Bud1",
    "main.tex~": CITING_MAIN,
    "#main.tex#": CITING_MAIN,
    "Icon\r": b"",
}
# Files that cleanup removes from the copy, each for the reason cleanup_reason gives it.
CLEANED_DEBRIS = (
    "main.aux",
    "main.blg",
    "main.fdb_latexmk",
    "main.fls",
    "main.log",
    "main.tex~",
    "#main.tex#",
)
# Metadata that import leaves out of the snapshot. The inventory has one row for .git:
# import never reads the files inside an excluded metadata directory.
IMPORT_METADATA = (".DS_Store", ".git", "Icon\r")
# The reason the inventory records for such metadata (src/latexprep/project.py).
METADATA_REASON = "Version-control or operating-system metadata is excluded from the snapshot."
CLI_SETTINGS = f"source_date_epoch = {EPOCH}\njob_timeout_seconds = {JOB_TIMEOUT_SECONDS}\n"


def _settings(**options: Any) -> Settings:
    """Return settings for main.tex with a fixed epoch and a deadline that tolerates a loaded host.

    Args:
        **options: Further settings.

    Returns:
        The settings.
    """
    return Settings(
        main="main.tex", source_date_epoch=EPOCH, job_timeout_seconds=JOB_TIMEOUT_SECONDS, **options
    )


def _unresolved(report: Report) -> list[tuple[object, ...]]:
    """List the findings that block a release or await review, to explain a failed assertion.

    Args:
        report: The report of the job.

    Returns:
        The code, rule, severity, status, stage and message of each finding that is not
        informational and did not pass.
    """
    return [
        (item.code, item.rule, item.severity, item.status, item.details.get("stage"), item.message)
        for item in report.findings
        if item.severity != "info" and item.status != "passed"
    ]


def _finding_keys(report: Report) -> Counter[tuple[object, ...]]:
    """Count how often each finding decided what, and where, without its wording or measurements.

    Args:
        report: The report of the job.

    Returns:
        The number of findings with each code, rule, status, stage, path and line.
    """
    return Counter(
        (item.code, item.rule, item.status, item.details.get("stage"), item.path, item.line)
        for item in report.findings
    )


def _archive_files(archive: Path) -> dict[str, bytes]:
    """Read every entry of ``archive`` with its bytes."""
    with zipfile.ZipFile(archive) as packaged:
        return {name: packaged.read(name) for name in packaged.namelist()}


def _inventory_rows(report: Report) -> dict[object, dict[str, Any]]:
    """Return the rows of the report's only package inventory, by source path."""
    (inventory,) = report.package_contents
    rows = inventory["files"]
    assert isinstance(rows, list)
    return {row["source"]: row for row in rows}


async def _directories_while(task: asyncio.Task[Any], directory: Path) -> set[str]:
    """Collect the names of the directories that ``directory`` holds while ``task`` runs.

    The directory is listed until the task is done, waiting ``LISTING_INTERVAL_SECONDS``
    between listings.

    Args:
        task: The task to watch.
        directory: The directory to list.

    Returns:
        The name of every directory seen in any listing.
    """
    seen: set[str] = set()
    while not task.done():
        seen.update(entry.name for entry in directory.iterdir() if entry.is_dir())
        await asyncio.wait({task}, timeout=LISTING_INTERVAL_SECONDS)
    return seen


def _write_macos_zip(folder: Path, archive: Path) -> None:
    """Compress ``folder`` the way macOS does: inside a wrapper, with __MACOSX and .DS_Store.

    Args:
        folder: The project folder to compress.
        archive: The ZIP file to write.
    """
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
        zipped.write(folder, WRAPPER)
        for path in sorted(folder.rglob("*")):
            zipped.write(path, f"{WRAPPER}/{path.relative_to(folder).as_posix()}")
        zipped.writestr(f"{WRAPPER}/.DS_Store", b"\x00\x00\x00\x01Bud1")
        zipped.mkdir("__MACOSX")
        zipped.mkdir(f"__MACOSX/{WRAPPER}")
        zipped.writestr(f"__MACOSX/{WRAPPER}/._main.tex", APPLE_DOUBLE)


@live_test("Set FLEDGE_RUN_INTEGRATION=1 for live preparation of awkward inputs")
class InputPreparationTests(unittest.IsolatedAsyncioTestCase):
    """Real TeX, Poppler, tex-fmt, Biber and sandbox runs on inputs as authors supply them."""

    def setUp(self) -> None:
        """Create a temporary base directory."""
        temporary = tempfile.TemporaryDirectory(prefix="fledge-inputs-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()

    def copy_nested_paper(self, name: str) -> Path:
        """Copy examples/nested-paper, a BibTeX project that builds cleanly, to ``name``.

        Returns:
            The fresh copy in the base directory.
        """
        return Path(shutil.copytree(NESTED_PAPER, self.base / name))

    async def prepare(self, source: Path, settings: Settings, output_name: str) -> Report:
        """Prepare ``source`` into the new directory ``output_name``, which must be released.

        Args:
            source: The project or ZIP to prepare.
            settings: The settings of the job.
            output_name: The name of the output directory in the base directory.

        Returns:
            The report of the job.
        """
        output = self.base / output_name
        report = await run_job(JobRequest("prepare", source, settings, output))
        self.assert_published(report, output)
        return report

    def assert_published(self, report: Report, output: Path) -> None:
        """Assert that the run released its bundle, and released it into ``output``."""
        self.assertIn(report.outcome, RELEASED, _unresolved(report))
        archive = output / "submission.zip"
        self.assertEqual(report.artifacts.get("archive"), str(archive))
        self.assertTrue(archive.is_file())

    async def test_macos_zip_with_wrapper_and_metadata_packages_like_its_folder(self) -> None:
        """Check that a ZIP input gives the same submission.zip as the folder it was made from.

        Documented: the input "can also be a ZIP of the project" (quickstart), and import
        handles "Folder or ZIP; wrapper-directory handling; ... OS metadata excluded"
        (workflow reference, implemented areas). The ZIP has the shape macOS gives a
        compressed folder: a wrapper directory, AppleDouble files under __MACOSX and a
        .DS_Store. Each exclusion is reported as an import change, the field
        tests/test_project.py checks, and as an excluded row of the package inventory.
        """
        folder = self.copy_nested_paper("paper")
        archive = self.base / f"{WRAPPER}.zip"
        _write_macos_zip(folder, archive)
        settings = _settings(layout="flat")
        from_zip = await self.prepare(archive, settings, "prepared-from-zip")
        await self.prepare(folder, settings, "prepared-from-folder")
        self.assertEqual(
            (self.base / "prepared-from-zip" / "submission.zip").read_bytes(),
            (self.base / "prepared-from-folder" / "submission.zip").read_bytes(),
        )
        expected_changes = {
            ("__MACOSX", ChangeKind.EXCLUDE),
            (".DS_Store", ChangeKind.EXCLUDE),
            (f"{WRAPPER}/", ChangeKind.UNWRAP),
        }
        changes = {(change.path, change.kind) for change in from_zip.changes}
        self.assertLessEqual(expected_changes, changes)
        rows = _inventory_rows(from_zip)
        for name in ("__MACOSX", ".DS_Store"):
            self.assertEqual(rows[name]["status"], "excluded", rows[name])
            self.assertIn(METADATA_REASON, rows[name]["reasons"])

    async def test_repeated_preparation_is_deterministic_and_idempotent(self) -> None:
        """Check that repeated preparation is deterministic and idempotent.

        The same input gives the same archive, and a prepared output prepares to itself.

        Documented in the workflow reference (implemented areas): packaging writes a
        "deterministic ZIP", the flat layout is a "Deterministic mapping", and formatting
        is checked for "tex-fmt diffs/idempotence". Private-comment removal, tex-fmt and the
        flat layout all edit the copy, so preparing the published sources again must make
        none of those edits and give a byte-identical archive.
        """
        project = self.copy_nested_paper("paper")
        (project / "sections" / "results.tex").write_text(ANNOTATED_RESULTS, encoding="utf-8")
        settings = _settings(
            layout="flat",
            apply_formatting=True,
            source_transforms=SourceTransformOptions(comment_policy="private"),
        )
        first = await self.prepare(project, settings, "prepared-first")
        await self.prepare(project, settings, "prepared-second")
        first_archive = self.base / "prepared-first" / "submission.zip"
        second_archive = self.base / "prepared-second" / "submission.zip"
        first_files = _archive_files(first_archive)
        self.assert_private_comments_removed(first, first_files)
        self.assertEqual(sorted(first_files), sorted(_archive_files(second_archive)))
        self.assertEqual(first_archive.read_bytes(), second_archive.read_bytes())
        again = await self.prepare(
            self.base / "prepared-first" / "sources", settings, "prepared-again"
        )
        repeated = [(c.path, c.kind) for c in again.changes if c.kind in SOURCE_EDITS]
        self.assertEqual(repeated, [])
        self.assertEqual(
            (self.base / "prepared-again" / "submission.zip").read_bytes(),
            first_archive.read_bytes(),
        )

    def assert_private_comments_removed(self, report: Report, shipped: dict[str, bytes]) -> None:
        """Assert that the edits really happened, so the archive comparisons are not vacuous.

        Comment removal rewrote the annotated section, tex-fmt reformatted it and the flat
        layout moved it. No shipped file keeps either private comment, but the section's
        own text is still shipped.

        Args:
            report: The report of the job.
            shipped: The bytes of each shipped file by archive name.
        """
        edits = {(change.path, change.kind) for change in report.changes}
        self.assertLessEqual({("sections/results.tex", kind) for kind in SOURCE_EDITS}, edits)
        text = b"\n".join(shipped.values())
        for comment in (INTERNAL_COMMENT, PRIVATE_ASIDE):
            self.assertNotIn(comment.encode(), text)
        self.assertIn(b"The second finding.", text)

    async def test_messy_working_directory_ships_only_what_the_build_needs(self) -> None:
        """Check that a messy working directory ships only what the build needs.

        Local build output, editor backups and repository metadata stay out of the bundle.

        Documented in the workflow reference ("How package contents are selected"): cleanup
        removes OS and version-control metadata, editor backups and rebuildable auxiliary
        files, never a .bbl; a main.bbl beside main.tex whose name the build regenerated is
        shipped as supplied, and the package inventory lists it under
        details.generated_bibliography. The inventory gives each excluded file its reason
        (reports.md, "What is in the submission?"), and the original project is never
        changed (quickstart).
        """
        missing = [tool for tool in LOCAL_BUILD_TOOLS if shutil.which(tool) is None]
        if missing:
            self.skipTest(f"The author's local build needs {', '.join(missing)}")
        project = write_project(
            self.base / "paper", {"main.tex": CITING_MAIN, "refs.bib": BIBLIOGRAPHY}
        )
        await self.leave_local_build_output(project)
        with (project / "main.aux").open("a", encoding="utf-8") as stale:
            stale.write(STALE_LABEL)
        write_project(project, WORKING_DIRECTORY_DEBRIS)
        originals = directory_contents(project)
        report = await self.prepare(project, _settings(layout="flat"), "prepared")
        shipped = _archive_files(self.base / "prepared" / "submission.zip")
        self.assertEqual(sorted(shipped), ["main.bbl", "main.tex", "refs.bib"])
        self.assertEqual(shipped["main.bbl"], originals["main.bbl"])
        selections = [
            item for item in report.findings if item.code == "PKG302" and item.status == "passed"
        ]
        self.assertTrue(selections, _unresolved(report))
        for selection in selections:
            self.assertIn("main.bbl", selection.details.get("generated_bibliography", []))
        self.assert_excluded_for_their_reasons(report)
        self.assertEqual(directory_contents(project), originals)

    def assert_excluded_for_their_reasons(self, report: Report) -> None:
        """Assert that every leftover file is excluded and its inventory row names the rule."""
        expected = {
            **{name: cleanup_reason(name) for name in CLEANED_DEBRIS},
            # A PDF is not debris: it is left out only because the build never reads it.
            "main.pdf": UNUSED_REASON,
            **dict.fromkeys(IMPORT_METADATA, METADATA_REASON),
        }
        rows = _inventory_rows(report)
        for name, reason in expected.items():
            with self.subTest(excluded=name):
                row = rows.get(name, {})
                self.assertEqual(row.get("status"), "excluded", row)
                self.assertIn(reason, row.get("reasons", []), row)
        self.assertEqual([name for name in rows if str(name).startswith(".git/")], [])

    async def leave_local_build_output(self, project: Path) -> None:
        """Build a copy with latexmk as the author did, and leave its output beside main.tex.

        -norc keeps a personal latexmkrc, one that sets an output directory for instance,
        from moving the files this test expects.

        Asserts that latexmk succeeded and wrote the expected build output.

        Args:
            project: The project that receives the build output.
        """
        scratch = Path(shutil.copytree(project, self.base / "local-build"))
        completed = await asyncio.to_thread(
            subprocess.run,
            ["latexmk", "-norc", "-pdf", "-interaction=nonstopmode", "main.tex"],
            cwd=scratch,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=JOB_TIMEOUT_SECONDS,
            check=False,
        )
        missing = [name for name in LOCAL_BUILD_OUTPUT if not (scratch / name).is_file()]
        self.assertEqual((completed.returncode, missing), (0, []), completed.stdout[-3000:])
        for name in (*LOCAL_BUILD_OUTPUT, *OPTIONAL_LOCAL_BUILD_OUTPUT):
            if (scratch / name).is_file():
                shutil.copyfile(scratch / name, project / name)

    async def test_awkward_paths_and_relative_command_line_arguments(self) -> None:
        """Check preparation with awkward paths and relative command-line arguments.

        Spaces, quotes, parentheses and accents in paths, and relative command-line paths.

        Documented: INPUT is "a source folder or ZIP" and --output a "New directory outside
        the input" (command help; quickstart), so any folder name is accepted, and a sibling
        output named relative to a working directory inside the project is allowed. The
        input is copied into the job's workspace in the system temporary directory, so its
        name never reaches a tool. The command-line runs therefore set TMPDIR to a directory
        named with spaces, parentheses and accents, and each run must create its workspace
        there and remove it at the end: the workspace paths in the sandbox profile, the TeX
        recorder trace and the latexmk database then carry those characters. The name has
        no double quote, because latexmk refuses any path that contains one.
        """
        project = self.copy_nested_paper('My Paper (draft) "v2" ünï')
        await self.prepare(project, _settings(), 'Prepared (final) "v2" ünï')
        originals = directory_contents(project)
        temporary = self.base / "job space (tmp) ünï"
        temporary.mkdir()
        (self.base / "cli-settings.toml").write_text(CLI_SETTINGS, encoding="utf-8")
        await self.run_cli(project, temporary, "check", ".", "--main", "main.tex")
        prepared = await self.run_cli(
            project,
            temporary,
            *("prepare", ".", "--main", "main.tex", "--output", "../prepared-relative"),
        )
        output = self.base / "prepared-relative"
        self.assertEqual(prepared["artifacts"]["archive"], str(output / "submission.zip"))
        self.assertLessEqual(
            {"sources", "submission.zip", "manuscript.pdf", "report.json"},
            {path.name for path in output.iterdir()},
        )
        self.assertEqual(directory_contents(project), originals)

    async def run_cli(self, project: Path, temporary: Path, *arguments: str) -> dict[str, Any]:
        """Run ``python -m latexprep`` from inside ``project`` with relative paths.

        ``temporary`` is the run's TMPDIR: a job workspace must appear in it while the run
        lasts and be gone afterwards. The settings file beside the project fixes the epoch
        and deadline that the in-process runs use.

        Args:
            project: The project directory, the working directory of the command.
            temporary: The empty directory used as TMPDIR.
            *arguments: The command and its arguments, relative to ``project``.

        Returns:
            The JSON report, which must have released.
        """
        run = asyncio.create_task(
            asyncio.to_thread(
                subprocess.run,
                [
                    *(sys.executable, "-m", "latexprep", *arguments),
                    *("--config", "../cli-settings.toml", "--json", "--quiet"),
                ],
                cwd=project,
                env={**os.environ, "PYTHONPATH": str(SOURCE_ROOT), "TMPDIR": str(temporary)},
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=CLI_TIMEOUT_SECONDS,
                check=False,
            )
        )
        workspaces = await _directories_while(run, temporary)
        completed = run.result()
        try:
            report = json.loads(completed.stdout)
        except json.JSONDecodeError:
            self.fail(f"Exit {completed.returncode} without a JSON report: {completed.stderr!r}")
        unresolved = [
            (item["code"], item["rule"], item["status"], item["message"])
            for item in report["findings"]
            if item["severity"] != "info" and item["status"] != "passed"
        ]
        self.assertIn(completed.returncode, RELEASED_EXIT_CODES, unresolved)
        self.assertEqual(report["outcome"], RELEASED_EXIT_CODES[completed.returncode])
        self.assertEqual(report["command"], arguments[0])
        self.assertTrue(workspaces, "No job workspace appeared in TMPDIR during the run")
        self.assertEqual(list(temporary.iterdir()), [])
        return report

    async def test_serial_parallel_and_concurrent_jobs_give_the_same_results(self) -> None:
        """Check that serial, parallel and concurrent jobs give the same results.

        The number of parallel slots changes scheduling, never the findings or the archive.

        Documented in the workflow reference ("Execution and resource limits"): --jobs sets
        the shared CPU slots and --jobs 1 runs everything serially, while the build gates
        stay ordered and publication waits for every required result; the ZIP is
        deterministic. Each finding must be reported as often in both modes. Two jobs
        started together in one event loop, a BibTeX paper and a Biber paper, share the
        process, the loop and the host's tools. Each job has its own tool runner and
        workspace, so the sealed Biber copy (on macOS) and the cached tool versions belong
        to the job that made them; each must pass and publish only its own package into
        its own output.
        """
        reports = {}
        for jobs in (1, 4):
            project = self.copy_nested_paper(f"paper-jobs-{jobs}")
            reports[jobs] = await self.prepare(
                project, _settings(layout="flat", jobs=jobs), f"prepared-jobs-{jobs}"
            )
        serial, parallel = _finding_keys(reports[1]), _finding_keys(reports[4])
        self.assertEqual(
            serial, parallel, {"serial only": serial - parallel, "parallel only": parallel - serial}
        )
        self.assertEqual(
            (self.base / "prepared-jobs-1" / "submission.zip").read_bytes(),
            (self.base / "prepared-jobs-4" / "submission.zip").read_bytes(),
        )
        await self.assert_concurrent_jobs_stay_apart()

    async def assert_concurrent_jobs_stay_apart(self) -> None:
        """Assert that a BibTeX job and a Biber job started together each publish their package."""
        projects = (
            self.copy_nested_paper("concurrent-bibtex"),
            write_project(
                self.base / "concurrent-biber",
                {"main.tex": BIBLATEX_MAIN, "refs.bib": BIBLIOGRAPHY},
            ),
        )
        settings = (_settings(), _settings(bibliography_backend="biber"))
        outputs = (self.base / "prepared-bibtex", self.base / "prepared-biber")
        reports = await asyncio.gather(
            *(
                run_job(JobRequest("prepare", project, chosen, output))
                for project, chosen, output in zip(projects, settings, outputs, strict=True)
            )
        )
        for project, output, report in zip(projects, outputs, reports, strict=True):
            self.assert_published(report, output)
            with zipfile.ZipFile(output / "submission.zip") as packaged:
                self.assertEqual(packaged.read("main.tex"), (project / "main.tex").read_bytes())
        # The second job really ran Biber; without it the pair would not be the one described.
        backends = [
            item.details.get("observed_backends")
            for item in reports[1].findings
            if item.code == "BLD201"
        ]
        self.assertIn(["biber"], backends)


if __name__ == "__main__":
    unittest.main()
