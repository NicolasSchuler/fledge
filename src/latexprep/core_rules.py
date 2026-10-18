"""Data-only catalogue for the original source, packaging, build, PDF and comparison checks."""

from .rule_definition import Rule

RULES = (
    Rule(
        code="TEX001",
        name="source.edit_marker",
        aliases=("source-edit-marker",),
        title="Unfinished text markers",
        description=(
            "Find TODO, FIXME, XXX and ?? in active source; comments and literal examples "
            "are ignored. Advisory heuristic."
        ),
        tests=(
            "tests.test_source.SourceTests.test_markers_labels_and_refs_are_heuristics_not_blockers",
        ),
        fix="Finish or remove the marked draft text after reviewing its context.",
        reviewable=True,
    ),
    Rule(
        code="TEX002",
        name="source.edit_command",
        aliases=("source-edit-command",),
        title="Editing commands",
        description=(
            "Find supported draft/editing commands such as todo in active source. Advisory "
            "heuristic."
        ),
        tests=(
            "tests.test_source.SourceTests.test_markers_labels_and_refs_are_heuristics_not_blockers",
        ),
        fix="Resolve the editing note and remove its draft command.",
        reviewable=True,
    ),
    Rule(
        code="TEX003",
        name="source.duplicate_label",
        aliases=("source-duplicate-label",),
        title="Duplicate literal labels",
        description=(
            "Find repeated literal labels in the selected source graph without expanding "
            "TeX macros. Advisory heuristic."
        ),
        tests=(
            "tests.test_source.SourceTests.test_markers_labels_and_refs_are_heuristics_not_blockers",
        ),
        fix="Give distinct objects unique labels and update their references.",
        reviewable=True,
    ),
    Rule(
        code="TEX004",
        name="source.unresolved_reference",
        aliases=("source-unresolved-reference",),
        title="Unresolved literal references",
        description=(
            "Find literal references without a matching scanned label. Generated labels may"
            " make this inconclusive; advisory heuristic."
        ),
        tests=(
            "tests.test_source.SourceTests.test_markers_labels_and_refs_are_heuristics_not_blockers",
        ),
        fix="Correct the reference key or add the intended label; confirm with a clean build.",
        reviewable=True,
    ),
    Rule(
        code="TEX005",
        name="source.missing_dependency",
        aliases=("source-missing-dependency",),
        title="Missing source dependencies",
        description="Detect absent files named by supported literal source commands.",
        tests=(
            "tests.test_source.SourceTests.test_absolute_outside_and_missing_dependencies_are_errors",
        ),
        fix="Include the missing dependency or correct the literal file path.",
        mandatory=True,
    ),
    Rule(
        code="TEX006",
        name="source.external_path",
        aliases=("source-external-path",),
        title="External source paths",
        description=(
            "Reject absolute paths and literal file references escaping the imported project."
        ),
        tests=(
            "tests.test_source.SourceTests.test_absolute_outside_and_missing_dependencies_are_errors",
        ),
        fix="Copy the dependency into the project and use a project-relative path.",
        mandatory=True,
    ),
    Rule(
        code="TEX007",
        name="source.case_mismatch",
        aliases=("source-case-mismatch",),
        title="Dependency filename case",
        description=(
            "Detect case mismatches without guessing which file a case-sensitive submission"
            " system will load."
        ),
        tests=("tests.test_source.SourceTests.test_case_mismatch_is_reported_and_not_guessed",),
        fix="Make the reference match the filename's exact letter case.",
        mandatory=True,
    ),
    Rule(
        code="TEX008",
        name="source.ambiguous_dependency",
        aliases=("source-ambiguous-dependency",),
        title="Ambiguous source dependencies",
        description=(
            "Report a literal reference that several files could satisfy, and the file TeX's "
            "lookup order selects (build directory, then search paths; default graphics "
            "extensions in pdfTeX order). Advisory; flat layout rewrites the selected name."
        ),
        tests=(
            "tests.test_source.SourceTests.test_extensionless_graphics_with_multiple_extensions_are_ambiguous",
            "tests.test_source.SourceTests.test_searchpath_ambiguity_uses_tex_lookup_order",
        ),
        fix=(
            "Confirm the selected file is intended; use an explicit path and extension "
            "or remove the unused alternative."
        ),
    ),
    Rule(
        code="TEX101",
        name="manuscript.document_class",
        title="Expected document class",
        description=(
            "Compare the selected document's literal class declaration with "
            "expected_document_class."
        ),
        tests=(
            "tests.test_manuscript.ManuscriptTests.test_expected_document_class_matches_and_mismatch_blocks",
        ),
        fix="Use the configured document class, or correct the expected class in your settings.",
        reviewable=True,
    ),
    Rule(
        code="TEX102",
        name="manuscript.class_options",
        title="Document class options",
        description=(
            "Check required_class_options and forbidden_class_options in literal class "
            "declarations."
        ),
        tests=(
            "tests.test_manuscript.ManuscriptTests.test_class_options_report_missing_and_forbidden_values",
        ),
        fix="Add the missing class options and remove the forbidden options listed above.",
        reviewable=True,
    ),
    Rule(
        code="TEX103",
        name="manuscript.forbidden_packages",
        title="Forbidden package declarations",
        description=(
            "Check direct literal package declarations in selected local sources; packages "
            "loaded by system classes are outside this scan."
        ),
        tests=(
            "tests.test_manuscript.ManuscriptTests.test_forbidden_packages_check_direct_local_graph_declarations",
        ),
        fix="Remove or replace the listed forbidden package declarations and rebuild.",
        reviewable=True,
        not_checked_area="forbidden_packages",
    ),
    Rule(
        code="TEX104",
        name="manuscript.abstract_required",
        title="Required abstract",
        description="Require one supported abstract declaration when require_abstract is enabled.",
        tests=(
            "tests.test_manuscript.ManuscriptTests.test_required_abstract_rejects_missing_and_empty_content",
            "tests.test_manuscript.ManuscriptTests.test_invisible_abstract_commands_do_not_establish_nonempty_content",
            "tests.test_manuscript.ManuscriptTests.test_math_only_abstract_presence_is_inconclusive_but_word_count_is_explicit",
        ),
        fix="Add a nonempty abstract in a supported abstract declaration.",
        reviewable=True,
    ),
    Rule(
        code="TEX105",
        name="manuscript.abstract_words",
        title="Abstract word count",
        description=(
            "Apply explicit abstract_min_words/abstract_max_words to a lexical source "
            "count, excluding math and citation arguments. Each unresolved macro widens "
            "the reported word-count range by one word; only a range wholly inside or "
            "outside the limits is decisive."
        ),
        tests=(
            "tests.test_manuscript.ManuscriptTests.test_abstract_word_limits_use_documented_math_and_markup_counting",
            "tests.test_manuscript.ManuscriptTests.test_unresolved_abstract_macros_bound_the_word_count",
        ),
        fix="Edit the abstract to fit the configured word range, preserving its meaning.",
        reviewable=True,
    ),
    Rule(
        code="TEX106",
        name="manuscript.keyword_count",
        title="Keyword count",
        description=(
            "Apply keywords_min_count/keywords_max_count to supported keyword declarations "
            "with explicit separators."
        ),
        tests=(
            "tests.test_manuscript.ManuscriptTests.test_keyword_limits_count_phrases_and_common_environments",
        ),
        fix="Adjust the keyword list to the configured range using explicit separators.",
        reviewable=True,
    ),
    Rule(
        code="TEX107",
        name="manuscript.required_sections",
        title="Required section headings",
        description=(
            "Check required_sections against literal headings after normalizing whitespace "
            "and case. Headings inside executed conditionals or with unresolved titles are "
            "reported as unconfirmed rather than missing; content adequacy is not assessed."
        ),
        tests=(
            "tests.test_manuscript.ManuscriptTests.test_required_sections_match_literal_headings_not_body_claims",
            "tests.test_manuscript.ManuscriptTests.test_endinput_stops_heading_and_dependency_search_in_included_file",
            "tests.test_manuscript.ManuscriptTests.test_endinput_same_line_conditional_and_macro_contexts_are_inconclusive",
            "tests.test_manuscript.ManuscriptTests.test_executed_conditional_still_blocks_the_declarations_it_encloses",
        ),
        fix="Add the required section headings and their content, or correct their spelling.",
        reviewable=True,
    ),
    Rule(
        code="PKG001",
        name="flatten.identity_collision",
        aliases=("flatten-identity-collision",),
        title="Colliding protected filenames",
        description=(
            "Block flattening when class, style, bibliography or root filenames cannot "
            "retain their required identities."
        ),
        tests=("tests.test_source.SourceTests.test_local_style_collision_is_blocked",),
        fix="Keep the hierarchy, or resolve colliding protected filenames in the source project.",
        mandatory=True,
    ),
    Rule(
        code="PKG002",
        name="flatten.identity_name",
        aliases=("flatten-identity-name",),
        title="Nonportable protected filenames",
        description=(
            "Block flattening when sanitizing a protected filename would change its naming "
            "contract."
        ),
        tests=("tests.test_source.SourceTests.test_protected_filename_cannot_be_sanitized",),
        fix="Keep the hierarchy, or explicitly rename the protected file and its references.",
        mandatory=True,
    ),
    Rule(
        code="PKG003",
        name="flatten.system_shadowing",
        aliases=("flatten-system-shadowing",),
        title="Relocation shadows a system file",
        description=(
            "Block flattening when relocating a local class/style would shadow a previously"
            " external dependency."
        ),
        tests=("tests.test_source.SourceTests.test_relocated_style_cannot_shadow_system_style",),
        fix=(
            "Keep the hierarchy, or resolve the local class/style name that would shadow a "
            "system file."
        ),
        mandatory=True,
    ),
    Rule(
        code="PKG004",
        name="flatten.companion_identity",
        aliases=("flatten-companion-identity",),
        title="Broken companion filename relationships",
        description=(
            "Block flattening that would break supported generated/sidecar filename relationships."
        ),
        tests=("tests.test_source.SourceTests.test_sidecar_relationship_breakage_is_blocked",),
        fix="Keep the hierarchy, or rename the companion files and their references together.",
        mandatory=True,
    ),
    Rule(
        code="PKG005",
        name="project.nonportable_filename",
        title="Nonportable project file names",
        description=(
            "Warn once per kept file or empty folder whose path contains a control character, "
            'a character from <>:"|?*, a trailing space or dot, or a reserved Windows device '
            "name. The file is kept unchanged."
        ),
        tests=(
            "tests.test_project.ProjectTests.test_nonportable_zip_names_are_kept_with_one_warning_per_file",
        ),
        fix="Rename the file to a portable name, or accept that it is awkward on other systems.",
    ),
    Rule(
        code="BIB001",
        name="bibliography.syntax",
        title="Bibliography syntax",
        description=(
            "Detect incomplete or malformed entries using the bounded local BibTeX scanner;"
            " do not partially normalize damaged files."
        ),
        tests=(
            "tests.test_bibliography.BibliographyTests.test_malformed_file_is_never_partially_normalized",
        ),
        fix="Repair the indicated entry syntax before applying bibliography transformations.",
        mandatory=True,
    ),
    Rule(
        code="BIB002",
        name="bibliography.duplicate_key",
        title="Duplicate citation keys",
        description=(
            "Report duplicate bibliography keys and conflicting fields without merging entries."
        ),
        tests=(
            "tests.test_bibliography.BibliographyTests.test_duplicate_keys_and_dois_include_conflicts_without_merging",
        ),
        fix="Review conflicting entries, choose distinct keys, and update affected citations.",
        mandatory=True,
    ),
    Rule(
        code="BIB003",
        name="bibliography.repeated_field",
        title="Repeated bibliography fields",
        description=(
            "Report repeated fields within an entry instead of selecting or normalizing an "
            "arbitrary value."
        ),
        tests=(
            "tests.test_bibliography.BibliographyTests.test_repeated_doi_fields_are_not_chosen_or_normalized",
        ),
        fix="Keep the intended field value and remove the duplicate field in this entry.",
    ),
    Rule(
        code="BIB004",
        name="bibliography.undefined_macro",
        title="Undefined bibliography string macros",
        description=(
            "Check string macros against local definitions, built-in month names and the "
            "macros of the named bibliography style (the journal macros of the standard styles, "
            "and the MACRO definitions of a project-shipped .bst file); incomplete parse "
            "coverage is inconclusive."
        ),
        tests=(
            "tests.test_bibliography.BibliographyTests.test_literal_relationships_and_builtin_months_are_checked",
        ),
        fix="Define the missing string macro or replace it with the intended literal text.",
        mandatory=True,
    ),
    Rule(
        code="BIB005",
        name="bibliography.missing_relationship",
        aliases=("bibliography.relationship_unresolved",),
        title="Bibliography relationship targets",
        description=(
            "Resolve supported cross-reference relationships locally; dynamic or "
            "incompletely parsed targets are inconclusive."
        ),
        tests=(
            "tests.test_bibliography.BibliographyTests.test_literal_relationships_and_builtin_months_are_checked",
        ),
        fix="Add the missing parent entry or correct the relationship target key.",
        mandatory=True,
    ),
    Rule(
        code="BIB006",
        name="bibliography.invalid_doi",
        aliases=("bibliography.doi_unresolved",),
        title="Literal DOI syntax",
        description=(
            "Validate supported literal DOI syntax; macros, concatenation and ambiguous "
            "markup are inconclusive. No online resolution is performed."
        ),
        tests=(
            "tests.test_bibliography.BibliographyTests.test_invalid_plain_dois_and_ambiguous_markup_are_distinct",
            "tests.test_bibliography.BibliographyTests.test_macro_and_concatenated_dois_remain_unchanged",
        ),
        fix="Check the DOI against the publication and correct its literal bibliography value.",
        reviewable=True,
    ),
    Rule(
        code="BIB007",
        name="bibliography.duplicate_doi",
        title="Exact duplicate DOI values",
        description=(
            "Detect exact duplicate normalized literal DOI values without folding case or "
            "merging entries."
        ),
        tests=(
            "tests.test_bibliography.BibliographyTests.test_duplicate_keys_and_dois_include_conflicts_without_merging",
            "tests.test_bibliography.BibliographyTests.test_doi_case_is_not_collapsed_for_exact_duplicate_detection",
        ),
        fix=(
            "Review whether the entries describe the same work before merging or changing "
            "citations."
        ),
        reviewable=True,
    ),
    Rule(
        code="BIB008",
        name="bibliography.input",
        title="Unreadable bibliography files",
        description=(
            "Report a .bib file that is a symbolic link, cannot be read, is not UTF-8 or "
            "exceeds the bibliography size limit. It is neither inspected nor normalized, so "
            "the checks over its entries are incomplete."
        ),
        tests=(
            "tests.test_bibliography.BibliographyTests.test_non_utf8_and_symbolic_link_inputs_are_not_modified",
        ),
        fix=(
            "Make the file readable, save it as UTF-8 and keep it within the size limit, or "
            "supply the actual file instead of a link."
        ),
        mandatory=True,
    ),
    Rule(
        code="BIB009",
        name="bibliography.source_error",
        title="Bibliography checks incomplete after a source error",
        description=(
            "Replace the inconclusive bibliography checks whose only cause is a source error "
            "reported elsewhere, such as a missing file or an ambiguous main file, with one "
            "note that lists them, instead of repeating the cause once per check."
        ),
        tests=(
            "tests.test_report_cascades.BibliographyCascadeTests.test_missing_figure_yields_one_bibliography_note",
            "tests.test_report_cascades.BibliographyCascadeTests.test_ambiguous_main_yields_one_bibliography_note",
        ),
        fix="Fix the reported source error; these bibliography checks then run in full.",
        mandatory=True,
    ),
    Rule(
        code="BLD001",
        name="build.undefined_citation",
        title="Undefined citations in build log",
        description=(
            "Detect TeX's undefined-citation diagnostics; this does not check whether every"
            " bibliography entry is cited."
        ),
        tests=("tests.test_runtime.BuildTests.test_build_log_check_codes_match_diagnostics",),
        fix="Correct the citation key or supply its bibliography entry, then rebuild.",
    ),
    Rule(
        code="BLD002",
        name="build.undefined_reference",
        title="Undefined references in build log",
        description="Detect TeX's undefined-reference diagnostics after the build.",
        tests=("tests.test_runtime.BuildTests.test_build_log_check_codes_match_diagnostics",),
        fix="Correct the reference key or add its intended label, then rebuild.",
    ),
    Rule(
        code="BLD003",
        name="build.duplicate_label",
        title="Duplicate labels in build log",
        description="Detect multiply-defined label diagnostics emitted by TeX.",
        tests=("tests.test_runtime.BuildTests.test_build_log_check_codes_match_diagnostics",),
        fix="Rename duplicate labels and update references to the intended objects.",
    ),
    Rule(
        code="BLD004",
        name="build.missing_character",
        title="Missing characters in build log",
        description=(
            "Detect missing-character diagnostics emitted by TeX; no visual glyph "
            "recognition is performed."
        ),
        tests=("tests.test_runtime.BuildTests.test_build_log_check_codes_match_diagnostics",),
        fix=(
            "Use a font/engine supporting the reported character or its intended TeX "
            "representation."
        ),
    ),
    Rule(
        code="BLD005",
        name="build.font_substitution",
        title="Font warnings in build log",
        description="Detect font substitution and other TeX font warnings.",
        tests=("tests.test_runtime.BuildTests.test_build_log_check_codes_match_diagnostics",),
        fix="Use an available font and shape; check the rebuilt PDF for unintended substitution.",
        reviewable=True,
    ),
    Rule(
        code="BLD006",
        name="build.overfull_box",
        title="Overfull boxes in build log",
        description=(
            "Detect overfull horizontal/vertical boxes and extract reported point overflow;"
            " no threshold is assumed."
        ),
        tests=(
            "tests.test_runtime.BuildTests.test_build_log_check_codes_match_diagnostics",
            "tests.test_runtime.BuildTests.test_diagnostics_do_not_invent_source_locations",
        ),
        fix=(
            "Inspect the reported box; reflow the text, equation or table within the "
            "intended margins."
        ),
        reviewable=True,
    ),
    Rule(
        code="BLD007",
        name="build.underfull_box",
        title="Underfull boxes in build log",
        description="Detect underfull horizontal/vertical box warnings emitted by TeX.",
        tests=("tests.test_runtime.BuildTests.test_build_log_check_codes_match_diagnostics",),
        fix="Inspect the reported box for poor spacing and adjust its line or page breaks.",
        reviewable=True,
    ),
    Rule(
        code="BLD008",
        name="build.rerun_required",
        title="Unresolved rebuild requests",
        description="Block success when the final build log still requests a rerun.",
        tests=(
            "tests.test_runtime.BuildTests.test_build_log_check_codes_match_diagnostics",
            "tests.test_runtime.BuildTests.test_final_rerun_request_blocks_success",
        ),
        fix=(
            "Resolve the remaining reference/bibliography rerun request and rebuild to convergence."
        ),
        mandatory=True,
    ),
    Rule(
        code="BLD009",
        name="build.tex_error",
        title="TeX errors in build log",
        description="Detect explicit TeX error, fatal-error and emergency-stop diagnostics.",
        tests=("tests.test_runtime.BuildTests.test_build_log_check_codes_match_diagnostics",),
        fix="Fix the reported TeX error and repeat the clean build.",
        mandatory=True,
    ),
    Rule(
        code="BLD010",
        name="build.external_dependency",
        title="Undeclared external build inputs",
        description=(
            "Report a file outside the project and the declared TeX trees that the build "
            "recorder shows the TeX run read. The build is not reproducible from the project "
            "alone, so it does not succeed."
        ),
        tests=("tests.test_runtime.BuildTests.test_external_recorder_dependency_blocks_success",),
        fix=(
            "Copy the file into the project and refer to it by a project-relative path, or "
            "remove the dependency, then rebuild."
        ),
        mandatory=True,
    ),
    Rule(
        code="PDF001",
        name="pdf.page_limit",
        title="Total PDF page limit",
        description=(
            "Compare the full page count, including references and appendices, with "
            "explicit max_pages."
        ),
        tests=("tests.test_pdf.PdfConstraintTests.test_page_limit_boundary_and_overflow",),
        fix=(
            "Reduce the document to the configured total page limit, including appendices "
            "and references."
        ),
        reviewable=True,
        not_checked_area="page_limit",
    ),
    Rule(
        code="PDF101",
        name="pdf.page_dimensions",
        title="Expected PDF page dimensions",
        description=(
            "Compare each page's reported dimensions in PDF points with "
            "expected_page_width_pt/expected_page_height_pt using page_size_tolerance_pt."
        ),
        tests=(
            "tests.test_pdf.PdfConstraintTests.test_expected_dimensions_measure_every_page_with_pdf_point_tolerance",
        ),
        fix="Set the intended paper dimensions in the source and rebuild the PDF.",
        reviewable=True,
    ),
    Rule(
        code="PDF102",
        name="pdf.page_size_consistency",
        title="Consistent PDF page dimensions",
        description=(
            "Compare the full range of page dimensions using page_size_tolerance_pt when "
            "require_consistent_page_size is enabled."
        ),
        tests=(
            "tests.test_pdf.PdfConstraintTests.test_page_consistency_uses_full_range_not_just_first_page",
        ),
        fix="Make the page sizes consistent, checking imported PDF pages and orientation settings.",
        reviewable=True,
    ),
    Rule(
        code="PDF103",
        name="pdf.image_resolution",
        title="Minimum effective image resolution",
        description=(
            "Compare Poppler's reported x/y raster PPI at embedded scale with explicit "
            "min_image_dpi; vector art and figure text are outside this check."
        ),
        tests=(
            "tests.test_pdf.PdfConstraintTests.test_image_resolution_checks_both_axes_and_repeated_placements",
        ),
        fix=(
            "Replace the reported raster with a higher-resolution original or reduce its "
            "rendered size."
        ),
        reviewable=True,
        not_checked_area="image_resolution",
    ),
    Rule(
        code="PDF104",
        name="pdf.font_embedding",
        title="Embedded PDF fonts",
        description=(
            "Require every font reported by pdffonts to be embedded when "
            "require_embedded_fonts is enabled."
        ),
        tests=(
            "tests.test_pdf.PdfConstraintTests.test_font_embedding_violation_changes_status_and_zero_fonts_are_inconclusive",
        ),
        fix="Embed the named fonts when exporting figures or building the document, then rebuild.",
        reviewable=True,
        not_checked_area="font_embedding",
    ),
    Rule(
        code="PDF105",
        name="pdf.type3_fonts",
        title="Type 3 PDF fonts",
        description=(
            "Reject fonts reported as Type 3 when forbid_type3_fonts is enabled; Type 3 "
            "does not necessarily mean bitmap."
        ),
        tests=(
            "tests.test_pdf.PdfConstraintTests.test_forbidden_type3_font_policy_detects_only_type3",
        ),
        fix="Replace the reported Type 3 fonts with suitable embedded outline fonts and rebuild.",
        reviewable=True,
        not_checked_area="font_embedding",
    ),
    Rule(
        code="PDF106",
        name="pdf.encryption_policy",
        title="PDF encryption",
        description=(
            "Reject encryption reported by pdfinfo when forbid_encryption is enabled; "
            "inaccessible metadata is inconclusive."
        ),
        tests=(
            "tests.test_pdf.PdfConstraintTests.test_encryption_policy_uses_explicit_reported_feature",
        ),
        fix="Export an unencrypted PDF from the source and repeat inspection.",
    ),
    Rule(
        code="PDF107",
        name="pdf.forms_policy",
        title="PDF forms",
        description="Reject forms reported by pdfinfo when forbid_forms is enabled.",
        tests=("tests.test_pdf.PdfConstraintTests.test_forms_policy_detects_acroform_and_xfa",),
        fix="Remove or flatten form fields when exporting the PDF, then inspect it again.",
    ),
    Rule(
        code="PDF108",
        name="pdf.javascript_policy",
        title="PDF JavaScript",
        description=(
            "Reject JavaScript reported by pdfinfo when forbid_javascript is enabled; this "
            "is not exhaustive active-content analysis."
        ),
        tests=(
            "tests.test_pdf.PdfConstraintTests.test_javascript_policy_requires_explicit_reported_value",
        ),
        fix="Export the document without embedded JavaScript and inspect it again.",
    ),
    Rule(
        code="PDF109",
        name="pdf.attachments_policy",
        title="Embedded PDF attachments",
        description=(
            "Reject embedded files reported by pdfdetach when forbid_attachments is enabled."
        ),
        tests=("tests.test_pdf.PdfConstraintTests.test_attachments_policy_counts_embedded_files",),
        fix="Remove embedded file attachments from the exported PDF and inspect it again.",
    ),
    Rule(
        code="CMP001",
        name="compare.page_count",
        title="PDF comparison page count",
        description="Compare the number of pages in the two supplied PDFs.",
        tests=("tests.test_pdf.PdfTests.test_page_count_difference_identifies_missing_pages",),
        fix=(
            "Check the selected source/PDF versions and explain or correct the differing "
            "page count."
        ),
        mandatory=True,
        reviewable=True,
    ),
    Rule(
        code="CMP002",
        name="compare.text",
        title="PDF comparison extracted text",
        description=(
            "Compare per-page Poppler text extraction; this is not proof of semantic equivalence."
        ),
        tests=(
            "tests.test_pdf.PdfTests.test_extracted_text_change_is_detected_when_raster_matches",
        ),
        fix="Inspect changed pages and restore the intended text before publishing the bundle.",
        mandatory=True,
        reviewable=True,
    ),
    Rule(
        code="CMP003",
        name="compare.rendering",
        title="PDF comparison rendered pages",
        description=(
            "Compare exact RGB renders at 144 DPI within bounded page/pixel limits; smaller"
            " details may escape detection."
        ),
        tests=(
            "tests.test_pdf.PdfTests.test_rendered_change_is_detected_when_text_matches",
            "tests.test_pdf.PdfTests.test_matching_comparison_states_exact_tolerance_and_limits",
        ),
        fix="Inspect changed pages and correct unintended visual differences before publishing.",
        mandatory=True,
        reviewable=True,
    ),
    Rule(
        code="FMT001",
        name="source.formatting",
        title="Source formatting differences",
        description=(
            "Report differences from explicitly configured tex-fmt output without modifying"
            " the original project."
        ),
        tests=(
            "tests.test_formatting.FormattingTests.test_formatting_differences_preserve_original",
        ),
        fix=(
            "Review the formatting diff; use prepare --format to apply it to a separate "
            "verified copy."
        ),
        reviewable=True,
    ),
    Rule(
        code="FMT002",
        name="source.format_idempotence",
        title="Formatter idempotence",
        description="Reject a formatting proposal when a second tex-fmt pass changes it again.",
        tests=("tests.test_formatting.FormattingTests.test_only_idempotent_output_is_proposed",),
        fix=(
            "Inspect the unstable formatter output or change formatter settings/version "
            "before retrying."
        ),
        mandatory=True,
    ),
)
