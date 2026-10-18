"""Data-only entries for explicit review and diagnostic reporting safeguards."""

from .rule_definition import Rule

RULES = (
    Rule(
        code="RPT101",
        name="report.review_unmatched",
        title="Unmatched review decision",
        description="Report an explicit suppression or accepted exception whose exact "
        "code and location scope match no finding. Decisions are not inferred or broadened.",
        tests=("tests.test_reporting.ReviewTests.test_unmatched_review_is_visible",),
        fix="Review the selected code and location against the current findings.",
    ),
    Rule(
        code="RPT102",
        name="report.review_ineligible",
        title="Ineligible review decision",
        description="Retain failures when a requested review decision targets incomplete "
        "evidence, an unwaivable safety/build/archive check or an error suppression.",
        tests=(
            "tests.test_reporting.ReviewTests.test_incomplete_and_safety_findings_cannot_be_waived",
            "tests.test_reporting.ReviewTests.test_real_privacy_aggregates_with_missing_channels_cannot_be_waived",
            "tests.test_reporting.ReviewTests.test_unreadable_source_aggregate_retains_coverage_blocker",
            "tests.test_reporting.ReviewTests.test_comparison_exceptions_cannot_accept_archive_changes",
        ),
        fix="Resolve the original finding; review decisions cannot establish missing evidence.",
    ),
    Rule(
        code="RPT103",
        name="report.diagnostic_bundle",
        title="Unverified diagnostic export",
        description="Label sanitized diagnostic reports and diffs as unverified material, "
        "never a submission package.",
        tests=(
            "tests.test_reporting.DiagnosticBundleTests.test_bundle_is_unverified_and_cross_redacted",
            "tests.test_reporting.DiagnosticBundleTests.test_export_refuses_to_overwrite_an_existing_file",
        ),
        fix="Use this bundle for diagnosis; run preparation separately for a verified output.",
    ),
)
