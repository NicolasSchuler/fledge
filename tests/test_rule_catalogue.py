"""``fledge rules`` and ``fledge rule``: order, filters, activation, suggestions and completion."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from click.testing import CliRunner

from latexprep.cli import USAGE_EXIT_CODE, cli
from latexprep.cli_completion import complete_check_codes, complete_check_selectors
from latexprep.config import Settings
from latexprep.rule_catalogue_view import ALWAYS, DEFAULT, OPT_IN, rule_activation
from latexprep.rules import RULES, get_rule
from tests import SOURCE_ROOT

SCRIPT = SOURCE_ROOT.parent / "scripts" / "generate_check_docs.py"


def catalogue_generator():
    """Load ``scripts/generate_check_docs.py``, which classifies the rules for docs/checks.md."""
    spec = importlib.util.spec_from_file_location("generate_check_docs", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RuleListTests(unittest.TestCase):
    """The list of checks: sorted by code, filtered by selector, marked by activation."""

    def run_rules(self, *words: str):
        """Run ``fledge rules`` with ``words`` at width 200 and return the Click result."""
        return CliRunner().invoke(cli, ["rules", *words], terminal_width=200)

    def test_the_list_is_sorted_by_code_and_marks_how_each_check_runs(self) -> None:
        result = self.run_rules()
        self.assertEqual(result.exit_code, 0, result.output)
        rows = [line.split() for line in result.stdout.splitlines()[2:] if line[:3].isupper()]
        codes = [row[0] for row in rows if row[0][:3].isalpha() and row[0][3:].isdigit()]
        self.assertEqual(codes, sorted(rule.code for rule in RULES))
        marks = {row[0]: row[1] for row in rows if row[0] in {"TEX001", "PDF001", "TEX005"}}
        self.assertEqual(marks, {"TEX001": DEFAULT, "PDF001": OPT_IN, "TEX005": ALWAYS})
        self.assertIn("opt-in: runs once configured", result.stdout)

    def test_selectors_filter_the_list_in_any_case(self) -> None:
        result = self.run_rules("tex", "BIB1")
        self.assertEqual(result.exit_code, 0, result.output)
        listed = json.loads(self.run_rules("tex", "BIB1", "--json").stdout)["rules"]
        codes = [rule["code"] for rule in listed]
        self.assertTrue(codes)
        self.assertTrue(all(code.startswith(("TEX", "BIB1")) for code in codes))
        self.assertEqual(codes, sorted(codes))
        self.assertIn(f"{len(codes)} registered checks", result.stdout)
        one = json.loads(self.run_rules("source.edit_marker", "--json").stdout)["rules"]
        self.assertEqual([rule["code"] for rule in one], ["TEX001"])

    def test_an_unknown_selector_is_a_usage_error_with_suggestions(self) -> None:
        result = self.run_rules("TEX99")
        self.assertEqual(result.exit_code, USAGE_EXIT_CODE, result.output)
        self.assertIn("did you mean", result.stderr)

    def test_json_carries_the_activation_and_the_settings_that_enable_a_check(self) -> None:
        listed = {
            rule["code"]: rule for rule in json.loads(self.run_rules("--json").stdout)["rules"]
        }
        self.assertEqual(set(listed), {rule.code for rule in RULES})
        self.assertEqual(listed["TEX001"]["activation"], DEFAULT)
        self.assertEqual(listed["PDF001"]["activation"], OPT_IN)
        self.assertEqual(listed["PDF001"]["enabled_by"], ["max_pages"])
        self.assertEqual(listed["TEX005"]["enabled_by"], [])

    def test_activation_agrees_with_the_catalogue_page_for_every_check(self) -> None:
        generator = catalogue_generator()
        defaults = Settings()
        for rule in RULES:
            with self.subTest(code=rule.code):
                kind = rule_activation(rule, defaults).kind
                self.assertEqual(kind, generator.classify(rule, defaults)[0])


class RuleExplanationTests(unittest.TestCase):
    """Explaining one check: when it runs, and suggestions for a code that does not exist."""

    def test_the_explanation_says_when_the_check_runs(self) -> None:
        for code, expected in (
            ("TEX001", "Runs: by default"),
            ("PDF001", "Runs: only once configured; set max_pages."),
            ("TEX005", "Runs: always"),
        ):
            with self.subTest(code=code):
                result = CliRunner().invoke(cli, ["rule", code.lower()], terminal_width=200)
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertIn(expected, result.stdout)
        payload = json.loads(CliRunner().invoke(cli, ["rule", "PDF001", "--json"]).stdout)
        self.assertEqual(payload["activation"], OPT_IN)

    def test_a_mistyped_code_suggests_the_closest_codes(self) -> None:
        result = CliRunner().invoke(cli, ["rule", "TEX01"])
        self.assertEqual(result.exit_code, USAGE_EXIT_CODE, result.output)
        self.assertIn("did you mean TEX001, TEX010, TEX011?", result.stderr)
        for typed, first in (("tex01", "TEX001"), ("TEX0001", "TEX001"), ("tx001", "TEX001")):
            with self.subTest(typed=typed):
                with self.assertRaisesRegex(ValueError, f"did you mean {first}"):
                    get_rule(typed)

    def test_a_family_points_to_the_list_of_its_checks(self) -> None:
        with self.assertRaisesRegex(ValueError, "run 'fledge rules BIB' to list them"):
            get_rule("bib")
        with self.assertRaisesRegex(ValueError, "did you mean source.edit_marker"):
            get_rule("source.edit_markr")


class CompletionTests(unittest.TestCase):
    """Shell completion of check codes and selectors."""

    def completions(self, *words: str) -> list[str]:
        """Return what bash completion offers after ``fledge`` and ``words``."""
        result = CliRunner().invoke(
            cli,
            [],
            prog_name="fledge",
            env={
                "_FLEDGE_COMPLETE": "bash_complete",
                "COMP_WORDS": " ".join(("fledge", *words)),
                "COMP_CWORD": str(len(words)),
            },
        )
        return [line.split(",", 1)[1] for line in result.output.splitlines() if "," in line]

    def test_selectors_and_codes_complete_in_any_case(self) -> None:
        selectors = [item.value for item in complete_check_selectors(None, None, "tex00")]
        self.assertIn("TEX00", selectors)
        self.assertIn("TEX001", selectors)
        codes = [item.value for item in complete_check_codes(None, None, "tex00")]
        self.assertNotIn("TEX00", codes)
        self.assertIn("TEX001", codes)
        [described] = complete_check_codes(None, None, "TEX001")
        self.assertEqual(described.help, get_rule("TEX001").title)

    def test_the_shell_receives_codes_for_select_ignore_rule_and_rules(self) -> None:
        self.assertIn("TEX001", self.completions("inspect", "paper", "--select", "TEX00"))
        self.assertIn("BIB", self.completions("check", "paper", "--ignore", "BI"))
        self.assertIn("TEX001", self.completions("rule", "tex00"))
        self.assertIn("PDF", self.completions("rules", "PD"))

    def test_main_completes_the_tex_files_inside_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("main.tex", "sections/intro.tex", ".hidden/x.tex", "figure.pdf"):
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text("x\n")
            (root / "linked.tex").symlink_to(root / "main.tex")
            self.assertEqual(
                self.completions("check", str(root), "--main", ""),
                ["main.tex", "sections/intro.tex"],
            )
            self.assertEqual(
                self.completions("submission-text", str(root), "-m", "sec"),
                ["sections/intro.tex"],
            )
            self.assertEqual(self.completions("check", str(root / "main.tex"), "-m", ""), [])

    def test_commands_complete_in_workflow_order(self) -> None:
        self.assertEqual(self.completions("")[:3], ["inspect", "check", "prepare"])


if __name__ == "__main__":
    unittest.main()
