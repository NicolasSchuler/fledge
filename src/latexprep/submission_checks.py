"""Bounded, local submission-policy checks; never edit sources or execute TeX.

Identity checks are literal whole-word (or, on request, substring) scans, not an
anonymity guarantee. Privacy checks cover the whole supplied bundle; unused-asset
hints alone use one selected literal dependency graph. No publisher policy or
template registry is embedded.

This module owns the bundle-level policies: file names, deliverables, template
references, unused assets, the compressed archive, the PDF identity scan and the scan of
the information that included PDFs left in a built PDF, and it orchestrates the content
scan. The options are in ``submission_options``, the single
pass over file contents in ``submission_content_scan``, and the shared finding
wording in ``submission_scan_findings``.
"""

from __future__ import annotations

import re
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

from .analysed_source import AnalysedSource
from .cancellation import cancellation_point
from .errors import PreparationError
from .file_safety import FileTooLargeError, read_regular_file
from .file_scan import ByteBudget, inventory_files
from .finding_text import more_suffix, plural, redact_findings
from .finding_weight import Weight
from .findings import EvidenceKind, Finding, Status
from .limits import MIB
from .metadata_privacy import home_folder_path_kind
from .pdf_metadata_reader import (
    MetadataOrigin,
    NotPdfError,
    PdfMetadata,
    PdfMetadataEntry,
    PdfMetadataLimits,
    read_pdf_metadata,
)
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
# Information that included PDFs left in a built PDF: a found term or home folder warns, and
# a PDF that cannot be read within the bounds only informs, so this default check never turns
# a large or unusual PDF into an advisory.
_INCLUDED_INFORMATION_RULE = "privacy.included_pdf_information"
_INCLUDED_INFORMATION_WEIGHT = Weight("warning", "info", EvidenceKind.HEURISTIC)
# The baseline of prepare is not published, so what it carries is only information.
_BASELINE_INFORMATION_WEIGHT = Weight("info", "info", EvidenceKind.HEURISTIC)
# Which PDF the included-PDF scan reads: the one PDF that check builds (``built``), the
# baseline that prepare builds from the unchanged sources and does not publish
# (``baseline``), or the PDF that prepare publishes (``published``).
BuiltPdfRole = Literal["built", "baseline", "published"]
# The largest built PDF whose objects are read for included-PDF information: the file is held
# in memory, so the bound matches the memory a default lease of the shared budget reserves.
_MAX_BUILT_PDF_BYTES = 64 * MIB
# Fields named in the message; the details list every match.
_MAX_LISTED_FIELDS = 3
_SUPPRESSION_ADVICE = (
    "Add \\pdfsuppressptexinfo=-1 (pdfLaTeX) or \\pdfvariable suppressoptionalinfo 15 "
    "(LuaLaTeX) at the start of the main file, or set "
    "source_transforms.suppress_included_pdf_information = true so that prepare adds it to "
    "the prepared copy. The included page's XMP and application data are copied regardless: "
    "re-export a figure whose page-level metadata holds the value."
)
_DEFAULT_OPTIONS = SubmissionOptions()


def _long_file_names(limit: int) -> Describe:
    """Return the message wording for file names longer than ``limit`` characters."""

    def describe(matches: Sequence[Mapping[str, object]], count: str) -> str:
        """Describe the over-long file names, each with its length in characters.

        Args:
            matches: The matches of the scan, each with the ``path`` of a file.
            count: The number of matches as text, such as ``5`` or ``200+``.

        Returns:
            The finding message.
        """
        names = [
            f"{short_name(str(match['path']))} ({len(Path(str(match['path'])).name)} characters)"
            for match in matches
        ]
        return f"File names longer than {limit} characters ({count}): {listed(names)}"

    return describe


def _disallowed_characters(allowed: set[str]) -> Describe:
    """Return the message wording for file names with characters outside ``allowed``."""

    def describe(matches: Sequence[Mapping[str, object]], count: str) -> str:
        """Describe the file names, each with the first few characters that are not allowed.

        Args:
            matches: The matches of the scan, each with the ``path`` of a file.
            count: The number of matches as text, such as ``5`` or ``200+``.

        Returns:
            The finding message.
        """
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
    """Describe the files whose extensions are not allowed, each with its extension.

    Args:
        matches: The matches of the scan, each with the ``path`` of a file.
        count: The number of matches as text, such as ``5`` or ``200+``.

    Returns:
        The finding message.
    """
    names = []
    for match in matches:
        path = str(match["path"])
        names.append(f"{short_name(path)} ({Path(path).suffix.lower() or 'no extension'})")
    return f"Files with extensions that are not allowed ({count}): {listed(names)}"


def _filename_findings(
    files: Mapping[str, Path], issues: list[str], options: SubmissionOptions, limits: ScanLimits
) -> list[Finding]:
    """Check every file basename against the configured length, character and extension policies.

    Args:
        files: The supplied regular files by project path.
        issues: The reasons the file inventory was incomplete.
        options: The submission options that configure the policies.
        limits: The limits of the scans.

    Returns:
        One finding per configured policy.
    """
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

    Args:
        root: The project directory.
        main: The project path of the selected main file, if any; only the unused-asset
            scan uses it.
        options: The submission options that select and configure the checks.
        limits: The limits of the scans.
        analysed: The shared source analysis of ``root`` and ``main``, if one exists.

    Returns:
        The findings of the selected checks, with every discovered secret value redacted;
        empty when the options select no bundle check.
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
    """Describe the required deliverables that are missing or do not match their kind.

    Args:
        matches: The matches of the scan, each with the ``path``, ``kind`` and ``reason``
            of a deliverable.
        count: The number of matches as text, such as ``5`` or ``200+``.

    Returns:
        The finding message.
    """
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
    """Return whether ``data`` fits the declared ``kind`` of a deliverable.

    The leading bytes are judged for a PDF or ZIP file and the whole content for any other
    kind (text): it must be non-empty UTF-8 without NUL characters.

    Args:
        kind: The declared kind of the deliverable.
        data: The leading bytes (PDF, ZIP) or the whole content (text) of the file.

    Returns:
        ``True`` when the bytes fit the kind.
    """
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
    """Check that each required deliverable is present and fits its declared kind.

    A deliverable of kind ``file`` needs only to be present. While the file inventory is
    incomplete, a missing deliverable is not reported as a match.

    Args:
        files: The supplied regular files by project path.
        issues: The reasons the file inventory was incomplete.
        options: The submission options that list the required deliverables.
        limits: The limits of the scan.

    Returns:
        The finding of the deliverable check.
    """
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
    r"""Return the ``\ProvidesClass``-style name, date and version declared in a file, if any.

    Literal regions are masked before the declaration is searched.

    Args:
        raw: The bytes of the file.

    Returns:
        The declared name, date and version (``None`` when no version is given), or
        ``None`` when the file is not UTF-8 or declares none.
    """
    try:
        match = _VERSION.search(mask_literals(raw.decode("utf-8-sig")))
    except UnicodeDecodeError:
        return None
    return match.groups() if match else None


def _template_comparison(
    name: str, reference: str, files: Mapping[str, Path], budget: ByteBudget
) -> tuple[list[dict[str, object]], dict[str, object]]:
    """Compare a project template with its reference file.

    Args:
        name: The project path of the template.
        reference: The path of the reference file; ``~`` is expanded.
        files: The supplied regular files by project path, which include ``name``.
        budget: The byte budget that both reads count against.

    Returns:
        The matches, one when the bytes differ, and the details: both paths, whether the
        bytes are equal, and how the declared versions compare.

    Raises:
        OSError: A file is a link or a special file, holds more than the budget allows, or
            reading it failed.
        RuntimeError: ``Path.expanduser`` cannot find a home directory.
    """
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
    """Compare each configured project template with its reference file.

    Args:
        files: The supplied regular files by project path.
        issues: The reasons the file inventory was incomplete.
        options: The submission options that list the template references.
        limits: The limits of the scan.

    Returns:
        One finding per template reference.
    """
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
    """Describe the asset files that the selected document does not reference.

    Args:
        matches: The matches of the scan, each with the ``path`` of a file.
        count: The number of matches as text, such as ``5`` or ``200+``.

    Returns:
        The finding message.
    """
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
    """List the asset files outside the selected literal dependency graph.

    The finding is advisory, and inconclusive whenever the file inventory or the
    dependency graph is incomplete; no candidate is safe to delete based on it.

    Args:
        root: The project directory.
        main: The project path of the selected main file, if any.
        files: The supplied regular files by project path.
        issues: The reasons the file inventory was incomplete; with any, the graph is not
            analysed.
        limits: The limits of the scan.
        analysed: The shared source analysis of ``root`` and ``main``, if one exists.

    Returns:
        The finding of the unused-asset scan.
    """
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
    """Check the actual compressed archive length independently of import limits.

    Args:
        archive: The final compressed archive.
        options: The submission options, whose ``max_archive_bytes`` sets the limit.
        limits: The limits of the scan.

    Returns:
        One finding, or none when no archive limit is configured.
    """
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
        """Describe the archive size and the configured limit."""
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
    """The extracted PDF text and metadata to scan, and why that evidence is incomplete.

    Attributes:
        values: The evidence to scan, in the order it was accepted.
        issues: The reasons the evidence is incomplete.
        used_bytes: The UTF-8 bytes of the evidence accepted so far.
    """

    # Each value is a channel ("text" or "metadata") and the text found there.
    values: list[tuple[str, str]] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    used_bytes: int = 0

    def add_text(self, text: str, limits: ScanLimits) -> None:
        """Add the extracted PDF text, or an issue when it is missing, empty or over the limit.

        Args:
            text: The extracted text.
            limits: The limits of the scan, whose ``max_pdf_evidence_bytes`` bounds the text.
        """
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
        """Add each metadata entry as its key and value joined by a space.

        An entry that is not a string key with a scalar value within ``MAX_SOURCE_BYTES`` is
        skipped with an issue. Collection stops with an issue after
        ``_MAX_PDF_METADATA_ENTRIES`` entries or when the combined evidence exceeds
        ``limits.max_pdf_evidence_bytes``; missing or empty metadata is an issue too.

        Args:
            metadata: The PDF metadata.
            limits: The limits of the scan.
        """
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
    """Scan supplied extracted evidence; empty text/metadata mark extraction incomplete.

    Args:
        text: The text extracted from the PDF.
        metadata: The PDF metadata.
        options: The submission options, whose identity terms and secret scan select the
            checks.
        limits: The limits of the scan.

    Returns:
        One finding for the identity terms and one for secrets, as configured, with every
        secret value found redacted; empty when neither is configured.
    """
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
            identity_matches.extend(
                {"channel": channel, "term_index": index} for index in term_matches(value, options)
            )
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


def _included_information_matches(
    entries: Sequence[PdfMetadataEntry], options: SubmissionOptions, limit: int
) -> tuple[list[dict[str, object]], bool]:
    """Find identity terms and home-folder paths in the information copied from included PDFs.

    Args:
        entries: The entries copied from included PDFs.
        options: The submission options with the identity terms and their matching mode.
        limit: The most matches kept.

    Returns:
        Each match's field with the term's index or the kind of path, and whether matches
        beyond ``limit`` were dropped.
    """
    matches: list[dict[str, object]] = []
    found = 0
    for entry in entries:
        cancellation_point()
        items: list[dict[str, object]] = [
            {"channel": entry.channel, "term_index": index}
            for index in (term_matches(entry.text, options) if options.identity_terms else [])
        ]
        if kind := home_folder_path_kind(entry.text):
            items.append({"channel": entry.channel, "kind": kind})
        found += len(items)
        matches.extend(items[: max(0, limit - len(matches))])
    return matches, found > len(matches)


def _described_match(match: Mapping[str, object]) -> str:
    """Describe one match by its field and what was found, never by the value."""
    found = match.get("kind") or f"identity term {match.get('term_index')}"
    return f"{match['channel']} ({found})"


def _included_information_finding(
    status: Status,
    message: str,
    details: dict[str, object],
    suggestion: str | None = None,
    *,
    weight: Weight = _INCLUDED_INFORMATION_WEIGHT,
) -> Finding:
    """Build a ``privacy.included_pdf_information`` finding, by default with the advisory weight."""
    return Finding(
        _INCLUDED_INFORMATION_RULE,
        message,
        severity=weight.severity(status),
        status=status,
        evidence_kind=weight.evidence_kind,
        incomplete=status == "inconclusive",
        suggestion=suggestion,
        details={
            "scope": "Information that pdfTeX and LuaTeX copied from included PDFs into form "
            "XObjects: /PTEX.FileName, /PTEX.InfoDict, and the included page's XMP and "
            "PieceInfo; configured identity terms and home-folder paths, reported by field "
            "and never by value.",
            **details,
        },
    )


def check_included_pdf_information(
    pdf: Path,
    options: SubmissionOptions = _DEFAULT_OPTIONS,
    *,
    role: BuiltPdfRole = "built",
    limits: PdfMetadataLimits | None = None,
) -> list[Finding]:
    """Report identity terms and home-folder paths that included PDFs left in a built PDF.

    pdfTeX and LuaTeX copy each included PDF's information dictionary (``/PTEX.InfoDict``)
    and file name (``/PTEX.FileName``), with the included page's XMP and application data,
    into the form XObject that draws it. Poppler's ``pdfinfo`` reports none of them, so the
    PDF identity scan does not see them.

    The PDF that a job ships is the one judged. The baseline of ``prepare`` is built from
    the unchanged sources and never published, so what it carries is reported as
    information that cannot change the outcome; the published PDF is judged on its own.

    Args:
        pdf: The built PDF.
        options: The submission options; their identity terms are searched, and are empty
            when the PDF identity check is not selected.
        role: Which PDF ``pdf`` is: the one ``check`` builds, the baseline of ``prepare``,
            or the PDF ``prepare`` publishes.
        limits: The bounds of reading the PDF; the defaults when ``None``.

    Returns:
        One ``privacy.included_pdf_information`` finding. A PDF that is missing, unreadable
        or not a PDF the reader can walk gives none, because the PDF inspection reports it,
        and so does a PDF without information from an included PDF, unless it is the
        published one, whose clean result is recorded as a pass.
    """
    try:
        metadata = read_pdf_metadata(read_regular_file(pdf, _MAX_BUILT_PDF_BYTES), limits)
    except FileTooLargeError:
        reason = "the PDF is larger than the scan reads"
    except PreparationError as error:
        if isinstance(error, NotPdfError):
            return []
        reason = str(error)
    except OSError:
        return []
    else:
        return _included_information_findings(metadata, options, role)
    return [
        _included_information_finding(
            "inconclusive",
            f"Information copied from included PDFs could not be checked: {reason}.",
            {"issues": [reason]},
        )
    ]


def _included_information_findings(
    metadata: PdfMetadata, options: SubmissionOptions, role: BuiltPdfRole
) -> list[Finding]:
    """Judge the information that included PDFs left in a PDF whose metadata was read.

    Args:
        metadata: The metadata read from the PDF.
        options: The submission options with the identity terms and their matching mode.
        role: Which PDF of the job the metadata was read from.

    Returns:
        A finding naming the fields with a term or home-folder path (a warning, or for the
        baseline of ``prepare`` information), a passed one when there is none, or no
        finding when no included PDF left information in a PDF that is not published.
    """
    entries = [entry for entry in metadata.entries if entry.origin is MetadataOrigin.INCLUDED]
    if not entries:
        if role != "published":
            return []
        return [
            _included_information_finding(
                "passed",
                "The published PDF carries no information copied from included PDFs.",
                {"matches": [], "included_objects": metadata.included_objects},
            )
        ]
    limit = DEFAULT_SCAN_LIMITS.max_matches
    matches, limited = _included_information_matches(entries, options, limit)
    details: dict[str, object] = {
        "matches": matches,
        "match_output_limited": limited,
        "included_objects": metadata.included_objects,
        "channels": sorted({entry.channel for entry in entries}),
        "limitations": metadata.limitations,
    }
    copied = plural(metadata.included_objects, "included PDF")
    if not matches:
        return [
            _included_information_finding(
                "passed",
                f"The PDF carries information copied from {copied}; none of it holds a "
                "configured identity term or a home-folder path.",
                details,
            )
        ]
    shown = ", ".join(_described_match(match) for match in matches[:_MAX_LISTED_FIELDS])
    fields = f"{shown}{more_suffix(len(matches) - _MAX_LISTED_FIELDS)}"
    if role == "baseline":
        return [
            _included_information_finding(
                "failed",
                "The PDF built from the unchanged sources carries information copied from "
                f"{copied} that may identify its authors: {fields}. prepare does not publish "
                "this PDF; the PDF it publishes is checked on its own.",
                details,
                weight=_BASELINE_INFORMATION_WEIGHT,
            )
        ]
    return [
        _included_information_finding(
            "failed",
            f"The PDF carries information copied from {copied} that may identify its authors: "
            f"{fields}.",
            details,
            _SUPPRESSION_ADVICE,
        )
    ]
