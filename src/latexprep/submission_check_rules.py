"""Data-only catalogue entries for configurable local submission checks."""

from .rule_definition import Rule

RULES = (
    Rule(
        code="PRV001",
        name="submission.identity_terms",
        title="Configured source identity terms",
        description=(
            "Scan configured literal identity terms in bundle text, comments, URLs "
            "and filenames. Case-insensitive whole-word matches (set identity_term_matching to "
            "substring to match inside words) do not establish anonymity."
        ),
        tests=(
            "tests.test_submission_checks.SubmissionTests.test_source_identity_terms",
            "tests.test_submission_checks.SubmissionTests."
            "test_short_identity_terms_match_whole_words_unless_substring_is_requested",
        ),
        fix="Review the reported locations against the intended anonymity policy.",
        reviewable=True,
        not_checked_area="anonymity",
    ),
    Rule(
        code="PRV002",
        name="submission.pdf_identity_terms",
        title="Configured PDF identity terms",
        description=(
            "Scan extracted PDF text and supplied metadata for configured literal "
            "identity terms using the same whole-word or substring matching as source files; "
            "unavailable extraction is inconclusive."
        ),
        tests=("tests.test_submission_checks.SubmissionTests.test_pdf_identity_terms",),
        fix="Review the PDF text and metadata before submission, then regenerate the PDF.",
        reviewable=True,
        not_checked_area="anonymity",
    ),
    Rule(
        code="PRV105",
        name="privacy.included_pdf_information",
        title="Included-PDF information in the built PDF",
        description=(
            "Default advisory over the PDF a job ships: the PDF that check builds, and in "
            "prepare the PDF it publishes. pdfTeX and LuaTeX copy each included PDF's "
            "information dictionary (/PTEX.InfoDict) and file name (/PTEX.FileName), with the "
            "included page's XMP and application data, into the form XObject that draws it, "
            "where pdfinfo and the PDF identity scan (PRV002) do not look. Report configured "
            "identity terms (submission_checks.identity_terms) and home-folder paths found "
            "there, by field and never by value. The baseline that prepare builds from the "
            "unchanged sources is not published, so what it carries is reported as "
            "information only, and a published PDF without such information is a pass. A PDF "
            "that cannot be read within the bounds only informs."
        ),
        tests=(
            "tests.test_submission_checks.IncludedPdfInformationTests.test_terms_and_home_folders_copied_from_included_pdfs",
            "tests.test_submission_checks.IncludedPdfInformationTests.test_clean_suppressed_and_unreadable_pdfs",
            "tests.test_submission_checks.IncludedPdfInformationTests.test_prepare_notes_its_baseline_and_judges_the_pdf_it_publishes",
            "tests.test_submission_checks.IncludedPdfRoleWiringTests.test_prepare_notes_its_baseline_and_judges_the_published_pdf",
        ),
        fix=(
            "Add \\pdfsuppressptexinfo=-1 (pdfLaTeX) or \\pdfvariable suppressoptionalinfo 15 "
            "(LuaLaTeX) at the start of the main file, or set "
            "source_transforms.suppress_included_pdf_information = true for prepare; "
            "re-export figures whose page-level XMP or application data holds the values."
        ),
        reviewable=True,
    ),
    Rule(
        code="PRV003",
        name="submission.identity_hints",
        title="Possible identifying language",
        description=(
            "Optionally flag acknowledgement declarations and literal "
            "self-identifying phrases in active source as advisory hints."
        ),
        tests=("tests.test_submission_checks.SubmissionTests.test_identity_hints",),
        fix="Review these heuristic hints in context; do not rewrite self-citations blindly.",
        reviewable=True,
    ),
    Rule(
        code="PRV004",
        name="submission.secrets",
        title="Possible credentials",
        description=(
            "Optionally detect bounded credential-shaped tokens, private-key "
            "headers and secret assignments in local text, filenames and PDF metadata. "
            "Candidate values are redacted; absence is not proof of safety."
        ),
        tests=("tests.test_submission_checks.SubmissionTests.test_secrets_are_redacted",),
        fix=(
            "Inspect the indicated location privately; remove unintended credentials "
            "and rotate any credential that has been exposed."
        ),
        reviewable=True,
    ),
    Rule(
        code="PRV005",
        name="submission.private_comments",
        title="Private comments and unfinished notes",
        description=(
            "Optionally scan TeX comments for unfinished markers and private-note "
            "phrases without changing licensing, directives or meaningful comments."
        ),
        tests=("tests.test_submission_checks.SubmissionTests.test_private_comments",),
        fix="Review flagged comments and remove only material that should not be submitted.",
        reviewable=True,
    ),
    Rule(
        code="PRV006",
        name="submission.shell_escape",
        title="Literal shell-execution preflight",
        description=(
            "Optionally flag supported literal write18 and shell-execution "
            "constructs in active source. This advisory scan does not replace runtime isolation."
        ),
        tests=("tests.test_submission_checks.SubmissionTests.test_shell_escape",),
        fix=(
            "Review whether the dependency can be prepared without shell escape; keep "
            "the runtime sandbox and shell-escape prohibition enabled."
        ),
    ),
    Rule(
        code="PKG101",
        name="submission.filename_length",
        title="Configured filename length",
        description=(
            "Apply an explicit maximum Unicode-character length to each "
            "file basename in the supplied bundle."
        ),
        tests=("tests.test_submission_checks.SubmissionTests.test_filename_length",),
        fix="Rename affected files in an explicit copy and update their references.",
        reviewable=True,
    ),
    Rule(
        code="PKG102",
        name="submission.filename_characters",
        title="Configured filename characters",
        description=(
            "Apply a user-supplied literal allowed-character set to file "
            "basenames; this is separate from archive path-safety checks."
        ),
        tests=("tests.test_submission_checks.SubmissionTests.test_filename_characters",),
        fix="Use filenames accepted by the explicit character policy and update references.",
        reviewable=True,
    ),
    Rule(
        code="PKG103",
        name="submission.file_extensions",
        title="Configured bundle file types",
        description=(
            "Compare case-insensitive final filename extensions with an explicit "
            "allowlist; an extension does not establish the actual file format."
        ),
        tests=("tests.test_submission_checks.SubmissionTests.test_file_extensions",),
        fix="Review disallowed file types and retain all required build dependencies.",
        reviewable=True,
    ),
    Rule(
        code="PKG104",
        name="submission.deliverables",
        title="Explicit required deliverables",
        description=(
            "Require configured relative paths and check their declared kind "
            "using regular-file, PDF/ZIP signature or UTF-8-text evidence. Presence does not "
            "establish separate delivery or successful compilation."
        ),
        tests=("tests.test_submission_checks.SubmissionTests.test_required_deliverables",),
        fix="Supply each required file at its configured path and verify its intended format.",
        reviewable=True,
    ),
    Rule(
        code="PKG105",
        name="submission.archive_size",
        title="Compressed archive size",
        description=(
            "Compare the actual final archive's byte length with a separate "
            "explicit compressed-size limit without extracting it."
        ),
        tests=("tests.test_submission_checks.SubmissionTests.test_archive_size",),
        fix="Reduce the final archive within the configured limit while preserving dependencies.",
        reviewable=True,
        not_checked_area="archive_size",
    ),
    Rule(
        code="PKG106",
        name="submission.unused_assets",
        title="Assets outside the selected dependency graph",
        description=(
            "Optionally list supported asset files not observed in the selected "
            "literal dependency graph. Dynamic, missing or ambiguous dependencies make this "
            "inconclusive; candidates are never deletion recommendations."
        ),
        tests=("tests.test_submission_checks.SubmissionTests.test_unused_assets",),
        fix=(
            "Review candidates against every intended document and build recorder before "
            "excluding any asset from an explicit copy."
        ),
        reviewable=True,
    ),
    Rule(
        code="PKG107",
        name="submission.template_reference",
        title="Author-supplied template references",
        description=(
            "Compare explicit project/reference file pairs byte-for-byte and "
            "report whether supported ProvidesClass/ProvidesPackage version declarations "
            "agree. No registry, official-status or age inference is made."
        ),
        tests=("tests.test_submission_checks.SubmissionTests.test_template_references",),
        fix=(
            "Review differences against the supplied reference and confirm intended template "
            "modifications and version compatibility."
        ),
        reviewable=True,
    ),
)
