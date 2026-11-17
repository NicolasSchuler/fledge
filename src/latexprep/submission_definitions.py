r"""The definitions of a project that may change what a submission field says.

A supported spelling such as ``\&`` or ``\title`` does not establish meaning when the
project defines it, so the fields that use such a command need manual review. This
module finds those definitions in the selected sources of an expanded document; it
converts no text.
"""

from __future__ import annotations

import re
from collections.abc import Collection

from .expanded_document import ExpandedDocument, package_code_sources
from .tex_commands import MACRO_DEFINITION_COMMANDS, PRIMITIVE_DEFINITION_COMMANDS
from .tex_lexer import Command

# Document-text grammar, unlike the lexer's package-code CONTROL_SEQUENCE: "@" is
# not a letter, and a control symbol may be any character, including a line break.
TEXT_CONTROL_SEQUENCE = re.compile(r"\\([A-Za-z]+|.)", re.S)
# Prefixes of a definition, which define nothing themselves.
_PREFIXES = frozenset({"global", "long", "protected", "outer"})
# Characters after a primitive definition that it cannot make active: groups, parameters
# and comments.
_NEVER_ACTIVE = frozenset("{}#%")


def _defined_target(masked: str, command: Command) -> tuple[str | None, str | None]:
    r"""Return what one definition command defines: a command or environment, or a character.

    A primitive definition followed by a brace, a parameter or a comment makes no character
    active.

    Args:
        masked: The masked text of the file that holds the command.
        command: A definition command other than a prefix such as ``\global``.

    Returns:
        The name of the defined command or environment, or ``None``, and the character the
        definition makes active, or ``None``.
    """
    if command.arguments and command.name not in PRIMITIVE_DEFINITION_COMMANDS:
        target = command.arguments[0].value.strip()
    else:
        target = masked[command.name_end :].lstrip()
    token = TEXT_CONTROL_SEQUENCE.match(target)
    if token is not None:
        return token.group(1), None
    if (
        target
        and command.name in PRIMITIVE_DEFINITION_COMMANDS
        and target[0] not in _NEVER_ACTIVE
        and not target[0].isalnum()
    ):
        return None, target[0]
    if "environment" in command.name.casefold() and re.fullmatch(r"[A-Za-z]+", target):
        return target, None
    return None, None


def local_definitions(
    document: ExpandedDocument, declarations: Collection[str]
) -> tuple[frozenset[str], frozenset[str]]:
    r"""Return the commands, environments and active characters that the project itself defines.

    Conditional or order-dependent activation is not inferred. The document class, such
    as a shipped ``llncs.cls``, defines how ``\title`` or ``\\`` is typeset, not what the
    field says, so its definitions do not count. A package loaded only with
    ``\usepackage``, such as a venue's ``.sty``, likewise defines how the declarations of
    the fields are typeset, so its definitions of them do not count; its other
    definitions, such as the author's text macros, do, and so does every definition in
    the paper's ``.tex`` files. A prefix such as ``\global`` defines nothing itself.

    Args:
        document: The expanded document whose selected sources are scanned.
        declarations: The commands and environments that declare a field, such as
            ``title`` and ``abstract``.

    Returns:
        The names of the defined commands and environments, and the characters that
        primitive definitions make active.
    """
    redefined: set[str] = set()
    active_characters: set[str] = set()
    packages = package_code_sources(document.graph, document.selected)
    for path in document.selected:
        source = document.graph.sources.get(path)
        if source is None or path.endswith(".cls"):
            continue
        for command in source.commands:
            if command.name not in MACRO_DEFINITION_COMMANDS or command.name in _PREFIXES:
                continue
            name, character = _defined_target(source.masked, command)
            if character is not None:
                active_characters.add(character)
            elif name is not None and not (path in packages and name in declarations):
                redefined.add(name)
    return frozenset(redefined), frozenset(active_characters)
