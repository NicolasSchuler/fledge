"""Default submission-readiness advisories and their opt-in policies on real source trees."""

from __future__ import annotations

import asyncio
import struct
import unittest
import zlib

from latexprep.bbl_checks import BblOptions, validate_bbl_format_version
from latexprep.config import load_settings
from latexprep.core import run_job
from latexprep.document_tasks import source_readiness_findings
from latexprep.errors import PreparationError
from latexprep.figure_color_space import JpegColor, jpeg_color_family, png_color_family
from latexprep.findings import Finding
from latexprep.job_context import JobRequest
from latexprep.manuscript_check_options import ManuscriptCheckOptions
from latexprep.manuscript_checks import check_manuscript_policies
from latexprep.submission_readiness_checks import check_submission_readiness
from tests.support import ProjectTestCase, controlled_backends, fake_build

PREAMBLE = "\\documentclass{article}\n"


def png(color_type: int) -> bytes:
    header = struct.pack(">IIBBBBB", 4, 4, 8, color_type, 0, 0, 0)
    chunk = b"IHDR" + header
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", len(header))
        + chunk
        + struct.pack(">I", zlib.crc32(chunk))
    )


def jpeg(components: int, *, adobe_transform: int | None = None) -> bytes:
    data = b"\xff\xd8"
    if adobe_transform is not None:
        payload = b"Adobe" + b"\x00\x64" + b"\x00\x00" + b"\x00\x00" + bytes([adobe_transform])
        data += b"\xff\xee" + struct.pack(">H", len(payload) + 2) + payload
    # An unrelated application segment before the frame header must be skipped.
    comment = b"Exif\x00\x00" + b"\x00" * 20
    data += b"\xff\xe1" + struct.pack(">H", len(comment) + 2) + comment
    frame = bytes([8]) + struct.pack(">HH", 4, 4) + bytes([components])
    frame += b"".join(bytes([index + 1, 0x11, 0]) for index in range(components))
    return data + b"\xff\xc0" + struct.pack(">H", len(frame) + 2) + frame + b"\xff\xd9"


class ReadinessCase(ProjectTestCase):
    def document(self, preamble: str = "", body: str = "Text.") -> None:
        self.write(
            "main.tex", PREAMBLE + preamble + "\\begin{document}\n" + body + "\n\\end{document}\n"
        )

    def findings(
        self,
        rule: str,
        main: str | None = "main.tex",
        required_color_space: str | None = None,
        **options: object,
    ) -> list[Finding]:
        readiness = check_submission_readiness(
            self.root,
            main,
            BblOptions(**options),  # type: ignore[arg-type]
            required_color_space=required_color_space,
        )
        return [item for item in readiness if item.rule == rule]

    def one(self, rule: str, **options: object) -> Finding:
        findings = self.findings(rule, **options)
        self.assertEqual(len(findings), 1, findings)
        return findings[0]


class BibliographyBblTests(ReadinessCase):
    def test_bib_without_bbl_is_a_note_and_bbl_passes(self) -> None:
        self.write("refs.bib", "@book{knuth, title={The TeXbook}, year={1984}}")
        self.document(body="Text \\cite{knuth}.\n\\bibliographystyle{plain}\n\\bibliography{refs}")
        missing = self.one("source.bibliography_bbl")
        # Most services run BibTeX themselves: a scope note listed only on request.
        self.assertEqual(
            (missing.code, missing.severity, missing.status), ("TEX009", "info", "not_applicable")
        )
        self.assertTrue(missing.is_scope_disclaimer)
        self.assertEqual((missing.path, missing.line), ("main.tex", 5))
        self.assertIn("main.bbl is not in the package", missing.message)
        self.assertIn("submission_checks.require_bbl", missing.message)
        self.assertEqual(missing.details["backend"], "bibtex")
        self.assertFalse(missing.details["require_bbl"])
        self.write("old/paper.bbl", "\\begin{thebibliography}{1}\\end{thebibliography}")
        self.assertIn("old/paper.bbl", self.one("source.bibliography_bbl").message)
        self.write("main.bbl", "\\begin{thebibliography}{1}\\end{thebibliography}")
        shipped = self.one("source.bibliography_bbl")
        self.assertEqual((shipped.severity, shipped.status), ("info", "passed"))
        (self.root / "main.bbl").unlink()
        self.document("\\usepackage{biblatex}\n\\addbibresource{refs.bib}\n", "\\printbibliography")
        self.assertEqual(self.one("source.bibliography_bbl").details["backend"], "biblatex")

    def test_require_bbl_makes_a_missing_bbl_a_warning(self) -> None:
        self.write("refs.bib", "@book{knuth, title={The TeXbook}, year={1984}}")
        self.document(body="Text \\cite{knuth}.\n\\bibliography{refs}")
        required = self.one("source.bibliography_bbl", require_bbl=True)
        self.assertEqual(
            (required.code, required.severity, required.status), ("TEX009", "warning", "failed")
        )
        self.assertIn("would render empty citations", required.message)
        self.assertTrue(required.details["require_bbl"])
        self.write("main.bbl", "\\begin{thebibliography}{1}\\end{thebibliography}")
        shipped = self.one("source.bibliography_bbl", require_bbl=True)
        self.assertEqual((shipped.severity, shipped.status), ("info", "passed"))
        with self.assertRaisesRegex(PreparationError, "require_bbl must be a boolean"):
            BblOptions(require_bbl="yes")  # type: ignore[arg-type]

    def test_require_bbl_is_read_from_project_settings_and_reaches_the_check(self) -> None:
        self.write("refs.bib", "@book{knuth, title={The TeXbook}, year={1984}}")
        self.document(body="Text \\cite{knuth}.\n\\bibliography{refs}")
        config = self.root / "fledge.toml"
        config.write_text("[submission_checks]\nrequire_bbl = true\n")
        for settings, severity in (
            (load_settings(None, {}), "info"),
            (load_settings(config, {}), "warning"),
        ):
            with self.subTest(severity=severity):
                findings = [
                    item
                    for item in source_readiness_findings(self.root, "main.tex", settings)
                    if item.rule == "source.bibliography_bbl"
                ]
                self.assertEqual([item.severity for item in findings], [severity])
        config.write_text("[submission_checks]\nrequire_bbl = 'yes'\n")
        with self.assertRaisesRegex(PreparationError, "require_bbl must be a boolean"):
            load_settings(config, {})

    def test_no_bib_resource_or_no_main_reports_nothing(self) -> None:
        self.document(body="\\begin{thebibliography}{1}\\bibitem{a} A.\\end{thebibliography}")
        self.assertEqual(self.findings("source.bibliography_bbl"), [])
        self.write("refs.bib", "@book{knuth, title={T}}")
        self.write("other.tex", PREAMBLE + "\\begin{document}\\bibliography{refs}\\end{document}")
        # Two document roots and no selected main: no graph, so no bibliography claim.
        self.assertEqual(self.findings("source.bibliography_bbl", main=None), [])
        self.assertEqual(
            self.one("bibliography.bbl_coverage", check_bbl_coverage=True).status,
            "not_applicable",
        )
        unchecked = self.one("bibliography.bbl_format_version", biblatex_bbl_format_version="3.3")
        self.assertEqual((unchecked.code, unchecked.status), ("BIB110", "not_applicable"))
        self.assertIn("no literal .bib resource", unchecked.message)

    def test_bbl_coverage_reports_missing_keys_and_parses_both_formats(self) -> None:
        self.write("refs.bib", "@book{knuth, title={T}}\n@book{lamport, title={L}}")
        self.document(body="\\cite{knuth} and \\citep{lamport}.\n\\bibliography{refs}")
        unshipped = self.one("bibliography.bbl_coverage", check_bbl_coverage=True)
        # The missing file is TEX009's finding; coverage only defers to it, without blocking.
        self.assertEqual(
            (unshipped.code, unshipped.severity, unshipped.status),
            ("BIB109", "warning", "inconclusive"),
        )
        self.assertEqual(unshipped.details["blocked_by"], "TEX009")
        self.assertIn("main.bbl", unshipped.suggestion or "")
        self.write(
            "main.bbl",
            "\\begin{thebibliography}{1}\n\\bibitem[{Knuth [1]}(1984)]{knuth}\nD. Knuth.\n"
            "\\end{thebibliography}\n",
        )
        failed = self.one("bibliography.bbl_coverage", check_bbl_coverage=True)
        self.assertEqual((failed.severity, failed.status), ("error", "failed"))
        self.assertEqual([item["key"] for item in failed.details["missing"]], ["lamport"])
        self.assertIn("lamport", failed.message)
        self.assertIn("main.tex:3", failed.message)
        self.write(
            "main.bbl",
            "\\datalist[entry]{nty/global//global/global}\n"
            "\\entry{knuth}{book}{}\n\\endentry\n\\keyalias{lamport}{lamport94}\n",
        )
        passed = self.one("bibliography.bbl_coverage", check_bbl_coverage=True)
        self.assertEqual((passed.status, passed.details["bbl_format"]), ("passed", "biblatex"))
        self.assertEqual(self.findings("bibliography.bbl_coverage"), [])

    def test_bbl_format_version_matches_the_configured_target(self) -> None:
        rule = "bibliography.bbl_format_version"
        self.write("refs.bib", "@book{knuth, title={T}}")
        self.document("\\usepackage{biblatex}\n\\addbibresource{refs.bib}\n", "\\cite{knuth}")
        self.assertEqual(self.findings(rule), [])
        unshipped = self.one(rule, biblatex_bbl_format_version="3.3")
        # The missing file is TEX009's finding; the format check only defers to it.
        self.assertEqual(
            (unshipped.code, unshipped.severity, unshipped.status),
            ("BIB110", "warning", "inconclusive"),
        )
        self.assertEqual(unshipped.details["blocked_by"], "TEX009")
        header = "% $ biblatex auxiliary file $\n% $ biblatex bbl format version {} $\n"
        self.write("main.bbl", header.format("3.3") + "% Do not modify the above lines!\n")
        passed = self.one(rule, biblatex_bbl_format_version="3.3")
        self.assertEqual((passed.severity, passed.status, passed.line), ("info", "passed", 2))
        mismatch = self.one(rule, biblatex_bbl_format_version="3.2")
        self.assertEqual((mismatch.severity, mismatch.status), ("error", "failed"))
        self.assertEqual(mismatch.details["found_version"], "3.3")
        self.assertIn("format 3.3, but the target service expects 3.2", mismatch.message)
        self.write("main.bbl", "% $ biblatex auxiliary file $\n% edited by hand\n")
        unreadable = self.one(rule, biblatex_bbl_format_version="3.3")
        self.assertEqual((unreadable.severity, unreadable.status), ("warning", "inconclusive"))
        self.write("main.bbl", "\\begin{thebibliography}{1}\\end{thebibliography}\n")
        bibtex_file = self.one(rule, biblatex_bbl_format_version="3.3")
        self.assertEqual((bibtex_file.severity, bibtex_file.status), ("info", "not_applicable"))
        (self.root / "main.bbl").unlink()
        self.document(body="\\cite{knuth}\n\\bibliography{refs}")
        bibtex_document = self.one(rule, biblatex_bbl_format_version="3.3")
        self.assertEqual(bibtex_document.status, "not_applicable")
        self.assertIn("uses BibTeX", bibtex_document.message)

    def test_bbl_format_version_setting_is_validated_and_reaches_the_check(self) -> None:
        self.write("refs.bib", "@book{knuth, title={T}}")
        self.document("\\usepackage{biblatex}\n\\addbibresource{refs.bib}\n", "\\cite{knuth}")
        self.write(
            "main.bbl",
            "% $ biblatex auxiliary file $\n% $ biblatex bbl format version 3.1 $\n",
        )
        config = self.root / "fledge.toml"
        config.write_text('[submission_checks]\nbiblatex_bbl_format_version = "3.3"\n')
        findings = [
            item
            for item in source_readiness_findings(self.root, "main.tex", load_settings(config, {}))
            if item.rule == "bibliography.bbl_format_version"
        ]
        self.assertEqual([(item.code, item.status) for item in findings], [("BIB110", "failed")])
        for value in ("3.3 beta", "", "3.3$"):
            with self.subTest(value=value):
                config.write_text(f'[submission_checks]\nbiblatex_bbl_format_version = "{value}"\n')
                with self.assertRaisesRegex(PreparationError, "biblatex_bbl_format_version"):
                    load_settings(config, {})
        with self.assertRaisesRegex(PreparationError, "biblatex_bbl_format_version"):
            BblOptions(biblatex_bbl_format_version=3.3)  # type: ignore[arg-type]

    def test_the_format_version_is_validated_where_the_bbl_options_live(self) -> None:
        for version in (None, "3", "3.3", "3.3.1.2"):
            with self.subTest(version=version):
                validate_bbl_format_version(version)
        for version in ("", " ", "3.3 ", "v3.3", "3.3.1.2.5", "33333.1", 3.3):
            with (
                self.subTest(version=version),
                self.assertRaisesRegex(
                    PreparationError, 'biblatex_bbl_format_version must be a version such as "3.3"'
                ),
            ):
                validate_bbl_format_version(version)

    def test_the_bbl_options_share_the_submission_checks_table_without_changing_its_keys(
        self,
    ) -> None:
        config = self.root / "fledge.toml"
        config.write_text(
            "[submission_checks]\n"
            "scan_secrets = true\n"
            "check_bbl_coverage = true\n"
            "require_bbl = true\n"
            'biblatex_bbl_format_version = "3.3"\n'
        )
        settings = load_settings(config, {})
        self.assertTrue(settings.submission_checks.scan_secrets)
        self.assertEqual(
            settings.bbl_checks,
            BblOptions(
                check_bbl_coverage=True, require_bbl=True, biblatex_bbl_format_version="3.3"
            ),
        )
        recorded = settings.to_dict()["submission_checks"]
        assert isinstance(recorded, dict)
        self.assertTrue(recorded["scan_secrets"])
        self.assertEqual(recorded["biblatex_bbl_format_version"], "3.3")
        self.assertNotIn("bbl_checks", settings.to_dict())
        config.write_text("[submission_checks]\ncheck_bbl_coverage_typo = true\n")
        with self.assertRaisesRegex(
            PreparationError,
            r"Unknown submission_checks setting\(s\): check_bbl_coverage_typo "
            r"\(did you mean check_bbl_coverage\?\)",
        ):
            load_settings(config, {})

    def test_arxiv_preset_without_a_bbl_is_an_advisory_not_a_block(self) -> None:
        self.write("refs.bib", "@book{knuth, title={The TeXbook}, year={1984}}")
        self.document(body="Text \\cite{knuth}.\n\\bibliography{refs}")
        settings = load_settings(None, {}, preset="arxiv")
        backends = controlled_backends(build=fake_build)
        report = asyncio.run(run_job(JobRequest("check", self.root, settings), backends=backends))
        bbl = {item.code: item for item in report.findings if item.code in {"TEX009", "BIB109"}}
        self.assertEqual((bbl["TEX009"].severity, bbl["TEX009"].status), ("warning", "failed"))
        self.assertEqual(
            (bbl["BIB109"].severity, bbl["BIB109"].status), ("warning", "inconclusive")
        )
        self.assertEqual(report.outcome, "passed_with_advisories", report.findings)

    def test_bbl_coverage_is_inconclusive_for_dynamic_citations(self) -> None:
        self.write("refs.bib", "@book{knuth, title={T}}")
        self.write("main.bbl", "\\bibitem{knuth} D. Knuth.")
        self.document(
            "\\newcommand{\\mykey}{knuth}\n", "\\cite{knuth} \\cite{\\mykey}\n\\bibliography{refs}"
        )
        finding = self.one("bibliography.bbl_coverage", check_bbl_coverage=True)
        self.assertEqual((finding.severity, finding.status), ("error", "inconclusive"))
        self.assertTrue(finding.details["uncertainty"])


class HyperrefOrderTests(ReadinessCase):
    def test_packages_before_hyperref_are_reported(self) -> None:
        self.write("preamble.tex", "\\usepackage{amsmath,cleveref}\n")
        self.document("\\input{preamble}\n\\usepackage{bookmark}\n\\usepackage{hyperref}\n")
        findings = self.findings("source.hyperref_load_order")
        self.assertEqual(
            [(item.code, item.status, item.severity) for item in findings],
            [("TEX010", "failed", "warning")] * 2,
        )
        self.assertEqual(
            [(item.details["package"], item.path, item.line) for item in findings],
            [("cleveref", "preamble.tex", 1), ("bookmark", "main.tex", 3)],
        )
        self.assertIn("before hyperref (main.tex:4)", findings[0].message)

    def test_correct_order_conditionals_and_missing_hyperref_are_not_reported(self) -> None:
        self.document("\\usepackage{hyperref}\n\\usepackage[capitalise]{cleveref}\n")
        self.assertEqual(self.one("source.hyperref_load_order").status, "passed")
        for preamble in (
            "\\usepackage{cleveref}\n",
            "% \\usepackage{cleveref}\n\\usepackage{hyperref}\n",
            "\\ifdefined\\final\\usepackage{cleveref}\\fi\n\\usepackage{hyperref}\n",
            "\\newcommand{\\later}{\\usepackage{cleveref}}\n\\usepackage{hyperref}\n",
            "\\usepackage{nameref}\n\\usepackage{hyperref}\n",
        ):
            with self.subTest(preamble=preamble):
                self.document(preamble)
                self.assertEqual(self.findings("source.hyperref_load_order"), [])


class FigureTests(ReadinessCase):
    def test_eps_figures_are_reported_as_information(self) -> None:
        self.write("figs/plot.eps", "%!PS-Adobe-3.0 EPSF-3.0\n")
        self.write("photo.pdf", b"%PDF-1.5\n")
        self.document(
            "\\usepackage{graphicx}\n", "\\includegraphics{figs/plot}\n\\includegraphics{photo.pdf}"
        )
        finding = self.one("source.eps_figures")
        self.assertEqual(
            (finding.code, finding.severity, finding.status), ("TEX011", "info", "failed")
        )
        self.assertEqual(
            (finding.path, finding.details["figures"]), ("figs/plot.eps", ["figs/plot.eps"])
        )
        self.assertIn("shell escape", finding.message)
        self.document("\\usepackage{graphicx}\n", "\\includegraphics{photo.pdf}")
        self.assertEqual(self.findings("source.eps_figures"), [])

    def test_jpeg_walker_reads_the_frame_and_stops_quietly_at_damage(self) -> None:
        self.assertEqual(jpeg_color_family(jpeg(3)), JpegColor("rgb", None))
        self.assertEqual(jpeg_color_family(jpeg(4, adobe_transform=2)), JpegColor("cmyk", 2))
        self.assertEqual(jpeg_color_family(jpeg(1)), JpegColor("gray", None))
        # Restart markers and fill bytes before the frame header carry no length.
        padded = b"\xff\xd8\xff\xd0\xff\xff" + jpeg(3)[2:]
        self.assertEqual(jpeg_color_family(padded), JpegColor("rgb", None))
        # Not a JPEG, a truncated segment, or a stream that ends before any frame header.
        for damaged in (b"", b"GIF89a", jpeg(3)[:12], b"\xff\xd8\xff\xd9"):
            with self.subTest(damaged=damaged):
                self.assertEqual(jpeg_color_family(damaged).family, None)
        self.assertEqual(png_color_family(png(6)), "rgb")
        self.assertEqual(png_color_family(png(4)), "gray")
        self.assertIsNone(png_color_family(png(1)))
        self.assertIsNone(png_color_family(b"\x89PNG"))

    def test_vector_figures_are_not_applicable_to_the_raster_color_space_policy(self) -> None:
        self.write("plot.eps", "%!PS\n")
        self.write("chart.pdf", b"%PDF-1.5\n")
        self.write("photo.png", png(2))
        self.document(
            "\\usepackage{graphicx}\n",
            "\\includegraphics{plot.eps}\\includegraphics{chart.pdf}",
        )
        only_vector = self.one("figure.color_space", required_color_space="rgb")
        self.assertEqual((only_vector.severity, only_vector.status), ("info", "passed"))
        self.assertEqual(
            only_vector.details["not_applicable_vector_figures"], ["chart.pdf", "plot.eps"]
        )
        self.assertNotIn("raster figure(s)", only_vector.message)
        self.document(
            "\\usepackage{graphicx}\n",
            "\\includegraphics{plot.eps}\\includegraphics{photo.png}",
        )
        mixed = self.one("figure.color_space", required_color_space="cmyk")
        self.assertEqual((mixed.severity, mixed.status), ("error", "failed"))
        self.assertEqual(
            mixed.message, "Raster figures that are not CMYK or grayscale (1): photo.png."
        )

    def test_raster_color_space_policy_parses_png_and_jpeg_headers(self) -> None:
        self.write("rgb.png", png(2))
        self.write("gray.jpg", jpeg(1))
        self.write("cmyk.jpg", jpeg(4, adobe_transform=2))
        self.write("vector.pdf", b"%PDF-1.5\n")
        self.document(
            "\\usepackage{graphicx}\n",
            "\\includegraphics{rgb.png}\\includegraphics{gray.jpg}"
            "\\includegraphics{cmyk.jpg}\\includegraphics{vector.pdf}",
        )
        self.assertEqual(self.findings("figure.color_space"), [])
        rgb = self.one("figure.color_space", required_color_space="rgb")
        self.assertEqual((rgb.code, rgb.severity, rgb.status), ("PDF313", "error", "failed"))
        self.assertEqual(
            rgb.details["violations"],
            [{"path": "cmyk.jpg", "color_space": "cmyk", "adobe_transform": 2}],
        )
        cmyk = self.one("figure.color_space", required_color_space="cmyk")
        self.assertEqual([item["path"] for item in cmyk.details["violations"]], ["rgb.png"])
        self.document("\\usepackage{graphicx}\n", "\\includegraphics{gray.jpg}")
        self.assertEqual(
            self.one("figure.color_space", required_color_space="cmyk").status, "passed"
        )
        # A truncated header or an unparsed raster format is never a pass.
        self.write("broken.jpg", jpeg(3)[:12])
        self.write("scan.tif", b"II*\x00")
        for name in ("broken.jpg", "scan.tif"):
            with self.subTest(name=name):
                self.document("\\usepackage{graphicx}\n", f"\\includegraphics{{{name}}}")
                result = self.one("figure.color_space", required_color_space="rgb")
                self.assertEqual((result.severity, result.status), ("error", "inconclusive"))
        self.assertIn("scan.tif", result.message)
        self.assertIn("cannot be read here", result.message)
        with self.assertRaises(PreparationError):
            check_submission_readiness(self.root, "main.tex", required_color_space="srgb")


class PrivateMarkerTests(ReadinessCase):
    def test_private_markers_report_locations_and_categories_only(self) -> None:
        self.document(
            "\\author{Nina Schmidt}",
            body="% NS: rephrase before the deadline\n"
            "% Do not submit: Secret Project Falcon\n"
            "% Added because Reviewer #2 asked\n"
            "% note to self: check with Bob\n"
            "\\begin{verbatim}\n% do not distribute\n\\end{verbatim}\n"
            "Ordinary text.",
        )
        self.write("style.sty", "% Do not distribute modified copies of this file.\n")
        finding = self.one("submission.private_comment_markers")
        self.assertEqual(
            (finding.code, finding.severity, finding.status, finding.evidence_kind),
            ("PRV007", "warning", "failed", "heuristic"),
        )
        self.assertEqual(
            [(item["line"], item["reason"]) for item in finding.details["matches"]],
            [
                (3, "author-initials note"),
                (4, "distribution restriction"),
                (5, "review correspondence"),
                (6, "private note"),
            ],
        )
        report = finding.message + repr(finding.details)
        for private in ("Falcon", "Bob", "rephrase"):
            self.assertNotIn(private, report)

    def test_ordinary_template_and_technical_comments_are_not_reported(self) -> None:
        self.document(
            body="% Do not change the following lines.\n"
            "% Confidential computing protects data in use.\n"
            "% SQL: SELECT * FROM papers\n"
            "% TODO: polish (reported by TEX001, not here)\n"
            "% Section 2: method overview\n"
            "Reviewer 2 asked for this experiment, as reported in the text."
        )
        finding = self.one("submission.private_comment_markers")
        self.assertEqual((finding.severity, finding.status), ("info", "passed"))
        self.write("legacy.tex", b"% caf\xe9 is Latin-1\n")
        incomplete = self.one("submission.private_comment_markers")
        self.assertEqual((incomplete.severity, incomplete.status), ("info", "inconclusive"))

    def test_acronyms_are_not_author_initials(self) -> None:
        notes = "% MLP: first layer\n% KL: divergence term\n% RNN: recurrent cell\n"
        self.document("\\author{Nina Schmidt}", body=notes + "% NS: ask Bob\n")
        finding = self.one("submission.private_comment_markers")
        self.assertEqual(
            [(item["line"], item["reason"]) for item in finding.details["matches"]],
            [(6, "author-initials note")],
        )

    def test_initials_need_a_literal_author_name_to_match(self) -> None:
        self.document(body="% NS: rephrase\n% MLP: first layer\n")
        self.assertEqual(self.one("submission.private_comment_markers").status, "passed")
        self.document("\\author{\\authorname}", body="% NS: rephrase\n")
        self.assertEqual(self.one("submission.private_comment_markers").status, "passed")
        for author, note, flagged in (
            ("\\author{Ada Lovelace \\and Grace Hopper}", "GH", True),
            ("\\author[1]{Ada Byron King}", "AK", True),
            ("\\author{Lovelace, Ada}", "AL", True),
            ("\\author{\\'Eva Nagy}", "EN", True),
            ("\\author{Ada Lovelace}", "GH", False),
        ):
            with self.subTest(author=author, note=note):
                self.document(author, body=f"% {note}: check this\n")
                status = self.one("submission.private_comment_markers").status
                self.assertEqual(status, "failed" if flagged else "passed")


class LineNumberTests(ReadinessCase):
    def check(self, **options: bool) -> Finding:
        findings = [
            item
            for item in check_manuscript_policies(
                self.root, "main.tex", ManuscriptCheckOptions(**options)
            )
            if item.rule == "manuscript.line_numbers"
        ]
        self.assertEqual(len(findings), 1, findings)
        return findings[0]

    def test_required_line_numbers(self) -> None:
        self.document("\\usepackage{lineno}\n")
        loaded_only = self.check(require_line_numbers=True)
        self.assertEqual(
            (loaded_only.code, loaded_only.severity, loaded_only.status),
            ("MAN017", "error", "failed"),
        )
        self.assertEqual(loaded_only.details["lineno_loaded"][0]["line"], 2)
        for preamble in (
            "\\usepackage{lineno}\n\\linenumbers\n",
            "\\usepackage[switch,linenumbers]{lineno}\n",
            "\\usepackage{lineno}\n\\begin{document}\\begin{runninglinenumbers}x"
            "\\end{runninglinenumbers}\\end{document}\n",
        ):
            with self.subTest(preamble=preamble):
                if preamble.endswith("\\end{document}\n"):
                    self.write("main.tex", PREAMBLE + preamble)
                else:
                    self.document(preamble)
                self.assertEqual(self.check(require_line_numbers=True).status, "passed")
        self.write("main.tex", "\\documentclass[lineno]{article}\\begin{document}x\\end{document}")
        self.assertEqual(self.check(require_line_numbers=True).status, "passed")
        self.document("\\usepackage{lineno}\n\\ifdefined\\review\\linenumbers\\fi\n")
        self.assertEqual(self.check(require_line_numbers=True).status, "inconclusive")
        self.assertEqual(check_manuscript_policies(self.root, "main.tex"), [])

    def test_forbidden_line_numbers(self) -> None:
        self.document("\\usepackage{lineno}\n", "Text.\n\\linenumbers")
        forbidden = self.check(forbid_line_numbers=True)
        self.assertEqual((forbidden.severity, forbidden.status), ("error", "failed"))
        self.assertEqual((forbidden.path, forbidden.line), ("main.tex", 5))
        self.document("\\usepackage{lineno}\n")
        self.assertEqual(self.check(forbid_line_numbers=True).status, "passed")
        self.document("\\usepackage{lineno}\n\\iffalse\\linenumbers\\fi\n")
        self.assertEqual(self.check(forbid_line_numbers=True).status, "inconclusive")
        with self.assertRaises(PreparationError):
            ManuscriptCheckOptions(require_line_numbers=True, forbid_line_numbers=True)


if __name__ == "__main__":
    unittest.main()
