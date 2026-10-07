"""Data-only catalogue for explicit author and PDF structure checks."""

RULE_DEFINITIONS = (
    {
        "code": "MAN201",
        "name": "manuscript.author_records",
        "title": "Configured per-author field completeness",
        "description": (
            "Check each sequential literal preamble author record against user-selected "
            "associated commands; shared fields and template semantics are not inferred. A "
            "record enclosed by primitive conditional control flow is inconclusive."
        ),
        "tests": (
            "tests.test_structure_checks.AuthorRecordTests.test_each_author_needs_its_own_fields",
            "tests.test_structure_checks.AuthorRecordTests.test_grouped_and_dynamic_records_are_inconclusive",
            "tests.test_structure_checks.AuthorRecordTests.test_defined_conditional_names_do_not_block_author_records",
            "tests.test_structure_checks.AuthorRecordTests.test_failed_author_records_report_counts_and_locations",
        ),
        "fix": (
            "Supply the missing fields for the indicated author using the selected record "
            "grammar, or review unsupported template structures manually."
        ),
    },
    {
        "code": "PDF401",
        "name": "pdf.expected_headings",
        "title": "Explicit rendered heading expectations",
        "description": (
            "Find user-supplied heading title and number text on explicit PDF pages after "
            "whitespace normalization; section and appendix numbering semantics are not inferred."
        ),
        "tests": (
            "tests.test_structure_checks.PdfStructureTests.test_heading_expectations_match_differ_and_remain_uncertain",
        ),
        "fix": (
            "Inspect the specified PDF page, correct the heading or explicit expectation, "
            "and review ambiguous extraction manually."
        ),
    },
    {
        "code": "PDF402",
        "name": "pdf.table_structure",
        "title": "Tagged table row and cell structure",
        "description": (
            "Inspect declared Table, row-group, TR, TH and TD relationships. Untagged visual "
            "tables, meaningful headers, cell spans and PDF/UA conformance remain unverified."
        ),
        "tests": (
            "tests.test_structure_checks.PdfStructureTests.test_tagged_tables_have_rows_and_cells",
        ),
        "fix": (
            "Correct the table tagging hierarchy in the authoring tool and manually verify "
            "header associations and reading order."
        ),
    },
    {
        "code": "PDF403",
        "name": "pdf.link_structure",
        "title": "Link tag and annotation associations",
        "description": (
            "Match Link structure elements to page Link annotations and inspect supported "
            "target presence; accessible-name adequacy, navigation and remote target availability "
            "are not established."
        ),
        "tests": (
            "tests.test_structure_checks.PdfStructureTests.test_link_tags_match_real_page_annotations",
        ),
        "fix": (
            "Repair Link tags, object references and annotation targets, then manually verify "
            "link text and navigation."
        ),
    },
    {
        "code": "PDF404",
        "name": "pdf.figure_alternative_text",
        "title": "Nonempty tagged Figure alternative text",
        "description": (
            "Require nonempty supported Alt strings on declared Figure elements. This does "
            "not find every visible image or judge description quality."
        ),
        "tests": (
            "tests.test_structure_checks.PdfStructureTests.test_figure_alt_presence_and_unreadable_strings",
        ),
        "fix": (
            "Supply appropriate Figure alternative text in the authoring source and inspect "
            "its adequacy manually."
        ),
    },
    {
        "code": "PDF405",
        "name": "pdf.structure_references",
        "title": "Structure tree and marked-content reference integrity",
        "description": (
            "Inspect an explicitly required structure tree, parent links, duplicate or invalid "
            "MCIDs and parent-tree backlinks. Stream coverage, natural reading order and "
            "accessibility conformance remain unverified."
        ),
        "tests": (
            "tests.test_structure_checks.PdfStructureTests.test_structure_references_reject_duplicate_missing_and_invalid_mcid_links",
            "tests.test_structure_checks.PdfStructureTests.test_missing_or_malformed_structure_and_tools_are_not_passes",
        ),
        "fix": (
            "Repair the reported structure references in the authoring/tagging tool and "
            "manually assess reading order and accessibility."
        ),
    },
)
