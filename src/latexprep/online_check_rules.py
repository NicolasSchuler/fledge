"""Data-only catalogue entries for explicitly selected remote reference checks."""

from .rule_definition import Rule

RULES = (
    Rule(
        code="NET001",
        name="online.doi_resolution",
        title="DOI resolution",
        description=(
            "Resolve explicitly selected literal DOIs through doi.org; distinguish missing, "
            "restricted and unavailable responses from successful resolution."
        ),
        tests=(
            "tests.test_online_checks.OnlineCheckTests.test_doi_resolution_valid_invalid_uncertain",
        ),
        fix=(
            "Check the DOI and retry unavailable services; a resolving DOI alone does not "
            "verify the cited work."
        ),
        reviewable=True,
    ),
    Rule(
        code="NET002",
        name="online.metadata",
        title="Remote bibliography metadata agreement",
        description=(
            "Compare supported literal title, author, year and venue fields with Crossref DOI "
            "metadata; report differences as reviewable evidence, not automatic corrections."
        ),
        tests=("tests.test_online_checks.OnlineCheckTests.test_metadata_valid_invalid_uncertain",),
        fix=(
            "Review local and provider metadata against the cited version; do not replace "
            "fields automatically."
        ),
        reviewable=True,
    ),
    Rule(
        code="NET003",
        name="online.missing_doi",
        title="Missing DOI candidates",
        description=(
            "Search Crossref using an explicitly shared literal bibliography title and rank "
            "reviewable DOI candidates; no match and ambiguity remain distinct."
        ),
        tests=(
            "tests.test_online_checks.OnlineCheckTests.test_missing_doi_valid_invalid_uncertain",
        ),
        fix=(
            "Inspect each candidate's title, authors and year before adding a DOI; a missing "
            "candidate is not proof that no DOI exists."
        ),
        reviewable=True,
    ),
    Rule(
        code="NET004",
        name="online.published_version",
        title="Published versions of preprints",
        description=(
            "Surface Crossref published-version relations or title-search candidates for "
            "explicitly identified preprints; never infer that similar titles establish the "
            "same work."
        ),
        tests=(
            "tests.test_online_checks.OnlineCheckTests.test_published_version_valid_invalid_uncertain",
        ),
        fix=(
            "Review the relation and cited version before changing the reference; retain the "
            "preprint when appropriate."
        ),
        reviewable=True,
    ),
    Rule(
        code="NET005",
        name="online.notices",
        title="Correction and retraction notices",
        description=(
            "Surface Crossref update metadata and related notice records, keeping corrections "
            "and retractions separate and provider coverage explicit."
        ),
        tests=("tests.test_online_checks.OnlineCheckTests.test_notices_valid_invalid_uncertain",),
        fix=(
            "Read the attributed notice and assess its relevance; absence of Crossref update "
            "metadata is not proof that no notice exists."
        ),
        reviewable=True,
    ),
    Rule(
        code="NET006",
        name="online.reference_link",
        title="Reference URL health",
        description=(
            "Check explicitly selected literal bibliography URLs with bounded public-network "
            "requests; redirects, authentication, throttling and unavailable resources are "
            "distinct outcomes."
        ),
        tests=(
            "tests.test_online_checks.OnlineCheckTests.test_reference_link_valid_invalid_uncertain",
        ),
        fix=(
            "Review broken links and retry restricted or temporarily unavailable resources "
            "without exposing credentials."
        ),
        reviewable=True,
    ),
    Rule(
        code="NET007",
        name="online.replication_link",
        title="Replication and source-link health",
        description=(
            "Check explicitly supplied replication URLs and literal URL commands in the "
            "selected source graph; HTTP availability does not establish reproducibility or "
            "artifact completeness."
        ),
        tests=(
            "tests.test_online_checks.OnlineCheckTests.test_replication_link_valid_invalid_uncertain",
        ),
        fix=(
            "Review artifact destinations and access requirements, then verify their contents "
            "and reproducibility separately."
        ),
        reviewable=True,
    ),
    Rule(
        code="NET008",
        name="online.sharing",
        title="Data shared with online services",
        description=(
            "State what leaves the machine when online checks run: DOI identifiers, URL "
            "destinations and bibliography titles go to DOI and Crossref services and to the "
            "selected public hosts. No bibliography or manuscript file is uploaded. Online "
            "checks are off unless requested."
        ),
        tests=(
            "tests.test_online_checks.OnlineCheckTests.test_doi_resolution_valid_invalid_uncertain",
            "tests.test_terminal_presentation.OnlineDisclosureTests.test_the_privacy_disclosure_is_listed_without_show_passed",
        ),
        fix=(
            "Check that sharing these fields is acceptable before using --online; "
            "otherwise leave online checks off."
        ),
        mandatory=True,
    ),
)
