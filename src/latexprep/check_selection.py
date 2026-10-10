"""Validated public check selectors, independent of checker configuration."""

from __future__ import annotations

import difflib
from dataclasses import dataclass

from .models import PreparationError
from .rules import BY_CODE

_SELECTORS = frozenset(
    code[:length]
    for code in BY_CODE
    for length in range(len(code.rstrip("0123456789")), len(code) + 1)
) | {"ALL"}


def validate_selectors(selectors: tuple[str, ...], source: str) -> None:
    """Reject unknown selectors, naming their source and the closest valid ones."""
    for selector in selectors:
        if selector in _SELECTORS:
            continue
        candidate = selector.strip().upper()
        close = (
            [candidate]
            if candidate in _SELECTORS
            else difflib.get_close_matches(candidate, sorted(_SELECTORS), n=3)
        )
        hint = f" (did you mean {' or '.join(close)}?)" if close else ""
        raise PreparationError(
            f"Unknown {source} selector {selector!r}{hint}. Selectors are case-sensitive: "
            "use ALL, a check code such as TEX001, or a family or family-plus-number prefix "
            "such as TEX or BIB1; fledge rules lists every check code"
        )


@dataclass(frozen=True)
class CheckSelection:
    """Filter configured checks; selection never supplies missing options or consent."""

    select: tuple[str, ...] = ("ALL",)
    ignore: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("select", "ignore"):
            selectors = getattr(self, name)
            if not isinstance(selectors, tuple) or any(
                not isinstance(selector, str) for selector in selectors
            ):
                raise PreparationError(f"checks.{name} must be an array of check selectors")
            validate_selectors(selectors, f"checks.{name}")

    def with_command_line(
        self, select: tuple[str, ...] = (), ignore: tuple[str, ...] = ()
    ) -> CheckSelection:
        """Layer --select and --ignore over this selection.

        --select replaces the configured select list; --ignore adds to the
        configured ignore list. Specificity and ignore-wins-ties still apply.
        """
        validate_selectors(select, "--select")
        validate_selectors(ignore, "--ignore")
        return CheckSelection(
            select=select or self.select, ignore=tuple(dict.fromkeys((*self.ignore, *ignore)))
        )

    def enabled(self, code: str) -> bool:
        """Use the most specific selector; ignore wins equally specific matches."""
        if code not in BY_CODE:
            raise ValueError(f"Unknown public check code: {code}")

        def specificity(selectors: tuple[str, ...]) -> int:
            return max(
                (
                    0 if selector == "ALL" else len(selector)
                    for selector in selectors
                    if selector == "ALL" or code.startswith(selector)
                ),
                default=-1,
            )

        return specificity(self.select) > specificity(self.ignore)
