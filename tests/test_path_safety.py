"""Path safety: relative-path rules and their options, containment, and each caller's rules."""

from __future__ import annotations

import posixpath
import re
import tempfile
import unittest
from pathlib import Path

from latexprep.errors import PreparationError
from latexprep.formatting import FormattingOptions
from latexprep.latexmk_build import BuildRequest
from latexprep.main_selection import names_project_file
from latexprep.metadata_privacy import MetadataEdit
from latexprep.path_safety import (
    contained_relative_name,
    has_drive_prefix,
    is_contained,
    is_link_free_regular_file,
    is_relative_posix_path,
    lexically_escapes,
    relative_path_problem,
)
from latexprep.review_policy import ReviewRule
from latexprep.source_transform import SourceTransformOptions
from latexprep.submission_options import SubmissionOptions
from latexprep.workflow_options import DocumentOptions, PackageOptions


class RelativePathAlwaysRefusedTests(unittest.TestCase):
    """Rules no option relaxes: such a name can never be inside an imported project.

    Attributes:
        OPTION_SETS: The option sets under which every rule is checked.
    """

    OPTION_SETS = (
        {},
        {"normalized": False},
        {"normalized": False, "allow_current": True},
        {"normalized": False, "characters": "no_controls"},
        {"characters": "literal"},
    )

    def test_each_refusal_has_a_named_reason(self) -> None:
        reasons = {
            "": "empty",
            "/etc/passwd": "absolute",
            "//server/share": "absolute",
            "dir\\file.tex": "backslash",
            "\\\\server\\share": "backslash",
            "a\x00b.tex": "characters",
            "../secret.tex": "traversal",
            "dir/../../secret.tex": "traversal",
            "dir/..": "traversal",
            "C:main.tex": "drive",
            "c:/main.tex": "drive",
        }
        for value, reason in reasons.items():
            for options in self.OPTION_SETS:
                with self.subTest(value=value, options=options):
                    self.assertEqual(relative_path_problem(value, **options), reason)  # type: ignore[arg-type]
                    self.assertFalse(is_relative_posix_path(value, **options))  # type: ignore[arg-type]

    def test_ordinary_project_paths_are_accepted_everywhere(self) -> None:
        for value in (
            "main.tex",
            "paper/main.tex",
            "figures/plot 1.pdf",
            "figures/a:b.pdf",
            ".hidden",
        ):
            for options in self.OPTION_SETS[:-1]:
                with self.subTest(value=value, options=options):
                    self.assertIsNone(relative_path_problem(value, **options))  # type: ignore[arg-type]

    def test_a_drive_prefix_is_refused_only_at_the_start(self) -> None:
        self.assertTrue(has_drive_prefix("C:"))
        self.assertTrue(has_drive_prefix("z:\\x"))
        self.assertFalse(has_drive_prefix("figures/C:x"))
        self.assertFalse(has_drive_prefix("1:x"))
        self.assertTrue(is_relative_posix_path("figures/C:x.pdf"))


class NormalizedOptionTests(unittest.TestCase):
    """The ``normalized`` and ``allow_current`` options of the relative-path rules."""

    def test_normalized_spelling_is_the_default(self) -> None:
        for value in ("a//b", "./a", "a/./b", "a/", ".", "./", "a/."):
            with self.subTest(value=value):
                self.assertFalse(is_relative_posix_path(value))
        self.assertEqual(relative_path_problem("a//b"), "unnormalized")
        self.assertEqual(relative_path_problem("./"), "unnormalized")
        self.assertEqual(relative_path_problem("."), "current")

    def test_redundant_segments_are_tolerated_without_it(self) -> None:
        for value in ("a//b", "./a", "a/./b", "a/"):
            with self.subTest(value=value):
                self.assertTrue(is_relative_posix_path(value, normalized=False))

    def test_the_project_root_needs_allow_current(self) -> None:
        for value in (".", "./", "./."):
            with self.subTest(value=value):
                self.assertEqual(relative_path_problem(value, normalized=False), "current")
                self.assertIsNone(
                    relative_path_problem(value, normalized=False, allow_current=True)
                )
        self.assertIsNone(relative_path_problem(".", allow_current=True))
        self.assertEqual(relative_path_problem("./", allow_current=True), "unnormalized")


class CharacterLevelTests(unittest.TestCase):
    """The ``characters`` levels, each refusing more than the one before."""

    def accepted(self, value: str, characters: str) -> bool:
        """Return whether :func:`is_relative_posix_path` accepts ``value`` at a character level.

        Args:
            value: The path to check.
            characters: The character level, such as ``"literal"``.

        Returns:
            Whether the path is accepted.
        """
        return is_relative_posix_path(value, characters=characters)  # type: ignore[arg-type]

    def test_each_level_refuses_what_the_previous_levels_refuse_and_more(self) -> None:
        cases = {
            "tab\there.tex": (True, True, False, False),
            "line\nbreak.tex": (True, False, False, False),
            "carriage\rreturn.tex": (True, False, False, False),
            "bell\x07.tex": (True, True, False, False),
            "delete\x7f.tex": (True, True, True, False),
            "fig:1.pdf": (True, True, True, False),
            "zero\u200bwidth.tex": (True, True, True, False),
            "private\ue000use.tex": (True, True, True, False),
            "plain-name.tex": (True, True, True, True),
            "ünïcode/naïve.tex": (True, True, True, True),
        }
        levels = ("any", "no_line_breaks", "no_controls", "literal")
        for value, expected in cases.items():
            for level, accepted in zip(levels, expected, strict=True):
                with self.subTest(value=value, level=level):
                    self.assertEqual(self.accepted(value, level), accepted)

    def test_a_refused_character_is_reported_as_characters(self) -> None:
        self.assertEqual(relative_path_problem("a\nb", characters="no_line_breaks"), "characters")
        self.assertEqual(relative_path_problem("fig:1", characters="literal"), "characters")


class ComponentPatternTests(unittest.TestCase):
    """The ``component_pattern`` option: every segment must match it completely.

    Attributes:
        PATTERN: The pattern of a segment: ASCII letters, digits, ``_``, ``.`` and ``-``.
    """

    PATTERN = re.compile(r"[A-Za-z0-9_.-]+")

    def test_every_segment_must_match_completely(self) -> None:
        def problem(value: str) -> str | None:
            return relative_path_problem(value, normalized=False, component_pattern=self.PATTERN)

        self.assertIsNone(problem("paper/main.tex"))
        self.assertIsNone(problem("./paper//main.tex"))
        self.assertEqual(problem("my paper/main.tex"), "component")
        self.assertEqual(problem("paper/ma\u00efn.tex"), "component")
        self.assertEqual(problem("a:b/main.tex"), "drive")
        self.assertEqual(problem("figures/a:b.tex"), "component")
        self.assertEqual(problem("../main.tex"), "traversal")


class LexicalEscapeTests(unittest.TestCase):
    """:func:`lexically_escapes` for normalized paths that climb out of the project."""

    def test_a_normalized_path_that_climbs_out_escapes(self) -> None:
        self.assertTrue(lexically_escapes(".."))
        self.assertTrue(lexically_escapes("../x"))
        self.assertFalse(lexically_escapes("..x"))
        self.assertTrue(lexically_escapes(posixpath.normpath("a/../../b")))
        self.assertFalse(lexically_escapes(posixpath.normpath("a/../b")))
        self.assertFalse(lexically_escapes("."))
        self.assertFalse(lexically_escapes("a/b"))


class ContainmentTests(unittest.TestCase):
    """Containment on disk: links, paths that climb out, regular files and resolved names."""

    def setUp(self) -> None:
        """Create a project with ``main.tex`` and ``sub/part.tex``, and an outside folder.

        The outside folder holds ``secret.tex``.
        """
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name).resolve()
        self.root = base / "project"
        self.outside = base / "outside"
        (self.root / "sub").mkdir(parents=True)
        self.outside.mkdir()
        (self.root / "main.tex").write_text("x")
        (self.root / "sub" / "part.tex").write_text("x")
        (self.outside / "secret.tex").write_text("x")

    def test_a_regular_file_inside_is_contained(self) -> None:
        self.assertTrue(is_contained(self.root, self.root / "main.tex"))
        self.assertTrue(is_contained(self.root, self.root / "sub" / "part.tex", regular_file=True))

    def test_a_final_link_is_refused_even_when_it_stays_inside(self) -> None:
        (self.root / "alias.tex").symlink_to(self.root / "main.tex")
        (self.root / "escape.tex").symlink_to(self.outside / "secret.tex")
        for name in ("alias.tex", "escape.tex"):
            with self.subTest(name=name):
                self.assertFalse(is_contained(self.root, self.root / name))
                self.assertFalse(is_contained(self.root, self.root / name, regular_file=True))

    def test_a_linked_parent_is_followed_and_judged_by_where_it_leads(self) -> None:
        (self.root / "inside").symlink_to(self.root / "sub")
        (self.root / "outside").symlink_to(self.outside)
        self.assertTrue(is_contained(self.root, self.root / "inside" / "part.tex"))
        self.assertFalse(is_contained(self.root, self.root / "outside" / "secret.tex"))

    def test_a_path_that_climbs_out_is_refused(self) -> None:
        self.assertFalse(is_contained(self.root, self.root / ".." / "outside" / "secret.tex"))
        self.assertTrue(is_contained(self.root, self.root / "sub" / ".." / "main.tex"))

    def test_regular_file_excludes_directories_and_missing_paths(self) -> None:
        self.assertTrue(is_contained(self.root, self.root / "sub"))
        self.assertFalse(is_contained(self.root, self.root / "sub", regular_file=True))
        self.assertTrue(is_contained(self.root, self.root / "later.tex"))
        self.assertFalse(is_contained(self.root, self.root / "later.tex", regular_file=True))

    def test_the_root_may_itself_be_reached_through_a_link(self) -> None:
        shortcut = self.root.parent / "shortcut"
        shortcut.symlink_to(self.root)
        self.assertTrue(is_contained(shortcut, shortcut / "main.tex", regular_file=True))
        self.assertTrue(is_contained(self.root, shortcut / "main.tex", regular_file=True))

    def test_link_free_regular_file_accepts_only_unlinked_chains(self) -> None:
        self.assertTrue(is_link_free_regular_file(self.root, self.root / "sub" / "part.tex"))
        self.assertFalse(is_link_free_regular_file(self.root, self.root / "sub"))
        self.assertFalse(is_link_free_regular_file(self.root, self.root / "absent.tex"))
        self.assertFalse(is_link_free_regular_file(self.root, self.outside / "secret.tex"))

    def test_link_free_regular_file_refuses_a_parent_segment_that_leaves_the_root(self) -> None:
        escaping = self.root / ".." / self.outside.name / "secret.tex"
        self.assertTrue((self.root / ".." / "outside" / "secret.tex").is_file())
        self.assertFalse(is_link_free_regular_file(self.root, escaping))
        # Even a ".." that returns inside the root is a different spelling than the file's own.
        self.assertFalse(
            is_link_free_regular_file(self.root, self.root / "sub" / ".." / "main.tex")
        )

    def test_link_free_regular_file_refuses_a_link_that_resolves_inside(self) -> None:
        (self.root / "inside").symlink_to(self.root / "sub")
        (self.root / "alias.tex").symlink_to(self.root / "main.tex")
        # Resolution-based containment accepts a parent link that stays inside; the
        # strict variant refuses every link between the root and the file.
        self.assertTrue(
            is_contained(self.root, self.root / "inside" / "part.tex", regular_file=True)
        )
        self.assertFalse(is_link_free_regular_file(self.root, self.root / "inside" / "part.tex"))
        self.assertFalse(is_link_free_regular_file(self.root, self.root / "alias.tex"))

    def test_link_free_regular_file_refuses_a_linked_root(self) -> None:
        shortcut = self.root.parent / "shortcut"
        shortcut.symlink_to(self.root)
        self.assertFalse(is_link_free_regular_file(shortcut, shortcut / "main.tex"))

    def test_contained_relative_name_reports_the_resolved_place_under_the_root(self) -> None:
        (self.root / "inside").symlink_to(self.root / "sub")
        (self.root / "outside").symlink_to(self.outside)
        self.assertEqual(
            contained_relative_name(self.root, self.root / "sub" / "part.tex"), "sub/part.tex"
        )
        self.assertEqual(
            contained_relative_name(self.root, self.root / "inside" / "part.tex"), "sub/part.tex"
        )
        self.assertEqual(
            contained_relative_name(self.root, self.root / "sub" / ".." / "main.tex"), "main.tex"
        )
        self.assertIsNone(contained_relative_name(self.root, self.root / "outside" / "secret.tex"))
        self.assertIsNone(contained_relative_name(self.root, self.outside / "secret.tex"))


class CallerRuleTests(unittest.TestCase):
    """The option set each caller passes, seen through the caller's own public entry point."""

    def accepts(self, build, accepted: list[str], refused: list[str]) -> None:
        """Assert that ``build`` accepts every value of ``accepted`` and refuses ``refused``.

        Each value runs in its own subtest; a refusal must raise :class:`PreparationError`.

        Args:
            build: The caller's entry point, called with one value.
            accepted: The values it must accept.
            refused: The values it must refuse.
        """
        for value in accepted:
            with self.subTest(accepted=value):
                build(value)
        for value in refused:
            with self.subTest(refused=value), self.assertRaises(PreparationError):
                build(value)

    def test_document_main_and_source_directory(self) -> None:
        self.accepts(
            lambda value: DocumentOptions("paper", main=value),
            ["main.tex", "paper/main.tex", "./paper//main.tex"],
            ["", ".", "/abs/main.tex", "../main.tex", "a\\b.tex", "C:main.tex", "tab\tmain.tex"],
        )
        self.accepts(
            lambda value: DocumentOptions("paper", main="main.tex", source_directory=value),
            [".", "paper", "./paper"],
            ["", "/abs", "../up", "C:paper", "a\\b"],
        )

    def test_document_include_patterns_and_package_includes_refuse_the_root(self) -> None:
        self.accepts(
            lambda value: DocumentOptions("paper", main="main.tex", include=(value,)),
            ["figures/*.pdf", "**/*.tex"],
            ["", ".", "/abs/*.tex", "../*.tex"],
        )
        self.accepts(
            lambda value: PackageOptions(include=(value,)),
            ["notes/readme.txt"],
            ["", ".", "../x", "C:x", "bell\x07"],
        )

    def test_source_transform_paths_allow_tabs_but_not_line_breaks_or_the_root(self) -> None:
        self.accepts(
            lambda value: SourceTransformOptions(inline_bibliography=value),
            ["refs.bbl", "build/refs.bbl", "tab\t.bbl"],
            ["", "refs.tex", "../refs.bbl", "C:refs.bbl", "a\nb.bbl", "a\\b.bbl"],
        )
        self.accepts(
            lambda value: SourceTransformOptions(filename_overrides=((value, "short.tex"),)),
            ["chapters/one.tex", "./chapters/one.tex"],
            ["", ".", "/abs.tex", "../one.tex", "C:one.tex", "a\rb.tex"],
        )

    def test_formatting_exclusions_are_globs_without_line_breaks_or_escapes(self) -> None:
        self.accepts(
            lambda value: FormattingOptions(exclude=(value,)),
            ["drafts/*.tex", "**/old_[ab].tex", "tab\t*.tex"],
            ["", "/abs/*.tex", "../*.tex", "a\\*.tex", "a\n*.tex", "C:*.tex"],
        )

    def test_build_main_is_a_tex_file_with_whitelisted_segments(self) -> None:
        def build(value: str) -> BuildRequest:
            return BuildRequest(Path("source"), value, Path("work"), "pdflatex")

        self.accepts(
            build,
            ["main.tex", "paper/main-v2.tex", "./paper//main.tex"],
            ["", ".tex", "main.pdf", "/main.tex", "../main.tex", "my paper/main.tex", "a:b.tex"],
        )

    def test_submission_paths_are_normalized_project_files(self) -> None:
        self.accepts(
            lambda value: SubmissionOptions(required_deliverables=((value, "file"),)),
            ["main.pdf", "out/main.pdf", "tab\t.pdf"],
            ["", ".", "./main.pdf", "out//main.pdf", "out/", "/abs.pdf", "../x.pdf", "C:x.pdf"],
        )

    def test_literal_paths_exclude_colons_controls_and_unnormalized_spellings(self) -> None:
        self.accepts(
            lambda value: MetadataEdit(value, "title", "A", "B"),
            ["main.tex", "paper/main.tex"],
            ["", "./main.tex", "a//b.tex", "fig:1.tex", "tab\t.tex", "zero\u200b.tex"],
        )
        self.accepts(
            lambda value: ReviewRule("TEX001", "Reviewed", path=value),
            ["main.tex", "paper/main.tex"],
            ["", ".", "./main.tex", "a//b.tex", "fig:1.tex", "tab\t.tex", "../x"],
        )


class NamesProjectFileTests(unittest.TestCase):
    """:func:`names_project_file`: a typed path may be redundant but must name a project file."""

    def setUp(self) -> None:
        """Create a snapshot folder that holds ``paper/main.tex``."""
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.snapshot = Path(self.temporary.name)
        (self.snapshot / "paper").mkdir()
        (self.snapshot / "paper" / "main.tex").write_text("x")

    def test_a_typed_path_may_use_redundant_segments_but_not_leave_the_project(self) -> None:
        for value in ("paper/main.tex", "./paper/main.tex", "paper//main.tex"):
            with self.subTest(value=value):
                self.assertTrue(names_project_file(self.snapshot, value))
        for value in ("", ".", "paper", "missing.tex", "../paper/main.tex", "paper\\main.tex"):
            with self.subTest(value=value):
                self.assertFalse(names_project_file(self.snapshot, value))
        self.assertFalse(names_project_file(self.snapshot, str(self.snapshot / "paper/main.tex")))


if __name__ == "__main__":
    unittest.main()
