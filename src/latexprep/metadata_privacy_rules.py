"""Data-only privacy entries for selected metadata edits and figure metadata scans."""

from .rule_definition import Rule

RULES = (
    Rule(
        code="PRV101",
        name="privacy.metadata_sanitization",
        title="Explicit source metadata sanitization",
        description=(
            "Plan user-selected literal metadata replacements in copied TeX source "
            "only when the unique field matches its exact expected-before value. Generated, "
            "ambiguous and unsupported declarations block the proposal, as does conditional "
            "control flow that encloses a selected declaration."
        ),
        tests=(
            "tests.test_metadata_privacy.MetadataPrivacyTests.test_literal_replacements_preserve_originals_and_other_metadata",
            "tests.test_metadata_privacy.MetadataPrivacyTests.test_uncertain_or_stale_source_blocks_the_whole_plan",
            "tests.test_metadata_privacy.MetadataPrivacyTests.test_conditionals_only_block_the_declarations_they_enclose",
        ),
        fix=(
            "Review the literal field and expected old value, then rebuild the edited copy "
            "and recheck its final PDF metadata."
        ),
        mandatory=True,
    ),
    Rule(
        code="PRV102",
        name="privacy.sanitized_pdf_metadata",
        title="Rebuilt PDF metadata after sanitization",
        description=(
            "Check selected hypersetup replacements against final reported PDF "
            "properties and search those properties for removed literal source values. Missing "
            "metadata evidence remains inconclusive; binary objects and anonymity are not verified."
        ),
        tests=(
            "tests.test_metadata_privacy.MetadataPrivacyTests.test_final_pdf_recheck_requires_evidence_and_selected_values",
        ),
        fix=(
            "Remove remaining configured values or correct the selected properties in the "
            "source, rebuild, and repeat final-PDF inspection."
        ),
        mandatory=True,
    ),
    Rule(
        code="PRV103",
        name="privacy.image_metadata",
        title="Configured identity terms in figure metadata",
        description=(
            "Scan the figures that the selected document includes with \\includegraphics or "
            "\\includepdf (every PNG and JPEG file of the project when that set is uncertain) "
            "for explicit literal identity terms: supported PNG text/EXIF, pre-scan JPEG "
            "EXIF/XMP/extended XMP/IPTC IIM application records/comments, and a PDF's "
            "information dictionary, XMP and application data (PieceInfo), with bounded "
            "parsing and decompression. Vendor blocks such as the EXIF MakerNote are listed as "
            "limitations instead of making the scan unusable. No OCR, pixel scan, image "
            "rewriting or general anonymity guarantee."
        ),
        tests=(
            "tests.test_metadata_privacy.ImageMetadataTests.test_png_text_compressed_text_and_exif_match_without_echoing_values",
            "tests.test_metadata_privacy.ImageMetadataTests.test_jpeg_exif_xmp_and_comments_are_scanned",
            "tests.test_metadata_privacy.ImageMetadataTests.test_malformed_compressed_and_unsupported_metadata_are_inconclusive",
            "tests.test_metadata_privacy.ImageMetadataTests.test_vendor_blocks_are_limitations_rather_than_an_unusable_scan",
            "tests.test_metadata_privacy.ImageMetadataTests.test_iptc_application_records_and_extended_xmp_are_scanned",
            "tests.test_metadata_privacy.PdfFigureMetadataTests.test_included_pdf_figures_are_scanned_for_identity_terms",
            "tests.test_metadata_privacy.PdfFigureMetadataTests.test_unreadable_pdf_figures_and_unincluded_pdfs",
            "tests.test_metadata_privacy.PdfFigureMetadataTests.test_only_the_figures_the_document_includes_are_scanned",
        ),
        fix=(
            "Review the named metadata fields privately and explicitly re-export or "
            "sanitize the figure when those values should not be shared."
        ),
    ),
    Rule(
        code="PRV104",
        name="privacy.figure_home_folder_paths",
        title="Home-folder paths in figure metadata",
        description=(
            "Default advisory over the same figure metadata as PRV103: report a personal "
            "home-folder path such as /Users/<name>/, /home/<name>/ or C:\\Users\\<name>\\ in "
            "a title, file record or other field, which names the author's account. Shared "
            "folders such as /Users/Shared do not count. The file, field and kind of path are "
            "reported, never the folder's name; a figure that cannot be read only informs."
        ),
        tests=(
            "tests.test_metadata_privacy.PdfFigureMetadataTests.test_home_folder_paths_are_reported_without_names",
            "tests.test_metadata_privacy.PdfFigureMetadataTests.test_shared_folders_relative_paths_and_no_figures_report_nothing",
            "tests.test_metadata_privacy.PdfFigureMetadataTests.test_unreadable_pdf_figures_and_unincluded_pdfs",
            "tests.test_metadata_privacy.PdfFigureMetadataTests.test_only_the_figures_the_document_includes_are_scanned",
        ),
        fix=(
            "Re-export the figure without personal metadata or clear the named field; for "
            "PDF figures, source_transforms.suppress_included_pdf_information also keeps the "
            "information dictionary out of the PDF that prepare builds."
        ),
        reviewable=True,
    ),
)
