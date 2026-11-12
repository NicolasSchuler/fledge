"""Data-only diagnostic catalogue for explicitly selected source preparation."""

from .rule_definition import Rule

RULES = (
    Rule(
        code="FMT003",
        name="source.format_encoding",
        title="Formatter source encoding",
        description=(
            "Report a source file that is not valid UTF-8 when formatting is requested. "
            "Formatting currently requires UTF-8, so the file is left unformatted and no "
            "proposal covers it."
        ),
        tests=(
            "tests.test_limit_findings.FormatterLimitTests.test_a_source_that_is_not_utf8_is_reported_unformattable",
        ),
        fix=(
            "Convert an explicit copy of the file to UTF-8, or exclude it from the formatted files."
        ),
        mandatory=True,
    ),
    Rule(
        code="FMT004",
        name="source.format_size",
        title="Formatter source size limit",
        description=(
            "Report a source file over the formatting size limit when formatting is "
            "requested. The file is left unformatted and no proposal covers it."
        ),
        tests=(
            "tests.test_limit_findings.FormatterLimitTests.test_a_source_over_the_size_limit_is_reported_unformattable",
        ),
        fix="Split the file or exclude it from the formatted files.",
        mandatory=True,
    ),
    Rule(
        code="TEX201",
        name="source.comment_removal",
        title="Reviewed comment removal",
        description=(
            "Propose removing selected comment bodies from document sources "
            "(.tex/.ltx/.latex) while retaining percent signs, line endings, literal regions, "
            "directives and license blocks; local class/style files are copied unchanged. Unclosed "
            "or dynamic lexical contexts are inconclusive."
        ),
        tests=("tests.test_source_transform.SourceTransformTests.test_comment_removal",),
        fix="Review the copy and resolve unsupported lexical constructs before retrying.",
        mandatory=True,
    ),
    Rule(
        code="TEX202",
        name="source.merge_inputs",
        title="Literal source input merging",
        description=(
            "Plan recursive merging of standalone literal input lines within a bounded, "
            "single-root dependency graph; include/import execution semantics remain unsupported."
        ),
        tests=("tests.test_source_transform.SourceTransformTests.test_merge_inputs",),
        fix="Use standalone literal input lines and a complete graph, or disable merging.",
        mandatory=True,
    ),
    Rule(
        code="TEX203",
        name="source.inline_bibliography",
        title="Generated bibliography inlining",
        description=(
            "Replace one standalone bibliography command with an explicitly selected "
            "local BibTeX thebibliography environment; biblatex data and unresolved graphs "
            "are inconclusive."
        ),
        tests=("tests.test_source_transform.SourceTransformTests.test_inline_bibliography",),
        fix="Supply the intended generated BibTeX bibliography and review the rebuilt copy.",
        mandatory=True,
    ),
    Rule(
        code="TEX204",
        name="source.suppress_included_pdf_information",
        title="Included-PDF information kept out of the built PDF",
        description=(
            "Opt-in (source_transforms.suppress_included_pdf_information, set by the "
            "anonymous-review preset; prepare only): add one line after the leading comments "
            "of the prepared main file that sets \\pdfsuppressptexinfo=-1 for pdfLaTeX or "
            "\\pdfvariable suppressoptionalinfo 15 for LuaLaTeX. pdfTeX and LuaTeX otherwise "
            "copy each included PDF's information dictionary (author, title, creator) and "
            "file name into the output as /PTEX.InfoDict and /PTEX.FileName. The included "
            "page's XMP and application data (PieceInfo) are copied regardless; XeLaTeX copies "
            "none of this. A main file that already sets either value is left unchanged; the "
            "original project is never edited."
        ),
        tests=(
            "tests.test_source_transform.SuppressionTests.test_line_follows_leading_comments_and_shows_in_the_changes",
            "tests.test_source_transform.SuppressionTests.test_existing_settings_and_unreadable_main_are_respected",
        ),
        fix=(
            "Review the added line in the prepared copy's diff; remove an existing "
            "\\pdfsuppressptexinfo setting that contradicts it, or re-export figures whose "
            "page-level metadata must not be shared."
        ),
        mandatory=True,
    ),
    Rule(
        code="FMT101",
        name="source.format_protected",
        title="Formatter protected regions",
        description=(
            "Reject changes to literal environments, inline literals, formatter-off "
            "regions, directives or license blocks; unmatched regions are inconclusive."
        ),
        tests=("tests.test_formatting.FormattingTests.test_protected_regions",),
        fix="Exclude this source or use a formatter that preserves its protected regions.",
        mandatory=True,
    ),
    Rule(
        code="FMT102",
        name="source.format_exclusions",
        title="Project formatting exclusions",
        description=(
            "Select source with explicit project-relative glob exclusions and report "
            "unmatched patterns; unsafe linked source traversal remains inconclusive."
        ),
        tests=("tests.test_formatting.FormattingTests.test_project_exclusions",),
        fix="Correct exclusion patterns or replace linked inputs with local files.",
        mandatory=True,
    ),
    Rule(
        code="FMT103",
        name="source.format_blank_lines",
        title="Explicit formatting blank-line policy",
        description=(
            "Verify paragraph-separator preservation or collapse repeated blank lines "
            "outside protected spans; this adapter policy is independent of formatter CLI flags."
        ),
        tests=("tests.test_formatting.FormattingTests.test_blank_line_policy",),
        fix="Review the policy and diff, or exclude source with unresolved protected context.",
        mandatory=True,
    ),
    Rule(
        code="PKG201",
        name="flatten.filename_overrides",
        title="Explicit flat filename overrides",
        description=(
            "Apply portable unique filename overrides with unchanged extensions to the "
            "flat map; renaming required root/class/style/bibliography identities is unsupported."
        ),
        tests=("tests.test_flat_layout.FlatLayoutTests.test_filename_overrides",),
        fix="Choose unique portable names with unchanged extensions and protected identities.",
        mandatory=True,
    ),
)
