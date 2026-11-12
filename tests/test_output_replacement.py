"""Output folders: an empty one is filled, an earlier Fledge output is replaced only on request.

A published folder carries ``OUTPUT_MARKER``. ``--force`` (``JobRequest.force``) lets a run
replace such a folder, and only it: the new output is written beside it and swapped into
place once complete, so a run that fails or is blocked leaves the earlier output as it was.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import stat
from pathlib import Path
from unittest.mock import patch

from latexprep.config import Settings
from latexprep.core import run_job
from latexprep.errors import PreparationError, RequestError
from latexprep.findings import Finding
from latexprep.job_context import JobRequest
from latexprep.publication import OutputTransaction, publish_previews, validate_destination
from latexprep.publication_folders import OUTPUT_MARKER, is_fledge_output, is_fledge_report
from latexprep.report import Report
from tests.support import ProjectTestCase, controlled_backends, minimal_project

PASSED_COMPARISON = [
    Finding("pdf.comparison", "Controlled comparison", severity="info", status="passed")
]


def leftovers(folder: Path) -> list[str]:
    """List the hidden staging and replaced directories that publication left in ``folder``."""
    return sorted(path.name for path in folder.iterdir() if path.name.startswith(".fledge-"))


class DestinationTests(ProjectTestCase):
    """Which existing paths :func:`validate_destination` accepts, with and without force."""

    def setUp(self) -> None:
        """Write a project ``paper`` and an earlier Fledge output ``earlier``."""
        super().setUp()
        minimal_project(self.root / "paper")
        self.earlier = self.root / "earlier"
        self.earlier.mkdir()
        (self.earlier / OUTPUT_MARKER).write_text("Fledge wrote this folder.\n")
        (self.earlier / "report.json").write_text("{}\n")

    def validate(self, destination: Path, *, folder: bool = True, force: bool = False) -> Path:
        """Validate ``destination`` as an output folder or file of the project ``paper``."""
        return validate_destination(
            self.root / "paper", destination, "output", folder=folder, force=force
        )

    def test_an_empty_folder_is_accepted_without_force(self) -> None:
        empty = self.root / "empty"
        empty.mkdir()
        self.assertEqual(self.validate(empty), empty.resolve())

    def test_an_earlier_fledge_output_needs_force(self) -> None:
        with self.assertRaises(RequestError) as raised:
            self.validate(self.earlier)
        self.assertIn("Output already exists", str(raised.exception))
        self.assertIn("rerun with --force", raised.exception.suggestion)
        self.assertEqual(self.validate(self.earlier, force=True), self.earlier.resolve())

    def test_force_never_accepts_a_folder_or_file_fledge_did_not_write(self) -> None:
        other = self.root / "other"
        other.mkdir()
        (other / "notes.txt").write_text("mine\n")
        stray = self.root / "stray.json"
        stray.write_text('{"a": 1}\n')
        linked = self.root / "linked"
        linked.symlink_to(self.earlier)
        for destination, folder in ((other, True), (stray, False), (linked, True)):
            with self.subTest(destination=destination.name):
                with self.assertRaises(RequestError) as raised:
                    self.validate(destination, folder=folder, force=True)
                self.assertNotIn("--force", raised.exception.suggestion)

    def test_force_accepts_a_file_that_begins_like_a_fledge_report(self) -> None:
        json_report = self.root / "report.json"
        json_report.write_text(Report("inspect").to_json())
        html_report = self.root / "report.html"
        html_report.write_text(
            '<!doctype html><html lang="en"><head><meta charset="utf-8">'
            "<title>Fledge check report: main.tex</title></head></html>"
        )
        for report in (json_report, html_report):
            with self.subTest(report=report.name):
                self.assertTrue(is_fledge_report(report))
                self.assertEqual(self.validate(report, folder=False, force=True), report.resolve())
                for folder, force in ((False, False), (True, True)):
                    with self.assertRaises(RequestError):
                        self.validate(report, folder=folder, force=force)
        self.assertFalse(is_fledge_report(self.root / "missing.json"))

    def test_a_report_file_is_never_an_existing_folder(self) -> None:
        empty = self.root / "empty"
        empty.mkdir()
        for force in (False, True):
            with self.subTest(force=force), self.assertRaises(RequestError) as raised:
                self.validate(empty, folder=False, force=force)
            self.assertNotIn("empty folder", raised.exception.suggestion)
        with self.assertRaises(RequestError):
            self.validate(self.earlier, folder=False, force=True)

    def test_a_folder_that_contains_the_input_is_never_replaced(self) -> None:
        (self.earlier / "paper").mkdir()
        minimal_project(self.earlier / "paper")
        with self.assertRaises(RequestError) as raised:
            validate_destination(
                self.earlier / "paper", self.earlier, "output", folder=True, force=True
            )
        self.assertIn("Output contains the input", str(raised.exception))


class TransactionTests(ProjectTestCase):
    """Filling, replacing and rolling back with :class:`OutputTransaction`."""

    def earlier_output(self) -> Path:
        """Create an earlier Fledge output ``output`` holding ``old.txt``."""
        output = self.root / "output"
        output.mkdir()
        (output / OUTPUT_MARKER).write_text("Fledge wrote this folder.\n")
        (output / "old.txt").write_text("old\n")
        return output

    def victim(self) -> Path:
        """Create a folder ``victim`` with one file, which no publication may touch."""
        victim = self.root / "victim"
        victim.mkdir()
        (victim / "precious.txt").write_text("keep\n")
        return victim

    def assert_untouched(self, victim: Path) -> None:
        """Assert that ``victim`` still holds only its one file, unchanged."""
        self.assertEqual([path.name for path in victim.iterdir()], ["precious.txt"])
        self.assertEqual((victim / "precious.txt").read_text(), "keep\n")

    def test_a_new_folder_is_written_in_place_and_an_empty_one_beside_it(self) -> None:
        empty = self.root / "empty"
        empty.mkdir()
        with OutputTransaction() as transaction:
            self.assertEqual(transaction.create(self.root / "new"), self.root / "new")
            staging = transaction.create(empty)
            self.assertNotEqual(staging, empty)
            self.assertEqual(staging.parent, empty.parent)
            (staging / "file.txt").write_text("x\n")
            # Nothing is written into the existing folder before the block ends.
            self.assertEqual(list(empty.iterdir()), [])
        for folder in (self.root / "new", empty):
            self.assertTrue(is_fledge_output(folder), folder)
        self.assertEqual((empty / "file.txt").read_text(), "x\n")
        self.assertEqual(leftovers(self.root), [])

    def test_a_private_folder_stays_private_when_the_output_takes_its_place(self) -> None:
        empty = self.root / "private"
        empty.mkdir()
        empty.chmod(0o700)
        with OutputTransaction() as transaction:
            directory = transaction.create(empty)
            # While it is written, the staging folder beside it is private too.
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode) & 0o077, 0)
            (directory / "report.json").write_text("{}\n")
        self.assertEqual(stat.S_IMODE(empty.stat().st_mode), 0o700)
        self.assertTrue((empty / "report.json").is_file())
        empty.chmod(0o750)
        with OutputTransaction(replace_previous=True) as transaction:
            (transaction.create(empty) / "report.json").write_text("{}\n")
        self.assertEqual(stat.S_IMODE(empty.stat().st_mode), 0o750)
        self.assertEqual(leftovers(self.root), [])

    def test_rolling_back_leaves_an_empty_folder_as_it_was(self) -> None:
        empty = self.root / "empty"
        empty.mkdir()
        with self.assertRaises(RuntimeError), OutputTransaction() as transaction:
            directory = transaction.create(empty)
            (transaction.create(empty / "sub") / "file.txt").write_text("x\n")
            (directory / "top.txt").write_text("x\n")
            raise RuntimeError("publication failed")
        self.assertTrue(empty.is_dir())
        self.assertEqual(list(empty.iterdir()), [])
        self.assertEqual(leftovers(self.root), [])

    def test_a_link_planted_in_an_empty_folder_fails_the_run_and_is_never_followed(self) -> None:
        victim = self.victim()
        empty = self.root / "empty"
        empty.mkdir()
        with self.assertRaisesRegex(PreparationError, "nothing new was published"):
            with OutputTransaction() as transaction:
                staging = transaction.create(empty)
                (staging / "sources").mkdir()
                (staging / "sources" / "main.tex").write_text("new\n")
                # Someone plants a link where the output's sources will go.
                (empty / "sources").symlink_to(victim)
        self.assert_untouched(victim)
        # The planted link is not the transaction's to delete; it stays, unfollowed.
        self.assertEqual([path.name for path in empty.iterdir()], ["sources"])
        self.assertTrue((empty / "sources").is_symlink())
        self.assertEqual(leftovers(self.root), [])

    def test_a_folder_replaced_by_a_link_is_refused(self) -> None:
        for replaces in (False, True):
            with self.subTest(replaces=replaces):
                victim = self.victim()
                if replaces:
                    (victim / OUTPUT_MARKER).write_text("Fledge wrote this folder.\n")
                    target = self.earlier_output()
                else:
                    target = self.root / "output"
                    target.mkdir()
                with self.assertRaisesRegex(PreparationError, "no longer a folder of yours"):
                    with OutputTransaction(replace_previous=replaces) as transaction:
                        (transaction.create(target) / "new.txt").write_text("new\n")
                        shutil.rmtree(target)
                        target.symlink_to(victim)
                self.assertTrue(target.is_symlink())
                self.assertEqual(
                    sorted(path.name for path in victim.iterdir()),
                    sorted(["precious.txt", *([OUTPUT_MARKER] if replaces else [])]),
                )
                self.assertEqual(leftovers(self.root), [])
                target.unlink()
                shutil.rmtree(victim)

    def test_rollback_removes_only_what_the_transaction_created(self) -> None:
        empty = self.root / "empty"
        empty.mkdir()
        output = self.earlier_output()
        with self.assertRaises(RuntimeError):
            with OutputTransaction(replace_previous=True) as transaction:
                transaction.create(empty)
                transaction.create(output)
                transaction.create(self.root / "new")
                # Someone else writes into the existing folders meanwhile.
                (empty / "theirs.txt").write_text("theirs\n")
                (output / "theirs.txt").write_text("theirs\n")
                raise RuntimeError("publication failed")
        self.assertEqual([path.name for path in empty.iterdir()], ["theirs.txt"])
        self.assertEqual(
            sorted(path.name for path in output.iterdir()), [OUTPUT_MARKER, "old.txt", "theirs.txt"]
        )
        self.assertFalse((self.root / "new").exists())
        self.assertEqual(leftovers(self.root), [])

    def test_removing_a_replaced_output_never_follows_a_link_in_it(self) -> None:
        victim = self.victim()
        output = self.earlier_output()
        (output / "planted").symlink_to(victim)
        with OutputTransaction(replace_previous=True) as transaction:
            (transaction.create(output) / "new.txt").write_text("new\n")
        self.assert_untouched(victim)
        self.assertEqual(sorted(path.name for path in output.iterdir()), [OUTPUT_MARKER, "new.txt"])
        self.assertEqual(leftovers(self.root), [])

    def test_without_safe_tree_removal_nothing_is_removed_and_the_folders_are_named(self) -> None:
        output = self.earlier_output()
        messages: list[str] = []
        with patch("latexprep.publication_folders.SAFE_TREE_REMOVAL", new=False):
            with OutputTransaction(messages.append, replace_previous=True) as transaction:
                (transaction.create(output) / "new.txt").write_text("new\n")
            [replaced] = leftovers(self.root)
            self.assertTrue(replaced.startswith(".fledge-replaced-"))
            self.assertIn(
                f"Could not remove the replaced output {self.root / replaced}", messages[-1]
            )
            with self.assertRaises(RuntimeError):
                with OutputTransaction(messages.append) as transaction:
                    transaction.create(self.root / "new")
                    raise RuntimeError("publication failed")
        self.assertTrue((self.root / "new").is_dir())
        self.assertIn("remove it manually", messages[-1])

    def test_a_folder_of_another_user_is_neither_accepted_nor_replaced(self) -> None:
        empty = self.root / "empty"
        empty.mkdir()
        output = self.earlier_output()
        with patch("os.getuid", return_value=os.getuid() + 1):
            for folder, force in ((empty, False), (output, True)):
                with self.subTest(folder=folder.name):
                    with self.assertRaises(RequestError) as raised:
                        validate_destination(
                            self.root / "missing", folder, "output", folder=True, force=force
                        )
                    self.assertIn("belongs to another user", str(raised.exception))
                    with self.assertRaisesRegex(PreparationError, "changed while the job ran"):
                        with OutputTransaction(replace_previous=force) as transaction:
                            transaction.create(folder)

    def test_a_replacement_is_written_beside_and_swapped_in_once_complete(self) -> None:
        output = self.earlier_output()
        with OutputTransaction(replace_previous=True) as transaction:
            staging = transaction.create(output)
            self.assertNotEqual(staging, output)
            self.assertEqual(transaction.physical(output / "a" / "b.png"), staging / "a" / "b.png")
            (staging / "new.txt").write_text("new\n")
            # Until the block ends, the earlier output is untouched.
            self.assertTrue((output / "old.txt").is_file())
        self.assertEqual(sorted(path.name for path in output.iterdir()), [OUTPUT_MARKER, "new.txt"])
        self.assertEqual(leftovers(self.root), [])

    def test_a_failed_replacement_keeps_the_earlier_output(self) -> None:
        output = self.earlier_output()
        with (
            self.assertRaises(RuntimeError),
            OutputTransaction(replace_previous=True) as transaction,
        ):
            (transaction.create(output) / "new.txt").write_text("new\n")
            raise RuntimeError("publication failed")
        self.assertEqual(sorted(path.name for path in output.iterdir()), [OUTPUT_MARKER, "old.txt"])
        self.assertEqual(leftovers(self.root), [])

    def test_a_swap_that_fails_restores_the_earlier_output_and_clears_the_artifacts(self) -> None:
        output = self.earlier_output()
        report = Report("prepare", artifacts={"sources": str(output / "sources")})
        rename = Path.rename

        def refuse_staging(path: Path, target: Path) -> Path:
            """Refuse to move the new output into place; move anything else."""
            if path.name.startswith(".fledge-staging-"):
                raise PermissionError(13, "Permission denied", str(path))
            return rename(path, target)

        with patch("latexprep.publication.Path.rename", new=refuse_staging):
            with self.assertRaisesRegex(PreparationError, "left as it was and nothing new"):
                with OutputTransaction(report=report, replace_previous=True) as transaction:
                    (transaction.create(output) / "new.txt").write_text("new\n")
        self.assertEqual(sorted(path.name for path in output.iterdir()), [OUTPUT_MARKER, "old.txt"])
        self.assertEqual(report.artifacts, {})
        self.assertEqual(leftovers(self.root), [])

    def test_previews_of_a_replaced_folder_are_written_beside_and_swapped_in(self) -> None:
        output = self.earlier_output()
        image = self.root / "work" / "gray.png"
        image.parent.mkdir()
        image.write_bytes(b"PNG fixture")
        report = Report("check")
        with OutputTransaction(replace_previous=True) as transaction:
            published = publish_previews(
                transaction, output, ((image, output / "page-0001.png"),), report, lambda: None
            )
        self.assertTrue(published)
        self.assertEqual(
            sorted(path.name for path in output.iterdir()),
            [OUTPUT_MARKER, "page-0001.png", "report.json"],
        )
        self.assertEqual((output / "page-0001.png").read_bytes(), b"PNG fixture")
        self.assertEqual(leftovers(self.root), [])

    def test_without_permission_an_earlier_output_is_not_replaced(self) -> None:
        output = self.earlier_output()
        with self.assertRaisesRegex(PreparationError, "changed while the job ran"):
            with OutputTransaction() as transaction:
                transaction.create(output)
        self.assertTrue((output / "old.txt").is_file())


class PrepareRerunTests(ProjectTestCase):
    """``prepare`` into an empty folder, then again with and without force."""

    def prepare(self, output: Path, *, force: bool = False, backends=None) -> Report:
        """Prepare the project ``paper`` into ``output`` with controlled tools."""
        request = JobRequest("prepare", self.root / "paper", Settings(), output, force=force)
        return asyncio.run(
            run_job(
                request, backends=backends or controlled_backends(compare_pdfs=PASSED_COMPARISON)
            )
        )

    def setUp(self) -> None:
        """Write the project ``paper``."""
        super().setUp()
        minimal_project(self.root / "paper")

    def test_an_empty_folder_is_filled_and_a_rerun_replaces_it_only_with_force(self) -> None:
        output = self.root / "submission"
        output.mkdir()
        first = self.prepare(output)
        self.assertIn(first.outcome, {"passed", "passed_with_advisories"})
        self.assertTrue(is_fledge_output(output))
        self.assertTrue((output / "submission.zip").is_file())
        (output / "stale.txt").write_text("from before\n")
        with self.assertRaises(RequestError) as raised:
            self.prepare(output)
        self.assertIn("--force", raised.exception.suggestion)
        second = self.prepare(output, force=True)
        self.assertIn(second.outcome, {"passed", "passed_with_advisories"})
        self.assertFalse((output / "stale.txt").exists())
        self.assertTrue((output / "manuscript.pdf").is_file())
        self.assertEqual(second.artifacts["archive"], str(output.resolve() / "submission.zip"))
        self.assertEqual(leftovers(self.root), [])

    def test_a_blocked_or_failing_rerun_leaves_the_earlier_output(self) -> None:
        output = self.root / "submission"
        self.prepare(output)
        earlier = (output / "report.json").read_text()
        blocked = self.prepare(
            output,
            force=True,
            backends=controlled_backends(build=RuntimeError("build crashed")),
        )
        self.assertNotIn(blocked.outcome, {"passed", "passed_with_advisories"})
        self.assertEqual((output / "report.json").read_text(), earlier)
        copy = shutil.copyfile

        def fail_on_archive(source, destination, *args, **kwargs):
            """Fail to copy the archive into the new output, as a full disk would."""
            if Path(destination).name == "submission.zip":
                raise OSError(28, "No space left on device")
            return copy(source, destination, *args, **kwargs)

        with (
            patch("latexprep.publication.shutil.copyfile", new=fail_on_archive),
            self.assertRaises(OSError),
        ):
            self.prepare(output, force=True)
        self.assertEqual((output / "report.json").read_text(), earlier)
        self.assertEqual(leftovers(self.root), [])
