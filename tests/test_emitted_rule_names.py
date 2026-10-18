"""Every rule name the code emits is catalogued or declared operational.

A finding whose rule is in neither place has no public code and no fix text, so users cannot
select, ignore or explain it with ``fledge rule``. The scan gathers the string constants that
look like rule names (``family.check_name``) and the first argument of every ``Finding(...)``
call, so a new name that skips the catalogue fails here instead of surfacing in a report.

Names assembled at run time (``bibliography.<operation>``, ``build.<stage>``) are not visible
to a scan of constants; the first is covered by the catalogued ``BIB2xx`` rules and the second
is operational, as ``OPERATIONAL_RULE_NAMES`` notes.
"""

from __future__ import annotations

import ast
import re
import tempfile
import unittest
from collections.abc import Iterator
from pathlib import Path

from latexprep.rules import BY_NAME, OPERATIONAL_RULE_NAMES

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "latexprep"
_RULE_NAME = re.compile(r"[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*")
# Strings shaped like a rule name that are file names: ``report.json``, ``submission.zip``.
_FILE_SUFFIXES = frozenset(
    {"bib", "css", "html", "json", "log", "pdf", "tex", "toml", "txt", "zip"}
)


def _constant(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _names_in(node: ast.AST, families: set[str]) -> Iterator[str]:
    value = _constant(node)
    if value is not None and _RULE_NAME.fullmatch(value):
        family, _, tail = value.partition(".")
        if family in families and tail not in _FILE_SUFFIXES:
            yield value
    if isinstance(node, ast.Call) and node.args:
        function = node.func
        called = function.id if isinstance(function, ast.Name) else getattr(function, "attr", "")
        # A finding of a family nothing has used before is still a finding.
        if called == "Finding" and (first := _constant(node.args[0])) is not None:
            yield first


def emitted_rule_names(package: Path) -> dict[str, list[str]]:
    """The rule names the modules of ``package`` carry, each with the modules that name it.

    The ``*_rules.py`` modules are the catalogue itself and are not scanned.
    """
    families = {name.partition(".")[0] for name in (*BY_NAME, *OPERATIONAL_RULE_NAMES)}
    found: dict[str, list[str]] = {}
    for module in sorted(package.glob("*.py")):
        if module.name.endswith("_rules.py"):
            continue
        tree = ast.parse(module.read_text(encoding="utf-8"))
        for name in {name for node in ast.walk(tree) for name in _names_in(node, families)}:
            found.setdefault(name, []).append(module.name)
    return found


def undeclared(names: dict[str, list[str]]) -> dict[str, list[str]]:
    """The names that are neither catalogued nor declared operational."""
    return {
        name: modules
        for name, modules in names.items()
        if name not in BY_NAME and name not in OPERATIONAL_RULE_NAMES
    }


class EmittedRuleNameTests(unittest.TestCase):
    def test_every_emitted_name_is_catalogued_or_declared_operational(self) -> None:
        self.assertEqual(undeclared(emitted_rule_names(PACKAGE)), {})

    def test_a_new_name_that_is_neither_fails_the_scan(self) -> None:
        sources = {
            "checks.py": (
                "from .findings import Finding\n"
                "_RULE = 'package.another_new_name'\n"
                "def check():\n"
                "    return [\n"
                "        Finding('source.brand_new_problem', 'message'),\n"
                "        Finding('novelfamily.thing', 'message'),\n"
                "        Finding('source.edit_marker', 'catalogued'),\n"
                "        Finding('build.failed', 'operational'),\n"
                "        ('report.json', 'a file name, not a rule'),\n"
                "    ]\n"
            ),
            # The catalogue modules are data, not emitters.
            "fake_rules.py": "NAME = 'source.listed_only_in_a_catalogue_module'\n",
        }
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory)
            for name, text in sources.items():
                (package / name).write_text(text, encoding="utf-8")
            found = emitted_rule_names(package)
        self.assertEqual(
            set(undeclared(found)),
            {"package.another_new_name", "source.brand_new_problem", "novelfamily.thing"},
        )
        self.assertIn("source.edit_marker", found)
        self.assertNotIn("report.json", found)

    def test_operational_names_are_not_catalogued_and_are_still_emitted(self) -> None:
        emitted = emitted_rule_names(PACKAGE)
        for name in sorted(OPERATIONAL_RULE_NAMES):
            with self.subTest(name=name):
                self.assertNotIn(name, BY_NAME, "an operational name must not be catalogued")
                self.assertRegex(name, _RULE_NAME)
                self.assertIn(name, emitted, "a declared operational name is no longer emitted")


if __name__ == "__main__":
    unittest.main()
