"""Tests of the submission dependencies read from the TeX recorder and the latexmk database.

``collect_submission_dependencies`` ships the original project inputs a build read and
nothing generated or from the toolchain; missing, linked, malformed, inconsistent or
oversized evidence fails closed.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from latexprep.build_dependencies import (
    DependencyRequest,
    collect_submission_dependencies,
    read_recorder_trace,
)
from latexprep.build_workspace import BuildLayout
from latexprep.errors import PreparationError
from tests.support import ProjectTestCase


def rule(
    name: str,
    source: str,
    destination: str,
    inputs: list[tuple[str, str]],
    *,
    generated: tuple[str, ...] = (),
    rewritten: tuple[str, ...] = (),
) -> str:
    """Return one rule of a latexmk database (``.fdb_latexmk``).

    Args:
        name: The name of the rule, such as ``pdflatex``.
        source: The source file of the rule.
        destination: The destination file of the rule, listed first among its generated files.
        inputs: The path and producing rule of each input; an empty producer for none.
        generated: The files the rule generates besides ``destination``.
        rewritten: The files the rule rewrites before reading them.

    Returns:
        The text of the rule: its header line, its inputs, and its generated and rewritten
        files.
    """
    return (
        f'["{name}"] 1 "{source}" "{destination}" "main" 1 0\n'
        + "".join(f'  "{path}" 1 10 {"0" * 32} "{producer}"\n' for path, producer in inputs)
        + "  (generated)\n"
        + "".join(f'  "{path}"\n' for path in (destination, *generated))
        + "  (rewritten before read)\n"
        + "".join(f'  "{path}"\n' for path in rewritten)
    )


class SubmissionDependenciesTests(ProjectTestCase):
    """Tests of ``collect_submission_dependencies`` on written recorder and database files."""

    def setUp(self) -> None:
        """Create the source, build copy, output and toolchain directories and build evidence.

        The evidence records a pdflatex build of ``article/main.tex`` into ``output/main.pdf``.
        """
        super().setUp()
        self.source = self.root / "source"
        self.project = self.root / "work/project"
        self.cwd = self.project / "article"
        self.output = self.root / "work/output"
        self.toolchain = self.root / "toolchain"
        for directory in (self.source, self.cwd, self.output, self.toolchain):
            directory.mkdir(parents=True, exist_ok=True)
        self.original("article/main.tex")
        self.pdf = "../../output/main.pdf"
        self.fls = self.output / "main.fls"
        self.fdb = self.output / "main.fdb_latexmk"
        self.evidence()

    def original(self, name: str) -> None:
        """Write ``name`` with the same content into the source and into the build copy."""
        for root in (self.source, self.project):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("source content")

    def evidence(
        self,
        inputs: tuple[str, ...] = (),
        *,
        generated: tuple[str, ...] = (),
        rewritten: tuple[str, ...] = (),
        extra_rules: str = "",
        producers: dict[str, str] | None = None,
    ) -> None:
        """Write the recorder trace and the latexmk database of a build of ``main.tex``.

        Args:
            inputs: The files the build read besides ``main.tex``, relative to its working
                directory.
            generated: The files the build wrote besides the PDF.
            rewritten: The files the pdflatex rule rewrote before reading them.
            extra_rules: Database rules written before the pdflatex rule.
            producers: The rule that produced each input, by input path.
        """
        names = ("main.tex", *inputs)
        self.fls.write_text(
            f"PWD {self.cwd}\n"
            + "".join(f"INPUT {name}\n" for name in names)
            + "".join(f"OUTPUT {name}\n" for name in (self.pdf, *generated))
        )
        self.fdb.write_text(
            "# Fdb version 4\n"
            + extra_rules
            + rule(
                "pdflatex",
                "main.tex",
                self.pdf,
                [(name, (producers or {}).get(name, "")) for name in names],
                generated=generated,
                rewritten=rewritten,
            )
        )

    def collect(
        self,
        *,
        complete: bool = True,
        max_bytes: int = 2_097_152,
        tool_state: Path | None = None,
    ):
        """Collect the submission dependencies of the build from its evidence.

        Args:
            complete: Whether the recorder is reported complete.
            max_bytes: The byte limit of reading the recorder trace and of the collection.
            tool_state: The tool state directory of the build, if any.

        Returns:
            The collected dependencies.
        """
        layout = BuildLayout(self.source, self.project.parent, self.project, self.output, self.cwd)
        return collect_submission_dependencies(
            DependencyRequest(
                layout=layout,
                main="article/main.tex",
                engine="pdflatex",
                recorder=read_recorder_trace(self.fls, self.output, self.cwd, max_bytes),
                recorder_complete=complete,
                resource_roots=(self.toolchain,),
                max_bytes=max_bytes,
                tool_state=tool_state,
            )
        )

    def assert_incomplete(self, fragment: str = "") -> None:
        """Assert that collecting yields no inputs and is reported incomplete.

        Args:
            fragment: Text that the reported uncertainty must contain.
        """
        result = self.collect()
        self.assertIsNone(result.inputs, result)
        self.assertFalse(result.to_details()["complete"])
        self.assertIn(fragment, str(result.to_details()["uncertainty"]))

    def test_unions_tex_and_backend_sources_without_shipping_generated_or_system_files(self):
        for name in (
            "style/macros.tex",
            "data/measurements.csv",
            "refs.bib",
            "local.bst",
            "article/keep.bbl",
            "article/rebuilt.dat",
        ):
            self.original(name)
        system = self.toolchain / "article.cls"
        system.write_text("system class")
        bibliography = rule(
            "bibtex ../../output/main",
            "../../output/main.aux",
            "../../output/main.bbl",
            [("../../output/main.aux", "pdflatex"), ("../refs.bib", ""), ("../local.bst", "")],
        )
        self.evidence(
            (
                "../style/macros.tex",
                "../data/measurements.csv",
                "keep.bbl",
                "rebuilt.dat",
                "../../output/main.aux",
                "../../output/main.bbl",
                str(system),
            ),
            generated=("../../output/main.aux", "rebuilt.dat"),
            extra_rules=bibliography,
            producers={"../../output/main.bbl": "bibtex ../../output/main"},
        )
        result = self.collect()
        self.assertEqual(
            result.inputs,
            {
                "article/main.tex",
                "style/macros.tex",
                "data/measurements.csv",
                "article/keep.bbl",
                "refs.bib",
                "local.bst",
            },
            result.to_details(),
        )
        self.assertEqual(result.toolchain_inputs, 1)
        self.assertEqual(result.generated_inputs, 3)
        # Generated files the build read, relative to the directory that holds them.
        self.assertEqual(result.generated_reads, {"main.aux", "main.bbl", "article/rebuilt.dat"})

    def test_tool_state_the_build_wrote_and_read_back_is_a_toolchain_input(self):
        """Count a font cache the build wrote and read back as toolchain evidence.

        luaotfload writes its font cache under the build's tool state (``TEXMFVAR``) and
        reads it back, and the TeX recorder lists that read. The cache is toolchain evidence,
        not an input outside the declared paths, so a LuaLaTeX build's evidence is complete.
        """
        cache = (
            "../../.tool-state/invocation-x/texmf-var/luatex-cache/generic/fonts/otl/"
            "lmroman10-regular.luc"
        )
        self.evidence((cache,), generated=(cache,))
        self.assert_incomplete("outside declared")
        tool_state = self.project.parent / ".tool-state"
        result = self.collect(tool_state=tool_state)
        self.assertEqual(result.inputs, {"article/main.tex"}, result.to_details())
        self.assertEqual((result.toolchain_inputs, result.generated_inputs), (1, 0))
        # A sibling of the tool state is still an input outside the declared paths.
        self.evidence(("../../.tool-state-x/names.luc",))
        result = self.collect(tool_state=tool_state)
        self.assertIsNone(result.inputs, result.to_details())
        self.assertIn("outside declared", str(result.to_details()["uncertainty"]))

    def test_new_project_outputs_and_rewritten_inputs_are_not_original_inputs(self):
        (self.cwd / "generated.tex").write_text("new generated input")
        self.original("article/rewritten.aux")
        self.evidence(
            ("generated.tex", "rewritten.aux"),
            generated=("generated.tex",),
            rewritten=("rewritten.aux",),
        )
        self.assertEqual(self.collect().inputs, {"article/main.tex"})

    def test_rule_producer_marks_rebuilt_input_even_without_matching_output_record(self):
        self.original("article/generated.dat")
        self.evidence(("generated.dat",), producers={"generated.dat": "pdflatex"})
        self.assertEqual(self.collect().inputs, {"article/main.tex"})

    def test_missing_original_requires_generated_evidence(self):
        (self.cwd / "unrecorded.dat").write_text("created during build")
        self.evidence(("unrecorded.dat",))
        self.assert_incomplete("generated without evidence")

    def test_unknown_external_backend_input_is_rejected(self):
        external = self.root / "outside.bib"
        external.write_text("bibliography")
        self.evidence(
            extra_rules=rule(
                "bibtex main",
                "../../output/main.aux",
                "../../output/main.bbl",
                [(str(external), ""), ("../../output/main.aux", "pdflatex")],
            )
        )
        self.assert_incomplete("outside declared")

    def test_source_and_build_copy_must_remain_regular_files(self):
        self.original("article/data.dat")
        self.evidence(("data.dat",))
        for root in (self.source, self.project):
            with self.subTest(root=root):
                target = root / "article/data.dat"
                target.unlink()
                target.symlink_to(root / "article/main.tex")
                self.assert_incomplete("unsafe")
                target.unlink()
                target.write_text("restored")

    def test_missing_and_symlinked_database_fail_closed(self):
        content = self.fdb.read_bytes()
        self.fdb.unlink()
        self.assert_incomplete("missing")
        elsewhere = self.root / "database"
        elsewhere.write_bytes(content)
        self.fdb.symlink_to(elsewhere)
        self.assert_incomplete("owned regular")

    def test_database_versions_malformed_rows_and_truncations_fail_closed(self):
        valid = self.fdb.read_text()
        mutations = {
            "unsupported version": valid.replace("version 4", "version 5"),
            "no version": valid.split("\n", 1)[1],
            "missing terminal section": valid.rsplit("  (rewritten before read)", 1)[0],
            "partial row": valid + '  "unfinished',
            "unexpected section": valid.replace("(generated)", "(source)"),
            "malformed input": valid.replace('  "main.tex" 1 10', '  "main.tex" bad 10'),
            "failed rule": valid.replace('"main" 1 0', '"main" 1 1'),
            "never-run rule": valid.replace('["pdflatex"] 1', '["pdflatex"] 0'),
            "missing producer": valid.replace(f'{"0" * 32} ""', f'{"0" * 32} "unknown"'),
            "missing generated destination": valid.replace(f'  "{self.pdf}"\n', ""),
        }
        for name, content in mutations.items():
            with self.subTest(name=name):
                self.fdb.write_text(content)
                self.assert_incomplete()

    def test_selected_main_and_primary_rule_must_agree(self):
        valid = self.fdb.read_text()
        for old, new in (
            ('["pdflatex"]', '["xelatex"]'),
            ('1 "main.tex"', '1 "other.tex"'),
            (f'"{self.pdf}"', '"../../output/other.pdf"'),
            ('  "main.tex" 1', '  "other.tex" 1'),
        ):
            with self.subTest(replacement=new):
                self.fdb.write_text(valid.replace(old, new))
                self.assert_incomplete()

    def test_recorder_requires_bounded_complete_consistent_output(self):
        valid = self.fls.read_text()
        variants = (
            valid.rstrip("\n"),
            valid.replace(f"OUTPUT {self.pdf}\n", ""),
            valid + "IGNORED record\n",
            valid.replace(str(self.cwd), str(self.project)),
            valid.replace("INPUT main.tex", 'INPUT "main.tex'),
            valid.replace("INPUT main.tex", "INPUT missing.tex"),
        )
        for content in variants:
            with self.subTest(content=content):
                self.fls.write_text(content)
                self.assert_incomplete()
        self.fls.write_bytes(b"INPUT \xff\n")
        self.assert_incomplete()
        self.fls.write_text(valid)
        self.assertIsNone(self.collect(complete=False).inputs)
        # An oversized, missing or foreign recorder is not a trace at all.
        with self.assertRaisesRegex(PreparationError, "size limit"):
            read_recorder_trace(self.fls, self.output, self.cwd, 1)
        elsewhere = self.root / "recorder"
        elsewhere.write_text(valid)
        self.fls.unlink()
        with self.assertRaisesRegex(PreparationError, "missing"):
            read_recorder_trace(self.fls, self.output, self.cwd, 2_097_152)
        self.fls.symlink_to(elsewhere)
        with self.assertRaisesRegex(PreparationError, "owned regular"):
            read_recorder_trace(self.fls, self.output, self.cwd, 2_097_152)

    def test_oversized_database_and_path_are_rejected(self):
        self.fdb.write_text(self.fdb.read_text() + " " * 3000 + "\n")
        self.assertIsNone(self.collect(max_bytes=1000).inputs)
        self.evidence(("x" * 4097,))
        self.assert_incomplete("oversized path")


if __name__ == "__main__":
    unittest.main()
