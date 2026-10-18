from __future__ import annotations

import os
import unittest

from latexprep.file_safety import (
    FileTooLargeError,
    byte_count_text,
    read_file_prefix,
    read_regular_file,
)
from latexprep.limits import MIB
from tests.support import ProjectTestCase


class ByteCountTextTests(unittest.TestCase):
    def test_counts_are_written_in_the_largest_exact_binary_unit(self) -> None:
        self.assertEqual(byte_count_text(256 * MIB), "256 MiB")
        self.assertEqual(byte_count_text(MIB + MIB // 2), "1.5 MiB")
        self.assertEqual(byte_count_text(4096), "4 KiB")
        self.assertEqual(byte_count_text(300), "300 bytes")
        self.assertEqual(byte_count_text(1500), "1500 bytes")


class BoundedReadTests(ProjectTestCase):
    def test_a_file_at_the_limit_is_read_whole_and_one_byte_more_is_refused(self) -> None:
        large = self.root / "large.tex"
        large.write_bytes(b"x" * 2048)
        self.assertEqual(read_regular_file(large, 2048), b"x" * 2048)
        with self.assertRaisesRegex(OSError, "exceeds the scan limit of 1 KiB"):
            read_regular_file(large, 1024)
        with self.assertRaises(FileTooLargeError):
            read_regular_file(large, 2047)

    def test_an_empty_file_reads_within_a_zero_limit(self) -> None:
        (self.root / "empty.tex").write_bytes(b"")
        self.assertEqual(read_regular_file(self.root / "empty.tex", 0), b"")

    def test_oversize_is_distinguishable_from_other_read_failures(self) -> None:
        (self.root / "large.tex").write_bytes(b"x" * 8)
        with self.assertRaises(FileTooLargeError):
            read_regular_file(self.root / "large.tex", 4)
        with self.assertRaises(FileNotFoundError) as missing:
            read_regular_file(self.root / "absent.tex", 4)
        self.assertNotIsInstance(missing.exception, FileTooLargeError)

    def test_special_file_is_refused_without_blocking(self) -> None:
        os.mkfifo(self.root / "pipe")
        with self.assertRaisesRegex(OSError, "not a regular file"):
            read_regular_file(self.root / "pipe", 1024)
        with self.assertRaisesRegex(OSError, "not a regular file"):
            read_file_prefix(self.root / "pipe", 16)

    def test_directory_is_refused(self) -> None:
        with self.assertRaisesRegex(OSError, "not a regular file"):
            read_regular_file(self.root, 1024)

    def test_linked_file_is_refused(self) -> None:
        target = self.root / "target.tex"
        target.write_text("x")
        link = self.root / "link.tex"
        link.symlink_to(target)
        with self.assertRaisesRegex(OSError, "Linked files"):
            read_regular_file(link, 1024)
        with self.assertRaisesRegex(OSError, "Linked files"):
            read_file_prefix(link, 1024)

    def test_refusals_state_their_reason_without_naming_a_path(self) -> None:
        target = self.root / "target.tex"
        target.write_bytes(b"x" * 8)
        link = self.root / "link.tex"
        link.symlink_to(target)
        os.mkfifo(self.root / "pipe")
        for path, limit in ((link, 1024), (self.root / "pipe", 1024), (target, 4)):
            with self.subTest(path=path.name), self.assertRaises(OSError) as raised:
                read_regular_file(path, limit)
            self.assertTrue(raised.exception.strerror)
            self.assertNotIn(str(self.root), str(raised.exception))

    def test_prefix_of_a_longer_file_is_read_without_a_size_limit(self) -> None:
        (self.root / "image.png").write_bytes(b"0123456789")
        self.assertEqual(read_file_prefix(self.root / "image.png", 4), b"0123")
        self.assertEqual(read_file_prefix(self.root / "image.png", 100), b"0123456789")


if __name__ == "__main__":
    unittest.main()
