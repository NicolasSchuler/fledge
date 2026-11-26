"""Tests of the submission dependencies read from the TeX recorder and the latexmk database.

``collect_submission_dependencies`` ships the original project inputs a build read and
nothing generated or from the toolchain; missing, linked, malformed, inconsistent or
oversized evidence fails closed. ``read_recorder`` judges the recorder trace for build
checks: a trace that is absent, unreadable, oversized or unresolvable is reported as
inconclusive, and an input that leaves the workspace is undeclared.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from latexprep.build_dependencies import (
    DependencyRequest,
    collect_submission_dependencies,
    read_recorder_trace,
)
from latexprep.build_recorder import RecorderEvidence, read_recorder
from latexprep.build_workspace import BuildLayout, seed_generated_files
from latexprep.errors import PreparationError
from latexprep.package_headers import LoadedPackage
from tests.support import ProjectTestCase


def rule(
    name: str,
    source: str,
    destination: str,
    inputs: list[tuple[str, str]],
    *,
    generated: tuple[str, ...] = (),
    rewritten: tuple[str, ...] = (),
    missing: tuple[str, ...] = (),
    ran: bool = True,
) -> str:
    """Return one rule of a latexmk database (``.fdb_latexmk``).

    Args:
        name: The name of the rule, such as ``pdflatex``.
        source: The source file of the rule.
        destination: The destination file of the rule, listed first among its generated files.
        inputs: The path and producing rule of each input; an empty producer for none.
        generated: The files the rule generates besides ``destination``.
        rewritten: The files the rule rewrites before reading them.
        missing: Inputs recorded as absent, with size -1 and no producer.
        ran: Whether latexmk ran the rule; a rule it never ran has run time 0.

    Returns:
        The text of the rule: its header line, its inputs, and its generated and rewritten
        files.
    """
    return (
        f'["{name}"] {1 if ran else 0} "{source}" "{destination}" "main" {1 if ran else 0} 0\n'
        + "".join(f'  "{path}" 1 10 {"0" * 32} "{producer}"\n' for path, producer in inputs)
        + "".join(f'  "{path}" 0 -1 0 ""\n' for path in missing)
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
        missing: tuple[str, ...] = (),
    ) -> None:
        """Write the recorder trace and the latexmk database of a build of ``main.tex``.

        Args:
            inputs: The files the build read besides ``main.tex``, relative to its working
                directory.
            generated: The files the build wrote besides the PDF.
            rewritten: The files the pdflatex rule rewrote before reading them.
            extra_rules: Database rules written before the pdflatex rule.
            producers: The rule that produced each input, by input path.
            missing: Files the pdflatex rule looked for that never existed (size -1).
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
                missing=missing,
            )
        )

    def collect(
        self,
        *,
        complete: bool = True,
        max_bytes: int = 2_097_152,
        tool_state: Path | None = None,
        seeded: dict[Path, str] | None = None,
    ):
        """Collect the submission dependencies of the build from its evidence.

        Args:
            complete: Whether the recorder is reported complete.
            max_bytes: The byte limit of reading the recorder trace and of the collection.
            tool_state: The tool state directory of the build, if any.
            seeded: The supplied generated files copied into the output folder before the
                build, each mapped to the project-relative name of the file it copies.

        Returns:
            The collected dependencies.
        """
        layout = BuildLayout(
            self.source, self.project.parent, self.project, self.output, self.cwd, seeded or {}
        )
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

    def test_a_seeded_bbl_that_no_rule_regenerated_stands_for_the_supplied_file(self):
        """Count the supplied .bbl as an input when the build read its seeded copy unchanged.

        latexmk records a BibTeX rule it never ran (run time 0) when no .bib exists; the
        copy of the supplied ``main.bbl`` in the output folder is then what the build read.
        """
        self.original("article/main.bbl")
        seeded = self.output / "main.bbl"
        seeded.write_text("source content")
        never_ran = rule(
            "bibtex ../../output/main",
            "../../output/main.aux",
            "../../output/main.bbl",
            [],
            missing=("../../output/main.aux", "refs.bib"),
            ran=False,
        )
        self.evidence(
            ("../../output/main.aux", "../../output/main.bbl"),
            generated=("../../output/main.aux",),
            extra_rules=never_ran,
            producers={
                "../../output/main.aux": "pdflatex",
                "../../output/main.bbl": "bibtex ../../output/main",
            },
        )
        self.assert_incomplete("no build rule generated it")
        result = self.collect(seeded={seeded: "article/main.bbl"})
        self.assertEqual(result.inputs, {"article/main.tex", "article/main.bbl"}, result)
        self.assertEqual(result.rules, ("pdflatex",))
        self.assertEqual(result.generated_reads, {"main.aux"})

    def test_a_seeded_bbl_that_bibtex_regenerated_is_a_generated_read(self):
        self.original("article/main.bbl")
        self.original("refs.bib")
        seeded = self.output / "main.bbl"
        seeded.write_text("regenerated")
        bibliography = rule(
            "bibtex ../../output/main",
            "../../output/main.aux",
            "../../output/main.bbl",
            [("../../output/main.aux", "pdflatex"), ("../refs.bib", "")],
        )
        self.evidence(
            ("../../output/main.aux", "../../output/main.bbl"),
            generated=("../../output/main.aux",),
            extra_rules=bibliography,
            producers={
                "../../output/main.aux": "pdflatex",
                "../../output/main.bbl": "bibtex ../../output/main",
            },
        )
        result = self.collect(seeded={seeded: "article/main.bbl"})
        self.assertEqual(result.inputs, {"article/main.tex", "refs.bib"}, result)
        self.assertEqual(result.generated_reads, {"main.aux", "main.bbl"})
        self.assertEqual(result.generated_files["main.bbl"], seeded)

    def test_a_working_directory_bibtex_rule_that_never_ran_is_ignored(self):
        """Ignore the rule latexmk 4.83 creates for a supplied .bbl beside the main file.

        That release names the rule ``bibtex main`` after the supplied file and gives it a
        source, ``main.aux`` in the working directory, that does not exist, so it never
        runs; the supplied ``main.bbl`` the build read is an original input.
        """
        self.original("article/main.bbl")
        stale = rule("bibtex main", "main.aux", "main.bbl", [], missing=("main.aux",), ran=False)
        self.evidence(
            ("main.bbl", "../../output/main.aux"),
            generated=("../../output/main.aux",),
            extra_rules=stale,
            producers={"main.bbl": "bibtex main", "../../output/main.aux": "pdflatex"},
        )
        result = self.collect()
        self.assertEqual(result.inputs, {"article/main.tex", "article/main.bbl"}, result)
        self.assertEqual(result.rules, ("pdflatex",))

    def test_a_failed_rule_still_makes_the_evidence_incomplete(self):
        failed = rule("bibtex main", "main.aux", "main.bbl", [("main.aux", "")]).replace(
            '"main" 1 0', '"main" 1 2'
        )
        self.evidence(extra_rules=failed)
        self.assert_incomplete("did not complete successfully")

    def test_inputs_that_never_existed_are_skipped_and_a_missing_list_is_named(self):
        """Skip size -1 rows, and record a list whose tool input has entries as never created.

        TeX looked for ``main.nls`` and ``main.ind``; neither was written. Only the
        nomenclature is missing from the document: ``main.nlo`` has entries, while the empty
        ``main.idx`` means an empty index.
        """
        (self.output / "main.nlo").write_text("\\item [{$\\eta$}]\n")
        (self.output / "main.idx").write_text("")
        self.evidence(missing=("../../output/main.nls", "../../output/main.ind"))
        result = self.collect()
        self.assertEqual(result.inputs, {"article/main.tex"}, result.to_details())
        self.assertEqual(result.never_created, ("main.nls",))
        self.assertEqual(result.to_details()["never_created"], ["main.nls"])

    def test_a_glossary_written_for_xindy_is_named_as_not_built_and_not_shipped(self):
        """Name the empty glossary the rule left for xindy; a makeindex glossary is shipped.

        The glossary rule of ``latexmk_command`` leaves ``main.gls`` empty when ``main.aux``
        records an xindy style, and the document then lacks its glossary.
        """
        (self.output / "main.glo").write_text('(indexentry :tkey (("svm" "svm")) :locref "1")\n')
        (self.output / "main.gls").write_text("")
        glossary = rule(
            "cusdep glo gls ../../output/main",
            "../../output/main.glo",
            "../../output/main.gls",
            [("../../output/main.glo", "pdflatex")],
        )
        for style, xindy in (("main.xdy", True), ("main.ist", False)):
            with self.subTest(style=style):
                (self.output / "main.aux").write_text(f"\\relax\n\\@istfilename{{{style}}}\n")
                self.evidence(
                    ("../../output/main.gls",),
                    generated=("../../output/main.glo",),
                    extra_rules=glossary,
                    producers={"../../output/main.gls": "cusdep glo gls ../../output/main"},
                )
                result = self.collect()
                self.assertEqual(result.inputs, {"article/main.tex"}, result.to_details())
                self.assertIn("main.gls", result.generated_reads)
                self.assertEqual(result.xindy_lists, ("main.gls",) if xindy else ())
                self.assertEqual(result.never_created, ("main.gls",) if xindy else ())
                self.assertEqual("main.gls" in result.generated_files, not xindy)

    def test_seeded_lists_preserved_for_xindy_are_project_inputs(self):
        """Treat the lists as supplied even when the custom rules claim to produce them."""
        extra_rules, producers = [], {}
        tool_inputs = []
        for tool_suffix, list_suffix in (("glo", "gls"), ("acn", "acr")):
            self.original(f"article/main.{list_suffix}")
            (self.output / f"main.{tool_suffix}").write_text("xindy entries\n")
            tool_input = f"../../output/main.{tool_suffix}"
            destination = f"../../output/main.{list_suffix}"
            producer = f"cusdep {tool_suffix} {list_suffix} ../../output/main"
            extra_rules.append(rule(producer, tool_input, destination, [(tool_input, "pdflatex")]))
            producers[destination] = producer
            tool_inputs.append(tool_input)
        (self.output / "main.aux").write_text("\\@istfilename{main.xdy}\n")
        seeded = seed_generated_files(self.project, self.cwd, self.output)
        self.evidence(
            tuple(producers),
            generated=tuple(tool_inputs),
            extra_rules="".join(extra_rules),
            producers=producers,
        )
        result = self.collect(seeded=seeded)
        self.assertEqual(
            result.inputs, {"article/main.tex", "article/main.gls", "article/main.acr"}
        )
        self.assertEqual(result.generated_reads, {"main.glo", "main.acn"})
        self.assertEqual(set(result.generated_files), result.generated_reads)
        self.assertEqual(result.generated_inputs, 2)
        self.assertEqual(result.never_created, ())
        self.assertEqual(result.xindy_lists, ())

        # A prior makeindex pass can replace a seed before the AUX switches to xindy.
        (self.output / "main.gls").write_text("edited content")
        changed = self.collect(seeded=seeded)
        self.assertEqual(changed.inputs, {"article/main.tex", "article/main.acr"})
        self.assertEqual(changed.generated_files["main.gls"], self.output / "main.gls")
        self.assertEqual(changed.xindy_lists, ())

        # Empty supplied lists are still inputs, but provide no entries for the PDF.
        for path, name in seeded.items():
            for copy in (self.source / name, self.project / name, path):
                copy.write_bytes(b"")
        empty = self.collect(seeded=seeded)
        self.assertEqual(empty.inputs, result.inputs)
        self.assertEqual(empty.xindy_lists, ("main.acr", "main.gls"))
        self.assertEqual(empty.never_created, empty.xindy_lists)

        (self.output / "main.aux").write_text("\\@istfilename{main.ist}\n")
        for path in seeded:
            path.write_text("regenerated with makeindex\n")
        regenerated = self.collect(seeded=seeded)
        self.assertEqual(regenerated.inputs, {"article/main.tex"})
        self.assertEqual(
            regenerated.generated_reads, {"main.glo", "main.acn", "main.gls", "main.acr"}
        )

    def test_empty_seed_read_without_a_custom_rule_still_reports_the_missing_list(self):
        for root in (self.source, self.project):
            (root / "article/main.acr").write_bytes(b"")
        seeded = seed_generated_files(self.project, self.cwd, self.output)
        (self.output / "main.aux").write_text("\\@istfilename{main.xdy}\n")
        (self.output / "main.acn").write_text("acronym entry\n")
        self.assertEqual(self.collect(seeded=seeded).xindy_lists, ())  # No read of the empty seed.
        self.evidence(("../../output/main.acr",), generated=("../../output/main.acn",))
        result = self.collect(seeded=seeded)
        self.assertEqual(result.xindy_lists, ("main.acr",))
        self.assertEqual(result.inputs, {"article/main.tex", "article/main.acr"})

    def test_a_generated_input_without_a_producer_names_the_tool_that_writes_it(self):
        (self.output / "main.gls").write_text("glossary")
        self.evidence(("../../output/main.gls",))
        self.assert_incomplete("main.gls holds the glossary, which makeglossaries writes")
        self.evidence(("main.ind",))
        self.assert_incomplete("main.ind holds the index, which makeindex writes from main.idx")

    def test_each_toolchain_input_is_resolved_once_whatever_the_number_of_roots(self):
        """Resolve a toolchain input once, not once per root: TeX Live trees are deep in links."""
        inputs = []
        for number in range(3):
            path = self.toolchain / f"package{number}.sty"
            path.write_text("system package")
            inputs.append(str(path))
        self.evidence(tuple(inputs))
        layout = BuildLayout(self.source, self.project.parent, self.project, self.output, self.cwd)
        roots = (*(self.root / f"root{number}" for number in range(5)), self.toolchain)
        resolved: list[Path] = []
        original = Path.resolve

        def counted(path: Path, *, strict: bool = False) -> Path:
            """Record which path is resolved, then resolve it."""
            resolved.append(path)
            return original(path, strict=strict)

        request = DependencyRequest(
            layout=layout,
            main="article/main.tex",
            engine="pdflatex",
            recorder=read_recorder_trace(self.fls, self.output, self.cwd, 2_097_152),
            recorder_complete=True,
            resource_roots=roots,
            max_bytes=2_097_152,
        )
        with patch.object(Path, "resolve", counted):
            result = collect_submission_dependencies(request)
        self.assertEqual(result.toolchain_inputs, 3, result.to_details())
        for name in inputs:
            self.assertEqual(resolved.count(Path(name)), 1, name)


class RecorderEvidenceTests(ProjectTestCase):
    """Tests of ``read_recorder`` on written recorder traces in a build workspace.

    The workspace ``work`` holds the project copy with the main file ``article/main.tex`` and
    the output folder that receives the trace; ``toolchain`` is the declared toolchain root.
    """

    def setUp(self) -> None:
        """Create the workspace, the project copy with its main file and the toolchain root."""
        super().setUp()
        self.work = self.root / "work"
        self.project = self.work / "project"
        self.cwd = self.project / "article"
        self.output = self.work / "output"
        self.toolchain = self.root / "toolchain"
        for directory in (self.cwd, self.output, self.toolchain):
            directory.mkdir(parents=True)
        (self.cwd / "main.tex").write_text("main")
        self.layout = BuildLayout(
            self.root / "source", self.work, self.project, self.output, self.cwd
        )
        self.fls = self.output / "main.fls"

    def trace(self, *inputs: str) -> None:
        """Write the recorder trace of a build that read ``main.tex`` and then ``inputs``.

        Args:
            inputs: The input paths recorded after the main file, as TeX wrote them.
        """
        self.fls.write_text(
            f"PWD {self.cwd}\n" + "".join(f"INPUT {name}\n" for name in ("main.tex", *inputs))
        )

    def package(self, directory: Path, name: str) -> Path:
        """Write the class or package file ``name`` in ``directory`` with a literal header.

        Args:
            directory: The directory of the file.
            name: The file name, ending in ``.cls`` or ``.sty``.

        Returns:
            The path of the file.
        """
        path = directory / name
        kind = "Class" if path.suffix == ".cls" else "Package"
        path.write_text(f"\\Provides{kind}{{{path.stem}}}[2024/01/02 v1.0 Test]\n")
        return path

    def read(self, max_bytes: int = 2_097_152) -> RecorderEvidence:
        """Read the trace of ``article/main.tex``, with the toolchain as the declared root."""
        return read_recorder("article/main.tex", self.layout, (self.toolchain,), max_bytes)

    def resolution_fails(self, path: Path, failure: Exception):
        """Make resolving ``path`` raise ``failure``, as a loop of links or an odd name would.

        Args:
            path: The path that cannot be resolved.
            failure: The exception that resolving it raises.

        Returns:
            The patch, to use as a context manager.
        """
        original = Path.resolve

        def resolve(candidate: Path, *, strict: bool = False) -> Path:
            """Raise ``failure`` for ``path`` and resolve every other path as usual."""
            if candidate == path:
                raise failure
            return original(candidate, strict=strict)

        return patch.object(Path, "resolve", resolve)

    def assert_trace_finding(self, evidence: RecorderEvidence, fragment: str) -> None:
        """Assert that ``evidence`` is incomplete and holds one inconclusive trace error.

        Args:
            evidence: The evidence that was read.
            fragment: Text that the message of the finding must contain.
        """
        self.assertFalse(evidence.complete)
        self.assertEqual(len(evidence.findings), 1, evidence.findings)
        finding = evidence.findings[0]
        self.assertEqual(
            (finding.rule, finding.severity, finding.status, finding.path),
            ("build.dependency_trace", "error", "inconclusive", "article/main.tex"),
        )
        self.assertIn(fragment, finding.message)

    def assert_without_trace(self, evidence: RecorderEvidence) -> None:
        """Assert that no trace could be read: no evidence at all, and the finding that says so."""
        self.assertIsNone(evidence.trace)
        self.assertEqual(evidence.inputs, frozenset())
        self.assertEqual(evidence.project_inputs, frozenset())
        self.assertEqual(evidence.loaded_packages, ())
        self.assert_trace_finding(evidence, "recorder trace was not produced")

    def test_a_complete_trace_sorts_project_and_toolchain_inputs(self):
        local = self.package(self.cwd, "local.sty")
        (self.cwd / "chapter.tex").write_text("chapter")
        system = self.package(self.toolchain, "article.cls")
        self.trace("local.sty", "chapter.tex", str(system))
        evidence = self.read()
        self.assertTrue(evidence.complete, evidence.findings)
        self.assertEqual(evidence.findings, ())
        self.assertIsNotNone(evidence.trace)
        self.assertEqual(
            evidence.project_inputs,
            {"article/main.tex", "article/local.sty", "article/chapter.tex"},
        )
        self.assertEqual(
            evidence.inputs,
            {self.cwd / "main.tex", local, self.cwd / "chapter.tex", system},
        )
        self.assertEqual(
            evidence.loaded_packages,
            (
                LoadedPackage("local.sty", "article/local.sty", "project", "2024/01/02", "v1.0"),
                LoadedPackage("article.cls", str(system), "toolchain", "2024/01/02", "v1.0"),
            ),
        )

    def test_a_missing_trace_leaves_no_evidence_and_an_inconclusive_error(self):
        self.assert_without_trace(self.read())

    def test_a_directory_in_place_of_the_trace_leaves_no_evidence(self):
        self.fls.mkdir()
        self.assert_without_trace(self.read())

    def test_a_linked_trace_leaves_no_evidence(self):
        self.trace()
        elsewhere = self.root / "elsewhere.fls"
        self.fls.rename(elsewhere)
        self.fls.symlink_to(elsewhere)
        self.assert_without_trace(self.read())

    @unittest.skipIf(os.geteuid() == 0, "the superuser can read every file")
    def test_a_trace_without_read_permission_leaves_no_evidence(self):
        self.trace()
        self.fls.chmod(0)
        self.assert_without_trace(self.read())

    def test_a_trace_over_the_size_limit_leaves_no_evidence_but_one_at_the_limit_is_read(self):
        self.trace()
        size = self.fls.stat().st_size
        self.assert_without_trace(self.read(max_bytes=size - 1))
        evidence = self.read(max_bytes=size)
        self.assertTrue(evidence.complete, evidence.findings)
        self.assertIsNotNone(evidence.trace)

    def test_a_recorded_path_that_cannot_be_resolved_leaves_the_trace_incomplete(self):
        self.trace("unresolvable.tex")
        unresolvable = self.cwd / "unresolvable.tex"
        failures = (
            OSError("unreadable"),
            RuntimeError("loop of links"),
            ValueError("embedded null byte"),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                with self.resolution_fails(unresolvable, failure):
                    evidence = self.read()
                self.assert_trace_finding(evidence, "contains a path that could not be resolved")
                # The path is skipped, not reported as external, and the main file still counts.
                self.assertIsNotNone(evidence.trace)
                self.assertEqual(evidence.inputs, {self.cwd / "main.tex"})
                self.assertEqual(evidence.project_inputs, {"article/main.tex"})

    def test_more_loaded_packages_than_the_limit_leave_the_trace_incomplete(self):
        one, two, three = (self.package(self.toolchain, f"{name}.sty") for name in "abc")
        (self.cwd / "chapter.tex").write_text("chapter")
        with patch("latexprep.build_recorder.MAX_LOADED_PACKAGES", 2):
            # A package read twice and a file that is no package do not count against the limit.
            self.trace(str(one), str(two), str(one), "chapter.tex")
            at_limit = self.read()
            self.trace(str(one), str(two), str(three))
            over_limit = self.read()
        self.assertTrue(at_limit.complete, at_limit.findings)
        self.assertEqual([package.name for package in at_limit.loaded_packages], ["a.sty", "b.sty"])
        self.assert_trace_finding(over_limit, "exceeds the limit of 2 loaded packages")
        # The first packages stay recorded and the one over the limit is still an input.
        self.assertEqual(
            [package.name for package in over_limit.loaded_packages], ["a.sty", "b.sty"]
        )
        self.assertIn(three, over_limit.inputs)

    def test_a_link_that_leads_out_of_the_workspace_is_an_undeclared_external_input(self):
        outside = self.package(self.root, "secret.sty")
        (self.cwd / "linked.sty").symlink_to(outside)
        self.trace("linked.sty")
        evidence = self.read()
        self.assertEqual(len(evidence.findings), 1, evidence.findings)
        finding = evidence.findings[0]
        self.assertEqual(
            (finding.rule, finding.severity, finding.status, finding.path),
            ("build.external_dependency", "error", "failed", "article/main.tex"),
        )
        self.assertIn("linked.sty", finding.message)
        # The target is recorded where it lies, is no project file and is never read as a package.
        self.assertIn(outside, evidence.inputs)
        self.assertEqual(evidence.project_inputs, {"article/main.tex"})
        self.assertEqual(evidence.loaded_packages, ())
        self.assertTrue(evidence.complete)

    def test_a_link_from_the_workspace_into_a_declared_root_is_not_a_declared_input(self):
        system = self.package(self.toolchain, "system.sty")
        (self.cwd / "linked.sty").symlink_to(system)
        self.trace("linked.sty")
        linked = self.read()
        self.assertEqual(
            [finding.rule for finding in linked.findings], ["build.external_dependency"]
        )
        self.assertIn("linked.sty", linked.findings[0].message)
        self.assertEqual(linked.loaded_packages, ())
        self.assertEqual(linked.project_inputs, {"article/main.tex"})
        # Reading the file by its own path is declared; reaching it through the workspace is not.
        self.trace("linked.sty", str(system))
        both = self.read()
        self.assertEqual(both.findings, linked.findings)
        self.assertEqual(
            both.loaded_packages,
            (LoadedPackage("system.sty", str(system), "toolchain", "2024/01/02", "v1.0"),),
        )
        self.assertEqual(both.inputs, {self.cwd / "main.tex", system})


if __name__ == "__main__":
    unittest.main()
