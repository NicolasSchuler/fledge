"""Validated public check selectors, independent of checker configuration."""

from __future__ import annotations

import difflib
from dataclasses import dataclass

from .errors import PreparationError
from .rules import BY_CODE, BY_NAME


def _prefixes(code: str) -> list[str]:
    """List the selectors matching ``code``: its family, each longer prefix and the code.

    ``TEX101`` yields ``TEX``, ``TEX1``, ``TEX10`` and ``TEX101``.
    """
    family = len(code.rstrip("0123456789"))
    return [code[:length] for length in range(family, len(code) + 1)]


# Every valid selector that is not a rule name: ALL, the check codes and their prefixes.
SELECTORS = frozenset(prefix for code in BY_CODE for prefix in _prefixes(code)) | {"ALL"}


def _resolve_selector(selector: str, source: str) -> str:
    """Return the code, prefix or ``ALL`` that one selector stands for.

    Args:
        selector: The selector as written.
        source: Where the selector comes from, named in the error.

    Returns:
        The check code of a rule name, otherwise the selector in upper case.

    Raises:
        PreparationError: The selector is unknown; the error names the closest valid ones.
    """
    if selector in BY_NAME:
        return BY_NAME[selector].code
    candidate = selector.upper()
    if candidate in SELECTORS:
        return candidate
    stripped = candidate.strip()
    close = (
        [stripped]
        if stripped in SELECTORS
        else difflib.get_close_matches(stripped, sorted(SELECTORS), n=3)
    )
    hint = f" (did you mean {' or '.join(close)}?)" if close else ""
    raise PreparationError(
        f"Unknown {source} selector {selector!r}{hint}. Use ALL, a check code such as TEX001, "
        "a rule name such as source.edit_marker, or a family or family-plus-number prefix "
        "such as TEX or BIB1, in any case; fledge rules lists every check code"
    )


def resolve_selectors(selectors: tuple[str, ...], source: str) -> tuple[str, ...]:
    """Replace each rule name among the selectors by its check code; reject unknown ones.

    A rule name selects its check: the dotted name reports carry (``source.edit_marker``)
    and a former hyphenated name (``source-edit-marker``) both stand for the check's code.
    Codes, prefixes and ``ALL`` may be written in any case and are returned in upper case,
    so ``tex001`` selects ``TEX001``; surrounding white space is still an error.

    Args:
        selectors: The selectors to resolve.
        source: Where the selectors come from, named in the error.

    Returns:
        The selectors with each rule name replaced by its check code and every other
        selector in upper case.

    Raises:
        PreparationError: A selector is unknown. The error names the ``source`` of the
            selector and the closest valid ones.
    """
    return tuple(_resolve_selector(selector, source) for selector in selectors)


def selected_codes(selector: str) -> tuple[str, ...]:
    """List the check codes one resolved selector matches, in catalogue order.

    Args:
        selector: A resolved selector: ``ALL``, a check code or a code prefix.

    Returns:
        Every check code for ``ALL``, otherwise the codes that start with ``selector``.
    """
    if selector == "ALL":
        return tuple(BY_CODE)
    return tuple(code for code in BY_CODE if code.startswith(selector))


@dataclass(frozen=True)
class CheckSelection:
    """Filter configured checks; selection never supplies missing options or consent.

    Construction raises ``PreparationError`` when a field is not a tuple of strings or
    holds an unknown selector, and stores the selectors with rule names resolved to check
    codes.

    Attributes:
        select: The selectors of the checks to run.
        ignore: The selectors of the checks to leave out.
        advisory: The selectors of the checks that run and are reported as warnings: a
            violation or an undecided result of one never blocks.
    """

    select: tuple[str, ...] = ("ALL",)
    ignore: tuple[str, ...] = ()
    advisory: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("select", "ignore", "advisory"):
            selectors = getattr(self, name)
            if not isinstance(selectors, tuple) or any(
                not isinstance(selector, str) for selector in selectors
            ):
                raise PreparationError(f"checks.{name} must be an array of check selectors")
            # The frozen dataclass keeps the codes, so every later comparison uses them.
            object.__setattr__(self, name, resolve_selectors(selectors, f"checks.{name}"))

    def with_command_line(
        self, select: tuple[str, ...] = (), ignore: tuple[str, ...] = ()
    ) -> CheckSelection:
        """Layer --select and --ignore over this selection.

        --select replaces the configured select list; --ignore adds to the
        configured ignore list. Specificity and ignore-wins-ties still apply.

        Args:
            select: The ``--select`` selectors; none keeps the configured select list.
            ignore: The ``--ignore`` selectors.

        Returns:
            The combined selection.

        Raises:
            PreparationError: A selector is unknown.
        """
        select = resolve_selectors(select, "--select")
        ignore = resolve_selectors(ignore, "--ignore")
        return CheckSelection(
            select=select or self.select,
            ignore=tuple(dict.fromkeys((*self.ignore, *ignore))),
            advisory=self.advisory,
        )

    def is_advisory(self, code: str) -> bool:
        """Return whether the check ``code`` is listed in ``advisory``, by code or prefix."""
        return any(selector == "ALL" or code.startswith(selector) for selector in self.advisory)

    def enabled(self, code: str) -> bool:
        """Return whether the check ``code`` is enabled.

        The most specific selector decides; ignore wins equally specific matches.

        Args:
            code: A public check code.

        Returns:
            Whether the check is enabled.

        Raises:
            PreparationError: ``code`` is not a public check code.
        """
        if code not in BY_CODE:
            raise PreparationError(f"Unknown public check code: {code}")

        def specificity(selectors: tuple[str, ...]) -> int:
            """Measure the most specific of ``selectors`` that matches: ``ALL`` 0, none -1."""
            return max(
                (
                    0 if selector == "ALL" else len(selector)
                    for selector in selectors
                    if selector == "ALL" or code.startswith(selector)
                ),
                default=-1,
            )

        return specificity(self.select) > specificity(self.ignore)
