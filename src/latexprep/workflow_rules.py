"""Behavior-linked diagnostics for document packages and repeat builds."""

WORKFLOW_RULES = (
    {
        "code": "PKG301",
        "name": "package.independent_documents",
        "title": "Independent document packages",
        "description": "Verify every explicitly selected document independently and release all "
        "packages only when every required check and archive rebuild succeeds.",
        "fix": "Correct the failed document or its explicit file selection and repeat "
        "preparation; no sibling package is released early.",
        "tests": (
            "tests.test_multi_document.MultiDocumentTests.test_every_document_rebuilds_own_archive_and_releases_together",
            "tests.test_multi_document.MultiDocumentTests.test_one_failed_document_prevents_all_publication",
        ),
    },
    {
        "code": "CMP101",
        "name": "compare.baseline_stability",
        "title": "Repeated baseline stability",
        "description": "Repeat the clean baseline build the configured number of times and "
        "require identical page count, extracted text and exact 144-DPI renders "
        "before transformation.",
        "fix": "Remove nondeterministic content or stabilize the build environment "
        "before preparing transformed sources.",
        "tests": (
            "tests.test_multi_document.BaselineStabilityTests.test_repeat_build_difference_blocks_publication",
            "tests.test_multi_document.BaselineStabilityTests.test_identical_repeats_pass_and_do_not_relax_archive_comparison",
        ),
    },
)
