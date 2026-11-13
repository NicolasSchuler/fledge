"""Every module of the package stays under 1000 lines.

A module that grows past this size usually holds several responsibilities. Split it along
one of them, as the module map (``docs/modules.md``) describes each module by one, instead
of raising the limit.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "latexprep"
# Every module must have fewer lines than this.
MODULE_LINE_LIMIT = 1000


def oversized_modules(package: Path) -> dict[str, int]:
    """Count the lines of each Python module below ``package`` that reaches the limit.

    Args:
        package: The directory of the package.

    Returns:
        The line count of each module with ``MODULE_LINE_LIMIT`` lines or more, by its path
        relative to ``package``.
    """
    counts = {
        module.relative_to(package).as_posix(): len(module.read_text(encoding="utf-8").splitlines())
        for module in sorted(package.rglob("*.py"))
    }
    return {name: count for name, count in counts.items() if count >= MODULE_LINE_LIMIT}


def describe(oversized: dict[str, int]) -> str:
    """Name the oversized modules with their line counts, for a failure message."""
    listed = ", ".join(f"{name} ({count} lines)" for name, count in sorted(oversized.items()))
    return f"Split these modules below {MODULE_LINE_LIMIT} lines: {listed}"


class ModuleSizeTests(unittest.TestCase):
    """The line limit of every package module, and the message that names the offenders."""

    def test_every_package_module_stays_under_the_line_limit(self) -> None:
        self.assertTrue(any(PACKAGE.glob("*.py")), PACKAGE)
        oversized = oversized_modules(PACKAGE)
        if oversized:
            self.fail(describe(oversized))

    def test_the_failure_names_each_oversized_module_with_its_line_count(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory)
            (package / "small.py").write_text("x = 1\n" * (MODULE_LINE_LIMIT - 1))
            (package / "nested").mkdir()
            (package / "nested" / "large.py").write_text("x = 1\n" * MODULE_LINE_LIMIT)
            (package / "huge.py").write_text("x = 1\n" * 1234)
            oversized = oversized_modules(package)
        self.assertEqual(oversized, {"huge.py": 1234, "nested/large.py": MODULE_LINE_LIMIT})
        self.assertEqual(
            describe(oversized),
            "Split these modules below 1000 lines: huge.py (1234 lines), "
            "nested/large.py (1000 lines)",
        )


if __name__ == "__main__":
    unittest.main()
