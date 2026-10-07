"""Data-only catalogue for explicitly selected manuscript structure checks."""

RULE_DEFINITIONS = (
    {
        "code": "MAN001",
        "name": "manuscript.package_allowlist",
        "title": "Allowed direct package declarations",
        "description": (
            "Compare direct literal declarations in the selected local graph with an explicit "
            "allowlist; system transitive packages are outside scope. Only constructs that "
            "could build or conditionally execute a declaration make the inventory "
            "inconclusive, and a conditional declaration is never reported as a violation."
        ),
        "tests": ("tests.test_manuscript_checks.ManuscriptDetailTests.test_package_allowlist",),
        "fix": (
            "Remove or replace the disallowed direct package declaration, or update the "
            "explicit allowlist after reviewing the requirement."
        ),
    },
    {
        "code": "MAN002",
        "name": "manuscript.layout_manipulation",
        "title": "Layout override candidates",
        "description": (
            "Advisory scan for literal negative spacing, page breaks, shrinking, forced "
            "placement and layout length overrides; intent is not inferred."
        ),
        "tests": ("tests.test_manuscript_checks.ManuscriptDetailTests.test_layout_manipulation",),
        "fix": (
            "Review the reported layout override in the compiled document and retain it only "
            "when intended and permitted by your requirements."
        ),
    },
    {
        "code": "MAN003",
        "name": "manuscript.abstract_citations",
        "title": "Abstract citation policy",
        "description": (
            "Apply an explicitly enabled no-citation policy to supported citation commands in "
            "literal abstracts."
        ),
        "tests": ("tests.test_manuscript_checks.ManuscriptDetailTests.test_abstract_citations",),
        "fix": (
            "Review the abstract citation against the explicitly selected policy and revise it "
            "manually if needed."
        ),
    },
    {
        "code": "MAN004",
        "name": "manuscript.abstract_abbreviations",
        "title": "Abstract abbreviation candidates",
        "description": (
            "Apply a selected forbid/define policy heuristically to uppercase tokens; "
            "definition candidates use expanded words followed by an abbreviation in "
            "parentheses."
        ),
        "tests": (
            "tests.test_manuscript_checks.ManuscriptDetailTests.test_abstract_abbreviations",
        ),
        "fix": (
            "Review the candidate abbreviation, add its definition if required, or add an "
            "intentional exception to the explicit policy."
        ),
    },
    {
        "code": "MAN005",
        "name": "manuscript.heading_depth",
        "title": "Maximum literal heading depth",
        "description": (
            "Compare supported literal heading commands with a user-supplied maximum depth; "
            "section has depth one."
        ),
        "tests": ("tests.test_manuscript_checks.ManuscriptDetailTests.test_heading_depth",),
        "fix": (
            "Review headings deeper than the configured limit and reorganize them manually if "
            "the limit applies."
        ),
    },
    {
        "code": "MAN006",
        "name": "manuscript.empty_sections",
        "title": "Apparently empty sections",
        "description": (
            "Advisory lexical scan for headings whose section subtree has no observable "
            "content. Unexpanded constructs inside a section make only that section "
            "inconclusive; bibliography and layout commands count as zero content."
        ),
        "tests": (
            "tests.test_manuscript_checks.ManuscriptDetailTests.test_empty_sections",
            "tests.test_manuscript_checks.ManuscriptDetailTests"
            ".test_preamble_declarations_are_not_body_constructs",
            "tests.test_manuscript_checks.ManuscriptDetailTests"
            ".test_unknown_constructs_make_their_own_region_inconclusive",
        ),
        "fix": (
            "Review the apparently empty section in the compiled manuscript and add content or "
            "remove its heading if appropriate."
        ),
    },
    {
        "code": "MAN007",
        "name": "manuscript.hardcoded_references",
        "title": "Hard-coded reference candidates",
        "description": (
            "Advisory scan for prose such as Figure 2, Table 3, Section 4 or Equation (5); "
            "literal examples and command arguments are excluded."
        ),
        "tests": ("tests.test_manuscript_checks.ManuscriptDetailTests.test_hardcoded_references",),
        "fix": (
            "Review the numbered reference and use a label/reference command when it refers to "
            "a manuscript object."
        ),
    },
    {
        "code": "MAN008",
        "name": "manuscript.required_metadata",
        "title": "Required literal author metadata",
        "description": (
            "Require nonempty explicitly selected title, author, affiliation, email and "
            "corresponding-author declarations in supported source commands; no identity or "
            "per-author completeness claim."
        ),
        "tests": (
            "tests.test_manuscript_checks.ManuscriptDetailTests.test_required_metadata",
            "tests.test_manuscript_checks.ManuscriptDetailTests"
            ".test_unknown_constructs_make_their_own_region_inconclusive",
        ),
        "fix": (
            "Supply the configured metadata using a supported literal command, or inspect the "
            "template-specific metadata manually."
        ),
    },
    {
        "code": "MAN009",
        "name": "manuscript.orcid",
        "title": "Literal ORCID syntax and checksum",
        "description": (
            "Validate literal identifiers in orcid, ORCID and orcidlink commands with the ISO "
            "7064 MOD 11-2 checksum; validity does not establish ownership."
        ),
        "tests": ("tests.test_manuscript_checks.ManuscriptDetailTests.test_orcid",),
        "fix": (
            "Correct the reported ORCID from the author's verified identifier; a valid "
            "checksum alone does not establish identity or ownership."
        ),
    },
    {
        "code": "MAN010",
        "name": "manuscript.required_declarations",
        "title": "Nonempty required declarations",
        "description": (
            "Require literal content under user-named headings or declaration{name}{body} "
            "commands; statement adequacy and applicability require human review."
        ),
        "tests": ("tests.test_manuscript_checks.ManuscriptDetailTests.test_required_declarations",),
        "fix": (
            "Supply a nonempty statement for the configured declaration and review its "
            "applicability and adequacy manually."
        ),
    },
    {
        "code": "MAN011",
        "name": "manuscript.float_captions",
        "title": "Required figure and table captions",
        "description": (
            "Require nonempty literal caption commands in supported figure/table environments, "
            "including starred environments and certain literal input boundaries. A float whose "
            "own region could not be interpreted is reported as inconclusive without hiding "
            "the floats that were confirmed."
        ),
        "tests": (
            "tests.test_manuscript_checks.ManuscriptDetailTests.test_float_captions",
            "tests.test_manuscript_checks.ManuscriptDetailTests"
            ".test_unrelated_uncertainty_never_hides_a_confirmed_violation",
            "tests.test_manuscript_checks.ManuscriptDetailTests"
            ".test_failure_messages_report_counts_and_first_locations",
        ),
        "fix": (
            "Add a nonempty caption to the reported figure or table, or review unsupported "
            "template-generated captions manually."
        ),
    },
    {
        "code": "MAN012",
        "name": "manuscript.float_labels",
        "title": "Required figure and table labels",
        "description": (
            "Require a nonempty literal label associated with each supported figure/table "
            "environment."
        ),
        "tests": ("tests.test_manuscript_checks.ManuscriptDetailTests.test_float_labels",),
        "fix": (
            "Add an unambiguous literal label to the reported figure or table, normally after "
            "its caption."
        ),
    },
    {
        "code": "MAN013",
        "name": "manuscript.float_label_order",
        "title": "Figure and table label placement",
        "description": (
            "Check that literal float labels follow a caption in source order; custom counter "
            "changes and subfloat semantics are inconclusive."
        ),
        "tests": ("tests.test_manuscript_checks.ManuscriptDetailTests.test_float_label_order",),
        "fix": (
            "Place the figure/table label after the corresponding caption and verify its "
            "resolved number after compilation."
        ),
    },
    {
        "code": "MAN014",
        "name": "manuscript.float_references",
        "title": "Figure and table references",
        "description": (
            "Require an external supported literal reference to each labeled figure/table; "
            "generated references and unsupported float structures are inconclusive."
        ),
        "tests": ("tests.test_manuscript_checks.ManuscriptDetailTests.test_float_references",),
        "fix": (
            "Add a meaningful reference to the figure or table in the manuscript, or review "
            "unsupported generated references manually."
        ),
    },
    {
        "code": "MAN015",
        "name": "manuscript.float_reference_order",
        "title": "Figure and table first-reference source order",
        "description": (
            "Compare first literal reference order separately for figures and tables with "
            "their source appearance; this does not infer rendered float order."
        ),
        "tests": ("tests.test_manuscript_checks.ManuscriptDetailTests.test_float_reference_order",),
        "fix": (
            "Review figure/table introductions in source order and the compiled document; "
            "float placement may differ from source order."
        ),
    },
    {
        "code": "MAN016",
        "name": "manuscript.figure_descriptions",
        "title": "Required figure descriptions",
        "description": (
            "Require nonempty literal Description commands for figures and flag obvious "
            "placeholder text; presence does not establish description quality or PDF "
            "accessibility."
        ),
        "tests": ("tests.test_manuscript_checks.ManuscriptDetailTests.test_figure_descriptions",),
        "fix": (
            "Add a useful nonempty Description for the figure and manually assess whether it "
            "conveys the information needed by readers."
        ),
    },
)
