from __future__ import annotations

import unittest

from latexprep.file_safety import MIB, byte_count_text


class ByteCountTextTests(unittest.TestCase):
    def test_counts_are_written_in_the_largest_exact_binary_unit(self) -> None:
        self.assertEqual(byte_count_text(256 * MIB), "256 MiB")
        self.assertEqual(byte_count_text(MIB + MIB // 2), "1.5 MiB")
        self.assertEqual(byte_count_text(4096), "4 KiB")
        self.assertEqual(byte_count_text(300), "300 bytes")
        self.assertEqual(byte_count_text(1500), "1500 bytes")


if __name__ == "__main__":
    unittest.main()
