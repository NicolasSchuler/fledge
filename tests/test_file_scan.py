from __future__ import annotations

import os
import unittest

from latexprep.file_scan import inventory_files
from latexprep.finding_text import plural
from tests.support import ProjectTestCase


class InventoryTests(ProjectTestCase):
    def test_unreadable_directory_is_named_in_the_issue(self) -> None:
        (self.root / "main.tex").write_text("x")
        locked = self.root / "figures" / "raw"
        locked.mkdir(parents=True)
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o700)
        if os.access(locked, os.R_OK):
            self.skipTest("Directory permissions are not enforced for this user")
        inventory = inventory_files(self.root)
        self.assertEqual(
            inventory.issues, ["Directory could not be read (check its permissions): figures/raw"]
        )
        self.assertEqual(list(inventory.files), ["main.tex"])


class PluralTests(unittest.TestCase):
    def test_noun_agrees_with_the_count(self) -> None:
        self.assertEqual(plural(0, "float"), "0 floats")
        self.assertEqual(plural(1, "float"), "1 float")
        self.assertEqual(plural(3, "float"), "3 floats")


if __name__ == "__main__":
    unittest.main()
