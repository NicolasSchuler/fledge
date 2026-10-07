"""Data-only catalogue for explicit build backend verification."""

RULE_DEFINITIONS = (
    {
        "code": "BLD201",
        "name": "build.bibliography_backend",
        "title": "Configured bibliography backend",
        "description": (
            "Compare the selected auto, BibTeX or Biber strategy with bounded fresh AUX/BCF "
            "control evidence. Explicit strategies disable the other backend; malformed "
            "control files remain inconclusive."
        ),
        "tests": (
            "tests.test_runtime_backends.BibliographyBackendTests."
            "test_requested_backend_matches_fresh_controls",
            "tests.test_runtime_backends.BibliographyBackendTests."
            "test_mismatching_and_malformed_controls_block",
        ),
        "fix": (
            "Select the bibliography backend required by the source and supplied generated "
            "files, or correct the source/backend configuration, then rebuild."
        ),
    },
    {
        "code": "BLD202",
        "name": "build.bibliography_tool",
        "title": "Required bibliography tool availability",
        "description": (
            "Verify and record the locally available backend version in isolation when fresh "
            "bibliography controls require BibTeX or Biber. Availability alone does not prove "
            "bibliography completion or toolchain compatibility."
        ),
        "tests": (
            "tests.test_runtime_backends.BibliographyBackendTests."
            "test_required_backend_version_is_reported_and_unavailable_is_inconclusive",
        ),
        "fix": (
            "Install the required compatible bibliography tool explicitly or select a "
            "compatible supported strategy, then repeat the complete build."
        ),
    },
)
