r"""Tests of the build workspace: the bounded copy, the ``\include`` folders and seeded files.

TeX writes the ``.aux`` file of ``\include{chapters/one}`` into ``chapters/`` inside the
output folder, which latexmk 4.83 does not create when ``-file-line-error`` is on, so the
workspace creates those folders. A generated file supplied beside the main file is
copied into the output folder, where TeX looks first, so that latexmk attributes it to
its aux folder on every release.
"""

from __future__ import annotations

import unittest

from latexprep.build_workspace import (
    MAX_INCLUDE_FOLDERS,
    include_folder,
    include_folders,
    prepare_layout,
)
from latexprep.latexmk_command import BiberPlan, controlled_rules
from latexprep.limits import MAX_WALKED_FOLDER_LEVELS
from tests.support import ProjectTestCase

_LIMIT = 10_000_000


class IncludeFolderTests(ProjectTestCase):
    r"""The folders of literal ``\include`` targets are created in the output folder."""

    def test_the_folder_of_each_literal_include_target_exists_before_the_build(self) -> None:
        project = self.write_project(
            {
                "paper/main.tex": (
                    "\\documentclass{article}\n\\begin{document}\n"
                    "\\include{chapters/one}\n\\include{parts/a/two}\n\\include{flat}\n"
                    "% \\include{commented/out}\n\\end{document}\n"
                ),
                # \include names its target relative to the main file's folder, whichever
                # file holds the command.
                "paper/chapters/one.tex": "\\include{appendix/three}\n",
                "paper/parts/a/two.tex": "Two.\n",
                "paper/flat.tex": "Flat.\n",
                "paper/appendix/three.tex": "Three.\n",
            }
        )
        layout = prepare_layout(project, "paper/main.tex", self.root / "work", _LIMIT)
        self.assertEqual(layout.cwd, layout.project / "paper")
        self.assertTrue((layout.output / "chapters").is_dir())
        self.assertTrue((layout.output / "parts/a").is_dir())
        self.assertTrue((layout.output / "appendix").is_dir())
        self.assertFalse((layout.output / "commented").exists())
        self.assertEqual(
            sorted(path.name for path in layout.output.iterdir()),
            ["appendix", "chapters", "parts"],
        )

    def test_targets_that_are_not_plain_relative_paths_create_nothing(self) -> None:
        for target in ("one", "../outside/one", "/abs/one", "\\folder/one", "#1/one", "a:b/c", ""):
            with self.subTest(target=target):
                self.assertIsNone(include_folder(target))
        self.assertEqual(include_folder(" chapters/one "), "chapters")
        self.assertEqual(include_folder("./chapters//deep/one"), "chapters/deep")

    def test_the_number_of_created_folders_is_bounded(self) -> None:
        body = "".join(f"\\include{{folder{number}/x}}\n" for number in range(300))
        self.write("main.tex", body)
        self.assertEqual(len(include_folders(self.root)), MAX_INCLUDE_FOLDERS)

    def test_parent_folders_count_against_the_bound(self) -> None:
        # 256 targets 100 levels deep created 25,856 folders, beyond the workspace's limits.
        body = "".join(
            f"\\include{{top{number}/{'level/' * 99}x}}\n" for number in range(MAX_INCLUDE_FOLDERS)
        )
        project = self.write_project({"main.tex": body})
        layout = prepare_layout(project, "main.tex", self.root / "work", _LIMIT)
        created = [path for path in layout.output.rglob("*") if path.is_dir()]
        self.assertLessEqual(len(created), MAX_INCLUDE_FOLDERS)
        self.assertTrue((layout.output / "top0" / ("level/" * 99)).is_dir())

    def test_folders_too_deep_or_too_long_to_create_are_skipped(self) -> None:
        deepest = "a/" * MAX_WALKED_FOLDER_LEVELS
        project = self.write_project(
            {
                "main.tex": (
                    # Deeper than a build copy can be: a 600-level target raised ENAMETOOLONG.
                    f"\\include{{{'b/' * 600}x}}\n\\include{{{deepest}b/x}}\n"
                    # A name longer than a file system allows.
                    f"\\include{{{'n' * 300}/x}}\n\\include{{{deepest}x}}\n"
                    "\\include{chapters/one}\n"
                ),
            }
        )
        layout = prepare_layout(project, "main.tex", self.root / "work", _LIMIT)
        self.assertEqual(sorted(path.name for path in layout.output.iterdir()), ["a", "chapters"])
        self.assertTrue((layout.output / deepest).is_dir())
        self.assertFalse((layout.output / deepest / "b").exists())


class SeededGeneratedFileTests(ProjectTestCase):
    """Supplied generated files beside the main file are copied into the output folder."""

    def test_supplied_generated_files_beside_the_main_file_are_seeded(self) -> None:
        project = self.write_project(
            {
                "paper/main.tex": "\\documentclass{article}\n",
                "paper/main.bbl": "\\begin{thebibliography}{1}\\end{thebibliography}\n",
                "paper/main.ind": "\\begin{theindex}\\end{theindex}\n",
                "paper/authors.ind": "\\begin{theindex}\\end{theindex}\n",
                "paper/main.aux": "\\relax\n",
                "paper/sub/other.bbl": "Elsewhere.\n",
            }
        )
        layout = prepare_layout(project, "paper/main.tex", self.root / "work", _LIMIT)
        output = layout.output
        self.assertEqual(
            dict(layout.seeded),
            {
                output / "authors.ind": "paper/authors.ind",
                output / "main.bbl": "paper/main.bbl",
                output / "main.ind": "paper/main.ind",
            },
        )
        self.assertEqual(
            (output / "main.bbl").read_bytes(), (project / "paper/main.bbl").read_bytes()
        )
        # Auxiliary files and files outside the main file's folder are not seeded.
        self.assertFalse((output / "main.aux").exists())
        self.assertFalse((output / "other.bbl").exists())

    def test_a_project_without_generated_files_seeds_nothing(self) -> None:
        project = self.write_project({"main.tex": "\\documentclass{article}\n"})
        layout = prepare_layout(project, "main.tex", self.root / "work", _LIMIT)
        self.assertEqual(dict(layout.seeded), {})
        self.assertEqual(list(layout.output.iterdir()), [])


class KeptGeneratedFileRuleTests(unittest.TestCase):
    """The latexmk configuration of a build that must read shipped generated files as they are."""

    def test_kept_files_switch_off_the_tools_that_would_rewrite_them(self) -> None:
        plan = BiberPlan(deferred=False, note=None)
        default = controlled_rules("auto", plan, "biber")
        self.assertIn("$bibtex_use=1;", default)
        self.assertNotIn("fledge_generated_file_kept", default)
        bibliography = controlled_rules("auto", plan, "biber", kept_generated=frozenset({".bbl"}))
        self.assertIn("$bibtex_use=0;", bibliography)
        self.assertNotIn("fledge_generated_file_kept", bibliography)
        index = controlled_rules("auto", plan, "biber", kept_generated=frozenset({".ind"}))
        self.assertIn("$bibtex_use=1;", index)
        self.assertIn("$makeindex='internal fledge_generated_file_kept';", index)


if __name__ == "__main__":
    unittest.main()
