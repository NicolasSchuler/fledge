"""Stable public check codes, descriptions, and behavioral test associations.

Codes are assigned explicitly and are never recycled. Execution diagnostics and
raw inventories are not checks and intentionally have no code. Existing internal
rule names remain in reports for compatibility and more detailed context.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from types import MappingProxyType
from typing import Any, cast

from .bibliography_check_rules import RULE_DEFINITIONS as BIBLIOGRAPHY_RULES
from .bibliography_transform_rules import RULE_DEFINITIONS as BIBLIOGRAPHY_TRANSFORM_RULES
from .build_check_rules import RULE_DEFINITIONS as BUILD_RULES
from .loaded_options_rules import RULE_DEFINITIONS as LOADED_OPTIONS_RULES
from .manuscript_check_rules import RULE_DEFINITIONS as MANUSCRIPT_RULES
from .metadata_privacy_rules import RULE_DEFINITIONS as METADATA_PRIVACY_RULES
from .online_check_rules import RULE_DEFINITIONS as ONLINE_RULES
from .pdf_artwork_rules import RULE_DEFINITIONS as PDF_ARTWORK_RULES
from .pdf_check_rules import RULE_DEFINITIONS as PDF_RULES
from .reporting_rules import RULE_DEFINITIONS as REPORTING_RULES
from .runtime_rules import RULE_DEFINITIONS as RUNTIME_RULES
from .source_transform_rules import SOURCE_TRANSFORM_RULES
from .structure_rules import RULE_DEFINITIONS as STRUCTURE_RULES
from .submission_check_rules import RULE_DEFINITIONS as SUBMISSION_RULES
from .workflow_rules import WORKFLOW_RULES


@dataclass(frozen=True)
class Rule:
    code: str
    name: str
    title: str
    description: str
    tests: tuple[str, ...]
    aliases: tuple[str, ...] = ()

    @property
    def fix(self) -> str:
        return _FIXES[self.code]

    def to_dict(self) -> dict[str, object]:
        return {**asdict(self), "fix": self.fix}


def _tests(module: str, suite: str, *names: str) -> tuple[str, ...]:
    return tuple(f"tests.test_{module}.{suite}.{name}" for name in names)


RULES = (
    Rule(
        "TEX001",
        "source-edit-marker",
        "Unfinished text markers",
        "Find TODO, FIXME, XXX and ?? in active source; comments and literal examples are "
        "ignored. Advisory heuristic.",
        _tests("source", "SourceTests", "test_markers_labels_and_refs_are_heuristics_not_blockers"),
    ),
    Rule(
        "TEX002",
        "source-edit-command",
        "Editing commands",
        "Find supported draft/editing commands such as todo in active source. Advisory heuristic.",
        _tests("source", "SourceTests", "test_markers_labels_and_refs_are_heuristics_not_blockers"),
    ),
    Rule(
        "TEX003",
        "source-duplicate-label",
        "Duplicate literal labels",
        "Find repeated literal labels in the selected source graph without expanding TeX "
        "macros. Advisory heuristic.",
        _tests("source", "SourceTests", "test_markers_labels_and_refs_are_heuristics_not_blockers"),
    ),
    Rule(
        "TEX004",
        "source-unresolved-reference",
        "Unresolved literal references",
        "Find literal references without a matching scanned label. Generated labels may make "
        "this inconclusive; advisory heuristic.",
        _tests("source", "SourceTests", "test_markers_labels_and_refs_are_heuristics_not_blockers"),
    ),
    Rule(
        "TEX005",
        "source-missing-dependency",
        "Missing source dependencies",
        "Detect absent files named by supported literal source commands.",
        _tests(
            "source", "SourceTests", "test_absolute_outside_and_missing_dependencies_are_errors"
        ),
    ),
    Rule(
        "TEX006",
        "source-external-path",
        "External source paths",
        "Reject absolute paths and literal file references escaping the imported project.",
        _tests(
            "source", "SourceTests", "test_absolute_outside_and_missing_dependencies_are_errors"
        ),
    ),
    Rule(
        "TEX007",
        "source-case-mismatch",
        "Dependency filename case",
        "Detect case mismatches without guessing which file a case-sensitive submission "
        "system will load.",
        _tests("source", "SourceTests", "test_case_mismatch_is_reported_and_not_guessed"),
    ),
    Rule(
        "TEX008",
        "source-ambiguous-dependency",
        "Ambiguous source dependencies",
        "Detect multiple candidate files for one supported literal source reference.",
        _tests(
            "source",
            "SourceTests",
            "test_extensionless_graphics_with_multiple_extensions_are_ambiguous",
        ),
    ),
    Rule(
        "TEX101",
        "manuscript.document_class",
        "Expected document class",
        "Compare the selected document's literal class declaration with expected_document_class.",
        _tests(
            "manuscript",
            "ManuscriptTests",
            "test_expected_document_class_matches_and_mismatch_blocks",
        ),
    ),
    Rule(
        "TEX102",
        "manuscript.class_options",
        "Document class options",
        "Check required_class_options and forbidden_class_options in literal class declarations.",
        _tests(
            "manuscript",
            "ManuscriptTests",
            "test_class_options_report_missing_and_forbidden_values",
        ),
    ),
    Rule(
        "TEX103",
        "manuscript.forbidden_packages",
        "Forbidden package declarations",
        "Check direct literal package declarations in selected local sources; packages loaded "
        "by system classes are outside this scan.",
        _tests(
            "manuscript",
            "ManuscriptTests",
            "test_forbidden_packages_check_direct_local_graph_declarations",
        ),
    ),
    Rule(
        "TEX104",
        "manuscript.abstract_required",
        "Required abstract",
        "Require one supported abstract declaration when require_abstract is enabled.",
        _tests(
            "manuscript",
            "ManuscriptTests",
            "test_required_abstract_rejects_missing_and_empty_content",
            "test_invisible_abstract_commands_do_not_establish_nonempty_content",
            "test_math_only_abstract_presence_is_inconclusive_but_word_count_is_explicit",
        ),
    ),
    Rule(
        "TEX105",
        "manuscript.abstract_words",
        "Abstract word count",
        "Apply explicit abstract_min_words/abstract_max_words to a lexical source count, "
        "excluding math and citation arguments. Unexpanded macros are inconclusive.",
        _tests(
            "manuscript",
            "ManuscriptTests",
            "test_abstract_word_limits_use_documented_math_and_markup_counting",
        ),
    ),
    Rule(
        "TEX106",
        "manuscript.keyword_count",
        "Keyword count",
        "Apply keywords_min_count/keywords_max_count to supported keyword declarations with "
        "explicit separators.",
        _tests(
            "manuscript",
            "ManuscriptTests",
            "test_keyword_limits_count_phrases_and_common_environments",
        ),
    ),
    Rule(
        "TEX107",
        "manuscript.required_sections",
        "Required section headings",
        "Check required_sections against literal headings after normalizing whitespace and "
        "case; content adequacy is not assessed.",
        _tests(
            "manuscript",
            "ManuscriptTests",
            "test_required_sections_match_literal_headings_not_body_claims",
            "test_endinput_stops_heading_and_dependency_search_in_included_file",
            "test_endinput_same_line_conditional_and_macro_contexts_are_inconclusive",
        ),
    ),
    Rule(
        "PKG001",
        "flatten-identity-collision",
        "Colliding protected filenames",
        "Block flattening when class, style, bibliography or root filenames cannot retain "
        "their required identities.",
        _tests("source", "SourceTests", "test_local_style_collision_is_blocked"),
    ),
    Rule(
        "PKG002",
        "flatten-identity-name",
        "Nonportable protected filenames",
        "Block flattening when sanitizing a protected filename would change its naming contract.",
        _tests("source", "SourceTests", "test_protected_filename_cannot_be_sanitized"),
    ),
    Rule(
        "PKG003",
        "flatten-system-shadowing",
        "Relocation shadows a system file",
        "Block flattening when relocating a local class/style would shadow a previously "
        "external dependency.",
        _tests("source", "SourceTests", "test_relocated_style_cannot_shadow_system_style"),
    ),
    Rule(
        "PKG004",
        "flatten-companion-identity",
        "Broken companion filename relationships",
        "Block flattening that would break supported generated/sidecar filename relationships.",
        _tests("source", "SourceTests", "test_sidecar_relationship_breakage_is_blocked"),
    ),
    Rule(
        "BIB001",
        "bibliography.syntax",
        "Bibliography syntax",
        "Detect incomplete or malformed entries using the bounded local BibTeX scanner; do "
        "not partially normalize damaged files.",
        _tests(
            "bibliography", "BibliographyTests", "test_malformed_file_is_never_partially_normalized"
        ),
    ),
    Rule(
        "BIB002",
        "bibliography.duplicate_key",
        "Duplicate citation keys",
        "Report duplicate bibliography keys and conflicting fields without merging entries.",
        _tests(
            "bibliography",
            "BibliographyTests",
            "test_duplicate_keys_and_dois_include_conflicts_without_merging",
        ),
    ),
    Rule(
        "BIB003",
        "bibliography.repeated_field",
        "Repeated bibliography fields",
        "Report repeated fields within an entry instead of selecting or normalizing an "
        "arbitrary value.",
        _tests(
            "bibliography",
            "BibliographyTests",
            "test_repeated_doi_fields_are_not_chosen_or_normalized",
        ),
    ),
    Rule(
        "BIB004",
        "bibliography.undefined_macro",
        "Undefined bibliography string macros",
        "Check string macros against local definitions and built-in month names; incomplete "
        "parse coverage is inconclusive.",
        _tests(
            "bibliography",
            "BibliographyTests",
            "test_literal_relationships_and_builtin_months_are_checked",
        ),
    ),
    Rule(
        "BIB005",
        "bibliography.missing_relationship",
        "Bibliography relationship targets",
        "Resolve supported cross-reference relationships locally; dynamic or incompletely "
        "parsed targets are inconclusive.",
        _tests(
            "bibliography",
            "BibliographyTests",
            "test_literal_relationships_and_builtin_months_are_checked",
        ),
        aliases=("bibliography.relationship_unresolved",),
    ),
    Rule(
        "BIB006",
        "bibliography.invalid_doi",
        "Literal DOI syntax",
        "Validate supported literal DOI syntax; macros, concatenation and ambiguous markup "
        "are inconclusive. No online resolution is performed.",
        _tests(
            "bibliography",
            "BibliographyTests",
            "test_invalid_plain_dois_and_ambiguous_markup_are_distinct",
            "test_macro_and_concatenated_dois_remain_unchanged",
        ),
        aliases=("bibliography.doi_unresolved",),
    ),
    Rule(
        "BIB007",
        "bibliography.duplicate_doi",
        "Exact duplicate DOI values",
        "Detect exact duplicate normalized literal DOI values without folding case or merging "
        "entries.",
        _tests(
            "bibliography",
            "BibliographyTests",
            "test_duplicate_keys_and_dois_include_conflicts_without_merging",
            "test_doi_case_is_not_collapsed_for_exact_duplicate_detection",
        ),
    ),
    Rule(
        "BLD001",
        "build.undefined_citation",
        "Undefined citations in build log",
        "Detect TeX's undefined-citation diagnostics; this does not check whether every "
        "bibliography entry is cited.",
        _tests("runtime", "BuildTests", "test_build_log_check_codes_match_diagnostics"),
    ),
    Rule(
        "BLD002",
        "build.undefined_reference",
        "Undefined references in build log",
        "Detect TeX's undefined-reference diagnostics after the build.",
        _tests("runtime", "BuildTests", "test_build_log_check_codes_match_diagnostics"),
    ),
    Rule(
        "BLD003",
        "build.duplicate_label",
        "Duplicate labels in build log",
        "Detect multiply-defined label diagnostics emitted by TeX.",
        _tests("runtime", "BuildTests", "test_build_log_check_codes_match_diagnostics"),
    ),
    Rule(
        "BLD004",
        "build.missing_character",
        "Missing characters in build log",
        "Detect missing-character diagnostics emitted by TeX; no visual glyph recognition is "
        "performed.",
        _tests("runtime", "BuildTests", "test_build_log_check_codes_match_diagnostics"),
    ),
    Rule(
        "BLD005",
        "build.font_substitution",
        "Font warnings in build log",
        "Detect font substitution and other TeX font warnings.",
        _tests("runtime", "BuildTests", "test_build_log_check_codes_match_diagnostics"),
    ),
    Rule(
        "BLD006",
        "build.overfull_box",
        "Overfull boxes in build log",
        "Detect overfull horizontal/vertical boxes and extract reported point overflow; no "
        "threshold is assumed.",
        _tests(
            "runtime",
            "BuildTests",
            "test_build_log_check_codes_match_diagnostics",
            "test_diagnostics_do_not_invent_source_locations",
        ),
    ),
    Rule(
        "BLD007",
        "build.underfull_box",
        "Underfull boxes in build log",
        "Detect underfull horizontal/vertical box warnings emitted by TeX.",
        _tests("runtime", "BuildTests", "test_build_log_check_codes_match_diagnostics"),
    ),
    Rule(
        "BLD008",
        "build.rerun_required",
        "Unresolved rebuild requests",
        "Block success when the final build log still requests a rerun.",
        _tests(
            "runtime",
            "BuildTests",
            "test_build_log_check_codes_match_diagnostics",
            "test_final_rerun_request_blocks_success",
        ),
    ),
    Rule(
        "BLD009",
        "build.tex_error",
        "TeX errors in build log",
        "Detect explicit TeX error, fatal-error and emergency-stop diagnostics.",
        _tests("runtime", "BuildTests", "test_build_log_check_codes_match_diagnostics"),
    ),
    Rule(
        "PDF001",
        "pdf.page_limit",
        "Total PDF page limit",
        "Compare the full page count, including references and appendices, with explicit "
        "max_pages.",
        _tests("pdf", "PdfConstraintTests", "test_page_limit_boundary_and_overflow"),
    ),
    Rule(
        "PDF101",
        "pdf.page_dimensions",
        "Expected PDF page dimensions",
        "Compare each page's reported dimensions in PDF points with "
        "expected_page_width_pt/expected_page_height_pt using page_size_tolerance_pt.",
        _tests(
            "pdf",
            "PdfConstraintTests",
            "test_expected_dimensions_measure_every_page_with_pdf_point_tolerance",
        ),
    ),
    Rule(
        "PDF102",
        "pdf.page_size_consistency",
        "Consistent PDF page dimensions",
        "Compare the full range of page dimensions using page_size_tolerance_pt when "
        "require_consistent_page_size is enabled.",
        _tests(
            "pdf", "PdfConstraintTests", "test_page_consistency_uses_full_range_not_just_first_page"
        ),
    ),
    Rule(
        "PDF103",
        "pdf.image_resolution",
        "Minimum effective image resolution",
        "Compare Poppler's reported x/y raster PPI at embedded scale with explicit "
        "min_image_dpi; vector art and figure text are outside this check.",
        _tests(
            "pdf",
            "PdfConstraintTests",
            "test_image_resolution_checks_both_axes_and_repeated_placements",
        ),
    ),
    Rule(
        "PDF104",
        "pdf.font_embedding",
        "Embedded PDF fonts",
        "Require every font reported by pdffonts to be embedded when require_embedded_fonts "
        "is enabled.",
        _tests(
            "pdf",
            "PdfConstraintTests",
            "test_font_embedding_violation_changes_status_and_zero_fonts_are_inconclusive",
        ),
    ),
    Rule(
        "PDF105",
        "pdf.type3_fonts",
        "Type 3 PDF fonts",
        "Reject fonts reported as Type 3 when forbid_type3_fonts is enabled; Type 3 does not "
        "necessarily mean bitmap.",
        _tests("pdf", "PdfConstraintTests", "test_forbidden_type3_font_policy_detects_only_type3"),
    ),
    Rule(
        "PDF106",
        "pdf.encryption_policy",
        "PDF encryption",
        "Reject encryption reported by pdfinfo when forbid_encryption is enabled; "
        "inaccessible metadata is inconclusive.",
        _tests(
            "pdf", "PdfConstraintTests", "test_encryption_policy_uses_explicit_reported_feature"
        ),
    ),
    Rule(
        "PDF107",
        "pdf.forms_policy",
        "PDF forms",
        "Reject forms reported by pdfinfo when forbid_forms is enabled.",
        _tests("pdf", "PdfConstraintTests", "test_forms_policy_detects_acroform_and_xfa"),
    ),
    Rule(
        "PDF108",
        "pdf.javascript_policy",
        "PDF JavaScript",
        "Reject JavaScript reported by pdfinfo when forbid_javascript is enabled; this is not "
        "exhaustive active-content analysis.",
        _tests(
            "pdf", "PdfConstraintTests", "test_javascript_policy_requires_explicit_reported_value"
        ),
    ),
    Rule(
        "PDF109",
        "pdf.attachments_policy",
        "Embedded PDF attachments",
        "Reject embedded files reported by pdfdetach when forbid_attachments is enabled.",
        _tests("pdf", "PdfConstraintTests", "test_attachments_policy_counts_embedded_files"),
    ),
    Rule(
        "CMP001",
        "compare.page_count",
        "PDF comparison page count",
        "Compare the number of pages in the two supplied PDFs.",
        _tests("pdf", "PdfTests", "test_page_count_difference_identifies_missing_pages"),
    ),
    Rule(
        "CMP002",
        "compare.text",
        "PDF comparison extracted text",
        "Compare per-page Poppler text extraction; this is not proof of semantic equivalence.",
        _tests("pdf", "PdfTests", "test_extracted_text_change_is_detected_when_raster_matches"),
    ),
    Rule(
        "CMP003",
        "compare.rendering",
        "PDF comparison rendered pages",
        "Compare exact RGB renders at 144 DPI within bounded page/pixel limits; smaller "
        "details may escape detection.",
        _tests(
            "pdf",
            "PdfTests",
            "test_rendered_change_is_detected_when_text_matches",
            "test_matching_comparison_states_exact_tolerance_and_limits",
        ),
    ),
    Rule(
        "FMT001",
        "source.formatting",
        "Source formatting differences",
        "Report differences from explicitly configured tex-fmt output without modifying the "
        "original project.",
        _tests("formatting", "FormattingTests", "test_formatting_differences_preserve_original"),
    ),
    Rule(
        "FMT002",
        "source.format_idempotence",
        "Formatter idempotence",
        "Reject a formatting proposal when a second tex-fmt pass changes it again.",
        _tests("formatting", "FormattingTests", "test_only_idempotent_output_is_proposed"),
    ),
)

_additional: tuple[dict[str, Any], ...] = (
    *BIBLIOGRAPHY_RULES,
    *BUILD_RULES,
    *LOADED_OPTIONS_RULES,
    *MANUSCRIPT_RULES,
    *METADATA_PRIVACY_RULES,
    *ONLINE_RULES,
    *PDF_RULES,
    *SUBMISSION_RULES,
    *STRUCTURE_RULES,
    *WORKFLOW_RULES,
    *REPORTING_RULES,
    *RUNTIME_RULES,
    *SOURCE_TRANSFORM_RULES,
    *BIBLIOGRAPHY_TRANSFORM_RULES,
    *PDF_ARTWORK_RULES,
)
RULES += tuple(
    Rule(
        cast(str, definition["code"]),
        cast(str, definition["name"]),
        cast(str, definition["title"]),
        cast(str, definition["description"]),
        tuple(definition["tests"]),
    )
    for definition in _additional
)

_FIXES: dict[str, str] = {
    "TEX001": "Finish or remove the marked draft text after reviewing its context.",
    "TEX002": "Resolve the editing note and remove its draft command.",
    "TEX003": "Give distinct objects unique labels and update their references.",
    "TEX004": "Correct the reference key or add the intended label; confirm with a clean build.",
    "TEX005": "Include the missing dependency or correct the literal file path.",
    "TEX006": "Copy the dependency into the project and use a project-relative path.",
    "TEX007": "Make the reference match the filename's exact letter case.",
    "TEX008": "Use an explicit unambiguous file path and extension.",
    "TEX101": "Use the configured document class, or correct the expected class in your settings.",
    "TEX102": "Add the missing class options and remove the forbidden options listed above.",
    "TEX103": "Remove or replace the listed forbidden package declarations and rebuild.",
    "TEX104": "Add a nonempty abstract in a supported abstract declaration.",
    "TEX105": "Edit the abstract to fit the configured word range, preserving its meaning.",
    "TEX106": "Adjust the keyword list to the configured range using explicit separators.",
    "TEX107": "Add the required section headings and their content, or correct their spelling.",
    "PKG001": "Keep the hierarchy, or resolve colliding protected filenames in the source project.",
    "PKG002": "Keep the hierarchy, or explicitly rename the protected file and its references.",
    "PKG003": (
        "Keep the hierarchy, or resolve the local class/style name that would shadow a system file."
    ),
    "PKG004": "Keep the hierarchy, or rename the companion files and their references together.",
    "BIB001": "Repair the indicated entry syntax before applying bibliography transformations.",
    "BIB002": "Review conflicting entries, choose distinct keys, and update affected citations.",
    "BIB003": "Keep the intended field value and remove the duplicate field in this entry.",
    "BIB004": "Define the missing string macro or replace it with the intended literal text.",
    "BIB005": "Add the missing parent entry or correct the relationship target key.",
    "BIB006": "Check the DOI against the publication and correct its literal bibliography value.",
    "BIB007": (
        "Review whether the entries describe the same work before merging or changing citations."
    ),
    "BLD001": "Correct the citation key or supply its bibliography entry, then rebuild.",
    "BLD002": "Correct the reference key or add its intended label, then rebuild.",
    "BLD003": "Rename duplicate labels and update references to the intended objects.",
    "BLD004": (
        "Use a font/engine supporting the reported character or its intended TeX representation."
    ),
    "BLD005": "Use an available font and shape; check the rebuilt PDF for unintended substitution.",
    "BLD006": (
        "Inspect the reported box; reflow the text, equation or table within the intended margins."
    ),
    "BLD007": "Inspect the reported box for poor spacing and adjust its line or page breaks.",
    "BLD008": (
        "Resolve the remaining reference/bibliography rerun request and rebuild to convergence."
    ),
    "BLD009": "Fix the reported TeX error and repeat the clean build.",
    "PDF001": (
        "Reduce the document to the configured total page limit, including appendices and "
        "references."
    ),
    "PDF101": "Set the intended paper dimensions in the source and rebuild the PDF.",
    "PDF102": (
        "Make the page sizes consistent, checking imported PDF pages and orientation settings."
    ),
    "PDF103": (
        "Replace the reported raster with a higher-resolution original or reduce its rendered size."
    ),
    "PDF104": (
        "Embed the named fonts when exporting figures or building the document, then rebuild."
    ),
    "PDF105": "Replace the reported Type 3 fonts with suitable embedded outline fonts and rebuild.",
    "PDF106": "Export an unencrypted PDF from the source and repeat inspection.",
    "PDF107": "Remove or flatten form fields when exporting the PDF, then inspect it again.",
    "PDF108": "Export the document without embedded JavaScript and inspect it again.",
    "PDF109": "Remove embedded file attachments from the exported PDF and inspect it again.",
    "CMP001": (
        "Check the selected source/PDF versions and explain or correct the differing page count."
    ),
    "CMP002": "Inspect changed pages and restore the intended text before publishing the bundle.",
    "CMP003": "Inspect changed pages and correct unintended visual differences before publishing.",
    "FMT001": (
        "Review the formatting diff; use prepare --format to apply it to a separate verified copy."
    ),
    "FMT002": (
        "Inspect the unstable formatter output or change formatter settings/version "
        "before retrying."
    ),
}

_FIXES.update(
    {cast(str, definition["code"]): cast(str, definition["fix"]) for definition in _additional}
)
BY_CODE = MappingProxyType({rule.code: rule for rule in RULES})
BY_NAME = MappingProxyType({name: rule for rule in RULES for name in (rule.name, *rule.aliases)})


def code_for(name: str) -> str | None:
    rule = BY_NAME.get(name)
    return rule.code if rule is not None else None


def get_rule(code: str) -> Rule:
    try:
        return BY_CODE[code.upper()]
    except KeyError:
        raise ValueError(
            f"Unknown check code {code!r}; run 'latex-prep rules' to list checks"
        ) from None
