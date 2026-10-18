"""The tagged structure tree of a PDF, read from qpdf's object data.

``StructureTree.read`` walks the structure tree and the parent number tree within fixed
bounds, so a cyclic, repeated, excessively deep or oversized tree makes the structure
checks inconclusive instead of looping. What the tree means (tables, links, figure
alternative text, reference consistency) is decided by ``pdf_structure_checks``. This
module does not run qpdf or interpret role semantics beyond the standard role set.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import NamedTuple, Self

from .cancellation import cancellation_point
from .errors import PreparationError
from .limits import MAX_PDF_NESTING_LEVELS
from .pdf_qpdf_graph import QpdfObjectGraph, is_object_reference

# Structure elements, marked-content references, parent-tree entries and page annotations
# are each materialized and cross-checked one by one, so their bound is lower than the
# ``MAX_PARSED_NODES`` bound on raw qpdf objects.
MAX_STRUCTURE_ITEMS = 10_000
_STANDARD_ROLES = frozenset(
    "/" + role
    for role in (
        "Document Part Art Sect Div BlockQuote Caption TOC TOCI Index NonStruct Private "
        "P H H1 H2 H3 H4 H5 H6 L LI Lbl LBody Table TR TH TD THead TBody TFoot Span "
        "Quote Note Reference BibEntry Code Link Annot Ruby RB RT RP Warichu WT WP "
        "Figure Formula Form"
    ).split()
)


@dataclass
class StructureElement:
    """One structure element: its object, resolved role, parent and children by index."""

    reference: str | None
    role: str
    data: dict[str, object]
    parent: int | None
    page: str | None
    children: list[int] = field(default_factory=list)


class MarkedContent(NamedTuple):
    """A marked-content item (by id) that an element owns on a page, in a stream or not."""

    owner: int
    page: str | None
    stream: str | None
    mcid: int


class ObjectReference(NamedTuple):
    """An element's reference to another object (an annotation, say) on a page."""

    owner: int
    page: str | None
    target: object


class StructureTree:
    """A PDF's tagged structure tree and parent tree, read from qpdf's object data.

    ``read`` walks the tree within fixed bounds; anything cyclic, repeated, excessively
    deep or malformed raises ``PreparationError``. Problems that leave the tree readable
    (a wrong parent link, say) accumulate in ``problems`` for the checks to report, and
    constructs the checks do not interpret accumulate in ``uncertainty``.
    """

    def __init__(self, objects: QpdfObjectGraph):
        self.objects = objects
        root_value = objects.root.get("/StructTreeRoot")
        self.root_reference = root_value if isinstance(root_value, str) else None
        self.root = objects.dictionary(root_value)
        if self.root.get("/Type") != "/StructTreeRoot":
            raise PreparationError("The PDF structure root has an unsupported object type")
        self.pages: dict[str, int] = {}
        for number, page in enumerate(objects.pages, 1):
            if not isinstance(page, dict) or not isinstance(page.get("object"), str):
                raise PreparationError("qpdf page inventory has no valid object reference")
            reference = page["object"]
            if reference in self.pages or objects.dictionary(reference).get("/Type") != "/Page":
                raise PreparationError("qpdf page inventory is duplicated or malformed")
            self.pages[reference] = number
        self.roles = objects.dictionary(self.root.get("/RoleMap", {}))
        self.elements: list[StructureElement] = []
        self.content: list[MarkedContent] = []
        self.object_references: list[ObjectReference] = []
        self.problems: list[object] = []
        self.uncertainty: set[str] = set()
        self.parents: dict[int, object] = {}
        self._visited: set[str] = set()
        self._steps = 0

    @classmethod
    def read(cls, objects: QpdfObjectGraph) -> Self:
        """The structure tree of ``objects`` with its elements, content and parent tree."""
        tree = cls(objects)
        tree._visit(tree.root.get("/K", []), None, None, 0)
        tree.parents = tree._parent_tree(tree.root.get("/ParentTree"))
        return tree

    def _role(self, value: object) -> str:
        role = self.objects.resolve(value)
        seen = set()
        while isinstance(role, str) and role in self.roles:
            if role in seen or len(seen) > MAX_PDF_NESTING_LEVELS:
                raise PreparationError("PDF structure role mapping is cyclic or excessively deep")
            seen.add(role)
            role = self.objects.resolve(self.roles[role])
        if not isinstance(role, str) or not role.startswith("/"):
            raise PreparationError("PDF structure element has no supported role name")
        if role not in _STANDARD_ROLES:
            self.uncertainty.add("unmapped or unsupported PDF structure role")
        return role

    def _visit(self, raw: object, parent: int | None, page: str | None, depth: int) -> None:
        cancellation_point()
        self._steps += 1
        if self._steps > MAX_STRUCTURE_ITEMS or depth > MAX_PDF_NESTING_LEVELS:
            raise PreparationError("PDF structure tree exceeds bounded traversal limits")
        reference = raw if is_object_reference(raw) else None
        if reference:
            if reference in self._visited:
                raise PreparationError("PDF structure tree has a cycle or repeated object child")
            self._visited.add(reference)
        value = self.objects.resolve(raw)
        if isinstance(value, list):
            for child in value:
                self._visit(child, parent, page, depth + 1)
        elif type(value) is int:
            self._visit_content_id(value, parent, page)
        elif isinstance(value, dict):
            self._visit_dictionary(value, reference, parent, page, depth)
        else:
            raise PreparationError("PDF structure child is not a supported element or content item")

    def _visit_content_id(self, mcid: int, parent: int | None, page: str | None) -> None:
        if parent is None or mcid < 0:
            self.problems.append({"reason": "MCID has no structural owner or is negative"})
        else:
            self.content.append(MarkedContent(parent, page, None, mcid))

    def _visit_dictionary(
        self,
        value: dict[str, object],
        reference: str | None,
        parent: int | None,
        page: str | None,
        depth: int,
    ) -> None:
        declared_page = value.get("/Pg", page)
        page = declared_page if isinstance(declared_page, str) else None
        kind = value.get("/Type")
        if kind == "/MCR":
            self._visit_marked_content_reference(value, parent, page)
        elif kind == "/OBJR":
            if parent is None:
                self.problems.append({"reason": "Object reference has no structural owner"})
            else:
                self.object_references.append(ObjectReference(parent, page, value.get("/Obj")))
        else:
            self._visit_element(value, reference, parent, page, depth)

    def _visit_marked_content_reference(
        self, value: dict[str, object], parent: int | None, page: str | None
    ) -> None:
        mcid = self.objects.resolve(value.get("/MCID"))
        stream = value.get("/Stm")
        if parent is None or type(mcid) is not int or mcid < 0:
            self.problems.append({"reason": "MCR is missing a valid nonnegative MCID or owner"})
        elif stream is not None and not is_object_reference(stream):
            self.uncertainty.add("non-reference marked-content stream")
        else:
            self.content.append(MarkedContent(parent, page, stream, mcid))

    def _visit_element(
        self,
        value: dict[str, object],
        reference: str | None,
        parent: int | None,
        page: str | None,
        depth: int,
    ) -> None:
        role = self._role(value.get("/S"))
        if "/NS" in value:
            self.uncertainty.add("PDF namespace-specific structure roles are not interpreted")
        expected_parent = self.root_reference if parent is None else self.elements[parent].reference
        if expected_parent is None:
            self.uncertainty.add("direct structure dictionaries lack verifiable parent identities")
        elif value.get("/P") != expected_parent:
            self.problems.append(
                {"object": reference, "reason": "missing or incorrect structure parent"}
            )
        index = len(self.elements)
        self.elements.append(StructureElement(reference, role, value, parent, page))
        if parent is not None:
            self.elements[parent].children.append(index)
        self._visit(value.get("/K", []), index, page, depth + 1)

    def _parent_tree(self, raw: object) -> dict[int, object]:
        if raw is None:
            return {}
        result: dict[int, object] = {}
        pending = [(raw, 0)]
        seen: set[str] = set()
        steps = 0
        while pending:
            value, depth = pending.pop()
            steps += 1
            if steps > MAX_STRUCTURE_ITEMS or depth > MAX_PDF_NESTING_LEVELS:
                raise PreparationError("Structure parent number tree exceeds traversal limits")
            if isinstance(value, str):
                if value in seen:
                    raise PreparationError("Structure parent number tree is cyclic")
                seen.add(value)
            node = self.objects.dictionary(value)
            numbers = self.objects.resolve(node.get("/Nums", []))
            children = self.objects.resolve(node.get("/Kids", []))
            if not isinstance(numbers, list) or len(numbers) % 2 or not isinstance(children, list):
                raise PreparationError("Structure parent number tree is malformed")
            if (
                len(result) + len(numbers) // 2 > MAX_STRUCTURE_ITEMS
                or len(pending) + len(children) > MAX_STRUCTURE_ITEMS
            ):
                raise PreparationError("Structure parent number tree exceeds entry limits")
            for index in range(0, len(numbers), 2):
                key = numbers[index]
                if type(key) is not int or key < 0 or key in result:
                    raise PreparationError(
                        "Structure parent number tree has invalid or duplicate keys"
                    )
                result[key] = numbers[index + 1]
            pending.extend((child, depth + 1) for child in children)
        return result

    def parent_entry(self, container: dict[str, object], name: str) -> object:
        key = self.objects.resolve(container.get(name))
        if type(key) is not int or key < 0 or key not in self.parents:
            return None
        return self.objects.resolve(self.parents[key])

    def location(self, index: int) -> dict[str, object]:
        element = self.elements[index]
        return {
            "object": element.reference,
            "role": element.role,
            "page": self.pages.get(element.page or ""),
        }
