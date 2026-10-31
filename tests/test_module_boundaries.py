"""Module boundaries: shared code is public, the package has no import cycles, and layers hold.

A name with a leading underscore is private to its module. Code shared between
modules belongs in a public module under a public name, so these tests read every
import statement in the package (including function-level and type-only imports)
instead of trusting convention.

The package is flat but layered (see ``module_layers`` and ``docs/modules.md``): every
module belongs to exactly one layer, and imports point down the layers, never up.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

from .module_layers import LAYER_OF, LAYER_ORDER, LAYER_TITLES, MODULES_BY_LAYER

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "latexprep"
MODULE_MAP = ROOT / "docs" / "modules.md"

# Imports that point up the layers, each with the reason it is allowed. A module and the
# module it imports are named; the test fails when an exception is no longer needed, so
# the list can only shrink. There are none today.
UPWARD_IMPORT_EXCEPTIONS: dict[tuple[str, str], str] = {}

# Third-party packages that only the presentation layer may import.
PRESENTATION_ONLY_PACKAGES = frozenset({"click", "rich"})
TESTS = Path(__file__).resolve().parent


def _relative_imports() -> dict[str, list[tuple[str, int, tuple[str, ...]]]]:
    """Collect each module's package-internal imports.

    Returns:
        Each module's imports as (target module, line, imported names), by module name.
    """
    imports: dict[str, list[tuple[str, int, tuple[str, ...]]]] = {}
    for path in sorted(PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found = imports.setdefault(path.stem, [])
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 1:
                if node.module is None:
                    # `from . import name` imports package attributes such as __version__.
                    continue
                names = tuple(alias.name for alias in node.names)
                found.append((node.module.split(".")[0], node.lineno, names))
            elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("latexprep."):
                names = tuple(alias.name for alias in node.names)
                found.append((node.module.split(".")[1], node.lineno, names))
    return imports


def _third_party_imports() -> dict[str, set[str]]:
    """Collect the top-level names of each module's absolute imports.

    The names include standard library modules; a caller compares them with the
    third-party packages it cares about.

    Returns:
        The top-level package names each module imports, by module name.
    """
    found: dict[str, set[str]] = {}
    for path in sorted(PACKAGE.glob("*.py")):
        names: set[str] = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
        found[path.stem] = names
    return found


def _documented_layers() -> dict[str, list[tuple[str, str]]]:
    """Read the layer tables of ``docs/modules.md``.

    Returns:
        Each layer's (module, responsibility) rows, by layer, for every heading that names
        a layer.
    """
    title_to_layer = {title: layer for layer, title in LAYER_TITLES.items()}
    tables: dict[str, list[tuple[str, str]]] = {}
    current: str | None = None
    for line in MODULE_MAP.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            current = title_to_layer.get(line[3:].strip())
            if current is not None:
                tables[current] = []
        elif current is not None and (row := re.fullmatch(r"\| `(\w+)` \| (.+) \|", line)):
            tables[current].append((row.group(1), row.group(2).strip()))
    return tables


class ModuleBoundaryTests(unittest.TestCase):
    """Package-internal imports: no private names across modules and no import cycles."""

    def test_no_module_imports_another_modules_private_names(self) -> None:
        violations = [
            f"{module}.py:{line} imports {name} from {target}"
            for module, entries in _relative_imports().items()
            for target, line, names in entries
            if target != module
            for name in names
            if name.startswith("_") and not name.startswith("__")
        ]
        self.assertEqual(violations, [])

    def test_package_has_no_import_cycles(self) -> None:
        graph = {
            module: {target for target, _, _ in entries if target != module}
            for module, entries in _relative_imports().items()
        }
        cycles: list[list[str]] = []
        state: dict[str, str] = {}
        stack: list[str] = []

        def visit(module: str) -> None:
            state[module] = "active"
            stack.append(module)
            for target in sorted(graph.get(module, ())):
                if target not in graph:
                    continue
                if state.get(target) == "active":
                    cycles.append([*stack[stack.index(target) :], target])
                elif target not in state:
                    visit(target)
            stack.pop()
            state[module] = "done"

        for module in sorted(graph):
            if module not in state:
                visit(module)
        self.assertEqual([" -> ".join(cycle) for cycle in cycles], [])


class LayerTests(unittest.TestCase):
    """Module layers: one per module, imports point down, and ``docs/modules.md`` agrees."""

    def test_every_module_belongs_to_exactly_one_layer(self) -> None:
        listed = [module for modules in MODULES_BY_LAYER.values() for module in modules]
        duplicated = sorted({module for module in listed if listed.count(module) > 1})
        existing = {path.stem for path in PACKAGE.glob("*.py")}
        self.assertEqual(duplicated, [], "listed in more than one layer")
        self.assertEqual(sorted(existing - set(listed)), [], "modules with no layer")
        self.assertEqual(sorted(set(listed) - existing), [], "layer entries without a module")
        self.assertEqual(set(MODULES_BY_LAYER), set(LAYER_ORDER))

    def test_no_module_imports_from_a_layer_above_its_own(self) -> None:
        rank = {layer: index for index, layer in enumerate(LAYER_ORDER)}
        upward = {
            (module, target): f"{module}.py:{line}"
            for module, entries in _relative_imports().items()
            for target, line, _ in entries
            if target != module and rank[LAYER_OF[target]] > rank[LAYER_OF[module]]
        }
        unexplained = [
            f"{where}: {module} ({LAYER_OF[module]}) imports {target} ({LAYER_OF[target]})"
            for (module, target), where in sorted(upward.items())
            if (module, target) not in UPWARD_IMPORT_EXCEPTIONS
        ]
        self.assertEqual(unexplained, [])
        stale = sorted(set(UPWARD_IMPORT_EXCEPTIONS) - set(upward))
        self.assertEqual(stale, [], "exceptions that no import needs any more")

    def test_only_the_presentation_layer_imports_click_or_rich(self) -> None:
        violations = [
            f"{module}.py ({LAYER_OF[module]}) imports {package}"
            for module, packages in _third_party_imports().items()
            for package in sorted(packages & PRESENTATION_ONLY_PACKAGES)
            if LAYER_OF[module] != "presentation"
        ]
        self.assertEqual(violations, [])

    def test_checks_adapters_and_domain_never_import_the_workflow_or_the_cli(self) -> None:
        # The ordering test implies this; it names the boundaries that matter most.
        forbidden = {"orchestration", "presentation"}
        violations = [
            f"{module}.py imports {target}"
            for module, entries in _relative_imports().items()
            if LAYER_OF[module] in {"domain", "adapters", "checks"}
            for target, _, _ in entries
            if LAYER_OF[target] in forbidden
        ]
        self.assertEqual(violations, [])

    def test_module_map_lists_the_same_layers_as_the_mapping(self) -> None:
        documented = _documented_layers()
        self.assertEqual(set(documented), set(LAYER_ORDER), "layer headings in docs/modules.md")
        for layer in LAYER_ORDER:
            with self.subTest(layer=layer):
                self.assertEqual(
                    sorted(module for module, _ in documented[layer]),
                    sorted(MODULES_BY_LAYER[layer]),
                )

    def test_module_map_gives_every_module_a_responsibility(self) -> None:
        for layer, rows in _documented_layers().items():
            for module, responsibility in rows:
                with self.subTest(layer=layer, module=module):
                    self.assertGreater(len(responsibility.strip(" .")), 12)
                    self.assertFalse(responsibility.endswith("."), "end the sentence without a dot")


def _imported_test_modules(source: str) -> list[tuple[int, str]]:
    """List the test modules that the module with ``source`` imports.

    Every import form counts: ``import tests.test_x``, ``from tests.test_x import y`` and
    ``from .test_x import y`` name the test module as the module, while ``from tests import
    test_x`` and ``from . import test_x`` name it among the imported names.

    Args:
        source: The Python source of a module.

    Returns:
        The line and the name of each imported test module.
    """
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            from_package = (node.level, node.module) in {(0, "tests"), (1, None)}
            modules = [alias.name for alias in node.names] if from_package else [node.module or ""]
        else:
            continue
        found.extend(
            (node.lineno, name)
            for name in (module.rsplit(".", 1)[-1] for module in modules)
            if name.startswith("test_")
        )
    return found


class TestSuiteBoundaryTests(unittest.TestCase):
    """Fixtures shared between test modules live in support modules, never in test modules."""

    def test_no_test_module_imports_another_test_module(self) -> None:
        violations = [
            f"{path.name}:{line} imports {name}"
            for path in sorted(TESTS.glob("test_*.py"))
            for line, name in _imported_test_modules(path.read_text(encoding="utf-8"))
        ]
        self.assertEqual(
            violations,
            [],
            "Move the shared fixture into tests/support.py, tests/fakes.py or tests/pdf_fakes.py.",
        )

    def test_every_form_of_importing_a_test_module_is_caught(self) -> None:
        for source in (
            "import tests.test_pdf",
            "import tests.test_pdf as pdf_tests",
            "from tests.test_pdf import ConfigurablePdfRunner",
            "from .test_pdf import ConfigurablePdfRunner",
            "from tests import test_pdf as pdf_tests",
            "from . import test_pdf",
        ):
            with self.subTest(source=source):
                self.assertEqual(_imported_test_modules(source), [(1, "test_pdf")])
        for source in (
            "from tests import SOURCE_ROOT",
            "from . import support",
            "from .support import live_test",
        ):
            with self.subTest(source=source):
                self.assertEqual(_imported_test_modules(source), [])


if __name__ == "__main__":
    unittest.main()
