"""Data-only diagnostic catalogue for explicitly selected source preparation."""

SOURCE_TRANSFORM_RULES = (
    {
        "code": "TEX201",
        "name": "source.comment_removal",
        "title": "Reviewed comment removal",
        "description": "Propose removing selected comment bodies from document sources "
        "(.tex/.ltx/.latex) while retaining percent signs, line endings, literal regions, "
        "directives and license blocks; local class/style files are copied unchanged. Unclosed "
        "or dynamic lexical contexts are inconclusive.",
        "tests": ("tests.test_source_transform.SourceTransformTests.test_comment_removal",),
        "fix": "Review the copy and resolve unsupported lexical constructs before retrying.",
    },
    {
        "code": "TEX202",
        "name": "source.merge_inputs",
        "title": "Literal source input merging",
        "description": "Plan recursive merging of standalone literal input lines within a bounded, "
        "single-root dependency graph; include/import execution semantics remain unsupported.",
        "tests": ("tests.test_source_transform.SourceTransformTests.test_merge_inputs",),
        "fix": "Use standalone literal input lines and a complete graph, or disable merging.",
    },
    {
        "code": "TEX203",
        "name": "source.inline_bibliography",
        "title": "Generated bibliography inlining",
        "description": "Replace one standalone bibliography command with an explicitly selected "
        "local BibTeX thebibliography environment; biblatex data and unresolved graphs "
        "are inconclusive.",
        "tests": ("tests.test_source_transform.SourceTransformTests.test_inline_bibliography",),
        "fix": "Supply the intended generated BibTeX bibliography and review the rebuilt copy.",
    },
    {
        "code": "FMT101",
        "name": "source.format_protected",
        "title": "Formatter protected regions",
        "description": "Reject changes to literal environments, inline literals, formatter-off "
        "regions, directives or license blocks; unmatched regions are inconclusive.",
        "tests": ("tests.test_formatting.FormattingTests.test_protected_regions",),
        "fix": "Exclude this source or use a formatter that preserves its protected regions.",
    },
    {
        "code": "FMT102",
        "name": "source.format_exclusions",
        "title": "Project formatting exclusions",
        "description": "Select source with explicit project-relative glob exclusions and report "
        "unmatched patterns; unsafe linked source traversal remains inconclusive.",
        "tests": ("tests.test_formatting.FormattingTests.test_project_exclusions",),
        "fix": "Correct exclusion patterns or replace linked inputs with local files.",
    },
    {
        "code": "FMT103",
        "name": "source.format_blank_lines",
        "title": "Explicit formatting blank-line policy",
        "description": "Verify paragraph-separator preservation or collapse repeated blank lines "
        "outside protected spans; this adapter policy is independent of formatter CLI flags.",
        "tests": ("tests.test_formatting.FormattingTests.test_blank_line_policy",),
        "fix": "Review the policy and diff, or exclude source with unresolved protected context.",
    },
    {
        "code": "PKG201",
        "name": "flatten.filename_overrides",
        "title": "Explicit flat filename overrides",
        "description": "Apply portable unique filename overrides with unchanged extensions to the "
        "flat map; renaming required root/class/style/bibliography identities is unsupported.",
        "tests": ("tests.test_source.SourceTests.test_filename_overrides",),
        "fix": "Choose unique portable names with unchanged extensions and protected identities.",
    },
)

RULE_DEFINITIONS = SOURCE_TRANSFORM_RULES
