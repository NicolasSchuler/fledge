"""Module boundaries: shared code is public, and the package has no import cycles.

A name with a leading underscore is private to its module. Code shared between
modules belongs in a public module under a public name, so these tests read every
import statement in the package (including function-level and type-only imports)
instead of trusting convention.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "latexprep"


def _relative_imports() -> dict[str, list[tuple[str, int, tuple[str, ...]]]]:
    """Each module's package-internal imports as (target module, line, imported names)."""
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


class ModuleBoundaryTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
