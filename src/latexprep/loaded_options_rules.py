"""Data-only catalogue for actual post-document kernel option evidence."""

RULE_DEFINITIONS = (
    {
        "code": "BLD301",
        "name": "build.loaded_options",
        "title": "Actual loaded class and package option inventory",
        "description": (
            "Collect bounded post-document normalized/raw kernel option lists and global "
            "class options from a line-neutral disposable-build hook. Require fresh complete "
            "markers, a successful build, and exact recorder agreement for package origins. "
            "Effective defaults and later package setup commands are outside this inventory."
        ),
        "tests": (
            "tests.test_loaded_options.LoadedOptionsTests.test_actual_options_and_origins_match_recorder",
            "tests.test_loaded_options.LoadedOptionsTests.test_absent_tampered_and_unsupported_evidence_is_inconclusive",
            "tests.test_loaded_options.LoadedOptionsTests.test_disabled_preserves_original_build_and_diagnostics",
        ),
        "fix": (
            "Use a supported LaTeX kernel/class with complete instrumentation and recorder "
            "evidence, then rebuild; inspect defaults and post-load setup separately."
        ),
    },
)
