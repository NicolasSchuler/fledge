from __future__ import annotations

import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from latexprep.bibliography_checks import BibliographyOptions, check_bibliography_details
from latexprep.bibliography_transform import BibliographyTransformOptions
from latexprep.build_checks import BuildCheckOptions, check_build_details
from latexprep.check_policy import (
    _GROUP_CHECKS,
    _ROOT_CHECKS,
    MANDATORY_CODES,
    effective_settings,
    selected_findings,
)
from latexprep.check_selection import CheckSelection
from latexprep.config import Settings
from latexprep.formatting import FormattingOptions
from latexprep.manuscript import check_manuscript
from latexprep.manuscript_checks import ManuscriptCheckOptions, check_manuscript_details
from latexprep.metadata_privacy import (
    MetadataEdit,
    MetadataPrivacyOptions,
    check_image_metadata,
    check_sanitized_pdf_metadata,
    plan_metadata_sanitization,
)
from latexprep.models import Finding
from latexprep.online_checks import OnlineOptions, Response, check_online_references
from latexprep.pdf import _configured_pdf_rules
from latexprep.pdf_artwork import PdfArtworkOptions, inspect_artwork_inputs, inspect_pdf_artwork
from latexprep.pdf_checks import (
    PdfCheckOptions,
    PdfRegion,
    PdfSectionBudget,
    inspect_included_pdf_figures,
    inspect_pdf_details,
)
from latexprep.rules import BY_CODE, BY_NAME, RULES, code_for
from latexprep.runtime import BuildResult
from latexprep.source import analyze_sources
from latexprep.source_transform import SourceTransformOptions
from latexprep.structure_checks import (
    HeadingExpectation,
    StructureOptions,
    check_author_records,
    inspect_pdf_structure,
)
from latexprep.submission_checks import (
    SubmissionOptions,
    check_pdf_identity,
    check_submission,
    check_submission_archive,
)
from latexprep.workflow_options import DocumentOptions, WorkflowOptions
from tests.test_pdf_artwork import ArtworkRunner, contrast_sample
from tests.test_pdf_checks import DetailRunner


def selection(*codes: str) -> CheckSelection:
    return CheckSelection(select=codes)


def codes(findings: list[Finding]) -> set[str]:
    return {finding.code for finding in findings if finding.code is not None}


def configured_manuscript() -> Settings:
    return Settings(
        expected_document_class="article",
        required_class_options=("draft",),
        forbidden_class_options=("twocolumn",),
        forbidden_packages=("draftwatermark",),
        require_abstract=True,
        abstract_min_words=1,
        abstract_max_words=100,
        keywords_min_count=1,
        keywords_max_count=5,
        required_sections=("Introduction",),
        manuscript_checks=ManuscriptCheckOptions(
            allowed_packages=("graphicx",),
            check_layout_manipulation=True,
            forbid_abstract_citations=True,
            abstract_abbreviation_policy="define",
            abstract_abbreviation_exceptions=("PDF",),
            max_heading_depth=2,
            check_empty_sections=True,
            check_hardcoded_references=True,
            required_metadata=("title", "author"),
            check_orcid=True,
            required_declarations=("Funding",),
            require_float_captions=True,
            require_float_labels=True,
            check_float_label_order=True,
            require_float_references=True,
            check_float_reference_order=True,
            require_figure_descriptions=True,
        ),
        structure_checks=StructureOptions(required_author_fields=("email",)),
    )


def configured_artwork() -> PdfArtworkOptions:
    region = PdfRegion("Figure", 1, 5, 5, 95, 95)
    return PdfArtworkOptions(
        detect_printer_marks=True,
        trim_regions=(region,),
        printer_mark_max_length_pt=10,
        printer_mark_exemptions=(2,),
        figure_regions=(region,),
        max_figure_whitespace_ratio=0.8,
        figure_edge_band_pt=2,
        drawing_regions=(region,),
        check_clipping=True,
        classify_artwork=True,
        artwork_category="line",
        category_min_dpi=(("line", 600),),
        classify_type3_glyphs=True,
        forbid_bitmap_type3_glyphs=True,
        min_stroke_width_pt=0.3,
        raster_regions=(region,),
        min_raster_region_coverage=0.8,
        grayscale_preview=True,
        contrast_samples=(contrast_sample(),),
        raster_dpi=72,
    )


def configured_pdf_details() -> PdfCheckOptions:
    return PdfCheckOptions(
        section_budgets=(PdfSectionBudget("Body", 1, 1, 2),),
        min_text_size_pt=10,
        text_size_regions=(PdfRegion("Body", 1, 0, 0, 500, 700, 10),),
        printable_margins_pt=(20, 20, 20, 20),
        printable_regions=(PdfRegion("Body", 1, 20, 20, 500, 700),),
        overlap_min_area_ratio=0.2,
        min_page_ink_ratio=0.1,
        sparse_page_exemptions=(2,),
        required_metadata=("Title",),
        expected_metadata=(("Title", "Paper"),),
        require_tagged=True,
        require_language=True,
        expected_language="en-GB",
        forbidden_annotation_types=("Text",),
        forbidden_action_types=("JavaScript",),
        min_stroke_width_pt=0.3,
        allowed_color_spaces=("DeviceRGB",),
        min_text_contrast=4.5,
        contrast_background_rgb=(1.0, 1.0, 1.0),
        min_grayscale_luminance_difference=0.1,
        require_embedded_figure_fonts=True,
        forbid_type3_figure_fonts=True,
    )


class CheckPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.main = self.root / "main.tex"
        self.main.write_text(
            "\\documentclass{article}\n\\title{Paper}\\author{Ada}\\email{ada@example.org}\n"
            "\\hypersetup{pdfauthor={Ada}}\n"
            "\\begin{document}\n\\begin{abstract}Words.\\end{abstract}\n"
            "\\section{Introduction}\nText \\cite{key}.\\bibliography{references}\n"
            "\\end{document}\n"
        )
        (self.root / "references.bib").write_text(
            "@article{key, title={A reference title}, author={Smith, Ada}, year={2024}, "
            "journal={Example}, doi={10.1234/example}, url={https://example.org/paper}}\n"
        )

    def test_default_selection_is_identity_and_narrowing_never_changes_original(self) -> None:
        original = replace(
            configured_manuscript(),
            pdf_checks=configured_pdf_details(),
            pdf_artwork=configured_artwork(),
            figure_artwork=configured_artwork(),
        )
        snapshot = asdict(original)
        self.assertIs(effective_settings(original), original)
        narrowed = replace(original, checks=selection("TEX101", "PDF209", "PDF303"))
        narrowed_snapshot = asdict(narrowed)
        effective = effective_settings(narrowed)
        self.assertEqual(asdict(original), snapshot)
        self.assertEqual(asdict(narrowed), narrowed_snapshot)
        self.assertEqual(effective.expected_document_class, "article")
        self.assertIsNone(effective.abstract_min_words)
        self.assertEqual(effective.pdf_checks.expected_language, "en-GB")
        self.assertIsNone(effective.pdf_checks.min_text_contrast)
        self.assertIsNone(effective.pdf_checks.contrast_background_rgb)
        self.assertEqual(effective.pdf_artwork.figure_regions, original.pdf_artwork.figure_regions)
        self.assertEqual(effective.pdf_artwork.figure_edge_band_pt, 2)
        self.assertIsNone(effective.pdf_artwork.max_figure_whitespace_ratio)
        self.assertIs(effective_settings(effective), effective)

    def test_selection_does_not_opt_in_parameters_network_or_transforms(self) -> None:
        requested = replace(Settings(), checks=selection("NET", "PDF", "TEX", "BIB2"))
        effective = effective_settings(requested)
        self.assertFalse(effective.online_checks.online)
        self.assertEqual(effective.online_checks, OnlineOptions())
        self.assertEqual(effective.pdf_checks, PdfCheckOptions())
        self.assertEqual(effective.pdf_artwork, PdfArtworkOptions())
        self.assertFalse(effective.format)
        self.assertFalse(effective.normalize_doi)
        self.assertEqual(effective.source_transforms, SourceTransformOptions())
        self.assertEqual(effective.bibliography_transform, BibliographyTransformOptions())

    def test_each_manuscript_policy_is_disabled_before_its_checker(self) -> None:
        original = configured_manuscript()
        expected = {
            *(f"TEX{number}" for number in range(101, 108)),
            *(f"MAN{number:03}" for number in range(1, 17)),
            "MAN201",
        }

        def inspect(settings: Settings) -> list[Finding]:
            return [
                *check_manuscript(self.root, "main.tex", settings.manuscript_options()),
                *check_manuscript_details(self.root, "main.tex", settings.manuscript_checks),
                *check_author_records(self.root, "main.tex", settings.structure_checks),
            ]

        self.assertEqual(codes(inspect(original)), expected)
        for code in expected:
            with self.subTest(code=code):
                effective = effective_settings(replace(original, checks=selection(code)))
                self.assertEqual(codes(inspect(effective)), {code})
        disabled = effective_settings(replace(original, checks=selection()))
        with (
            patch("latexprep.manuscript._Inspection", side_effect=AssertionError("source scan")),
            patch("latexprep.manuscript_checks._read", side_effect=AssertionError("source scan")),
            patch("latexprep.structure_checks._read", side_effect=AssertionError("author scan")),
        ):
            self.assertEqual(inspect(disabled), [])

    def test_bibliography_switches_control_actual_checker_and_preserve_context(self) -> None:
        original = Settings(
            bibliography_checks=BibliographyOptions(
                all_roots=True,
                required_fields=(("article", ("title",)),),
                doi_entry_types=("article",),
                check_capitalization=True,
                check_fuzzy_duplicates=True,
                fuzzy_duplicate_threshold=0.8,
            )
        )
        expected = {f"BIB{number}" for number in range(101, 109)}
        self.assertEqual(
            codes(check_bibliography_details(self.root, "main.tex", original.bibliography_checks)),
            expected,
        )
        for code in expected:
            with self.subTest(code=code):
                effective = effective_settings(replace(original, checks=selection(code)))
                self.assertEqual(
                    codes(
                        check_bibliography_details(
                            self.root, "main.tex", effective.bibliography_checks
                        )
                    ),
                    {code},
                )
                self.assertTrue(effective.bibliography_checks.all_roots)
                self.assertEqual(effective.bibliography_checks.fuzzy_duplicate_threshold, 0.8)
        disabled = effective_settings(replace(original, checks=selection()))
        with patch("latexprep.bibliography_checks._Inspection", side_effect=AssertionError("scan")):
            self.assertEqual(
                check_bibliography_details(self.root, "main.tex", disabled.bibliography_checks), []
            )

    def test_build_policy_and_instrumentation_switches_keep_explicit_inventory(self) -> None:
        original = Settings(
            build_checks=BuildCheckOptions(
                overfull_tolerance_pt=1,
                inventory_loaded_packages=True,
                inventory_loaded_options=True,
                allowed_loaded_packages=("article.cls",),
                required_loaded_packages=("article.cls",),
                forbidden_loaded_packages=("draftwatermark.sty",),
                minimum_package_dates=(("article.cls", "2020-01-01"),),
                check_local_package_shadows=True,
            )
        )
        result = BuildResult(
            True,
            findings=[Finding("build.overfull_box", "Overflow", details={"overflow_pt": 2})],
            loaded_packages=[{"name": "article.cls", "date": "2024-01-01", "origin": "system"}],
            recorder_complete=True,
        )
        self.assertEqual(
            codes(check_build_details(result, original.build_checks)),
            {"BLD101", "BLD102", "BLD103", "BLD301"},
        )
        disabled = effective_settings(replace(original, checks=selection()))
        self.assertFalse(disabled.build_checks.inventory_loaded_options)
        self.assertFalse(disabled.build_checks.check_local_package_shadows)
        inventory = check_build_details(result, disabled.build_checks)
        self.assertEqual([finding.rule for finding in inventory], ["build.package_inventory"])
        self.assertEqual(selected_findings(inventory, selection()), inventory)
        for code in ("BLD101", "BLD102", "BLD103", "BLD301"):
            with self.subTest(code=code):
                effective = effective_settings(replace(original, checks=selection(code)))
                self.assertEqual(codes(check_build_details(result, effective.build_checks)), {code})
        threshold = effective_settings(replace(original, checks=selection("BLD101")))
        self.assertEqual(check_build_details(result, threshold.build_checks)[0].status, "failed")
        self.assertTrue(result.findings, "Raw build evidence is needed by the selected tolerance")

    def test_ignored_privacy_scan_preserves_metadata_edits_and_verification(self) -> None:
        original = Settings(
            checks=selection(),
            metadata_privacy=MetadataPrivacyOptions(
                edits=(MetadataEdit("main.tex", "hypersetup.pdfauthor", "Ada", "Anonymous"),),
                image_identity_terms=("Ada",),
            ),
        )
        self.assertEqual(
            codes(check_image_metadata(self.root, original.metadata_privacy)), {"PRV103"}
        )
        effective = effective_settings(original)
        with patch(
            "latexprep.metadata_privacy._inventory", side_effect=AssertionError("image scan")
        ):
            self.assertEqual(check_image_metadata(self.root, effective.metadata_privacy), [])
        before = self.main.read_bytes()
        plan = plan_metadata_sanitization(self.root, effective.metadata_privacy)
        self.assertIn(b"Anonymous", plan.contents["main.tex"])
        self.assertEqual(self.main.read_bytes(), before)
        self.assertEqual(codes(selected_findings(plan.findings, effective.checks)), {"PRV101"})
        verification = check_sanitized_pdf_metadata(
            [
                Finding(
                    "pdf.metadata",
                    "Properties",
                    "info",
                    "passed",
                    details={"properties": {"Pages": "1", "Author": "Ada"}},
                )
            ],
            effective.metadata_privacy,
        )
        self.assertEqual(codes(selected_findings(verification, effective.checks)), {"PRV102"})
        self.assertEqual(verification[0].status, "failed")

    def test_requested_transformations_and_required_source_guards_survive_ignore_all(self) -> None:
        original = Settings(
            checks=CheckSelection(ignore=("ALL",)),
            format=True,
            normalize_doi=True,
            layout="flat",
            formatting_options=FormattingOptions(exclude=("vendor/*",), blank_lines="preserve"),
            source_transforms=SourceTransformOptions(
                comment_policy="private",
                merge_inputs=True,
                inline_bibliography="main.bbl",
                filename_overrides=(("main.tex", "paper.tex"),),
            ),
            bibliography_transform=BibliographyTransformOptions(
                format_entries=True,
                remove_fields=("abstract",),
                cited_only=True,
                key_renames=(("key", "renamed"),),
                order_entries=True,
            ),
            workflow=WorkflowOptions(
                documents=(DocumentOptions("paper", "main.tex"),), baseline_runs=2
            ),
        )
        effective = effective_settings(original)
        self.assertTrue(effective.format)
        self.assertTrue(effective.normalize_doi)
        self.assertEqual(effective.layout, "flat")
        for name in (
            "formatting_options",
            "source_transforms",
            "bibliography_transform",
            "workflow",
            "reporting",
        ):
            self.assertIs(getattr(effective, name), getattr(original, name))
        self.main.write_text(
            "\\documentclass{article}\\input{missing}\\begin{document}Body\\end{document}"
        )
        findings = analyze_sources(self.root, "main.tex").findings
        self.assertIn("TEX005", codes(selected_findings(findings, effective.checks)))

    def test_privacy_shared_terms_remain_until_both_producers_are_disabled(self) -> None:
        original = Settings(submission_checks=SubmissionOptions(identity_terms=("Ada",)))
        for code in ("PRV001", "PRV002"):
            effective = effective_settings(replace(original, checks=selection(code)))
            self.assertEqual(effective.submission_checks.identity_terms, ("Ada",))
            # Each dispatcher clears its own terms when the other producer alone
            # is selected. A shared Settings field cannot encode that distinction.
            findings = [
                *check_submission(self.root, "main.tex", effective.submission_checks),
                *check_pdf_identity("Ada", {"Title": "Paper"}, effective.submission_checks),
            ]
            self.assertEqual(codes(selected_findings(findings, effective.checks)), {code})
        disabled = effective_settings(replace(original, checks=selection()))
        with patch("latexprep.submission_checks._inventory", side_effect=AssertionError("scan")):
            self.assertEqual(
                check_submission(self.root, "main.tex", disabled.submission_checks), []
            )
        self.assertEqual(
            check_pdf_identity("Ada", {"Title": "Paper"}, disabled.submission_checks), []
        )

    def test_submission_and_archive_checks_stop_before_scanning_when_deselected(self) -> None:
        original = Settings(
            submission_checks=SubmissionOptions(
                identity_terms=("Ada",),
                scan_identity_hints=True,
                scan_secrets=True,
                scan_private_comments=True,
                check_shell_escape=True,
                filename_max_length=12,
                filename_allowed_characters="abcdefghijklmnopqrstuvwxyz._",
                allowed_extensions=(".tex",),
                required_deliverables=(("main.tex", "text"),),
                max_archive_bytes=1,
                report_unused_assets=True,
                template_references=(("missing.cls", "2024/01/01"),),
            )
        )
        before = check_submission(self.root, "main.tex", original.submission_checks)
        self.assertEqual(
            codes(before),
            {
                "PRV001",
                "PRV003",
                "PRV004",
                "PRV005",
                "PRV006",
                "PKG101",
                "PKG102",
                "PKG103",
                "PKG104",
                "PKG106",
                "PKG107",
            },
        )
        disabled = effective_settings(replace(original, checks=selection()))
        with patch("latexprep.submission_checks._inventory", side_effect=AssertionError("scan")):
            self.assertEqual(
                check_submission(self.root, "main.tex", disabled.submission_checks), []
            )
        self.assertEqual(
            check_submission_archive(self.root / "absent.zip", disabled.submission_checks), []
        )

    def test_mandatory_and_uncoded_findings_survive_and_other_codes_follow_selection(self) -> None:
        expected = {
            "TEX005",
            "TEX006",
            "TEX007",
            "TEX008",
            "TEX201",
            "TEX202",
            "TEX203",
            "PKG001",
            "PKG002",
            "PKG003",
            "PKG004",
            "PKG201",
            "PKG301",
            "CMP001",
            "CMP002",
            "CMP003",
            "CMP101",
            "FMT002",
            "FMT101",
            "FMT102",
            "FMT103",
            "BLD008",
            "BLD009",
            "BLD201",
            "BLD202",
            "PRV101",
            "PRV102",
            "BIB001",
            "BIB002",
            "BIB004",
            "BIB005",
            "BIB201",
            "BIB202",
            "BIB203",
            "BIB204",
            "BIB205",
            "BIB206",
            "RPT101",
            "RPT102",
            "RPT103",
        }
        self.assertEqual(MANDATORY_CODES, expected)
        findings = [Finding(rule.name, rule.title) for rule in RULES]
        diagnostic = Finding("build.failed", "Build failed", "error")
        inventory = Finding("pdf.metadata", "Metadata", "info", "passed")
        findings += [diagnostic, inventory]
        self.assertEqual(codes(selected_findings(iter(findings), selection())), expected)
        selected = CheckSelection(select=("ALL", "MAN001"), ignore=("MAN", "PDF3"))
        retained = selected_findings(findings, selected)
        self.assertIn(BY_CODE["MAN001"].name, [finding.rule for finding in retained])
        self.assertNotIn(BY_CODE["MAN002"].name, [finding.rule for finding in retained])
        self.assertNotIn(BY_CODE["PDF311"].name, [finding.rule for finding in retained])
        self.assertIs(retained[-2], diagnostic)
        self.assertIs(retained[-1], inventory)
        self.assertEqual(selected_findings(findings, CheckSelection()), findings)

    def test_future_registered_code_is_selected_without_policy_allowlist(self) -> None:
        future = replace(BY_CODE["MAN001"], code="NEW001", name="future.check")
        finding = Finding(future.name, "Future check")
        with (
            patch("latexprep.rules.BY_NAME", {**BY_NAME, future.name: future}),
            patch("latexprep.check_selection.BY_CODE", {**BY_CODE, future.code: future}),
        ):
            self.assertEqual(selected_findings([finding], CheckSelection()), [finding])
            self.assertEqual(selected_findings([finding], selection()), [])

    def test_every_current_code_has_an_execution_category(self) -> None:
        gated = set(_ROOT_CHECKS).union(*(set(group) for group in _GROUP_CHECKS.values()))
        shared = {"PRV001", "PRV002"}
        # These are observations emitted by mandatory parsing/building or by
        # explicitly requested formatting. Selection filters their findings after
        # dependent policies consume the evidence; it cannot suppress that work.
        produced_with_required_work = {
            "TEX001",
            "TEX002",
            "TEX003",
            "TEX004",
            "BIB003",
            "BIB006",
            "BIB007",
            "BLD001",
            "BLD002",
            "BLD003",
            "BLD004",
            "BLD005",
            "BLD006",
            "BLD007",
            "FMT001",
        }
        self.assertFalse(gated & MANDATORY_CODES)
        self.assertEqual(
            set(BY_CODE), gated | shared | MANDATORY_CODES | produced_with_required_work
        )


class CheckPolicyAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.pdf = self.root / "paper.pdf"
        self.pdf.write_bytes(b"%PDF-fixture")

    async def test_all_seven_online_switches_project_without_granting_network(self) -> None:
        toggles = {
            "NET001": "online_doi_resolution",
            "NET002": "online_metadata",
            "NET003": "online_missing_doi",
            "NET004": "online_published_versions",
            "NET005": "online_notices",
            "NET006": "online_reference_links",
            "NET007": "online_replication_links",
        }
        options = OnlineOptions(
            **dict.fromkeys(toggles.values(), True),
            online_replication_urls=("https://example.org/code",),
        )
        self.assertEqual(
            codes(await check_online_references(self.root, options=options)), set(toggles)
        )
        for code in toggles:
            with self.subTest(code=code):
                effective = effective_settings(
                    Settings(checks=selection(code), online_checks=options)
                )
                with patch(
                    "latexprep.online_checks._collect", side_effect=AssertionError("offline scan")
                ):
                    findings = await check_online_references(
                        self.root, options=effective.online_checks
                    )
                self.assertEqual(codes(findings), {code})
                self.assertEqual(findings[0].status, "skipped")
                self.assertFalse(effective.online_checks.online)
                self.assertEqual(
                    effective.online_checks.online_replication_urls, options.online_replication_urls
                )

    async def test_ignored_online_check_performs_no_scan_dns_or_http(self) -> None:
        (self.root / "references.bib").write_text("@article{key, doi={10.1234/example}}\n")
        original = Settings(online_checks=OnlineOptions(online=True, online_doi_resolution=True))
        dns = AsyncMock(return_value=("93.184.216.34",))
        http = AsyncMock(return_value=Response(200))
        with (
            patch("latexprep.online_checks._resolve", dns),
            patch("latexprep.online_checks._exchange", http),
        ):
            findings = await check_online_references(self.root, options=original.online_checks)
        self.assertEqual(codes(findings), {"NET001"})
        self.assertGreater(dns.await_count, 0)
        self.assertGreater(http.await_count, 0)
        effective = effective_settings(replace(original, checks=CheckSelection(ignore=("NET",))))
        with (
            patch("latexprep.online_checks._collect", side_effect=AssertionError("scan")),
            patch("latexprep.online_checks._resolve", side_effect=AssertionError("DNS")),
            patch("latexprep.online_checks._exchange", side_effect=AssertionError("HTTP")),
        ):
            self.assertEqual(
                await check_online_references(self.root, options=effective.online_checks), []
            )

    async def test_each_pdf_detail_disables_other_adapters_and_keeps_paired_arguments_valid(
        self,
    ) -> None:
        original = Settings(
            pdf_checks=configured_pdf_details(), match_source_pdf_metadata=("title", "author")
        )
        expected = {f"PDF{number}" for number in range(201, 217)}
        before = await inspect_pdf_details(
            self.pdf, self.root / "details-all", DetailRunner(), options=original.pdf_checks
        )
        self.assertEqual(codes(before), expected - {"PDF212"})
        for code in expected - {"PDF212"}:
            with self.subTest(code=code):
                effective = effective_settings(replace(original, checks=selection(code)))
                runner = DetailRunner()
                findings = await inspect_pdf_details(
                    self.pdf, self.root / code, runner, options=effective.pdf_checks
                )
                self.assertEqual(codes(findings), {code})
                self.assertTrue(runner.calls)
                self.assertEqual(
                    effective.match_source_pdf_metadata,
                    original.match_source_pdf_metadata if code == "PDF207" else (),
                )
        disabled = effective_settings(replace(original, checks=selection()))
        runner = DetailRunner()
        self.assertEqual(
            await inspect_pdf_details(
                self.pdf, self.root / "details-off", runner, options=disabled.pdf_checks
            ),
            [],
        )
        self.assertEqual(
            await inspect_included_pdf_figures(
                (self.pdf,), self.root / "figures-off", runner, options=disabled.pdf_checks
            ),
            [],
        )
        self.assertEqual(runner.calls, [])
        fonts = effective_settings(replace(original, checks=selection("PDF212")))
        findings = await inspect_included_pdf_figures(
            (self.pdf,), self.root / "figures-on", runner, options=fonts.pdf_checks
        )
        self.assertEqual(codes(findings), {"PDF212"})
        self.assertIn("pdffonts", {call[0][0] for call in runner.calls})

    async def test_base_pdf_policy_constraints_are_removed_together(self) -> None:
        original = Settings(
            max_pages=10,
            expected_page_width_pt=612,
            expected_page_height_pt=792,
            require_consistent_page_size=True,
            min_image_dpi=300,
            require_embedded_fonts=True,
            forbid_type3_fonts=True,
            forbid_encryption=True,
            forbid_forms=True,
            forbid_javascript=True,
            forbid_attachments=True,
        )
        expected = {"PDF001", *(f"PDF{number}" for number in range(101, 110))}
        self.assertEqual(
            {
                code_for(name)
                for name in _configured_pdf_rules(original.pdf_options(), original.max_pages)
            },
            expected,
        )
        for code in expected:
            effective = effective_settings(replace(original, checks=selection(code)))
            self.assertEqual(
                {
                    code_for(name)
                    for name in _configured_pdf_rules(effective.pdf_options(), effective.max_pages)
                },
                {code},
            )
        disabled = effective_settings(replace(original, checks=selection()))
        self.assertEqual(_configured_pdf_rules(disabled.pdf_options(), disabled.max_pages), [])

    async def test_artwork_regions_and_rendering_stay_only_when_their_check_is_selected(
        self,
    ) -> None:
        original = Settings(pdf_artwork=configured_artwork(), figure_artwork=configured_artwork())
        expected = {f"PDF{number}" for number in range(301, 313)}
        before = await inspect_pdf_artwork(
            self.pdf, self.root / "artwork-all", ArtworkRunner(), options=original.pdf_artwork
        )
        self.assertEqual(codes(before), expected)
        for code in expected:
            with self.subTest(code=code):
                effective = effective_settings(replace(original, checks=selection(code)))
                self.assertEqual(effective.figure_artwork, effective.pdf_artwork)
                runner = ArtworkRunner()
                findings = await inspect_pdf_artwork(
                    self.pdf, self.root / code, runner, options=effective.pdf_artwork
                )
                self.assertEqual(codes(findings), {code})
                if code in {"PDF302", "PDF303"}:
                    self.assertEqual(
                        effective.pdf_artwork.figure_regions, original.pdf_artwork.figure_regions
                    )
                else:
                    self.assertEqual(effective.pdf_artwork.figure_regions, ())
                if code == "PDF308":
                    self.assertNotIn("pdftoppm", {call[0][0] for call in runner.calls})
        disabled = effective_settings(replace(original, checks=CheckSelection(ignore=("PDF3",))))
        runner = ArtworkRunner()
        self.assertEqual(
            await inspect_pdf_artwork(
                self.pdf, self.root / "artwork-off", runner, options=disabled.pdf_artwork
            ),
            [],
        )
        self.assertEqual(
            await inspect_artwork_inputs(
                (self.pdf,), self.root / "inputs-off", runner, options=disabled.figure_artwork
            ),
            [],
        )
        self.assertEqual(runner.calls, [])

    async def test_structure_inspection_stops_before_tools_and_retains_other_structure_checks(
        self,
    ) -> None:
        original = Settings(
            structure_checks=StructureOptions(
                heading_expectations=(HeadingExpectation(1, "Introduction", "1"),),
                require_structure_tree=True,
                check_table_structure=True,
                check_link_structure=True,
                require_figure_alt=True,
                check_structure_references=True,
            )
        )
        expected = {f"PDF{number}" for number in range(401, 406)}
        before = await inspect_pdf_structure(
            self.pdf, self.root / "structure-all", DetailRunner(), original.structure_checks
        )
        self.assertEqual(codes(before), expected)
        for code in expected:
            effective = effective_settings(replace(original, checks=selection(code)))
            findings = await inspect_pdf_structure(
                self.pdf, self.root / code, DetailRunner(), effective.structure_checks
            )
            self.assertEqual(codes(findings), {code})
        disabled = effective_settings(replace(original, checks=selection()))
        runner = DetailRunner()
        self.assertEqual(
            await inspect_pdf_structure(
                self.pdf, self.root / "structure-off", runner, disabled.structure_checks
            ),
            [],
        )
        self.assertEqual(runner.calls, [])


if __name__ == "__main__":
    unittest.main()
