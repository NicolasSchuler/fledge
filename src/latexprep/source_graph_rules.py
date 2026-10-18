"""Data-only catalogue for the literal source graph.

The graph covers the files the scan cannot read, the constructs it cannot follow, its
traversal limits and the choice of the main file, and the flat-layout errors that follow
from them. A gap in the graph makes every check that depends on the source incomplete, so
these findings are mandatory unless they only advise on how a reference was written.
"""

from .rule_definition import Rule

RULES = (
    Rule(
        code="TEX301",
        name="source.encoding",
        title="Unreadable or non-UTF-8 source",
        description=(
            "Report a project source that could not be read or is not valid UTF-8. The file "
            "is neither scanned nor changed, so its commands and references are unknown and "
            "the checks that depend on them are incomplete."
        ),
        tests=(
            "tests.test_source.SourceTests.test_read_failures_and_encoding_failures_are_told_apart_without_local_paths",
        ),
        fix="Make the file readable and convert an explicit copy to UTF-8, then repeat the check.",
        mandatory=True,
    ),
    Rule(
        code="TEX302",
        name="source.size_limit",
        title="Source size limit",
        description=(
            "Report source parsing that exceeded the per-file or total size limit. Files "
            "beyond the limit are not read, so the checks that depend on them are incomplete."
        ),
        tests=(
            "tests.test_source.SourceTests.test_unsupported_encoding_and_large_files_are_explicit",
        ),
        fix="Reduce the selected source set or inspect the large file separately.",
        mandatory=True,
    ),
    Rule(
        code="TEX303",
        name="source.link",
        title="Symbolic-link sources",
        description=(
            "Report a source file that is a symbolic link. Links are outside the supported "
            "source subset: they are not read, followed or rewritten, and the checks that "
            "depend on the file are incomplete."
        ),
        tests=(
            "tests.test_source.SourceTests.test_selected_analysis_requires_readable_sources_and_rejects_linked_dependencies",
        ),
        fix="Supply the actual project file inside the project instead of a link.",
        mandatory=True,
    ),
    Rule(
        code="TEX304",
        name="source.command_limit",
        title="TeX command count limit",
        description=(
            "Report a file, or the selected sources together, with more TeX commands than "
            "the scan examines. Nothing past the limit is inspected, so the checks that depend "
            "on it are incomplete."
        ),
        tests=("tests.test_source.SourceTests.test_lexical_bounds_report_their_cause_and_line",),
        fix="Split the file or reduce the selected source set.",
        mandatory=True,
    ),
    Rule(
        code="TEX305",
        name="source.argument_limit",
        title="Command argument limit",
        description=(
            "Report a brace group that is never closed, a command argument longer than the "
            "scan allows, or arguments that together exceed the supported size. The rest of "
            "the file cannot be read reliably, so its commands are not inspected."
        ),
        tests=(
            "tests.test_source.SourceTests.test_unclosed_and_oversized_arguments_are_bounded_and_explicit",
            "tests.test_source.SourceTests.test_lexical_bounds_report_their_cause_and_line",
        ),
        fix=(
            "Close the brace group, or move long text out of the command argument, for "
            "example into its own file."
        ),
        mandatory=True,
    ),
    Rule(
        code="TEX306",
        name="source.literal_unclosed",
        title="Unclosed literal region",
        description=(
            "Report a verbatim-like region, such as a verbatim environment or a \\verb "
            "delimiter, that has no closing delimiter. Later commands cannot be inspected "
            "reliably, so the checks that depend on them are incomplete."
        ),
        tests=(
            "tests.test_source_graph_findings.SourceGraphFindingTests.test_an_unclosed_literal_region_is_reported_with_its_line",
        ),
        fix="Close the literal region or remove the stray delimiter.",
        mandatory=True,
    ),
    Rule(
        code="TEX307",
        name="source.dynamic_path",
        title="Non-literal file paths",
        description=(
            "Report a file command without a supported literal filename argument, such as a "
            "path built from a macro. The file cannot be followed, packaged or rewritten, so "
            "the checks that depend on it are incomplete."
        ),
        tests=(
            "tests.test_source.SourceTests.test_selected_analysis_reports_unsupported_or_unresolved_dependencies_as_incomplete",
        ),
        fix="Use a plain literal filename in the file command.",
        mandatory=True,
    ),
    Rule(
        code="TEX308",
        name="source.path_literal_context",
        title="Comments or literal commands inside a path",
        description=(
            "Report a file command whose path argument contains a comment or a literal "
            "command such as \\verb. The scan cannot tell which text names the file."
        ),
        tests=(
            "tests.test_source_graph_findings.SourceGraphFindingTests.test_a_comment_inside_a_path_argument_is_reported",
        ),
        fix="Use a plain filename argument without comments or literal commands.",
        mandatory=True,
    ),
    Rule(
        code="TEX309",
        name="source.nested_file_command",
        title="File commands inside groups or macro arguments",
        description=(
            "Report a file command inside a group or macro argument. Its execution context "
            "is not established, so the scan does not follow the reference."
        ),
        tests=(
            "tests.test_source_graph_findings.SourceGraphFindingTests.test_a_file_command_inside_a_group_or_macro_is_reported",
        ),
        fix="Use a direct literal reference outside groups and macro arguments.",
        mandatory=True,
    ),
    Rule(
        code="TEX310",
        name="source.conditional_dependency",
        title="Conditional file references",
        description=(
            "Report a file reference inside TeX conditional control flow. The static scan "
            "does not establish which branch executes. Toolchain classes and packages that "
            "the recorded build covers are not reported."
        ),
        tests=(
            "tests.test_source.SourceTests.test_conditional_uncertainty_covers_only_enclosed_project_references",
        ),
        fix=(
            "Make the dependency unconditional so it can be followed; with layout = "
            '"preserve" the files the build read are shipped anyway.'
        ),
        mandatory=True,
    ),
    Rule(
        code="TEX311",
        name="source.custom_path",
        title="Unknown commands with file-like arguments",
        description=(
            "Report an unrecognized command whose argument looks like a project file path. "
            "Its lookup semantics are unknown, so the file cannot be followed or rewritten."
        ),
        tests=(
            "tests.test_source.SourceTests.test_selected_analysis_reports_unsupported_or_unresolved_dependencies_as_incomplete",
        ),
        fix="Name the file with a supported command such as \\input or \\includegraphics.",
        mandatory=True,
    ),
    Rule(
        code="TEX312",
        name="source.unsupported_command",
        title="Commands that read files at run time",
        description=(
            "Report commands such as \\IfFileExists or \\directlua, and font commands without "
            "an explicit local font file, whose paths or lookups fall outside the supported "
            "literal subset."
        ),
        tests=(
            "tests.test_source.SourceTests.test_selected_analysis_reports_unsupported_or_unresolved_dependencies_as_incomplete",
        ),
        fix="Replace the command with a literal file reference the scan can follow.",
        mandatory=True,
    ),
    Rule(
        code="TEX313",
        name="source.subfile_structure",
        title="Unsupported subfile structure",
        description=(
            "Report a subfile that lacks one literal subfiles class and document "
            "environment or an explicit parent path naming the selected main file, and the "
            "subfiles v1 preamble mode. Their content is not interpreted."
        ),
        tests=(
            "tests.test_source_graph_findings.SourceGraphFindingTests.test_the_subfiles_v1_preamble_mode_is_reported_as_unsupported_structure",
        ),
        fix=(
            "Give the subfile one literal subfiles class option naming the main file, or "
            "select the parent document."
        ),
        mandatory=True,
    ),
    Rule(
        code="TEX314",
        name="source.font_options",
        title="Unsupported font options",
        description=(
            "Report a font command whose options contain masked text, select additional faces "
            "or dynamic filenames, or give a Path that is not a single literal directory. "
            "Only Path, Ligatures, Scale, Renderer and Numbers are followed."
        ),
        tests=(
            "tests.test_source_graph_findings.SourceGraphFindingTests.test_font_options_outside_the_supported_set_are_reported",
        ),
        fix="Use only the supported options and one literal Path directory ending in a slash.",
        mandatory=True,
    ),
    Rule(
        code="TEX315",
        name="source.path_option",
        title="File-like options of file commands",
        description=(
            "Report an option of a file command that looks like a path, and a subfiles class "
            "compiled as the main root. Their lookup rules are not followed. Advisory about "
            "how the reference was written."
        ),
        tests=(
            "tests.test_source.SourceTests.test_subfiles_parent_path_option_is_explicitly_unsupported",
        ),
        fix=(
            "Simplify the option so it no longer looks like a file path, or select the "
            "parent document."
        ),
    ),
    Rule(
        code="TEX316",
        name="source.path_identity",
        title="Declared names that contain a directory",
        description=(
            "Report \\ProvidesPackage, \\ProvidesClass or \\ProvidesFile naming a directory. "
            "A flat layout would move the file and change the declared identity. Advisory."
        ),
        tests=(
            "tests.test_source_graph_findings.SourceGraphFindingTests.test_a_declared_name_with_a_directory_is_an_advisory",
        ),
        fix='Declare the plain name, or use layout = "preserve" to keep the file in place.',
    ),
    Rule(
        code="TEX317",
        name="source.scoped_search_path",
        title="Scoped graphics search paths",
        description=(
            "Report a \\graphicspath inside a group or macro definition, which is outside "
            "the supported subset. Advisory about how the search path was written."
        ),
        tests=(
            "tests.test_source_graph_findings.SourceGraphFindingTests.test_graphics_search_paths_outside_the_supported_form_are_advisories",
        ),
        fix="Declare \\graphicspath once, at the top level of the preamble.",
    ),
    Rule(
        code="TEX318",
        name="source.dynamic_search_path",
        title="Non-literal graphics search paths",
        description=(
            "Report a \\graphicspath that is not a literal list of directories such as "
            "{{figures/}{plots/}}. Advisory about how the search path was written."
        ),
        tests=(
            "tests.test_source_graph_findings.SourceGraphFindingTests.test_graphics_search_paths_outside_the_supported_form_are_advisories",
        ),
        fix="List the directories literally, for example \\graphicspath{{figures/}{plots/}}.",
    ),
    Rule(
        code="TEX319",
        name="source.search_path_syntax",
        title="Graphics search-path syntax",
        description=(
            "Report a \\graphicspath directory that does not end in a slash. The scan does "
            "not reinterpret it. Advisory about how the search path was written."
        ),
        tests=(
            "tests.test_source_graph_findings.SourceGraphFindingTests.test_graphics_search_paths_outside_the_supported_form_are_advisories",
        ),
        fix="End every \\graphicspath directory with a slash.",
    ),
    Rule(
        code="TEX320",
        name="source.input_cycle",
        title="Cyclic source inputs",
        description=(
            "Report a source file that is read again while it is being read. Execution order "
            "is not established, so the cycle is not followed."
        ),
        tests=(
            "tests.test_source_graph_findings.SourceGraphFindingTests.test_a_cycle_of_inputs_is_reported_once",
        ),
        fix="Remove the circular \\input or \\include.",
        mandatory=True,
    ),
    Rule(
        code="TEX321",
        name="source.input_depth",
        title="Source input depth limit",
        description=(
            "Report a chain of inputs deeper than the bounded traversal depth. Files below "
            "the limit are not inspected."
        ),
        tests=(
            "tests.test_source.SourceTests.test_deep_input_chains_stop_before_python_recursion_limit",
        ),
        fix="Reduce the nesting of \\input and \\include.",
        mandatory=True,
    ),
    Rule(
        code="TEX322",
        name="source.expansion_limit",
        title="Dependency traversal budget",
        description=(
            "Report a source graph whose traversal exceeded its bounded command budget. "
            "The commands after the limit are not followed."
        ),
        tests=(
            "tests.test_source_graph_findings.SourceGraphFindingTests.test_a_traversal_over_its_command_budget_is_reported",
        ),
        fix="Reduce the selected source set or split the project.",
        mandatory=True,
    ),
    Rule(
        code="TEX323",
        name="source.context_ambiguity",
        title="References that depend on the reading directory",
        description=(
            "Report one reference that selects different files when it is read under "
            "different path contexts, for example a file input from two directories."
        ),
        tests=(
            "tests.test_source.SourceTests.test_repeated_input_in_different_search_contexts_is_blocked",
        ),
        fix=(
            "Give the file a name that resolves to the same file from every directory that "
            "reads it."
        ),
        mandatory=True,
    ),
    Rule(
        code="TEX324",
        name="project.main_selection",
        title="No single main file selected",
        description=(
            "Report an input in which no single main file could be selected: several files "
            "declare a document class, or none does. Fledge does not pick one."
        ),
        tests=(
            "tests.test_workflow.WorkflowTests.test_input_without_a_document_says_no_main_file_was_found",
            "tests.test_workflow.WorkflowTests.test_ambiguous_root_blocks_without_running_tools",
        ),
        fix="Name the main file with --main (or the main setting).",
        mandatory=True,
    ),
    Rule(
        code="TEX325",
        name="source.root_ambiguous",
        title="Several document roots in the source analysis",
        description=(
            "Report that source analysis found several document roots and was given no main "
            "file. Where the workflow has already reported the choice as TEX324, this "
            "repeat is omitted."
        ),
        tests=(
            "tests.test_source.SourceTests.test_main_file_and_root_selection_errors_list_what_exists",
        ),
        fix="Select the main file explicitly with --main (or the main setting).",
        mandatory=True,
    ),
    Rule(
        code="TEX326",
        name="source.root_missing",
        title="No document root in the source analysis",
        description=(
            "Report that source analysis found no file with a literal document class and was "
            "given no main file. Where the workflow has already reported the choice as "
            "TEX324, this repeat is omitted."
        ),
        tests=(
            "tests.test_source_graph_findings.SourceGraphFindingTests.test_a_project_without_a_document_root_reports_it",
        ),
        fix="Select the main file explicitly with --main (or the main setting).",
        mandatory=True,
    ),
    Rule(
        code="TEX327",
        name="source.main_invalid",
        title="Invalid main file",
        description=(
            "Report a main file that is absolute, leaves the project folder or names no "
            "project file. The path is never read outside the project."
        ),
        tests=(
            "tests.test_source.SourceTests.test_main_file_and_root_selection_errors_list_what_exists",
            "tests.test_source.SourceTests.test_invalid_main_does_not_read_external_file",
        ),
        fix="Give the main file relative to the project folder, for example --main paper/main.tex.",
        mandatory=True,
    ),
    Rule(
        code="PKG006",
        name="flatten.unsupported_source",
        title="Source constructs a flat layout cannot move",
        description=(
            "A flat package needs every file reference followed and rewritten. Report each "
            "gap in the source graph as a blocking flat-layout error; the original rule is "
            "in details.source_rule."
        ),
        tests=(
            "tests.test_source.SourceTests.test_dynamic_and_unsupported_commands_are_flat_blockers",
            "tests.test_source.SourceTests.test_advice_names_the_layout_setting_only_where_it_applies",
        ),
        fix='Resolve the construct the finding names, or set layout = "preserve".',
        mandatory=True,
    ),
    Rule(
        code="PKG007",
        name="flatten.multiple_roots",
        title="One document root per flat package",
        description=(
            "Report a flat package that would also contain other document roots. They "
            "cannot share the top level with the selected document."
        ),
        tests=(
            "tests.test_source.SourceTests.test_flat_layout_with_other_roots_explains_the_limit_in_plain_words",
        ),
        fix="Select only this document and its dependencies.",
        mandatory=True,
    ),
    Rule(
        code="PKG008",
        name="flatten.unresolved_context",
        title="Unvisited sources with file references",
        description=(
            "Report a source file that no input reaches but whose file commands name other "
            "files. Its compilation context is unknown, so a flat layout cannot place them."
        ),
        tests=(
            "tests.test_source_graph_findings.SourceGraphFindingTests.test_an_unvisited_source_with_file_references_blocks_a_flat_layout",
        ),
        fix=(
            "Declare a literal input for the file, exclude it from the package, or set "
            'layout = "preserve".'
        ),
        mandatory=True,
    ),
)
