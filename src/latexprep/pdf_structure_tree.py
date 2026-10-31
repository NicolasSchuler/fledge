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
    """One structure element: its object, resolved role, parent and children by index.

    Attributes:
        reference: The element's object reference, or ``None`` for a direct dictionary.
        role: The resolved role name, such as ``/Table``.
        data: The element's dictionary.
        parent: The index of the parent element, or ``None`` directly below the root.
        page: The object reference of the element's page, if known.
        children: The indexes of the child elements.
    """

    reference: str | None
    role: str
    data: dict[str, object]
    parent: int | None
    page: str | None
    children: list[int] = field(default_factory=list)


class MarkedContent(NamedTuple):
    """A marked-content item (by id) that an element owns on a page, in a stream or not.

    Attributes:
        owner: The index of the element that owns the item.
        page: The object reference of the page, if known.
        stream: The object reference of the content stream (``/Stm``), if one is named.
        mcid: The marked-content id.
    """

    owner: int
    page: str | None
    stream: str | None
    mcid: int


class ObjectReference(NamedTuple):
    """An element's reference to another object (an annotation, say) on a page.

    Attributes:
        owner: The index of the element that owns the reference.
        page: The object reference of the page, if known.
        target: The referenced object (``/Obj``).
    """

    owner: int
    page: str | None
    target: object


class StructureTree:
    """A PDF's tagged structure tree and parent tree, read from qpdf's object data.

    ``read`` walks the tree within fixed bounds; anything cyclic, repeated, excessively
    deep or malformed raises ``PreparationError``. Construction raises it as well when
    the structure root or the page inventory is malformed.

    Attributes:
        objects: The qpdf object graph.
        root_reference: The object reference of the structure root, or ``None``.
        root: The structure root dictionary.
        pages: The page number, from 1, of each page object reference.
        roles: The role map.
        elements: The structure elements in traversal order.
        content: The marked-content items the elements own.
        object_references: The object references the elements own.
        problems: Problems that leave the tree readable (a wrong parent link, say), for
            the checks to report.
        uncertainty: Constructs the checks do not interpret.
        parents: The entries of the parent number tree, by key.
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
        """Read the structure tree of ``objects`` with its elements, content and parent tree.

        Returns:
            The tree.

        Raises:
            PreparationError: The tree is cyclic, repeated, excessively deep, oversized or
                malformed.
        """
        tree = cls(objects)
        tree._visit(tree.root.get("/K", []), None, None, 0)
        tree.parents = tree._parent_tree(tree.root.get("/ParentTree"))
        return tree

    def _role(self, value: object) -> str:
        """Resolve a role through the role map; a nonstandard result adds an uncertainty.

        Returns:
            The role name, such as ``/Table``.

        Raises:
            PreparationError: The mapping is cyclic or excessively deep, or yields no role
                name.
        """
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
        """Visit one child of the tree: a list, a marked-content id or a dictionary.

        Args:
            raw: The child, possibly an object reference.
            parent: The index of the owning element, or ``None`` at the root.
            page: The inherited page reference.
            depth: The nesting depth.

        Raises:
            PreparationError: The traversal exceeds its bounds, an object repeats, or the
                child has an unsupported type.
        """
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
        """Record a marked-content id, or a problem if it is negative or has no owner."""
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
        """Visit a marked-content reference, an object reference or a structure element."""
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
        """Record a marked-content reference, or a problem if it lacks a valid id or owner."""
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
        """Record a structure element, check its parent link and visit its children."""
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
        """Read the parent number tree within the traversal and entry bounds.

        Returns:
            The entries by key; empty when the PDF has no parent tree.

        Raises:
            PreparationError: The tree is cyclic, malformed, too deep or too large, or has
                invalid or duplicate keys.
        """
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
        """Look up the parent tree entry that a key of ``container`` names.

        Args:
            container: The dictionary that holds the key.
            name: The name of the key.

        Returns:
            The resolved entry, or ``None`` when the key is missing, invalid or unknown.
        """
        key = self.objects.resolve(container.get(name))
        if type(key) is not int or key < 0 or key not in self.parents:
            return None
        return self.objects.resolve(self.parents[key])

    def location(self, index: int) -> dict[str, object]:
        """Describe where element ``index`` is: its object, role and page number."""
        element = self.elements[index]
        return {
            "object": element.reference,
            "role": element.role,
            "page": self.pages.get(element.page or ""),
        }
