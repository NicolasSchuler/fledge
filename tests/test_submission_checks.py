"""Submission checks: anonymity, secrets, private comments, file policy and deliverables."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from dataclasses import asdict, fields
from pathlib import Path
from unittest.mock import patch

from latexprep.config import Settings
from latexprep.core import run_job
from latexprep.errors import PreparationError
from latexprep.findings import Finding
from latexprep.job_context import JobRequest
from latexprep.option_config import read_options
from latexprep.submission_checks import (
    BuiltPdfRole,
    check_included_pdf_information,
    check_pdf_identity,
    check_submission,
    check_submission_archive,
)
from latexprep.submission_options import SubmissionOptions
from latexprep.submission_scan_findings import DEFAULT_SCAN_LIMITS, ScanLimits
from tests.support import (
    AsyncProjectTestCase,
    ProjectTestCase,
    controlled_backends,
    object_stream,
    pdf_bytes,
    pdf_stream,
    single_file_project,
)

# Characters of hostile text for the identity-hint patterns. A linear scan of the slowest
# input takes about 0.1 s here, and coverage tracing in CI makes it about ten times slower;
# a pattern that backtracks quadratically over this much text takes minutes.
HOSTILE_CHARACTERS = 200_000
LINEAR_TIME_LIMIT_SECONDS = 10.0


class SubmissionTests(ProjectTestCase):
    """Each submission check, its options and limits, and the messages it reports.

    The messages state counts, locations and limits but never echo identity terms or credentials,
    and an incomplete file inventory makes a check inconclusive instead of claiming missing files.
    """

    def setUp(self) -> None:
        """Write a one-line article as the project's ``main.tex``."""
        super().setUp()
        self.write("main.tex", r"\documentclass{article}\begin{document}Results.\end{document}")

    def result(
        self, rule: str, options: SubmissionOptions, limits: ScanLimits = DEFAULT_SCAN_LIMITS
    ) -> Finding:
        """Run the submission checks on the test project and return the finding of ``rule``.

        Args:
            rule: The rule whose finding to return.
            options: The submission options.
            limits: The scan limits.

        Returns:
            The first finding of ``rule``.
        """
        findings = check_submission(self.root, "main.tex", options, limits=limits)
        return next(item for item in findings if item.rule == rule)

    def incomplete(self, rule: str, options: SubmissionOptions) -> Finding:
        """Return the finding of ``rule`` when the file inventory reports an unreadable directory.

        Args:
            rule: The rule whose finding to return.
            options: The submission options.

        Returns:
            The first finding of ``rule``.
        """
        with patch(
            "latexprep.submission_checks.inventory_files",
            return_value=({}, ["Unreadable directory."]),
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
        tiny = ScanLimits(max_pdf_evidence_bytes=4)
        self.assertEqual(
            check_pdf_identity("very long text", {"A": "B"}, options, limits=tiny)[0].status,
            "inconclusive",
        )

    def test_short_identity_terms_match_whole_words_unless_substring_is_requested(self) -> None:
        rule = "submission.identity_terms"
        word = SubmissionOptions(identity_terms=("Li",))
        substring = SubmissionOptions(identity_terms=("Li",), identity_term_matching="substring")
        self.assertEqual(word.identity_term_matching, "word")
        self.write("Linear.tex", "Linear models, Delivery, and a complicated table.\n")
        self.assertEqual(self.result(rule, word).status, "passed")
        found = self.result(rule, substring)
        self.assertEqual(found.status, "failed")
        self.assertEqual({item["path"] for item in found.details["matches"]}, {"Linear.tex"})
        for text, expected in (
            ("Li, Wei and Zhang\n", True),
            ("by LI\n", True),
            ("(Li)\n", True),
            ("Li2\n", False),
            ("li_paper has no word edge\n", True),
            ("Lisbon\n", False),
            ("Mali\n", False),
        ):
            with self.subTest(text=text):
                self.write("Linear.tex", text)
                self.assertEqual(self.result(rule, word).status == "failed", expected)
                self.assertEqual(self.result(rule, substring).status, "failed")
        self.write("Linear.tex", "")
        self.write("li_paper.tex", "")
        self.assertEqual(self.result(rule, word).details["matches"][0]["channel"], "filename")
        # Terms that begin or end with punctuation keep that edge literal.
        self.write("Linear.tex", "mail jane@private.example now\n")
        email = SubmissionOptions(identity_terms=("@private.example",))
        self.assertEqual(self.result(rule, email).status, "failed")
        folded = SubmissionOptions(identity_terms=("Jürgen",))
        self.write("Linear.tex", "JÜRGEN\u0301x and Ju\u0308rgen\n")
        self.assertEqual(self.result(rule, folded).status, "failed")
        pdf_word = check_pdf_identity("Linear regression", {"Author": "Anonymous"}, word)[0]
        self.assertEqual(pdf_word.status, "passed")
        pdf_substring = check_pdf_identity("Linear regression", {"Author": "Anon"}, substring)[0]
        self.assertEqual(pdf_substring.status, "failed")
        self.assertEqual(
            check_pdf_identity("Contact Li", {"Author": "Anon"}, word)[0].status, "failed"
        )

    def test_identity_term_matching_is_validated_and_configurable(self) -> None:
        for value in ("regex", "", "WORD", None, True):
            with self.subTest(value=value), self.assertRaises(PreparationError):
                SubmissionOptions(identity_term_matching=value)  # type: ignore[arg-type]
        configured = read_options(
            SubmissionOptions,
            {"identity_terms": ["Li"], "identity_term_matching": "substring"},
            "submission_checks",
        )
        self.assertEqual(configured.identity_term_matching, "substring")
        with self.assertRaises(PreparationError):
            read_options(SubmissionOptions, {"identity_term_matching": 1}, "submission_checks")

    def test_failure_message_states_count_and_locations_without_identity_terms(self) -> None:
        rule = "submission.identity_terms"
        options = SubmissionOptions(identity_terms=("Jane Smith", "private.example"))
        self.write("a.tex", "Jane Smith\n\nprivate.example\n")
        self.write("b.txt", "x\nJane Smith and private.example\n")
        result = self.result(rule, options)
        # Two terms on one line are two matches at one location.
        self.assertEqual(result.message, "4 matches: a.tex:1, a.tex:3, b.txt:2")
        self.assertNotIn("Jane", result.message)
        self.assertNotIn("private", result.message)
        for index in range(5):
            self.write(f"c{index}.tex", "Jane Smith\n")
        result = self.result(rule, options)
        self.assertEqual(result.message, "9 matches: a.tex:1, a.tex:3, b.txt:2 (+5 more)")
        many = self.result("submission.filename_length", SubmissionOptions(filename_max_length=4))
        self.assertEqual(
            many.message,
            "File names longer than 4 characters (8): a.tex (5 characters), b.txt (5 characters), "
            "c0.tex (6 characters) (+5 more)",
        )
        self.assertEqual(len(many.details["matches"]), 8)
        self.write("x" * 240 + ".dat", "")
        long = self.result("submission.filename_length", SubmissionOptions(filename_max_length=200))
        self.assertEqual(
            long.message,
            "File names longer than 200 characters (1): " + "x" * 119 + "… (244 characters)",
        )
        self.assertEqual(long.details["matches"], [{"path": "x" * 240 + ".dat"}])

    def test_file_policy_and_deliverable_messages_name_the_offending_files(self) -> None:
        self.write("only-one.dat", "")
        one = self.result(
            "submission.file_extensions", SubmissionOptions(allowed_extensions=(".tex",))
        )
        self.assertEqual(
            one.message, "Files with extensions that are not allowed (1): only-one.dat (.dat)"
        )
        # Deliverable reasons stay visible without opening the JSON details.
        missing = self.result(
            "submission.deliverables",
            SubmissionOptions(required_deliverables=(("missing.pdf", "pdf"),)),
        )
        self.assertEqual(
            missing.message, "Required deliverables not satisfied (1): missing.pdf is missing"
        )
        self.write("a.tex", "Identity\n" * 5)
        capped = self.result(
            "submission.identity_terms",
            SubmissionOptions(identity_terms=("Identity",)),
            limits=ScanLimits(max_matches=2),
        )
        self.assertEqual(capped.message, "2+ matches: a.tex:1, a.tex:2")

    def test_file_policy_messages_give_the_limit_the_characters_and_the_extension(self) -> None:
        self.write("bad name.tex", "")
        self.write("notes", "")
        characters = self.result(
            "submission.filename_characters",
            SubmissionOptions(filename_allowed_characters="abcdefghijklmnopqrstuvwxyz."),
        )
        self.assertEqual(
            characters.message,
            "File names with characters outside the allowed set (1): "
            "bad name.tex (not allowed: ' ')",
        )
        extensions = self.result(
            "submission.file_extensions", SubmissionOptions(allowed_extensions=(".tex",))
        )
        self.assertEqual(
            extensions.message,
            "Files with extensions that are not allowed (1): notes (no extension)",
        )
        self.assertEqual(extensions.details["matches"], [{"path": "notes"}])

    def test_deliverable_messages_say_what_is_missing_or_wrong_in_plain_words(self) -> None:
        self.write("title.pdf", "Not a PDF")
        self.write("data.zip", b"PK\x05\x06" + bytes(18))
        self.write("empty.txt", "")
        result = self.result(
            "submission.deliverables",
            SubmissionOptions(
                required_deliverables=(
                    ("title.pdf", "pdf"),
                    ("data.zip", "zip"),
                    ("empty.txt", "text"),
                    ("cover.pdf", "pdf"),
                )
            ),
        )
        self.assertEqual(
            result.message,
            "Required deliverables not satisfied (3): title.pdf does not start like a PDF file, "
            "empty.txt is empty or is not UTF-8 text, cover.pdf is missing",
        )
        self.assertNotIn("match", result.message)

    def test_archive_size_message_states_the_size_and_the_limit(self) -> None:
        archive = self.write("project.zip", b"0" * 12345)
        result = check_submission_archive(archive, SubmissionOptions(max_archive_bytes=10000))[0]
        self.assertEqual(
            result.message,
            "The compressed archive is 12,345 bytes, over the configured limit of 10,000 bytes",
        )

    def test_unused_asset_message_names_the_files(self) -> None:
        self.write("figures/unused.png", b"image")
        result = self.result(
            "submission.unused_assets", SubmissionOptions(report_unused_assets=True)
        )
        self.assertEqual(
            result.message,
            "Asset files not referenced by the selected document (1): figures/unused.png",
        )

    def test_every_option_either_enables_the_bundle_scan_or_is_handled_elsewhere(self) -> None:
        enabling = {
            "identity_terms": ("Jane",),
            "scan_identity_hints": True,
            "scan_secrets": True,
            "scan_private_comments": True,
            "check_shell_escape": True,
            "filename_max_length": 1,
            "filename_allowed_characters": "a",
            "allowed_extensions": (".tex",),
            "required_deliverables": (("main.tex", "file"),),
            "report_unused_assets": True,
            "template_references": (("main.tex", "reference.tex"),),
        }
        elsewhere = {
            "identity_term_matching": "substring",
            "max_archive_bytes": 1,
        }
        # A new option has to be classified here, so it cannot silently run no check.
        self.assertEqual(
            {option.name for option in fields(SubmissionOptions)}, {*enabling, *elsewhere}
        )
        for name, value in enabling.items():
            with self.subTest(option=name):
                options = SubmissionOptions(**{name: value})  # type: ignore[arg-type]
                self.assertTrue(check_submission(self.root, "main.tex", options))
        for name, value in elsewhere.items():
            with self.subTest(option=name):
                options = SubmissionOptions(**{name: value})  # type: ignore[arg-type]
                self.assertEqual(check_submission(self.root, "main.tex", options), [])

    def test_inconclusive_message_names_the_first_issue_and_pdf_message_names_channels(
        self,
    ) -> None:
        options = SubmissionOptions(identity_terms=("Jane Smith",))
        incomplete = self.incomplete("submission.identity_terms", options)
        self.assertEqual(
            incomplete.message, "The configured scan is incomplete: Unreadable directory."
        )
        self.assertEqual(
            self.result("submission.identity_terms", options).message,
            "No matches in the stated scope.",
        )
        bad = check_pdf_identity("Jane Smith", {"Author": "JANE SMITH"}, options)[0]
        self.assertEqual(bad.message, "2 matches: text, metadata")
        empty = check_pdf_identity("", {}, options)[0]
        self.assertEqual(
            empty.message,
            "The configured scan is incomplete: "
            "Extracted PDF text is unavailable or empty. (+1 more)",
        )

    def test_failure_message_never_echoes_credentials_from_file_names(self) -> None:
        token = "ghp_" + "Ab12" * 10
        self.write(token + ".tex", "Jane Smith")
        results = check_submission(
            self.root,
            "main.tex",
            SubmissionOptions(identity_terms=("Jane Smith",), scan_secrets=True),
        )
        for item in results:
            self.assertNotIn(token, item.message)
            self.assertNotIn(token, json.dumps(asdict(item)))
        self.assertTrue(any("REDACTED" in item.message for item in results))

    def test_identity_hints(self) -> None:
        rule = "submission.identity_hints"
        options = SubmissionOptions(scan_identity_hints=True)
        self.write("main.tex", "% our university\n" + r"\verb|in our previous work|")
        self.assertEqual(self.result(rule, options).status, "passed")
        self.write("main.tex", r"\section*{Acknowledgements}Our institution supported this.")
        result = self.result(rule, options)
        self.assertEqual((result.code, result.status), ("PRV003", "failed"))
        self.assertEqual(result.evidence_kind, "heuristic")
        self.assertEqual(result.severity, "warning")
        self.assertEqual(self.incomplete(rule, options).status, "inconclusive")

    def test_self_citation_phrasing_counts_only_beside_a_citation(self) -> None:
        rule = "submission.identity_hints"
        options = SubmissionOptions(scan_identity_hints=True)
        flagged = (
            r"In our earlier paper \cite{key} we proved the bound.",
            "As we previously showed in this setting\n\\citep{key}, it holds.",
            r"Following \citet{key}, we earlier proposed a variant.",
            r"Our prior work \parencite{key} is related.",
            r"In our previous work \cite{key} we used Python.",
        )
        for text in flagged:
            with self.subTest(text=text):
                self.write("main.tex", text)
                result = self.result(rule, options)
                self.assertEqual((result.code, result.status), ("PRV003", "failed"))
                self.assertEqual(result.details["matches"][0]["line"], 1)
        ordinary = (
            # The phrases without a citation in the sentence, and a citation far away.
            r"We previously showed nothing. In our earlier work we used Python.",
            "Our prior work hours were long. " + "x" * 120 + r" \cite{key}.",
            r"The authors of \cite{key} previously showed a bound.",
            "In our previous work we used Python.",
            r"% In our earlier paper \cite{key}",
        )
        for text in ordinary:
            with self.subTest(text=text):
                self.write("main.tex", text)
                self.assertEqual(self.result(rule, options).status, "passed")

    def test_self_citation_patterns_scan_hostile_text_in_linear_time(self) -> None:
        options = SubmissionOptions(scan_identity_hints=True)
        size = HOSTILE_CHARACTERS
        for text in (
            "in our earlier work " * (size // 20),
            r"\cite{a} " * (size // 9),
            "we previously showed " + "x" * size,
        ):
            with self.subTest(text=text[:20]):
                self.write("main.tex", text)
                started = time.perf_counter()
                self.result("submission.identity_hints", options)
                self.assertLess(time.perf_counter() - started, LINEAR_TIME_LIMIT_SECONDS)

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
        self.assertEqual(result.evidence_kind, "heuristic")
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

    def test_private_comment_scan_shares_the_private_note_phrases_and_reads_tikz_comments(
        self,
    ) -> None:
        rule = "submission.private_comments"
        options = SubmissionOptions(scan_private_comments=True)
        for comment in (
            "note to self: fix",
            "Do not submit this figure",
            "not for distribution",
            "personal notes",
        ):
            with self.subTest(comment=comment):
                self.write("main.tex", f"% {comment}\n")
                self.assertEqual(self.result(rule, options).status, "failed")
        self.write("main.tex", "% a confidential computing enclave\n% an ordinary remark\n")
        self.assertEqual(self.result(rule, options).status, "passed")
        self.write("main.tex", "")
        self.write("figures/plot.tikz", "% TODO redraw\n")
        result = self.result(rule, options)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.details["matches"], [{"path": "figures/plot.tikz", "line": 1}])

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
        with (
            patch("latexprep.file_scan.read_regular_file", side_effect=OSError("unreadable")),
            patch("latexprep.file_scan.read_file_prefix", side_effect=OSError("unreadable")),
        ):
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
            with patch("latexprep.file_scan.read_regular_file", side_effect=OSError("unreadable")):
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
        with patch("latexprep.file_scan.MAX_SOURCE_BYTES", 4):
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
        result = self.result(
            "submission.identity_terms",
            SubmissionOptions(identity_terms=("Identity",)),
            limits=ScanLimits(max_matches=2),
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

    def test_more_matches_than_the_limit_are_reported_as_the_limit_with_a_plus(self) -> None:
        # File-name policies do not stop at the limit, so they can supply more matches.
        for name in ("a.dat", "b.dat", "c.dat"):
            self.write(name, "")
        result = self.result(
            "submission.file_extensions",
            SubmissionOptions(allowed_extensions=(".tex",)),
            limits=ScanLimits(max_matches=2),
        )
        # The message counts and names only the matches the details keep.
        self.assertEqual(
            result.message,
            "Files with extensions that are not allowed (2+): a.dat (.dat), b.dat (.dat)",
        )
        self.assertEqual([item["path"] for item in result.details["matches"]], ["a.dat", "b.dat"])
        self.assertTrue(result.details["match_output_limited"])
        self.assertTrue(result.incomplete)


def built_pdf(form_entries: bytes, info: bytes = b"") -> bytes:
    """Return a PDF that draws one included PDF through a form XObject, as pdfTeX writes it.

    Args:
        form_entries: Further entries of the form XObject, such as its ``/PTEX.FileName``.
        info: The entries of the information dictionary that the form's ``/PTEX.InfoDict``
            names; none when empty.

    Returns:
        The bytes of the PDF.
    """
    reference = b" /PTEX.InfoDict 7 0 R" if info else b""
    objects = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [] /Count 0 >>",
        3: b"<< /Producer (pdfTeX-1.40.29) /Author (Paper Author) >>",
        4: pdf_stream(
            b"/Type /XObject /Subtype /Form /BBox [0 0 10 10]" + reference + b" " + form_entries,
            b"0 0 10 10 re f",
        ),
        5: object_stream({7: b"<< " + info + b" >>"}),
    }
    return pdf_bytes(objects, b"<< /Root 1 0 R /Info 3 0 R >>")


class IncludedPdfInformationTests(ProjectTestCase):
    """What included PDFs left in a built PDF: identity terms and home folders (PRV105)."""

    def check(
        self, data: bytes, terms: tuple[str, ...] = (), role: BuiltPdfRole = "built"
    ) -> list[Finding]:
        """Write ``data`` as the built PDF and check it in ``role`` with ``terms`` as terms."""
        pdf = self.write("build/main.pdf", data)
        options = SubmissionOptions(identity_terms=terms)
        return check_included_pdf_information(pdf, options, role=role)

    def test_terms_and_home_folders_copied_from_included_pdfs(self) -> None:
        data = built_pdf(
            b"/PTEX.FileName (./figures/plot.pdf) /PTEX.PageNumber 1",
            b"/Author (Jane Q. Secret) /Title (/Users/jsecret/Private/fig.pdf)",
        )
        (finding,) = self.check(data, ("Jane Q. Secret",))
        self.assertEqual(
            (finding.code, finding.status, finding.severity), ("PRV105", "failed", "warning")
        )
        self.assertEqual(
            finding.details["matches"],
            [
                {"channel": "PTEX.InfoDict Author", "term_index": 1},
                {"channel": "PTEX.InfoDict Title", "kind": "macOS home folder"},
            ],
        )
        self.assertEqual(finding.details["included_objects"], 1)
        self.assertEqual(
            finding.details["channels"],
            ["PTEX.FileName", "PTEX.InfoDict Author", "PTEX.InfoDict Title"],
        )
        self.assertIn("PTEX.InfoDict Author (identity term 1)", finding.message)
        for value in ("Jane", "jsecret"):
            self.assertNotIn(value, str(finding))
        self.assertIn("\\pdfsuppressptexinfo=-1", finding.next_step or "")
        # The document's own information dictionary is the PDF identity scan's, not this one's.
        self.assertNotIn("Paper Author", str(finding))
        (paths_only,) = self.check(data)
        self.assertEqual(
            paths_only.details["matches"],
            [{"channel": "PTEX.InfoDict Title", "kind": "macOS home folder"}],
        )

    def test_clean_suppressed_and_unreadable_pdfs(self) -> None:
        (clean,) = self.check(built_pdf(b"/PTEX.FileName (./figures/plot.pdf)"), ("Jane",))
        self.assertEqual((clean.status, clean.severity), ("passed", "info"))
        self.assertIn("copied from 1 included PDF", clean.message)
        # A suppressed build keeps no included-PDF information, so there is nothing to report;
        # nor is there for bytes that are no PDF at all, which the PDF inspection reports.
        self.assertEqual(self.check(built_pdf(b"/Group << /S /Transparency >>")), [])
        self.assertEqual(self.check(b"%PDF-1.7\ncontrolled build result\n"), [])
        encrypted = pdf_bytes(
            {1: b"<< /Type /Catalog >>", 2: b"<< >>"}, b"<< /Root 1 0 R /Encrypt 2 0 R >>"
        )
        (refused,) = self.check(encrypted)
        self.assertEqual((refused.status, refused.severity), ("inconclusive", "info"))
        self.assertIn("encrypted", refused.message)
        # A missing or linked file is the PDF inspection's to report; one too large to read
        # leaves this check open.
        self.assertEqual(check_included_pdf_information(self.root / "absent.pdf"), [])
        with patch("latexprep.submission_checks._MAX_BUILT_PDF_BYTES", 10):
            (large,) = self.check(built_pdf(b"/PTEX.FileName (./figures/plot.pdf)"))
        self.assertEqual((large.status, large.severity), ("inconclusive", "info"))
        self.assertIn("larger than the scan reads", large.message)

    def test_prepare_notes_its_baseline_and_judges_the_pdf_it_publishes(self) -> None:
        leaking = built_pdf(b"/PTEX.FileName (./plot.pdf)", b"/Author (Jane Q. Secret)")
        (baseline,) = self.check(leaking, ("Jane Q. Secret",), role="baseline")
        # The baseline is built from the unchanged sources and never published: information.
        self.assertEqual((baseline.status, baseline.severity), ("failed", "info"))
        self.assertIn("unchanged sources", baseline.message)
        self.assertIsNone(baseline.suggestion)
        (published,) = self.check(leaking, ("Jane Q. Secret",), role="published")
        self.assertEqual((published.status, published.severity), ("failed", "warning"))
        # A published PDF without copied information records its clean result as a pass.
        (clean,) = self.check(built_pdf(b"/Group << /S /Transparency >>"), role="published")
        self.assertEqual((clean.status, clean.severity), ("passed", "info"))
        self.assertIn("published PDF carries no information", clean.message)
        self.assertEqual(
            self.check(built_pdf(b"/Group << /S /Transparency >>"), role="baseline"), []
        )


class IncludedPdfRoleWiringTests(AsyncProjectTestCase):
    """Which PDF of a job the included-PDF scan judges: check's build, or prepare's output."""

    async def roles(self, command: str) -> list[str]:
        """Run ``command`` on a controlled project and return the PDF role of each PDF check run.

        Args:
            command: ``check`` or ``prepare``.

        Returns:
            The ``built_pdf_role`` of every configured PDF check run, in run order.
        """
        seen: list[str] = []

        async def configured(_pdf, _root, _main, _work, _runner, settings, *_args, **_options):
            seen.append(settings.built_pdf_role)
            return []

        source = self.root / f"{command}-input"
        single_file_project(source)
        with patch(
            "latexprep.document_tasks.inspect_configured_pdf_checks", side_effect=configured
        ):
            await run_job(
                JobRequest(command, source, Settings(main="main.tex"), self.root / command),
                backends=controlled_backends(),
            )
        return seen

    async def test_prepare_notes_its_baseline_and_judges_the_published_pdf(self) -> None:
        self.assertEqual(await self.roles("check"), ["built"])
        self.assertEqual(await self.roles("prepare"), ["baseline", "published"])


if __name__ == "__main__":
    unittest.main()
