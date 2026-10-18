"""Read access to a PDF's objects through qpdf's JSON version 2 output.

``parse_qpdf_json_v2`` validates the JSON envelope. ``QpdfObjectGraph`` resolves
indirect references and walks reachable dictionaries within fixed bounds, so a cyclic or
oversized object graph makes a check inconclusive instead of looping or exhausting
memory. This module interprets objects; running qpdf is ``pdf_tools``.
"""

from __future__ import annotations

import json
import re
from typing import Any, NamedTuple, TypeGuard

from .errors import PreparationError
from .limits import MAX_PDF_NESTING_LEVELS
from .pdf_tool_output import MAX_PARSED_NODES

_OBJECT_REFERENCE = re.compile(r"[0-9]+ [0-9]+ R")


def is_object_reference(value: object) -> TypeGuard[str]:
    """An indirect qpdf object reference such as ``12 0 R``."""
    return isinstance(value, str) and _OBJECT_REFERENCE.fullmatch(value) is not None


class QpdfJson(NamedTuple):
    """The object table of a qpdf JSON v2 document, and its page list (empty unless requested)."""

    objects: dict[str, Any]
    pages: list[Any]


def parse_qpdf_json_v2(output: str, failure: str, *, pages: int | None = None) -> QpdfJson:
    """Validate the envelope of qpdf ``--json=2`` output and return its object table.

    The envelope is the format version, the ``qpdf`` entry holding the header and the
    objects, and, when ``pages`` is given, a page list of exactly that length. Any
    deviation raises ``PreparationError(failure)``: the caller names what it needed.
    """
    try:
        document = json.loads(output)
        entries = document["qpdf"]
        if (
            document["version"] != 2
            or len(entries) != 2
            or entries[0]["jsonversion"] != 2
            or not isinstance(entries[1], dict)
        ):
            raise ValueError("unsupported JSON shape")
        page_list: list[Any] = []
        if pages is not None:
            page_list = document["pages"]
            if not isinstance(page_list, list) or len(page_list) != pages:
                raise ValueError("incomplete page data")
    except (ValueError, KeyError, TypeError, RecursionError) as error:
        raise PreparationError(failure) from error
    return QpdfJson(entries[1], page_list)


class QpdfObjectGraph:
    """qpdf JSON v2 object access with bounded, cycle-aware reference traversal."""

    def __init__(self, output: str, pages: int):
        document = parse_qpdf_json_v2(
            output, "qpdf did not return complete supported JSON v2 object data", pages=pages
        )
        self.objects = document.objects
        self.pages = document.pages
        if len(self.objects) > MAX_PARSED_NODES:
            raise PreparationError(
                f"The PDF has {len(self.objects):,} objects; object-level checks support at most "
                f"{MAX_PARSED_NODES:,}. Check a smaller PDF or turn these checks off for it."
            )
        self.root = self.dictionary(self.dictionary(self.lookup("trailer")).get("/Root"))
        if self.root.get("/Type") != "/Catalog":
            raise PreparationError("qpdf object data lacks a valid catalog")

    def lookup(self, key: str) -> object:
        try:
            entry = self.objects[key]
            value = entry["value"] if "value" in entry else entry["stream"]["dict"]
        except (KeyError, TypeError) as error:
            raise PreparationError("qpdf contains an unresolved or malformed object") from error
        return value

    def resolve(self, value: object) -> object:
        seen: set[str] = set()
        while is_object_reference(value):
            if value in seen or len(seen) > MAX_PDF_NESTING_LEVELS:
                raise PreparationError("qpdf contains a cyclic direct-reference chain")
            seen.add(value)
            value = self.lookup("obj:" + value)
        return value

    def dictionary(self, value: object) -> dict[str, object]:
        value = self.resolve(value)
        if not isinstance(value, dict):
            raise PreparationError("qpdf contains an unexpected non-dictionary object")
        return value

    def reachable_dictionaries(self) -> list[dict[str, object]]:
        pending: list[tuple[object, int]] = [(self.root, 0)]
        references: set[str] = set()
        result = []
        visited = 0
        while pending:
            value, depth = pending.pop()
            visited += 1
            if visited > MAX_PARSED_NODES or depth > MAX_PDF_NESTING_LEVELS:
                raise PreparationError("qpdf object graph exceeds the bounded traversal limit")
            if is_object_reference(value):
                if value in references:
                    continue
                references.add(value)
                value = self.resolve(value)
            if isinstance(value, dict):
                result.append(value)
                pending.extend((child, depth + 1) for child in value.values())
            elif isinstance(value, list):
                pending.extend((child, depth + 1) for child in value)
            if len(pending) > MAX_PARSED_NODES:
                raise PreparationError("qpdf object graph exceeds the bounded traversal limit")
        return result
