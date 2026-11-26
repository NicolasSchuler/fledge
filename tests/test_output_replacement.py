"""Output folders: an empty one is filled, an earlier Fledge output is replaced only on request.

A published folder carries ``OUTPUT_MARKER``. ``--force`` (``JobRequest.force``) lets a run
replace such a folder, and only it: the new output is written beside it and swapped into
place once complete, so a run that fails or is blocked leaves the earlier output as it was.
"""

from __future__ import annotations

import asyncio
import errno
import json
import os
import shutil
import stat
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from latexprep import publication_folders
from latexprep.cli import cli
from latexprep.config import Settings
from latexprep.core import run_job
from latexprep.errors import PreparationError, RequestError
from latexprep.findings import Finding
from latexprep.job_context import JobRequest
from latexprep.pdf_artwork_options import PdfArtworkOptions
from latexprep.publication import (
    OutputTransaction,
    publish_previews,
    validate_destination,
    validate_output_directory,
)
from latexprep.publication_folders import OUTPUT_MARKER, is_fledge_output, is_fledge_report
from latexprep.report import Outcome, Report
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


class ReportFileRerunTests(ProjectTestCase):
    """A rerun replaces an earlier report file only with ``--force``, and only once complete."""

    def test_a_report_file_is_replaced_only_with_force(self) -> None:
        minimal_project(self.root / "paper")
        report = self.root / "report.json"
        command = ["inspect", str(self.root / "paper"), "--quiet", "--report", str(report)]
        runner = CliRunner()
        first = runner.invoke(cli, command)
        self.assertIn(first.exit_code, {0, 1}, first.output)
        self.assertEqual(json.loads(report.read_text())["command"], "inspect")
        refused = runner.invoke(cli, command)
        self.assertEqual(refused.exit_code, 4, refused.output)
        self.assertIn("--force", refused.stderr)
        replaced = runner.invoke(cli, [*command, "--force"])
        self.assertIn(replaced.exit_code, {0, 1}, replaced.output)
        self.assertEqual(json.loads(report.read_text())["command"], "inspect")
        # A file that is not a Fledge report is never replaced, even with --force.
        notes = self.root / "notes.json"
        notes.write_text('{"mine": true}\n')
        kept = runner.invoke(
            cli, ["inspect", str(self.root / "paper"), "--quiet", "--report", str(notes), "--force"]
        )
        self.assertEqual(kept.exit_code, 4, kept.output)
        self.assertEqual(json.loads(notes.read_text()), {"mine": True})
        self.assertEqual(
            sorted(path.name for path in self.root.iterdir()),
            ["notes.json", "paper", "report.json"],
        )

    def test_a_rejected_rerun_preserves_earlier_reports_even_with_force(self) -> None:
        earlier = self.write("report.json", Report("inspect", outcome=Outcome.PASSED).to_json())
        original = earlier.read_bytes()
        result = CliRunner().invoke(
            cli,
            ["inspect", str(self.root / "missing"), "--report", str(earlier), "--force", "--json"],
        )
        self.assertEqual(result.exit_code, 4, result.output)
        self.assertEqual(json.loads(result.stdout)["outcome"], "error")
        self.assertEqual(earlier.read_bytes(), original)

    def test_a_replaced_report_file_keeps_its_permissions(self) -> None:
        # Under the usual umask, a report created like any new file would be 0644.
        self.addCleanup(os.umask, os.umask(0o022))
        minimal_project(self.root / "paper")
        report = self.root / "report.json"
        command = ["inspect", str(self.root / "paper"), "--quiet", "--report", str(report)]
        runner = CliRunner()
        first = runner.invoke(cli, command)
        self.assertIn(first.exit_code, {0, 1}, first.output)
        report.chmod(0o600)
        replaced = runner.invoke(cli, [*command, "--force"])
        self.assertIn(replaced.exit_code, {0, 1}, replaced.output)
        self.assertEqual(stat.S_IMODE(report.stat().st_mode), 0o600)
        self.assertEqual(
            sorted(path.name for path in self.root.iterdir()), ["paper", "report.json"]
        )


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

    def test_new_and_empty_outputs_stay_behind_a_private_wrapper_until_rename(self) -> None:
        self.addCleanup(os.umask, os.umask(0o027))
        new, empty = self.root / "new", self.root / "empty"
        empty.mkdir()
        empty.chmod(0o755)
        modes = {new: 0o750, empty: 0o755}
        payloads: dict[Path, Path] = {}
        installed = []
        rename = publication_folders._rename_exclusive

        def check_at_rename(source: Path, target: Path) -> None:
            """Inspect permissions at the last point before installation."""
            if target in modes:
                self.assertEqual(source, payloads[target])
                self.assertEqual(source.parent.parent, target.parent)
                self.assertEqual(stat.S_IMODE(source.parent.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(source.stat().st_mode), modes[target])
                installed.append(target)
            rename(source, target)

        with (
            patch.object(publication_folders, "_rename_exclusive", check_at_rename),
            OutputTransaction() as transaction,
        ):
            for folder in modes:
                staging = transaction.create(folder)
                payloads[folder] = staging
                self.assertNotEqual(staging, folder)
                (staging / "file.txt").write_text("x\n")
            self.assertFalse(new.exists())
            self.assertEqual(list(empty.iterdir()), [])
        self.assertEqual(installed, [new, empty])
        for folder, mode in modes.items():
            self.assertTrue(is_fledge_output(folder), folder)
            self.assertEqual((folder / "file.txt").read_text(), "x\n")
            self.assertEqual(stat.S_IMODE(folder.stat().st_mode), mode)
        self.assertEqual(leftovers(self.root), [])

    def test_new_outputs_inherit_shared_directory_settings_without_chmod(self) -> None:
        self.addCleanup(os.umask, os.umask(0o027))
        parent = self.root / "shared"
        parent.mkdir()
        parent.chmod(0o2770)
        ordinary = parent / "ordinary"
        ordinary.mkdir()
        output = parent / "output"
        # No chmod means a nonmember of the parent group won't lose inherited setgid.
        with (
            patch.object(Path, "chmod", side_effect=AssertionError("unnecessary chmod")),
            OutputTransaction() as transaction,
        ):
            payload = transaction.create(output)
            self.assertEqual(stat.S_IMODE(payload.parent.stat().st_mode) & 0o777, 0o700)
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), stat.S_IMODE(ordinary.stat().st_mode))
        self.assertEqual(output.stat().st_gid, ordinary.stat().st_gid)
        self.assertEqual(leftovers(parent), [])

    def test_new_output_restores_its_mode_after_staging_under_a_restrictive_umask(self) -> None:
        self.addCleanup(os.umask, os.umask(0o427))
        output = self.root / "new"
        with OutputTransaction() as transaction:
            payload = transaction.create(output)
            self.assertEqual(stat.S_IMODE(payload.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(payload.stat().st_mode), 0o750)
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o350)
        self.assertTrue(is_fledge_output(output))
        self.assertEqual(leftovers(self.root), [])

    def test_a_failed_mode_change_does_not_publish_with_different_permissions(self) -> None:
        self.addCleanup(os.umask, os.umask(0o027))
        output = self.earlier_output()
        output.chmod(0o700)
        # The filesystem can report success even when the requested mode does not stick.
        with (
            patch.object(Path, "chmod", return_value=None) as chmod,
            self.assertRaisesRegex(PreparationError, "could not apply the directory permissions"),
            OutputTransaction(replace_previous=True) as transaction,
        ):
            payload = transaction.create(output)
        chmod.assert_called_once_with(0o700, follow_symlinks=False)
        self.assertFalse(payload.exists())
        self.assertEqual((output / "old.txt").read_text(), "old\n")
        self.assertEqual(leftovers(self.root), [])

    def test_a_failed_payload_creation_removes_the_private_wrapper(self) -> None:
        output = self.root / "new"
        mkdir = Path.mkdir

        def refuse_payload(path: Path, *args, **kwargs) -> None:
            """Fail to create the payload as a full disk would; create any other folder."""
            if path.name == "payload":
                raise OSError(errno.ENOSPC, "No space left on device", str(path))
            mkdir(path, *args, **kwargs)

        with (
            patch.object(Path, "mkdir", refuse_payload),
            self.assertRaises(OSError) as raised,
            OutputTransaction() as transaction,
        ):
            transaction.create(output)
        self.assertEqual(raised.exception.errno, errno.ENOSPC)
        # The wrapper was removed, so the error needs no note about leftovers.
        self.assertEqual(getattr(raised.exception, "__notes__", []), [])
        self.assertFalse(output.exists())
        self.assertEqual(leftovers(self.root), [])

    def test_a_failed_staging_removes_every_staged_output_and_says_so(self) -> None:
        first, second = self.root / "first", self.root / "second"
        set_mode = publication_folders._set_mode
        first_staged: list[bool] = []

        def refuse_second_payload(path: Path, current: int, wanted: int) -> None:
            """Fail to open the second payload for writing; set any other mode."""
            if first_staged and path.name == "payload":
                raise OSError(errno.EIO, "Input/output error", str(path))
            set_mode(path, current, wanted)

        messages: list[str] = []
        with (
            patch.object(publication_folders, "_set_mode", refuse_second_payload),
            self.assertRaises(OSError) as raised,
            OutputTransaction(messages.append) as transaction,
        ):
            (transaction.create(first) / "file.txt").write_text("x\n")
            first_staged.append(True)
            transaction.create(second)
        self.assertEqual(raised.exception.errno, errno.EIO)
        self.assertEqual(getattr(raised.exception, "__notes__", []), [])
        self.assertEqual(messages, ["Publication failed; removing the partial output"])
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())
        self.assertEqual(leftovers(self.root), [])

    def test_a_staging_wrapper_that_cannot_be_removed_is_named_on_the_error(self) -> None:
        output = self.root / "new"
        set_mode = publication_folders._set_mode

        def fail_after_writing(path: Path, current: int, wanted: int) -> None:
            """Leave a file in the payload, then fail as a full disk would."""
            if path.name == "payload":
                (path / "partial.txt").write_text("partial\n")
                raise OSError(errno.ENOSPC, "No space left on device", str(path))
            set_mode(path, current, wanted)

        with (
            patch.object(publication_folders, "_set_mode", fail_after_writing),
            self.assertRaises(OSError) as raised,
            OutputTransaction() as transaction,
        ):
            transaction.create(output)
        self.assertEqual(raised.exception.errno, errno.ENOSPC)
        # The payload is not empty, so it and its wrapper stay, and the error says where.
        [holding] = leftovers(self.root)
        wrapper = self.root / holding
        [note] = raised.exception.__notes__
        self.assertIn(f"Could not remove the incomplete staging wrapper {wrapper} (", note)
        self.assertEqual((wrapper / "payload" / "partial.txt").read_text(), "partial\n")
        self.assertFalse(output.exists())

    def test_a_private_folder_stays_private_when_the_output_takes_its_place(self) -> None:
        empty = self.root / "private"
        empty.mkdir()
        empty.chmod(0o700)
        with OutputTransaction() as transaction:
            directory = transaction.create(empty)
            self.assertEqual(stat.S_IMODE(directory.parent.stat().st_mode) & 0o777, 0o700)
            (directory / "report.json").write_text("{}\n")
        self.assertEqual(stat.S_IMODE(empty.stat().st_mode), 0o700)
        self.assertTrue((empty / "report.json").is_file())
        empty.chmod(0o750)
        with OutputTransaction(replace_previous=True) as transaction:
            (transaction.create(empty) / "report.json").write_text("{}\n")
        self.assertEqual(stat.S_IMODE(empty.stat().st_mode), 0o750)
        self.assertEqual(leftovers(self.root), [])

    def test_a_private_empty_folder_put_back_after_a_failed_swap_stays_private(self) -> None:
        # Under the usual umask, a folder made with the default mode would be 0755.
        self.addCleanup(os.umask, os.umask(0o022))
        private = self.root / "private"
        private.mkdir()
        private.chmod(0o700)
        rename = publication_folders._rename_exclusive

        def refuse_staging(path: Path, target: Path) -> None:
            """Refuse to move a new output into place; move anything else."""
            if path == staging:
                raise PermissionError(13, "Permission denied", str(path))
            rename(path, target)

        # The output cannot move into the private folder, which is put back.
        with patch.object(publication_folders, "_rename_exclusive", refuse_staging):
            with self.assertRaisesRegex(PreparationError, "left as it was"):
                with OutputTransaction() as transaction:
                    staging = transaction.create(private)
                    (staging / "new.txt").write_text("new\n")
        self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o700)
        # A second output cannot move into its folder, so the first one moves back out.
        other = self.root / "other"
        other.mkdir()
        with self.assertRaisesRegex(PreparationError, "left as it was"):
            with OutputTransaction() as transaction:
                (transaction.create(private) / "new.txt").write_text("new\n")
                (transaction.create(other) / "new.txt").write_text("new\n")
                (other / "theirs.txt").write_text("theirs\n")
        self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o700)
        self.assertEqual(list(private.iterdir()), [])
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

    def test_a_failing_writer_does_not_remove_a_second_writers_commit(self) -> None:
        output = self.root / "output"
        with self.assertRaisesRegex(RuntimeError, "copy failed"), OutputTransaction() as failing:
            (failing.create(output) / "partial.txt").write_text("partial")
            with OutputTransaction() as winner:
                (winner.create(output) / "winner.txt").write_text("complete")
            raise RuntimeError("copy failed")
        self.assertEqual((output / "winner.txt").read_text(), "complete")
        self.assertFalse((output / "partial.txt").exists())
        self.assertEqual(leftovers(self.root), [])

    def test_a_late_empty_directory_is_not_replaced_and_earlier_moves_are_undone(self) -> None:
        output, previews = self.root / "output", self.root / "previews"
        rename = publication_folders._rename_exclusive
        arrivals = []

        def arrive_at_commit(source: Path, destination: Path) -> None:
            """Create an empty preview destination at the final rename, after staging."""
            if destination == previews:
                self.assertTrue((output / OUTPUT_MARKER).is_file())
                destination.mkdir()
                arrivals.append(destination.stat().st_ino)
            rename(source, destination)

        report = Report("prepare", artifacts={"preview.1": str(previews / "page-0001.png")})
        with (
            patch.object(publication_folders, "_rename_exclusive", arrive_at_commit),
            self.assertRaisesRegex(PreparationError, "nothing new was published"),
            OutputTransaction(report=report, replace_previous=True) as transaction,
        ):
            transaction.create(output)
            transaction.create(previews)
        self.assertEqual(arrivals, [previews.stat().st_ino])
        self.assertEqual(list(previews.iterdir()), [])
        self.assertFalse(output.exists())
        self.assertEqual(report.artifacts, {})
        self.assertEqual(leftovers(self.root), [])

    def test_a_late_empty_arrival_does_not_overwrite_or_destroy_a_force_backup(self) -> None:
        output = self.earlier_output()
        rename = publication_folders._rename_exclusive

        def arrive_during_replacement(source: Path, destination: Path) -> None:
            """Create an empty directory after the old output has been moved aside."""
            if destination == output and source == staging:
                destination.mkdir()
            rename(source, destination)

        messages: list[str] = []
        with (
            patch.object(publication_folders, "_rename_exclusive", arrive_during_replacement),
            self.assertRaisesRegex(PreparationError, "check rollback diagnostics"),
            OutputTransaction(messages.append, replace_previous=True) as transaction,
        ):
            staging = transaction.create(output)
        self.assertEqual(list(output.iterdir()), [])
        [holding] = leftovers(self.root)
        [backup] = (self.root / holding).iterdir()
        self.assertEqual((backup / "old.txt").read_text(), "old\n")
        self.assertTrue(any(str(backup) in message for message in messages))

    def test_a_failed_second_move_does_not_remove_a_replaced_first_destination(self) -> None:
        output, other = self.root / "output", self.root / "other"
        displaced = self.root / "displaced"
        rename = publication_folders._rename_exclusive

        def replace_first(source: Path, destination: Path) -> None:
            """Replace the first publication before the second move fails."""
            if destination == other:
                output.rename(displaced)
                output.mkdir()
                (output / "theirs.txt").write_text("keep")
                other.mkdir()
            rename(source, destination)

        messages: list[str] = []
        with (
            patch.object(publication_folders, "_rename_exclusive", replace_first),
            self.assertRaisesRegex(PreparationError, "check rollback diagnostics"),
            OutputTransaction(messages.append) as transaction,
        ):
            transaction.create(output)
            transaction.create(other)
        self.assertEqual((output / "theirs.txt").read_text(), "keep")
        self.assertTrue(any("Could not undo publication" in message for message in messages))
        self.assertEqual(leftovers(self.root), [])

    def test_interrupt_during_second_swap_restores_both_earlier_outputs(self) -> None:
        for move in ("set aside", "before install", "after install"):
            with self.subTest(move=move):
                self._check_interrupted_second_swap(move)

    def _check_interrupted_second_swap(self, move: str) -> None:
        """Interrupt at one move boundary; both original directory identities must return."""
        first, second = self.root / "output", self.root / "previews"
        accepted = {}
        for output in (first, second):
            output.mkdir(exist_ok=True)
            (output / OUTPUT_MARKER).write_text("Fledge wrote this folder.\n")
            (output / "old.txt").write_text(output.name)
            accepted[output] = output.stat().st_ino
        preview = str(second / "page-0001.png")
        report = Report(
            "prepare", artifacts={"archive": str(first / "archive.zip"), "preview.1": preview}
        )
        report.findings.append(
            Finding("pdf.comparison", "Preview", details={"artifacts": [preview]})
        )
        interrupt = KeyboardInterrupt("interrupt the second swap")
        rename = publication_folders._rename_exclusive

        def interrupt_swap(source: Path, target: Path) -> None:
            """Inject a single interrupt around a rename in the second replacement."""
            matches = (
                source == second and target.parent == payload.parent
                if move == "set aside"
                else source == payload and target == second
            )
            if matches:
                self.assertTrue((first / "new.txt").is_file())
                if move != "before install":
                    rename(source, target)
                raise interrupt
            rename(source, target)

        with (
            patch.object(publication_folders, "_rename_exclusive", interrupt_swap),
            self.assertRaises(KeyboardInterrupt) as raised,
            OutputTransaction(report=report, replace_previous=True) as transaction,
        ):
            (transaction.create(first) / "new.txt").write_text("new")
            payload = transaction.create(second)
            (payload / "new.txt").write_text("new")
        self.assertIs(raised.exception, interrupt)
        self.assertEqual(getattr(interrupt, "__notes__", []), [])
        for output, inode in accepted.items():
            self.assertEqual(output.stat().st_ino, inode)
            self.assertEqual((output / "old.txt").read_text(), output.name)
            self.assertEqual({path.name for path in output.iterdir()}, {OUTPUT_MARKER, "old.txt"})
        self.assertEqual(leftovers(self.root), [])
        self.assertEqual(report.artifacts, {})
        self.assertEqual(report.findings[0].details["artifacts"], [])

    def test_two_staged_replacements_preserve_the_first_commit(self) -> None:
        output = self.earlier_output()
        with self.assertRaisesRegex(PreparationError, "accepted output folder changed"):
            with OutputTransaction(replace_previous=True) as second:
                (second.create(output) / "second.txt").write_text("second")
                with OutputTransaction(replace_previous=True) as first:
                    (first.create(output) / "first.txt").write_text("first")
        self.assertEqual((output / "first.txt").read_text(), "first")
        self.assertFalse((output / "second.txt").exists())
        self.assertEqual(leftovers(self.root), [])

    def test_a_folder_whose_permissions_change_before_the_swap_is_not_replaced(self) -> None:
        empty = self.root / "empty"
        empty.mkdir()
        output = self.earlier_output()
        for folder, replaces, contents in (
            (empty, False, []),
            (output, True, [OUTPUT_MARKER, "old.txt"]),
        ):
            folder.chmod(0o755)
            with self.subTest(folder=folder.name):
                with self.assertRaises(PreparationError) as raised:
                    with OutputTransaction(replace_previous=replaces) as transaction:
                        (transaction.create(folder) / "new.txt").write_text("new\n")
                        # Another process tightens the folder after it was accepted.
                        folder.chmod(0o700)
                self.assertIn("the accepted output folder changed", str(raised.exception))
                self.assertIn("nothing new was published", str(raised.exception))
                self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
                self.assertEqual(sorted(path.name for path in folder.iterdir()), contents)
                self.assertEqual(leftovers(self.root), [])

    def test_an_output_that_loses_its_marker_before_the_swap_is_not_replaced(self) -> None:
        output = self.earlier_output()
        with self.assertRaises(PreparationError) as raised:
            with OutputTransaction(replace_previous=True) as transaction:
                (transaction.create(output) / "new.txt").write_text("new\n")
                # Without its marker the folder is no longer one that Fledge may replace.
                (output / OUTPUT_MARKER).unlink()
        self.assertIn("it no longer holds the Fledge output marker", str(raised.exception))
        self.assertIn("nothing new was published", str(raised.exception))
        self.assertEqual(sorted(path.name for path in output.iterdir()), ["old.txt"])
        self.assertEqual((output / "old.txt").read_text(), "old\n")
        self.assertEqual(leftovers(self.root), [])

    def test_a_folder_that_loses_its_marker_while_set_aside_is_put_back(self) -> None:
        output = self.earlier_output()
        accepted = output.stat().st_ino
        rename = publication_folders._rename_exclusive

        def lose_marker_when_set_aside(source: Path, destination: Path) -> None:
            """Move the folder aside, then delete its marker as another process might."""
            rename(source, destination)
            if source == output:
                (destination / OUTPUT_MARKER).unlink()

        with (
            patch.object(publication_folders, "_rename_exclusive", lose_marker_when_set_aside),
            self.assertRaises(PreparationError) as raised,
            OutputTransaction(replace_previous=True) as transaction,
        ):
            (transaction.create(output) / "new.txt").write_text("new\n")
        self.assertIn("it no longer holds the Fledge output marker", str(raised.exception))
        self.assertIn("nothing new was published", str(raised.exception))
        # The very folder that was accepted is back in its place, as the failed check left it.
        self.assertEqual(output.stat().st_ino, accepted)
        self.assertEqual(sorted(path.name for path in output.iterdir()), ["old.txt"])
        self.assertEqual(leftovers(self.root), [])

    def test_publication_rejects_a_new_destination_after_request_validation(self) -> None:
        output = self.earlier_output()
        source = self.root / "source"
        source.mkdir()
        path, identity = validate_output_directory(source, output, "output", force=True)
        with OutputTransaction(replace_previous=True) as other:
            (other.create(output) / "other.txt").write_text("other")
        with (
            self.assertRaisesRegex(PreparationError, "changed while the job ran"),
            OutputTransaction(replace_previous=True, accepted={path: identity}) as transaction,
        ):
            transaction.create(path)
        self.assertEqual((output / "other.txt").read_text(), "other")
        self.assertEqual(leftovers(self.root), [])

    def test_a_busy_publication_lock_retains_the_previous_output_and_clears_artifacts(self):
        output = self.earlier_output()
        report = Report(command="prepare")
        report.artifacts["archive"] = str(output / "submission.zip")
        with (
            patch("latexprep.publication_folders.fcntl.flock", side_effect=BlockingIOError),
            self.assertRaisesRegex(PreparationError, "Another publication is committing"),
            OutputTransaction(report=report, replace_previous=True) as transaction,
        ):
            (transaction.create(output) / "new.txt").write_text("new")
        self.assertTrue(is_fledge_output(output))
        self.assertFalse((output / "new.txt").exists())
        self.assertEqual(report.artifacts, {})
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
                payload = transaction.create(output)
                (payload / "new.txt").write_text("new\n")
            self.assertEqual(leftovers(self.root), [payload.parent.name])
            [backup] = payload.parent.iterdir()
            self.assertEqual((backup / "old.txt").read_text(), "old\n")
            self.assertIn(str(payload.parent), messages[-1])
            with self.assertRaises(RuntimeError):
                with OutputTransaction(messages.append) as transaction:
                    staging = transaction.create(self.root / "new")
                    raise RuntimeError("publication failed")
        self.assertFalse((self.root / "new").exists())
        self.assertTrue(staging.is_dir())
        self.assertIn(str(staging), messages[-1])
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
        rename = publication_folders._rename_exclusive

        def refuse_staging(path: Path, target: Path) -> None:
            """Refuse to move the new output into place; move anything else."""
            if path == staging:
                raise PermissionError(13, "Permission denied", str(path))
            rename(path, target)

        with patch.object(publication_folders, "_rename_exclusive", refuse_staging):
            with self.assertRaisesRegex(PreparationError, "left as it was and nothing new"):
                with OutputTransaction(report=report, replace_previous=True) as transaction:
                    staging = transaction.create(output)
                    (staging / "new.txt").write_text("new\n")
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


async def preview_measurement(_pdf, _root, _main, work, *_args, **_options) -> list[Finding]:
    """Measure like the configured PDF checks: write one grayscale preview into ``work``."""
    work.mkdir(parents=True)
    preview = work / "gray.png"
    preview.write_bytes(b"new preview")
    return [
        Finding(
            "pdf.grayscale_preview",
            "Generated",
            severity="info",
            status="passed",
            details={"artifacts": [str(preview)]},
        )
    ]


class PreviewRerunTests(ProjectTestCase):
    """``check --preview-output DIR --force`` replaces an earlier preview folder only on success.

    A blocked run still writes its previews into a new or empty folder.
    """

    def setUp(self) -> None:
        """Write a passing project ``paper``, a ``broken`` one and earlier previews ``previews``.

        ``broken`` inputs a file that does not exist, which the paper must fix. The name
        has a folder, so it is never looked up in the TeX installation and the run is
        blocked on a computer with TeX and on one without.
        """
        super().setUp()
        minimal_project(self.root / "paper")
        self.write(
            "broken/main.tex",
            "\\documentclass{article}\n\\begin{document}\n"
            "\\input{sections/absent}\n\\end{document}\n",
        )
        self.previews = self.root / "previews"
        self.previews.mkdir()
        (self.previews / OUTPUT_MARKER).write_text("Fledge wrote this folder.\n")
        (self.previews / "page-0001.png").write_bytes(b"earlier preview")

    def check(
        self, project: str, previews: Path | None = None, messages: list[str] | None = None
    ) -> Report:
        """Check ``project`` with grayscale previews and force, with controlled tools.

        Args:
            project: The project folder in the test directory.
            previews: The preview output; the earlier previews by default.
            messages: Receives the progress messages, if given.

        Returns:
            The report of the job.
        """
        settings = Settings(rendered_pdf_artwork=PdfArtworkOptions(grayscale_preview=True))
        request = JobRequest(
            "check",
            self.root / project,
            settings,
            preview_output=previews or self.previews,
            force=True,
        )
        progress = messages.append if messages is not None else None
        with patch(
            "latexprep.document_tasks.inspect_configured_pdf_checks",
            side_effect=preview_measurement,
        ):
            return asyncio.run(run_job(request, progress, backends=controlled_backends()))

    def test_a_blocked_check_leaves_an_earlier_preview_folder_as_it_was(self) -> None:
        messages: list[str] = []
        report = self.check("broken", messages=messages)
        self.assertEqual(report.outcome, Outcome.BLOCKED)
        self.assertEqual(
            sorted(path.name for path in self.previews.iterdir()), [OUTPUT_MARKER, "page-0001.png"]
        )
        self.assertEqual((self.previews / "page-0001.png").read_bytes(), b"earlier preview")
        # The report points at no preview, and the progress says why.
        self.assertEqual(report.artifacts, {})
        (preview,) = [item for item in report.findings if item.rule == "pdf.grayscale_preview"]
        self.assertEqual(preview.details["previews_exported"], 0)
        self.assertTrue(any("Left the earlier preview output" in item for item in messages))
        self.assertEqual(leftovers(self.root), [])

    def test_a_blocked_check_still_writes_its_previews_into_a_new_folder(self) -> None:
        fresh = self.root / "fresh"
        report = self.check("broken", fresh)
        self.assertEqual(report.outcome, Outcome.BLOCKED)
        self.assertEqual((fresh / "page-0001.png").read_bytes(), b"new preview")
        self.assertTrue(is_fledge_output(fresh))

    def test_a_passing_check_replaces_the_earlier_preview_folder(self) -> None:
        report = self.check("paper")
        self.assertTrue(report.outcome.passed, report.outcome)
        self.assertEqual((self.previews / "page-0001.png").read_bytes(), b"new preview")
        self.assertTrue((self.previews / "report.json").is_file())
        self.assertEqual(leftovers(self.root), [])
