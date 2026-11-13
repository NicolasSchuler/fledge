"""Data-only catalogue for cheap submission-readiness checks and their opt-in policies."""

from .rule_definition import Rule

RULES = (
    Rule(
        code="TEX009",
        name="source.bibliography_bbl",
        title="Generated bibliography shipped with the sources",
        description=(
            "Default check: the selected document reads a .bib file through BibTeX "
            "(\\bibliography) or biblatex (\\addbibresource), but no generated .bbl with the "
            "main file's name sits beside the main file. A service that compiles without "
            "running BibTeX or Biber then produces empty citations. Most services run the "
            "bibliography backend themselves when no .bbl is uploaded (arXiv does, and gives "
            "an uploaded .bbl precedence), so by default the missing file is recorded as a "
            "not-applicable scope note, listed only with --show-passed and in JSON or HTML "
            "reports, and does not change the outcome. With "
            "submission_checks.venue_runs_bibliography_tools = false, or require_bbl = true, "
            "it is a warning, and prepare ships the .bbl its verified build generated."
        ),
        tests=(
            "tests.test_submission_readiness.BibliographyBblTests.test_bib_without_bbl_is_a_note_and_bbl_passes",
            "tests.test_submission_readiness.BibliographyBblTests.test_require_bbl_makes_a_missing_bbl_a_warning",
            "tests.test_submission_readiness.BibliographyBblTests.test_no_bib_resource_or_no_main_reports_nothing",
        ),
        fix=(
            "Only needed when the receiving service does not run BibTeX or Biber: set "
            "submission_checks.venue_runs_bibliography_tools = false, and prepare ships the "
            "<main>.bbl its verified build generated; for check, build locally and keep the "
            "generated <main>.bbl next to the main file. Biber-generated .bbl files must match "
            "the biblatex version of the compiling service. For BibTeX, "
            "source_transforms.inline_bibliography (TEX203) can inline the .bbl instead."
        ),
    ),
    Rule(
        code="BIB109",
        name="bibliography.bbl_coverage",
        title="Shipped .bbl covers cited keys",
        description=(
            "Opt-in (submission_checks.check_bbl_coverage): compare literal citation keys in the "
            "selected source graph with \\bibitem (BibTeX) or \\entry/\\keyalias (biblatex) keys "
            "in the shipped <main>.bbl. \\nocite{*} additions and entry content are not "
            "checked; dynamic citations are inconclusive. Without a shipped <main>.bbl the "
            "coverage is an inconclusive warning that defers to TEX009."
        ),
        tests=(
            "tests.test_submission_readiness.BibliographyBblTests.test_bbl_coverage_reports_missing_keys_and_parses_both_formats",
            "tests.test_submission_readiness.BibliographyBblTests.test_bbl_coverage_is_inconclusive_for_dynamic_citations",
        ),
        fix=(
            "Rebuild the bibliography after the last citation change and ship the regenerated "
            "<main>.bbl, or correct the reported citation keys."
        ),
    ),
    Rule(
        code="BIB110",
        name="bibliography.bbl_format_version",
        title="Shipped biblatex .bbl matches the target format",
        description=(
            "Opt-in (submission_checks.biblatex_bbl_format_version): compare the format "
            "version on the second line of a shipped biblatex <main>.bbl, such as "
            "'% $ biblatex bbl format version 3.3 $', with the version used by the service that "
            "reads the shipped .bbl instead of running Biber (arXiv does when one is uploaded). "
            "biblatex there reports a wrong format version for any other value. BibTeX .bbl "
            "files and "
            "documents without a literal .bib resource are not applicable; without a shipped "
            "<main>.bbl the result is an inconclusive warning "
            "that defers to TEX009. Only the header is compared, not the entries."
        ),
        tests=(
            "tests.test_submission_readiness.BibliographyBblTests.test_bbl_format_version_matches_the_configured_target",
            "tests.test_submission_readiness.BibliographyBblTests.test_bbl_format_version_setting_is_validated_and_reaches_the_check",
        ),
        fix=(
            "Build with the biblatex release of the target service's TeX Live (its "
            "instructions name the release; TeX Live 2026 writes format 3.3), then ship the "
            "regenerated <main>.bbl. Otherwise, choose a service or backend that runs Biber."
        ),
    ),
    Rule(
        code="TEX010",
        name="source.hyperref_load_order",
        title="Packages that must follow hyperref",
        description=(
            "Default advisory: in the expanded literal preamble, cleveref, hypcap, bookmark, "
            "glossaries or glossaries-extra is declared before hyperref. Conditional, grouped "
            "and macro-generated declarations and packages loaded inside local style files are "
            "not interpreted, so absence of a finding is not proof of correct order."
        ),
        tests=(
            "tests.test_submission_readiness.HyperrefOrderTests.test_packages_before_hyperref_are_reported",
            "tests.test_submission_readiness.HyperrefOrderTests.test_correct_order_conditionals_and_missing_hyperref_are_not_reported",
        ),
        fix=(
            "Move the reported \\usepackage after \\usepackage{hyperref}. These packages document "
            "that requirement: cleveref and hypcap patch hyperref's reference and anchor "
            "commands; bookmark loads hyperref itself, so later hyperref options clash; "
            "glossaries and glossaries-extra only create hyperlinks when hyperref is "
            "loaded first."
        ),
    ),
    Rule(
        code="TEX011",
        name="source.eps_figures",
        title="EPS/PS figures in a PDF-producing build",
        description=(
            "Default check: the selected document includes .eps or .ps graphics. No engine "
            "converts them in Fledge's isolated build: pdfLaTeX and LuaLaTeX convert EPS only "
            "through epstopdf, which needs shell escape, and XeLaTeX only through Ghostscript, "
            "and both are disabled. TeX Live 2023 and earlier then print the file name in "
            "place of the figure without an error, so the build may pass with the figure "
            "missing; arXiv converts no figures either. A figure without its converted PDF "
            "beside it blocks. A shipped <name>-eps-converted-to.pdf, which TeX Live 2024 and "
            "later use, only warns, because earlier releases and XeLaTeX ignore it."
        ),
        tests=(
            "tests.test_submission_readiness.FigureTests.test_eps_figures_block_unless_their_converted_pdf_is_shipped",
        ),
        fix=(
            "Convert each figure to PDF (for example with epstopdf), include the PDF "
            "(\\includegraphics{figs/plot} finds figs/plot.pdf) and ship it with the sources."
        ),
    ),
    Rule(
        code="TEX012",
        name="source.generated_lists",
        title="Generated index and glossary files shipped with the sources",
        description=(
            "Default check: the selected document prints an index (\\printindex), a glossary "
            "or list of acronyms (\\printglossary, \\printglossaries, \\printacronyms) or a "
            "nomenclature (\\printnomenclature), but the generated file TeX reads for it "
            "(<main>.ind, .gls, .acr or .nls, or <name>.ind for a named index) is not beside "
            "the main file. A service that runs neither makeindex nor makeglossaries, arXiv "
            "among them, then prints the list empty. prepare ships the files its verified "
            "build generated, so the prepared package has them. A missing file is a "
            "not-applicable scope note unless submission_checks.venue_runs_index_tools = false "
            "(set by the arxiv preset) makes it a warning. A preparation build that read no "
            "such file although the document wrote entries for it reports the file and the "
            "tool that would write it."
        ),
        tests=(
            "tests.test_submission_readiness.GeneratedListTests.test_missing_index_and_glossary_files_are_notes_by_default",
            "tests.test_submission_readiness.GeneratedListTests.test_a_venue_without_index_tools_makes_a_missing_file_a_warning",
            "tests.test_submission_readiness.GeneratedListTests.test_shipped_lists_pass_and_documents_without_lists_report_nothing",
            "tests.test_submission_readiness.NeverCreatedListTests.test_a_list_the_build_never_got_is_reported_with_its_tool",
            "tests.test_submission_readiness.NeverCreatedListTests.test_the_baseline_build_reports_the_lists_it_never_got",
            "tests.test_submission_readiness.NeverCreatedListTests.test_a_glossary_written_for_xindy_says_that_xindy_is_not_available",
        ),
        fix=(
            "Run fledge prepare, which ships the index and glossary files its verified build "
            "generated; for check, build locally and keep the generated files next to the "
            "main file."
        ),
    ),
    Rule(
        code="TEX013",
        name="source.venue_engine",
        title="Engine offered by the receiving service",
        description=(
            "Default check that reports only when submission_checks.venue_engines lists the "
            "engines the receiving service compiles with: the document builds with another "
            "engine. The arxiv preset lists pdflatex and xelatex, because arXiv's processors "
            "are tex, pdftex, latex (DVI), pdflatex and xelatex, not LuaLaTeX."
        ),
        tests=(
            "tests.test_submission_readiness.VenueEngineTests.test_an_engine_the_venue_does_not_offer_warns",
        ),
        fix=(
            "Build with an engine the service offers (for example --engine pdflatex), or "
            "submit where the document's engine is offered."
        ),
    ),
    Rule(
        code="TEX014",
        name="source.engine_packages",
        title="Packages the build engine cannot run",
        description=(
            "Default check: the expanded literal preamble loads a package that stops with an "
            "error under the engine the document builds with (the engine setting, else the "
            "main file's % !TEX program comment, else pdfLaTeX). fontspec, unicode-math, "
            "polyglossia, fontsetup and xltxtra need XeLaTeX or LuaLaTeX; xeCJK and mathspec "
            "need XeLaTeX; luacode, luatexja and luaotfload need LuaLaTeX. Found without "
            "running TeX, so inspect reports it too. Conditional, grouped and "
            "macro-generated declarations and packages loaded inside local style files are "
            "not interpreted."
        ),
        tests=(
            "tests.test_submission_readiness.EnginePackageTests.test_a_package_the_engine_cannot_run_warns_with_its_line",
            "tests.test_submission_readiness.EnginePackageTests.test_a_chosen_engine_that_runs_the_packages_passes",
            "tests.test_submission_readiness.EnginePackageTests.test_packages_that_need_different_engines_say_so",
        ),
        fix=(
            "Choose an engine that runs the package: add % !TEX program = xelatex (or "
            "lualatex) as the first line of the main file, pass --engine, or set engine in "
            "fledge.toml."
        ),
    ),
    Rule(
        code="PRV007",
        name="submission.private_comment_markers",
        title="Comments carrying private-note markers",
        description=(
            "Default advisory over TeX comments in shipped .tex/.ltx/.latex files: reviewer or "
            "rebuttal correspondence, distribution restrictions such as 'do not submit' or "
            "'confidential', 'note to self' and 'NS:'-style notes whose capital letters are the "
            "initials of a literal \\author name (other acronyms such as 'MLP:' are ignored, "
            "and so are initials when no literal author name is available). Only "
            "locations and marker categories are reported. This narrow scan is separate from "
            "the opt-in PRV005 comment policy and does not establish anonymity."
        ),
        tests=(
            "tests.test_submission_readiness.PrivateMarkerTests.test_private_markers_report_locations_and_categories_only",
            "tests.test_submission_readiness.PrivateMarkerTests.test_ordinary_template_and_technical_comments_are_not_reported",
        ),
        fix=(
            "Delete or rewrite the reported comments before submission, or set "
            "source_transforms.comment_policy = 'all' so the prepared copy omits comments."
        ),
    ),
)
