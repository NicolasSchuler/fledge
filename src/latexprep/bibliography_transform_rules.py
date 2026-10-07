"""Catalogue entries for explicit, atomic bibliography transformations."""

RULE_DEFINITIONS = (
    {
        "code": "BIB201",
        "name": "bibliography.format_entries",
        "title": "Lossless bibliography field layout",
        "description": "Propose whitespace and trailing-comma layout for declared bibliography "
        "entries. Literal values, brace protection, concatenations, unknown fields, comments, "
        "string declarations, preambles, UTF-8 BOM and newline style are retained. Malformed or "
        "unreadable selected resources block the entire proposal; originals are never written.",
        "tests": (
            "tests.test_bibliography_transform.BibliographyTransformTests.test_format_preserves_values_comments_macros_bom_and_crlf",
            "tests.test_bibliography_transform.BibliographyTransformTests.test_format_refuses_partial_or_unreadable_bibliographies",
            "tests.test_bibliography_transform.BibliographyTransformTests.test_resource_limits_and_links_refuse_proposals",
        ),
        "fix": "Repair the indicated resource or source-scope ambiguity, then review the proposed "
        "diff and verify the rendered bibliography in the prepared copy.",
    },
    {
        "code": "BIB202",
        "name": "bibliography.remove_fields",
        "title": "Explicit bibliography field removal",
        "description": "Remove explicitly named non-relationship fields in selected resources. "
        "No private-field list is assumed; unknown fields, values outside selected fields, string "
        "macros and percent comments are preserved. Invalid input blocks the complete proposal.",
        "tests": (
            "tests.test_bibliography_transform.BibliographyTransformTests.test_selected_field_removal_retains_other_fields_and_comments",
            "tests.test_bibliography_transform.BibliographyTransformTests.test_selected_field_removal_noop_and_damaged_file_refusal",
        ),
        "fix": "Choose exact field names to remove, repair unsupported resources and inspect the "
        "resulting diff; no field is treated as private without explicit selection.",
    },
    {
        "code": "BIB203",
        "name": "bibliography.cited_only",
        "title": "Conservative cited-only bibliography",
        "description": "Propose removal of entries unreachable from the selected literal citation "
        "graph, preserving nocite, aliases, relationships, entry sets, preambles and all string "
        "declarations. Multiple roots, dynamic citations, filters, missing keys, ambiguous aliases "
        "and unresolved relationships block pruning. No arbitrary-TeX coverage is claimed.",
        "tests": (
            "tests.test_bibliography_transform.BibliographyTransformTests.test_cited_only_retains_nocite_relationship_closure_aliases_sets_and_macros",
            "tests.test_bibliography_transform.BibliographyTransformTests.test_cited_only_nocite_star_and_fully_used_noop",
            "tests.test_bibliography_transform.BibliographyTransformTests.test_cited_only_refuses_uncertain_usage_and_relationships",
            "tests.test_bibliography_transform.BibliographyTransformTests.test_imported_resource_declarations_contribute_to_the_selected_graph",
            "tests.test_bibliography_transform.BibliographyTransformTests.test_subfile_citation_context_is_inconclusive_before_any_prune_or_rename",
        ),
        "fix": "Resolve citation-scope uncertainty, or retain the complete bibliography. "
        "Review removed entries and rebuild the selected manuscript before releasing a copy.",
    },
    {
        "code": "BIB204",
        "name": "bibliography.key_mapping",
        "title": "Reviewed bibliography key mapping",
        "description": "Apply explicit key renames and donor-to-retained-target merge mappings "
        "atomically to entry keys, supported literal citations and relationships. A merge retains "
        "target metadata exactly and drops the explicitly selected donor; identity and conflicts "
        "are never inferred. Dynamic uses, multiple roots, unknown keys and collisions block the "
        "complete proposal.",
        "tests": (
            "tests.test_bibliography_transform.BibliographyTransformTests.test_key_rename_updates_only_literal_uses_and_relationships_atomically",
            "tests.test_bibliography_transform.BibliographyTransformTests.test_reviewed_merge_keeps_target_metadata_and_redirects_donor_aliases",
            "tests.test_bibliography_transform.BibliographyTransformTests.test_key_mapping_noop_and_invalid_reviewed_mappings",
            "tests.test_bibliography_transform.BibliographyTransformTests.test_key_mapping_refuses_dynamic_uses_alias_collision_and_mixed_scope",
            "tests.test_bibliography_transform.BibliographyTransformTests.test_imported_citations_are_retained_and_renamed_in_their_actual_sources",
            "tests.test_bibliography_transform.BibliographyTransformTests.test_subfile_citation_context_is_inconclusive_before_any_prune_or_rename",
        ),
        "fix": "Review exact source and target keys and merge metadata, resolve uncertain uses, "
        "then retry the complete mapping and verify rendered citations.",
    },
    {
        "code": "BIB205",
        "name": "bibliography.field_edits",
        "title": "Guarded bibliography metadata edits",
        "description": "Apply explicit key/field/expected/replacement rows to literal metadata, "
        "including capitalization brace protection. None represents field absence or removal. "
        "Expected contents must match exactly; macros, concatenations, repeated fields and "
        "invalid replacement syntax block the complete proposal. No metadata is guessed.",
        "tests": (
            "tests.test_bibliography_transform.BibliographyTransformTests.test_guarded_field_edits_preserve_protection_and_add_delete_literal_metadata",
            "tests.test_bibliography_transform.BibliographyTransformTests.test_guarded_field_edit_noop_and_stale_or_invalid_replacements",
            "tests.test_bibliography_transform.BibliographyTransformTests.test_guarded_field_edits_refuse_macros_concatenation_and_repeated_fields",
        ),
        "fix": "Verify the current literal value and intended replacement against the cited work. "
        "Supply explicit capitalization protection and inspect the rendered result.",
    },
    {
        "code": "BIB206",
        "name": "bibliography.order_entries",
        "title": "Preservable bibliography key ordering",
        "description": "Opt-in literal key ordering moves intact entry bytes within a resource. "
        "Interleaved comments, free text and macros block moves across their boundaries. Sorting "
        "that would place a cross-reference parent before its child or requires resolving an "
        "external/macro parent is refused; bibliography backend semantics are not inferred.",
        "tests": (
            "tests.test_bibliography_transform.BibliographyTransformTests.test_ordering_retains_prefix_macros_and_exact_entry_bytes",
            "tests.test_bibliography_transform.BibliographyTransformTests.test_ordering_refuses_ambiguous_comment_macro_and_crossref_boundaries",
        ),
        "fix": "Preserve the current order where comments, macros or cross-references make sorting "
        "ambiguous, or resolve those boundaries explicitly before retrying.",
    },
)
