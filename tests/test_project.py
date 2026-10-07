from __future__ import annotations

import io
import os
import stat
import struct
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest.mock import patch

from latexprep.models import PreparationError
from latexprep.project import ImportLimits, create_archive, import_project, plan_cleanup


class ProjectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.source = self.base / "source"
        self.source.mkdir()

    def write_file(self, name: str, content: bytes = b"source") -> Path:
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def make_zip(
        self,
        entries: list[tuple[str | zipfile.ZipInfo, bytes]],
        *,
        compression: int = zipfile.ZIP_STORED,
    ) -> Path:
        path = self.base / "input.zip"
        with (
            warnings.catch_warnings(),
            zipfile.ZipFile(path, "w", compression=compression) as archive,
        ):
            warnings.simplefilter("ignore", UserWarning)
            for name, data in entries:
                archive.writestr(name, data)
        return path

    def tree(self, root: Path) -> dict[str, bytes | None]:
        return {
            path.relative_to(root).as_posix(): None if path.is_dir() else path.read_bytes()
            for path in root.rglob("*")
        }

    def test_folder_and_wrapped_zip_have_equivalent_logical_roots(self) -> None:
        self.write_file("main.tex", b"manuscript")
        self.write_file("figures/result.pdf", b"figure")
        original = self.tree(self.source)
        zipped = self.make_zip(
            [("paper/main.tex", b"manuscript"), ("paper/figures/result.pdf", b"figure")]
        )
        zip_bytes = zipped.read_bytes()
        folder_import = import_project(self.source, self.base / "from-folder")
        zip_import = import_project(zipped, self.base / "from-zip")
        self.assertEqual(folder_import.files, zip_import.files)
        self.assertEqual(self.tree(folder_import.root), self.tree(zip_import.root))
        self.assertEqual(self.tree(self.source), original)
        self.assertEqual(zipped.read_bytes(), zip_bytes)
        self.assertEqual(zip_import.changes[0].kind, "unwrap")

    def test_nested_single_directory_is_preserved_for_exact_extraction(self) -> None:
        self.write_file("chapters/chapter.tex")
        (self.source / "empty").mkdir()
        self.write_file(".DS_Store", b"metadata")
        archive = self.base / "submission.zip"
        create_archive(self.source, archive)
        result = import_project(archive, self.base / "fresh", unwrap=False, exclude_metadata=False)
        self.assertEqual(self.tree(result.root), self.tree(self.source))
        self.assertEqual(result.changes, [])

    def test_exact_extraction_does_not_strip_genuine_single_directory(self) -> None:
        self.write_file("chapters/chapter.tex")
        archive = self.base / "submission.zip"
        create_archive(self.source, archive)
        result = import_project(archive, self.base / "fresh", unwrap=False, exclude_metadata=False)
        self.assertEqual(self.tree(result.root), self.tree(self.source))

    def test_metadata_exclusions_are_reported_and_originals_remain(self) -> None:
        self.write_file("main.tex")
        self.write_file(".git/config", b"private")
        self.write_file(".hg/store/data", b"private")
        self.write_file(".DS_Store", b"private")
        self.write_file("data/unknown.bin", b"keep")
        result = import_project(self.source, self.base / "import")
        self.assertEqual(result.files, ["data/unknown.bin", "main.tex"])
        self.assertEqual({change.path for change in result.changes}, {".git", ".hg", ".DS_Store"})
        self.assertTrue((self.source / ".git/config").exists())

    def test_wrapped_zip_ignores_outer_metadata_when_selecting_wrapper(self) -> None:
        archive = self.make_zip(
            [("paper/main.tex", b"source"), ("__MACOSX/paper/._main.tex", b"metadata")]
        )
        result = import_project(archive, self.base / "import")
        self.assertEqual(result.files, ["main.tex"])
        self.assertEqual({change.kind for change in result.changes}, {"exclude", "unwrap"})

    def test_file_named_like_metadata_directory_is_preserved(self) -> None:
        self.write_file(".git", b"gitdir: elsewhere")
        result = import_project(self.source, self.base / "import")
        self.assertEqual(result.files, [".git"])

    def test_unsafe_zip_names_are_rejected(self) -> None:
        names = [
            "../escaped.tex",
            "a/../../escaped.tex",
            "/absolute.tex",
            "C:/drive.tex",
            "C:relative.tex",
            "a\\..\\escaped.tex",
            "\\server\\share\\file.tex",
            "file.tex:stream",
            "name. ",
            "NUL.tex",
            "con/file.tex",
        ]
        for name in names:
            with self.subTest(name=name):
                archive = self.make_zip([(name, b"bad")])
                with self.assertRaises(PreparationError):
                    import_project(archive, self.base / "import")
                self.assertFalse((self.base / "import").exists())
        self.assertFalse((self.base / "escaped.tex").exists())

    def test_normalized_case_and_unicode_collisions_are_rejected(self) -> None:
        pairs = [
            ("same.tex", "same.tex"),
            ("a//same.tex", "a/same.tex"),
            ("./same.tex", "same.tex"),
            ("same.tex", "SAME.tex"),
            ("café.tex", "cafe\u0301.tex"),
            ("A/one.tex", "a/two.tex"),
            ("café/one.tex", "cafe\u0301/two.tex"),
        ]
        for first, second in pairs:
            with self.subTest(first=first, second=second):
                archive = self.make_zip([(first, b"one"), (second, b"two")])
                with self.assertRaises(PreparationError):
                    import_project(archive, self.base / "import")
                self.assertFalse((self.base / "import").exists())

    def test_file_directory_conflicts_are_rejected_in_either_order(self) -> None:
        cases = [
            [("a", b"file"), ("a/child.tex", b"child")],
            [("a/child.tex", b"child"), ("a", b"file")],
            [("a/", b""), ("a", b"file")],
            [("a", b"file"), ("a/", b"")],
            [("a/", b""), ("a/", b"")],
        ]
        for entries in cases:
            with self.subTest(entries=entries):
                archive = self.make_zip(entries)
                with self.assertRaises(PreparationError):
                    import_project(archive, self.base / "import")

    def test_explicit_directory_after_child_is_not_a_duplicate(self) -> None:
        for order in (False, True):
            with self.subTest(parent_first=order):
                entries = [("a/child.tex", b"child"), ("a/", b"")]
                if order:
                    entries.reverse()
                archive = self.make_zip(entries)
                result = import_project(archive, self.base / f"import-{order}", unwrap=False)
                self.assertEqual(result.files, ["a/child.tex"])

    def test_zip_symlinks_and_special_files_are_rejected(self) -> None:
        for mode in (stat.S_IFLNK, stat.S_IFIFO, stat.S_IFSOCK, stat.S_IFCHR, stat.S_IFBLK):
            with self.subTest(mode=mode):
                info = zipfile.ZipInfo("unsafe")
                info.create_system = 3
                info.external_attr = (mode | 0o600) << 16
                archive = self.make_zip([(info, b"target")])
                with self.assertRaisesRegex(PreparationError, "special files"):
                    import_project(archive, self.base / "import")

    def test_folder_file_and_directory_symlinks_are_rejected(self) -> None:
        external = self.base / "external"
        external.mkdir()
        (external / "private").write_text("private")
        for target in (external, external / "private"):
            with self.subTest(target=target):
                link = self.source / "link"
                link.symlink_to(target)
                with self.assertRaisesRegex(PreparationError, "Links and special"):
                    import_project(self.source, self.base / "import")
                link.unlink()

    def test_source_root_symlink_is_rejected(self) -> None:
        link = self.base / "linked-source"
        link.symlink_to(self.source)
        with self.assertRaises(PreparationError):
            import_project(link, self.base / "import")

    def test_fifo_is_rejected_without_opening_it(self) -> None:
        os.mkfifo(self.source / "pipe")
        with self.assertRaisesRegex(PreparationError, "special files"):
            import_project(self.source, self.base / "import")

    def test_encrypted_archive_is_rejected(self) -> None:
        archive = self.make_zip([("main.tex", b"source")])
        data = bytearray(archive.read_bytes())
        for signature, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
            position = data.index(signature) + offset
            struct.pack_into("<H", data, position, struct.unpack_from("<H", data, position)[0] | 1)
        archive.write_bytes(data)
        with self.assertRaisesRegex(PreparationError, "Encrypted"):
            import_project(archive, self.base / "import")

    def test_unsupported_compression_is_rejected(self) -> None:
        archive = self.make_zip([("main.tex", b"source")], compression=zipfile.ZIP_BZIP2)
        with self.assertRaisesRegex(PreparationError, "Unsupported ZIP compression"):
            import_project(archive, self.base / "import")

    def test_invalid_zip_is_actionable(self) -> None:
        archive = self.base / "bad.zip"
        archive.write_bytes(b"not a zip")
        with self.assertRaises(PreparationError):
            import_project(archive, self.base / "import")

    def test_actual_directory_count_is_checked_before_zipfile_allocates_entries(self) -> None:
        archive = self.make_zip([("one", b"1"), ("two", b"2"), ("three", b"3")])
        data = bytearray(archive.read_bytes())
        footer = data.rfind(b"PK\x05\x06")
        struct.pack_into("<2H", data, footer + 8, 0, 0)
        archive.write_bytes(data)
        with patch("latexprep.project.zipfile.ZipFile", side_effect=AssertionError("too late")):
            with self.assertRaisesRegex(PreparationError, "entry limit"):
                import_project(archive, self.base / "import", ImportLimits(max_files=2))

    def test_metadata_size_is_checked_before_loading_the_zip(self) -> None:
        archive = self.make_zip([("main.tex", b"source")])
        data = bytearray(archive.read_bytes())
        footer = data.rfind(b"PK\x05\x06")
        struct.pack_into("<I", data, footer + 12, 17 * 1024 * 1024)
        archive.write_bytes(data)
        with self.assertRaisesRegex(PreparationError, "metadata exceeds"):
            import_project(archive, self.base / "import")

    def test_small_zip64_archive_is_supported(self) -> None:
        archive = self.make_zip([("main.tex", b"source")])
        data = archive.read_bytes()
        footer = data.rfind(b"PK\x05\x06")
        _, _, _, count, _, size, offset, _ = struct.unpack_from("<4s4H2IH", data, footer)
        record = struct.pack(
            "<4sQ2H2I4Q", b"PK\x06\x06", 44, 45, 45, 0, 0, count, count, size, offset
        )
        locator = struct.pack("<4sIQI", b"PK\x06\x07", 0, footer, 1)
        archive.write_bytes(data[:footer] + record + locator + data[footer:])
        result = import_project(archive, self.base / "import")
        self.assertEqual(result.files, ["main.tex"])
        self.assertEqual((result.root / "main.tex").read_bytes(), b"source")

    def test_embedded_nul_filename_is_rejected(self) -> None:
        archive = self.make_zip([("main.tex", b"source")])
        archive.write_bytes(archive.read_bytes().replace(b"main.tex", b"ma\x00n.tex"))
        with self.assertRaisesRegex(PreparationError, "Unsafe project path"):
            import_project(archive, self.base / "import")

    def test_corrupt_entry_removes_partial_output(self) -> None:
        archive = self.make_zip([("main.tex", b"source")])
        archive.write_bytes(archive.read_bytes().replace(b"source", b"broken"))
        with self.assertRaises(PreparationError):
            import_project(archive, self.base / "import")
        self.assertFalse((self.base / "import").exists())

    def test_entry_limits_cover_folder_zip_and_empty_directories(self) -> None:
        self.write_file("first.tex")
        self.write_file("second.tex")
        archive = self.make_zip([("one/", b""), ("two/", b"")])
        for source in (self.source, archive):
            with self.subTest(source=source):
                with self.assertRaisesRegex(PreparationError, "entry limit"):
                    import_project(source, self.base / "import", ImportLimits(max_files=1))

    def test_actual_folder_bytes_are_bounded_and_partial_copy_removed(self) -> None:
        self.write_file("large.tex", b"x" * 1025)
        with self.assertRaisesRegex(PreparationError, "size limit"):
            import_project(self.source, self.base / "import", ImportLimits(max_bytes=1024))
        self.assertFalse((self.base / "import").exists())
        self.assertEqual((self.source / "large.tex").stat().st_size, 1025)

    def test_zip_actual_stream_bytes_are_bounded_independently_of_metadata(self) -> None:
        archive = self.make_zip([("main.tex", b"x")])
        with patch.object(zipfile.ZipFile, "open", return_value=io.BytesIO(b"x" * 5)):
            with self.assertRaisesRegex(PreparationError, "size limit"):
                import_project(archive, self.base / "import", ImportLimits(max_bytes=4))
        self.assertFalse((self.base / "import").exists())

    def test_depth_limits_cover_folder_and_archive(self) -> None:
        self.write_file("a/b/main.tex")
        archive = self.make_zip([("a/b/main.tex", b"source")])
        for source in (self.source, archive):
            with self.subTest(source=source):
                with self.assertRaisesRegex(PreparationError, "nesting limit"):
                    import_project(source, self.base / "import", ImportLimits(max_depth=2))

    def test_compression_ratio_limit(self) -> None:
        archive = self.make_zip([("main.tex", b"x" * 100_000)], compression=zipfile.ZIP_DEFLATED)
        with self.assertRaisesRegex(PreparationError, "compression ratio"):
            import_project(archive, self.base / "import")

    def test_existing_or_symlink_destinations_are_preserved(self) -> None:
        destination = self.base / "existing"
        destination.mkdir()
        (destination / "keep").write_bytes(b"unchanged")
        link = self.base / "linked-destination"
        link.symlink_to(destination)
        for output in (destination, link):
            with self.subTest(output=output), self.assertRaisesRegex(PreparationError, "exists"):
                import_project(self.source, output)
        self.assertEqual((destination / "keep").read_bytes(), b"unchanged")
        self.assertTrue(link.is_symlink())

    def test_output_inside_input_is_rejected_including_parent_alias(self) -> None:
        alias = self.base / "alias"
        alias.symlink_to(self.source)
        for destination in (self.source / "copy", alias / "copy"):
            with self.subTest(destination=destination):
                with self.assertRaisesRegex(PreparationError, "outside"):
                    import_project(self.source, destination)
        self.assertEqual(self.tree(self.source), {})

    def test_archive_bytes_ignore_input_modes_timestamps_and_creation_order(self) -> None:
        first = self.write_file("zeta.tex", b"zeta")
        self.write_file("figures/plot.pdf", b"plot")
        (self.source / "empty").mkdir()
        original = self.tree(self.source)
        archive_one = self.base / "first.zip"
        archive_two = self.base / "second.zip"
        create_archive(self.source, archive_one)
        os.utime(first, (1000, 2000))
        first.chmod(0o700)
        create_archive(self.source, archive_two)
        self.assertEqual(archive_one.read_bytes(), archive_two.read_bytes())
        self.assertEqual(self.tree(self.source), original)
        with zipfile.ZipFile(archive_one) as archive:
            self.assertEqual(
                archive.namelist(), ["empty/", "figures/", "figures/plot.pdf", "zeta.tex"]
            )
            for info in archive.infolist():
                self.assertEqual(info.date_time, (1980, 1, 1, 0, 0, 0))
                self.assertEqual(
                    stat.S_IMODE(info.external_attr >> 16), 0o755 if info.is_dir() else 0o644
                )
                self.assertEqual(info.compress_type, zipfile.ZIP_DEFLATED)

    def test_archive_refuses_overwrite_containment_and_links(self) -> None:
        archive = self.base / "existing.zip"
        archive.write_bytes(b"preserve")
        with self.assertRaises(PreparationError):
            create_archive(self.source, archive)
        self.assertEqual(archive.read_bytes(), b"preserve")
        with self.assertRaises(PreparationError):
            create_archive(self.source, self.source / "self.zip")
        (self.source / "link").symlink_to(archive)
        with self.assertRaises(PreparationError):
            create_archive(self.source, self.base / "new.zip")
        self.assertFalse((self.base / "new.zip").exists())

    def test_archive_entry_order_includes_directory_slashes(self) -> None:
        self.write_file("figures/plot.pdf")
        self.write_file("figures.pdf")
        archive = self.base / "sorted.zip"
        create_archive(self.source, archive)
        with zipfile.ZipFile(archive) as zipped:
            self.assertEqual(zipped.namelist(), sorted(zipped.namelist()))

    def test_cleanup_retains_dependencies_bibliography_styles_and_unknown_files(self) -> None:
        names = [
            "main.tex",
            "main.aux",
            "main.log",
            "main.fdb_latexmk",
            "main.synctex.gz",
            "main.bbl",
            "refs.bib",
            "style.bst",
            "package.sty",
            "main.tex~",
            ".main.tex.swp",
            "data.bin",
            "results.out",
            "experiment.bak",
            "data/required.aux",
            ".DS_Store",
            ".git/config",
            "unknown/file.custom",
        ]
        for name in names:
            self.write_file(name)
        before = self.tree(self.source)
        changes = plan_cleanup(self.source, {"data/required.aux", str(self.source / "main.log")})
        self.assertEqual(
            {change.path for change in changes},
            {
                "main.aux",
                "main.fdb_latexmk",
                "main.synctex.gz",
                "main.tex~",
                ".main.tex.swp",
                ".DS_Store",
                ".git/config",
            },
        )
        self.assertTrue(all(change.kind == "exclude" and change.reason for change in changes))
        self.assertEqual(self.tree(self.source), before)

    def test_observed_dependency_comparison_is_conservative_about_case(self) -> None:
        self.write_file("main.aux")
        self.assertEqual(plan_cleanup(self.source, {"MAIN.AUX"}), [])
        self.assertEqual(plan_cleanup(self.source, {"sections/../main.aux"}), [])

    def test_limits_reject_invalid_values(self) -> None:
        for value in (0, -1, True, 1.5):
            with self.subTest(value=value), self.assertRaises(PreparationError):
                ImportLimits(max_files=value)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
