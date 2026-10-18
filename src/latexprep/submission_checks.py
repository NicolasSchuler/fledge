"""Bounded, local submission-policy checks; never edit sources or execute TeX.

Identity checks are literal whole-word (or, on request, substring) scans, not an
anonymity guarantee. Privacy checks cover the whole supplied bundle; unused-asset
hints alone use one selected literal dependency graph. No publisher policy or
template registry is embedded.
"""

from __future__ import annotations

import re
import stat
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import KW_ONLY, dataclass, field, fields, replace
from functools import lru_cache
from pathlib import Path
from urllib.parse import unquote

from .file_scan import (
    MAX_SCAN_ENTRIES,
    MAX_SCAN_MATCHES,
    ByteBudget,
    check_status,
    inventory_files,
    is_relative_project_path,
    marked_comments,
    more_suffix,
    redact_findings,
    redact_secrets,
    tex_comments,
)
from .models import EvidenceKind, Finding, PreparationError
from .redaction import SECRET_PATTERNS
from .scheduler import cancellation_point
from .source import analyze_sources
from .tex_lexer import (
    MAX_SOURCE_BYTES,
    MAX_TOTAL_SOURCE_BYTES,
    blank_regions,
    mask_literals,
    source_regions,
)

IDENTITY_TERM_MATCHING = ("word", "substring")
# Matches named in a message; the details list every match up to MAX_SCAN_MATCHES.
_MAX_LISTED = 3
# Longer file names are shortened in messages; the details keep them whole.
_MAX_LABEL_CHARS = 120
_TEXT_EXTENSIONS = {
    ".tex",
    ".ltx",
    ".latex",
    ".sty",
    ".cls",
    ".def",
    ".cfg",
    ".bib",
    ".bbl",
    ".bst",
    ".txt",
    ".md",
    ".rst",
    ".csv",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".sh",
    ".py",
    ".lua",
    ".pl",
    ".xml",
    ".html",
    ".svg",
    ".dtx",
    ".ins",
    ".tikz",
    ".pgf",
    ".url",
    ".pdf_tex",
    ".log",
}
_TEX_EXTENSIONS = {".tex", ".ltx", ".latex", ".sty", ".cls", ".def", ".cfg", ".dtx"}
_ASSET_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".eps", ".ps", ".svg", ".csv", ".dat"}
_PLACEHOLDERS = {"your_api_key_here", "your_password_here", "example_password", "changemechangeme"}
_HINT = re.compile(
    r"(?i)\\(?:acknowledg(?:e)?ments?|thanks)\b|"
    r"\\(?:section\*?|begin)\s*\{acknowledg(?:e)?ments?\}|"
    r"\b(?:at our (?:university|institution|laboratory)|in our previous work|"
    r"our (?:institution|laboratory|university)|the authors are (?:at|from))\b"
)
_NOTE = re.compile(r"(?i)\b(?:TODO|FIXME|XXX|private note|do not submit|internal only)\b|\?\?")
_SHELL = re.compile(
    r"\\write\s*18\b|\\(?:ShellEscape|ShellEscapeAsync)\b|\\sys_shell_now:[A-Za-z]+\b"
)
_VERSION = re.compile(
    r"\\Provides(?:Class|Package|File)\s*\{([^{}]+)\}\s*\["
    r"\s*(\d{4}[-/]\d{2}[-/]\d{2})(?:\s+(v[\w.+-]+))?"
)


def validate_bbl_format_version(value: object) -> None:
    """A biblatex .bbl format version such as "3.3", as written in the .bbl header."""
    if value is not None and (
        not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,4}(?:\.[0-9]{1,4}){0,3}", value)
    ):
        raise PreparationError(
            'biblatex_bbl_format_version must be a version such as "3.3", as written on the '
            "second line of a biblatex .bbl file."
        )


@dataclass(frozen=True)
class SubmissionOptions:
    identity_terms: tuple[str, ...] = ()
    scan_identity_hints: bool = False
    scan_secrets: bool = False
    scan_private_comments: bool = False
    check_shell_escape: bool = False
    filename_max_length: int | None = None
    filename_allowed_characters: str | None = None
    allowed_extensions: tuple[str, ...] = ()
    required_deliverables: tuple[tuple[str, str], ...] = ()
    max_archive_bytes: int | None = None
    report_unused_assets: bool = False
    template_references: tuple[tuple[str, str], ...] = ()
    identity_term_matching: str = "word"
    check_bbl_coverage: bool = False
    require_bbl: bool = False
    biblatex_bbl_format_version: str | None = None

    def __post_init__(self) -> None:
        if self.identity_term_matching not in IDENTITY_TERM_MATCHING:
            raise PreparationError("identity_term_matching must be 'word' or 'substring'.")
        validate_bbl_format_version(self.biblatex_bbl_format_version)
        for name in (
            "scan_identity_hints",
            "scan_secrets",
            "scan_private_comments",
            "check_shell_escape",
            "report_unused_assets",
            "check_bbl_coverage",
            "require_bbl",
        ):
            if type(getattr(self, name)) is not bool:
                raise PreparationError(f"{name} must be a boolean.")
        for name in ("filename_max_length", "max_archive_bytes"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 1):
                raise PreparationError(f"{name} must be a positive integer.")
        for name in ("identity_terms", "allowed_extensions"):
            values = getattr(self, name)
            if (
                not isinstance(values, tuple)
                or len(values) > 256
                or any(
                    not isinstance(value, str) or not value.strip() or len(value) > 1024
                    for value in values
                )
                or len(set(values)) != len(values)
            ):
                raise PreparationError(f"{name} must contain unique nonempty strings.")
        if any(re.fullmatch(r"\.[A-Za-z0-9_+-]+", ext) is None for ext in self.allowed_extensions):
            raise PreparationError("allowed_extensions must contain dot-prefixed final extensions.")
        chars = self.filename_allowed_characters
        if chars is not None and (not isinstance(chars, str) or not chars or len(chars) > 4096):
            raise PreparationError("filename_allowed_characters must be a nonempty literal set.")
        for name in ("required_deliverables", "template_references"):
            pairs = getattr(self, name)
            if not isinstance(pairs, tuple) or len(pairs) > 256:
                raise PreparationError(f"{name} must be a tuple of path/value pairs.")
            seen = set()
            for pair in pairs:
                if (
                    not isinstance(pair, tuple)
                    or len(pair) != 2
                    or not all(
                        isinstance(value, str)
                        and value
                        and "\x00" not in value
                        and len(value) <= 4096
                        for value in pair
                    )
                ):
                    raise PreparationError(f"{name} must contain nonempty string pairs.")
                path, value = pair
                if not is_relative_project_path(path) or path in seen:
                    raise PreparationError(f"{name} requires unique safe relative project paths.")
                seen.add(path)
                if name == "required_deliverables" and value not in {"file", "pdf", "zip", "text"}:
                    raise PreparationError("Deliverable kind must be file, pdf, zip or text.")


_DEFAULT_OPTIONS = SubmissionOptions()
# Options that do not make check_submission inspect the bundle: a matching mode that
# only refines identity terms, the compressed archive (check_submission_archive) and
# the shipped bibliography (check_bbl in the readiness checks).
_OPTIONS_OUTSIDE_BUNDLE_SCAN = frozenset(
    {
        "identity_term_matching",
        "max_archive_bytes",
        "check_bbl_coverage",
        "require_bbl",
        "biblatex_bbl_format_version",
    }
)


def _inspects_bundle(options: SubmissionOptions) -> bool:
    """Whether any option other than those handled elsewhere differs from its default.

    A newly added option therefore enables the bundle scan unless it is listed above.
    """
    return any(
        getattr(options, option.name) != option.default
        for option in fields(options)
        if option.name not in _OPTIONS_OUTSIDE_BUNDLE_SCAN
    )


def _fold(value: str) -> str:
    return unicodedata.normalize("NFKC", unquote(value)).casefold()


def _secrets(text: str) -> list[tuple[int, str, str]]:
    result = []
    for kind, pattern in SECRET_PATTERNS:
        cancellation_point()
        for match in pattern.finditer(text):
            value = match.groupdict().get("secret") or match.group()
            if value.lower() in _PLACEHOLDERS:
                continue
            result.append((match.start(), kind, value))
            if len(result) >= MAX_SCAN_MATCHES:
                return result
    return result


def _private_note(comment: str) -> str | None:
    return "private note" if _NOTE.search(comment) else None


def _short(value: str) -> str:
    """Shorten an over-long file name; it is the finding itself and the details keep it whole."""
    return value if len(value) <= _MAX_LABEL_CHARS else value[: _MAX_LABEL_CHARS - 1] + "…"


def _listed(items: Sequence[str]) -> str:
    """The first few distinct items; the suffix counts hidden items, repeats included."""
    shown = list(dict.fromkeys(items))[:_MAX_LISTED]
    return ", ".join(shown) + more_suffix(sum(item not in shown for item in items))


def _match_count(matches: Sequence[Mapping[str, object]]) -> str:
    # Scans stop collecting at the limit, so a full list means "at least" this many.
    return f"{len(matches)}{'+' if len(matches) == MAX_SCAN_MATCHES else ''}"


# Renders the matches of one rule for its message; rules without one use ``_message``'s
# generic "N matches: location" wording, which never repeats matched text.
_Describe = Callable[[Sequence[Mapping[str, object]]], str]


def _match_label(match: Mapping[str, object]) -> str:
    # Matches hold only locations and generated reasons, never matched text or terms.
    path, line, reason = match.get("path"), match.get("line"), match.get("reason")
    label = path if isinstance(path, str) else match.get("channel")
    label = label if isinstance(label, str) else ""
    if label and type(line) is int:
        label += f":{line}"
    label = _short(label)
    if isinstance(reason, str):
        label = f"{label} ({reason})" if label else reason
    return label or "unspecified location"


def _message(
    matches: Sequence[Mapping[str, object]], issues: list[str], describe: _Describe | None = None
) -> str:
    if not matches:
        if not issues:
            return "No matches in the stated scope."
        return f"The configured scan is incomplete: {issues[0]}{more_suffix(len(issues) - 1)}"
    if describe is not None:
        message = describe(matches)
    else:
        count = _match_count(matches)
        labels = [_match_label(match) for match in matches]
        message = f"{count} match{'es' if len(matches) != 1 else ''}: {_listed(labels)}"
    if issues:
        message += f"; the scan was also incomplete: {issues[0]}"
    return message


def _scan_finding(
    rule: str,
    matches: Sequence[Mapping[str, object]],
    issues: list[str],
    scope: str,
    *,
    advisory: bool = False,
    details: dict[str, object] | None = None,
    describe: _Describe | None = None,
) -> Finding:
    status = check_status(failed=bool(matches), incomplete=bool(issues))
    # Scans stop collecting at the reporting limit, so further matches may be unlisted.
    limited = len(matches) >= MAX_SCAN_MATCHES
    return Finding(
        rule,
        _message(matches, issues, describe),
        severity="info" if status == "passed" else "warning" if advisory else "error",
        status=status,
        evidence_kind=EvidenceKind.HEURISTIC if advisory else EvidenceKind.DIRECT,
        incomplete=bool(issues) or limited,
        details={
            "scope": scope,
            "matches": [dict(item) for item in matches[:MAX_SCAN_MATCHES]],
            "match_output_limited": limited,
            "issues": issues[:MAX_SCAN_MATCHES],
            **(details or {}),
        },
    )


@lru_cache(maxsize=16)
def _term_patterns(terms: tuple[str, ...], matching: str) -> tuple[re.Pattern[str], ...]:
    """Compile folded terms once; word mode requires non-alphanumeric (or no) neighbours."""
    patterns = []
    for term in terms:
        folded = _fold(term)
        if matching == "word":
            # Underscores and punctuation separate words (jane_smith.tex); letters/digits do not.
            before = r"(?<![^\W_])" if folded[:1].isalnum() else ""
            after = r"(?![^\W_])" if folded[-1:].isalnum() else ""
            patterns.append(re.compile(before + re.escape(folded) + after))
        else:
            patterns.append(re.compile(re.escape(folded)))
    return tuple(patterns)


def _term_matches(text: str, options: SubmissionOptions) -> list[int]:
    folded = _fold(text)
    patterns = _term_patterns(options.identity_terms, options.identity_term_matching)
    return [index for index, pattern in enumerate(patterns, 1) if pattern.search(folded)]


def _term_rule(options: SubmissionOptions) -> str:
    if options.identity_term_matching == "word":
        return "case-insensitive whole-word terms bounded by non-alphanumeric characters"
    return "case-insensitive literal substrings"


def _line_channel(value: str, line: int, comment_lines: set[int]) -> str:
    if re.search(r"https?://", value):
        return "url"
    return "comment" if line in comment_lines else "text"


@dataclass(slots=True)
class _Matches:
    """Matches of one scan rule; scans stop adding once the reported limit is reached."""

    items: list[dict[str, object]] = field(default_factory=list)

    @property
    def room(self) -> int:
        return MAX_SCAN_MATCHES - len(self.items)

    @property
    def has_room(self) -> bool:
        return self.room > 0

    def extend_until_full(self, items: Iterable[dict[str, object]]) -> None:
        """Add items until the limit is reached; lazy ``items`` are not read further."""
        for item in items:
            self.items.append(item)
            if not self.has_room:
                break


@dataclass(slots=True)
class _ContentRule:
    """One filename and text-content scan rule of ``check_submission``."""

    rule: str
    enabled: bool
    _: KW_ONLY
    advisory: bool
    # Whether an unclosed TeX literal region leaves this rule's scan incomplete.
    lexical: bool
    matches: _Matches = field(default_factory=_Matches)


class _ContentScan:
    """One pass over every supplied file shared by the identity, secret and note scans."""

    def __init__(self, options: SubmissionOptions, inventory_issues: list[str]) -> None:
        self.options = options
        self.issues = list(inventory_issues)
        self.lexical_issues: list[str] = []
        self.budget = ByteBudget()
        # Values to redact from every finding, including those of other rules.
        self.secret_values: set[str] = set()
        self.identity_terms = _ContentRule(
            "submission.identity_terms", bool(options.identity_terms), advisory=False, lexical=False
        )
        self.identity_hints = _ContentRule(
            "submission.identity_hints", options.scan_identity_hints, advisory=True, lexical=True
        )
        self.secrets = _ContentRule(
            "submission.secrets", options.scan_secrets, advisory=True, lexical=False
        )
        self.private_comments = _ContentRule(
            "submission.private_comments",
            options.scan_private_comments,
            advisory=True,
            lexical=True,
        )
        self.shell_escape = _ContentRule(
            "submission.shell_escape", options.check_shell_escape, advisory=True, lexical=True
        )
        self.rules = (
            self.identity_terms,
            self.identity_hints,
            self.secrets,
            self.private_comments,
            self.shell_escape,
        )

    def scan(self, name: str, path: Path) -> None:
        if self.identity_terms.enabled and self.identity_terms.matches.has_room:
            # Every term in one filename is kept, so the list may pass the limit slightly.
            self.identity_terms.matches.items.extend(
                {"path": name, "channel": "filename", "term_index": index}
                for index in _term_matches(name, self.options)
            )
        self._filename_secrets(name)
        if not any(rule.enabled for rule in self.rules):
            return
        if path.suffix.lower() not in _TEXT_EXTENSIONS and path.name != ".env":
            return
        try:
            text = self.budget.read(path).decode("utf-8-sig")
        except (OSError, UnicodeDecodeError):
            self.issues.append(
                "Text file was unreadable, non-UTF-8 or over the scan budget: "
                + redact_secrets(name)
            )
            return
        self._text_secrets(name, text)
        tex = path.suffix.lower() in _TEX_EXTENSIONS
        comments = tex_comments(text) if tex else []
        if self.identity_terms.enabled and self.identity_terms.matches.has_room:
            comment_lines = {comment.line for comment in comments}
            self.identity_terms.matches.extend_until_full(
                self._identity_lines(name, text, comment_lines)
            )
        notes = self.private_comments.matches
        if self.private_comments.enabled and notes.has_room:
            notes.items.extend(
                {"path": name, "line": marker.line}
                for marker in marked_comments(comments, _private_note, notes.room)
            )
        if tex and any(rule.enabled for rule in self.rules if rule.lexical):
            self._active_source(name, text)

    def _filename_secrets(self, name: str) -> None:
        for _, kind, value in _secrets(name):
            if len(self.secret_values) < MAX_SCAN_MATCHES:
                self.secret_values.add(value)
            if self.secrets.enabled and self.secrets.matches.has_room:
                self.secrets.matches.items.append(
                    {"path": name, "channel": "filename", "kind": kind}
                )

    def _text_secrets(self, name: str, text: str) -> None:
        secrets = _secrets(text) if len(self.secret_values) < MAX_SCAN_MATCHES else []
        self.secret_values.update(
            value for _, _, value in secrets[: MAX_SCAN_MATCHES - len(self.secret_values)]
        )
        if self.secrets.enabled and self.secrets.matches.has_room:
            self.secrets.matches.extend_until_full(
                {"path": name, "line": text.count("\n", 0, offset) + 1, "kind": kind}
                for offset, kind, _ in secrets
            )

    def _identity_lines(
        self, name: str, text: str, comment_lines: set[int]
    ) -> Iterable[dict[str, object]]:
        for line, value in enumerate(text.splitlines(), 1):
            if line % 256 == 0:
                cancellation_point()
            for index in _term_matches(value, self.options):
                yield {
                    "path": name,
                    "line": line,
                    "channel": _line_channel(value, line, comment_lines),
                    "term_index": index,
                }

    def _active_source(self, name: str, text: str) -> None:
        """Hints and shell-escape preflight read active TeX outside literal regions."""
        regions, unclosed = source_regions(text)
        masked = blank_regions(text, regions)
        if unclosed:
            self.lexical_issues.append(
                "A TeX literal region is unclosed; active-source scan is incomplete."
            )
        for rule, pattern in ((self.identity_hints, _HINT), (self.shell_escape, _SHELL)):
            if rule.enabled and rule.matches.has_room:
                rule.matches.extend_until_full(
                    {"path": name, "line": masked.count("\n", 0, match.start()) + 1}
                    for match in pattern.finditer(masked)
                )

    def findings(self) -> list[Finding]:
        scope = (
            "All supplied filenames and supported UTF-8 text files, "
            "including other document roots; "
            "PDF contents require the separate extracted-text/metadata scan. "
            f"Identity terms are Unicode-normalized, {_term_rule(self.options)}; "
            "hints and shell preflight use active TeX; "
            "private-note checks use TeX comments."
        )
        return [
            _scan_finding(
                rule.rule,
                rule.matches.items,
                self.issues + (self.lexical_issues if rule.lexical else []),
                scope,
                advisory=rule.advisory,
                details={"reported_match_limit": MAX_SCAN_MATCHES, "bytes_read": self.budget.used},
            )
            for rule in self.rules
            if rule.enabled
        ]


def _long_file_names(limit: int) -> _Describe:
    def describe(matches: Sequence[Mapping[str, object]]) -> str:
        names = [
            f"{_short(str(match['path']))} ({len(Path(str(match['path'])).name)} characters)"
            for match in matches
        ]
        return (
            f"File names longer than {limit} characters ({_match_count(matches)}): {_listed(names)}"
        )

    return describe


def _disallowed_characters(allowed: set[str]) -> _Describe:
    def describe(matches: Sequence[Mapping[str, object]]) -> str:
        names = []
        for match in matches:
            path = str(match["path"])
            unexpected = sorted(set(Path(path).name) - allowed)
            shown = ", ".join(map(repr, unexpected[:_MAX_LISTED]))
            names.append(
                f"{_short(path)} (not allowed: {shown}{more_suffix(len(unexpected) - _MAX_LISTED)})"
            )
        return (
            f"File names with characters outside the allowed set ({_match_count(matches)}): "
            f"{_listed(names)}"
        )

    return describe


def _other_extensions(matches: Sequence[Mapping[str, object]]) -> str:
    names = []
    for match in matches:
        path = str(match["path"])
        names.append(f"{_short(path)} ({Path(path).suffix.lower() or 'no extension'})")
    return f"Files with extensions that are not allowed ({_match_count(matches)}): {_listed(names)}"


def _filename_findings(
    files: Mapping[str, Path], issues: list[str], options: SubmissionOptions
) -> list[Finding]:
    scope = "All supplied regular-file basenames."
    findings = []
    if options.filename_max_length is not None:
        limit = options.filename_max_length
        findings.append(
            _scan_finding(
                "submission.filename_length",
                [{"path": name} for name in files if len(Path(name).name) > limit],
                issues,
                scope,
                describe=_long_file_names(limit),
            )
        )
    if options.filename_allowed_characters is not None:
        allowed = set(options.filename_allowed_characters)
        findings.append(
            _scan_finding(
                "submission.filename_characters",
                [{"path": name} for name in files if set(Path(name).name) - allowed],
                issues,
                scope,
                describe=_disallowed_characters(allowed),
            )
        )
    if options.allowed_extensions:
        allowed_extensions = {extension.lower() for extension in options.allowed_extensions}
        findings.append(
            _scan_finding(
                "submission.file_extensions",
                [
                    {"path": name}
                    for name in files
                    if Path(name).suffix.lower() not in allowed_extensions
                ],
                issues,
                scope,
                describe=_other_extensions,
            )
        )
    return findings


def check_submission(
    root: Path,
    main: str | None = None,
    options: SubmissionOptions = _DEFAULT_OPTIONS,
) -> list[Finding]:
    """Inspect local files without modifying or excluding any bundle contents."""
    if not _inspects_bundle(options):
        return []
    files, inventory_issues = inventory_files(root)
    findings = _filename_findings(files, inventory_issues, options)
    content = _ContentScan(options, inventory_issues)
    for name, path in files.items():
        cancellation_point()
        content.scan(name, path)
    findings.extend(content.findings())
    if options.required_deliverables:
        findings.append(_deliverables(files, inventory_issues, options))
    if options.template_references:
        findings.extend(_templates(files, inventory_issues, options))
    if options.report_unused_assets:
        findings.append(_unused(root, main, files, inventory_issues))
    return redact_findings(findings, content.secret_values)


# What is wrong with a present deliverable whose content does not match its declared kind.
_DELIVERABLE_PROBLEMS = {
    "pdf": "does not start like a PDF file",
    "zip": "does not start like a ZIP archive",
    "text": "is empty or is not UTF-8 text",
}


def _unmet_deliverables(matches: Sequence[Mapping[str, object]]) -> str:
    problems = []
    for match in matches:
        path = _short(str(match["path"]))
        if match.get("reason") == "missing regular file":
            problems.append(f"{path} is missing")
        else:
            problems.append(
                f"{path} {_DELIVERABLE_PROBLEMS.get(str(match.get('kind')), 'does not match')}"
            )
    return f"Required deliverables not satisfied ({_match_count(matches)}): {_listed(problems)}"


def _deliverables(files: dict[str, Path], issues: list[str], options: SubmissionOptions) -> Finding:
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
            data = budget.read_prefix(path, 8) if kind in {"pdf", "zip"} else budget.read(path)
        except OSError:
            incomplete.append(
                f"Required deliverable could not be inspected: {redact_secrets(name)}"
            )
            continue
        valid = (kind == "pdf" and data.startswith(b"%PDF-")) or (
            kind == "zip" and data.startswith((b"PK\x03\x04", b"PK\x05\x06"))
        )
        if kind == "text":
            try:
                valid = bool(data) and "\x00" not in data.decode("utf-8-sig")
            except UnicodeDecodeError:
                valid = False
        if not valid:
            matches.append({"path": name, "kind": kind, "reason": "format evidence does not match"})
    return _scan_finding(
        "submission.deliverables",
        matches,
        incomplete,
        "Explicit project-relative paths; PDF/ZIP signatures and bounded UTF-8 text only, "
        "not format validation or verification of separately transmitted deliverables.",
        describe=_unmet_deliverables,
    )


def _templates(
    files: dict[str, Path], issues: list[str], options: SubmissionOptions
) -> list[Finding]:
    results = []
    budget = ByteBudget()
    for name, reference in options.template_references:
        cancellation_point()
        incomplete = list(issues)
        matches = []
        details: dict[str, object] = {"project_path": name, "reference_path": reference}
        if name not in files:
            if not issues:
                matches.append({"path": name, "reason": "missing project template"})
        else:
            try:
                actual = budget.read(files[name])
                expected = budget.read(Path(reference).expanduser())
                details["byte_equal"] = actual == expected
                versions = []
                for raw in (actual, expected):
                    try:
                        match = _VERSION.search(mask_literals(raw.decode("utf-8-sig")))
                        versions.append(match.groups() if match else None)
                    except UnicodeDecodeError:
                        versions.append(None)
                details["declared_version_comparison"] = (
                    ("equal" if versions[0] == versions[1] else "different")
                    if all(versions)
                    else "unavailable"
                )
                if actual != expected:
                    matches.append({"path": name, "reason": "bytes differ from explicit reference"})
            except (OSError, RuntimeError):
                incomplete.append(
                    "Project template or supplied reference is unreadable, linked "
                    "or over the byte limit."
                )
        result = _scan_finding(
            "submission.template_reference",
            matches,
            incomplete,
            "Explicit local author-provided reference pairs; equal bytes establish equality only. "
            "Version comparisons use supported literal Provides declarations and do not infer age, "
            "official status or semantic compatibility.",
            details=details,
        )
        results.append(result)
    return results


def _unused_assets(matches: Sequence[Mapping[str, object]]) -> str:
    names = [_short(str(match["path"])) for match in matches]
    return (
        f"Asset files not referenced by the selected document ({_match_count(matches)}): "
        f"{_listed(names)}"
    )


def _unused(root: Path, main: str | None, files: dict[str, Path], issues: list[str]) -> Finding:
    incomplete = list(issues)
    candidates = []
    if not incomplete:
        try:
            analysis = analyze_sources(root, main)
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
    result = _scan_finding(
        "submission.unused_assets",
        candidates,
        incomplete,
        "Supported asset extensions outside one selected literal source/dependency graph; "
        "other document roots, generated/dynamic dependencies "
        "and toolchain inputs may need these files. "
        "No candidate is safe to delete based on this scan.",
        advisory=True,
        describe=_unused_assets,
    )
    return replace(result, status="inconclusive") if incomplete else result


def check_submission_archive(
    archive: Path,
    options: SubmissionOptions = _DEFAULT_OPTIONS,
) -> list[Finding]:
    """Check the actual compressed archive length independently of import limits."""
    if options.max_archive_bytes is None:
        return []
    cancellation_point()
    issues = []
    matches = []
    details: dict[str, object] = {"maximum_bytes": options.max_archive_bytes}
    try:
        metadata = archive.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError("Archive is not a regular file.")
        details["compressed_bytes"] = metadata.st_size
        if metadata.st_size > options.max_archive_bytes:
            matches.append({"reason": "compressed archive exceeds configured byte limit"})
    except OSError:
        issues.append("The final archive is missing or is not an accessible regular file.")
    limit = options.max_archive_bytes
    return [
        _scan_finding(
            "submission.archive_size",
            matches,
            issues,
            "Actual compressed archive bytes.",
            details=details,
            describe=lambda _: (
                f"The compressed archive is {details['compressed_bytes']:,} bytes, "
                f"over the configured limit of {limit:,} bytes"
            ),
        )
    ]


def check_pdf_identity(
    text: str,
    metadata: dict,
    options: SubmissionOptions = _DEFAULT_OPTIONS,
) -> list[Finding]:
    """Scan supplied extracted evidence; empty text/metadata mark extraction incomplete."""
    if not options.identity_terms and not options.scan_secrets:
        return []
    cancellation_point()
    issues = []
    values = []
    evidence_bytes = 0
    if not isinstance(text, str) or not text.strip():
        issues.append("Extracted PDF text is unavailable or empty.")
    elif len(text.encode("utf-8")) > MAX_TOTAL_SOURCE_BYTES:
        issues.append("Extracted PDF text exceeds the scan limit.")
    else:
        values.append(("text", text))
        evidence_bytes = len(text.encode("utf-8"))
    if not isinstance(metadata, dict) or not metadata:
        issues.append("PDF metadata evidence is unavailable or empty.")
    else:
        for index, (key, value) in enumerate(metadata.items(), 1):
            cancellation_point()
            if index > MAX_SCAN_ENTRIES:
                issues.append("PDF metadata exceeds the entry limit.")
                break
            if (
                not isinstance(key, str)
                or len(key) > MAX_SOURCE_BYTES
                or not isinstance(value, (str, int, float, bool))
                or len(str(value)) > MAX_SOURCE_BYTES
            ):
                issues.append("A PDF metadata value could not be scanned within the text budget.")
                continue
            joined = key + " " + str(value)
            evidence_bytes += len(joined.encode("utf-8"))
            if evidence_bytes > MAX_TOTAL_SOURCE_BYTES:
                issues.append("Combined PDF evidence exceeds the scan budget.")
                break
            values.append(("metadata", joined))
    identity_matches = []
    secret_matches = []
    secret_values = set()
    for channel, value in values:
        cancellation_point()
        if len(identity_matches) < MAX_SCAN_MATCHES:
            for index in _term_matches(value, options):
                identity_matches.append({"channel": channel, "term_index": index})
        if len(secret_matches) < MAX_SCAN_MATCHES:
            for _, kind, candidate in _secrets(value):
                secret_values.add(candidate)
                secret_matches.append({"channel": channel, "kind": kind})
    results = []
    if options.identity_terms:
        results.append(
            _scan_finding(
                "submission.pdf_identity_terms",
                identity_matches,
                issues,
                "Supplied extracted PDF text and metadata; Unicode-normalized "
                f"{_term_rule(options)} only.",
            )
        )
    if options.scan_secrets:
        results.append(
            _scan_finding(
                "submission.secrets",
                secret_matches,
                issues,
                "Supplied extracted PDF text and metadata; values are redacted.",
                advisory=True,
            )
        )
    return redact_findings(results, secret_values)
