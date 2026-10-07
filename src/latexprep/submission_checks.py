"""Bounded, local submission-policy checks; never edit sources or execute TeX.

Identity checks are literal whole-word (or, on request, substring) scans, not an
anonymity guarantee. Privacy checks cover the whole supplied bundle; unused-asset
hints alone use one selected literal dependency graph. No publisher policy or
template registry is embedded.
"""

from __future__ import annotations

import os
import re
import stat
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import cast
from urllib.parse import unquote

from .models import Finding, PreparationError
from .redaction import SECRET_PATTERNS as _SECRET_PATTERNS
from .scheduler import cancellation_point
from .source import (
    _LITERAL_ENVS,
    _TOKEN,
    MAX_SOURCE_BYTES,
    MAX_TOTAL_SOURCE_BYTES,
    _group,
    _mask,
    _skip_trivia,
    analyze_sources,
    mask_literals,
)

IDENTITY_TERM_MATCHING = ("word", "substring")
MAX_SCAN_ENTRIES = 10_000
MAX_SCAN_MATCHES = 200
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

    def __post_init__(self) -> None:
        if self.identity_term_matching not in IDENTITY_TERM_MATCHING:
            raise PreparationError("identity_term_matching must be 'word' or 'substring'.")
        for name in (
            "scan_identity_hints",
            "scan_secrets",
            "scan_private_comments",
            "check_shell_escape",
            "report_unused_assets",
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
                if not _relative_path(path) or path in seen:
                    raise PreparationError(f"{name} requires unique safe relative project paths.")
                seen.add(path)
                if name == "required_deliverables" and value not in {"file", "pdf", "zip", "text"}:
                    raise PreparationError("Deliverable kind must be file, pdf, zip or text.")


def _relative_path(value: str) -> bool:
    path = PurePosixPath(value)
    return (
        not path.is_absolute()
        and ".." not in path.parts
        and "\\" not in value
        and path.as_posix() == value
        and value not in {"", "."}
        and "\x00" not in value
    )


_DEFAULT_OPTIONS = SubmissionOptions()


def _fold(value: str) -> str:
    return unicodedata.normalize("NFKC", unquote(value)).casefold()


def _secrets(text: str) -> list[tuple[int, str, str]]:
    result = []
    for kind, pattern in _SECRET_PATTERNS:
        cancellation_point()
        for match in pattern.finditer(text):
            value = match.groupdict().get("secret") or match.group()
            if value.lower() in _PLACEHOLDERS:
                continue
            result.append((match.start(), kind, value))
            if len(result) >= MAX_SCAN_MATCHES:
                return result
    return result


def _redact(value: str) -> str:
    for _, pattern in _SECRET_PATTERNS:
        value = pattern.sub("[REDACTED]", value)
    return value


def _redacted(findings: list[Finding], values: set[str]) -> list[Finding]:
    ordered_values = sorted(values, key=len, reverse=True)

    def clean(value: object) -> object:
        if isinstance(value, str):
            cancellation_point()
            for secret in ordered_values:
                value = value.replace(secret, "[REDACTED]")
            return _redact(value)
        if isinstance(value, list):
            return [clean(item) for item in value]
        if isinstance(value, dict):
            return {str(clean(key)): clean(item) for key, item in value.items()}
        return value

    return [
        replace(
            item,
            path=str(clean(item.path)) if item.path else None,
            message=str(clean(item.message)),
            suggestion=str(clean(item.suggestion)) if item.suggestion else None,
            details=cast(dict[str, object], clean(item.details)),
        )
        for item in findings
    ]


def _inventory(root: Path) -> tuple[dict[str, Path], list[str]]:
    files: dict[str, Path] = {}
    issues: list[str] = []
    if not root.is_dir() or root.is_symlink():
        return files, ["The supplied project is not an accessible regular directory."]
    pending = [root]
    entries = 0
    while pending:
        cancellation_point()
        directory = pending.pop()
        try:
            with os.scandir(directory) as listing:
                for entry in listing:
                    entries += 1
                    if entries > MAX_SCAN_ENTRIES:
                        return files, [*issues, "Project inventory exceeded the entry limit."]
                    cancellation_point()
                    path = Path(entry.path)
                    name = path.relative_to(root).as_posix()
                    if entry.is_symlink():
                        issues.append(f"Linked entry was not read: {_redact(name)}")
                    elif entry.is_dir(follow_symlinks=False):
                        if entry.name not in {".git", ".hg", ".svn", "__MACOSX"}:
                            pending.append(path)
                    elif entry.is_file(follow_symlinks=False):
                        files[name] = path
                    else:
                        issues.append(f"Special entry was not read: {_redact(name)}")
        except OSError:
            issues.append("A project directory could not be inventoried.")
    return dict(sorted(files.items())), issues


def _read(path: Path, limit: int = MAX_SOURCE_BYTES, *, prefix: bool = False) -> bytes:
    cancellation_point()
    if path.is_symlink():
        raise OSError("Linked files are outside the scan.")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or (not prefix and metadata.st_size > limit):
            raise OSError("File is not regular or exceeds the scan limit.")
        if prefix:
            return stream.read(limit)
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise OSError("File exceeds the scan limit.")
        return data


def _match_label(match: Mapping[str, object]) -> str:
    # Matches hold only locations and generated reasons, never matched text or terms.
    path, line, reason = match.get("path"), match.get("line"), match.get("reason")
    label = path if isinstance(path, str) else match.get("channel")
    label = label if isinstance(label, str) else ""
    if label and type(line) is int:
        label += f":{line}"
    if len(label) > 120:  # Over-long file names are the finding itself; details keep them whole.
        label = label[:119] + "…"
    if isinstance(reason, str):
        label = f"{label} ({reason})" if label else reason
    return label or "unspecified location"


def _message(matches: Sequence[Mapping[str, object]], issues: list[str]) -> str:
    if not matches:
        if not issues:
            return "No matches in the stated scope."
        more = f" (+{len(issues) - 1} more)" if len(issues) > 1 else ""
        return f"The configured scan is incomplete: {issues[0]}{more}"
    labels = [_match_label(match) for match in matches]
    shown = list(dict.fromkeys(labels))[:3]
    # Scans stop collecting at the limit, so a full list means "at least" this many.
    count = f"{len(matches)}{'+' if len(matches) == MAX_SCAN_MATCHES else ''}"
    message = f"{count} match{'es' if len(matches) != 1 else ''}: {', '.join(shown)}"
    if more := sum(label not in shown for label in labels):
        message += f" (+{more} more)"
    if issues:
        message += f"; the scan was also incomplete: {issues[0]}"
    return message


def _result(
    rule: str,
    matches: Sequence[Mapping[str, object]],
    issues: list[str],
    scope: str,
    *,
    advisory: bool = False,
    details: dict[str, object] | None = None,
) -> Finding:
    status = "failed" if matches else "inconclusive" if issues else "passed"
    return Finding(
        rule,
        _message(matches, issues),
        severity="info" if status == "passed" else "warning" if advisory else "error",
        status=status,
        evidence="heuristic" if advisory else "direct",
        details={
            "scope": scope,
            "matches": [dict(item) for item in matches[:MAX_SCAN_MATCHES]],
            "match_output_limited": len(matches) >= MAX_SCAN_MATCHES,
            "issues": issues[:MAX_SCAN_MATCHES],
            **(details or {}),
        },
    )


def _comments(text: str) -> list[tuple[int, str]]:
    """Extract TeX comments, skipping the source scanner's common literal regions."""
    result = []
    pos = 0
    previous = 0
    number = 1
    while pos < len(text):
        number += text.count("\n", previous, pos)
        previous = pos
        if pos % 4096 == 0:
            cancellation_point()
        if text[pos] == "%":
            end = text.find("\n", pos)
            end = len(text) if end < 0 else end
            result.append((number, text[pos + 1 : end]))
            pos = end
            continue
        token = _TOKEN.match(text, pos) if text[pos] == "\\" else None
        if token is None:
            pos += 1
            continue
        name, end = token.group(1), token.end()
        if name == "begin":
            arg = _group(text, _skip_trivia(text, end))
            if arg and arg.value.strip() in _LITERAL_ENVS:
                stop = re.search(
                    r"\\end\s*\{\s*" + re.escape(arg.value.strip()) + r"\s*\}", text[arg.end + 1 :]
                )
                pos = arg.end + 1 + stop.end() if stop else len(text)
                continue
        if name in {"verb", "lstinline", "mintinline", "url", "nolinkurl", "path"}:
            cursor = end + int(end < len(text) and text[end] == "*")
            if name != "verb":
                cursor = _skip_trivia(text, cursor)
                option = _group(text, cursor, "[", "]")
                if option:
                    cursor = _skip_trivia(text, option.end + 1)
                if name == "mintinline":
                    language = _group(text, cursor)
                    if language:
                        cursor = _skip_trivia(text, language.end + 1)
            if cursor < len(text) and not text[cursor].isspace():
                if text[cursor] == "{" and name != "verb":
                    arg = _group(text, cursor)
                    end = arg.end + 1 if arg else len(text)
                else:
                    line_end = text.find("\n", cursor)
                    line_end = len(text) if line_end < 0 else line_end
                    close = text.find(text[cursor], cursor + 1, line_end)
                    end = close + 1 if close >= 0 else line_end
        pos = end
    return result


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
    return (
        "case-insensitive whole-word terms bounded by non-alphanumeric characters"
        if options.identity_term_matching == "word"
        else "case-insensitive literal substrings"
    )


def check_submission(
    root: Path,
    main: str | None = None,
    options: SubmissionOptions = _DEFAULT_OPTIONS,
) -> list[Finding]:
    """Inspect local files without modifying or excluding any bundle contents."""
    enabled = any(
        (
            options.identity_terms,
            options.scan_identity_hints,
            options.scan_secrets,
            options.scan_private_comments,
            options.check_shell_escape,
            options.filename_max_length,
            options.filename_allowed_characters,
            options.allowed_extensions,
            options.required_deliverables,
            options.report_unused_assets,
            options.template_references,
        )
    )
    if not enabled:
        return []
    files, inventory_issues = _inventory(root)
    findings: list[Finding] = []
    secret_values: set[str] = set()
    for rule, active, predicate in (
        (
            "submission.filename_length",
            options.filename_max_length is not None,
            lambda name: len(Path(name).name) > (options.filename_max_length or 0),
        ),
        (
            "submission.filename_characters",
            options.filename_allowed_characters is not None,
            lambda name: bool(
                set(Path(name).name) - set(options.filename_allowed_characters or "")
            ),
        ),
        (
            "submission.file_extensions",
            bool(options.allowed_extensions),
            lambda name: (
                Path(name).suffix.lower() not in {ext.lower() for ext in options.allowed_extensions}
            ),
        ),
    ):
        if active:
            matches = [{"path": name} for name in files if predicate(name)]
            findings.append(
                _result(rule, matches, inventory_issues, "All supplied regular-file basenames.")
            )
    scans = {
        "submission.identity_terms": (bool(options.identity_terms), False),
        "submission.identity_hints": (options.scan_identity_hints, True),
        "submission.secrets": (options.scan_secrets, True),
        "submission.private_comments": (options.scan_private_comments, True),
        "submission.shell_escape": (options.check_shell_escape, True),
    }
    matches: dict[str, list[dict[str, object]]] = {name: [] for name in scans}
    scan_issues = list(inventory_issues)
    lexical_issues: list[str] = []
    total = 0
    for name, path in files.items():
        cancellation_point()
        if options.identity_terms and len(matches["submission.identity_terms"]) < MAX_SCAN_MATCHES:
            for index in _term_matches(name, options):
                matches["submission.identity_terms"].append(
                    {"path": name, "channel": "filename", "term_index": index}
                )
        for _, kind, value in _secrets(name):
            if len(secret_values) < MAX_SCAN_MATCHES:
                secret_values.add(value)
            if options.scan_secrets and len(matches["submission.secrets"]) < MAX_SCAN_MATCHES:
                matches["submission.secrets"].append(
                    {"path": name, "channel": "filename", "kind": kind}
                )
        if not any(active for active, _ in scans.values()):
            continue
        if path.suffix.lower() not in _TEXT_EXTENSIONS and path.name != ".env":
            continue
        try:
            raw = _read(path, min(MAX_SOURCE_BYTES, max(0, MAX_TOTAL_SOURCE_BYTES - total)))
            total += len(raw)
            text = raw.decode("utf-8-sig")
        except (OSError, UnicodeDecodeError):
            scan_issues.append(
                f"Text file was unreadable, non-UTF-8 or over the scan budget: {_redact(name)}"
            )
            continue
        secrets = _secrets(text) if len(secret_values) < MAX_SCAN_MATCHES else []
        secret_values.update(
            value for _, _, value in secrets[: MAX_SCAN_MATCHES - len(secret_values)]
        )
        if options.scan_secrets and len(matches["submission.secrets"]) < MAX_SCAN_MATCHES:
            for offset, kind, _ in secrets:
                matches["submission.secrets"].append(
                    {"path": name, "line": text.count("\n", 0, offset) + 1, "kind": kind}
                )
                if len(matches["submission.secrets"]) >= MAX_SCAN_MATCHES:
                    break
        comments = _comments(text) if path.suffix.lower() in _TEX_EXTENSIONS else []
        comment_lines = {line for line, _ in comments}
        if options.identity_terms and len(matches["submission.identity_terms"]) < MAX_SCAN_MATCHES:
            for line, value in enumerate(text.splitlines(), 1):
                if line % 256 == 0:
                    cancellation_point()
                for index in _term_matches(value, options):
                    channel = (
                        "url"
                        if re.search(r"https?://", value)
                        else "comment"
                        if line in comment_lines
                        else "text"
                    )
                    matches["submission.identity_terms"].append(
                        {"path": name, "line": line, "channel": channel, "term_index": index}
                    )
                    if len(matches["submission.identity_terms"]) >= MAX_SCAN_MATCHES:
                        break
                if len(matches["submission.identity_terms"]) >= MAX_SCAN_MATCHES:
                    break
        if (
            options.scan_private_comments
            and len(matches["submission.private_comments"]) < MAX_SCAN_MATCHES
        ):
            for line, value in comments:
                if _NOTE.search(value):
                    matches["submission.private_comments"].append({"path": name, "line": line})
                if len(matches["submission.private_comments"]) >= MAX_SCAN_MATCHES:
                    break
        if path.suffix.lower() in _TEX_EXTENSIONS and (
            options.scan_identity_hints
            or options.check_shell_escape
            or options.scan_private_comments
        ):
            masked, incomplete = _mask(text)
            if incomplete:
                lexical_issues.append(
                    "A TeX literal region is unclosed; active-source scan is incomplete."
                )
            for rule, pattern in (
                ("submission.identity_hints", _HINT),
                ("submission.shell_escape", _SHELL),
            ):
                if scans[rule][0] and len(matches[rule]) < MAX_SCAN_MATCHES:
                    for match in pattern.finditer(masked):
                        matches[rule].append(
                            {"path": name, "line": masked.count("\n", 0, match.start()) + 1}
                        )
                        if len(matches[rule]) >= MAX_SCAN_MATCHES:
                            break
    for rule, (active, advisory) in scans.items():
        if active:
            findings.append(
                _result(
                    rule,
                    matches[rule],
                    scan_issues
                    + (lexical_issues if advisory and rule != "submission.secrets" else []),
                    "All supplied filenames and supported UTF-8 text files, "
                    "including other document roots; "
                    "PDF contents require the separate extracted-text/metadata scan. "
                    f"Identity terms are Unicode-normalized, {_term_rule(options)}; "
                    "hints and shell preflight use active TeX; "
                    "private-note checks use TeX comments.",
                    advisory=advisory,
                    details={"reported_match_limit": MAX_SCAN_MATCHES, "bytes_read": total},
                )
            )
    if options.required_deliverables:
        findings.append(_deliverables(files, inventory_issues, options))
    if options.template_references:
        findings.extend(_templates(files, inventory_issues, options))
    if options.report_unused_assets:
        findings.append(_unused(root, main, files, inventory_issues))
    return _redacted(findings, secret_values)


def _deliverables(files: dict[str, Path], issues: list[str], options: SubmissionOptions) -> Finding:
    matches = []
    incomplete = list(issues)
    total = 0
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
            data = (
                _read(path, 8, prefix=True)
                if kind in {"pdf", "zip"}
                else _read(path, min(MAX_SOURCE_BYTES, max(0, MAX_TOTAL_SOURCE_BYTES - total)))
            )
            total += len(data)
        except OSError:
            incomplete.append(f"Required deliverable could not be inspected: {_redact(name)}")
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
    return _result(
        "submission.deliverables",
        matches,
        incomplete,
        "Explicit project-relative paths; PDF/ZIP signatures and bounded UTF-8 text only, "
        "not format validation or verification of separately transmitted deliverables.",
    )


def _templates(
    files: dict[str, Path], issues: list[str], options: SubmissionOptions
) -> list[Finding]:
    results = []
    total = 0
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
                actual = _read(
                    files[name], min(MAX_SOURCE_BYTES, max(0, MAX_TOTAL_SOURCE_BYTES - total))
                )
                total += len(actual)
                expected = _read(
                    Path(reference).expanduser(),
                    min(MAX_SOURCE_BYTES, max(0, MAX_TOTAL_SOURCE_BYTES - total)),
                )
                total += len(expected)
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
        result = _result(
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
    result = _result(
        "submission.unused_assets",
        candidates,
        incomplete,
        "Supported asset extensions outside one selected literal source/dependency graph; "
        "other document roots, generated/dynamic dependencies "
        "and toolchain inputs may need these files. "
        "No candidate is safe to delete based on this scan.",
        advisory=True,
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
    return [
        _result(
            "submission.archive_size",
            matches,
            issues,
            "Actual compressed archive bytes.",
            details=details,
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
            _result(
                "submission.pdf_identity_terms",
                identity_matches,
                issues,
                "Supplied extracted PDF text and metadata; Unicode-normalized "
                f"{_term_rule(options)} only.",
            )
        )
    if options.scan_secrets:
        results.append(
            _result(
                "submission.secrets",
                secret_matches,
                issues,
                "Supplied extracted PDF text and metadata; values are redacted.",
                advisory=True,
            )
        )
    return _redacted(results, secret_values)
