"""Generated documentation and shipped examples stay consistent with the code."""

from __future__ import annotations

import ast
import importlib.util
import re
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


def _defined_tests() -> tuple[set[str], dict[str, set[str]]]:
    """Every test function name in ``tests/`` and the test methods of each class."""
    names: set[str] = set()
    methods: dict[str, set[str]] = {}
    for path in sorted((ROOT / "tests").glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ClassDef):
                methods.setdefault(node.name, set()).update(
                    item.name
                    for item in node.body
                    if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef)
                )
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith(
                "test"
            ):
                names.add(node.name)
    return names, methods


class DocumentationTests(unittest.TestCase):
    def test_documentation_links_match_the_documentation_url_of_the_project(self) -> None:
        import tomllib

        from latexprep.documentation_links import (
            CONFIGURATION_GUIDE,
            DOCUMENTATION_URL,
            INSTALLATION_GUIDE,
        )

        with (ROOT / "pyproject.toml").open("rb") as stream:
            declared = tomllib.load(stream)["project"]["urls"]["Documentation"]
        self.assertEqual(DOCUMENTATION_URL, declared)
        self.assertEqual(INSTALLATION_GUIDE, declared + "installation.html")
        self.assertEqual(CONFIGURATION_GUIDE, declared + "configuration.html")

    def test_check_catalogue_table_matches_registered_rules(self) -> None:
        generator = _generator()
        current = generator.DOCUMENT.read_text(encoding="utf-8")
        self.assertEqual(
            current,
            generator.render(current),
            "docs/checks.md is out of date; run scripts/generate_check_docs.py",
        )

    def test_check_catalogue_lists_every_code_once_with_its_fix(self) -> None:
        import re

        from latexprep.rules import RULES

        generator = _generator()
        catalogue = generator.DOCUMENT.read_text(encoding="utf-8")
        rows = re.findall(r"^\| `([A-Z]{3}\d{3})` \|", catalogue, flags=re.MULTILINE)
        self.assertCountEqual(rows, [rule.code for rule in RULES])
        kinds = {generator.classify(rule)[0] for rule in RULES}
        self.assertEqual(kinds, {generator.ALWAYS, generator.DEFAULT, generator.OPT_IN})
        for rule in RULES:
            kind, setting = generator.classify(rule)
            with self.subTest(code=rule.code):
                self.assertEqual(kind == generator.OPT_IN, bool(setting))

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

    def test_test_names_quoted_in_the_published_docs_exist(self) -> None:
        """The backlog and development notes name tests as evidence; a rename must update them."""
        names, methods = _defined_tests()
        quoted = re.compile(r"(?<![\w/.<])(?:([A-Z]\w+)\.)?(test_[a-z0-9_]+)(?!\w)")
        checked = 0
        for path in sorted((ROOT / "docs").glob("*.md")):
            text = path.read_text(encoding="utf-8")
            for match in quoted.finditer(text):
                if text.startswith(".py", match.end()):
                    continue  # a test file name such as test_source.py, not a test
                owner, name = match.groups()
                checked += 1
                with self.subTest(document=path.name, test=match.group(0)):
                    known = names if owner is None else methods.get(owner, set())
                    self.assertTrue(
                        name in known, f"{path.name} quotes {match.group(0)}, which does not exist"
                    )
        self.assertGreater(checked, 40, "the docs no longer quote tests; is the pattern stale?")

    def test_shipped_example_settings_load(self) -> None:
        for path in sorted((ROOT / "examples").glob("*.toml")):
            with self.subTest(path=path.name):
                load_settings(path, {})


if __name__ == "__main__":
    unittest.main()
