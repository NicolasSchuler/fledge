"""Project selected checks onto their activation options before dispatch.

Selection can remove an optional policy, but cannot supply its missing arguments,
grant network permission, cancel a requested edit, or weaken preparation guards.
Uncoded execution diagnostics and explicit inventories always remain visible.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace

from .check_selection import CheckSelection
from .config import Settings
from .models import Finding

# These establish the safety of reading, transforming, building or publishing a
# prepared copy. Bibliography syntax, key, macro and relationship checks are kept
# in every command rather than trying to infer which proposed edit relies on them.
MANDATORY_CODES = frozenset(
    {
        "TEX005",
        "TEX006",
        "TEX007",
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
        "TEX201",
        "TEX202",
        "TEX203",
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
)

# Each row clears every activation field for one check together. This matters for
# dataclasses that reject a half-configured pair (regions/coverage, for example).
_ROOT_CHECKS: dict[str, dict[str, object]] = {
    "TEX101": {"expected_document_class": None},
    "TEX102": {"required_class_options": (), "forbidden_class_options": ()},
    "TEX103": {"forbidden_packages": ()},
    "TEX104": {"require_abstract": False},
    "TEX105": {"abstract_min_words": None, "abstract_max_words": None},
    "TEX106": {"keywords_min_count": None, "keywords_max_count": None},
    "TEX107": {"required_sections": ()},
    "PDF001": {"max_pages": None},
    "PDF101": {"expected_page_width_pt": None, "expected_page_height_pt": None},
    "PDF102": {"require_consistent_page_size": False},
    "PDF103": {"min_image_dpi": None},
    "PDF104": {"require_embedded_fonts": False},
    "PDF105": {"forbid_type3_fonts": False},
    "PDF106": {"forbid_encryption": False},
    "PDF107": {"forbid_forms": False},
    "PDF108": {"forbid_javascript": False},
    "PDF109": {"forbid_attachments": False},
    "PDF207": {"match_source_pdf_metadata": ()},
}

_ARTWORK_CHECKS: dict[str, dict[str, object]] = {
    "PDF301": {
        "detect_printer_marks": False,
        "trim_regions": (),
        "printer_mark_max_length_pt": None,
        "printer_mark_exemptions": (),
    },
    "PDF302": {"max_figure_whitespace_ratio": None},
    "PDF303": {"figure_edge_band_pt": None},
    "PDF304": {"drawing_regions": ()},
    "PDF305": {"check_clipping": False},
    "PDF306": {"classify_artwork": False},
    "PDF307": {"artwork_category": None, "category_min_dpi": ()},
    "PDF308": {"classify_type3_glyphs": False, "forbid_bitmap_type3_glyphs": False},
    "PDF309": {"min_stroke_width_pt": None},
    "PDF310": {"raster_regions": (), "min_raster_region_coverage": None},
    "PDF311": {"grayscale_preview": False},
    "PDF312": {"contrast_samples": ()},
}

_GROUP_CHECKS: dict[str, dict[str, dict[str, object]]] = {
    "bibliography_checks": {
        "BIB101": {"check_citation_coverage": False},
        "BIB102": {"check_uncited_entries": False},
        "BIB103": {"required_fields": ()},
        "BIB104": {"doi_entry_types": ()},
        "BIB105": {"check_page_ranges": False},
        "BIB106": {"check_urls": False},
        "BIB107": {"check_capitalization": False},
        "BIB108": {"check_fuzzy_duplicates": False},
    },
    "build_checks": {
        "BLD101": {"overfull_tolerance_pt": None},
        "BLD102": {
            "allowed_loaded_packages": None,
            "forbidden_loaded_packages": (),
            "required_loaded_packages": (),
        },
        "BLD103": {"minimum_package_dates": ()},
        "BLD104": {"check_local_package_shadows": False},
        "BLD301": {"inventory_loaded_options": False},
    },
    "manuscript_checks": {
        "MAN001": {"allowed_packages": None},
        "MAN002": {"check_layout_manipulation": False},
        "MAN003": {"forbid_abstract_citations": False},
        "MAN004": {
            "abstract_abbreviation_policy": None,
            "abstract_abbreviation_exceptions": (),
        },
        "MAN005": {"max_heading_depth": None},
        "MAN006": {"check_empty_sections": False},
        "MAN007": {"check_hardcoded_references": False},
        "MAN008": {"required_metadata": ()},
        "MAN009": {"check_orcid": False},
        "MAN010": {"required_declarations": ()},
        "MAN011": {"require_float_captions": False},
        "MAN012": {"require_float_labels": False},
        "MAN013": {"check_float_label_order": False},
        "MAN014": {"require_float_references": False},
        "MAN015": {"check_float_reference_order": False},
        "MAN016": {"require_figure_descriptions": False},
    },
    "metadata_privacy": {"PRV103": {"image_identity_terms": ()}},
    "online_checks": {
        "NET001": {"online_doi_resolution": False},
        "NET002": {"online_metadata": False},
        "NET003": {"online_missing_doi": False},
        "NET004": {"online_published_versions": False},
        "NET005": {"online_notices": False},
        "NET006": {"online_reference_links": False},
        "NET007": {"online_replication_links": False},
    },
    "pdf_checks": {
        "PDF201": {"section_budgets": ()},
        "PDF202": {"min_text_size_pt": None, "text_size_regions": ()},
        "PDF203": {"printable_margins_pt": None, "printable_regions": ()},
        "PDF204": {"overlap_min_area_ratio": None},
        "PDF205": {"min_page_ink_ratio": None, "sparse_page_exemptions": ()},
        "PDF206": {"required_metadata": ()},
        "PDF207": {"expected_metadata": ()},
        "PDF208": {"require_tagged": False},
        "PDF209": {"require_language": False, "expected_language": None},
        "PDF210": {"forbidden_annotation_types": ()},
        "PDF211": {"forbidden_action_types": ()},
        "PDF212": {"require_embedded_figure_fonts": False, "forbid_type3_figure_fonts": False},
        "PDF213": {"min_stroke_width_pt": None},
        "PDF214": {"allowed_color_spaces": ()},
        "PDF215": {"min_text_contrast": None, "contrast_background_rgb": None},
        "PDF216": {"min_grayscale_luminance_difference": None},
    },
    "pdf_artwork": _ARTWORK_CHECKS,
    "figure_artwork": _ARTWORK_CHECKS,
    "structure_checks": {
        "MAN201": {"required_author_fields": ()},
        "PDF401": {"heading_expectations": ()},
        "PDF402": {"check_table_structure": False},
        "PDF403": {"check_link_structure": False},
        "PDF404": {"require_figure_alt": False},
        "PDF405": {"require_structure_tree": False, "check_structure_references": False},
    },
    "submission_checks": {
        "PRV003": {"scan_identity_hints": False},
        "PRV004": {"scan_secrets": False},
        "PRV005": {"scan_private_comments": False},
        "PRV006": {"check_shell_escape": False},
        "PKG101": {"filename_max_length": None},
        "PKG102": {"filename_allowed_characters": None},
        "PKG103": {"allowed_extensions": ()},
        "PKG104": {"required_deliverables": ()},
        "PKG105": {"max_archive_bytes": None},
        "PKG106": {"report_unused_assets": False},
        "PKG107": {"template_references": ()},
    },
}


def _disabled_values(
    policies: dict[str, dict[str, object]], selection: CheckSelection
) -> dict[str, object]:
    return {
        field: disabled
        for code, values in policies.items()
        if not selection.enabled(code)
        for field, disabled in values.items()
    }


def effective_settings(settings: Settings) -> Settings:
    """Disable unselected configured policies without changing the user's Settings.

    Unchanged objects are returned by identity, including the default ALL policy.
    Context shared with a selected check is retained. PRV001/PRV002 share identity
    terms: their individual source/PDF dispatchers must also clear terms for their
    own unselected producer. Source heuristics, bibliography parse diagnostics and
    ordinary build-log findings arise during required work and are filtered only
    after their dependent checks have consumed that evidence.
    """
    selection = settings.checks
    updates = {
        name: value
        for name, value in _disabled_values(_ROOT_CHECKS, selection).items()
        if getattr(settings, name) != value
    }
    for group_name, policies in _GROUP_CHECKS.items():
        current = getattr(settings, group_name)
        values = _disabled_values(policies, selection)
        if group_name == "submission_checks" and not any(
            selection.enabled(code) for code in ("PRV001", "PRV002")
        ):
            values["identity_terms"] = ()
        if group_name in {"pdf_artwork", "figure_artwork"} and not any(
            selection.enabled(code) for code in ("PDF302", "PDF303")
        ):
            values["figure_regions"] = ()
        changed = {name: value for name, value in values.items() if getattr(current, name) != value}
        if changed:
            updates[group_name] = replace(current, **changed)
    return replace(settings, **updates) if updates else settings


def selected_findings(findings: Iterable[Finding], selection: CheckSelection) -> list[Finding]:
    """Keep selected public checks and every mandatory or uncoded diagnostic.

    Consult the selection for any registered code rather than an optional-check
    allowlist, so new rule families cannot silently disappear from reports.
    """
    return [
        finding
        for finding in findings
        if finding.code is None
        or finding.code in MANDATORY_CODES
        or selection.enabled(finding.code)
    ]
