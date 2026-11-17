"""Data-only catalogue for explicitly selected manuscript structure checks."""

from .rule_definition import Rule

RULES = (
    Rule(
        code="MAN001",
        name="manuscript.package_allowlist",
        title="Allowed direct package declarations",
        description=(
            "Compare direct literal declarations in the selected local graph with an explicit "
            "allowlist; system transitive packages are outside scope. Only constructs that "
            "could build or conditionally execute a declaration make the inventory "
            "inconclusive, and a conditional declaration is never reported as a violation."
        ),
        tests=("tests.test_manuscript_checks.ManuscriptDetailTests.test_package_allowlist",),
        fix=(
            "Remove or replace the disallowed direct package declaration, or update the "
            "explicit allowlist after reviewing the requirement."
        ),
        reviewable=True,
    ),
    Rule(
        code="MAN002",
        name="manuscript.layout_manipulation",
        title="Layout override candidates",
        description=(
            "Advisory scan for literal negative spacing, page breaks, shrinking, forced "
            "placement and layout length overrides, and for the usual ways of squeezing a "
            "paper under a page limit: geometry margin or text-area options, the fullpage "
            "and savetrees packages, caption skips below 3pt or caption fonts at footnote "
            "size or smaller (caption options or \\captionsetup), titlesec's compact "
            "option, \\titlespacing, zero or negative display skips and redefined float "
            "fractions such as \\topfraction; intent is not inferred."
        ),
        tests=(
            "tests.test_manuscript_checks.ManuscriptDetailTests.test_layout_manipulation",
            "tests.test_manuscript_checks.ManuscriptDetailTests"
            ".test_page_limit_squeezing_is_a_layout_override",
        ),
        fix=(
            "Review the reported layout override in the compiled document and retain it only "
            "when intended and permitted by your requirements."
        ),
        reviewable=True,
    ),
    Rule(
        code="MAN003",
        name="manuscript.abstract_citations",
        title="Abstract citation policy",
        description=(
            "Apply an explicitly enabled no-citation policy to supported citation commands in "
            "literal abstracts."
        ),
        tests=("tests.test_manuscript_checks.ManuscriptDetailTests.test_abstract_citations",),
        fix=(
            "Review the abstract citation against the explicitly selected policy and revise it "
            "manually if needed."
        ),
        reviewable=True,
    ),
    Rule(
        code="MAN004",
        name="manuscript.abstract_abbreviations",
        title="Abstract abbreviation candidates",
        description=(
            "Apply a selected forbid/define policy heuristically to uppercase tokens; "
            "definition candidates use expanded words followed by an abbreviation in "
            "parentheses."
        ),
        tests=("tests.test_manuscript_checks.ManuscriptDetailTests.test_abstract_abbreviations",),
        fix=(
            "Review the candidate abbreviation, add its definition if required, or add an "
            "intentional exception to the explicit policy."
        ),
        reviewable=True,
    ),
    Rule(
        code="MAN005",
        name="manuscript.heading_depth",
        title="Maximum literal heading depth",
        description=(
            "Compare supported literal heading commands with a user-supplied maximum depth; "
            "section has depth one."
        ),
        tests=("tests.test_manuscript_checks.ManuscriptDetailTests.test_heading_depth",),
        fix=(
            "Review headings deeper than the configured limit and reorganize them manually if "
            "the limit applies."
        ),
        reviewable=True,
    ),
    Rule(
        code="MAN006",
        name="manuscript.empty_sections",
        title="Apparently empty sections",
        description=(
            "Advisory lexical scan for headings whose section subtree has no observable "
            "content. Unexpanded constructs inside a section make only that section "
            "inconclusive; bibliography and layout commands count as zero content."
        ),
        tests=(
            "tests.test_manuscript_checks.ManuscriptDetailTests.test_empty_sections",
            "tests.test_manuscript_checks.ManuscriptDetailTests"
            ".test_preamble_declarations_are_not_body_constructs",
            "tests.test_manuscript_checks.ManuscriptDetailTests"
            ".test_unknown_constructs_make_their_own_region_inconclusive",
        ),
        fix=(
            "Review the apparently empty section in the compiled manuscript and add content or "
            "remove its heading if appropriate."
        ),
        reviewable=True,
    ),
    Rule(
        code="MAN007",
        name="manuscript.hardcoded_references",
        title="Hard-coded reference candidates",
        description=(
            "Advisory scan for prose such as Figure 2, Table 3, Section 4 or Equation (5); "
            "literal examples and command arguments are excluded."
        ),
        tests=("tests.test_manuscript_checks.ManuscriptDetailTests.test_hardcoded_references",),
        fix=(
            "Review the numbered reference and use a label/reference command when it refers to "
            "a manuscript object."
        ),
        reviewable=True,
    ),
    Rule(
        code="MAN008",
        name="manuscript.required_metadata",
        title="Required literal author metadata",
        description=(
            "Require nonempty explicitly selected title, author, affiliation, email and "
            "corresponding-author declarations in supported source commands, including ICML's "
            "icmltitle and icmlauthor and asmeconf's SetAuthors; literal text satisfies a field "
            "whatever unread command stands beside it. No identity or per-author completeness "
            "claim."
        ),
        tests=(
            "tests.test_manuscript_checks.ManuscriptDetailTests.test_required_metadata",
            "tests.test_manuscript_checks.ManuscriptDetailTests"
            ".test_unknown_constructs_make_their_own_region_inconclusive",
            "tests.test_manuscript_metadata_checks.AuthorMetadataTests"
            ".test_a_project_macro_that_may_drop_its_argument_leaves_the_author_open",
            "tests.test_manuscript_metadata_checks.AuthorMetadataTests"
            ".test_icml_title_and_author_commands_declare_the_fields",
        ),
        fix=(
            "Supply the configured metadata using a supported literal command, or inspect the "
            "template-specific metadata manually."
        ),
        reviewable=True,
    ),
    Rule(
        code="MAN009",
        name="manuscript.orcid",
        title="Literal ORCID syntax and checksum",
        description=(
            "Validate literal identifiers in orcid, ORCID and orcidlink commands with the ISO "
            "7064 MOD 11-2 checksum; validity does not establish ownership."
        ),
        tests=(
            "tests.test_manuscript_checks.ManuscriptDetailTests.test_orcid",
            "tests.test_manuscript_checks.ManuscriptDetailTests"
            ".test_obsolete_constructs_leave_the_orcid_check_decided",
            "tests.test_manuscript_metadata_checks.AuthorMetadataTests"
            ".test_identifiers_inside_orcidlink_are_checked_beside_unread_commands",
        ),
        fix=(
            "Correct the reported ORCID from the author's verified identifier; a valid "
            "checksum alone does not establish identity or ownership."
        ),
        reviewable=True,
    ),
    Rule(
        code="MAN010",
        name="manuscript.required_declarations",
        title="Nonempty required declarations",
        description=(
            "Require literal content under user-named headings or declaration{name}{body} "
            "commands; statement adequacy and applicability require human review."
        ),
        tests=("tests.test_manuscript_checks.ManuscriptDetailTests.test_required_declarations",),
        fix=(
            "Supply a nonempty statement for the configured declaration and review its "
            "applicability and adequacy manually."
        ),
        reviewable=True,
    ),
    Rule(
        code="MAN011",
        name="manuscript.float_captions",
        title="Required figure and table captions",
        description=(
            "Require nonempty literal caption commands in supported figure/table environments, "
            "including starred environments and certain literal input boundaries. A caption "
            "with literal text stands whatever else the float holds; a float without one is "
            "inconclusive only when a construct inside it, such as a project macro or a file "
            "that was not read, may supply the caption, and is named in the reasons."
        ),
        tests=(
            "tests.test_manuscript_checks.ManuscriptDetailTests.test_float_captions",
            "tests.test_manuscript_checks.ManuscriptDetailTests"
            ".test_unrelated_uncertainty_never_hides_a_confirmed_violation",
            "tests.test_manuscript_checks.ManuscriptDetailTests"
            ".test_failure_messages_report_counts_and_first_locations",
            "tests.test_manuscript_float_context.FloatEvidenceTests"
            ".test_an_unknown_macro_still_counts_where_the_evidence_is_missing",
            "tests.test_manuscript_float_context.FloatEvidenceTests.test_text_that_was_not_read_still_counts",
        ),
        fix=(
            "Add a nonempty caption to the reported figure or table, or review unsupported "
            "template-generated captions manually."
        ),
        reviewable=True,
    ),
    Rule(
        code="MAN012",
        name="manuscript.float_labels",
        title="Required figure and table labels",
        description=(
            "Require a nonempty literal label associated with each supported figure/table "
            "environment. A failure also counts the floats that could not be judged."
        ),
        tests=(
            "tests.test_manuscript_checks.ManuscriptDetailTests.test_float_labels",
            "tests.test_manuscript_float_context.FloatEvidenceTests.test_a_failure_says_how_many_floats_could_not_be_judged",
        ),
        fix=(
            "Add an unambiguous literal label to the reported figure or table, normally after "
            "its caption."
        ),
        reviewable=True,
    ),
    Rule(
        code="MAN013",
        name="manuscript.float_label_order",
        title="Figure and table label placement",
        description=(
            "Check that literal float labels follow a caption in source order; a label inside "
            "the caption's argument follows it, and with several captions, as in side-by-side "
            "minipages, each label follows the caption before it. Subfigure captions and "
            "labels belong to their subfigure and equation labels to their equation. Control "
            "flow, project macros, files that were not read and custom counter changes inside "
            "a float are inconclusive, because they may hold a misplaced label."
        ),
        tests=(
            "tests.test_manuscript_checks.ManuscriptDetailTests.test_float_label_order",
            "tests.test_manuscript_float_context.FloatEvidenceTests.test_a_label_before_its_caption_still_fails",
            "tests.test_manuscript_float_context.FloatEvidenceTests.test_labels_that_may_belong_to_another_caption_stay_open",
        ),
        fix=(
            "Place the figure/table label after the corresponding caption and verify its "
            "resolved number after compilation."
        ),
        reviewable=True,
    ),
    Rule(
        code="MAN014",
        name="manuscript.float_references",
        title="Figure and table references",
        description=(
            "Require an external supported literal reference to each labeled figure/table, "
            "from the text or from another float. A project macro whose definition holds a "
            "reference is read at each use, a reference in the argument of a project macro "
            "that typesets it counts, and a range refers to the floats between its ends. A "
            "float without a reference is inconclusive, not failed, when a reference that "
            "could not be read may name it."
        ),
        tests=(
            "tests.test_manuscript_checks.ManuscriptDetailTests.test_float_references",
            "tests.test_manuscript_float_context.FloatEvidenceTests"
            ".test_a_reference_macro_that_names_another_float_or_hides_its_reference",
            "tests.test_manuscript_float_context.FloatEvidenceTests.test_a_range_refers_to_the_floats_between_its_ends",
        ),
        fix=(
            "Add a meaningful reference to the figure or table in the manuscript, or review "
            "unsupported generated references manually."
        ),
        reviewable=True,
    ),
    Rule(
        code="MAN015",
        name="manuscript.float_reference_order",
        title="Figure and table first-reference source order",
        description=(
            "Compare first literal reference order separately for figures and tables with "
            "their source appearance and name the floats cited late; this does not infer "
            "rendered float order. Floats that the reference check reports as unreferenced "
            "are left out of the comparison."
        ),
        tests=(
            "tests.test_manuscript_checks.ManuscriptDetailTests.test_float_reference_order",
            "tests.test_manuscript_float_context.FloatEvidenceTests.test_the_order_check_names_the_float_cited_late",
            "tests.test_manuscript_float_context.FloatEvidenceTests"
            ".test_a_float_whose_first_reference_may_lie_elsewhere_keeps_the_order_open",
        ),
        fix=(
            "Review figure/table introductions in source order and the compiled document; "
            "float placement may differ from source order."
        ),
        reviewable=True,
    ),
    Rule(
        code="MAN016",
        name="manuscript.figure_descriptions",
        title="Required figure descriptions",
        description=(
            "Require a description of each figure: a nonempty literal Description command, "
            "or alt text on every \\includegraphics (alt=...) or graphics marked as "
            "decorative (artifact); obvious placeholder text is flagged. Presence does not "
            "establish description quality or PDF accessibility."
        ),
        tests=(
            "tests.test_manuscript_checks.ManuscriptDetailTests.test_figure_descriptions",
            "tests.test_manuscript_checks.ManuscriptDetailTests"
            ".test_figure_alt_text_and_artifacts_count_as_descriptions",
        ),
        fix=(
            "Add a useful nonempty Description for the figure and manually assess whether it "
            "conveys the information needed by readers."
        ),
        reviewable=True,
    ),
    Rule(
        code="MAN017",
        name="manuscript.line_numbers",
        title="Required or forbidden review line numbers",
        description=(
            "Opt-in (require_line_numbers or forbid_line_numbers): find literal lineno "
            "activation, meaning \\linenumbers and its running/pagewise variants or "
            "environments, the lineno linenumbers option, or a lineno/linenumbers class option. "
            "Class-specific review modes and macro-generated activation are not interpreted; "
            "conditional activation is inconclusive."
        ),
        tests=(
            "tests.test_submission_readiness.LineNumberTests.test_required_line_numbers",
            "tests.test_submission_readiness.LineNumberTests.test_forbidden_line_numbers",
        ),
        fix=(
            "For review copies, load lineno and call \\linenumbers (or use the class option the "
            "venue documents); for final versions, remove the activation. If the class numbers "
            "lines through another option, confirm it in the PDF and suppress this finding."
        ),
    ),
    Rule(
        code="MAN018",
        name="manuscript.output_drivers",
        title="Output drivers fixed in the sources",
        description=(
            "Opt-in (forbid_output_drivers): report output driver options such as pdftex or "
            "dvips on graphics, graphicx, color, xcolor and hyperref, or among the class "
            "options, and packages such as psfig that work with one driver only. These "
            "packages detect the driver themselves, so a fixed one breaks the build on a "
            "service that chooses the engine, as arXiv does. A declaration that could not be "
            "read leaves an informational inconclusive result."
        ),
        tests=("tests.test_manuscript_checks.ManuscriptDetailTests.test_output_drivers",),
        fix=(
            "Remove the driver option and let the package detect the driver; replace psfig "
            "by graphicx and \\includegraphics."
        ),
        reviewable=True,
        reference_url="https://ctan.org/pkg/graphicx",
    ),
    Rule(
        code="MAN019",
        name="manuscript.document_metadata",
        title="Tagged PDF with a document language",
        description=(
            "Opt-in (require_document_metadata): require a literal \\DocumentMetadata "
            "declaration before \\documentclass with a lang key and tagging=on or a PDF/UA "
            "pdfstandard, which ask a current LaTeX release for a tagged PDF with a document "
            "language. A declaration that could not be read leaves an informational "
            "inconclusive result. The declaration does not establish that the PDF is "
            "accessible; the PDF checks PDF208 and PDF209 measure the result."
        ),
        tests=("tests.test_manuscript_checks.ManuscriptDetailTests.test_document_metadata",),
        fix=(
            "Write \\DocumentMetadata{lang=en, pdfstandard=ua-2, tagging=on} before "
            "\\documentclass, with the document's language, and check the tagged PDF."
        ),
        reviewable=True,
        reference_url="https://latex3.github.io/tagging-project/documentation/usage-instructions",
    ),
)
