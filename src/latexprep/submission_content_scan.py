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
    """A credential-shaped value found in text.

    Attributes:
        offset: Where the value starts in the text.
        kind: The kind of the value, as ``SECRET_PATTERNS`` names it.
        value: The value itself.
    """

    offset: int
    kind: str
    value: str


def find_secrets(text: str, limit: int) -> list[SecretMatch]:
    """Return the first ``limit`` credential-shaped values in ``text``.

    The values are in the order patterns find them: each pattern of ``SECRET_PATTERNS`` is
    applied in turn, and known example credentials of templates and documentation are
    skipped.

    Args:
        text: The text to search.
        limit: The most values to return.

    Returns:
        The values found, each with its offset and kind. A value is the pattern's ``secret``
        group when it has one, else the whole match.
    """
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
    """Return ``value`` percent-decoded, NFKC-normalized and case-folded for matching."""
    return unicodedata.normalize("NFKC", unquote(value)).casefold()


@lru_cache(maxsize=16)
def _term_patterns(terms: tuple[str, ...], matching: str) -> tuple[re.Pattern[str], ...]:
    """Compile folded terms once; word mode requires non-alphanumeric (or no) neighbours.

    Underscores and punctuation separate words; a term's start or end that is not
    alphanumeric needs no separator.

    Args:
        terms: The identity terms.
        matching: ``"word"`` for whole-word matching; any other value matches substrings.

    Returns:
        One pattern per term, in the order of ``terms``.
    """
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
    """Return the 1-based indexes of the identity terms that occur in ``text``.

    Args:
        text: The text to search; it is folded as the terms are.
        options: The submission options with the identity terms and their matching mode.

    Returns:
        The indexes of the matching terms, in ascending order.
    """
    folded = _fold(text)
    patterns = _term_patterns(options.identity_terms, options.identity_term_matching)
    return [index for index, pattern in enumerate(patterns, 1) if pattern.search(folded)]


def term_rule_description(options: SubmissionOptions) -> str:
    """Return how identity terms are matched, in words for a scope sentence."""
    if options.identity_term_matching == "word":
        return "case-insensitive whole-word terms bounded by non-alphanumeric characters"
    return "case-insensitive literal substrings"


def _line_channel(value: str, line: int, comment_lines: set[int]) -> str:
    """Return the channel of one line: ``"url"``, ``"comment"`` or ``"text"``.

    Args:
        value: The text of the line; one that holds an ``http://`` or ``https://`` URL is a
            ``"url"`` line.
        line: The 1-based line number.
        comment_lines: The numbers of the lines that hold a TeX comment.

    Returns:
        The channel of the line.
    """
    if re.search(r"https?://", value):
        return "url"
    return "comment" if line in comment_lines else "text"


@dataclass(slots=True)
class _Matches:
    """Matches of one scan rule; scans stop adding once the reported limit is reached.

    Attributes:
        limit: The most matches the rule reports.
        items: The matches collected so far.
    """

    limit: int
    items: list[dict[str, object]] = field(default_factory=list)

    @property
    def room(self) -> int:
        """The number of matches that can still be added before the limit."""
        return self.limit - len(self.items)

    @property
    def has_room(self) -> bool:
        """Whether another match can be added."""
        return self.room > 0

    def extend_until_full(self, items: Iterable[dict[str, object]]) -> None:
        """Add items until the limit is reached; lazy ``items`` are not read further."""
        for item in items:
            self.items.append(item)
            if not self.has_room:
                break


@dataclass(slots=True)
class _ContentRule:
    """One filename and text-content scan rule of ``check_submission``.

    Attributes:
        rule: The rule name of the finding.
        enabled: Whether the options select the rule.
        matches: The matches the rule has collected.
        advisory: Whether the finding has the advisory weight.
        lexical: Whether the rule's scan depends on the TeX lexical regions.
    """

    rule: str
    enabled: bool
    matches: _Matches
    _: KW_ONLY
    advisory: bool
    # Whether an unclosed TeX literal region leaves this rule's scan incomplete.
    lexical: bool


class ContentScan:
    """One pass over every supplied file shared by the identity, secret and note scans.

    Args:
        inventory_issues: The reasons the file inventory is incomplete; they start
            ``issues``.

    Attributes:
        options: The submission options that select the rules.
        limits: The limits of the scans.
        issues: The reasons every rule's scan is incomplete, starting with the inventory's.
        lexical_issues: The reasons the scans of the lexical rules are incomplete.
        budget: The byte budget that every text read counts against.
        secret_values: The distinct secret values seen, up to ``limits.max_matches``.
        identity_terms: The configured identity-term rule.
        identity_hints: The identity-hint rule.
        secrets: The secret rule.
        private_comments: The private-comment rule.
        shell_escape: The shell-escape rule.
        rules: Every rule, in the order the findings are reported.
    """

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

        def rule(name: str, *, enabled: bool, advisory: bool, lexical: bool) -> _ContentRule:
            """Return a rule with an empty match list bounded by ``limits.max_matches``.

            Args:
                name: The rule name of the finding.
                enabled: Whether the options select the rule.
                advisory: Whether the finding has the advisory weight.
                lexical: Whether the rule's scan depends on the TeX lexical regions.

            Returns:
                The rule.
            """
            matches = _Matches(limits.max_matches)
            return _ContentRule(name, enabled, matches, advisory=advisory, lexical=lexical)

        self.identity_terms = rule(
            "submission.identity_terms",
            enabled=bool(options.identity_terms),
            advisory=False,
            lexical=False,
        )
        self.identity_hints = rule(
            "submission.identity_hints",
            enabled=options.scan_identity_hints,
            advisory=True,
            lexical=True,
        )
        self.secrets = rule(
            "submission.secrets", enabled=options.scan_secrets, advisory=True, lexical=False
        )
        self.private_comments = rule(
            "submission.private_comments",
            enabled=options.scan_private_comments,
            advisory=True,
            lexical=True,
        )
        self.shell_escape = rule(
            "submission.shell_escape",
            enabled=options.check_shell_escape,
            advisory=True,
            lexical=True,
        )
        self.rules = (
            self.identity_terms,
            self.identity_hints,
            self.secrets,
            self.private_comments,
            self.shell_escape,
        )

    def scan(self, name: str, path: Path) -> None:
        """Read one file by name and, when it is a text file, by content.

        The file name is always matched against identity terms and searched for secrets. The
        content is read only when some rule is enabled and the file has a text extension or
        is named ``.env``; a file that cannot be read as UTF-8 within the budget is an issue.
        Comments of TeX files are checked for private notes, and their active source feeds
        the lexical rules.

        Args:
            name: The project path of the file.
            path: The file to read.
        """
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
        """Collect the secrets in a file name for redaction and, when enabled, as matches.

        Args:
            name: The project path of the file.
        """
        limit = self.limits.max_matches
        for _, kind, value in find_secrets(name, limit):
            if len(self.secret_values) < limit:
                self.secret_values.add(value)
            if self.secrets.enabled and self.secrets.matches.has_room:
                self.secrets.matches.items.append(
                    {"path": name, "channel": "filename", "kind": kind}
                )

    def _text_secrets(self, name: str, text: str) -> None:
        """Collect the secrets in a file's text for redaction and, when enabled, as matches.

        No text is searched once ``limits.max_matches`` secret values have been collected.

        Args:
            name: The project path of the file.
            text: The text of the file.
        """
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
        """Yield the identity-term matches of each line of ``text``.

        Args:
            name: The project path of the file.
            text: The text of the file.
            comment_lines: The numbers of the lines that hold a TeX comment.

        Yields:
            One match per term found on a line, with the path, line, channel and 1-based term
            index.
        """
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
        """Scan active TeX outside literal regions for identity hints and shell-escape constructs.

        An unclosed literal region leaves the scans of the lexical rules incomplete.

        Args:
            name: The project path of the file.
            text: The text of the file.
        """
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
        """Return one finding per enabled rule.

        The lexical issues apply only to the lexical rules.
        """
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
