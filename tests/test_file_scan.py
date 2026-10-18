from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from latexprep.file_scan import inventory_files, plural, read_regular_file


class InventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

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


class BoundedReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_special_file_and_oversized_file_get_different_messages(self) -> None:
        os.mkfifo(self.root / "pipe")
        with self.assertRaisesRegex(OSError, "not a regular file"):
            read_regular_file(self.root / "pipe")
        large = self.root / "large.tex"
        large.write_bytes(b"x" * 2048)
        with self.assertRaisesRegex(OSError, "exceeds the scan limit of 1 KiB"):
            read_regular_file(large, 1024)
        self.assertEqual(read_regular_file(large, 2048), b"x" * 2048)

    def test_linked_file_is_refused(self) -> None:
        target = self.root / "target.tex"
        target.write_text("x")
        link = self.root / "link.tex"
        link.symlink_to(target)
        with self.assertRaisesRegex(OSError, "Linked files"):
            read_regular_file(link)


class PluralTests(unittest.TestCase):
    def test_noun_agrees_with_the_count(self) -> None:
        self.assertEqual(plural(0, "float"), "0 floats")
        self.assertEqual(plural(1, "float"), "1 float")
        self.assertEqual(plural(3, "float"), "3 floats")


if __name__ == "__main__":
    unittest.main()
