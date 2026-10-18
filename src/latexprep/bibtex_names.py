"""Family names in a BibTeX ``author`` field, read with BibTeX's own name syntax.

BibTeX writes a list of names as ``First von Last and Last, First and others``. The
family name of a person is everything from the first lowercase "von" word to the end of
the last name, or the part before the first comma; a braced group stays one word; and
``and others`` marks a list that ends before the real one. This module owns that reading.
It compares nothing with any other source (see ``online_comparison``).
"""

from __future__ import annotations

from typing import NamedTuple


class FamilyName(NamedTuple):
    """The family name of one author, with and without its lowercase particles."""

    full: str
    last: str

    def spellings(self) -> tuple[str, ...]:
        """The spellings under which another source may list this family name."""
        return (self.full, self.last, without_particles(self.full))


class AuthorList(NamedTuple):
    """The authors of an entry; ``truncated`` when the list ends with ``and others``."""

    names: list[FamilyName]
    truncated: bool


def _unbraced(text: str) -> str:
    return text.replace("{", "").replace("}", "")


def _starts_lowercase(word: str) -> bool:
    return word[:1].islower()


def without_particles(family: str) -> str:
    """``family`` without its leading lowercase words, such as "van der"."""
    words = family.split()
    while len(words) > 1 and _starts_lowercase(words[0]):
        words.pop(0)
    return " ".join(words)


def _words(text: str) -> list[str]:
    """The words and separating commas of an author list; a braced group stays one word."""
    words: list[str] = []
    current: list[str] = []
    depth = 0
    for char in text:
        if char == "{":
            depth += 1
        elif char == "}":
            depth = max(0, depth - 1)
        if depth == 0 and (char.isspace() or char == ","):
            if current:
                words.append("".join(current))
                current = []
            if char == ",":
                words.append(",")
        else:
            current.append(char)
    if current:
        words.append("".join(current))
    return words


def _family_name(words: list[str]) -> FamilyName | None:
    """The family name of one author written as ``First von Last`` or ``von Last, First``."""
    parts: list[list[str]] = [[]]
    for word in words:
        if word == ",":
            parts.append([])
        else:
            parts[-1].append(word)
    family = parts[0]
    if not family:
        return None
    if len(parts) == 1:
        # The von part runs from the first to the last lowercase word before the last word.
        lowercase = [index for index, word in enumerate(family[:-1]) if _starts_lowercase(word)]
        start = lowercase[0] if lowercase else len(family) - 1
        last_start = lowercase[-1] + 1 if lowercase else len(family) - 1
        family = family[start:]
        last = family[last_start - start :]
    else:
        particles = 0
        while particles < len(family) - 1 and _starts_lowercase(family[particles]):
            particles += 1
        last = family[particles:]
    return FamilyName(_unbraced(" ".join(family)), _unbraced(" ".join(last)))


def parse_author_list(text: str) -> AuthorList | None:
    """The family names of a BibTeX author list, or None when any name cannot be read."""
    names: list[list[str]] = [[]]
    for word in _words(text):
        if word.casefold() == "and":
            names.append([])
        else:
            names[-1].append(word)
    truncated = len(names) > 1 and [word.casefold() for word in names[-1]] == ["others"]
    if truncated:
        names.pop()
    parsed = [_family_name(words) for words in names]
    if not parsed or any(name is None for name in parsed):
        return None
    return AuthorList([name for name in parsed if name is not None], truncated)
