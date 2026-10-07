"""Data-only privacy entries for selected metadata edits and image metadata scans."""

RULE_DEFINITIONS = (
    {
        "code": "PRV101",
        "name": "privacy.metadata_sanitization",
        "title": "Explicit source metadata sanitization",
        "description": "Plan user-selected literal metadata replacements in copied TeX source "
        "only when the unique field matches its exact expected-before value. Generated, "
        "conditional, ambiguous and unsupported declarations block the proposal.",
        "tests": (
            "tests.test_metadata_privacy.MetadataPrivacyTests.test_literal_replacements_preserve_originals_and_other_metadata",
            "tests.test_metadata_privacy.MetadataPrivacyTests.test_uncertain_or_stale_source_blocks_the_whole_plan",
        ),
        "fix": "Review the literal field and expected old value, then rebuild the edited copy "
        "and recheck its final PDF metadata.",
    },
    {
        "code": "PRV102",
        "name": "privacy.sanitized_pdf_metadata",
        "title": "Rebuilt PDF metadata after sanitization",
        "description": "Check selected hypersetup replacements against final reported PDF "
        "properties and search those properties for removed literal source values. Missing "
        "metadata evidence remains inconclusive; binary objects and anonymity are not verified.",
        "tests": (
            "tests.test_metadata_privacy.MetadataPrivacyTests.test_final_pdf_recheck_requires_evidence_and_selected_values",
        ),
        "fix": "Remove remaining configured values or correct the selected properties in the "
        "source, rebuild, and repeat final-PDF inspection.",
    },
    {
        "code": "PRV103",
        "name": "privacy.image_metadata",
        "title": "Configured identity terms in image metadata",
        "description": "Scan supported PNG text/EXIF and pre-scan JPEG EXIF/XMP/comments for "
        "explicit literal identity terms with bounded parsing and decompression. No OCR, pixel "
        "scan, image rewriting or general anonymity guarantee.",
        "tests": (
            "tests.test_metadata_privacy.ImageMetadataTests.test_png_text_compressed_text_and_exif_match_without_echoing_values",
            "tests.test_metadata_privacy.ImageMetadataTests.test_jpeg_exif_xmp_and_comments_are_scanned",
            "tests.test_metadata_privacy.ImageMetadataTests.test_malformed_compressed_and_unsupported_metadata_are_inconclusive",
        ),
        "fix": "Review the named image metadata fields privately and explicitly re-export or "
        "sanitize the image when those values should not be shared.",
    },
)
