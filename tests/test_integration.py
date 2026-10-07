"""Opt-in live checks: no tool doubles and no unsandboxed execution fallback."""

from __future__ import annotations

import asyncio
import os
import shutil
import struct
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path

from latexprep.config import Settings
from latexprep.core import JobRequest, run_job
from latexprep.pdf import PdfOptions, inspect_pdf
from latexprep.runtime import RuntimeLimits, ToolRunner


def write_png(path: Path, rgb: bytes) -> None:
    def chunk(kind: bytes, data: bytes) -> bytes:
        payload = kind + data
        return struct.pack(">I", len(data)) + payload + struct.pack(">I", zlib.crc32(payload))

    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\0" + rgb))
        + chunk(b"IEND", b"")
    )


@unittest.skipUnless(
    os.environ.get("LATEX_PREP_RUN_INTEGRATION") == "1",
    "Set LATEX_PREP_RUN_INTEGRATION=1 for live restricted-tool integration checks",
)
class LiveIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_nested_sources_bibliography_images_formatting_and_zip_rebuild(self):
        with tempfile.TemporaryDirectory(prefix="latex-prep-live-") as directory:
            base = Path(directory).resolve()
            source = base / "paper"
            fixture = Path(__file__).resolve().parents[1] / "examples/nested-paper"
            shutil.copytree(fixture, source)
            (source / "figures").mkdir()
            write_png(source / "figures/chart.png", b"\xff\x00\x00")
            write_png(source / "appendix/chart.png", b"\x00\x00\xff")
            main = source / "main.tex"
            main.write_text(
                main.read_text().replace(
                    "\\bibliographystyle",
                    "\\includegraphics[width=1cm]{figures/chart}\n"
                    "\\includegraphics[width=1cm]{appendix/chart}\n\\bibliographystyle",
                )
            )
            marker = base / "untrusted-config-executed"
            (source / ".latexmkrc").write_text(
                f'open(my $fh, ">", "{marker}") or die $!; print $fh "executed"; close $fh;\n'
            )
            originals = {
                path.relative_to(source): path.read_bytes()
                for path in source.rglob("*")
                if path.is_file()
            }
            report = await run_job(
                JobRequest(
                    "prepare",
                    source,
                    Settings(
                        layout="flat",
                        format=True,
                        expected_document_class="article",
                        require_abstract=True,
                        abstract_min_words=1,
                        abstract_max_words=100,
                        required_sections=("Results",),
                        max_pages=10,
                        expected_page_width_pt=612,
                        expected_page_height_pt=792,
                        require_consistent_page_size=True,
                        min_image_dpi=1,
                        require_embedded_fonts=True,
                        forbid_type3_fonts=True,
                        forbid_encryption=True,
                        forbid_forms=True,
                        forbid_javascript=True,
                        forbid_attachments=True,
                    ),
                    base / "prepared",
                )
            )
            self.assertIn(report.outcome, {"passed", "passed_with_advisories"}, report.to_dict())
            self.assertFalse(marker.exists())
            self.assertEqual(
                originals,
                {
                    path.relative_to(source): path.read_bytes()
                    for path in source.rglob("*")
                    if path.is_file()
                },
            )
            with zipfile.ZipFile(base / "prepared/submission.zip") as archive:
                self.assertTrue(all("/" not in name for name in archive.namelist()))
                self.assertEqual(sum(name.endswith(".png") for name in archive.namelist()), 2)
                self.assertIn("main.tex", archive.namelist())
            self.assertTrue((base / "prepared/manuscript.pdf").read_bytes().startswith(b"%PDF-"))
            self.assertEqual(
                sum(
                    finding.status == "passed" and finding.rule == "compare.rendering"
                    for finding in report.findings
                ),
                2,
            )
            for code in ("PDF001", *(f"PDF{number}" for number in range(101, 110))):
                with self.subTest(code=code):
                    findings = [finding for finding in report.findings if finding.code == code]
                    self.assertEqual(len(findings), 2, code)
                    self.assertTrue(all(finding.status == "passed" for finding in findings))
            findings = await inspect_pdf(
                base / "prepared/manuscript.pdf",
                base / "strict-inspection",
                ToolRunner(),
                options=PdfOptions(min_image_dpi=300),
            )
            low_resolution = next(finding for finding in findings if finding.code == "PDF103")
            self.assertEqual(low_resolution.status, "failed")
            self.assertEqual(len(low_resolution.details["below_minimum"]), 2)

    async def test_confined_process_cannot_read_or_write_outside_its_workspace(self):
        with tempfile.TemporaryDirectory(prefix="latex-prep-confine-") as directory:
            base = Path(directory).resolve()
            workspace = base / "allowed"
            workspace.mkdir()
            secret = base / "not-a-toolchain-resource"
            secret.write_text("private fixture")
            runner = ToolRunner(RuntimeLimits(timeout_seconds=5))
            read = await runner.run(
                [
                    "/usr/bin/perl",
                    "-e",
                    'open(my $f, "<", $ARGV[0]) or exit 7; exit 0;',
                    str(secret),
                ],
                cwd=workspace,
                workspace=workspace,
            )
            self.assertEqual(read.returncode, 7, read)
            write = await runner.run(
                [
                    "/usr/bin/perl",
                    "-e",
                    'open(my $f, ">", $ARGV[0]) or exit 8; exit 0;',
                    str(secret),
                ],
                cwd=workspace,
                workspace=workspace,
            )
            self.assertEqual(write.returncode, 8, write)
            self.assertEqual(secret.read_text(), "private fixture")

    async def test_confined_process_cannot_connect_to_network(self):
        connected = False

        def accepted(reader, writer):
            nonlocal connected
            connected = True
            writer.close()

        server = await asyncio.start_server(accepted, "127.0.0.1", 0)
        try:
            port = server.sockets[0].getsockname()[1]
            with tempfile.TemporaryDirectory(prefix="latex-prep-network-") as directory:
                workspace = Path(directory).resolve()
                result = await ToolRunner(RuntimeLimits(timeout_seconds=5)).run(
                    [
                        "/usr/bin/perl",
                        "-MIO::Socket::INET",
                        "-e",
                        'my $s=IO::Socket::INET->new(PeerAddr=>"127.0.0.1",'
                        'PeerPort=>$ARGV[0],Proto=>"tcp",Timeout=>1); exit($s ? 0 : 9);',
                        str(port),
                    ],
                    cwd=workspace,
                    workspace=workspace,
                )
            self.assertEqual(result.returncode, 9, result)
            self.assertFalse(connected)
        finally:
            server.close()
            await server.wait_closed()


if __name__ == "__main__":
    unittest.main()
