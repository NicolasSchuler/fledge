"""Data-only catalogue for checks over build and recorder evidence."""

RULE_DEFINITIONS = (
    {
        "code": "BLD101",
        "name": "build.overfull_tolerance",
        "title": "Configured overfull-box tolerance",
        "description": (
            "Compare numerical final-log box overflow with an explicit tolerance in TeX "
            "points; incomplete build/log evidence cannot pass."
        ),
        "tests": ("tests.test_build_checks.BuildCheckTests.test_overfull_tolerance",),
        "fix": "Reflow the reported content or correct the explicit tolerance, then rebuild.",
    },
    {
        "code": "BLD102",
        "name": "build.loaded_package_policy",
        "title": "Recorder-observed package policy",
        "description": (
            "Apply explicit allow/deny/required sets to transitive .sty and .cls inputs "
            "observed by the build recorder."
        ),
        "tests": (
            "tests.test_build_checks.BuildCheckTests.test_loaded_package_policy",
            "tests.test_build_checks.BuildCheckTests."
            "test_recorder_collects_loaded_packages_and_rejects_empty_trace",
        ),
        "fix": (
            "Review all loaded class/package filenames, replace forbidden dependencies, or "
            "update the explicit policy."
        ),
    },
    {
        "code": "BLD103",
        "name": "build.package_date",
        "title": "Minimum declared package date",
        "description": (
            "Compare a loaded package's bounded literal ProvidesClass/ProvidesPackage header "
            "date to an author-specified minimum; this is not authenticity verification."
        ),
        "tests": (
            "tests.test_build_checks.BuildCheckTests.test_package_dates",
            "tests.test_build_checks.BuildCheckTests."
            "test_inactive_and_ambiguous_package_headers_never_supply_a_required_date",
        ),
        "fix": (
            "Supply the intended compatible class/package release, or update the explicit "
            "minimum date after review."
        ),
    },
    {
        "code": "BLD104",
        "name": "build.local_package_shadow",
        "title": "Existing local package shadow",
        "description": (
            "Look up a system counterpart from an empty sandboxed workspace for each loaded "
            "local class/package; report intentional-override candidates as advisories."
        ),
        "tests": ("tests.test_build_checks.BuildCheckTests.test_shadow_lookup",),
        "fix": (
            "Compare the local class/package with the intended template or toolchain and "
            "retain overrides only when deliberate."
        ),
    },
    {
        "code": "BLD105",
        "name": "build.tex_distribution",
        "title": "Local TeX distribution release",
        "description": (
            "Record the TeX distribution named by the baseline build's single engine version "
            "probe (for example TeX Live 2026) as information. Opt-in "
            "build_checks.expected_texlive_year turns a different or unidentified release into "
            "a warning; missing version evidence is inconclusive, never a pass."
        ),
        "tests": (
            "tests.test_build_checks.TexDistributionTests.test_records_texlive_year_and_compares_explicit_expectation",
            "tests.test_build_checks.TexDistributionTests.test_missing_or_unrecognized_version_is_inconclusive",
        ),
        "fix": (
            "Build with the TeX Live release the receiving service documents (arXiv lists its "
            "current one), or correct expected_texlive_year; rerun after changing toolchains."
        ),
    },
)
