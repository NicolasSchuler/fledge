"""Data-only catalogue for cheap submission-readiness advisories and their opt-in policies."""

RULE_DEFINITIONS = (
    {
        "code": "TEX009",
        "name": "source.bibliography_bbl",
        "title": "Generated bibliography shipped with the sources",
        "description": (
            "Default note: the selected document reads a .bib file through BibTeX "
            "(\\bibliography) or biblatex (\\addbibresource), but no generated .bbl with the "
            "main file's name sits beside the main file. Services that compile without running "
            "BibTeX or Biber, arXiv among them, then produce empty citations; most other "
            "services run the bibliography backend themselves, so by default the missing file "
            "is reported as information and does not change the outcome. Opt-in "
            "submission_checks.require_bbl = true reports it as a warning."
        ),
        "tests": (
            "tests.test_submission_readiness.BibliographyBblTests.test_bib_without_bbl_is_a_note_and_bbl_passes",
            "tests.test_submission_readiness.BibliographyBblTests.test_require_bbl_makes_a_missing_bbl_a_warning",
            "tests.test_submission_readiness.BibliographyBblTests.test_no_bib_resource_or_no_main_reports_nothing",
        ),
        "fix": (
            "Only needed when the receiving service does not run BibTeX or Biber: build "
            "locally and keep the generated <main>.bbl next to the main file; prepare retains "
            "an existing <main>.bbl in the package automatically. Biber-generated .bbl files "
            "must match the biblatex version of the compiling service. For BibTeX, "
            "source_transforms.inline_bibliography (TEX203) can inline the .bbl instead."
        ),
    },
    {
        "code": "BIB109",
        "name": "bibliography.bbl_coverage",
        "title": "Shipped .bbl covers cited keys",
        "description": (
            "Opt-in (submission_checks.check_bbl_coverage): compare literal citation keys in the "
            "selected source graph with \\bibitem (BibTeX) or \\entry/\\keyalias (biblatex) keys "
            "in the shipped <main>.bbl. \\nocite{*} additions and entry content are not "
            "checked; dynamic citations are inconclusive. Without a shipped <main>.bbl the "
            "coverage is an inconclusive warning that defers to TEX009."
        ),
        "tests": (
            "tests.test_submission_readiness.BibliographyBblTests.test_bbl_coverage_reports_missing_keys_and_parses_both_formats",
            "tests.test_submission_readiness.BibliographyBblTests.test_bbl_coverage_is_inconclusive_for_dynamic_citations",
        ),
        "fix": (
            "Rebuild the bibliography after the last citation change and ship the regenerated "
            "<main>.bbl, or correct the reported citation keys."
        ),
    },
    {
        "code": "TEX010",
        "name": "source.hyperref_load_order",
        "title": "Packages that must follow hyperref",
        "description": (
            "Default advisory: in the expanded literal preamble, cleveref, hypcap, bookmark, "
            "glossaries or glossaries-extra is declared before hyperref. Conditional, grouped "
            "and macro-generated declarations and packages loaded inside local style files are "
            "not interpreted, so absence of a finding is not proof of correct order."
        ),
        "tests": (
            "tests.test_submission_readiness.HyperrefOrderTests.test_packages_before_hyperref_are_reported",
            "tests.test_submission_readiness.HyperrefOrderTests.test_correct_order_conditionals_and_missing_hyperref_are_not_reported",
        ),
        "fix": (
            "Move the reported \\usepackage after \\usepackage{hyperref}. These packages document "
            "that requirement: cleveref and hypcap patch hyperref's reference and anchor "
            "commands; bookmark loads hyperref itself, so later hyperref options clash; "
            "glossaries and glossaries-extra only create hyperlinks when hyperref is "
            "loaded first."
        ),
    },
    {
        "code": "TEX011",
        "name": "source.eps_figures",
        "title": "EPS/PS figures in a PDF-producing build",
        "description": (
            "Default note: the selected document includes .eps or .ps graphics. pdfLaTeX and "
            "LuaLaTeX convert them only through epstopdf, which needs shell escape (disabled in "
            "Fledge's isolated build). arXiv converts EPS itself; other services may not."
        ),
        "tests": (
            "tests.test_submission_readiness.FigureTests.test_eps_figures_are_reported_as_information",
        ),
        "fix": (
            "Convert the figures to PDF (for example with epstopdf) and include the PDF files, "
            "or confirm that the receiving service compiles EPS through latex and dvips."
        ),
    },
    {
        "code": "PRV007",
        "name": "submission.private_comment_markers",
        "title": "Comments carrying private-note markers",
        "description": (
            "Default advisory over TeX comments in shipped .tex/.ltx/.latex files: reviewer or "
            "rebuttal correspondence, distribution restrictions such as 'do not submit' or "
            "'confidential', 'note to self' and 'NS:'-style author-initial notes. Only "
            "locations and marker categories are reported. This narrow scan is separate from "
            "the opt-in PRV005 comment policy and does not establish anonymity."
        ),
        "tests": (
            "tests.test_submission_readiness.PrivateMarkerTests.test_private_markers_report_locations_and_categories_only",
            "tests.test_submission_readiness.PrivateMarkerTests.test_ordinary_template_and_technical_comments_are_not_reported",
        ),
        "fix": (
            "Delete or rewrite the reported comments before submission, or set "
            "source_transforms.comment_policy = 'all' so the prepared copy omits comments."
        ),
    },
)
