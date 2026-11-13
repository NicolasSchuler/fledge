"""Default submission-readiness advisories and their opt-in policies on real source trees."""

from __future__ import annotations

import asyncio
import struct
import unittest
import zlib

from latexprep.bbl_checks import BblOptions, validate_bbl_format_version
from latexprep.build_dependencies import SubmissionDependencies
from latexprep.config import Settings, load_settings
from latexprep.core import run_job
from latexprep.document_tasks import source_readiness_findings
from latexprep.errors import PreparationError
from latexprep.figure_color_space import JpegColor, jpeg_color_family, png_color_family
from latexprep.findings import Finding
from latexprep.generated_file_checks import never_created_findings
from latexprep.job_context import JobRequest
from latexprep.manuscript_check_options import ManuscriptCheckOptions
from latexprep.manuscript_checks import check_manuscript_policies
from latexprep.setting_choices import Engine
from latexprep.submission_readiness_checks import check_submission_readiness
from tests.support import ProjectTestCase, controlled_backends, fake_build

PREAMBLE = "\\documentclass{article}\n"


def png(color_type: int) -> bytes:
    """Return the signature and ``IHDR`` chunk of a 4 by 4, 8-bit PNG of ``color_type``.

    The image ends after its header chunk.
    """
    header = struct.pack(">IIBBBBB", 4, 4, 8, color_type, 0, 0, 0)
    chunk = b"IHDR" + header
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", len(header))
        + chunk
        + struct.pack(">I", zlib.crc32(chunk))
    )


def jpeg(components: int, *, adobe_transform: int | None = None) -> bytes:
    """Return a 4 by 4, 8-bit baseline JPEG with ``components`` colour components.

    An Exif application segment precedes the frame header, so a reader must skip it; there is no
    image data between the frame header and the end marker.

    Args:
        components: The number of components in the frame header.
        adobe_transform: The transform flag of an Adobe segment placed first, or ``None`` for no
            Adobe segment.

    Returns:
        The JPEG bytes.
    """
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
    """Base of the readiness tests, which writes documents and runs the readiness checks."""

    def document(self, preamble: str = "", body: str = "Text.") -> None:
        """Write ``main.tex`` as an article with ``preamble`` and a document body of ``body``.

        Args:
            preamble: Text placed after the document class line.
            body: The text of the document environment.
        """
        self.write(
            "main.tex", PREAMBLE + preamble + "\\begin{document}\n" + body + "\n\\end{document}\n"
        )

    def findings(
        self,
        rule: str,
        main: str | None = "main.tex",
        required_color_space: str | None = None,
        engine: Engine | None = None,
        **options: object,
    ) -> list[Finding]:
        """Run the submission-readiness checks and return the findings of ``rule``.

        Args:
            rule: The rule whose findings to return.
            main: The main file, or ``None`` to select the single document root.
            required_color_space: The colour space raster figures must use, or ``None`` for no
                policy.
            engine: The engine the document builds with, or ``None`` when not known.
            **options: The fields of the :class:`BblOptions` to use.

        Returns:
            The findings of ``rule``, in the order the checks report them.
        """
        readiness = check_submission_readiness(
            self.root,
            main,
            BblOptions(**options),  # type: ignore[arg-type]
            required_color_space=required_color_space,
            engine=engine,
        )
        return [item for item in readiness if item.rule == rule]

    def one(self, rule: str, **options: object) -> Finding:
        """Return the only finding of ``rule``; the test fails unless there is exactly one.

        Args:
            rule: The rule whose finding to return.
            **options: Passed on to :meth:`findings`.

        Returns:
            The finding.
        """
        findings = self.findings(rule, **options)
        self.assertEqual(len(findings), 1, findings)
        return findings[0]


class BibliographyBblTests(ReadinessCase):
    """The ``.bbl`` advisories: a missing ``.bbl``, its coverage of the citations and its format.

    The ``require_bbl`` and format version options are read from the project settings, validated
    where the ``.bbl`` options live, and share the submission checks table.
    """

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
        self.assertIn("submission_checks.venue_runs_bibliography_tools = false", missing.message)
        self.assertIn("arXiv runs them when no .bbl is uploaded", missing.message)
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
        self.assertIn("renders empty citations", required.message)
        self.assertTrue(required.details["require_bbl"])
        # A venue that runs neither BibTeX nor Biber needs the .bbl just as much.
        venue = self.one("source.bibliography_bbl", venue_runs_bibliography_tools=False)
        self.assertEqual((venue.severity, venue.status), ("warning", "failed"))
        self.assertFalse(venue.details["venue_runs_bibliography_tools"])
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

    def test_arxiv_preset_needs_no_bbl_because_arxiv_runs_bibtex(self) -> None:
        """Record a missing .bbl only as a note: arXiv runs BibTeX when no .bbl is uploaded."""
        self.write("refs.bib", "@book{knuth, title={The TeXbook}, year={1984}}")
        self.document(body="Text \\cite{knuth}.\n\\bibliography{refs}")
        settings = load_settings(None, {}, preset="arxiv")
        backends = controlled_backends(build=fake_build)
        report = asyncio.run(run_job(JobRequest("check", self.root, settings), backends=backends))
        bbl = {item.code: item for item in report.findings if item.code in {"TEX009", "BIB109"}}
        self.assertEqual((bbl["TEX009"].severity, bbl["TEX009"].status), ("info", "not_applicable"))
        self.assertNotIn("BIB109", bbl)
        self.assertNotIn(report.outcome, {"blocked", "error"}, report.findings)

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
    """The advisory about packages loaded before hyperref."""

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
    """EPS figures, and the raster colour space policy read from PNG and JPEG headers."""

    def test_eps_figures_block_unless_their_converted_pdf_is_shipped(self) -> None:
        """Block an EPS figure the isolated build cannot convert, for every engine.

        TeX Live 2023 prints the file name instead of the figure without an error, so the
        source check must block. A shipped epstopdf output only warns, and a PDF that the
        extension search finds first replaces the EPS figure altogether.
        """
        self.write("figs/plot.eps", "%!PS-Adobe-3.0 EPSF-3.0\n")
        self.write("photo.pdf", b"%PDF-1.5\n")
        self.document(
            "\\usepackage{graphicx}\n", "\\includegraphics{figs/plot}\n\\includegraphics{photo.pdf}"
        )
        finding = self.one("source.eps_figures")
        self.assertEqual(
            (finding.code, finding.severity, finding.status), ("TEX011", "error", "failed")
        )
        self.assertEqual(
            (finding.path, finding.details["unconverted"]), ("figs/plot.eps", ["figs/plot.eps"])
        )
        self.assertIn("shell escape", finding.message)
        self.assertIn("Ghostscript", finding.message)
        self.assertIn("TeX Live 2023", finding.message)
        self.assertIn("\\includegraphics{figs/plot} finds figs/plot.pdf", finding.suggestion)
        # The epstopdf output beside the figure: TeX Live 2024 and later use it.
        self.write("figs/plot-eps-converted-to.pdf", b"%PDF-1.5\n")
        converted = self.one("source.eps_figures")
        self.assertEqual((converted.severity, converted.status), ("warning", "failed"))
        self.assertEqual(converted.details["converted"], ["figs/plot-eps-converted-to.pdf"])
        # A PDF named like the figure is found first, so no EPS figure is included.
        self.write("figs/plot.pdf", b"%PDF-1.5\n")
        self.assertEqual(self.findings("source.eps_figures"), [])
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


class GeneratedListTests(ReadinessCase):
    """The generated index, glossary and nomenclature files a document prints (TEX012)."""

    LISTS = (
        "\\usepackage{makeidx}\n\\usepackage{glossaries}\n\\usepackage{nomencl}\n"
        "\\makeindex\n\\makeglossaries\n\\makenomenclature\n"
    )
    BODY = (
        "Text\\index{text}.\n\\printglossaries\n\\printglossary[type=acronym]\n"
        "\\printnomenclature\n\\printindex\n\\printindex[authors]"
    )

    def test_missing_index_and_glossary_files_are_notes_by_default(self) -> None:
        self.document(self.LISTS, self.BODY)
        missing = self.one("source.generated_lists")
        self.assertEqual(
            (missing.code, missing.severity, missing.status), ("TEX012", "info", "not_applicable")
        )
        self.assertTrue(missing.is_scope_disclaimer)
        self.assertEqual(
            missing.details["expected"],
            ["authors.ind", "main.acr", "main.gls", "main.ind", "main.nls"],
        )
        self.assertEqual(missing.details["missing"], missing.details["expected"])
        self.assertEqual((missing.path, missing.line), ("main.tex", 10))
        self.assertIn("arXiv runs neither", missing.message)
        self.assertIn("prepare ships the files its verified build generated", missing.message)

    def test_a_venue_without_index_tools_makes_a_missing_file_a_warning(self) -> None:
        self.document(self.LISTS, self.BODY)
        for name in ("main.gls", "main.acr", "main.nls", "authors.ind"):
            self.write(name, "generated\n")
        warning = self.one("source.generated_lists", venue_runs_index_tools=False)
        self.assertEqual((warning.severity, warning.status), ("warning", "failed"))
        self.assertEqual(warning.details["missing"], ["main.ind"])
        self.assertIn("main.ind is not in the package", warning.message)
        self.assertIn("would be empty there", warning.message)

    def test_shipped_lists_pass_and_documents_without_lists_report_nothing(self) -> None:
        self.write(
            "paper/main.tex", PREAMBLE + "\\begin{document}\n\\printindex\n\\end{document}\n"
        )
        self.write("paper/main.ind", "\\begin{theindex}\\end{theindex}\n")
        shipped = self.one("source.generated_lists", main="paper/main.tex")
        self.assertEqual((shipped.severity, shipped.status), ("info", "passed"))
        self.assertEqual(shipped.path, "paper/main.ind")
        # A glossary type of its own definition, or a list without a print command, is
        # not checked; noidx glossaries are sorted by TeX and need no generated file.
        self.document(
            "\\usepackage{glossaries}\n",
            "\\printglossary[type=symbols]\n\\printnoidxglossaries\n\\makeindex",
        )
        self.assertEqual(self.findings("source.generated_lists"), [])


class NeverCreatedListTests(ReadinessCase):
    """A list the build looked for that no tool wrote is reported with its tool (TEX012)."""

    def test_a_list_the_build_never_got_is_reported_with_its_tool(self) -> None:
        (finding,) = never_created_findings(("main.nls", "notes.txt"), "main.tex")
        self.assertEqual(
            (finding.code, finding.severity, finding.status), ("TEX012", "warning", "failed")
        )
        self.assertIn("so the PDF has no nomenclature", finding.message)
        self.assertIn("makeindex with nomencl's style", finding.message)
        self.assertIn("main.nlo", finding.message)
        self.assertIn("ship main.nls beside the main file", finding.suggestion)
        self.assertEqual(never_created_findings((), "main.tex"), [])

    def test_the_baseline_build_reports_the_lists_it_never_got(self) -> None:
        self.document()

        async def build(tree, main, work, engine, runner, **options):
            """Build like ``fake_build`` and report a nomenclature that no tool wrote."""
            result = await fake_build(tree, main, work, engine, runner, **options)
            result.tools.submission_dependencies = SubmissionDependencies(
                {"main.tex"}, never_created=("main.nls",)
            )
            return result

        backends = controlled_backends(build=build)
        report = asyncio.run(run_job(JobRequest("check", self.root, Settings()), backends=backends))
        lists = [item for item in report.findings if item.code == "TEX012"]
        self.assertEqual([(item.status, item.stage) for item in lists], [("failed", "baseline")])

    def test_a_glossary_written_for_xindy_says_that_xindy_is_not_available(self) -> None:
        self.document()

        async def build(tree, main, work, engine, runner, **options):
            """Build like ``fake_build`` and report a glossary left empty for xindy."""
            result = await fake_build(tree, main, work, engine, runner, **options)
            result.tools.submission_dependencies = SubmissionDependencies(
                {"main.tex"}, never_created=("main.gls",), xindy_lists=("main.gls",)
            )
            return result

        backends = controlled_backends(build=build)
        report = asyncio.run(run_job(JobRequest("check", self.root, Settings()), backends=backends))
        (finding,) = [item for item in report.findings if item.code == "TEX012"]
        self.assertEqual((finding.severity, finding.status), ("warning", "failed"))
        self.assertIn("The glossary in main.gls was not built", finding.message)
        self.assertIn("xindy, which is not available in the isolated build", finding.message)
        self.assertIn("Run makeglossaries with xindy locally and ship main.gls", finding.suggestion)


class VenueEngineTests(ReadinessCase):
    """The engines the receiving service offers (TEX013)."""

    def test_an_engine_the_venue_does_not_offer_warns(self) -> None:
        self.document()
        arxiv = ("pdflatex", "xelatex")
        warning = self.one("source.venue_engine", engine="lualatex", venue_engines=arxiv)
        self.assertEqual(
            (warning.code, warning.severity, warning.status), ("TEX013", "warning", "failed")
        )
        self.assertIn("LuaLaTeX", warning.message)
        self.assertIn("pdfLaTeX, XeLaTeX", warning.message)
        offered = self.one("source.venue_engine", engine="xelatex", venue_engines=arxiv)
        self.assertEqual((offered.severity, offered.status), ("info", "passed"))
        # Nothing is reported while the venue's engines are not configured.
        self.assertEqual(self.findings("source.venue_engine", engine="lualatex"), [])
        with self.assertRaisesRegex(PreparationError, "venue_engines"):
            BblOptions(venue_engines=("context",))  # type: ignore[arg-type]

    def test_the_automatic_engine_follows_the_main_files_engine_comment(self) -> None:
        config = self.root / "fledge.toml"
        config.write_text('[submission_checks]\nvenue_engines = ["pdflatex", "xelatex"]\n')
        settings = load_settings(config, {})
        self.assertEqual(settings.engine, "auto")
        for comment, expected in (
            ("% !TEX program = lualatex\n", ("warning", "failed")),
            ("", ("info", "passed")),
        ):
            with self.subTest(comment=comment or "none"):
                self.write(
                    "main.tex", comment + PREAMBLE + "\\begin{document}\nT.\n\\end{document}\n"
                )
                findings = [
                    item
                    for item in source_readiness_findings(self.root, "main.tex", settings)
                    if item.rule == "source.venue_engine"
                ]
                self.assertEqual([(item.severity, item.status) for item in findings], [expected])


class EnginePackageTests(ReadinessCase):
    """Packages that the engine the document builds with cannot run (TEX014)."""

    def test_a_package_the_engine_cannot_run_warns_with_its_line(self) -> None:
        self.document("\\usepackage{graphicx}\n\\usepackage{fontspec}\n")
        warning = self.one("source.engine_packages", engine="pdflatex")
        self.assertEqual(
            (warning.code, warning.severity, warning.status), ("TEX014", "warning", "failed")
        )
        self.assertEqual((warning.path, warning.line), ("main.tex", 3))
        self.assertIn("fontspec needs XeLaTeX or LuaLaTeX", warning.message)
        self.assertIn("% !TEX program = xelatex", warning.suggestion or "")
        # Without a known engine, or without such a package, nothing is reported.
        self.assertEqual(self.findings("source.engine_packages"), [])
        self.document("\\usepackage{graphicx}\n")
        self.assertEqual(self.findings("source.engine_packages", engine="pdflatex"), [])

    def test_a_chosen_engine_that_runs_the_packages_passes(self) -> None:
        self.document("\\usepackage{fontspec}\n\\usepackage{luacode}\n")
        passed = self.one("source.engine_packages", engine="lualatex")
        self.assertEqual((passed.severity, passed.status), ("info", "passed"))
        warning = self.one("source.engine_packages", engine="xelatex")
        self.assertEqual(warning.details["package"], "luacode")
        self.assertIn("--engine lualatex", warning.suggestion or "")
        # A conditional declaration is not interpreted.
        self.document("\\ifdefined\\directlua\\usepackage{fontspec}\\fi\n")
        self.assertEqual(self.findings("source.engine_packages", engine="pdflatex"), [])

    def test_packages_that_need_different_engines_say_so(self) -> None:
        self.document("\\usepackage{xeCJK}\n\\usepackage{luatexja}\n")
        findings = self.findings("source.engine_packages", engine="pdflatex")
        self.assertEqual([item.details["package"] for item in findings], ["luatexja", "xeCJK"])
        self.assertTrue(
            all(
                "No engine runs all of luatexja, xeCJK" in (item.suggestion or "")
                for item in findings
            )
        )


class PrivateMarkerTests(ReadinessCase):
    """The advisory about private markers in comments, such as notes and author initials."""

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
    """The manuscript policies that require or forbid line numbers."""

    def check(self, **options: bool) -> Finding:
        """Run the manuscript policies with ``options`` and return the only line-number finding.

        The test fails unless there is exactly one.

        Args:
            **options: The fields of the :class:`ManuscriptCheckOptions` to use.

        Returns:
            The line-number finding.
        """
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
