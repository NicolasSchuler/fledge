"""The CI safeguards stay in place: coverage gate, scheduled-failure alert, release tests.

CI was red for three weeks without anyone noticing, and the release workflow once built and
published without running a test. Each test reads the workflow files or ``pyproject.toml`` and
fails when a safeguard that was added to prevent that is removed or loosened by accident.
"""

from __future__ import annotations

import re
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
JOB_HEADING = re.compile(r"^  ([a-z][a-z0-9-]*):\s*$", re.MULTILINE)


def jobs_of(workflow: str) -> dict[str, str]:
    """Split the text of a workflow file into its jobs.

    Args:
        workflow: The file name in ``.github/workflows``.

    Returns:
        The text of each job, by job id, from its heading to the next job.
    """
    text = (WORKFLOWS / workflow).read_text(encoding="utf-8")
    text = text[text.index("\njobs:\n") + len("\njobs:\n") :]
    headings = list(JOB_HEADING.finditer(text))
    return {
        heading.group(1): text[
            heading.end() : headings[index + 1].start() if index + 1 < len(headings) else None
        ]
        for index, heading in enumerate(headings)
    }


def needs_of(job: str) -> set[str]:
    """Return the jobs that the text of ``job`` lists under ``needs``, as a list or a name."""
    listed = re.search(r"^    needs:[ \t]*(\S.*)?\n((?:      - .*\n)*)", job, re.MULTILINE)
    if listed is None:
        return set()
    names = {line.removeprefix("      - ").strip() for line in listed.group(2).splitlines()}
    inline = (listed.group(1) or "").strip().strip("[]")
    return names | {name.strip() for name in inline.split(",") if name.strip()}


@unittest.skipUnless(WORKFLOWS.is_dir(), "the workflow files are not part of the source archive")
class WorkflowSafeguardTests(unittest.TestCase):
    """What the Tests and Release workflows must keep doing."""

    def test_the_release_builds_only_after_its_tests_pass_and_publishes_only_after_the_build(self):
        jobs = jobs_of("release.yml")
        self.assertIn("pytest tests", jobs["test"])
        self.assertEqual(needs_of(jobs["build"]), {"test"})
        self.assertEqual(needs_of(jobs["publish"]), {"build"})

    def test_a_failed_scheduled_run_opens_an_issue_from_the_only_job_that_may_write_issues(self):
        jobs = jobs_of("tests.yml")
        alert = jobs["alert-on-scheduled-failure"]
        self.assertIn("github.event_name == 'schedule'", alert)
        writes_issues = re.compile(r"^      issues: write$", re.MULTILINE)
        self.assertRegex(alert, writes_issues)
        self.assertNotIn("actions/checkout", alert)
        self.assertEqual(
            [name for name, text in jobs.items() if writes_issues.search(text)],
            ["alert-on-scheduled-failure"],
        )
        self.assertIn("gh issue create", alert)
        self.assertIn("gh issue comment", alert)

    def test_the_alert_job_waits_for_every_other_job_of_the_workflow(self):
        jobs = jobs_of("tests.yml")
        others = set(jobs) - {"alert-on-scheduled-failure"}
        self.assertEqual(needs_of(jobs["alert-on-scheduled-failure"]), others)

    def test_the_unit_tests_measure_coverage_in_exactly_one_matrix_cell(self):
        unit = jobs_of("tests.yml")["unit"]
        self.assertEqual(unit.count("coverage: true"), 1)
        self.assertEqual(len(re.findall(r"python -m pytest tests .*--cov\b", unit)), 1)
        # The plain run, the gated run and the summary are the three steps that depend on it.
        self.assertEqual(unit.count("matrix.coverage"), 3)

    def test_the_oldest_declared_dependencies_run_the_unit_tests(self):
        job = jobs_of("tests.yml")["lowest-dependencies"]
        self.assertIn("--resolution lowest-direct", job)
        self.assertIn('python-version: "3.11"', job)
        self.assertIn("-m pytest tests", job)

    def test_the_shared_test_support_modules_are_type_checked_with_the_package(self):
        lint = jobs_of("tests.yml")["lint"]
        for module in ("tests/support.py", "tests/fakes.py", "tests/pdf_fakes.py"):
            self.assertIn(module, lint)

    def test_the_linux_live_tests_run_on_ubuntu_packages_and_on_the_current_tex_live(self):
        job = jobs_of("tests.yml")["live-linux"]
        self.assertRegex(job, r"texlive: \[ubuntu-packages, current-from-tug\]")
        self.assertIn("matrix.texlive == 'ubuntu-packages'", job)
        self.assertIn("matrix.texlive == 'current-from-tug'", job)
        self.assertIn("tests/test_runtime_backends.py", job)

    def test_the_current_tex_live_is_a_small_scheme_installed_from_a_verified_installer(self):
        job = jobs_of("tests.yml")["live-linux"]
        install = job[job.index("name: Install the current TeX Live from tug.org") :]
        install = install[: install.index("- name: Install tex-fmt")]
        self.assertIn("timeout-minutes: 25", install)
        self.assertIn("selected_scheme scheme-small", install)
        self.assertNotIn("scheme-full", install)
        self.assertIn("sha512sum --check --strict", install)
        self.assertIn("gpg --batch --status-fd 1", install)
        self.assertRegex(install, r"TEXLIVE_SIGNING_KEY: [0-9A-F]{40}\n")
        self.assertLess(install.index("VALIDSIG"), install.index("install-tl --no-interaction"))
        self.assertIn("tlmgr --repository", install)
        for package in ("latexmk", "biber", "cm-super", "tex-gyre", "collection-luatex", "bbm"):
            self.assertIn(package, install)


class CoverageConfigurationTests(unittest.TestCase):
    """``pyproject.toml`` measures branches and child processes and gates the total."""

    def setUp(self) -> None:
        """Read ``pyproject.toml``."""
        self.configuration = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    def test_coverage_is_a_development_dependency(self):
        development = self.configuration["project"]["optional-dependencies"]["dev"]
        self.assertTrue(any(item.startswith("pytest-cov") for item in development), development)

    def test_branches_of_the_package_and_of_child_processes_are_measured(self):
        run = self.configuration["tool"]["coverage"]["run"]
        self.assertIs(run["branch"], True)
        self.assertEqual(run["source"], ["latexprep"])
        self.assertIn("subprocess", run["patch"])
        self.assertIs(run["parallel"], True)

    def test_the_gate_is_a_ratchet_set_a_little_below_full_coverage(self):
        gate = self.configuration["tool"]["coverage"]["report"]["fail_under"]
        self.assertGreaterEqual(gate, 90)
        self.assertLess(gate, 100)

    def test_the_click_floor_leaves_out_the_releases_that_break_the_tests_or_the_settings(self):
        dependencies = self.configuration["project"]["dependencies"]
        (click,) = [item for item in dependencies if item.startswith("click")]
        self.assertIn("!=8.2.2", click)
        self.assertRegex(click, r">=8\.[2-9]")

    def test_invalid_escape_sequences_are_a_lint_error(self):
        selected = self.configuration["tool"]["ruff"]["lint"]["select"]
        self.assertIn("W605", selected)


if __name__ == "__main__":
    unittest.main()
