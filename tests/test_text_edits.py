"""Text edits and byte-order-mark restoration."""

from __future__ import annotations

import unittest

from latexprep.text_edits import with_bom


class ByteOrderMarkTests(unittest.TestCase):
    """Restoring a removed byte-order mark with :func:`with_bom`."""

    def test_a_removed_mark_is_restored_only_when_the_file_had_one(self) -> None:
        self.assertEqual(with_bom("text", bom=True), "﻿text")
        self.assertEqual(with_bom("text", bom=False), "text")

    def test_the_flag_is_named_so_a_call_site_says_what_it_means(self) -> None:
        with self.assertRaises(TypeError):
            with_bom("text", True)  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
