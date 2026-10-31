"""The ``[submission_checks]`` settings: what to scan a submission bundle for, and how.

These options choose the privacy scans (identity terms, secrets, private comments,
shell escape), the file-name and archive policies, and the deliverables and template
references to verify. This module owns only the options and their validation; the scans
that use them live in ``submission_checks`` and ``submission_content_scan``. The same
table also holds the shipped-bibliography options, ``bbl_checks.BblOptions``, which
``config`` reads separately.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, fields
from functools import partial
from typing import ClassVar, Literal

from .errors import SettingsError
from .option_config import (
    POSITIVE_INTEGER,
    Bounds,
    TextFormat,
    require_choice,
    require_pairs,
    require_text,
    require_unique_strings,
    validate_scalars,
)
from .path_safety import is_relative_posix_path

IdentityTermMatching = Literal["word", "substring"]
DELIVERABLE_KINDS = ("file", "pdf", "zip", "text")
# Bounds on configured values, so a settings file cannot make the scan unbounded.
_MAX_LISTED_ENTRIES = 256  # entries in one list setting
_MAX_TERM_CHARACTERS = 1024  # characters in one identity term or extension
_MAX_CHARACTER_SET_CHARACTERS = 4096  # characters in filename_allowed_characters
_MAX_PAIR_CHARACTERS = 4096  # characters in a deliverable or template path or value
_FINAL_EXTENSION = TextFormat(
    re.compile(r"\.[A-Za-z0-9_+-]+"), "a dot-prefixed final extension such as .tex"
)

# Options that do not make ``check_submission`` inspect the bundle: a matching mode that
# only refines identity terms, and the compressed archive (``check_submission_archive``).
_OPTIONS_OUTSIDE_BUNDLE_SCAN = frozenset({"identity_term_matching", "max_archive_bytes"})


def _require_project_path(name: str, value: object) -> None:
    """Check that a setting value is a safe path relative to the project.

    Args:
        name: The setting name that error messages report.
        value: The configured value.

    Raises:
        SettingsError: The value is not text within the length bound, or not a relative
            POSIX path free of ``..``.
    """
    require_text(name, value, expected="a relative project path", max_length=_MAX_PAIR_CHARACTERS)
    if not isinstance(value, str) or not is_relative_posix_path(value):
        raise SettingsError(
            f"{name} must be a safe path relative to the project, without '..'", (name,)
        )


@dataclass(frozen=True)
class SubmissionOptions:
    """Which submission-bundle checks run, and the values they compare against.

    Construction raises ``SettingsError`` when a field fails its check.

    Attributes:
        FIELD_BOUNDS: The bounds :func:`validate_scalars` applies to ``filename_max_length``
            and ``max_archive_bytes``.
        identity_terms: Literal identity terms to scan the bundle for.
        scan_identity_hints: Whether to flag possible identifying language.
        scan_secrets: Whether to scan for credential-shaped values.
        scan_private_comments: Whether to scan TeX comments for private notes.
        check_shell_escape: Whether to flag literal shell-execution constructs.
        filename_max_length: The longest file basename allowed, in characters, or ``None``
            for no limit.
        filename_allowed_characters: The literal characters a file basename may use, or
            ``None`` for no character policy.
        allowed_extensions: The dot-prefixed final extensions the bundle may contain; empty
            for no extension policy.
        required_deliverables: Pairs of a project-relative path and the kind of file
            expected there, one of ``DELIVERABLE_KINDS``.
        max_archive_bytes: The largest compressed archive allowed, in bytes, or ``None``
            for no limit.
        report_unused_assets: Whether to list assets outside the selected dependency graph.
        template_references: Pairs of a project-relative path and the reference file to
            compare it with.
        identity_term_matching: Whether identity terms match whole words or inside words.
    """

    FIELD_BOUNDS: ClassVar[dict[str, Bounds]] = {
        "filename_max_length": POSITIVE_INTEGER,
        "max_archive_bytes": POSITIVE_INTEGER,
    }

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
    identity_term_matching: IdentityTermMatching = "word"

    def __post_init__(self) -> None:
        validate_scalars(self)
        require_unique_strings(
            "identity_terms",
            self.identity_terms,
            max_items=_MAX_LISTED_ENTRIES,
            max_length=_MAX_TERM_CHARACTERS,
            allow_surrounding_whitespace=True,
        )
        require_unique_strings(
            "allowed_extensions",
            self.allowed_extensions,
            max_items=_MAX_LISTED_ENTRIES,
            max_length=_MAX_TERM_CHARACTERS,
            allow_surrounding_whitespace=True,
            item_format=_FINAL_EXTENSION,
        )
        require_text(
            "filename_allowed_characters",
            self.filename_allowed_characters,
            expected="a nonempty set of literal characters",
            max_length=_MAX_CHARACTER_SET_CHARACTERS,
            allow_none=True,
        )
        require_pairs(
            "required_deliverables",
            self.required_deliverables,
            first=_require_project_path,
            second=partial(require_choice, choices=DELIVERABLE_KINDS),
            max_items=_MAX_LISTED_ENTRIES,
        )
        require_pairs(
            "template_references",
            self.template_references,
            first=_require_project_path,
            second=partial(
                require_text, expected="a nonempty path", max_length=_MAX_PAIR_CHARACTERS
            ),
            max_items=_MAX_LISTED_ENTRIES,
        )

    def inspects_bundle(self) -> bool:
        """Return whether an option the bundle scan acts on differs from its default.

        A newly added option enables the scan unless ``_OPTIONS_OUTSIDE_BUNDLE_SCAN``
        names it, so a forgotten option runs a check instead of silently running none.
        """
        return any(
            getattr(self, option.name) != option.default
            for option in fields(self)
            if option.name not in _OPTIONS_OUTSIDE_BUNDLE_SCAN
        )
