"""The names of the ``pdf.*`` rules reported for the manuscript PDF and its input figures.

A rule name is the identity a finding carries and the key the rule catalogue is looked up
by, so it is written once, here. ``OPERATIONAL_PDF_RULES`` are the names without a
catalogue entry: observations of what was read, and diagnostics of why a measurement could
not run. They report facts rather than check a requirement, so they have no public code.
The artwork and comparison checks keep their own names in their own modules.
"""

from __future__ import annotations

from enum import StrEnum


class PdfRule(StrEnum):
    # Configured measurements (pdf_checks).
    SECTION_PAGE_BUDGET = "pdf.section_page_budget"
    RENDERED_TEXT_SIZE = "pdf.rendered_text_size"
    PRINTABLE_TEXT_BOUNDS = "pdf.printable_text_bounds"
    TEXT_OVERLAP = "pdf.text_overlap"
    SPARSE_PAGES = "pdf.sparse_pages"
    REQUIRED_METADATA = "pdf.required_metadata"
    METADATA_AGREEMENT = "pdf.metadata_agreement"
    TAGGING_PRESENCE = "pdf.tagging_presence"
    DOCUMENT_LANGUAGE = "pdf.document_language"
    ANNOTATION_POLICY = "pdf.annotation_policy"
    ACTION_POLICY = "pdf.action_policy"
    INCLUDED_FIGURE_FONTS = "pdf.included_figure_fonts"
    STROKE_WIDTH = "pdf.stroke_width"
    COLOR_SPACE_POLICY = "pdf.color_space_policy"
    ASSUMED_TEXT_CONTRAST = "pdf.assumed_text_contrast"
    GRAYSCALE_SEPARATION = "pdf.grayscale_separation"
    # Acceptance policies of the PDF inspection (pdf_inspection).
    PAGE_LIMIT = "pdf.page_limit"
    PAGE_DIMENSIONS = "pdf.page_dimensions"
    PAGE_SIZE_CONSISTENCY = "pdf.page_size_consistency"
    IMAGE_RESOLUTION = "pdf.image_resolution"
    FONT_EMBEDDING = "pdf.font_embedding"
    TYPE3_FONTS = "pdf.type3_fonts"
    ENCRYPTION_POLICY = "pdf.encryption_policy"
    FORMS_POLICY = "pdf.forms_policy"
    JAVASCRIPT_POLICY = "pdf.javascript_policy"
    ATTACHMENTS_POLICY = "pdf.attachments_policy"
    # Tagged-structure and heading checks (pdf_structure_checks).
    EXPECTED_HEADINGS = "pdf.expected_headings"
    TABLE_STRUCTURE = "pdf.table_structure"
    LINK_STRUCTURE = "pdf.link_structure"
    FIGURE_ALTERNATIVE_TEXT = "pdf.figure_alternative_text"
    STRUCTURE_REFERENCES = "pdf.structure_references"
    # Operational observations of the PDF inspection.
    PAGES = "pdf.pages"
    METADATA = "pdf.metadata"
    PAGE_GEOMETRY = "pdf.page_geometry"
    ENCRYPTED = "pdf.encrypted"
    FORM = "pdf.form"
    JAVASCRIPT = "pdf.javascript"
    FONTS = "pdf.fonts"
    TEXT = "pdf.text"
    ATTACHMENTS = "pdf.attachments"
    IMAGES = "pdf.images"
    ANNOTATION_MEDIA_SCOPE = "pdf.annotation_media_scope"
    TOOLS = "pdf.tools"
    # Operational diagnostics: a measurement that could not run or be read.
    INSPECTION_UNAVAILABLE = "pdf.inspection_unavailable"
    GEOMETRY_UNAVAILABLE = "pdf.geometry_unavailable"
    FONTS_UNAVAILABLE = "pdf.fonts_unavailable"
    TEXT_UNAVAILABLE = "pdf.text_unavailable"
    ATTACHMENTS_UNAVAILABLE = "pdf.attachments_unavailable"
    IMAGES_UNAVAILABLE = "pdf.images_unavailable"
    # The scope of the included-figure artwork inputs, reported by the check pipeline.
    ARTWORK_INPUT_SCOPE = "pdf.artwork_input_scope"


OPERATIONAL_PDF_RULES = frozenset(
    {
        PdfRule.PAGES,
        PdfRule.METADATA,
        PdfRule.PAGE_GEOMETRY,
        PdfRule.ENCRYPTED,
        PdfRule.FORM,
        PdfRule.JAVASCRIPT,
        PdfRule.FONTS,
        PdfRule.TEXT,
        PdfRule.ATTACHMENTS,
        PdfRule.IMAGES,
        PdfRule.ANNOTATION_MEDIA_SCOPE,
        PdfRule.TOOLS,
        PdfRule.INSPECTION_UNAVAILABLE,
        PdfRule.GEOMETRY_UNAVAILABLE,
        PdfRule.FONTS_UNAVAILABLE,
        PdfRule.TEXT_UNAVAILABLE,
        PdfRule.ATTACHMENTS_UNAVAILABLE,
        PdfRule.IMAGES_UNAVAILABLE,
        PdfRule.ARTWORK_INPUT_SCOPE,
    }
)
