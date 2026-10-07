"""Data-only catalogue entries for configurable offline bibliography checks."""

RULE_DEFINITIONS = (
    {
        "code": "BIB101",
        "name": "bibliography.citation_coverage",
        "title": "Source citation coverage",
        "description": "Resolve supported literal citation keys against resources declared by the "
        "selected root, or each explicitly selected all-root graph. Dynamic source and incomplete "
        "bibliographies are inconclusive; this does not replace compilation.",
        "tests": (
            "tests.test_bibliography_checks.BibliographyCheckTests.test_citation_coverage_uses_selected_resources_and_inline_entries",
            "tests.test_bibliography_checks.BibliographyCheckTests.test_dynamic_and_incomplete_coverage_is_inconclusive",
            "tests.test_bibliography_checks.BibliographyCheckTests.test_all_roots_keeps_citation_resolution_per_document",
        ),
        "fix": "Correct the citation key or declared bibliography resource, then compile to verify "
        "the rendered citations. Resolve reported source uncertainty before claiming coverage.",
    },
    {
        "code": "BIB102",
        "name": "bibliography.uncited_entries",
        "title": "Apparently uncited bibliography entries",
        "description": "Find entries unreachable from literal citations, nocite and supported "
        "relationships within selected bibliography resources. Explicit all-root scope unions "
        "usage across roots; dynamic/filter/set uncertainty prevents a definitive absence claim. "
        "No entry is removed.",
        "tests": (
            "tests.test_bibliography_checks.BibliographyCheckTests.test_uncited_entries_respect_nocite_aliases_and_relationships",
            "tests.test_bibliography_checks.BibliographyCheckTests.test_uncited_candidates_are_inconclusive_for_filters_and_dynamic_relationships",
            "tests.test_bibliography_checks.BibliographyCheckTests.test_all_roots_unions_usage_without_importing_other_resources",
        ),
        "fix": "Review each candidate against all intended documents, filters and bibliography "
        "backend behavior before considering removal. This check never prunes a bibliography.",
    },
    {
        "code": "BIB103",
        "name": "bibliography.required_fields",
        "title": "Configured bibliography fields",
        "description": "Check user-supplied entry-type field requirements, including explicit "
        "alternatives such as author|editor. No publisher schema is assumed. Missing required "
        "fields fail; macros, unresolved inheritance and incomplete coverage are blocking "
        "inconclusive results. Include doi here to require DOI presence.",
        "tests": (
            "tests.test_bibliography_checks.BibliographyCheckTests.test_required_fields_are_entry_type_specific_and_accept_alternatives",
            "tests.test_bibliography_checks.BibliographyCheckTests.test_required_fields_do_not_guess_macro_values_or_inheritance",
            "tests.test_bibliography_checks.BibliographyCheckTests.test_required_doi_uncertainty_blocks_while_doi_advisories_do_not",
            "tests.test_bibliography_checks.BibliographyCheckTests.test_required_field_coverage_uncertainty_blocks_without_claiming_missing_metadata",
        ),
        "fix": "Supply or verify the configured metadata fields for this entry type. Preserve "
        "intentional inheritance and confirm that the requirements suit the cited resource.",
    },
    {
        "code": "BIB104",
        "name": "bibliography.missing_doi",
        "title": "Requested DOI presence",
        "description": "When DOI entry types are explicitly configured, report absent or empty "
        "DOI fields as advisory. Macros and inheritance are inconclusive; absence is not proof "
        "that a DOI exists, and syntax or identity is not verified by this presence check.",
        "tests": (
            "tests.test_bibliography_checks.BibliographyCheckTests.test_missing_doi_is_opt_in_advisory_and_type_specific",
            "tests.test_bibliography_checks.BibliographyCheckTests.test_macro_and_inherited_doi_presence_is_inconclusive",
        ),
        "fix": "Check whether this resource has a DOI and verify its identity before adding it. "
        "References without DOIs can be legitimate.",
    },
    {
        "code": "BIB105",
        "name": "bibliography.page_ranges",
        "title": "Bibliography page-range syntax",
        "description": "Check supported numeric and matching-prefix page ranges for malformed "
        "or descending endpoints. Single pages and comma-separated ranges are supported; Roman "
        "numerals, macros and unfamiliar notations are explicitly inconclusive.",
        "tests": (
            "tests.test_bibliography_checks.BibliographyCheckTests.test_page_ranges_accept_single_prefixed_and_multiple_ranges",
            "tests.test_bibliography_checks.BibliographyCheckTests.test_page_range_unknown_notation_and_macros_are_inconclusive",
        ),
        "fix": "Verify the page endpoints and notation against the cited resource. Do not replace "
        "article identifiers or unfamiliar page notation with guessed numeric ranges.",
    },
    {
        "code": "BIB106",
        "name": "bibliography.url_syntax",
        "title": "Bibliography URL syntax",
        "description": "Validate literal HTTP, HTTPS and FTP URL structure, host, port, whitespace "
        "and percent escapes. Other schemes and TeX markup are inconclusive. No request is made "
        "and syntax success does not establish reachability.",
        "tests": (
            "tests.test_bibliography_checks.BibliographyCheckTests.test_url_syntax_distinguishes_valid_and_malformed_literals",
            "tests.test_bibliography_checks.BibliographyCheckTests.test_url_macros_markup_and_other_schemes_are_inconclusive",
        ),
        "fix": "Correct the URL syntax using the intended resource address, then verify the "
        "destination separately if desired. No remote URL was contacted.",
    },
    {
        "code": "BIB107",
        "name": "bibliography.capitalization",
        "title": "Possible title capitalization loss",
        "description": "When explicitly enabled, flag unprotected uppercase or mixed-case title "
        "tokens as a style-dependent advisory. Existing braces are retained; macros are "
        "inconclusive and no capitalization is changed.",
        "tests": (
            "tests.test_bibliography_checks.BibliographyCheckTests.test_capitalization_is_opt_in_and_respects_nested_braces",
            "tests.test_bibliography_checks.BibliographyCheckTests.test_capitalization_macros_are_inconclusive",
        ),
        "fix": "Inspect the rendered title under the selected bibliography style. Add protection "
        "only for case-sensitive names or terms whose capitalization should survive that style.",
    },
    {
        "code": "BIB108",
        "name": "bibliography.fuzzy_duplicates",
        "title": "Possible duplicate bibliography works",
        "description": "When explicitly enabled, compare bounded literal titles at the configured "
        "similarity threshold within matching normalized author/year groups. Candidates are "
        "heuristic, separate from exact DOI identity, and never merged. Missing metadata, macros "
        "and comparison limits are inconclusive.",
        "tests": (
            "tests.test_bibliography_checks.BibliographyCheckTests.test_fuzzy_duplicates_are_advisory_distinct_from_exact_dois",
            "tests.test_bibliography_checks.BibliographyCheckTests.test_fuzzy_duplicates_report_missing_metadata_and_budget_uncertainty",
        ),
        "fix": "Compare the candidate works, versions, identifiers and metadata manually. Similar "
        "titles do not authorize merging, replacing a preprint, or renaming citation keys.",
    },
)
