r"""Typography suggestions over the prose of the document body (``STY301`` to ``STY307``).

Each check reads :attr:`StyleContext.prose`, where the preamble, mathematics, comments,
verbatim text and the arguments of commands that hold keys or values are blanked, so
``1-5`` in a label, a URL or a formula is never reported. White space that a comment at
the end of a line swallows is no space to TeX, so it is never reported as one. Every
pattern is linear: alternatives without nested repetition, possessive digit runs and
windows of bounded length.
"""

from __future__ import annotations

import re
from itertools import pairwise

from .cancellation import cancellation_point
from .style_support import Occurrence, StyleContext
from .tex_commands import PRINTED_CITATION_COMMANDS, with_capitalized_forms
from .tex_lexer import command_end

# White space within one paragraph: spaces and tabs, with at most one line break.
_SPACE = r"(?:[ \t]+(?:\r?\n[ \t]*)?|\r?\n[ \t]*)"
# A word ending in a letter, white space, then a reference or citation that wants a tie.
_UNTIED_REFERENCE = re.compile(
    rf"(?<![\\\w])\w*[^\W\d_](?P<space>{_SPACE})\\(?P<command>ref|eqref|cref|cite)(?![A-Za-z@])"
)
# "et al." followed by white space and a lower-case word, a command or a parenthesis: the
# sentence does not end, but TeX sets the wider space that follows a sentence.
_ET_AL_SPACE = re.compile(rf"(?<![\w\\])et[ ~]al\.(?P<space>{_SPACE})(?=[a-z\\(\[])")
# Two numbers joined by a hyphen, not part of a longer token such as a date or a version.
_HYPHEN_RANGE = re.compile(r"(?<![\w\-./:])(\d++)-(\d++)(?![\w\-/:]|\.\d)")
# A word or phrase of at most 80 characters in straight double quotes on one line. The
# opening quote may not start a babel shorthand such as "` or "<.
_STRAIGHT_QUOTES = re.compile(r'(?<![\\\w"])"(?=[^\s`\'<>|=~"-])[^"\n]{1,80}?(?<=\S)"(?![\w"\'])')
# Three periods that are not part of a longer run.
_ELLIPSIS = re.compile(r"(?<![.\\])\.\.\.(?!\.)")
# White space between the end of a word or command and a footnote.
_SPACED_FOOTNOTE = re.compile(rf"(?<=\S)(?P<space>{_SPACE})\\footnote(?![A-Za-z@])")
# Commands after which straight double quotes are what the author means: csquotes makes
# them active quotes.
_ACTIVE_QUOTE_COMMANDS = frozenset({"MakeOuterQuote", "MakeAutoQuote", "MakeForeignQuote"})
# Citation commands that may merge their keys into one citation.
_CITATIONS = with_capitalized_forms(PRINTED_CITATION_COMMANDS)


def untied_references(context: StyleContext) -> list[Occurrence]:
    r"""Find ``\ref``, ``\eqref``, ``\cref`` and ``\cite`` after a word and a breakable space.

    Args:
        context: The document.

    Returns:
        One occurrence per reference, at the reference.
    """
    occurrences = []
    for match in _UNTIED_REFERENCE.finditer(context.prose):
        if context.comment_between(match.start("space"), match.end("space")):
            continue
        word = match.group()[: match.start("space") - match.start()]
        command = match.group("command")
        occurrences.append(
            Occurrence(
                match.start("command") - 1,
                f"A line may break between {word!r} and \\{command}; a tie keeps the "
                "reference with the word before it.",
                suggestion=f"Write {word}~\\{command}{{...}}.",
                details={"word": word, "command": command},
            )
        )
    return occurrences


def et_al_spacing(context: StyleContext) -> list[Occurrence]:
    r"""Find "et al." followed by a space that TeX widens as if a sentence ended there.

    Nothing is reported when the document uses ``\frenchspacing``, which sets every space
    alike.

    Args:
        context: The document.

    Returns:
        One occurrence per "et al." that does not end a sentence, at the "et al.".
    """
    if any(command.name == "frenchspacing" for command in context.document.commands):
        return []
    return [
        Occurrence(
            match.start(),
            "The space after 'et al.' is set as the wider space after a sentence, but the "
            "sentence goes on.",
            suggestion=r"Write et al.\ before a word, or et al.~ before a citation.",
        )
        for match in _ET_AL_SPACE.finditer(context.prose)
        if not context.comment_between(match.start("space"), match.end("space"))
    ]


def hyphen_ranges(context: StyleContext) -> list[Occurrence]:
    """Find number ranges written with a hyphen instead of an en dash.

    Args:
        context: The document.

    Returns:
        One occurrence per range, at the range.
    """
    return [
        Occurrence(
            match.start(),
            f"'{match.group()}' is a number range, which takes an en dash, not a hyphen.",
            suggestion=f"Write {match.group(1)}--{match.group(2)}.",
            details={"range": match.group()},
        )
        for match in _HYPHEN_RANGE.finditer(context.prose)
    ]


def straight_quotes(context: StyleContext) -> list[Occurrence]:
    """Find text in straight double quotes, which TeX prints as two closing quotes.

    Nothing is reported when csquotes makes the straight quote an active quote.

    Args:
        context: The document.

    Returns:
        One occurrence per quoted phrase, at the opening quote.
    """
    if any(command.name in _ACTIVE_QUOTE_COMMANDS for command in context.document.commands):
        return []
    return [
        Occurrence(
            match.start(),
            f"{match.group()} uses straight double quotes; TeX prints both as closing quotes.",
            suggestion="Write ``...'' or use csquotes (\\enquote{...}).",
        )
        for match in _STRAIGHT_QUOTES.finditer(context.prose)
    ]


def ellipses(context: StyleContext) -> list[Occurrence]:
    r"""Find three periods typed for an ellipsis, which TeX spaces too tightly.

    Args:
        context: The document.

    Returns:
        One occurrence per ellipsis, at its first period.
    """
    return [
        Occurrence(
            match.start(),
            "Three periods are spaced as ordinary punctuation; \\dots spaces an ellipsis.",
            suggestion=r"Write \dots (or \ldots).",
        )
        for match in _ELLIPSIS.finditer(context.prose)
    ]


def spaced_footnotes(context: StyleContext) -> list[Occurrence]:
    r"""Find white space before ``\footnote``, which puts a space before the footnote mark.

    Args:
        context: The document.

    Returns:
        One occurrence per footnote, at the ``\footnote``.
    """
    return [
        Occurrence(
            match.end("space"),
            "The space before \\footnote is printed before the footnote mark.",
            suggestion="Remove the space, or end the line before it with %.",
        )
        for match in _SPACED_FOOTNOTE.finditer(context.prose)
        if not context.comment_between(match.start("space"), match.end("space"))
    ]


def consecutive_citations(context: StyleContext) -> list[Occurrence]:
    r"""Find citation commands that follow each other directly, such as ``\cite{a}\cite{b}``.

    Only the same command without optional arguments is reported: notes such as a page
    number would be lost when the keys are merged.

    Args:
        context: The document.

    Returns:
        One occurrence per citation that could join the one before it, at that citation.
    """
    document = context.document
    citations = [
        command
        for command in document.commands
        if command.name in _CITATIONS and command.start >= document.body_start
    ]
    occurrences = []
    for previous, command in pairwise(citations):
        cancellation_point()
        end = command_end(previous)
        between = document.text[end : command.start]
        if (
            command.start >= end
            and command.name == previous.name
            and not (command.options or previous.options)
            and previous.arguments
            and command.arguments
            and not between.strip()
        ):
            keys = f"{previous.arguments[0].value.strip()},{command.arguments[0].value.strip()}"
            occurrences.append(
                Occurrence(
                    command.start,
                    f"Two \\{command.name} commands in a row; one \\{command.name} with both "
                    "keys gives one citation, sorted and compressed by the style.",
                    suggestion=f"Write \\{command.name}{{{keys}}}.",
                    details={"command": command.name, "keys": keys},
                )
            )
    return occurrences
