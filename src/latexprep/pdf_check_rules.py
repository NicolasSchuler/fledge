"""Data-only catalogue for optional PDF measurements and object policies."""

RULE_DEFINITIONS = (
    {
        "code": "PDF201",
        "name": "pdf.section_page_budget",
        "title": "Section page budgets",
        "description": "Compare explicitly supplied inclusive page ranges with user-supplied "
        "section budgets; ranges outside the actual document are inconclusive.",
        "tests": (
            "tests.test_pdf_checks.PdfDetailTests.test_section_budget_accepts_rejects_and_marks_outside_ranges_uncertain",
        ),
        "fix": "Review the named section's content or correct its explicit page range and budget.",
    },
    {
        "code": "PDF202",
        "name": "pdf.rendered_text_size",
        "title": "Reported rendered text size",
        "description": "Compare Poppler text sizes with global or explicitly located regional "
        "minima. Integer rounding, absent text, unsupported rotation/crop and partial regions "
        "remain inconclusive. Raster/outlined text and semantic categories are not inferred.",
        "tests": (
            "tests.test_pdf_checks.PdfDetailTests.test_text_size_threshold_rounding_and_unmeasured_text_are_distinct",
            "tests.test_pdf_checks.PdfDetailTests.test_text_size_regions_apply_specific_minima_and_reject_partial_overlap",
        ),
        "fix": "Increase the reported text size in the listed region and inspect the page.",
    },
    {
        "code": "PDF203",
        "name": "pdf.printable_text_bounds",
        "title": "Printable text bounds",
        "description": "Compare extractable text boxes with supplied margins or allowed regions. "
        "Advisory; image bounds, clipping paths, rotated/cropped pages and semantic object "
        "attribution are outside this adapter's measured scope.",
        "tests": (
            "tests.test_pdf_checks.PdfDetailTests.test_printable_bounds_accept_full_width_regions_and_flag_outside_text",
        ),
        "fix": "Inspect the listed text boxes and adjust layout or explicitly allowed regions.",
    },
    {
        "code": "PDF204",
        "name": "pdf.text_overlap",
        "title": "Text overlap candidates",
        "description": "Measure pairwise extractable-text rectangle intersections against an "
        "explicit area ratio; legitimate mathematical overlays may be reported. Advisory.",
        "tests": (
            "tests.test_pdf_checks.PdfDetailTests.test_text_overlap_candidates_have_geometric_evidence_and_bounded_scope",
        ),
        "fix": "Inspect the listed box intersections and repair unintended overlap.",
    },
    {
        "code": "PDF205",
        "name": "pdf.sparse_pages",
        "title": "Sparse rendered page candidates",
        "description": "Compare bounded grayscale pixel coverage with an explicit minimum and "
        "background assumption. Exempt pages are supplied explicitly; intended blank pages "
        "and scan-only pages are not inferred. Advisory.",
        "tests": (
            "tests.test_pdf_checks.PdfDetailTests.test_sparse_pages_measure_ink_and_distinguish_missing_raster",
        ),
        "fix": "Review possible spillover/blank pages or record intended page exemptions.",
    },
    {
        "code": "PDF206",
        "name": "pdf.required_metadata",
        "title": "Required PDF metadata",
        "description": "Require nonempty user-selected pdfinfo text properties. Presence does "
        "not establish rights, identity or semantic correctness.",
        "tests": (
            "tests.test_pdf_checks.PdfDetailTests.test_required_metadata_handles_missing_fields_and_ambiguous_evidence",
        ),
        "fix": "Supply the required nonempty PDF properties in the document's export settings.",
    },
    {
        "code": "PDF207",
        "name": "pdf.metadata_agreement",
        "title": "Expected PDF metadata agreement",
        "description": "Compare properties with supplied expected values using whitespace-only "
        "normalization. Source expansion, identity and author order equivalence are not inferred.",
        "tests": (
            "tests.test_pdf_checks.PdfDetailTests.test_metadata_agreement_preserves_case_and_author_order",
        ),
        "fix": "Reconcile the listed PDF properties with the supplied expected metadata.",
    },
    {
        "code": "PDF208",
        "name": "pdf.tagging_presence",
        "title": "PDF tagging presence",
        "description": "Require pdfinfo to report tagging present; this is not a reading-order, "
        "table/link structure, PDF/UA or general accessibility validation.",
        "tests": (
            "tests.test_pdf_checks.PdfDetailTests.test_tagging_presence_does_not_claim_semantic_accessibility",
        ),
        "fix": "Enable supported tagged-PDF output and review its semantic structure separately.",
    },
    {
        "code": "PDF209",
        "name": "pdf.document_language",
        "title": "PDF language declaration",
        "description": "Inspect qpdf's resolved catalog language for required presence or an "
        "explicit expected language tag; unsupported encoding or missing qpdf is inconclusive.",
        "tests": (
            "tests.test_pdf_checks.PdfDetailTests.test_catalog_language_uses_resolved_objects_and_explicit_expectation",
        ),
        "fix": "Set the document's catalog language to the appropriate configured language tag.",
    },
    {
        "code": "PDF210",
        "name": "pdf.annotation_policy",
        "title": "PDF annotation type policy",
        "description": "Inspect resolved page annotation objects for explicitly forbidden "
        "subtypes, including indirect objects. Missing/incomplete qpdf evidence is inconclusive.",
        "tests": (
            "tests.test_pdf_checks.PdfDetailTests.test_annotation_policy_reads_indirect_page_annotations",
        ),
        "fix": "Remove or convert the forbidden annotation types in a separate submission copy.",
    },
    {
        "code": "PDF211",
        "name": "pdf.action_policy",
        "title": "PDF action-name candidates",
        "description": "Scan resolved reachable dictionaries for explicitly forbidden /S names. "
        "Advisory action candidates; opaque streams and orphan objects are excluded, and no "
        "malware-free guarantee is made.",
        "tests": (
            "tests.test_pdf_checks.PdfDetailTests.test_action_policy_scans_reachable_objects_without_echoing_payload",
        ),
        "fix": "Review the matched action types and remove disallowed active behavior from a copy.",
    },
    {
        "code": "PDF212",
        "name": "pdf.included_figure_fonts",
        "title": "Included PDF figure fonts",
        "description": "Inspect explicitly supplied input PDFs for configured embedding and "
        "Type 3 policies. No reported fonts is inconclusive; final-PDF attribution and bitmap "
        "Type 3 glyph classification are not established.",
        "tests": (
            "tests.test_pdf_checks.PdfDetailTests.test_included_figure_fonts_keep_source_attribution_and_unknown_coverage",
        ),
        "fix": "Re-export the named input PDF figure with the required font policy.",
    },
    {
        "code": "PDF213",
        "name": "pdf.stroke_width",
        "title": "Transformed PDF stroke width",
        "description": "Compare MuPDF trace stroke widths after uniform placement transforms "
        "with an explicit minimum. Hairlines/nonuniform transforms, raster lines and filled "
        "outlines are unmeasured; candidates remain advisory.",
        "tests": (
            "tests.test_pdf_checks.PdfDetailTests.test_stroke_width_applies_transform_and_rejects_hairline_or_anisotropic_measurements",
        ),
        "fix": "Inspect listed thin strokes and increase their width at the final placement scale.",
    },
    {
        "code": "PDF214",
        "name": "pdf.color_space_policy",
        "title": "Rendering color-space policy",
        "description": "Compare MuPDF trace rendering color-space names with an explicit allowed "
        "list. Source encoding, color-only meaning and perceptual accessibility are not inferred.",
        "tests": (
            "tests.test_pdf_checks.PdfDetailTests.test_rendered_color_policy_handles_allowed_disallowed_and_unmeasured_spaces",
        ),
        "fix": "Review disallowed rendering color spaces and export with the supplied policy.",
    },
    {
        "code": "PDF215",
        "name": "pdf.assumed_text_contrast",
        "title": "Text contrast against supplied background",
        "description": "Compare opaque trace text colors against an explicitly supplied uniform "
        "background, treating DeviceRGB/Gray as sRGB. Actual backgrounds, raster text and "
        "compositing are not established; the comparison is advisory.",
        "tests": (
            "tests.test_pdf_checks.PdfDetailTests.test_assumed_text_contrast_handles_high_low_and_unmeasured_colors",
        ),
        "fix": "Review the named colors against the actual background and improve their contrast.",
    },
    {
        "code": "PDF216",
        "name": "pdf.grayscale_separation",
        "title": "Grayscale color separation candidates",
        "description": "Compare nominal sRGB luminance for distinct opaque DeviceRGB/Gray colors "
        "on each page against a supplied difference. Advisory; color-only meaning, proximity, "
        "raster palettes, compositing and perceptual accessibility are not inferred.",
        "tests": (
            "tests.test_pdf_checks.PdfDetailTests.test_grayscale_separation_flags_nominal_luminance_collisions_only",
        ),
        "fix": "Inspect the listed color pairs and add luminance, pattern or label distinctions.",
    },
)
