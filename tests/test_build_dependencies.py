from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from latexprep.build_dependencies import collect_submission_dependencies


def rule(
    name: str,
    source: str,
    destination: str,
    inputs: list[tuple[str, str]],
    *,
    generated: tuple[str, ...] = (),
    rewritten: tuple[str, ...] = (),
) -> str:
    return (
        f'["{name}"] 1 "{source}" "{destination}" "main" 1 0\n'
        + "".join(f'  "{path}" 1 10 {"0" * 32} "{producer}"\n' for path, producer in inputs)
        + "  (generated)\n"
        + "".join(f'  "{path}"\n' for path in (destination, *generated))
        + "  (rewritten before read)\n"
        + "".join(f'  "{path}"\n' for path in rewritten)
    )


class SubmissionDependenciesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
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
        self.observed: set[Path] = set()
        self.evidence()

    def original(self, name: str) -> None:
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
        names = ("main.tex", *inputs)
        self.observed = {(self.cwd / name).resolve() for name in names}
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

    def collect(self, *, complete: bool = True, max_bytes: int = 2_097_152):
        return collect_submission_dependencies(
            source=self.source,
            project=self.project,
            cwd=self.cwd,
            output=self.output,
            main="article/main.tex",
            engine="pdflatex",
            recorder_inputs=self.observed,
            recorder_complete=complete,
            resource_roots=(self.toolchain,),
            max_bytes=max_bytes,
        )

    def assert_incomplete(self, fragment: str = "") -> None:
        result = self.collect()
        self.assertIsNone(result.inputs, result)
        self.assertFalse(result.details["complete"])
        self.assertIn(fragment, str(result.details["uncertainty"]))

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
            result.details,
        )
        self.assertEqual(result.details["toolchain_inputs"], 1)
        self.assertEqual(result.details["generated_inputs"], 3)

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
                self.observed = {
                    (self.cwd / "main.tex").resolve(),
                    (self.cwd / "data.dat").resolve(),
                }
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
        self.assertIsNone(self.collect(max_bytes=1).inputs)
        self.fls.unlink()
        self.assert_incomplete("missing")

    def test_oversized_database_and_path_are_rejected(self):
        self.fdb.write_text(self.fdb.read_text() + " " * 3000 + "\n")
        self.assertIsNone(self.collect(max_bytes=1000).inputs)
        self.evidence(("x" * 4097,))
        self.assert_incomplete("oversized path")


if __name__ == "__main__":
    unittest.main()
