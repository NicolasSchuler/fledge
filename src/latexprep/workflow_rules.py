"""Data-only catalogue for document packages and repeat builds."""

from .rule_definition import Rule

RULES = (
    Rule(
        code="PKG301",
        name="package.independent_documents",
        title="Independent document packages",
        description=(
            "Verify every explicitly selected document independently and release all "
            "packages only when every required check and archive rebuild succeeds."
        ),
        tests=(
            "tests.test_multi_document.MultiDocumentTests.test_every_document_rebuilds_own_archive_and_releases_together",
            "tests.test_multi_document.MultiDocumentTests.test_one_failed_document_prevents_all_publication",
        ),
        fix=(
            "Correct the failed document or its explicit file selection and repeat "
            "preparation; no sibling package is released early."
        ),
        mandatory=True,
    ),
    Rule(
        code="PKG302",
        name="package.dependencies",
        title="Package input selection",
        description=(
            "Select the files a package ships from the build's recorded inputs and the "
            "explicitly retained files. Report missing build dependency evidence and a "
            "required input that is missing, linked or not a plain file; warn when the "
            "literal source graph is incomplete or ambiguous, because the recorded inputs "
            "then decide. A passed note lists the selection."
        ),
        tests=(
            "tests.test_dependency_packaging.DependencyPackagingTests.test_supplied_generated_bibliography_ships_when_the_build_consumed_it",
            "tests.test_dependency_packaging.DependencyPackagingTests.test_unsupported_selected_dependencies_are_advisory_against_a_complete_trace",
        ),
        fix=(
            "Resolve the build's submission dependency diagnostic, or supply the "
            "project-relative file, then repeat preparation."
        ),
        mandatory=True,
    ),
    Rule(
        code="PKG303",
        name="package.archive_limit",
        title="Archive within the extraction limit",
        description=(
            "Fail when the prepared archive is larger than max_bytes. It then cannot be "
            "re-extracted for the rebuild that verifies it, so it is not released."
        ),
        tests=(
            "tests.test_limit_findings.ArchiveLimitTests.test_an_archive_over_max_bytes_blocks_verification",
        ),
        fix="Reduce the shipped files or raise max_bytes explicitly.",
        mandatory=True,
    ),
    Rule(
        code="CMP101",
        name="compare.baseline_stability",
        title="Repeated baseline stability",
        description=(
            "Repeat the clean baseline build the configured number of times and "
            "require identical page count, extracted text and exact 144-DPI renders "
            "before transformation."
        ),
        tests=(
            "tests.test_multi_document.BaselineStabilityTests.test_repeat_build_difference_blocks_publication",
            "tests.test_multi_document.BaselineStabilityTests.test_identical_repeats_pass_and_do_not_relax_archive_comparison",
        ),
        fix=(
            "Remove nondeterministic content or stabilize the build environment "
            "before preparing transformed sources."
        ),
        mandatory=True,
    ),
)
