"""Controlled Poppler output for tests of page ranges and of the PDF sessions a job shares.

``RangeRunner`` answers ``pdfinfo``, ``pdftotext`` and ``pdftoppm`` for a left and a right PDF
of any page count, renders single pages and ranges of pages the way ``pdftoppm`` names their
files, and can fail or thin out what a range leaves behind. ``multi_page_pdf`` writes a small
real PDF for the live tests.
"""

from __future__ import annotations

from tests.fakes import FakeCommandRunner, Reply, ToolCall


def _pixel(side: str, page: int, changed: tuple[int, ...]) -> bytes:
    """Return a one-pixel PPM page that is black for a changed page of the right side."""
    black = side == "right" and page in changed
    return b"P6\n1 1\n255\n" + (b"\0\0\0" if black else b"\xff\xff\xff")


class RangeRunner(FakeCommandRunner):
    """Poppler output for a left and a right PDF of ``pages`` pages that renders in ranges.

    ``pdftoppm`` writes ``page.ppm`` for a single page and ``page-NN.ppm`` for each page of a
    range, with as many digits as the page count has. The right PDF is the one whose bytes
    contain "right"; its pages in ``changed_pages`` render black.

    Args:
        pages: The page count of both PDFs.
        changed_pages: The pages of the right PDF that render differently.
        fail_ranges: Whether every multi-page render fails.
        omit_pages: The pages that a multi-page render leaves out.
        fail_pages: The pages whose single-page render fails.

    Attributes:
        renders: The side, first page, last page and whether it was a single page, of each
            render, in the order they were started.
    """

    def __init__(
        self,
        *,
        pages: int = 12,
        changed_pages: tuple[int, ...] = (),
        fail_ranges: bool = False,
        omit_pages: tuple[int, ...] = (),
        fail_pages: tuple[int, ...] = (),
    ) -> None:
        super().__init__()
        self.pages = pages
        self.changed_pages = changed_pages
        self.fail_ranges = fail_ranges
        self.omit_pages = omit_pages
        self.fail_pages = fail_pages
        self.renders: list[tuple[str, int, int, bool]] = []

    def respond(self, call: ToolCall) -> Reply:
        """Answer ``pdfinfo``, ``pdftotext`` and ``pdftoppm``; every other tool prints nothing."""
        right = any(b"right" in path.read_bytes() for path in call.readonly_inputs)
        side = "right" if right else "left"
        argv = call.argv
        if call.tool == "pdfinfo":
            output = f"Pages: {self.pages}\n"
            if "-box" in argv:
                output += "".join(
                    f"Page {page} size: 612 x 792 pts\n" for page in range(1, self.pages + 1)
                )
            return Reply(output)
        if call.tool == "pdftotext":
            return Reply("Text\n\f" * self.pages)
        if call.tool != "pdftoppm":
            return Reply()
        first, last = int(argv[argv.index("-f") + 1]), int(argv[argv.index("-l") + 1])
        single = "-singlefile" in argv
        self.renders.append((side, first, last, single))
        if single:
            if first in self.fail_pages:
                return Reply(stderr="Syntax Error: damaged page", returncode=1)
            return Reply(files={"page.ppm": _pixel(side, first, self.changed_pages)})
        if self.fail_ranges:
            return Reply(stderr="Syntax Error: damaged range", returncode=1)
        width = len(str(self.pages))
        return Reply(
            files={
                f"page-{page:0{width}d}.ppm": _pixel(side, page, self.changed_pages)
                for page in range(first, last + 1)
                if page not in self.omit_pages
            }
        )

    @property
    def single_renders(self) -> list[tuple[str, int]]:
        """The side and page of every single-page render."""
        return [(side, first) for side, first, _last, single in self.renders if single]

    @property
    def range_renders(self) -> list[tuple[str, int, int]]:
        """The side and pages of every multi-page render."""
        return [(side, first, last) for side, first, last, single in self.renders if not single]


def multi_page_pdf(pages: int, *, variant: int = 0) -> bytes:
    """Return a valid PDF of ``pages`` letter pages that each paint a different set of boxes.

    No font is used, so a renderer needs nothing from the host to draw it. A page's boxes depend
    on its number and ``variant``, so two PDFs with another ``variant`` differ on every page.

    Args:
        pages: The number of pages.
        variant: Changes what every page paints.

    Returns:
        The bytes of the PDF.
    """
    objects: list[bytes] = [b"<< /Type /Catalog /Pages 2 0 R >>"]
    kids = " ".join(f"{3 + 2 * index} 0 R" for index in range(pages))
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>".encode())
    for index in range(pages):
        boxes = "".join(
            f"{(index * 7 + step * 31 + variant * 13) % 100 / 100:.2f} "
            f"{(step * 17 + variant * 5) % 100 / 100:.2f} 0.5 rg "
            f"{40 + step * 40} {60 + (index * 23 + step * 50) % 500} "
            f"{30 + (index + step) % 5 * 20} {30 + step * 10} re f\n"
            for step in range(1, 6)
        )
        content = boxes.encode()
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Contents {4 + 2 * index} 0 R >>".encode()
        )
        objects.append(f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"endstream")
    document = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(document))
        document += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    table = len(document)
    document += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    document += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    document += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{table}\n%%EOF\n"
    ).encode()
    return bytes(document)
