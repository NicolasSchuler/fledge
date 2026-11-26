"""The version has one definition, the source archive ships what tests read, and CI stays honest."""

from __future__ import annotations

import fnmatch
import importlib.util
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
RELEASE_CHECKS = ROOT / "scripts" / "release_checks.py"


def _release_checks():
    """Load ``scripts/release_checks.py`` as a module.

    Returns:
        The loaded module.
    """
    spec = importlib.util.spec_from_file_location("release_checks", RELEASE_CHECKS)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_tree(root: Path, pyproject: str, version: str = "1.2.3") -> None:
    """Write a minimal source tree with ``pyproject.toml`` and the package's version.

    Args:
        root: The folder of the tree.
        pyproject: The text of ``pyproject.toml``.
        version: The ``__version__`` that ``src/latexprep/__init__.py`` defines.
    """
    (root / "src" / "latexprep").mkdir(parents=True, exist_ok=True)
    (root / "src" / "latexprep" / "__init__.py").write_text(f'__version__ = "{version}"\n')
    (root / "pyproject.toml").write_text(pyproject)


DYNAMIC_PYPROJECT = """[project]
name = "fledge-latex"
dynamic = ["version"]

[tool.setuptools.dynamic]
version = { attr = "latexprep.__version__" }
"""


class VersionTests(unittest.TestCase):
    """One version definition, a release number with a changelog entry, and the release check."""

    def test_the_version_is_defined_once_as_the_module_attribute(self) -> None:
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertNotIn("version", pyproject["project"])
        self.assertIn("version", pyproject["project"]["dynamic"])
        self.assertEqual(
            pyproject["tool"]["setuptools"]["dynamic"]["version"],
            {"attr": "latexprep.__version__"},
        )
        self.assertEqual(_release_checks().version_problems(ROOT), [])

    def test_the_declared_version_is_a_release_number_with_a_changelog_entry(self) -> None:
        version = _release_checks().declared_version(ROOT)
        self.assertRegex(version, r"^\d+\.\d+\.\d+$")
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertRegex(changelog, rf"(?m)^## \[{re.escape(version)}\]")

    def test_the_release_check_rejects_a_second_definition_a_misnamed_distribution_and_a_wrong_tag(
        self,
    ) -> None:
        checks = _release_checks()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_tree(root, DYNAMIC_PYPROJECT)
            self.assertEqual(checks.version_problems(root, "v1.2.3"), [])
            (wrong,) = checks.version_problems(root, "v1.2.4")
            self.assertIn("does not match", wrong)
            self.assertIn("'v1.2.3'", wrong)
            _write_tree(root, DYNAMIC_PYPROJECT.replace("fledge-latex", "fledge"))
            (renamed,) = checks.version_problems(root)
            self.assertIn("not 'fledge-latex'", renamed)
            _write_tree(
                root, DYNAMIC_PYPROJECT.replace('dynamic = ["version"]', 'version = "1.2.3"')
            )
            problems = checks.version_problems(root)
            self.assertTrue(any("literal version" in problem for problem in problems), problems)


def _write_sdist(dist: Path, name: str, members: dict[str, str]) -> None:
    """Write a gzipped tar archive into ``dist``.

    Args:
        dist: The folder that receives the archive.
        name: The archive's file name.
        members: The text of each member, by its path inside the archive.
    """
    dist.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as staging, tarfile.open(dist / name, "w:gz") as archive:
        for member, text in members.items():
            path = Path(staging) / member
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            archive.add(path, arcname=member)


class SourceArchiveCheckTests(unittest.TestCase):
    """``release_checks.py sdist`` runs the docs and packaging tests from the extracted archive.

    ``subprocess.run`` is replaced, so no environment is created and nothing is downloaded.
    """

    def run_check(self, dist: Path, tests_exit_code: int = 0) -> tuple[int, list[dict]]:
        """Run the sdist check on ``dist`` with every child process faked.

        Args:
            dist: The folder holding the built distributions.
            tests_exit_code: The exit code the faked pytest run returns.

        Returns:
            The check's exit code and, for each child, its command, working directory and
            whether that directory held the extracted ``pyproject.toml`` at the time.
        """
        checks = _release_checks()
        calls: list[dict] = []

        def fake_run(command, cwd=None, **_options):
            """Record ``command`` and pretend it ran."""
            calls.append(
                {
                    "command": [str(part) for part in command],
                    "cwd": cwd,
                    "extracted": cwd is not None and (Path(cwd) / "pyproject.toml").is_file(),
                }
            )
            exit_code = tests_exit_code if "pytest" in command[1:3] else 0
            return subprocess.CompletedProcess(command, exit_code)

        with patch.object(checks.subprocess, "run", side_effect=fake_run):
            return checks.main(["sdist", str(dist)]), calls

    def test_the_tests_run_from_the_extracted_archive_in_a_fresh_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            dist = Path(directory) / "dist"
            _write_sdist(
                dist,
                "fledge_latex-1.2.3.tar.gz",
                {"fledge_latex-1.2.3/pyproject.toml": "[project]\n"},
            )
            (dist / "fledge_latex-1.2.3-py3-none-any.whl").write_text("")
            exit_code, calls = self.run_check(dist)
        self.assertEqual(exit_code, 0)
        venv, install, tests = calls
        self.assertEqual(venv["command"][1:3], ["-m", "venv"])
        python = str(Path(venv["command"][3]) / "bin" / "python")
        self.assertEqual(install["command"][:4], [python, "-m", "pip", "install"])
        source = Path(install["command"][-2])
        self.assertEqual(source.name, "fledge_latex-1.2.3")
        self.assertEqual(install["command"][-1], "pytest>=9,<10")
        test_files = ["tests/test_docs.py", "tests/test_packaging.py"]
        self.assertEqual(
            tests["command"],
            [python, "-m", "pytest", *test_files, "-q", "-p", "no:cacheprovider"],
        )
        self.assertEqual(Path(tests["cwd"]), source)
        self.assertTrue(tests["extracted"])

    def test_a_failing_test_run_fails_the_check(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            dist = Path(directory) / "dist"
            _write_sdist(dist, "project-1.0.tar.gz", {"project-1.0/pyproject.toml": ""})
            exit_code, _calls = self.run_check(dist, tests_exit_code=1)
        self.assertEqual(exit_code, 1)

    def test_the_check_needs_exactly_one_archive_with_one_top_level_directory(self) -> None:
        cases = {
            "no archive": {},
            "two archives": {
                "a-1.0.tar.gz": {"a-1.0/pyproject.toml": ""},
                "b-1.0.tar.gz": {"b-1.0/pyproject.toml": ""},
            },
            "two top-level directories": {
                "a-1.0.tar.gz": {"a-1.0/pyproject.toml": "", "b-1.0/pyproject.toml": ""}
            },
        }
        for case, archives in cases.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                dist = Path(directory) / "dist"
                dist.mkdir()
                for name, members in archives.items():
                    _write_sdist(dist, name, members)
                exit_code, calls = self.run_check(dist)
                self.assertEqual(exit_code, 1)
                self.assertEqual(calls, [])


class SourceArchiveTests(unittest.TestCase):
    """The source archive ships the scripts, golden files and changelog the tests read."""

    def manifest_patterns(self, directive: str, directory: str) -> list[str]:
        """Collect the patterns of the ``MANIFEST.in`` lines for ``directive`` and ``directory``.

        Args:
            directive: The command a line starts with, such as ``recursive-include``.
            directory: The directory the line names.

        Returns:
            The patterns of every matching line, in file order.
        """
        patterns: list[str] = []
        for line in (ROOT / "MANIFEST.in").read_text(encoding="utf-8").splitlines():
            words = line.split()
            if words[:2] == [directive, directory]:
                patterns.extend(words[2:])
        return patterns

    def test_the_source_archive_ships_every_script_the_tests_load(self) -> None:
        patterns = self.manifest_patterns("recursive-include", "scripts")
        scripts = sorted(path.name for path in (ROOT / "scripts").glob("*.py"))
        self.assertIn("generate_check_docs.py", scripts, "tests/test_docs.py loads this script")
        for name in scripts:
            with self.subTest(script=name):
                self.assertTrue(any(fnmatch.fnmatch(name, pattern) for pattern in patterns))

    def test_the_source_archive_ships_the_golden_files_the_tests_compare(self) -> None:
        patterns = self.manifest_patterns("recursive-include", "tests/golden")
        goldens = sorted(path for path in (ROOT / "tests" / "golden").rglob("*") if path.is_file())
        self.assertGreater(len(goldens), 10)
        for path in goldens:
            with self.subTest(golden=path.name):
                self.assertTrue(any(fnmatch.fnmatch(path.name, pattern) for pattern in patterns))

    def test_the_source_archive_ships_the_changelog(self) -> None:
        manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
        self.assertRegex(manifest, r"(?m)^include .*\bCHANGELOG\.md\b")


@unittest.skipUnless(WORKFLOWS.is_dir(), "the workflow files are not part of the source archive")
class WorkflowTests(unittest.TestCase):
    """Action pins, Dependabot and the documented live opt-in variable."""

    def test_every_action_is_pinned_to_a_commit(self) -> None:
        references = [
            (path.name, match.group(1), match.group(2))
            for path in sorted(WORKFLOWS.glob("*.yml"))
            for match in re.finditer(r"uses:\s*(\S+)@(\S+)", path.read_text(encoding="utf-8"))
        ]
        self.assertGreater(len(references), 10)
        for workflow, action, reference in references:
            with self.subTest(workflow=workflow, action=action):
                self.assertRegex(reference, r"^[0-9a-f]{40}$")

    def test_dependabot_keeps_the_pinned_actions_current(self) -> None:
        configuration = (ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")
        self.assertIn("package-ecosystem: github-actions", configuration)

    def test_live_jobs_use_the_documented_environment_variable(self) -> None:
        workflows = "".join(path.read_text(encoding="utf-8") for path in WORKFLOWS.glob("*.yml"))
        self.assertIn('FLEDGE_RUN_INTEGRATION: "1"', workflows)
        for legacy in ("LATEX_PREP_RUN_INTEGRATION", "LATEXPREP_RUN_SANDBOX_TESTS"):
            self.assertNotIn(legacy, workflows)


class LiveTestMarkerTests(unittest.TestCase):
    """Live tests are opt-in, selectable with ``-m live`` and gated in one place."""

    def test_the_live_marker_is_registered(self) -> None:
        configuration = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        markers = configuration["tool"]["pytest"]["ini_options"]["markers"]
        self.assertTrue(any(marker.startswith("live:") for marker in markers), markers)

    def test_only_the_support_module_reads_the_opt_in_variable(self) -> None:
        offenders = [
            path.name
            for path in sorted((ROOT / "tests").glob("test_*.py"))
            if path != Path(__file__).resolve()
            and "live_tests_enabled(" in path.read_text(encoding="utf-8")
        ]
        self.assertEqual(offenders, [], "Gate live tests with @live_test from tests/support.py.")

    def test_the_decorator_applies_the_marker_and_gates_opt_in_and_platform(self) -> None:
        if importlib.util.find_spec("pytest") is None:
            self.skipTest("the live marker belongs to pytest")
        from tests.support import live_test

        def declare(platform: str) -> type[unittest.TestCase]:
            """Declare a live test for ``platform`` under the current environment."""

            @live_test("opt-in", platform=platform)
            class Live(unittest.TestCase):
                """A stand-in for a platform-specific live test."""

            return Live

        disabled = {"FLEDGE_RUN_INTEGRATION": "0", "LATEX_PREP_RUN_INTEGRATION": "0"}
        with patch.dict(os.environ, disabled):
            skipped = declare(sys.platform)
        with patch.dict(os.environ, {"FLEDGE_RUN_INTEGRATION": "1"}):
            enabled = declare(sys.platform)
            wrong_platform = declare("darwin" if sys.platform == "linux" else "linux")
        for declared in (skipped, wrong_platform):
            self.assertTrue(declared.__unittest_skip__)
            self.assertEqual(declared.__unittest_skip_why__, "opt-in")
        self.assertFalse(getattr(enabled, "__unittest_skip__", False))
        for declared in (skipped, wrong_platform, enabled):
            self.assertEqual([mark.name for mark in declared.pytestmark], ["live"])


if __name__ == "__main__":
    unittest.main()
