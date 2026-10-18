"""Bounded, local submission-policy checks; never edit sources or execute TeX.

Identity checks are literal whole-word (or, on request, substring) scans, not an
anonymity guarantee. Privacy checks cover the whole supplied bundle; unused-asset
hints alone use one selected literal dependency graph. No publisher policy or
template registry is embedded.

This module owns the bundle-level policies: file names, deliverables, template
references, unused assets, the compressed archive and the PDF identity scan, and it
orchestrates the content scan. The options are in ``submission_options``, the single
pass over file contents in ``submission_content_scan``, and the shared finding
wording in ``submission_scan_findings``.
"""

from __future__ import annotations

import re
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from .analysed_source import AnalysedSource
from .cancellation import cancellation_point
from .file_scan import ByteBudget, inventory_files
from .finding_text import more_suffix, redact_findings
from .findings import Finding
from .redaction import redact_secrets
from .source_analysis import analyze_sources
from .submission_content_scan import (
    ContentScan,
    find_secrets,
    term_matches,
    term_rule_description,
)
from .submission_options import SubmissionOptions
from .submission_scan_findings import (
    DEFAULT_SCAN_LIMITS,
    MAX_LISTED,
    Describe,
    ScanLimits,
    ScanRule,
    listed,
    scan_finding,
    short_name,
)
from .tex_lexer import MAX_SOURCE_BYTES, mask_literals

_ASSET_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".eps", ".ps", ".svg", ".csv", ".dat"}
_VERSION = re.compile(
    r"\\Provides(?:Class|Package|File)\s*\{([^{}]+)\}\s*\["
    r"\s*(\d{4}[-/]\d{2}[-/]\d{2})(?:\s+(v[\w.+-]+))?"
)
# PDF information-dictionary entries compared with the identity terms.
_MAX_PDF_METADATA_ENTRIES = 10_000
# Leading bytes read to tell a PDF ("%PDF-") or ZIP archive ("PK\x03\x04") from other files.
_SIGNATURE_PREFIX_BYTES = 8
_DEFAULT_OPTIONS = SubmissionOptions()


def _long_file_names(limit: int) -> Describe:
    def describe(matches: Sequence[Mapping[str, object]], count: str) -> str:
        names = [
            f"{short_name(str(match['path']))} ({len(Path(str(match['path'])).name)} characters)"
            for match in matches
        ]
        return f"File names longer than {limit} characters ({count}): {listed(names)}"

    return describe


def _disallowed_characters(allowed: set[str]) -> Describe:
    def describe(matches: Sequence[Mapping[str, object]], count: str) -> str:
        names = []
        for match in matches:
            path = str(match["path"])
            unexpected = sorted(set(Path(path).name) - allowed)
            shown = ", ".join(map(repr, unexpected[:MAX_LISTED]))
            hidden = more_suffix(len(unexpected) - MAX_LISTED)
            names.append(f"{short_name(path)} (not allowed: {shown}{hidden})")
        return f"File names with characters outside the allowed set ({count}): {listed(names)}"

    return describe


def _other_extensions(matches: Sequence[Mapping[str, object]], count: str) -> str:
    names = []
    for match in matches:
        path = str(match["path"])
        names.append(f"{short_name(path)} ({Path(path).suffix.lower() or 'no extension'})")
    return f"Files with extensions that are not allowed ({count}): {listed(names)}"


def _filename_findings(
    files: Mapping[str, Path], issues: list[str], options: SubmissionOptions, limits: ScanLimits
) -> list[Finding]:
    scope = "All supplied regular-file basenames."
    findings = []
    if options.filename_max_length is not None:
        limit = options.filename_max_length
        rule = ScanRule("submission.filename_length", scope, describe=_long_file_names(limit))
        matches = [{"path": name} for name in files if len(Path(name).name) > limit]
        findings.append(scan_finding(rule, matches, issues, limits))
    if options.filename_allowed_characters is not None:
        allowed = set(options.filename_allowed_characters)
        rule = ScanRule(
            "submission.filename_characters", scope, describe=_disallowed_characters(allowed)
        )
        matches = [{"path": name} for name in files if set(Path(name).name) - allowed]
        findings.append(scan_finding(rule, matches, issues, limits))
    if options.allowed_extensions:
        allowed_extensions = {extension.lower() for extension in options.allowed_extensions}
        rule = ScanRule("submission.file_extensions", scope, describe=_other_extensions)
        matches = [
            {"path": name} for name in files if Path(name).suffix.lower() not in allowed_extensions
        ]
        findings.append(scan_finding(rule, matches, issues, limits))
    return findings


def check_submission(
    root: Path,
    main: str | None = None,
    options: SubmissionOptions = _DEFAULT_OPTIONS,
    *,
    limits: ScanLimits = DEFAULT_SCAN_LIMITS,
    analysed: AnalysedSource | None = None,
) -> list[Finding]:
    """Inspect local files without modifying or excluding any bundle contents.

    ``analysed`` shares the source analysis of the same root and main file with other
    checks; the unused-asset scan is the only one that needs it.
    """
    if not options.inspects_bundle():
        return []
    files, inventory_issues = inventory_files(root)
    findings = _filename_findings(files, inventory_issues, options, limits)
    content = ContentScan(options, inventory_issues, limits)
    for name, path in files.items():
        cancellation_point()
        content.scan(name, path)
    findings.extend(content.findings())
    if options.required_deliverables:
        findings.append(_deliverables(files, inventory_issues, options, limits))
    if options.template_references:
        findings.extend(_templates(files, inventory_issues, options, limits))
    if options.report_unused_assets:
        findings.append(_unused(root, main, files, inventory_issues, limits, analysed))
    return redact_findings(findings, content.secret_values)


# What is wrong with a present deliverable whose content does not match its declared kind.
_DELIVERABLE_PROBLEMS = {
    "pdf": "does not start like a PDF file",
    "zip": "does not start like a ZIP archive",
    "text": "is empty or is not UTF-8 text",
}


def _unmet_deliverables(matches: Sequence[Mapping[str, object]], count: str) -> str:
    problems = []
    for match in matches:
        path = short_name(str(match["path"]))
        if match.get("reason") == "missing regular file":
            problems.append(f"{path} is missing")
        else:
            problem = _DELIVERABLE_PROBLEMS.get(str(match.get("kind")), "does not match")
            problems.append(f"{path} {problem}")
    return f"Required deliverables not satisfied ({count}): {listed(problems)}"


def _has_kind_evidence(kind: str, data: bytes) -> bool:
    """Whether the leading bytes (PDF, ZIP) or the whole content (text) fit the declared kind."""
    if kind == "pdf":
        return data.startswith(b"%PDF-")
    if kind == "zip":
        return data.startswith((b"PK\x03\x04", b"PK\x05\x06"))
    try:
        return bool(data) and "\x00" not in data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return False


def _deliverables(
    files: dict[str, Path], issues: list[str], options: SubmissionOptions, limits: ScanLimits
) -> Finding:
    matches = []
    incomplete = list(issues)
    budget = ByteBudget()
    for name, kind in options.required_deliverables:
        cancellation_point()
        path = files.get(name)
        if path is None:
            if not issues:
                matches.append({"path": name, "kind": kind, "reason": "missing regular file"})
            continue
        if kind == "file":
            continue
        try:
            if kind in {"pdf", "zip"}:
                data = budget.read_prefix(path, _SIGNATURE_PREFIX_BYTES)
            else:
                data = budget.read(path)
        except OSError:
            incomplete.append(
                f"Required deliverable could not be inspected: {redact_secrets(name)}"
            )
            continue
        if not _has_kind_evidence(kind, data):
            matches.append({"path": name, "kind": kind, "reason": "format evidence does not match"})
    rule = ScanRule(
        "submission.deliverables",
        "Explicit project-relative paths; PDF/ZIP signatures and bounded UTF-8 text only, "
        "not format validation or verification of separately transmitted deliverables.",
        describe=_unmet_deliverables,
    )
    return scan_finding(rule, matches, incomplete, limits)


def _declared_version(raw: bytes) -> tuple[str, ...] | None:
    """The ``\\ProvidesClass``-style name, date and version declared in a file, if any."""
    try:
        match = _VERSION.search(mask_literals(raw.decode("utf-8-sig")))
    except UnicodeDecodeError:
        return None
    return match.groups() if match else None


def _template_comparison(
    name: str, reference: str, files: Mapping[str, Path], budget: ByteBudget
) -> tuple[list[dict[str, object]], dict[str, object]]:
    """The matches and details of comparing a project template with its reference file."""
    details: dict[str, object] = {"project_path": name, "reference_path": reference}
    actual = budget.read(files[name])
    expected = budget.read(Path(reference).expanduser())
    details["byte_equal"] = actual == expected
    versions = [_declared_version(actual), _declared_version(expected)]
    details["declared_version_comparison"] = (
        ("equal" if versions[0] == versions[1] else "different") if all(versions) else "unavailable"
    )
    matches: list[dict[str, object]] = []
    if actual != expected:
        matches.append({"path": name, "reason": "bytes differ from explicit reference"})
    return matches, details


def _templates(
    files: dict[str, Path], issues: list[str], options: SubmissionOptions, limits: ScanLimits
) -> list[Finding]:
    results = []
    budget = ByteBudget()
    rule = ScanRule(
        "submission.template_reference",
        "Explicit local author-provided reference pairs; equal bytes establish equality only. "
        "Version comparisons use supported literal Provides declarations and do not infer age, "
        "official status or semantic compatibility.",
    )
    for name, reference in options.template_references:
        cancellation_point()
        incomplete = list(issues)
        matches: list[dict[str, object]] = []
        details: dict[str, object] = {"project_path": name, "reference_path": reference}
        if name not in files:
            if not issues:
                matches.append({"path": name, "reason": "missing project template"})
        else:
            try:
                matches, details = _template_comparison(name, reference, files, budget)
            except (OSError, RuntimeError):
                # RuntimeError: ``Path.expanduser`` cannot find a home directory.
                incomplete.append(
                    "Project template or supplied reference is unreadable, linked "
                    "or over the byte limit."
                )
        results.append(scan_finding(rule, matches, incomplete, limits, details=details))
    return results


def _unused_assets(matches: Sequence[Mapping[str, object]], count: str) -> str:
    names = [short_name(str(match["path"])) for match in matches]
    return f"Asset files not referenced by the selected document ({count}): {listed(names)}"


def _unused(
    root: Path,
    main: str | None,
    files: dict[str, Path],
    issues: list[str],
    limits: ScanLimits,
    analysed: AnalysedSource | None,
) -> Finding:
    incomplete = list(issues)
    candidates = []
    if not incomplete:
        try:
            analysis = analyze_sources(root, main, analysed=analysed)
            if analysis.main is None:
                incomplete.append("No unambiguous selected document root is available.")
            if any(
                item.status == "inconclusive" or item.severity == "error"
                for item in analysis.findings
            ):
                incomplete.append(
                    "The selected literal dependency graph has missing or uncertain inputs."
                )
            candidates = [
                {"path": name}
                for name, path in files.items()
                if path.suffix.lower() in _ASSET_EXTENSIONS and name not in analysis.dependencies
            ]
        except OSError:
            incomplete.append("The selected source dependency graph could not be inspected.")
    rule = ScanRule(
        "submission.unused_assets",
        "Supported asset extensions outside one selected literal source/dependency graph; "
        "other document roots, generated/dynamic dependencies "
        "and toolchain inputs may need these files. "
        "No candidate is safe to delete based on this scan.",
        advisory=True,
        describe=_unused_assets,
    )
    result = scan_finding(rule, candidates, incomplete, limits)
    return replace(result, status="inconclusive") if incomplete else result


def check_submission_archive(
    archive: Path,
    options: SubmissionOptions = _DEFAULT_OPTIONS,
    *,
    limits: ScanLimits = DEFAULT_SCAN_LIMITS,
) -> list[Finding]:
    """Check the actual compressed archive length independently of import limits."""
    maximum = options.max_archive_bytes
    if maximum is None:
        return []
    cancellation_point()
    issues = []
    matches = []
    details: dict[str, object] = {"maximum_bytes": maximum}
    try:
        metadata = archive.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError("Archive is not a regular file.")
        details["compressed_bytes"] = metadata.st_size
        if metadata.st_size > maximum:
            matches.append({"reason": "compressed archive exceeds configured byte limit"})
    except OSError:
        issues.append("The final archive is missing or is not an accessible regular file.")

    def describe(_matches: Sequence[Mapping[str, object]], _count: str) -> str:
        return (
            f"The compressed archive is {details['compressed_bytes']:,} bytes, "
            f"over the configured limit of {maximum:,} bytes"
        )

    rule = ScanRule(
        "submission.archive_size", "Actual compressed archive bytes.", describe=describe
    )
    return [scan_finding(rule, matches, issues, limits, details=details)]


@dataclass(slots=True)
class _PdfEvidence:
    """The extracted PDF text and metadata to scan, and why that evidence is incomplete."""

    # Each value is a channel ("text" or "metadata") and the text found there.
    values: list[tuple[str, str]] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    used_bytes: int = 0

    def add_text(self, text: str, limits: ScanLimits) -> None:
        if not isinstance(text, str) or not text.strip():
            self.issues.append("Extracted PDF text is unavailable or empty.")
            return
        size = len(text.encode("utf-8"))
        if size > limits.max_pdf_evidence_bytes:
            self.issues.append("Extracted PDF text exceeds the scan limit.")
            return
        self.values.append(("text", text))
        self.used_bytes = size

    def add_metadata(self, metadata: Mapping[str, object], limits: ScanLimits) -> None:
        if not isinstance(metadata, Mapping) or not metadata:
            self.issues.append("PDF metadata evidence is unavailable or empty.")
            return
        for index, (key, value) in enumerate(metadata.items(), 1):
            cancellation_point()
            if index > _MAX_PDF_METADATA_ENTRIES:
                self.issues.append("PDF metadata exceeds the entry limit.")
                return
            if (
                not isinstance(key, str)
                or len(key) > MAX_SOURCE_BYTES
                or not isinstance(value, (str, int, float, bool))
                or len(str(value)) > MAX_SOURCE_BYTES
            ):
                self.issues.append(
                    "A PDF metadata value could not be scanned within the text budget."
                )
                continue
            joined = key + " " + str(value)
            self.used_bytes += len(joined.encode("utf-8"))
            if self.used_bytes > limits.max_pdf_evidence_bytes:
                self.issues.append("Combined PDF evidence exceeds the scan budget.")
                return
            self.values.append(("metadata", joined))


def check_pdf_identity(
    text: str,
    metadata: Mapping[str, object],
    options: SubmissionOptions = _DEFAULT_OPTIONS,
    *,
    limits: ScanLimits = DEFAULT_SCAN_LIMITS,
) -> list[Finding]:
    """Scan supplied extracted evidence; empty text/metadata mark extraction incomplete."""
    if not options.identity_terms and not options.scan_secrets:
        return []
    cancellation_point()
    evidence = _PdfEvidence()
    evidence.add_text(text, limits)
    evidence.add_metadata(metadata, limits)
    identity_matches = []
    secret_matches = []
    secret_values = set()
    for channel, value in evidence.values:
        cancellation_point()
        if len(identity_matches) < limits.max_matches:
            for index in term_matches(value, options):
                identity_matches.append({"channel": channel, "term_index": index})
        if len(secret_matches) < limits.max_matches:
            for secret in find_secrets(value, limits.max_matches):
                secret_values.add(secret.value)
                secret_matches.append({"channel": channel, "kind": secret.kind})
    results = []
    if options.identity_terms:
        rule = ScanRule(
            "submission.pdf_identity_terms",
            "Supplied extracted PDF text and metadata; Unicode-normalized "
            f"{term_rule_description(options)} only.",
        )
        results.append(scan_finding(rule, identity_matches, evidence.issues, limits))
    if options.scan_secrets:
        rule = ScanRule(
            "submission.secrets",
            "Supplied extracted PDF text and metadata; values are redacted.",
            advisory=True,
        )
        results.append(scan_finding(rule, secret_matches, evidence.issues, limits))
    return redact_findings(results, secret_values)
