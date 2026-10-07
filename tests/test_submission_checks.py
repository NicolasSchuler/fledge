from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from latexprep.models import Finding, PreparationError
from latexprep.submission_checks import (
    SubmissionOptions,
    check_pdf_identity,
    check_submission,
    check_submission_archive,
)


class SubmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.write("main.tex", r"\documentclass{article}\begin{document}Results.\end{document}")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write(self, name: str, value: str | bytes) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value.encode() if isinstance(value, str) else value)
        return path

    def result(self, rule: str, options: SubmissionOptions) -> Finding:
        return next(
            item for item in check_submission(self.root, "main.tex", options) if item.rule == rule
        )

    def incomplete(self, rule: str, options: SubmissionOptions) -> Finding:
        with patch(
            "latexprep.submission_checks._inventory", return_value=({}, ["Unreadable directory."])
        ):
            return self.result(rule, options)

    def test_source_identity_terms(self) -> None:
        options = SubmissionOptions(identity_terms=("Jane Smith", "private.example"))
        rule = "submission.identity_terms"
        self.assertEqual(self.result(rule, options).status, "passed")
        self.write(
            "Jane Smith.tex", "% JANE SMITH\n" + r"\url{https://private.example/Jane%20Smith}"
        )
        result = self.result(rule, options)
        self.assertEqual((result.code, result.status), ("PRV001", "failed"))
        self.assertEqual(
            {item["channel"] for item in result.details["matches"]}, {"filename", "comment", "url"}
        )
        self.assertEqual(result.severity, "error")
        self.assertNotIn("term", result.details["matches"][0])
        self.write("Jane Smith.tex", b"\xff")
        self.assertTrue(self.result(rule, options).details["issues"])
        self.assertEqual(self.incomplete(rule, options).status, "inconclusive")

    def test_pdf_identity_terms(self) -> None:
        options = SubmissionOptions(identity_terms=("Jane Smith",))
        good = check_pdf_identity("Anonymous findings", {"Author": "Anonymous"}, options)[0]
        self.assertEqual(good.status, "passed")
        bad = check_pdf_identity("Jane Smith", {"Author": "JANE SMITH"}, options)[0]
        self.assertEqual((bad.code, bad.status), ("PRV002", "failed"))
        self.assertEqual({item["channel"] for item in bad.details["matches"]}, {"text", "metadata"})
        self.assertEqual(check_pdf_identity("", {}, options)[0].status, "inconclusive")
        with patch("latexprep.submission_checks.MAX_TOTAL_SOURCE_BYTES", 4):
            self.assertEqual(
                check_pdf_identity("very long text", {"A": "B"}, options)[0].status, "inconclusive"
            )

    def test_identity_hints(self) -> None:
        rule = "submission.identity_hints"
        options = SubmissionOptions(scan_identity_hints=True)
        self.write("main.tex", "% our university\n" + r"\verb|in our previous work|")
        self.assertEqual(self.result(rule, options).status, "passed")
        self.write("main.tex", r"\section*{Acknowledgements}Our institution supported this.")
        result = self.result(rule, options)
        self.assertEqual((result.code, result.status), ("PRV003", "failed"))
        self.assertEqual(result.evidence, "heuristic")
        self.assertEqual(result.severity, "warning")
        self.assertEqual(self.incomplete(rule, options).status, "inconclusive")

    def test_secrets_are_redacted(self) -> None:
        options = SubmissionOptions(scan_secrets=True, filename_max_length=8)
        rule = "submission.secrets"
        self.write("notes.txt", "password=your_password_here")
        self.assertEqual(self.result(rule, options).status, "passed")
        token = "ghp_" + "Ab12" * 10
        assigned = "SensitiveValue123456"
        self.write(token + ".tex", "% " + token + "\npassword=" + assigned)
        results = check_submission(self.root, "main.tex", options)
        result = next(item for item in results if item.rule == rule)
        self.assertEqual((result.code, result.status), ("PRV004", "failed"))
        self.assertEqual(result.evidence, "heuristic")
        serialized = json.dumps([asdict(item) for item in results])
        for value in (token, assigned):
            self.assertNotIn(value, serialized)
        pdf = check_pdf_identity("Text", {"Author": token}, options)
        self.assertEqual(pdf[0].status, "failed")
        self.assertNotIn(token, json.dumps([asdict(item) for item in pdf]))
        self.assertEqual(check_pdf_identity("", {}, options)[0].status, "inconclusive")
        self.assertEqual(self.incomplete(rule, options).status, "inconclusive")

    def test_private_comments(self) -> None:
        rule = "submission.private_comments"
        options = SubmissionOptions(scan_private_comments=True)
        self.write("main.tex", "% Copyright, license retained.\n" + r"\% TODO in active text")
        self.assertEqual(self.result(rule, options).status, "passed")
        self.write("main.tex", "% private note: TODO remove before submission\n")
        before = (self.root / "main.tex").read_bytes()
        result = self.result(rule, options)
        self.assertEqual((result.code, result.status), ("PRV005", "failed"))
        self.assertEqual(result.details["matches"], [{"path": "main.tex", "line": 1}])
        self.assertEqual((self.root / "main.tex").read_bytes(), before)
        self.assertEqual(self.incomplete(rule, options).status, "inconclusive")

    def test_comment_scan_ignores_literal_regions_and_marks_unclosed_input(self) -> None:
        options = SubmissionOptions(scan_private_comments=True)
        rule = "submission.private_comments"
        self.write(
            "main.tex", r"\verb|% TODO example|" + "\n" + r"\begin{verbatim}% FIXME\end{verbatim}"
        )
        self.assertEqual(self.result(rule, options).status, "passed")
        self.write("main.tex", r"\begin{verbatim}% TODO unclosed")
        self.assertEqual(self.result(rule, options).status, "inconclusive")

    def test_shell_escape(self) -> None:
        rule = "submission.shell_escape"
        options = SubmissionOptions(check_shell_escape=True)
        self.write("main.tex", "% \\write18{echo ignored}\n" + r"\verb|\ShellEscape{example}|")
        self.assertEqual(self.result(rule, options).status, "passed")
        for source in (
            r"\immediate\write18{echo private}",
            r"\write 18{command}",
            r"\ShellEscape{command}",
        ):
            self.write("main.tex", source)
            result = self.result(rule, options)
            self.assertEqual((result.code, result.status), ("PRV006", "failed"))
            self.assertEqual(result.severity, "warning")
            self.assertNotIn("command", json.dumps(asdict(result)))
        self.assertEqual(self.incomplete(rule, options).status, "inconclusive")

    def test_filename_length(self) -> None:
        rule = "submission.filename_length"
        options = SubmissionOptions(filename_max_length=8)
        self.assertEqual(self.result(rule, options).status, "passed")
        self.write("folder/a-long-name.tex", "")
        result = self.result(rule, options)
        self.assertEqual((result.code, result.status), ("PKG101", "failed"))
        self.assertEqual(result.details["matches"], [{"path": "folder/a-long-name.tex"}])
        self.assertEqual(self.incomplete(rule, options).status, "inconclusive")

    def test_filename_characters(self) -> None:
        rule = "submission.filename_characters"
        options = SubmissionOptions(filename_allowed_characters="abcdefghijklmnopqrstuvwxyz.")
        self.assertEqual(self.result(rule, options).status, "passed")
        self.write("bad name.tex", "")
        result = self.result(rule, options)
        self.assertEqual((result.code, result.status), ("PKG102", "failed"))
        self.assertEqual(self.incomplete(rule, options).status, "inconclusive")

    def test_file_extensions(self) -> None:
        rule = "submission.file_extensions"
        options = SubmissionOptions(allowed_extensions=(".tex", ".pdf"))
        self.write("image.PDF", b"%PDF-1.7")
        self.assertEqual(self.result(rule, options).status, "passed")
        self.write("image.bmp", b"BM")
        result = self.result(rule, options)
        self.assertEqual((result.code, result.status), ("PKG103", "failed"))
        self.assertEqual(result.details["matches"], [{"path": "image.bmp"}])
        self.assertEqual(self.incomplete(rule, options).status, "inconclusive")

    def test_required_deliverables(self) -> None:
        rule = "submission.deliverables"
        options = SubmissionOptions(
            required_deliverables=(
                ("title.pdf", "pdf"),
                ("letter.txt", "text"),
                ("data.zip", "zip"),
            )
        )
        self.write("title.pdf", b"%PDF-1.7\n")
        self.write("letter.txt", "Dear editor,")
        self.write("data.zip", b"PK\x05\x06" + bytes(18))
        self.assertEqual(self.result(rule, options).status, "passed")
        self.write("title.pdf", "Not a PDF")
        result = self.result(rule, options)
        self.assertEqual((result.code, result.status), ("PKG104", "failed"))
        self.assertEqual(result.details["matches"][0]["reason"], "format evidence does not match")
        missing = self.result(
            rule, SubmissionOptions(required_deliverables=(("missing.pdf", "pdf"),))
        )
        self.assertEqual(missing.details["matches"][0]["reason"], "missing regular file")
        with patch("latexprep.submission_checks._read", side_effect=OSError("unreadable")):
            self.assertEqual(self.result(rule, options).status, "inconclusive")

    def test_archive_size(self) -> None:
        options = SubmissionOptions(max_archive_bytes=10)
        archive = self.write("project.zip", b"0123456789")
        self.assertEqual(check_submission_archive(archive, options)[0].status, "passed")
        archive.write_bytes(b"01234567890")
        result = check_submission_archive(archive, options)[0]
        self.assertEqual((result.code, result.status), ("PKG105", "failed"))
        self.assertEqual(result.details["compressed_bytes"], 11)
        self.assertEqual(
            check_submission_archive(self.root / "absent.zip", options)[0].status, "inconclusive"
        )

    def test_unused_assets(self) -> None:
        rule = "submission.unused_assets"
        options = SubmissionOptions(report_unused_assets=True)
        self.write("used.png", b"image")
        self.write("main.tex", r"\documentclass{article}\includegraphics{used.png}")
        self.assertEqual(self.result(rule, options).status, "passed")
        self.write("other.png", b"image")
        result = self.result(rule, options)
        self.assertEqual((result.code, result.status), ("PKG106", "failed"))
        self.assertEqual(result.details["matches"], [{"path": "other.png"}])
        self.assertIn("No candidate is safe to delete", result.details["scope"])
        self.assertTrue((self.root / "other.png").exists())
        self.write("main.tex", r"\documentclass{article}\input{\dynamic}")
        self.assertEqual(self.result(rule, options).status, "inconclusive")

    def test_template_references(self) -> None:
        rule = "submission.template_reference"
        value = r"\ProvidesClass{sample}[2025/01/01 v1.0 Sample]"
        self.write("sample.cls", value)
        with tempfile.TemporaryDirectory() as directory:
            reference = Path(directory) / "reference.cls"
            reference.write_text(value)
            options = SubmissionOptions(template_references=(("sample.cls", str(reference)),))
            good = self.result(rule, options)
            self.assertEqual((good.code, good.status), ("PKG107", "passed"))
            self.assertTrue(good.details["byte_equal"])
            self.assertEqual(good.details["declared_version_comparison"], "equal")
            self.write("sample.cls", r"\ProvidesClass{sample}[2026/01/01 v2.0 Sample]")
            bad = self.result(rule, options)
            self.assertEqual(bad.status, "failed")
            self.assertEqual(bad.details["declared_version_comparison"], "different")
            self.assertEqual(reference.read_text(), value)
            with patch("latexprep.submission_checks._read", side_effect=OSError("unreadable")):
                self.assertEqual(self.result(rule, options).status, "inconclusive")

    def test_defaults_bounds_validation_and_cancellation(self) -> None:
        self.assertEqual(check_submission(self.root), [])
        self.assertEqual(check_submission_archive(self.root / "missing"), [])
        self.assertEqual(check_pdf_identity("", {}), [])
        for kwargs in (
            {"scan_secrets": 1},
            {"filename_max_length": -1},
            {"max_archive_bytes": True},
            {"identity_terms": ("",)},
            {"allowed_extensions": ("pdf",)},
            {"required_deliverables": (("../secret", "file"),)},
            {"required_deliverables": (("title.pdf", "unknown"),)},
            {"template_references": (("/absolute", "reference.cls"),)},
            {"template_references": (("sample.cls", "bad\x00reference"),)},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(PreparationError):
                SubmissionOptions(**kwargs)
        with patch("latexprep.submission_checks.MAX_SOURCE_BYTES", 4):
            self.assertEqual(
                self.result("submission.secrets", SubmissionOptions(scan_secrets=True)).status,
                "inconclusive",
            )
        with patch(
            "latexprep.submission_checks.cancellation_point",
            side_effect=PreparationError("Analysis cancelled"),
        ):
            with self.assertRaisesRegex(PreparationError, "cancelled"):
                check_submission(self.root, options=SubmissionOptions(scan_secrets=True))

    def test_match_limits_and_uncertain_inventory_do_not_claim_missing_files(self) -> None:
        self.write("main.tex", "Identity term\n" * 100)
        with patch("latexprep.submission_checks.MAX_SCAN_MATCHES", 2):
            result = self.result(
                "submission.identity_terms", SubmissionOptions(identity_terms=("Identity",))
            )
            self.assertEqual(len(result.details["matches"]), 2)
            self.assertTrue(result.details["match_output_limited"])
        options = SubmissionOptions(required_deliverables=(("unknown.pdf", "pdf"),))
        self.assertEqual(self.incomplete("submission.deliverables", options).status, "inconclusive")
        with patch(
            "latexprep.submission_checks.analyze_sources",
            side_effect=PreparationError("Analysis cancelled"),
        ):
            with self.assertRaisesRegex(PreparationError, "cancelled"):
                self.result(
                    "submission.unused_assets", SubmissionOptions(report_unused_assets=True)
                )


if __name__ == "__main__":
    unittest.main()
