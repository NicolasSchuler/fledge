"""The single pass over every supplied file that finds identity terms, secrets and notes.

``ContentScan`` reads each file of a submission bundle once and feeds five rules:
configured identity terms (in file names, text, comments and URLs), identity hints,
credential-shaped secrets, private or unfinished comments, and shell-escape
constructs. It also collects every secret it sees so that ``redact_findings`` can
remove those values from the findings of all rules. The helpers for term matching
and secret detection are public because the PDF identity check applies the same
matching to extracted text.

This module reports through ``submission_scan_findings`` and owns neither the
options (``submission_options``) nor the file-name, deliverable and archive policies
(``submission_checks``).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import KW_ONLY, dataclass, field
from functools import lru_cache
from itertools import islice
from pathlib import Path
from typing import NamedTuple
from urllib.parse import unquote

from .cancellation import cancellation_point
from .comment_markers import MarkerCategory, marker_pattern
from .file_scan import ByteBudget
from .findings import Finding
from .redaction import SECRET_PATTERNS, redact_secrets
from .submission_options import SubmissionOptions
from .submission_scan_findings import ScanLimits, ScanRule, scan_finding
from .tex_commands import TEX_SOURCE_SUFFIXES
from .tex_comments import tex_comments
from .tex_lexer import blank_regions, source_regions

# File types read as UTF-8 text; every other file is matched by name only.
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
_TEX_EXTENSIONS = TEX_SOURCE_SUFFIXES | {".dtx"}
_TEX_EXTENSIONS = TEX_SOURCE_SUFFIXES | {".dtx"}
# Example credentials shipped in templates and documentation; they are not secrets.
_PLACEHOLDERS = {"your_api_key_here", "your_password_here", "example_password", "changemechangeme"}
_HINT = re.compile(
    r"(?i)\\(?:acknowledg(?:e)?ments?|thanks)\b|"
    r"\\(?:section\*?|begin)\s*\{acknowledg(?:e)?ments?\}|"
    r"\b(?:at our (?:university|institution|laboratory)|in our previous work|"
    r"our (?:institution|laboratory|university)|the authors are (?:at|from))\b"
)
# Unfinished-work markers and private-note or distribution-restriction phrases; review
# correspondence is left to the default private-note advisory.
_PRIVATE_COMMENT_MARKER = marker_pattern(
    MarkerCategory.EDIT_MARKER,
    MarkerCategory.UNCERTAINTY_MARK,
    MarkerCategory.PRIVATE_NOTE,
    MarkerCategory.DISTRIBUTION_RESTRICTION,
)
_SHELL = re.compile(
    r"\\write\s*18\b|\\(?:ShellEscape|ShellEscapeAsync)\b|\\sys_shell_now:[A-Za-z]+\b"
)
# Lines scanned between two cancellation checks.
_CANCELLATION_STRIDE_LINES = 256


class SecretMatch(NamedTuple):
    """A credential-shaped value found in text: where it starts, its kind and the value."""

    offset: int
    kind: str
    value: str


def find_secrets(text: str, limit: int) -> list[SecretMatch]:
    """The first ``limit`` credential-shaped values in ``text``, in the order patterns find them."""
    result: list[SecretMatch] = []
    for kind, pattern in SECRET_PATTERNS:
        cancellation_point()
        for match in pattern.finditer(text):
            value = match.groupdict().get("secret") or match.group()
            if value.lower() in _PLACEHOLDERS:
                continue
            result.append(SecretMatch(match.start(), kind, value))
            if len(result) >= limit:
                return result
    return result


def _fold(value: str) -> str:
    return unicodedata.normalize("NFKC", unquote(value)).casefold()


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


def term_matches(text: str, options: SubmissionOptions) -> list[int]:
    """The 1-based indexes of the identity terms that occur in ``text``."""
    folded = _fold(text)
    patterns = _term_patterns(options.identity_terms, options.identity_term_matching)
    return [index for index, pattern in enumerate(patterns, 1) if pattern.search(folded)]


def term_rule_description(options: SubmissionOptions) -> str:
    """How identity terms are matched, in words for a scope sentence."""
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

    limit: int
    items: list[dict[str, object]] = field(default_factory=list)

    @property
    def room(self) -> int:
        return self.limit - len(self.items)

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
    matches: _Matches
    _: KW_ONLY
    advisory: bool
    # Whether an unclosed TeX literal region leaves this rule's scan incomplete.
    lexical: bool


class ContentScan:
    """One pass over every supplied file shared by the identity, secret and note scans."""

    def __init__(
        self, options: SubmissionOptions, inventory_issues: list[str], limits: ScanLimits
    ) -> None:
        self.options = options
        self.limits = limits
        self.issues = list(inventory_issues)
        self.lexical_issues: list[str] = []
        self.budget = ByteBudget()
        # Values to redact from every finding, including those of other rules.
        self.secret_values: set[str] = set()

        def rule(name: str, enabled: bool, *, advisory: bool, lexical: bool) -> _ContentRule:
            matches = _Matches(limits.max_matches)
            return _ContentRule(name, enabled, matches, advisory=advisory, lexical=lexical)

        self.identity_terms = rule(
            "submission.identity_terms", bool(options.identity_terms), advisory=False, lexical=False
        )
        self.identity_hints = rule(
            "submission.identity_hints", options.scan_identity_hints, advisory=True, lexical=True
        )
        self.secrets = rule(
            "submission.secrets", options.scan_secrets, advisory=True, lexical=False
        )
        self.private_comments = rule(
            "submission.private_comments",
            options.scan_private_comments,
            advisory=True,
            lexical=True,
        )
        self.shell_escape = rule(
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
        """Read one file by name and, when it is a text file, by content."""
        if self.identity_terms.enabled and self.identity_terms.matches.has_room:
            # Every term in one filename is kept, so the list may pass the limit slightly.
            self.identity_terms.matches.items.extend(
                {"path": name, "channel": "filename", "term_index": index}
                for index in term_matches(name, self.options)
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
            marked = (c for c in comments if _PRIVATE_COMMENT_MARKER.search(c.text))
            notes.items.extend(
                {"path": name, "line": comment.line} for comment in islice(marked, notes.room)
            )
        if tex and any(rule.enabled for rule in self.rules if rule.lexical):
            self._active_source(name, text)

    def _filename_secrets(self, name: str) -> None:
        limit = self.limits.max_matches
        for _, kind, value in find_secrets(name, limit):
            if len(self.secret_values) < limit:
                self.secret_values.add(value)
            if self.secrets.enabled and self.secrets.matches.has_room:
                self.secrets.matches.items.append(
                    {"path": name, "channel": "filename", "kind": kind}
                )

    def _text_secrets(self, name: str, text: str) -> None:
        limit = self.limits.max_matches
        secrets = find_secrets(text, limit) if len(self.secret_values) < limit else []
        self.secret_values.update(
            secret.value for secret in secrets[: limit - len(self.secret_values)]
        )
        if self.secrets.enabled and self.secrets.matches.has_room:
            self.secrets.matches.extend_until_full(
                {"path": name, "line": text.count("\n", 0, secret.offset) + 1, "kind": secret.kind}
                for secret in secrets
            )

    def _identity_lines(
        self, name: str, text: str, comment_lines: set[int]
    ) -> Iterable[dict[str, object]]:
        for line, value in enumerate(text.splitlines(), 1):
            if line % _CANCELLATION_STRIDE_LINES == 0:
                cancellation_point()
            for index in term_matches(value, self.options):
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
        """One finding per enabled rule."""
        scope = (
            "All supplied filenames and supported UTF-8 text files, "
            "including other document roots; "
            "PDF contents require the separate extracted-text/metadata scan. "
            f"Identity terms are Unicode-normalized, {term_rule_description(self.options)}; "
            "hints and shell preflight use active TeX; "
            "private-note checks use TeX comments."
        )
        details: dict[str, object] = {
            "reported_match_limit": self.limits.max_matches,
            "bytes_read": self.budget.used,
        }
        return [
            scan_finding(
                ScanRule(rule.rule, scope, advisory=rule.advisory),
                rule.matches.items,
                self.issues + (self.lexical_issues if rule.lexical else []),
                self.limits,
                details=details,
            )
            for rule in self.rules
            if rule.enabled
        ]
