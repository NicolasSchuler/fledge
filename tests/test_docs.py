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
    """Load ``scripts/generate_check_docs.py`` as a module.

    Returns:
        The executed ``generate_check_docs`` module.
    """
    spec = importlib.util.spec_from_file_location(
        "generate_check_docs", ROOT / "scripts" / "generate_check_docs.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _defined_tests() -> tuple[set[str], dict[str, set[str]]]:
    """Collect every test function name in ``tests/`` and the methods of each class.

    Returns:
        The names of all functions and methods that start with ``test``, and the names of
        the methods of each class by class name; classes of the same name are merged.
    """
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
    """Documentation links, the check catalogue, the agent skill, quoted test names and examples."""

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

    def test_check_catalogue_lists_every_code_once(self) -> None:
        from latexprep.rules import RULES

        catalogue = (ROOT / "docs" / "checks.md").read_text(encoding="utf-8")
        rows = re.findall(r"^\| `([A-Z]{3}\d{3})` \|", catalogue, flags=re.MULTILINE)
        self.assertCountEqual(rows, [rule.code for rule in RULES])

    @unittest.skipUnless(
        importlib.util.find_spec("myst_parser"), "needs MyST-Parser from the docs extra"
    )
    def test_check_catalogue_renders_rule_texts_exactly_as_written(self) -> None:
        """Check that MyST shows each rule's plain text literally in the catalogue.

        Rule texts hold LaTeX and paths, such as display-math brackets, ``<main>.bbl`` and
        TeX quotes, which unescaped Markdown would turn into escapes, HTML tags or code.
        """
        from docutils import nodes
        from docutils.core import publish_doctree
        from myst_parser.parsers.docutils_ import Parser

        from latexprep.rules import RULES

        doctree = publish_doctree(
            (ROOT / "docs" / "checks.md").read_text(encoding="utf-8"),
            parser=Parser(),
            settings_overrides={"report_level": 5},
        )
        self.assertEqual(list(doctree.findall(nodes.raw)), [], "raw HTML in the catalogue")
        rows = {}
        for row in doctree.findall(nodes.row):
            cells = [entry.astext() for entry in row.findall(nodes.entry)]
            rows[cells[0]] = cells
        for rule in RULES:
            with self.subTest(code=rule.code):
                cells = rows[rule.code]
                plain = [" ".join(text.split()) for text in (rule.title, rule.description)]
                self.assertEqual(cells[1], f"{plain[0]}. {plain[1]}")
                self.assertEqual(cells[-1], " ".join(rule.fix.split()))
        self.assertIn(r"\[ ... \]", rows["STY102"][-1])
        self.assertIn("Write ``word'' or use", rows["STY304"][-1])
        self.assertIn(r"/Users/<name>/, /home/<name>/ or C:\Users\<name>\ ", rows["PRV104"][1])
        self.assertIn("ships the <main>.bbl", rows["TEX009"][-1])

    def test_check_catalogue_names_both_pdf207_settings_in_their_own_scope(self) -> None:
        catalogue = (ROOT / "docs" / "checks.md").read_text(encoding="utf-8")
        [row] = [line for line in catalogue.splitlines() if line.startswith("| `PDF207` |")]
        self.assertIn(
            " | `match_source_pdf_metadata` or `expected_metadata` in `pdf_checks`. ", row
        )

    def test_catalogue_distinguishes_tuning_fields_combinations_and_commands(self) -> None:
        catalogue = (ROOT / "docs" / "checks.md").read_text(encoding="utf-8")
        for code, expected in (
            ("PDF205", " | `min_page_ink_ratio` in `pdf_checks` | "),
            ("PDF215", " | `min_text_contrast` and `contrast_background_rgb` in `pdf_checks` | "),
            (
                "PDF310",
                "`raster_regions` and `min_raster_region_coverage` in `rendered_pdf_artwork` or "
                "`raster_regions` and `min_raster_region_coverage` in `included_figure_artwork`",
            ),
            ("FMT001", " | `fledge fmt`. No setting switches on this diagnostic."),
        ):
            with self.subTest(code=code):
                [row] = [
                    line for line in catalogue.splitlines() if line.startswith(f"| `{code}` |")
                ]
                self.assertIn(expected, row)

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
        """Check that every test name quoted in the published docs exists.

        The backlog and development notes name tests as evidence; a rename must update them.
        """
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
