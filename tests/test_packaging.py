"""The version has one definition, the source archive ships what tests read, and CI stays honest."""

from __future__ import annotations

import fnmatch
import importlib.util
import re
import tempfile
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
RELEASE_CHECKS = ROOT / "scripts" / "release_checks.py"


def _release_checks():
    spec = importlib.util.spec_from_file_location("release_checks", RELEASE_CHECKS)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_tree(root: Path, pyproject: str, version: str = "1.2.3") -> None:
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


class SourceArchiveTests(unittest.TestCase):
    def manifest_patterns(self, directive: str, directory: str) -> list[str]:
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

    def test_the_source_archive_ships_the_changelog(self) -> None:
        manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
        self.assertRegex(manifest, r"(?m)^include .*\bCHANGELOG\.md\b")


@unittest.skipUnless(WORKFLOWS.is_dir(), "the workflow files are not part of the source archive")
class WorkflowTests(unittest.TestCase):
    def workflow(self, name: str) -> str:
        return (WORKFLOWS / name).read_text(encoding="utf-8")

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

    def test_every_test_file_with_live_tests_runs_in_a_live_job(self) -> None:
        """Hand-written file lists once left three live tests out of CI unnoticed."""
        live_jobs = self.workflow("tests.yml").split("\n  live-macos:", 1)[1]
        live_files = sorted(
            path.name
            for path in (ROOT / "tests").glob("test_*.py")
            if path != Path(__file__).resolve()
            and "live_tests_enabled(" in path.read_text(encoding="utf-8")
        )
        self.assertGreater(len(live_files), 5)
        for name in live_files:
            with self.subTest(file=name):
                self.assertIn(f"tests/{name}", live_jobs)

    def test_live_jobs_use_the_documented_environment_variable(self) -> None:
        workflows = "".join(path.read_text(encoding="utf-8") for path in WORKFLOWS.glob("*.yml"))
        self.assertIn('FLEDGE_RUN_INTEGRATION: "1"', workflows)
        for legacy in ("LATEX_PREP_RUN_INTEGRATION", "LATEXPREP_RUN_SANDBOX_TESTS"):
            self.assertNotIn(legacy, workflows)

    def test_the_linux_live_job_runs_the_linux_isolation_test(self) -> None:
        linux = self.workflow("tests.yml").split("\n  live-linux:", 1)[1]
        self.assertIn("tests/test_runtime_backends.py", linux)


if __name__ == "__main__":
    unittest.main()
