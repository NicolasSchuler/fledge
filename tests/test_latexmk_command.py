"""The fixed latexmk configuration: the glossary, acronym and nomenclature rules.

The rules are Perl that latexmk evaluates, so these tests run them with Perl itself and a
stand-in ``makeindex`` that records how it was called: each rule runs makeindex in the output
folder with the style file and order the glossaries package recorded, or nomencl's style.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from latexprep.latexmk_command import GLOSSARY_RULES, BiberPlan, controlled_rules

_PERL = shutil.which("perl")
# A makeindex that writes its working folder and arguments, one per line, to calls.txt.
_RECORDING_MAKEINDEX = """#!/bin/sh
{ pwd; for argument in "$@"; do printf '%s\\n' "$argument"; done; echo --; } >> "$CALLS"
"""


@unittest.skipIf(_PERL is None, "perl is not on PATH")
class GlossaryRuleTests(unittest.TestCase):
    """Each custom rule runs makeindex for exactly its own output."""

    def setUp(self) -> None:
        """Create an output folder, a recording makeindex and a call log."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        self.output = root / "output"
        self.output.mkdir()
        tools = root / "tools"
        tools.mkdir()
        makeindex = tools / "makeindex"
        makeindex.write_text(_RECORDING_MAKEINDEX)
        makeindex.chmod(0o755)
        self.calls = root / "calls.txt"
        self.environment = {
            "PATH": f"{tools}{os.pathsep}/usr/bin:/bin",
            "CALLS": str(self.calls),
        }

    def run_rule(self, rule: str) -> None:
        """Run the Perl subroutine ``rule`` on ``output/main`` and require it to succeed.

        Args:
            rule: The name of the rule's subroutine.
        """
        program = (
            "use File::Basename; use Cwd; sub add_cus_dep {} "
            f"{GLOSSARY_RULES} exit({rule}('{self.output / 'main'}') == 0 ? 0 : 1);"
        )
        completed = subprocess.run(
            [str(_PERL), "-e", program],
            env=self.environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def call(self, rule: str) -> list[str]:
        """Run the Perl subroutine ``rule`` on ``output/main`` and return makeindex's call.

        Args:
            rule: The name of the rule's subroutine.

        Returns:
            The folder makeindex ran in, then its arguments.
        """
        self.run_rule(rule)
        call = self.calls.read_text().split("--\n")[-2].splitlines()
        self.calls.unlink()
        return call

    def test_glossary_and_acronyms_use_the_recorded_style_and_order(self) -> None:
        (self.output / "main.aux").write_text(
            "\\relax\n\\@istfilename{custom.ist}\n\\@glsorder{letter}\n"
        )
        folder, *arguments = self.call("fledge_glossary")
        self.assertEqual(Path(folder).resolve(), self.output)
        self.assertEqual(
            arguments,
            ["-q", "-s", "custom.ist", "-l", "-t", "main.glg", "-o", "main.gls", "main.glo"],
        )
        _, *arguments = self.call("fledge_acronyms")
        self.assertEqual(
            arguments,
            ["-q", "-s", "custom.ist", "-l", "-t", "main.alg", "-o", "main.acr", "main.acn"],
        )

    def test_a_style_for_xindy_leaves_the_lists_empty_without_running_makeindex(self) -> None:
        # makeindex cannot read glossaries written for xindy, and without their style file
        # (\noist) it fails the build; latexmk fails a rule that makes no output.
        (self.output / "main.aux").write_text("\\relax\n\\@istfilename{main.xdy}\n")
        for rule, output in (("fledge_glossary", "main.gls"), ("fledge_acronyms", "main.acr")):
            with self.subTest(rule=rule):
                self.run_rule(rule)
                self.assertFalse(self.calls.exists())
                self.assertEqual((self.output / output).read_bytes(), b"")

    def test_a_style_for_xindy_preserves_supplied_lists_without_running_makeindex(self) -> None:
        """Keep the contents of a supplied glossary and list of acronyms."""
        (self.output / "main.aux").write_text("\\relax\n\\@istfilename{main.xdy}\n")
        for rule, output in (("fledge_glossary", "main.gls"), ("fledge_acronyms", "main.acr")):
            with self.subTest(rule=rule):
                seeded = self.output / output
                seeded.write_bytes(b"supplied list\n")
                self.run_rule(rule)
                self.assertFalse(self.calls.exists())
                self.assertEqual(seeded.read_bytes(), b"supplied list\n")

    def test_defaults_without_aux_records_and_the_nomenclature(self) -> None:
        _, *arguments = self.call("fledge_glossary")
        self.assertEqual(arguments[:3], ["-q", "-s", "main.ist"])
        self.assertNotIn("-l", arguments)
        _, *arguments = self.call("fledge_nomenclature")
        self.assertEqual(
            arguments, ["-q", "-s", "nomencl.ist", "-t", "main.nlg", "-o", "main.nls", "main.nlo"]
        )

    def test_shipped_index_files_leave_the_glossary_rules_out(self) -> None:
        # The archive rebuild reads shipped index and glossary files as they are, so no
        # rule may run makeindex, including the glossary rules that call it themselves.
        plan = BiberPlan(deferred=False, note=None)
        for kept in (frozenset({".ind"}), frozenset({".gls", ".acr"}), frozenset({".nls"})):
            with self.subTest(kept=sorted(kept)):
                rules = controlled_rules("auto", plan, "biber", kept_generated=kept)
                self.assertNotIn("add_cus_dep", rules)
                self.assertIn("$makeindex='internal fledge_generated_file_kept'", rules)
        bibliography_only = controlled_rules(
            "auto", plan, "biber", kept_generated=frozenset({".bbl"})
        )
        self.assertIn(GLOSSARY_RULES, bibliography_only)

    def test_rules_are_part_of_every_configuration_and_compile(self) -> None:
        rules = controlled_rules("auto", BiberPlan(deferred=False, note=None), "biber")
        self.assertIn('add_cus_dep("glo", "gls", 0, "fledge_glossary")', rules)
        self.assertIn('add_cus_dep("nlo", "nls", 0, "fledge_nomenclature")', rules)
        compiled = subprocess.run(
            [str(_PERL), "-c", "-e", f"sub add_cus_dep {{}} {rules}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        self.assertEqual(compiled.returncode, 0, compiled.stderr)
