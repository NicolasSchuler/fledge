"""The command-line interface: commands, request errors, unexpected failures and redaction."""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, patch

from click.testing import CliRunner

from latexprep.build_result import BuildResult
from latexprep.cli import (
    USAGE_EXIT_CODE,
    cli,
    main,
    settings_suggestion,
)
from latexprep.core import run_job
from latexprep.findings import Finding
from latexprep.redaction import scrub_local_paths
from latexprep.report import Outcome, Report
from latexprep.rule_definition import RULE_SCHEMA_VERSION
from latexprep.rules import RULES
from tests.support import ProjectTestCase, controlled_backends


class CommandTests(ProjectTestCase):
    def setUp(self):
        super().setUp()
        self.source = self.root / "project"
        self.source.mkdir()
        self.original = (
            "\\documentclass{article}\n\\begin{document}\nTODO finish this.\n\\end{document}"
        )
        (self.source / "main.tex").write_text(self.original)
        self.runner = CliRunner()

    def test_click_compact_output_identifies_problem_location_and_next_step(self):
        result = self.runner.invoke(
            cli, ["inspect", str(self.source), "--quiet", "--output-format", "compact"]
        )
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("TEX001 warning/failed main.tex:3", result.stdout)
        self.assertIn("TODO", result.stdout)
        self.assertIn("next:", result.stdout)
        self.assertNotIn("\x1b", result.stdout)
        self.assertNotIn("Importing", result.stdout)
        self.assertEqual(len(result.stdout.splitlines()), 3)
        self.assertEqual((self.source / "main.tex").read_text(), self.original)

    def test_configured_manuscript_violation_reaches_cli_exit_and_json(self):
        config = self.root / "settings.toml"
        config.write_text('expected_document_class = "report"\nrequire_abstract = true\n')
        result = self.runner.invoke(
            cli, ["inspect", str(self.source), "--config", str(config), "--output-format", "json"]
        )
        self.assertEqual(result.exit_code, 3, result.output)
        report = json.loads(result.stdout)
        codes = {finding["code"] for finding in report["findings"]}
        self.assertTrue({"TEX101", "TEX104", "TEX001"} <= codes)
        self.assertEqual(report["outcome"], "blocked")
        self.assertIn("manuscript checks", {stage["name"] for stage in report["stages"]})
        for finding in report["findings"]:
            if finding["status"] == "failed" and finding["code"]:
                self.assertTrue(finding["next_step"])

    def test_catalogue_and_explanation_are_available_without_a_project(self):
        result = self.runner.invoke(cli, ["rules", "--json"])
        self.assertEqual(result.exit_code, 0, result.output)
        catalogue = json.loads(result.stdout)
        self.assertEqual(catalogue["schema_version"], RULE_SCHEMA_VERSION)
        self.assertEqual({row["code"] for row in catalogue["rules"]}, {rule.code for rule in RULES})
        result = self.runner.invoke(cli, ["rule", "TEX001", "--json"])
        self.assertEqual(result.exit_code, 0)
        item = json.loads(result.stdout)
        self.assertEqual(item["schema_version"], RULE_SCHEMA_VERSION)
        self.assertEqual(item["name"], "source.edit_marker")
        self.assertTrue(item["tests"] and item["fix"])
        self.assertEqual(self.runner.invoke(cli, ["rule", "UNKNOWN"]).exit_code, USAGE_EXIT_CODE)
        self.assertEqual(self.runner.invoke(cli, ["prepare", "--help"]).exit_code, 0)
        self.assertEqual(self.runner.invoke(cli, ["inspect"]).exit_code, USAGE_EXIT_CODE)

    def test_parallel_cli_settings_reach_core_and_json_without_changing_output_mode(self):
        result = self.runner.invoke(
            cli,
            [
                "inspect",
                str(self.source),
                "--jobs",
                "4",
                "--build-jobs",
                "2",
                "--render-jobs",
                "2",
                "--job-timeout-seconds",
                "45",
                "--json",
                "--quiet",
            ],
        )
        self.assertEqual(result.exit_code, 1, result.output)
        report = json.loads(result.stdout)
        self.assertEqual(report["settings"]["jobs"], 4)
        self.assertEqual(report["settings"]["build_jobs"], 2)
        self.assertEqual(report["settings"]["render_jobs"], 2)
        self.assertEqual(report["settings"]["job_timeout_seconds"], 45)
        self.assertEqual(report["execution"]["resources"]["active_cpu"], 0)
        self.assertEqual(report["execution"]["resources"]["queued"], 0)
        self.assertNotIn("Importing", result.stdout)

    def test_boolean_options_preserve_toml_defaults_and_explicit_false_overrides(self):
        config = self.root / "settings.toml"
        config.write_text("apply_formatting = true\nnormalize_doi = true\ncleanup = true\n")
        for flags, expected in (
            ([], True),
            (["--no-format", "--no-normalize-doi", "--no-cleanup"], False),
        ):
            with self.subTest(flags=flags):
                with patch(
                    "latexprep.cli.run_job",
                    new=AsyncMock(return_value=Report("prepare", outcome=Outcome.PLANNED)),
                ) as run:
                    result = self.runner.invoke(
                        cli,
                        [
                            "prepare",
                            str(self.source),
                            "--config",
                            str(config),
                            "--dry-run",
                            "--json",
                            *flags,
                        ],
                    )
                self.assertEqual(result.exit_code, 0, result.output)
                settings = run.await_args.args[0].settings
                self.assertEqual(settings.apply_formatting, expected)
                self.assertEqual(settings.normalize_doi, expected)
                self.assertEqual(settings.cleanup, expected)

    def test_unexpected_internal_error_still_produces_a_json_error_report(self):
        with patch("latexprep.cli.run_job", new=AsyncMock(side_effect=KeyError("defect"))):
            result = self.runner.invoke(cli, ["inspect", str(self.source), "--json", "--quiet"])
        self.assertEqual(result.exit_code, 4, result.output)
        report = json.loads(result.stdout)
        self.assertEqual(report["outcome"], "error")
        self.assertEqual(report["findings"][0]["rule"], "execution.internal")
        self.assertIn("KeyError", report["findings"][0]["message"])

    def test_failed_html_report_is_reflected_in_saved_json(self):
        html = self.root / "missing-parent" / "report.html"
        saved = self.root / "report.json"
        with patch(
            "latexprep.report_files.validate_destination",
            side_effect=lambda _source, path, _option: path,
        ):
            result = self.runner.invoke(
                cli,
                [
                    "inspect",
                    str(self.source),
                    "--quiet",
                    "--html-report",
                    str(html),
                    "--report",
                    str(saved),
                ],
            )
        self.assertEqual(result.exit_code, 4, result.output)
        report = json.loads(saved.read_text())
        self.assertEqual(report["outcome"], "error")
        self.assertIn("report.write", {finding["rule"] for finding in report["findings"]})

    def test_conflicting_output_options_are_usage_errors(self):
        result = self.runner.invoke(
            cli, ["inspect", str(self.source), "--json", "--output-format", "compact"]
        )
        self.assertEqual(result.exit_code, USAGE_EXIT_CODE)
        self.assertIn("conflicts", result.output)


class RequestErrorTests(ProjectTestCase):
    """Rejected requests name the option or file at fault and a concrete next step."""

    def setUp(self):
        super().setUp()
        self.source = self.root / "project"
        self.source.mkdir()
        (self.source / "main.tex").write_text(
            "\\documentclass{article}\n\\begin{document}\nText.\n\\end{document}\n"
        )
        self.runner = CliRunner()

    def request_error(self, *arguments: str) -> dict:
        result = self.runner.invoke(cli, [*arguments, "--json", "--quiet"])
        self.assertEqual(result.exit_code, 4, result.output)
        report = json.loads(result.stdout)
        self.assertEqual(report["outcome"], "error")
        self.assertEqual(report["scope"], "request validation")
        [finding] = [item for item in report["findings"] if item["rule"] == "execution.request"]
        self.assertNotIn("missing or ambiguous evidence", finding["next_step"])
        return finding

    def test_existing_output_directory_names_the_option_to_change(self):
        existing = self.root / "existing"
        existing.mkdir()
        finding = self.request_error("prepare", str(self.source), "--output", str(existing))
        self.assertIn("Output already exists", finding["message"])
        self.assertIn("Choose a new output path", finding["next_step"])
        # Reports show the home and temporary directories generically.
        self.assertIn(scrub_local_paths(str(existing)), finding["next_step"])

    def test_existing_report_file_names_the_report_option(self):
        existing = self.root / "report.json"
        existing.write_text("{}")
        result = self.runner.invoke(
            cli, ["inspect", str(self.source), "--report", str(existing), "--output-format", "json"]
        )
        self.assertEqual(result.exit_code, 4, result.output)
        [finding] = json.loads(result.stdout)["findings"]
        self.assertIn("Choose a new --json-report path", finding["next_step"])

    def test_missing_input_suggests_checking_the_path(self):
        finding = self.request_error("inspect", str(self.root / "missing"))
        self.assertIn("Input does not exist", finding["message"])
        self.assertIn("LaTeX source folder or ZIP", finding["next_step"])

    def test_prepare_without_output_suggests_output_or_dry_run(self):
        finding = self.request_error("prepare", str(self.source))
        self.assertIn("output directory", finding["next_step"])
        self.assertIn("dry run", finding["next_step"])

    def test_unknown_main_lists_the_documents_found(self):
        finding = self.request_error("inspect", str(self.source), "--main", "nope.tex")
        self.assertIn("nope.tex", finding["message"])
        self.assertIn("--main", finding["next_step"])
        self.assertIn("documents found in the input: main.tex", finding["next_step"])

    def test_several_documents_suggest_choosing_one_with_main(self):
        (self.source / "other.tex").write_text((self.source / "main.tex").read_text())
        result = self.runner.invoke(cli, ["inspect", str(self.source), "--json", "--quiet"])
        report = json.loads(result.stdout)
        [finding] = [
            item for item in report["findings"] if item["rule"] == "project.main_selection"
        ]
        self.assertIn("Rerun with --main set to one of the candidate roots", finding["next_step"])

    def test_invalid_command_line_value_names_the_flag(self):
        finding = self.request_error("check", str(self.source), "--max-pages", "0")
        self.assertIn("max_pages must be a positive integer", finding["message"])
        self.assertIn("--max-pages on the command line", finding["next_step"])

    def test_invalid_configured_value_names_the_settings_file(self):
        config = self.source / ".fledge.toml"
        config.write_text("jobs = 100\n")
        finding = self.request_error("inspect", str(self.source))
        self.assertIn(f"jobs in {scrub_local_paths(str(config))}", finding["next_step"])

    def test_a_table_value_names_the_table_in_the_settings_file(self):
        config = self.source / ".fledge.toml"
        config.write_text("[online_checks]\nonline_timeout_seconds = 0\n")
        finding = self.request_error("inspect", str(self.source))
        self.assertIn("online_timeout_seconds", finding["message"])
        self.assertIn(f"online_checks in {scrub_local_paths(str(config))}", finding["next_step"])

    def test_every_setting_at_fault_is_named_in_the_order_of_the_message(self):
        config = self.source / ".fledge.toml"
        config.write_text("memory_mb = 300\nbuild_memory_mb = 1024\n")
        finding = self.request_error("inspect", str(self.source))
        shown = scrub_local_paths(str(config))
        self.assertIn(
            f"Change memory_mb in {shown}; build_memory_mb in {shown}, then rerun",
            finding["next_step"],
        )

    def test_a_setting_name_in_a_file_path_is_not_taken_for_a_setting_at_fault(self):
        project = self.root / "jobs"
        project.mkdir()
        (project / "main.tex").write_text((self.source / "main.tex").read_text())
        config = project / ".fledge.toml"
        config.write_text("jobs = [")
        finding = self.request_error("inspect", str(project))
        self.assertIn("Cannot read settings", finding["message"])
        self.assertEqual(
            finding["next_step"],
            f"Correct the settings in {scrub_local_paths(str(config))}, then rerun the command.",
        )

    def test_unknown_setting_names_the_file_and_the_closest_valid_key(self):
        config = self.source / ".fledge.toml"
        config.write_text("max_page = 3\n")
        finding = self.request_error("inspect", str(self.source))
        shown = scrub_local_paths(str(config))
        self.assertIn(shown, finding["message"])
        self.assertIn("max_page (did you mean max_pages?)", finding["message"])
        self.assertIn(f"Rename or remove it in {shown}", finding["next_step"])

    def test_unknown_setting_without_a_close_match_lists_valid_keys(self):
        config = self.source / ".fledge.toml"
        config.write_text("zzzz = 3\n")
        finding = self.request_error("inspect", str(self.source))
        self.assertIn("Valid top-level settings:", finding["next_step"])
        self.assertIn("max_pages", finding["next_step"])

    def test_terminal_report_is_headed_as_a_request_error(self):
        result = self.runner.invoke(cli, ["prepare", str(self.source), "--quiet"])
        self.assertEqual(result.exit_code, 4, result.output)
        self.assertTrue(result.stdout.startswith("Request error\n"), result.stdout)
        self.assertNotIn("source analysis", result.stdout)


class SettingsSuggestionTests(unittest.TestCase):
    def test_each_named_setting_says_where_it_was_supplied(self):
        names = ("jobs", "apply_formatting", "engine", "layout")
        overrides = {"jobs": 0, "apply_formatting": True}
        config = Path("/project/fledge.toml")
        self.assertEqual(
            settings_suggestion(names, overrides, config, "journal"),
            "Change --jobs on the command line; --format on the command line; "
            "engine in /project/fledge.toml or the journal preset; "
            "layout in /project/fledge.toml or the journal preset, then rerun the command.",
        )
        self.assertEqual(
            settings_suggestion(("engine",), {}, config, None),
            "Change engine in /project/fledge.toml, then rerun the command.",
        )
        self.assertEqual(
            settings_suggestion(("engine",), {}, None, "journal"),
            "Change engine in the journal preset, then rerun the command.",
        )

    def test_no_named_setting_gives_the_general_advice(self):
        self.assertEqual(
            settings_suggestion((), {}, Path("/project/fledge.toml"), None),
            "Correct the settings in /project/fledge.toml, then rerun the command.",
        )
        self.assertEqual(
            settings_suggestion((), {}, None, None),
            "Correct the options named above, then rerun the command.",
        )


class MissingToolchainTests(unittest.TestCase):
    def test_missing_tex_is_reported_once_with_installation_steps(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "project"
            source.mkdir()
            (source / "main.tex").write_text(
                "\\documentclass{article}\n\\begin{document}\nText.\n\\end{document}\n"
            )
            unavailable = BuildResult(
                False,
                findings=[
                    Finding(
                        "build.execution_unavailable",
                        "Required tool is unavailable: latexmk. Install it explicitly.",
                        severity="error",
                        status="inconclusive",
                        details={"tool": "latexmk"},
                    )
                ],
            )

            async def job_without_toolchain(request, progress=None, report=None):
                return await run_job(
                    request, progress, report, backends=controlled_backends(build=unavailable)
                )

            with (
                patch("latexprep.cli.run_job", new=job_without_toolchain),
                patch("latexprep.toolchain_help.shutil.which", return_value=None),
            ):
                result = CliRunner().invoke(cli, ["check", str(source), "--json", "--quiet"])
        self.assertEqual(result.exit_code, 3, result.output)
        report = json.loads(result.stdout)
        rules = [finding["rule"] for finding in report["findings"]]
        self.assertNotIn("build.baseline", rules)
        # The missing toolchain already explains why no TeX version was recorded.
        self.assertNotIn("build.tex_distribution", rules)
        [finding] = [
            item for item in report["findings"] if item["rule"] == "build.execution_unavailable"
        ]
        self.assertIn("latexmk and pdflatex were not found", finding["message"])
        self.assertEqual(finding["details"]["missing_tools"], ["latexmk", "pdflatex"])
        self.assertIn("nicolasschuler.github.io/fledge/installation.html", finding["next_step"])
        self.assertIn("fledge inspect", finding["next_step"])
        self.assertIn("basictex" if sys.platform == "darwin" else "TeX Live", finding["next_step"])


class RedactionOncePerRunTests(unittest.TestCase):
    def test_one_redaction_serves_every_file_and_the_final_report(self):
        from latexprep.redaction import redact_data

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project = root / "project"
            project.mkdir()
            (project / "main.tex").write_text("\\documentclass{article}\n")
            report = Report("inspect", outcome=Outcome.PASSED)
            for output_format in ("terminal", "compact", "json", "html", "ci"):
                destinations = {
                    option: root / f"{output_format}-{option[2:]}"
                    for option in ("--html-report", "--json-report", "--diagnostics")
                }
                arguments = ["inspect", str(project), "--quiet", "--output-format", output_format]
                for option, destination in destinations.items():
                    arguments += [option, str(destination)]
                with (
                    self.subTest(output_format=output_format),
                    patch("latexprep.cli.run_job", new=AsyncMock(return_value=report)),
                    patch("latexprep.report.redact_data", wraps=redact_data) as redaction,
                ):
                    result = CliRunner().invoke(cli, arguments)
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertTrue(all(path.exists() for path in destinations.values()))
                self.assertEqual(redaction.call_count, 1)


class UnexpectedFailureTests(ProjectTestCase):
    """A defect ends with the error exit code, never with a pass or an advisory code."""

    def setUp(self):
        super().setUp()
        self.source = self.root / "project"
        self.source.mkdir()
        (self.source / "main.tex").write_text(
            "\\documentclass{article}\n\\begin{document}\nTODO finish this.\n\\end{document}\n"
        )

    def run_main(self, *arguments: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(list(arguments))
        return code, out.getvalue(), err.getvalue()

    def inspect_json(self, *extra: str) -> tuple[int, dict, str]:
        code, out, err = self.run_main("inspect", str(self.source), "--json", "--quiet", *extra)
        return code, json.loads(out), err

    def test_crash_in_the_job_exits_4_and_keeps_the_error_type_and_frames(self):
        with patch("latexprep.cli.run_job", new=AsyncMock(side_effect=RuntimeError("defect"))):
            code, report, err = self.inspect_json()
        self.assertEqual(code, 4)
        self.assertEqual(report["outcome"], "error")
        self.assertEqual(err, "")
        [finding] = [item for item in report["findings"] if item["rule"] == "execution.internal"]
        self.assertEqual(finding["details"]["error_type"], "RuntimeError")
        frames = finding["details"]["frames"]
        self.assertTrue(frames)
        # file name, line and function; never a directory.
        for frame in frames:
            self.assertRegex(frame, r"^[\w.-]+\.py:\d+ in \S+$")
        self.assertIn("--debug", finding["next_step"])

    def test_internal_error_is_not_labelled_as_request_validation(self):
        with patch("latexprep.cli.run_job", new=AsyncMock(side_effect=KeyError("defect"))):
            _, report, _ = self.inspect_json()
        self.assertEqual(report["scope"], "run did not finish")
        self.assertNotIn("execution.request", {item["rule"] for item in report["findings"]})

    def test_value_error_from_the_job_is_a_defect_not_a_request_error(self):
        with patch("latexprep.cli.run_job", new=AsyncMock(side_effect=ValueError("bad value"))):
            code, report, _ = self.inspect_json()
        self.assertEqual(code, 4)
        rules = {item["rule"] for item in report["findings"]}
        self.assertIn("execution.internal", rules)
        self.assertNotIn("execution.request", rules)

    def test_genuine_input_output_errors_stay_request_errors(self):
        with patch("latexprep.cli.run_job", new=AsyncMock(side_effect=OSError("disk full"))):
            code, report, _ = self.inspect_json()
        self.assertEqual(code, 4)
        self.assertEqual(report["scope"], "request validation")
        self.assertIn("execution.request", {item["rule"] for item in report["findings"]})

    def test_cancellation_is_consistent_for_interrupt_and_asyncio_cancellation(self):
        for failure in (KeyboardInterrupt(), asyncio.CancelledError()):
            with self.subTest(failure=type(failure).__name__):
                with patch("latexprep.cli.run_job", new=AsyncMock(side_effect=failure)):
                    code, report, _ = self.inspect_json()
                self.assertEqual(code, 130)
                self.assertEqual(report["outcome"], "cancelled")
                self.assertEqual(report["scope"], "run did not finish")

    def test_crash_while_writing_the_report_exits_4_with_one_plain_line(self):
        with patch(
            "latexprep.cli.write_redacted_report", side_effect=RuntimeError("renderer defect")
        ):
            code, out, err = self.run_main("inspect", str(self.source), "--quiet")
        self.assertEqual(code, 4)
        self.assertEqual(out, "")
        self.assertEqual(len(err.strip().splitlines()), 1, err)
        self.assertIn("Internal error (RuntimeError) while writing the report", err)
        self.assertNotIn("Traceback", err)
        self.assertNotIn("renderer defect", err)

    def test_crash_while_saving_an_optional_report_file_is_recorded_in_the_report(self):
        html = self.root / "report.html"
        with patch(
            "latexprep.report_files.render_redacted_html", side_effect=RuntimeError("html defect")
        ):
            code, report, _ = self.inspect_json("--html-report", str(html))
        self.assertEqual(code, 4)
        self.assertEqual(report["outcome"], "error")
        self.assertIn("execution.internal", {item["rule"] for item in report["findings"]})

    def test_debug_flag_and_environment_variable_print_the_traceback_to_stderr(self):
        for environment, flags in (({"FLEDGE_DEBUG": "1"}, []), ({}, ["--debug"])):
            with self.subTest(environment=environment, flags=flags):
                with (
                    patch.dict(os.environ, environment),
                    patch(
                        "latexprep.cli.write_redacted_report",
                        side_effect=RuntimeError("renderer defect"),
                    ),
                ):
                    code, _, err = self.run_main("inspect", str(self.source), "--quiet", *flags)
                self.assertEqual(code, 4)
                self.assertIn("Traceback", err)
                self.assertIn("renderer defect", err)

    def test_debug_traceback_keeps_stdout_a_clean_report(self):
        with patch("latexprep.cli.run_job", new=AsyncMock(side_effect=RuntimeError("defect"))):
            code, report, err = self.inspect_json("--debug")
        self.assertEqual(code, 4)
        self.assertEqual(report["outcome"], "error")
        self.assertIn("Traceback", err)

    def test_commands_without_their_own_guard_exit_4_instead_of_status_1(self):
        (self.root / "empty").mkdir()
        cases = (
            ("rules", "latexprep.cli.print_rule_table", ["rules"]),
            (
                "init",
                "latexprep.init_command.parse_settings_text",
                ["init", str(self.root / "empty")],
            ),
            (
                "submission-text",
                "latexprep.submission_text_command.read_submission_text",
                ["submission-text", str(self.source)],
            ),
        )
        for name, target, arguments in cases:
            with self.subTest(command=name):
                with patch(target, side_effect=RuntimeError("defect")):
                    code, _, err = self.run_main(*arguments)
                self.assertEqual(code, 4)
                self.assertIn("Internal error (RuntimeError)", err)
                self.assertNotIn("Traceback", err)

    def test_doctor_treats_value_error_as_a_defect_and_cancellation_like_other_commands(self):
        for failure, expected_code, expected_rule in (
            (ValueError("bad"), 4, "execution.internal"),
            (asyncio.CancelledError(), 130, "execution.cancelled"),
        ):
            with self.subTest(failure=type(failure).__name__):
                with patch("latexprep.doctor_command.run_doctor", side_effect=failure):
                    code, out, _ = self.run_main("doctor", "--json", "--quiet")
                self.assertEqual(code, expected_code)
                report = json.loads(out)
                self.assertIn(expected_rule, {item["rule"] for item in report["findings"]})
                self.assertNotEqual(report["scope"], "request validation")

    def test_closed_output_pipe_keeps_the_verdict_and_prints_nothing(self):
        with patch("latexprep.cli.write_redacted_report", side_effect=BrokenPipeError):
            code, _, err = self.run_main("inspect", str(self.source), "--quiet")
        # The TODO marker is an advisory: exit code 1, not an error.
        self.assertEqual(code, 1)
        self.assertEqual(err, "")
        with patch("latexprep.cli.print_rule_table", side_effect=BrokenPipeError):
            code, _, err = self.run_main("rules")
        self.assertEqual((code, err), (0, ""))

    def test_missing_settings_file_says_it_does_not_exist_and_how_to_fix_it(self):
        missing = self.root / "missing.toml"
        code, report, _ = self.inspect_json("--config", str(missing))
        self.assertEqual(code, 4)
        [finding] = [item for item in report["findings"] if item["rule"] == "execution.request"]
        self.assertIn("does not exist", finding["message"])
        self.assertIn("missing.toml", finding["message"])
        self.assertNotIn("Errno", finding["message"])
        self.assertIn("--config", finding["next_step"])

    def test_report_write_failures_name_the_option_and_a_next_step(self):
        for option, directory in (("--html-report", "html"), ("--diagnostics", "bundle")):
            with self.subTest(option=option):
                destination = self.root / "no-such-folder" / directory
                with patch(
                    "latexprep.report_files.validate_destination",
                    side_effect=lambda _source, path, _option: path,
                ):
                    code, report, _ = self.inspect_json(option, str(destination))
                self.assertEqual(code, 4)
                [finding] = [item for item in report["findings"] if item["rule"] == "report.write"]
                self.assertIn(option, finding["message"])
                self.assertNotIn("Errno", finding["message"])
                self.assertIn(option, finding["next_step"])


if __name__ == "__main__":
    unittest.main()
