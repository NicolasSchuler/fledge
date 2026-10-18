from __future__ import annotations

import ast
import unittest
from pathlib import Path

from latexprep import limits

MODULE = Path(limits.__file__)
UNIT_SUFFIXES = ("_BYTES", "_CHARS", "_ENTRIES", "_LEVELS", "_MB", "_SECONDS")


def _constants() -> dict[str, int]:
    return {name: value for name, value in vars(limits).items() if name.isupper()}


class LimitsTests(unittest.TestCase):
    def test_binary_units_build_on_each_other(self) -> None:
        self.assertEqual(limits.KIB, 1024)
        self.assertEqual(limits.MIB, 1024 * limits.KIB)

    def test_every_other_constant_names_its_unit(self) -> None:
        constants = _constants()
        self.assertGreater(len(constants), 2)
        unitless = [
            name
            for name in constants
            if name not in {"KIB", "MIB"} and not name.endswith(UNIT_SUFFIXES)
        ]
        self.assertEqual(unitless, [])

    def test_limits_are_positive_integers(self) -> None:
        for name, value in _constants().items():
            with self.subTest(name=name):
                self.assertIs(type(value), int)
                self.assertGreater(value, 0)

    def test_stride_and_chunk_sizes_keep_their_established_values(self) -> None:
        self.assertEqual(limits.IO_CHUNK_BYTES, 64 * 1024)
        self.assertEqual(limits.CANCELLATION_STRIDE_CHARS, 4096)
        self.assertEqual(limits.MAX_PDF_NESTING_LEVELS, 100)
        self.assertEqual(limits.DEFAULT_LEASE_MEMORY_MB, 64)

    def test_tool_defaults_keep_their_established_values(self) -> None:
        self.assertEqual(limits.DEFAULT_TOOL_TIMEOUT_SECONDS, 120)
        self.assertEqual(limits.DEFAULT_BUILD_MEMORY_MB, 1024)
        self.assertEqual(limits.DEFAULT_MAX_PROJECT_BYTES, 256 * limits.MIB)
        self.assertEqual(limits.DEFAULT_MAX_GENERATED_FILE_BYTES, limits.DEFAULT_MAX_PROJECT_BYTES)

    def test_the_module_is_a_leaf_that_imports_no_package_module(self) -> None:
        tree = ast.parse(MODULE.read_text(encoding="utf-8"))
        imported = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Import | ast.ImportFrom)
            and not (isinstance(node, ast.ImportFrom) and node.module == "__future__")
        ]
        self.assertEqual(imported, [])


if __name__ == "__main__":
    unittest.main()
