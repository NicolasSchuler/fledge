"""Data-only catalogue entries for configurable local submission checks."""

RULE_DEFINITIONS = (
    {
        "code": "PRV001",
        "name": "submission.identity_terms",
        "title": "Configured source identity terms",
        "description": "Scan configured literal identity terms in bundle text, comments, URLs "
        "and filenames. Case-insensitive matches do not establish anonymity.",
        "tests": ("tests.test_submission_checks.SubmissionTests.test_source_identity_terms",),
        "fix": "Review the reported locations against the intended anonymity policy.",
    },
    {
        "code": "PRV002",
        "name": "submission.pdf_identity_terms",
        "title": "Configured PDF identity terms",
        "description": "Scan extracted PDF text and supplied metadata for configured literal "
        "identity terms; unavailable extraction is inconclusive.",
        "tests": ("tests.test_submission_checks.SubmissionTests.test_pdf_identity_terms",),
        "fix": "Review the PDF text and metadata before submission, then regenerate the PDF.",
    },
    {
        "code": "PRV003",
        "name": "submission.identity_hints",
        "title": "Possible identifying language",
        "description": "Optionally flag acknowledgement declarations and literal "
        "self-identifying phrases in active source as advisory hints.",
        "tests": ("tests.test_submission_checks.SubmissionTests.test_identity_hints",),
        "fix": "Review these heuristic hints in context; do not rewrite self-citations blindly.",
    },
    {
        "code": "PRV004",
        "name": "submission.secrets",
        "title": "Possible credentials",
        "description": "Optionally detect bounded credential-shaped tokens, private-key "
        "headers and secret assignments in local text, filenames and PDF metadata. "
        "Candidate values are redacted; absence is not proof of safety.",
        "tests": ("tests.test_submission_checks.SubmissionTests.test_secrets_are_redacted",),
        "fix": "Inspect the indicated location privately; remove unintended credentials "
        "and rotate any credential that has been exposed.",
    },
    {
        "code": "PRV005",
        "name": "submission.private_comments",
        "title": "Private comments and unfinished notes",
        "description": "Optionally scan TeX comments for unfinished markers and private-note "
        "phrases without changing licensing, directives or meaningful comments.",
        "tests": ("tests.test_submission_checks.SubmissionTests.test_private_comments",),
        "fix": "Review flagged comments and remove only material that should not be submitted.",
    },
    {
        "code": "PRV006",
        "name": "submission.shell_escape",
        "title": "Literal shell-execution preflight",
        "description": "Optionally flag supported literal write18 and shell-execution "
        "constructs in active source. This advisory scan does not replace runtime isolation.",
        "tests": ("tests.test_submission_checks.SubmissionTests.test_shell_escape",),
        "fix": "Review whether the dependency can be prepared without shell escape; keep "
        "the runtime sandbox and shell-escape prohibition enabled.",
    },
    {
        "code": "PKG101",
        "name": "submission.filename_length",
        "title": "Configured filename length",
        "description": "Apply an explicit maximum Unicode-character length to each "
        "file basename in the supplied bundle.",
        "tests": ("tests.test_submission_checks.SubmissionTests.test_filename_length",),
        "fix": "Rename affected files in an explicit copy and update their references.",
    },
    {
        "code": "PKG102",
        "name": "submission.filename_characters",
        "title": "Configured filename characters",
        "description": "Apply a user-supplied literal allowed-character set to file "
        "basenames; this is separate from archive path-safety checks.",
        "tests": ("tests.test_submission_checks.SubmissionTests.test_filename_characters",),
        "fix": "Use filenames accepted by the explicit character policy and update references.",
    },
    {
        "code": "PKG103",
        "name": "submission.file_extensions",
        "title": "Configured bundle file types",
        "description": "Compare case-insensitive final filename extensions with an explicit "
        "allowlist; an extension does not establish the actual file format.",
        "tests": ("tests.test_submission_checks.SubmissionTests.test_file_extensions",),
        "fix": "Review disallowed file types and retain all required build dependencies.",
    },
    {
        "code": "PKG104",
        "name": "submission.deliverables",
        "title": "Explicit required deliverables",
        "description": "Require configured relative paths and check their declared kind "
        "using regular-file, PDF/ZIP signature or UTF-8-text evidence. Presence does not "
        "establish separate delivery or successful compilation.",
        "tests": ("tests.test_submission_checks.SubmissionTests.test_required_deliverables",),
        "fix": "Supply each required file at its configured path and verify its intended format.",
    },
    {
        "code": "PKG105",
        "name": "submission.archive_size",
        "title": "Compressed archive size",
        "description": "Compare the actual final archive's byte length with a separate "
        "explicit compressed-size limit without extracting it.",
        "tests": ("tests.test_submission_checks.SubmissionTests.test_archive_size",),
        "fix": "Reduce the final archive within the configured limit "
        "while preserving dependencies.",
    },
    {
        "code": "PKG106",
        "name": "submission.unused_assets",
        "title": "Assets outside the selected dependency graph",
        "description": "Optionally list supported asset files not observed in the selected "
        "literal dependency graph. Dynamic, missing or ambiguous dependencies make this "
        "inconclusive; candidates are never deletion recommendations.",
        "tests": ("tests.test_submission_checks.SubmissionTests.test_unused_assets",),
        "fix": "Review candidates against every intended document and build recorder before "
        "excluding any asset from an explicit copy.",
    },
    {
        "code": "PKG107",
        "name": "submission.template_reference",
        "title": "Author-supplied template references",
        "description": "Compare explicit project/reference file pairs byte-for-byte and "
        "report whether supported ProvidesClass/ProvidesPackage version declarations "
        "agree. No registry, official-status or age inference is made.",
        "tests": ("tests.test_submission_checks.SubmissionTests.test_template_references",),
        "fix": "Review differences against the supplied reference and confirm intended template "
        "modifications and version compatibility.",
    },
)
