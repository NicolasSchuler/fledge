"""Generated documentation and shipped examples stay consistent with the code."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

from latexprep.config import load_settings

ROOT = Path(__file__).resolve().parents[1]


def _generator():
    spec = importlib.util.spec_from_file_location(
        "generate_check_docs", ROOT / "scripts" / "generate_check_docs.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DocumentationTests(unittest.TestCase):
    def test_check_catalogue_table_matches_registered_rules(self) -> None:
        generator = _generator()
        current = generator.DOCUMENT.read_text(encoding="utf-8")
        self.assertEqual(
            current,
            generator.render(current),
            "docs/checks.md is out of date; run scripts/generate_check_docs.py",
        )

    def test_agent_skill_uses_existing_commands_and_options(self) -> None:
        import re

        import click

        from latexprep.cli import cli

        skill = (ROOT / "skills" / "fledge" / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(skill.startswith("---\nname: fledge\ndescription: "))
        invocations = re.findall(r"`(fledge [^`]+)`", skill)
        self.assertGreater(len(invocations), 5)
        for invocation in invocations:
            with self.subTest(invocation=invocation):
                words = invocation.split()[1:]
                command: click.Command = cli
                while isinstance(command, click.Group) and words and words[0] in command.commands:
                    command = command.commands[words.pop(0)]
                options = {"--help", "-h"}
                for parameter in command.params:
                    options.update(getattr(parameter, "opts", ()))
                    options.update(getattr(parameter, "secondary_opts", ()))
                if command is cli:
                    options.add("--version")
                for word in words:
                    if word.startswith("-"):
                        self.assertIn(word, options, f"{invocation}: unknown option {word}")

    def test_shipped_example_settings_load(self) -> None:
        for path in sorted((ROOT / "examples").glob("*.toml")):
            with self.subTest(path=path.name):
                load_settings(path, {})


if __name__ == "__main__":
    unittest.main()
