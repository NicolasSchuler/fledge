"""Type 3 glyph programs: do declared glyphs paint images, vector shapes, or both?

qpdf supplies the declared glyph programs (content streams) of Type 3 fonts. A small,
conservative content-stream lexer classifies each program's painting; strings,
dictionaries, compressed streams and inline-image payloads are never guessed at, so
they leave the result uncertain. A declared Type 3 font does not prove that any of its
glyphs is used, and Type 3 alone never implies bitmap glyphs.

Does not own: which options enable the check (``pdf_artwork_options``), the other
artwork measurements or the ordering of findings (``pdf_artwork``).
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass
from functools import partial
from typing import Final, NamedTuple

from .command_runner import CommandResult
from .errors import PreparationError
from .finding_text import plural
from .findings import Finding
from .measurement_findings import MAX_SAMPLES, measurement_finding
from .pdf_artwork_options import ArtworkRule
from .pdf_qpdf_graph import QpdfObjectGraph, is_object_reference, parse_qpdf_json_v2
from .pdf_tools import PdfToolSession, qpdf_decoded_objects_argv, qpdf_objects_argv

# Tokens in one glyph program, tokens across every program of a document, and glyph
# programs in a document: each bounds the work a hostile font can demand.
_MAX_TOKENS_PER_PROGRAM: Final = 50_000
_MAX_TOKENS_PER_DOCUMENT: Final = 50_000
_MAX_GLYPH_PROGRAMS: Final = 50_000
_MAX_PROGRAM_BYTES: Final = 1_000_000
# Form XObjects a glyph program may nest through ``Do`` before it counts as unclassified.
_MAX_FORM_NESTING_LEVELS: Final = 20
# Glyph programs and Form XObjects decoded in one qpdf call.
_MAX_DECODED_OBJECTS: Final = 1000
_TOKEN = re.compile(r"/?[^\s\x00()<>\[\]{}%/]+|[\[\]]")
_LINE_END = re.compile(r"[\r\n]")
_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)")
_SIMPLE_GLYPH_OPERATORS: Final = frozenset(
    "q Q cm w J j M d ri i g G rg RG k K cs CS sc SC m l c v y h re n W W* d0 d1 [ ]".split()
)
_PAINT_OPERATORS: Final = frozenset("S s f F f* B B* b b*".split())


def _program_tokens(data: bytes) -> list[str]:
    """Tokenize a conservative stream subset without treating string payloads as operators.

    Tokenizing stops after a ``BI`` operator, whose binary payload is not parsed.

    Returns:
        The tokens, ending with ``BI`` if the program has one.

    Raises:
        PreparationError: The program holds a string, a dictionary or an unsupported
            token, or exceeds the token limit.
    """
    text = data.decode("latin1")
    tokens: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char.isspace() or char == "\0":
            index += 1
        elif char == "%":
            end = _LINE_END.search(text, index)
            index = len(text) if end is None else end.end()
        elif char in "(<":
            # Text strings, dictionaries and inline image data are deliberately
            # unsupported: do not mistake embedded operator-looking bytes for paint.
            raise PreparationError("Type 3 program uses unsupported string/dictionary content")
        else:
            match = _TOKEN.match(text, index)
            if match is None:
                raise PreparationError("Type 3 program contains unsupported token syntax")
            token = match.group()
            tokens.append(token)
            index = match.end()
            if len(tokens) > _MAX_TOKENS_PER_PROGRAM:
                raise PreparationError("Type 3 glyph program exceeds the token limit")
            if token == "BI":
                # BI is an actual inline-image operator, but the binary payload
                # cannot safely be parsed by this restricted content lexer.
                break
    return tokens


class GlyphPaint(NamedTuple):
    """What a glyph program paints, and whether part of it could not be classified.

    Attributes:
        raster: Whether the program paints an image.
        vector: Whether the program paints a vector shape.
        incomplete: Whether part of the program could not be classified.
    """

    raster: bool = False
    vector: bool = False
    incomplete: bool = False

    def merged(self, other: GlyphPaint) -> GlyphPaint:
        """Combine two classifications; each flag is set when either one sets it."""
        return GlyphPaint(
            self.raster or other.raster,
            self.vector or other.vector,
            self.incomplete or other.incomplete,
        )

    @property
    def classification(self) -> str:
        """The paint as ``mixed``, ``bitmap``, ``vector`` or ``empty``."""
        if self.raster and self.vector:
            return "mixed"
        return "bitmap" if self.raster else "vector" if self.vector else "empty"


_UNCLASSIFIED = GlyphPaint(incomplete=True)


@dataclass(slots=True)
class _TokenAllowance:
    """Glyph-program tokens still permitted across one Type 3 inventory.

    Attributes:
        remaining: The tokens still permitted.
    """

    remaining: int = _MAX_TOKENS_PER_DOCUMENT


@dataclass(frozen=True, slots=True)
class _Document:
    """The qpdf object graph of one inventory and the token allowance its programs share.

    Attributes:
        objects: The qpdf object graph.
        allowance: The token allowance the programs share.
    """

    objects: QpdfObjectGraph
    allowance: _TokenAllowance


def _program_tokens_within(document: _Document, reference: str) -> list[str] | None:
    """Tokenize an unfiltered inline program stream within the shared token allowance.

    Args:
        document: The object graph and the token allowance, which the tokens use up.
        reference: The program's object reference.

    Returns:
        The tokens, or ``None`` when the stream is unreadable, filtered, too large or
        unsupported, or exhausts the allowance.
    """
    entry = document.objects.objects.get("obj:" + reference)
    if not isinstance(entry, dict) or not isinstance(entry.get("stream"), dict):
        return None
    stream = entry["stream"]
    dictionary = stream.get("dict")
    if (
        not isinstance(dictionary, dict)
        or dictionary.get("/Filter")
        or not isinstance(stream.get("data"), str)
    ):
        return None
    try:
        data = base64.b64decode(stream["data"], validate=True)
        if len(data) > _MAX_PROGRAM_BYTES:
            return None
        tokens = _program_tokens(data)
    except (binascii.Error, PreparationError):
        return None
    document.allowance.remaining -= len(tokens)
    return tokens if document.allowance.remaining >= 0 else None


def _glyph_program(
    document: _Document,
    reference: object,
    resources: object,
    seen: frozenset[str] = frozenset(),
) -> GlyphPaint:
    """Classify what a glyph program or Form XObject paints.

    Args:
        document: The object graph and the token allowance.
        reference: The program's object reference.
        resources: The resources that ``Do`` operands are looked up in.
        seen: The programs whose classification encloses this one.

    Returns:
        The paint. A program that is not a reference, repeats, nests too deeply, finds
        the allowance spent or cannot be read is unclassified.
    """
    if (
        not is_object_reference(reference)
        or reference in seen
        or len(seen) >= _MAX_FORM_NESTING_LEVELS
        or document.allowance.remaining <= 0
    ):
        return _UNCLASSIFIED
    tokens = _program_tokens_within(document, reference)
    if tokens is None:
        return _UNCLASSIFIED
    paint = GlyphPaint()
    for index, token in enumerate(tokens):
        if token in _PAINT_OPERATORS:
            paint = paint._replace(vector=True)
        elif token == "BI":
            # Positive inline-image evidence survives while later content is unknown.
            paint = paint._replace(raster=True, incomplete=True)
        elif token == "Do":
            name = tokens[index - 1] if index else ""
            paint = paint.merged(_xobject_paint(document, name, resources, seen | {reference}))
        elif (
            token not in _SIMPLE_GLYPH_OPERATORS
            and not token.startswith("/")
            and not _NUMBER.fullmatch(token)
        ):
            paint = paint._replace(incomplete=True)
    return paint


def _xobject_paint(
    document: _Document, name: str, resources: object, seen: frozenset[str]
) -> GlyphPaint:
    """Classify what the XObject called ``name`` (the operand of a ``Do`` operator) paints.

    Args:
        document: The object graph and the token allowance.
        name: The XObject's resource name.
        resources: The resources the name is looked up in.
        seen: The programs whose classification encloses this call.

    Returns:
        The paint: raster for an image, the classification of a Form XObject's program,
        and unclassified for anything else.
    """
    if not name.startswith("/"):
        return _UNCLASSIFIED
    objects = document.objects
    try:
        xobjects = objects.dictionary(objects.dictionary(resources).get("/XObject"))
        child_reference = xobjects.get(name)
        child = objects.dictionary(child_reference)
    except PreparationError:
        return _UNCLASSIFIED
    if child.get("/Subtype") == "/Image":
        return GlyphPaint(raster=True)
    if child.get("/Subtype") == "/Form":
        return _glyph_program(document, child_reference, child.get("/Resources", resources), seen)
    return _UNCLASSIFIED


def _type3_finding(objects: QpdfObjectGraph, *, forbid_bitmap: bool) -> Finding:
    """Inventory and classify every reachable Type 3 glyph program.

    Args:
        objects: The object graph with the decoded programs.
        forbid_bitmap: Whether a program that paints an image is a violation.

    Returns:
        The Type 3 finding, with an inventory of at most ``MAX_SAMPLES`` programs.

    Raises:
        PreparationError: The fonts hold more than ``_MAX_GLYPH_PROGRAMS`` programs.
    """
    fonts = [item for item in objects.reachable_dictionaries() if item.get("/Subtype") == "/Type3"]
    inventory: list[object] = []
    violations: list[object] = []
    uncertain = False
    count = 0
    document = _Document(objects, _TokenAllowance())
    for index, font in enumerate(fonts, 1):
        programs = objects.dictionary(font.get("/CharProcs"))
        if not programs:
            uncertain = True
        for name, reference in sorted(programs.items()):
            count += 1
            if count > _MAX_GLYPH_PROGRAMS:
                raise PreparationError("Type 3 font inventory exceeds the glyph-program limit")
            paint = _glyph_program(document, reference, font.get("/Resources", {}))
            row = {
                "font_index": index,
                "glyph": name,
                "classification": paint.classification,
                "incomplete": paint.incomplete,
            }
            if len(inventory) < MAX_SAMPLES:
                inventory.append(row)
            uncertain |= paint.incomplete
            if paint.raster and forbid_bitmap:
                violations.append(row)
    return measurement_finding(
        ArtworkRule.TYPE3_GLYPH_PROGRAMS,
        _type3_message(
            violations, count, len(fonts), uncertain=uncertain, forbid_bitmap=forbid_bitmap
        ),
        violations,
        incomplete=uncertain,
        details={
            "font_count": len(fonts),
            "glyph_count": count,
            "inventory": inventory,
            "forbidden_bitmap": forbid_bitmap,
            "scope": "Reachable declared glyph programs, including supported nested Form XObjects. "
            "Unused glyphs may be inventoried. Unsupported operators, compressed/opaque streams "
            "and inline-image tails retain uncertainty; Type 3 alone never implies bitmap glyphs.",
        },
    )


def _type3_message(
    violations: list[object], glyphs: int, fonts: int, *, uncertain: bool, forbid_bitmap: bool
) -> str:
    """Describe the outcome of the Type 3 inspection in the researcher's terms.

    Args:
        violations: The programs that paint a forbidden image.
        glyphs: The number of programs inspected.
        fonts: The number of Type 3 fonts.
        uncertain: Whether some programs could not be classified.
        forbid_bitmap: Whether bitmap glyphs are forbidden.

    Returns:
        The finding's message.
    """
    if violations:
        return (
            f"Type 3 glyph programs that paint bitmap images: {len(violations)} of {glyphs}. "
            "Re-export the fonts as outlines or scalable fonts."
        )
    if not fonts:
        return "The PDF declares no Type 3 fonts."
    summary = f"Inspected {plural(glyphs, 'glyph program')} in {plural(fonts, 'Type 3 font')}"
    if uncertain:
        return f"{summary}; some programs could not be classified (compressed or unsupported)."
    return f"{summary}{'; none paints a bitmap image' if forbid_bitmap else ''}."


def _decoded_type3_finding(
    execution: CommandResult,
    *,
    objects: QpdfObjectGraph,
    references: set[str],
    forbid_bitmap: bool,
) -> Finding:
    """Merge the decoded programs into ``objects`` and classify them.

    Args:
        execution: The result of the qpdf command that decoded the programs.
        objects: The object graph without stream data.
        references: The programs and Form XObjects that were decoded.
        forbid_bitmap: Whether a program that paints an image is a violation.

    Returns:
        The Type 3 finding.

    Raises:
        PreparationError: qpdf did not return every selected program.
    """
    failure = "qpdf did not return every selected glyph/Form program"
    decoded = parse_qpdf_json_v2(execution.stdout, failure).objects
    for reference in references:
        key = "obj:" + reference
        if not isinstance(decoded.get(key), dict):
            raise PreparationError(failure)
        objects.objects[key] = decoded[key]
    return _type3_finding(objects, forbid_bitmap=forbid_bitmap)


def _glyph_program_references(objects: QpdfObjectGraph) -> set[str]:
    """Collect the references of every reachable Type 3 glyph program, then of every Form XObject.

    Form programs are included for supported nested ``Do`` calls. Image pixels and
    font-file streams are never requested: decoding every PDF stream can expand a small
    image-heavy paper far beyond the diagnostic output budget.

    Returns:
        The references; empty, without any Form XObject, when there is no glyph program.
    """
    references: set[str] = set()
    for font in objects.reachable_dictionaries():
        if font.get("/Subtype") == "/Type3":
            for value in objects.dictionary(font.get("/CharProcs")).values():
                if is_object_reference(value):
                    references.add(value)
    if not references:
        return references
    for key, entry in objects.objects.items():
        if (
            isinstance(entry, dict)
            and isinstance(entry.get("stream"), dict)
            and isinstance(entry["stream"].get("dict"), dict)
            and entry["stream"]["dict"].get("/Subtype") == "/Form"
        ):
            reference = key.removeprefix("obj:")
            if is_object_reference(reference):
                references.add(reference)
    return references


async def inspect_type3_glyphs(
    session: PdfToolSession, pages: int, *, forbid_bitmap: bool
) -> Finding:
    """Classify the painting of the document's declared Type 3 glyph programs.

    Args:
        session: The session over the frozen document.
        pages: The number of pages.
        forbid_bitmap: Whether a program that paints an image is a violation.

    Returns:
        The Type 3 finding.

    Raises:
        PreparationError: The object data cannot be read, or too many programs would be
            decoded.
    """
    objects = await session.measure(
        "glyph-objects",
        qpdf_objects_argv(),
        lambda execution: QpdfObjectGraph(execution.stdout, pages),
    )
    references = _glyph_program_references(objects)
    if not references:
        return _type3_finding(objects, forbid_bitmap=forbid_bitmap)
    if len(references) > _MAX_DECODED_OBJECTS:
        raise PreparationError("Type 3 program selection exceeds the bounded object limit")
    return await session.measure(
        "glyph-programs",
        qpdf_decoded_objects_argv(references),
        partial(
            _decoded_type3_finding,
            objects=objects,
            references=references,
            forbid_bitmap=forbid_bitmap,
        ),
    )
