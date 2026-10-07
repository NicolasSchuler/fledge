from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from latexprep.models import PreparationError, has_blockers
from latexprep.source_transform import SourceTransformOptions, plan_source_transforms


class SourceTransformTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def write(self, path: str, text: str | bytes) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8") if isinstance(text, str) else text)

    def plan(self, **options):
        return plan_source_transforms(self.root, "main.tex", SourceTransformOptions(**options))

    def test_comment_removal(self) -> None:
        original = (
            b"\xef\xbb\xbf% Copyright Example\r\n% Permission to redistribute.\r\n"
            b"\\documentclass{article}\r\n%!TeX program = pdflatex\r\n"
            b"one% TODO private draft\r\ntwo \\% still text\r\n"
            b"% ordinary public note\r\n\\verb|% private literal|\r\n"
            b"\\begin{verbatim}\r\n% TODO literal\r\n\\end{verbatim}\r\n"
        )
        self.write("main.tex", original)
        plan = self.plan(comment_policy="private")
        self.assertFalse(has_blockers(plan.findings), plan.findings)
        updated = plan.contents["main.tex"]
        self.assertIn(b"one%\r\ntwo \\% still text", updated)
        self.assertIn(b"% ordinary public note", updated)
        self.assertIn(b"% Permission to redistribute.", updated)
        self.assertIn(b"%!TeX program", updated)
        self.assertIn(b"% TODO literal", updated)
        self.assertIn(b"% private literal", updated)
        self.assertEqual(updated.count(b"\r\n"), original.count(b"\r\n"))
        self.assertTrue(updated.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(plan.originals["main.tex"], original)
        self.assertEqual((self.root / "main.tex").read_bytes(), original)
        self.assertEqual(next(f for f in plan.findings if f.code == "TEX201").status, "passed")
        all_comments = self.plan(comment_policy="all")
        self.assertNotIn(b"ordinary public note", all_comments.contents["main.tex"])
        self.write("main.tex", "\\documentclass{article}\nText.\n")
        clean = self.plan(comment_policy="all")
        self.assertEqual(clean.contents, {})
        self.assertEqual(clean.findings[0].details["removed_comments"], 0)
        self.write("main.tex", "\\documentclass{article}\n\\begin{verbatim}\n% TODO note\n")
        blocked = self.plan(comment_policy="all")
        self.assertTrue(has_blockers(blocked.findings))
        self.assertEqual(blocked.contents, {})
        self.assertEqual(
            next(f for f in blocked.findings if f.code == "TEX201").status, "inconclusive"
        )

    def test_comment_significant_percent_and_formatter_protection(self) -> None:
        self.write(
            "main.tex",
            "\\documentclass{article}\nA% remove me\nB\n"
            "% ordinary standalone comment\nC\n% tex-fmt: off\n"
            "% TODO retained in protected block\n% tex-fmt: on\n",
        )
        result = self.plan(comment_policy="all")
        self.assertFalse(has_blockers(result.findings), result.findings)
        self.assertIn(b"A%\nB\n%\nC\n", result.contents["main.tex"])
        self.assertIn(b"% TODO retained in protected block", result.contents["main.tex"])
        self.write("main.tex", "\\documentclass{article}\n\\catcode`\\%=12\n% private\n")
        self.assertTrue(has_blockers(self.plan(comment_policy="private").findings))

    def test_comment_removal_only_edits_document_sources(self) -> None:
        self.write("main.tex", "\\documentclass{local/template}\n% TODO note\nText.\n")
        self.write(
            "local/template.cls",
            "\\ProvidesClass{template}\n% TODO internal note\n\\LoadClass{article}\n",
        )
        plan = self.plan(comment_policy="all")
        self.assertFalse(has_blockers(plan.findings), plan.findings)
        self.assertEqual(set(plan.contents), {"main.tex"})
        self.assertNotIn(b"TODO note", plan.contents["main.tex"])
        self.assertEqual({finding.path for finding in plan.findings}, {"main.tex"})

    def test_merge_inputs(self) -> None:
        original = (
            "\\documentclass{article}\n\\begin{document}\n\\input{parts/body}\n\\end{document}\n"
        )
        self.write("main.tex", original)
        self.write("parts/body.tex", "Body.\n\\input{details}\n")
        self.write("details.tex", "Details.\n")
        plan = self.plan(merge_inputs=True)
        self.assertFalse(has_blockers(plan.findings), plan.findings)
        self.assertIn(b"Body.\nDetails.\n", plan.contents["main.tex"])
        self.assertNotIn(b"\\input", plan.contents["main.tex"])
        self.assertEqual(plan.removals, {"parts/body.tex", "details.tex"})
        self.assertEqual(plan.removal_originals["details.tex"], b"Details.\n")
        self.assertEqual(next(f for f in plan.findings if f.code == "TEX202").status, "passed")
        self.assertEqual((self.root / "main.tex").read_text(), original)
        self.assertTrue((self.root / "parts/body.tex").exists())
        self.write("main.tex", "\\documentclass{article}\n\\include{details}\n")
        blocked = self.plan(merge_inputs=True)
        self.assertTrue(has_blockers(blocked.findings))
        self.assertEqual(blocked.contents, {})
        self.assertEqual(blocked.removals, set())
        self.assertEqual(
            next(f for f in blocked.findings if f.code == "TEX202").status, "inconclusive"
        )
        self.write("main.tex", "\\documentclass{article}\n\\input{missing}\n")
        missing = self.plan(merge_inputs=True)
        self.assertTrue(has_blockers(missing.findings))
        self.assertEqual(next(f for f in missing.findings if f.code == "TEX202").status, "failed")

    def test_merge_rejects_inline_execution_and_retains_comments(self) -> None:
        self.write("main.tex", "\\documentclass{article}\nText \\input{body} after.\n")
        self.write("body.tex", "Body.\n")
        self.assertTrue(has_blockers(self.plan(merge_inputs=True).findings))
        self.write("main.tex", "\\documentclass{article}\n\\input{body}% retained note\n")
        plan = self.plan(merge_inputs=True)
        self.assertFalse(has_blockers(plan.findings))
        self.assertIn(b"Body.\n% retained note\n", plan.contents["main.tex"])
        self.write("main.tex", "\\documentclass{article}\n\\input% Copyright retained\n{ body }\n")
        wrapped = self.plan(merge_inputs=True)
        self.assertFalse(has_blockers(wrapped.findings), wrapped.findings)
        self.assertIn(b"% Copyright retained\nBody.\n", wrapped.contents["main.tex"])
        self.write("body.tex", "Body without newline")
        self.assertTrue(has_blockers(self.plan(merge_inputs=True).findings))

    def test_inline_bibliography(self) -> None:
        main = (
            "\\documentclass{article}\n\\begin{document}\n"
            "\\bibliography{references}\n\\end{document}\n"
        )
        bbl = "\\begin{thebibliography}{1}\n\\bibitem{key} Reference.\n\\end{thebibliography}\n"
        self.write("main.tex", main)
        self.write("references.bib", "@article{key,title={Reference}}")
        self.write("main.bbl", bbl)
        plan = self.plan(inline_bibliography="main.bbl")
        self.assertFalse(has_blockers(plan.findings), plan.findings)
        self.assertIn(bbl.encode(), plan.contents["main.tex"])
        self.assertNotIn(b"\\bibliography", plan.contents["main.tex"])
        self.assertEqual(next(f for f in plan.findings if f.code == "TEX203").status, "passed")
        self.assertEqual((self.root / "main.tex").read_text(), main)
        missing = self.plan(inline_bibliography="missing.bbl")
        self.assertEqual(next(f for f in missing.findings if f.code == "TEX203").status, "failed")
        self.assertEqual(missing.contents, {})
        self.write("main.bbl", "\\refsection{0}\n\\entry{key}{article}{}\n")
        unsupported = self.plan(inline_bibliography="main.bbl")
        self.assertEqual(
            next(f for f in unsupported.findings if f.code == "TEX203").status, "inconclusive"
        )
        self.assertEqual(unsupported.contents, {})

    def test_combined_proposal_is_atomic_when_any_transform_is_unresolved(self) -> None:
        self.write("main.tex", "\\documentclass{article}\n% TODO remove\n\\include{body}\n")
        self.write("body.tex", "Body.\n")
        plan = self.plan(comment_policy="all", merge_inputs=True)
        self.assertTrue(has_blockers(plan.findings))
        self.assertEqual(
            (plan.contents, plan.originals, plan.removals, plan.changes), ({}, {}, set(), [])
        )
        self.assertIn(b"TODO", (self.root / "main.tex").read_bytes())

    def test_invalid_options_are_rejected(self) -> None:
        for options in (
            {"comment_policy": "delete-lines"},
            {"merge_inputs": "yes"},
            {"inline_bibliography": "../private.bbl"},
            {"filename_overrides": (("main.tex", "a.tex"), ("main.tex", "b.tex"))},
        ):
            with self.subTest(options=options), self.assertRaises(PreparationError):
                SourceTransformOptions(**options)


if __name__ == "__main__":
    unittest.main()
