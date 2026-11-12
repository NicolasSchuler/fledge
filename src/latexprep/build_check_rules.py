"""Data-only catalogue for checks over build and recorder evidence."""

from .rule_definition import Rule

RULES = (
    Rule(
        code="BLD101",
        name="build.overfull_tolerance",
        title="Configured overfull-box tolerance",
        description=(
            "Compare numerical final-log box overflow with an explicit tolerance in TeX "
            "points; incomplete build/log evidence cannot pass."
        ),
        tests=("tests.test_build_checks.BuildCheckTests.test_overfull_tolerance",),
        fix="Reflow the reported content or correct the explicit tolerance, then rebuild.",
    ),
    Rule(
        code="BLD102",
        name="build.loaded_package_policy",
        title="Recorder-observed package policy",
        description=(
            "Apply explicit allow/deny/required sets to transitive .sty and .cls inputs "
            "observed by the build recorder."
        ),
        tests=(
            "tests.test_build_checks.BuildCheckTests.test_loaded_package_policy",
            "tests.test_build_checks.BuildCheckTests."
            "test_recorder_collects_loaded_packages_and_rejects_empty_trace",
        ),
        fix=(
            "Review all loaded class/package filenames, replace forbidden dependencies, or "
            "update the explicit policy."
        ),
        not_checked_area="forbidden_packages",
    ),
    Rule(
        code="BLD103",
        name="build.package_date",
        title="Minimum declared package date",
        description=(
            "Compare a loaded package's bounded literal ProvidesClass/ProvidesPackage header "
            "date to an author-specified minimum; this is not authenticity verification."
        ),
        tests=(
            "tests.test_build_checks.BuildCheckTests.test_package_dates",
            "tests.test_build_checks.BuildCheckTests."
            "test_inactive_and_ambiguous_package_headers_never_supply_a_required_date",
        ),
        fix=(
            "Supply the intended compatible class/package release, or update the explicit "
            "minimum date after review."
        ),
        reviewable=True,
    ),
    Rule(
        code="BLD104",
        name="build.local_package_shadow",
        title="Existing local package shadow",
        description=(
            "Look up a system counterpart from an empty sandboxed workspace for each loaded "
            "local class/package; report intentional-override candidates as advisories."
        ),
        tests=("tests.test_build_checks.BuildCheckTests.test_shadow_lookup",),
        fix=(
            "Compare the local class/package with the intended template or toolchain and "
            "retain overrides only when deliberate."
        ),
        reviewable=True,
    ),
    Rule(
        code="BLD105",
        name="build.tex_distribution",
        title="Local TeX distribution release",
        description=(
            "Record the TeX distribution named by the baseline build's single engine version "
            "probe (for example TeX Live 2026) as information. Opt-in "
            "build_checks.expected_texlive_year turns a different or unidentified release into "
            "a warning; missing version evidence is inconclusive, never a pass."
        ),
        tests=(
            "tests.test_build_checks.TexDistributionTests.test_records_texlive_year_and_compares_explicit_expectation",
            "tests.test_build_checks.TexDistributionTests.test_missing_or_unrecognized_version_is_inconclusive",
        ),
        fix=(
            "Build with the TeX Live release the receiving service documents (arXiv lists its "
            "current one), or correct expected_texlive_year; rerun after changing toolchains."
        ),
    ),
    Rule(
        code="BLD106",
        name="build.engine",
        title="Engine selection",
        description=(
            "Record which TeX engine built the document and what chose it: the engine setting "
            "(--engine or the settings file), the main file's % !TEX program comment, or the "
            "pdfLaTeX default. A comment that names a program Fledge cannot run is a warning."
        ),
        tests=(
            "tests.test_engine_selection.EngineChoiceTests.test_priority_setting_comment_default",
            "tests.test_engine_selection.EngineChoiceTests.test_unusable_comment_is_a_warning",
        ),
        fix=(
            "Choose the engine the document is written for with --engine, the engine setting "
            "or a % !TEX program comment among the first lines of the main file."
        ),
    ),
    Rule(
        code="BLD107",
        name="build.latexmk_configuration",
        title="Project latexmk configuration not executed",
        description=(
            "Report a latexmkrc or .latexmkrc in the project, which Fledge never executes "
            "(latexmk runs with -norc), and what it would change: the engine its $pdf_mode "
            "selects, custom rules and shell escape. A different engine is a warning."
        ),
        tests=(
            "tests.test_engine_selection.LatexmkConfigurationTests.test_engine_and_rules_are_named",
            "tests.test_engine_selection.LatexmkConfigurationTests.test_matching_or_absent_configuration",
        ),
        fix=(
            "Choose the engine with --engine, the engine setting or a % !TEX program comment; "
            "Fledge's own rules already run makeindex, makeglossaries and the nomencl index."
        ),
        reviewable=True,
    ),
    Rule(
        code="BLD108",
        name="build.host_fonts",
        title="Fonts outside the project and the TeX distribution",
        description=(
            "Report fonts that a XeLaTeX or LuaLaTeX build loaded from this computer's font "
            "folders rather than from the project or the TeX distribution, read from the XDV "
            "file or the LuaLaTeX font cache. Paths are compared after resolving links, so a "
            "system font folder entry that links into a TeX tree is a TeX font. The archive "
            "rebuilds here, but not on a computer that lacks those fonts."
        ),
        tests=(
            "tests.test_build_fonts.HostFontTests.test_fonts_outside_the_trees_are_reported",
            "tests.test_build_fonts.HostFontTests.test_project_and_distribution_fonts_pass",
        ),
        fix=(
            "Copy the font files into the project and load them by file name, if their license "
            "allows, or use a font that the TeX distribution ships."
        ),
        reviewable=True,
    ),
    Rule(
        code="BLD011",
        name="build.unused_global_option",
        title="Unused class options",
        description=(
            "Report LaTeX's warning that a class option was used by neither the class nor any "
            "package, which usually means a misspelled option such as twocolumns."
        ),
        tests=(
            "tests.test_build_log.NewLogRuleTests.test_each_rule_reports_its_message",
            "tests.test_build_log.NewLogRuleTests.test_clean_log_reports_none_of_the_new_rules",
        ),
        fix=(
            "Correct the spelling of the reported option in \\documentclass, or remove it if "
            "no package uses it."
        ),
        reviewable=True,
    ),
    Rule(
        code="BLD012",
        name="build.float_too_large",
        title="Floats too large for the page",
        description=(
            "Report LaTeX's warning that a figure or table is taller than the text area, so "
            "it overlaps the margin or the page foot."
        ),
        tests=(
            "tests.test_build_log.NewLogRuleTests.test_each_rule_reports_its_message",
            "tests.test_build_log.NewLogRuleTests.test_clean_log_reports_none_of_the_new_rules",
        ),
        fix="Make the reported float smaller, for example by scaling its content, and rebuild.",
        reviewable=True,
    ),
    Rule(
        code="BLD013",
        name="build.pdf_string_token",
        title="Removed tokens in PDF strings",
        description=(
            "Note hyperref's warning that a command or math in a heading or title cannot "
            "appear in a PDF string, such as a bookmark, and was removed there."
        ),
        tests=(
            "tests.test_build_log.NewLogRuleTests.test_each_rule_reports_its_message",
            "tests.test_build_log.NewLogRuleTests.test_clean_log_reports_none_of_the_new_rules",
        ),
        fix=(
            "Give the heading a plain-text bookmark with \\texorpdfstring{TeX version}{text "
            "version}, or ignore the note when the bookmark reads well."
        ),
        reviewable=True,
    ),
    Rule(
        code="BLD014",
        name="build.obsolete_package",
        title="Obsolete or deprecated packages and commands",
        description=(
            "Note class and package warnings that call a package, option or command obsolete "
            "or deprecated, such as fixltx2e or the LaTeX 2.09 font commands."
        ),
        tests=(
            "tests.test_build_log.NewLogRuleTests.test_each_rule_reports_its_message",
            "tests.test_build_log.NewLogRuleTests.test_clean_log_reports_none_of_the_new_rules",
        ),
        fix=(
            "Follow the replacement the warning names, or remove the obsolete package; check "
            "the PDF for changes afterwards."
        ),
        reviewable=True,
    ),
    Rule(
        code="BLD015",
        name="build.bibliography_warning",
        title="Bibliography tool warnings",
        description=(
            "Note the warnings BibTeX or Biber wrote to their log (.blg), such as an empty "
            "required field or a duplicate entry, as information: one finding per distinct "
            "warning, which never changes the outcome."
        ),
        tests=(
            "tests.test_build_log.BibliographyLogTests.test_bibtex_and_biber_warnings",
            "tests.test_build_log.BibliographyLogTests.test_clean_logs_and_many_warnings",
        ),
        fix=(
            "Correct the named entry in the .bib file, or ignore the warning after reviewing "
            "the reference in the PDF."
        ),
        reviewable=True,
    ),
)
