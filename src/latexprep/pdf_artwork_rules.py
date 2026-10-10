"""Data-only catalogue for explicit artwork measurements and review candidates."""

RULE_DEFINITIONS = (
    {
        "code": "PDF301",
        "name": "pdf.printer_marks",
        "title": "Printed crop/registration-mark candidates",
        "description": "Locate paired short strokes aligned with explicit trim corners; "
        "also identify crossed registration strokes. Raster/outlined marks remain unmeasured.",
        "tests": (
            "tests.test_pdf_artwork.PdfArtworkTests.test_crop_marks_need_trim_aligned_pairs_and_keep_exemptions_and_uncertainty",
            "tests.test_pdf_artwork.PdfArtworkTests.test_registration_crosses_are_located_outside_trim_only",
        ),
        "fix": "Remove unintended marks or exempt intentionally marked pages after review.",
    },
    {
        "code": "PDF302",
        "name": "pdf.figure_whitespace",
        "title": "Figure outer whitespace",
        "description": "Measure the area outside a bounded rendered ink box in an explicit region "
        "against a supplied ratio/background. Input PDF figures may use their whole page.",
        "tests": (
            "tests.test_pdf_artwork.PdfArtworkTests.test_whitespace_measures_outer_content_box_and_explicit_background",
        ),
        "fix": "Tighten unintended padding or revise the region/background/threshold.",
    },
    {
        "code": "PDF303",
        "name": "pdf.figure_edge_ink",
        "title": "Figure boundary ink candidates",
        "description": "Locate rendered ink in a supplied boundary band; touching ink can be an "
        "intentional border and never proves clipping. Unsupported page frames are inconclusive.",
        "tests": (
            "tests.test_pdf_artwork.PdfArtworkTests.test_figure_edge_ink_is_advisory_and_rejects_cropped_page_frame",
        ),
        "fix": "Inspect the figure boundary and repair unintended clipping or correct its region.",
    },
    {
        "code": "PDF304",
        "name": "pdf.drawing_bounds",
        "title": "Image and polygon drawing bounds",
        "description": "Compare affine image and straight filled-path bounds after rectangular "
        "clipping with allowed regions. Text, curves, strokes and masks remain unmeasured.",
        "tests": (
            "tests.test_pdf_artwork.PdfArtworkTests.test_drawing_bounds_transform_images_and_reject_unmeasured_curves",
        ),
        "fix": "Adjust the listed drawing placements or the explicitly allowed drawing regions.",
    },
    {
        "code": "PDF305",
        "name": "pdf.clipping_candidates",
        "title": "Rectangular clipping candidates",
        "description": "Locate image/polygon extents crossing the page or active supported "
        "rectangular clip; intentional cropping remains a review decision.",
        "tests": (
            "tests.test_pdf_artwork.PdfArtworkTests.test_clipping_tracks_rectangles_and_restores_nested_clip_state",
        ),
        "fix": "Inspect each clip crossing and correct unintended cropping in the source artwork.",
    },
    {
        "code": "PDF306",
        "name": "pdf.artwork_composition",
        "title": "Observed artwork composition",
        "description": "Inventory traced raster, vector or mixed operations for a selected PDF; "
        "text/backgrounds count, and unsupported compositing or empty evidence is inconclusive.",
        "tests": (
            "tests.test_pdf_artwork.PdfArtworkTests.test_composition_distinguishes_raster_vector_mixed_and_unknown",
            "tests.test_pdf_artwork.PdfArtworkTests.test_composition_does_not_assume_type3_text_is_vector",
        ),
        "fix": "Review the observed composition or obtain complete supported trace evidence.",
    },
    {
        "code": "PDF307",
        "name": "pdf.artwork_category_resolution",
        "title": "Explicit artwork-category DPI",
        "description": "Apply a supplied category threshold to each raster placement, including "
        "axes and the coarsest sampling direction under shear. Categories are never inferred.",
        "tests": (
            "tests.test_pdf_artwork.PdfArtworkTests.test_category_dpi_measures_rotation_shear_both_axes_and_repeated_placements",
        ),
        "fix": "Increase image resolution, reduce placement scale, or correct its category.",
    },
    {
        "code": "PDF308",
        "name": "pdf.type3_glyph_programs",
        "title": "Type 3 glyph painting classification",
        "description": "Distinguish image/vector painting in reachable declared Type 3 programs "
        "and Form XObjects, with an optional bitmap policy; opaque programs are inconclusive.",
        "tests": (
            "tests.test_pdf_artwork.PdfArtworkTests.test_type3_programs_distinguish_vector_bitmap_nested_forms_and_opaque_data",
            "tests.test_pdf_artwork.PdfArtworkTests.test_type3_operator_looking_comments_strings_and_inline_tails_do_not_false_pass",
            "tests.test_pdf_artwork.PdfArtworkTests.test_type3_decoding_excludes_image_pixels",
        ),
        "fix": "Re-export bitmap glyphs as scalable fonts or outlines when the policy requires it.",
    },
    {
        "code": "PDF309",
        "name": "pdf.transformed_stroke_width",
        "title": "Affine stroke-normal thickness",
        "description": "Measure straight-segment normal thickness after nonuniform affine "
        "transforms. Curves/text need uniform transforms; hairlines, caps and joins are excluded.",
        "tests": (
            "tests.test_pdf_artwork.PdfArtworkTests.test_affine_stroke_width_uses_path_normal_not_largest_axis",
        ),
        "fix": "Increase the listed stroke widths at their final placement and inspect joins/caps.",
    },
    {
        "code": "PDF310",
        "name": "pdf.raster_region_candidates",
        "title": "Raster-dominated region candidates",
        "description": "Measure union coverage of axis-aligned raster placements in explicit "
        "regions. High coverage without page text is a scan/table candidate, not a diagnosis.",
        "tests": (
            "tests.test_pdf_artwork.PdfArtworkTests.test_raster_region_coverage_uses_union_and_keeps_text_ambiguity",
        ),
        "fix": "Inspect the selected region and provide vector or searchable output as needed.",
    },
    {
        "code": "PDF311",
        "name": "pdf.grayscale_preview",
        "title": "Grayscale review preview",
        "description": "Retain validated, bounded grayscale page previews in the inspection "
        "workspace. Preview creation does not establish semantic color independence.",
        "tests": (
            "tests.test_pdf_artwork.PdfArtworkTests.test_grayscale_preview_retains_validated_artifact_and_rejects_partial_output",
        ),
        "fix": "Resolve the renderer limitation and inspect the regenerated grayscale preview.",
    },
    {
        "code": "PDF312",
        "name": "pdf.rendered_sample_contrast",
        "title": "Rendered foreground/background sample contrast",
        "description": "Compare composited RGB pixels from explicit foreground/background patches. "
        "Patches must be homogeneous unless a channel-spread tolerance is supplied. Conservative "
        "luminance bounds preserve threshold uncertainty; text semantics are not inferred.",
        "tests": (
            "tests.test_pdf_artwork.PdfArtworkTests.test_rendered_sample_contrast_uses_actual_foreground_and_background_pixels",
            "tests.test_pdf_artwork.PdfArtworkTests.test_rendered_sample_contrast_requires_homogeneous_patches_or_explicit_tolerance",
            "tests.test_pdf_artwork.PdfArtworkTests.test_rendered_sample_contrast_rejects_incomplete_render_and_unsupported_regions",
        ),
        "fix": "Adjust foreground/background colors or supply valid homogeneous sample patches.",
    },
    {
        "code": "PDF313",
        "name": "figure.color_space",
        "title": "Required figure colour space",
        "description": "Opt-in required_color_space ('rgb' or 'cmyk'): classify MuPDF trace "
        "colour-space names in each included PDF figure (figure_artwork) or the whole PDF "
        "(pdf_artwork), and PNG/JPEG figure headers in the selected source graph. Grayscale "
        "satisfies either family; unclassified spaces, EPS figures and missing tools are "
        "inconclusive.",
        "tests": (
            "tests.test_pdf_artwork.PdfArtworkTests.test_traced_figure_color_families_against_required_space",
            "tests.test_pdf_artwork.PdfArtworkTests.test_missing_trace_tool_is_inconclusive",
            "tests.test_submission_readiness.FigureTests.test_raster_color_space_policy_parses_png_and_jpeg_headers",
        ),
        "fix": "Re-export the reported figures in the required colour space (grayscale is "
        "accepted for either), or correct required_color_space.",
    },
)
