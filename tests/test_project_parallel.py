"""Cancellation stops archive readers before their workspace can be released."""

from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from latexprep import project
from latexprep.scheduler import run_in_thread


class ArchiveCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_during_file_copy_skips_later_files_and_removes_partial_archive(self):
        started, release = threading.Event(), threading.Event()
        opened = []
        original_open = project._open_regular

        @contextmanager
        def delayed_open(directory, relative):
            opened.append(relative)
            with original_open(directory, relative) as stream:
                if relative == "a.tex":
                    started.set()
                    if not release.wait(5):
                        raise AssertionError("Archive reader was never released")
                yield stream

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "input"
            source.mkdir()
            for name in ("a.tex", "b.tex"):
                (source / name).write_bytes(b"bounded archive fixture\n" * 100)
            archive = base / "submission.zip"
            with patch("latexprep.project._open_regular", side_effect=delayed_open):
                task = asyncio.create_task(run_in_thread(project.create_archive, source, archive))
                try:
                    self.assertTrue(await asyncio.to_thread(started.wait, 5))
                    task.cancel()
                    # Give run_in_thread ownership of cancellation before unblocking its reader.
                    await asyncio.sleep(0)
                    release.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                finally:
                    release.set()
                    await asyncio.gather(task, return_exceptions=True)
            self.assertEqual(opened, ["a.tex"])
            self.assertFalse(archive.exists())
            self.assertTrue((source / "b.tex").is_file())
