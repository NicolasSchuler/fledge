r"""Bounded scan of figure metadata for identity terms and home-folder paths.

The scanned files are the PNG, JPEG and PDF figures that the selected document includes with
``\includegraphics`` or ``\includepdf``, as the literal source graph resolves them; images
the document does not include never ship and are left alone. When that set is uncertain,
because no single main file was selected, a source could not be read or the document's
sources use a construct the scan cannot follow, every PNG and JPEG file of the project is
scanned instead, with the PDFs the graph does resolve. Bounded readers decode the metadata
without reading pixels or rendering a page:
``image_metadata_decoders`` reads PNG text, EXIF, XMP, IPTC and comments, and
``pdf_metadata_reader`` reads a PDF's information dictionary, XMP and application data.
Two checks search the decoded text:

- ``PRV103`` reports configured identity terms, when ``metadata_privacy.image_identity_terms``
  lists them; a match is an error.
- ``PRV104`` reports personal home-folder paths such as ``/Users/<name>/``; it is on by
  default and a match is a warning, because a path in a figure's title or file record
  names its author's account whatever the venue.

Vendor blocks that cannot be decoded portably are reported as limitations; a structural
defect makes only that file's result inconclusive. Matches report the file, the metadata
channel and the term's index or the kind of path, never the matched text.

Does not own: the settings and the recognition of paths (``metadata_privacy``), source
metadata edits (``metadata_source_edits``), what the built PDF copied from included PDFs
(``submission_checks``) or any pixel analysis. No OCR and no guarantee of anonymity.
"""

from __future__ import annotations

import html
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Final, NamedTuple

from .analysed_source import AnalysedSource, reuse_or_analyse
from .cancellation import cancellation_point
from .errors import PreparationError
from .expanded_document import reachable_sources
from .file_safety import read_regular_file
from .file_scan import inventory_files
from .finding_text import check_status, plural
from .finding_weight import Weight
from .findings import EvidenceKind, Finding
from .image_metadata_decoders import ImageScanLimits, MetadataText, jpeg_metadata, png_metadata
from .limits import MIB
from .metadata_privacy import MetadataPrivacyOptions, home_folder_path_kind, normalize_for_matching
from .pdf_metadata_reader import PdfMetadataLimits, read_pdf_metadata
from .redaction import redact_secrets

_SCANNED_IMAGE_SUFFIXES: Final = frozenset({".png", ".jpg", ".jpeg"})
_FIGURE_SUFFIXES: Final = _SCANNED_IMAGE_SUFFIXES | {".pdf"}
# Figure formats the scan does not read, named in the result so a pass is not over-read.
_UNSCANNED_FIGURE_FORMATS: Final = "EPS, SVG, TIFF, GIF and WebP figures"
# The commands that include a figure or a PDF page; pdfTeX and LuaTeX embed a PDF target as
# a form XObject.
_FIGURE_COMMANDS: Final = frozenset({"includegraphics", "includepdf"})
# Decompressed object and metadata streams of all PDF figures together; a project of many
# small compressed figures cannot make the scan inflate without bound.
_MAX_PDF_DECODED_BYTES: Final = 64 * MIB
# A path match warns; a scan that could not read every figure only informs, because this
# default check must not turn an unreadable vendor block into an advisory.
_HOME_PATH_WEIGHT: Final = Weight("warning", "info", EvidenceKind.HEURISTIC)
_TERMS_RULE: Final = "privacy.image_metadata"
_HOME_PATH_RULE: Final = "privacy.figure_home_folder_paths"
_SCOPE: Final = (
    "The PNG, JPEG and PDF figures the selected document includes, or every PNG and JPEG "
    "file of the project when that set is uncertain: PNG tEXt/zTXt/iTXt/EXIF, pre-scan "
    "JPEG EXIF/XMP/extended XMP/IPTC IIM application records/comments, and a PDF's "
    "information dictionary, XMP and application data (PieceInfo); literal text with "
    "character-reference decoding; skipped vendor blocks are listed as limitations; no "
    "OCR, pixel content, later JPEG scans, other image formats or guarantee of anonymity."
)


class _Selection(NamedTuple):
    """The figures one scan reads, and how they were chosen.

    Attributes:
        files: Each file's project path and location.
        issues: The problems that left the selection incomplete.
        uncertainty: Why the document's own figures could not be determined, so that every
            PNG and JPEG file of the project was taken instead; ``None`` when the selection
            is exactly the document's figures.
    """

    files: list[tuple[str, Path]]
    issues: list[dict[str, str]]
    uncertainty: str | None


def _unreadable_reason(error: Exception) -> str:
    """Explain why a file's metadata could not be read, without quoting contents or paths.

    Parser rejections are fixed text; other errors may quote file contents or paths, so they
    are described by kind only.
    """
    if isinstance(error, PreparationError):
        return str(error)
    if isinstance(error, UnicodeError):
        return "a metadata text field uses an unsupported character encoding"
    if isinstance(error, zlib.error):
        return "compressed metadata is damaged"
    if isinstance(error, OSError):
        return "the file is unreadable, not a regular file or larger than the scan limit"
    return "a metadata block is malformed"


def _first_problem(issues: Sequence[Mapping[str, str]]) -> str:
    """Describe the first scan problem with its redacted file path, and count the others."""
    first = issues[0]
    where = f"{redact_secrets(first['path'])}: " if "path" in first else ""
    others = len(issues) - 1
    return f"{where}{first['reason']}" + (f" (and {others} more)" if others else "")


def _is_pdf(name: str) -> bool:
    """Return whether a project path names a PDF file."""
    return PurePosixPath(name).suffix.lower() == ".pdf"


def _describe_files(names: Sequence[str], image_noun: str = "PNG/JPEG image") -> str:
    """Count files as images and PDF figures, such as ``2 PNG/JPEG images and 1 PDF figure``.

    Args:
        names: The project paths of the files.
        image_noun: How a PNG or JPEG file is named.

    Returns:
        The counted description; images alone are counted even when there are none.
    """
    pdfs = sum(1 for name in names if _is_pdf(name))
    images = len(names) - pdfs
    parts = [plural(images, image_noun)] if images or not pdfs else []
    if pdfs:
        parts.append(plural(pdfs, "PDF figure"))
    return " and ".join(parts)


@dataclass
class _ProjectScan:
    """What a scan has found so far across the figures of one project.

    Attributes:
        limits: The bounds of the scan.
        terms: The normalized identity terms; empty when none are configured.
        find_home_paths: Whether to look for home-folder paths.
        matches: The file, channel and 1-based term index of each identity-term match kept.
        home_paths: The file, channel and kind of each home-folder path kept.
        match_output_limited: Whether identity-term matches beyond ``limits.max_matches``
            were dropped.
        home_path_output_limited: Whether home-folder paths beyond ``limits.max_matches``
            were dropped.
        issues: The problems that leave the scan incomplete.
        limitations: The skipped vendor blocks of each file.
        scanned: The project paths of the files scanned.
        image_bytes: The bytes of the files read so far.
        metadata_bytes: The decoded metadata bytes so far.
        pdf_decoded_bytes: The decompressed stream bytes of the PDF figures so far.
    """

    limits: ImageScanLimits
    terms: list[str]
    find_home_paths: bool = False
    matches: list[dict[str, object]] = field(default_factory=list)
    home_paths: list[dict[str, object]] = field(default_factory=list)
    match_output_limited: bool = False
    home_path_output_limited: bool = False
    issues: list[dict[str, str]] = field(default_factory=list)
    limitations: list[dict[str, str]] = field(default_factory=list)
    scanned: list[str] = field(default_factory=list)
    image_bytes: int = 0
    metadata_bytes: int = 0
    pdf_decoded_bytes: int = 0

    def scan_image(self, name: str, path: Path) -> bool:
        """Scan one file; ``False`` when a project-wide limit ends the scan.

        Args:
            name: The project-relative path of the file, under which matches are reported.
            path: The file.

        Returns:
            ``True`` when the scan may continue with the next file.

        Raises:
            PreparationError: The file's metadata is malformed or exceeds a limit.
            OSError: The file is unreadable, not a regular file or too large.
            ValueError: A metadata text is malformed.
            zlib.error: Compressed metadata is damaged.
        """
        data = read_regular_file(path, self.limits.max_image_bytes)
        self.image_bytes += len(data)
        if self.image_bytes > self.limits.max_total_image_bytes:
            self.issues.append({"reason": "Total image bytes exceeded the scan limit"})
            return False
        text = self._decode(name, data)
        self.metadata_bytes += text.size
        if self.metadata_bytes > self.limits.max_total_metadata_bytes:
            self.issues.append({"reason": "Decoded metadata exceeded the scan limit"})
            return False
        self.scanned.append(name)
        # A skipped vendor block narrows this file's scope; the remaining
        # channels still support a determinable result for the project.
        self.limitations.extend({"path": name, "skipped": reason} for reason in text.limitations)
        for channel, value in text.entries:
            self._match(name, channel, value)
        return True

    def _decode(self, name: str, data: bytes) -> MetadataText:
        """Decode the metadata of one file by its suffix.

        Raises:
            PreparationError: The metadata is malformed or exceeds a limit.
            ValueError: A metadata text is malformed.
            zlib.error: Compressed metadata is damaged.
        """
        suffix = PurePosixPath(name).suffix.lower()
        if suffix == ".png":
            return png_metadata(data, self.limits)
        if suffix == ".pdf":
            return self._pdf_text(data)
        return jpeg_metadata(data, self.limits)

    def _pdf_text(self, data: bytes) -> MetadataText:
        """Decode a PDF figure's own metadata and whatever it copied from PDFs it includes.

        Raises:
            PreparationError: The PDF is unreadable, encrypted or exceeds a limit.
        """
        budget = _MAX_PDF_DECODED_BYTES - self.pdf_decoded_bytes
        limits = PdfMetadataLimits(
            max_decoded_bytes=max(0, budget),
            max_text_bytes=self.limits.max_image_metadata_bytes,
        )
        metadata = read_pdf_metadata(data, limits)
        self.pdf_decoded_bytes += metadata.decoded_bytes
        text = MetadataText(self.limits)
        for entry in metadata.entries:
            text.append(f"PDF {entry.channel}", entry.text)
        for limitation in metadata.limitations:
            text.limit(f"PDF {limitation}")
        return text

    def _match(self, name: str, channel: str, value: str) -> None:
        """Record each identity term and home-folder path that a decoded text contains.

        Args:
            name: The project-relative path of the file.
            channel: The metadata channel of the text.
            value: The decoded text, compared after character-reference decoding and
                normalization.
        """
        # XMP may encode names with numeric/predefined XML character references.
        # Decode text only; never evaluate entities or fetch XML resources.
        decoded = html.unescape(value)
        normalized = normalize_for_matching(decoded)
        for index, term in enumerate(self.terms, 1):
            if term not in normalized:
                continue
            if len(self.matches) < self.limits.max_matches:
                self.matches.append({"path": name, "channel": channel, "term_index": index})
            else:
                self.match_output_limited = True
        kind = home_folder_path_kind(decoded) if self.find_home_paths else None
        if kind is None:
            return
        if len(self.home_paths) < self.limits.max_matches:
            self.home_paths.append({"path": name, "channel": channel, "kind": kind})
        else:
            self.home_path_output_limited = True


def _document_figures(source: AnalysedSource) -> tuple[list[str], str | None]:
    r"""Find the figures that the selected document includes, and whether that set is certain.

    Only literal ``\includegraphics`` and ``\includepdf`` targets of the sources that the main
    file reaches count. The set is uncertain without a selected main file, when a source the
    document reads could not be parsed, or when the document's own sources hold a construct
    the literal scan does not follow; such gaps elsewhere in the project do not matter.

    Args:
        source: The analysis of the project and its main file.

    Returns:
        The project paths of the PNG, JPEG and PDF figures, sorted, and why the set is
        uncertain, or ``None`` when it is certain.
    """
    graph = source.graph
    if graph.main is None:
        return [], "no single main file was selected"
    reached = reachable_sources(graph)
    names = sorted(
        {
            target
            for (name, _, _), (target, command) in graph.references.items()
            if command in _FIGURE_COMMANDS
            and name in reached
            and PurePosixPath(target).suffix.lower() in _FIGURE_SUFFIXES
        }
    )
    if graph.unparsed:
        return names, "a source file could not be read"
    if any(item.path is None or item.path in reached for item in graph.uncertainties):
        return names, "the document's sources use constructs the literal scan cannot follow"
    return names, None


def _select_files(root: Path, limits: ImageScanLimits, source: AnalysedSource) -> _Selection:
    """Select the document's figures, or every PNG and JPEG file when they are uncertain.

    Returns:
        The files to scan, at most ``limits.max_images``, with the issues and the reason
        the whole project was scanned, if it was.
    """
    names, uncertainty = _document_figures(source)
    issues: list[dict[str, str]] = []
    if uncertainty is None:
        files = [(name, root / name) for name in names]
    else:
        inventory, inventory_issues = inventory_files(root)
        if inventory_issues:
            issues.append({"reason": "Project inventory was incomplete"})
        files = [
            (name, path)
            for name, path in inventory.items()
            if path.suffix.lower() in _SCANNED_IMAGE_SUFFIXES
        ]
        files += [(name, root / name) for name in names if _is_pdf(name)]
    if len(files) > limits.max_images:
        issues.append({"reason": "Image count exceeded the scan limit"})
        files = files[: limits.max_images]
    return _Selection(files, issues, uncertainty)


def _coverage(scan: _ProjectScan, uncertainty: str | None) -> str:
    """Name the scanned figures and how they were chosen, for a passed message.

    Args:
        scan: The finished scan.
        uncertainty: Why every PNG and JPEG file of the project was scanned, or ``None``
            when the scan read exactly the document's figures.

    Returns:
        A phrase such as ``the metadata of 2 PNG/JPEG images that the document includes``.
    """
    if uncertainty is None:
        if not scan.scanned:
            return "the figures of the document (it includes no PNG, JPEG or PDF figure)"
        return f"the metadata of {_describe_files(scan.scanned)} that the document includes"
    if not scan.scanned:
        return "any PNG or JPEG image (none was found)"
    return f"the metadata of {_describe_files(scan.scanned)}"


def _not_scanned(uncertainty: str | None) -> str:
    """State what the scan did not read, so that a pass is not over-read."""
    if uncertainty is None:
        return (
            "Images and PDF files the document does not include, "
            f"{_UNSCANNED_FIGURE_FORMATS}, and image pixels were not scanned."
        )
    return (
        f"Every PNG and JPEG file of the project was scanned because {uncertainty}. "
        f"PDF files the document does not include, {_UNSCANNED_FIGURE_FORMATS}, and image "
        "pixels were not scanned."
    )


def _terms_message(scan: _ProjectScan, status: str, selection: _Selection) -> str:
    """Describe the outcome of the identity-term scan; a pass states what was not covered.

    Args:
        scan: The finished scan.
        status: The status of the finding.
        selection: The files selected for the scan and how they were chosen.

    Returns:
        The message of the finding.
    """
    match status:
        case "failed":
            found = sorted({str(item["path"]) for item in scan.matches})
            message = (
                "Configured identity terms were found in the metadata of "
                f"{_describe_files(found, 'image')}."
            )
            if scan.issues:
                message += (
                    f" Other files could not be fully checked: {_first_problem(scan.issues)}."
                )
            return message
        case "inconclusive":
            selected = [name for name, _ in selection.files]
            return (
                f"Image metadata could not be fully checked: {_first_problem(scan.issues)}. "
                f"{len(scan.scanned)} of {_describe_files(selected)} were scanned."
            )
    message = (
        "No configured identity terms were found in "
        f"{_coverage(scan, selection.uncertainty)}. {_not_scanned(selection.uncertainty)}"
    )
    if scan.limitations:
        message += " Some vendor-specific blocks were skipped."
    return message


def _details(scan: _ProjectScan, selection: _Selection) -> dict[str, object]:
    """Return the details both findings share: what was selected, scanned and skipped."""
    limit = scan.limits.max_matches
    selected = [name for name, _ in selection.files]
    selected_pdfs = [name for name in selected if _is_pdf(name)]
    scanned_pdfs = [name for name in scan.scanned if _is_pdf(name)]
    return {
        "issues": scan.issues[:limit],
        "limitations": scan.limitations[:limit],
        "incomplete": bool(scan.issues),
        "figures": "project" if selection.uncertainty else "included",
        "figure_uncertainty": selection.uncertainty,
        "images_selected": len(selected) - len(selected_pdfs),
        "images_scanned": len(scan.scanned) - len(scanned_pdfs),
        "pdf_figures_selected": len(selected_pdfs),
        "pdf_figures_scanned": len(scanned_pdfs),
        "scope": _SCOPE,
    }


def _terms_finding(scan: _ProjectScan, selection: _Selection) -> Finding:
    """Return the ``PRV103`` finding: identity terms are a configured policy, so an error."""
    status = check_status(failed=bool(scan.matches), incomplete=bool(scan.issues))
    return Finding(
        _TERMS_RULE,
        _terms_message(scan, status, selection),
        severity="info" if status == "passed" else "error",
        status=status,
        suggestion=(
            "Re-save or re-export each listed image with standard metadata, or remove it "
            "from the project, then run the check again."
            if status == "inconclusive"
            else None
        ),
        details={
            "matches": scan.matches,
            "match_output_limited": scan.match_output_limited,
            **_details(scan, selection),
        },
    )


def _listed_paths(home_paths: Sequence[Mapping[str, object]]) -> str:
    """List the first home-folder paths as ``file (channel: kind)``, counting the rest."""
    shown = [
        f"{redact_secrets(str(item['path']))} ({item['channel']}: {item['kind']})"
        for item in home_paths[:3]
    ]
    others = len(home_paths) - len(shown)
    return ", ".join(shown) + (f" and {others} more" if others else "")


def _home_path_finding(scan: _ProjectScan, selection: _Selection) -> Finding:
    """Return the ``PRV104`` finding: a home-folder path in figure metadata is advisory."""
    status = check_status(failed=bool(scan.home_paths), incomplete=bool(scan.issues))
    suggestion = None
    if status == "failed":
        message = f"Figure metadata names home folders: {_listed_paths(scan.home_paths)}."
        suggestion = (
            "Re-export each listed figure without personal metadata, or clear the named "
            "field, then run the check again. For PDF figures, "
            "source_transforms.suppress_included_pdf_information keeps the information "
            "dictionary and file name out of the PDF that prepare builds, but not the "
            "figure file itself or its page-level XMP and application data."
        )
    elif status == "inconclusive":
        message = (
            "Figure metadata could not be fully checked for home-folder paths: "
            f"{_first_problem(scan.issues)}."
        )
    else:
        message = f"No home-folder paths were found in {_coverage(scan, selection.uncertainty)}."
    return Finding(
        _HOME_PATH_RULE,
        message,
        severity=_HOME_PATH_WEIGHT.severity(status),
        status=status,
        evidence_kind=_HOME_PATH_WEIGHT.evidence_kind,
        suggestion=suggestion,
        details={
            "matches": scan.home_paths,
            "match_output_limited": scan.home_path_output_limited,
            **_details(scan, selection),
        },
    )


def check_image_metadata(
    root: Path,
    options: MetadataPrivacyOptions,
    limits: ImageScanLimits | None = None,
    *,
    main: str | None = None,
    analysed: AnalysedSource | None = None,
    report_home_paths: bool = False,
) -> list[Finding]:
    """Search the metadata of the document's figures without decoding pixels or echoing values.

    The figures are those the selected document includes, or every PNG and JPEG file of the
    project when that set is uncertain. A file that cannot be read makes a result
    inconclusive unless a match was found in another file, which fails it; the scan
    continues with the next file until a project-wide limit ends it.

    Args:
        root: The project directory.
        options: The privacy options, which list the identity terms.
        limits: The bounds of the scan; the defaults when ``None``.
        main: The main file whose figures are scanned; ``None`` selects the project's
            single document root.
        analysed: The source analysis of the same root and main file, shared with other
            checks, or ``None`` to analyse here.
        report_home_paths: Whether to report home-folder paths (``PRV104``).

    Returns:
        The ``privacy.image_metadata`` finding when identity terms are configured, then the
        ``privacy.figure_home_folder_paths`` finding when ``report_home_paths`` is set and
        there is a figure to scan; an empty list when neither applies.
    """
    if not (options.image_identity_terms or report_home_paths):
        return []
    limits = limits or ImageScanLimits()
    selection = _select_files(root, limits, reuse_or_analyse(root, main, analysed))
    terms = [normalize_for_matching(term) for term in options.image_identity_terms]
    scan = _ProjectScan(
        limits, terms, find_home_paths=report_home_paths, issues=list(selection.issues)
    )
    for name, path in selection.files:
        cancellation_point()
        try:
            if not scan.scan_image(name, path):
                break
        except (PreparationError, OSError, ValueError, zlib.error) as error:
            scan.issues.append({"path": name, "reason": _unreadable_reason(error)})
    findings = [_terms_finding(scan, selection)] if options.image_identity_terms else []
    if report_home_paths and (selection.files or scan.issues):
        findings.append(_home_path_finding(scan, selection))
    return findings
