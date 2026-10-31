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
    """The names of the ``pdf.*`` rules, grouped by the module that reports them.

    Attributes:
        SECTION_PAGE_BUDGET: Section page budgets.
        RENDERED_TEXT_SIZE: Reported rendered text size.
        PRINTABLE_TEXT_BOUNDS: Printable text bounds.
        TEXT_OVERLAP: Text overlap candidates.
        SPARSE_PAGES: Sparse rendered page candidates.
        REQUIRED_METADATA: Required PDF metadata.
        METADATA_AGREEMENT: Expected PDF metadata agreement.
        TAGGING_PRESENCE: PDF tagging presence.
        DOCUMENT_LANGUAGE: PDF language declaration.
        ANNOTATION_POLICY: PDF annotation type policy.
        ACTION_POLICY: PDF action-name candidates.
        INCLUDED_FIGURE_FONTS: Included PDF figure fonts.
        STROKE_WIDTH: Transformed PDF stroke width.
        COLOR_SPACE_POLICY: Rendering color-space policy.
        ASSUMED_TEXT_CONTRAST: Text contrast against supplied background.
        GRAYSCALE_SEPARATION: Grayscale color separation candidates.
        PAGE_LIMIT: Total PDF page limit.
        PAGE_DIMENSIONS: Expected PDF page dimensions.
        PAGE_SIZE_CONSISTENCY: Consistent PDF page dimensions.
        IMAGE_RESOLUTION: Minimum effective image resolution.
        FONT_EMBEDDING: Embedded PDF fonts.
        TYPE3_FONTS: Type 3 PDF fonts.
        ENCRYPTION_POLICY: PDF encryption.
        FORMS_POLICY: PDF forms.
        JAVASCRIPT_POLICY: PDF JavaScript.
        ATTACHMENTS_POLICY: Embedded PDF attachments.
        EXPECTED_HEADINGS: Explicit rendered heading expectations.
        TABLE_STRUCTURE: Tagged table row and cell structure.
        LINK_STRUCTURE: Link tag and annotation associations.
        FIGURE_ALTERNATIVE_TEXT: Nonempty tagged Figure alternative text.
        STRUCTURE_REFERENCES: Structure tree and marked-content reference integrity.
        PAGES: The page count.
        METADATA: The document properties.
        PAGE_GEOMETRY: The page dimensions and boxes.
        ENCRYPTED: Whether the PDF is encrypted.
        FORM: Whether the PDF holds a form.
        JAVASCRIPT: Whether the PDF holds JavaScript.
        FONTS: The font resources and which are not embedded.
        TEXT: Whether every page has extractable text.
        ATTACHMENTS: The embedded files.
        IMAGES: The raster placements.
        ANNOTATION_MEDIA_SCOPE: That annotations and active media are not inspected.
        TOOLS: The versions of the PDF tools.
        INSPECTION_UNAVAILABLE: The inspection could not run.
        GEOMETRY_UNAVAILABLE: The page geometry could not be read.
        FONTS_UNAVAILABLE: The font inventory could not be read.
        TEXT_UNAVAILABLE: The text could not be extracted.
        ATTACHMENTS_UNAVAILABLE: The embedded files could not be listed.
        IMAGES_UNAVAILABLE: The raster placements could not be listed.
        ARTWORK_INPUT_SCOPE: Whether the included PDF figures measured for artwork are the
            complete set.
    """

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
