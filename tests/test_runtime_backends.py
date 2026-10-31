"""Tool runtimes and backends: resource grants, sandboxes, the Biber toolchain and bibliographies.

TeX Live and Homebrew installations grant only their own directories, the process table
parsers refuse malformed listings, the Bubblewrap sandbox mounts only what a command needs, a
universal Biber image is split and sealed in-process, and builds prepare and check the
bibliography backend that the sources ask for.
"""

from __future__ import annotations

import json
import shutil
import stat
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from latexprep.biber_toolchain import (
    BiberToolchain,
    create_biber_toolchain,
    host_macho_slice,
    seal_toolchain,
)
from latexprep.build_bibliography import sources_may_use_biber
from latexprep.command_runner import CommandResult
from latexprep.errors import PreparationError, SandboxRefusedError, ToolUnavailableError
from latexprep.file_safety import DirectoryIdentity, regular_file_identity
from latexprep.latexmk_build import build_project
from latexprep.process_table import ProcessSample, parse_ancestry_rows, parse_group_rows
from latexprep.resource_monitor import group_usage
from latexprep.runtime import ToolRunner
from latexprep.sandbox import BubblewrapSandbox, SeatbeltSandbox, UnavailableSandbox
from latexprep.tool_launch import create_tool_environment
from latexprep.toolchain import ToolContext, distribution_roots
from tests.fakes import FakeBuildRunner
from tests.support import live_test

_BCF = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<bcf:controlfile xmlns:bcf="https://sourceforge.net/projects/biblatex" version="3.11"/>'
)


class TeXLiveRuntimeTests(unittest.TestCase):
    """Resource grants and TeX lookup variables for TeX Live distribution directories.

    A full or basic distribution directory (``2026``, ``2026basic``) grants its whole tree and
    sets the ``TEXMF*`` variables; any other directory name grants only the executable.
    """

    def test_full_and_basic_distributions_have_scoped_resources_and_tex_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            workspace = base / "workspace"
            workspace.mkdir()
            for name in ("2026", "2026basic"):
                with self.subTest(distribution=name):
                    distribution = base / "texlive" / name
                    executable = distribution / "bin/universal-darwin/pdftex"
                    executable.parent.mkdir(parents=True)
                    executable.write_bytes(b"installed engine fixture")
                    engine = executable.with_name("pdflatex")
                    engine.symlink_to(executable)
                    roots = distribution_roots(engine)
                    self.assertEqual(roots, {executable, distribution})
                    context = ToolContext(
                        engine, frozenset({engine}), frozenset(roots), (str(engine.parent),)
                    )
                    environment = create_tool_environment(
                        workspace, context, source_date_epoch="0"
                    ).variables
                    self.assertEqual(
                        environment["TEXMFCNF"], str(distribution / "texmf-dist/web2c")
                    )
                    self.assertEqual(
                        environment["TEXMFSYSCONFIG"], str(distribution / "texmf-config")
                    )
                    self.assertEqual(environment["TEXMFSYSVAR"], str(distribution / "texmf-var"))
                    self.assertIn(str(distribution / "texmf-dist"), environment["TEXMF"])
                    self.assertTrue(Path(environment["TEXMFHOME"]).is_relative_to(workspace))

    def test_other_directory_names_do_not_expand_distribution_grants(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            workspace = base / "workspace"
            workspace.mkdir()
            for name in ("2026basic-extra", "2026custom", "202", "20260", "２０２６"):
                with self.subTest(directory=name):
                    distribution = base / "texlive" / name
                    executable = distribution / "bin/pdftex"
                    self.assertEqual(distribution_roots(executable), {executable})
                    context = ToolContext(
                        executable, frozenset({executable}), frozenset({distribution}), ()
                    )
                    environment = create_tool_environment(
                        workspace, context, source_date_epoch="0"
                    ).variables
                    self.assertNotIn("TEXMFCNF", environment)


class ProcessTableTests(unittest.TestCase):
    """Process group listings, and the process columns and tool grants of each sandbox."""

    def test_group_rows_are_read_as_listed_and_malformed_listings_are_refused(self) -> None:
        samples = parse_group_rows(b"  10  10  2048\n  11  10   512\ntoo few\n")
        self.assertEqual(samples, [ProcessSample(10, 10, 2048), ProcessSample(11, 10, 512)])
        for malformed in (b"", b"1 2\n", b"1 two 3\n"):
            with self.subTest(malformed=malformed), self.assertRaises(PreparationError):
                parse_group_rows(malformed)

    def test_each_backend_asks_for_the_columns_its_isolation_needs(self) -> None:
        self.assertEqual(SeatbeltSandbox().process_columns, "pid=,pgid=,rss=")
        self.assertEqual(BubblewrapSandbox().process_columns, "pid=,ppid=,pgid=,rss=")
        self.assertEqual(UnavailableSandbox().process_columns, SeatbeltSandbox().process_columns)
        self.assertTrue(UnavailableSandbox().declares_tool(Path("/anywhere"), Path("/anywhere")))
        self.assertTrue(SeatbeltSandbox().declares_tool(Path("/anywhere"), Path("/anywhere")))


class HomebrewRuntimeTests(unittest.TestCase):
    """Resource grants for Homebrew packages, derived from their install receipts.

    A package grants the runtime dependencies its receipt declares, including an upgraded alias
    under ``opt``, but never the prefix, the Cellar or a dependency link that leads elsewhere; a
    receipt that is not a JSON object grants only the package itself.
    """

    def test_declared_upgraded_nss_alias_is_granted_without_broad_homebrew_reads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory).resolve()
            cellar = prefix / "Cellar"
            poppler = cellar / "poppler" / "26.10.0"
            nss = cellar / "nss" / "3.131"
            nspr = cellar / "nspr" / "4.40"
            for package in (poppler, nss, nspr):
                package.mkdir(parents=True)
            (prefix / "opt").mkdir()
            for package in (poppler, nss, nspr):
                (prefix / "opt" / package.parent.name).symlink_to(package, target_is_directory=True)
            (poppler / "INSTALL_RECEIPT.json").write_text(
                json.dumps({"runtime_dependencies": [{"full_name": "nss", "pkg_version": "3.130"}]})
            )
            (nss / "INSTALL_RECEIPT.json").write_text(
                json.dumps({"runtime_dependencies": [{"full_name": "nspr", "version": "4.40"}]})
            )
            binary = poppler / "bin" / "pdfinfo"
            binary.parent.mkdir()
            binary.write_bytes(b"installed tool fixture")
            roots = distribution_roots(binary)
            self.assertIn(nss, roots)
            self.assertIn(prefix / "opt/nss", roots)
            self.assertIn(nspr, roots)
            for broad in (prefix, cellar, prefix / "opt", cellar / "nss"):
                self.assertNotIn(broad, roots)
            outside = prefix / "private"
            outside.mkdir()
            (prefix / "opt/nss").unlink()
            (prefix / "opt/nss").symlink_to(outside, target_is_directory=True)
            roots = distribution_roots(binary)
            self.assertNotIn(outside, roots)
            self.assertNotIn(prefix / "opt/nss", roots)

    def test_receipt_that_is_not_a_json_object_grants_only_its_own_package(self) -> None:
        for content in ("[]", '"text"', "7", "null", "not json"):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as directory:
                prefix = Path(directory).resolve()
                poppler = prefix / "Cellar" / "poppler" / "26.10.0"
                binary = poppler / "bin" / "pdfinfo"
                binary.parent.mkdir(parents=True)
                binary.write_bytes(b"installed tool fixture")
                (poppler / "INSTALL_RECEIPT.json").write_text(content)
                roots = distribution_roots(binary)
                self.assertIn(poppler, roots)
                self.assertNotIn(prefix / "Cellar", roots)


class LinuxSandboxTests(unittest.IsolatedAsyncioTestCase):
    """The Bubblewrap sandbox on Linux: mounts, refusals, process accounting and a live check."""

    async def asyncSetUp(self) -> None:
        """Create a temporary workspace and a Perl tool context with declared system paths."""
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.context = ToolContext(
            Path("/usr/bin/perl"),
            frozenset({Path("/usr/bin/perl"), Path("/bin/sh")}),
            frozenset({Path("/usr/lib")}),
            ("/usr/bin", "/bin"),
        )

    async def asyncTearDown(self) -> None:
        """Remove the temporary directory."""
        self.temporary.cleanup()

    def sandbox_command(
        self,
        runner: ToolRunner,
        inputs: tuple[Path, ...] = (),
        context: ToolContext | None = None,
        workspace: Path | None = None,
    ) -> list[str]:
        """Build the Bubblewrap command line that would run ``perl -v`` through ``runner``.

        The runner gets a :class:`BubblewrapSandbox`, ``bwrap`` is found at ``/usr/bin/bwrap`` and
        the toolchain reports ``context`` for the tool. Refusals of the sandbox propagate.

        Args:
            runner: The runner whose private ``_sandbox_command`` builds the command; its sandbox is
                replaced.
            inputs: The read-only inputs to grant.
            context: The tool context to report, or ``None`` for the one ``asyncSetUp`` built.
            workspace: The writable workspace, or ``None`` for the test's workspace.

        Returns:
            The wrapped command line.
        """
        runner.sandbox = BubblewrapSandbox()
        with (
            patch("latexprep.sandbox.shutil.which", return_value="/usr/bin/bwrap"),
            patch.object(runner.toolchain, "context", return_value=context or self.context),
        ):
            return runner._sandbox_command(
                ["/usr/bin/perl", "-v"], workspace or self.workspace, inputs
            )

    def test_linux_mounts_only_leaf_writes_and_exact_frozen_inputs(self) -> None:
        runner = ToolRunner()
        source = self.root / "input.pdf"
        source.write_bytes(b"%PDF-exact-input")
        frozen = runner.freeze_input(source, self.root / "frozen")
        command = self.sandbox_command(runner, (frozen,))
        self.assertEqual(command[0], "/usr/bin/bwrap")
        for option in (
            "--unshare-user",
            "--unshare-net",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-uts",
            "--unshare-cgroup",
            "--disable-userns",
            "--die-with-parent",
            "--new-session",
        ):
            self.assertIn(option, command)
        writes = [
            command[index + 1 : index + 3] for index, arg in enumerate(command) if arg == "--bind"
        ]
        reads = [
            command[index + 1 : index + 3]
            for index, arg in enumerate(command)
            if arg == "--ro-bind"
        ]
        self.assertEqual(writes, [[str(self.workspace), str(self.workspace)]])
        self.assertIn([str(frozen), str(frozen)], reads)
        for broad in (str(frozen.parent), str(self.root), "/", "/home", "/etc", "/usr/bin"):
            self.assertNotIn([broad, broad], reads)
        self.assertNotIn("--share-net", command)
        self.assertNotIn("--unshare-user-try", command)
        self.assertEqual(command[-4:], ["/", "--", "/usr/bin/perl", "-v"])
        with self.assertRaisesRegex(PreparationError, "exact runtime-frozen"):
            self.sandbox_command(runner, (source,))
        frozen.chmod(0o600)
        frozen.write_bytes(b"changed")
        with self.assertRaisesRegex(PreparationError, "modified or replaced"):
            self.sandbox_command(runner, (frozen,))

    def test_linux_rejects_private_executables_and_broad_resources(self) -> None:
        runner = ToolRunner()
        private = self.root / "private-program"
        private.write_text("private")
        private_context = ToolContext(private, frozenset({private}), frozenset(), ())
        with self.assertRaisesRegex(SandboxRefusedError, "private-home and project"):
            self.sandbox_command(runner, context=private_context)
        broad = ToolContext(
            self.context.executable, self.context.executables, frozenset({Path("/")}), ()
        )
        with self.assertRaisesRegex(SandboxRefusedError, "declared system paths"):
            self.sandbox_command(runner, context=broad)
        with self.assertRaisesRegex(SandboxRefusedError, "overlap"):
            self.sandbox_command(runner, workspace=Path("/usr/lib/job"))

    def test_a_platform_without_a_backend_refuses_every_command(self) -> None:
        with self.assertRaisesRegex(SandboxRefusedError, "unavailable on this platform") as error:
            UnavailableSandbox().wrap(
                ["/usr/bin/perl", "-v"],
                self.workspace,
                context=self.context,
                readonly_inputs=(),
                biber=None,
            )
        # A refusal is an expected failure that no caller may answer by running unrestricted.
        self.assertIsInstance(error.exception, PreparationError)

    async def test_missing_linux_backend_never_launches_command(self) -> None:
        runner = ToolRunner()
        runner.sandbox = BubblewrapSandbox()
        execute = AsyncMock()
        with (
            patch("latexprep.sandbox.shutil.which", return_value=None),
            patch.object(runner, "_resolve_tool", return_value=Path("/usr/bin/perl")),
            patch.object(runner.toolchain, "context", return_value=self.context),
            patch.object(runner, "_execute", execute),
            self.assertRaisesRegex(SandboxRefusedError, "No unrestricted fallback"),
        ):
            await runner.run(["perl", "-e", 'print "must not run"'], self.workspace, self.workspace)
        execute.assert_not_awaited()

    def test_linux_descendants_keep_memory_accounting_after_new_session(self) -> None:
        rows = parse_ancestry_rows(
            b"90 1 90 100\n100 90 100 10\n101 100 101 600\n"
            b"102 101 101 500\n103 102 777 100\n200 90 200 40\n",
            {100},
        )
        self.assertEqual(
            [group for pid, group, _ in rows if pid in {100, 101, 102, 103}], [100] * 4
        )
        memory, failure = group_usage(rows, 100, 1)
        self.assertEqual(memory, 1210)
        self.assertIn("exceeded 1 MiB", failure or "")
        for malformed in (b"", b"1 2 3\n", b"1 invalid 1 1\n", b"1 0 1 -2\n"):
            with self.subTest(malformed=malformed), self.assertRaises(PreparationError):
                parse_ancestry_rows(malformed, {1})

    @live_test(
        "opt-in live Linux isolation check; requires operational Bubblewrap user namespaces",
        "LATEXPREP_RUN_SANDBOX_TESTS",
        platform="linux",
    )
    async def test_live_linux_denies_sibling_reads_and_writes(self) -> None:
        perl = shutil.which("perl")
        self.assertIsNotNone(perl)
        outside = self.root / "private.txt"
        outside.write_text("private")
        runner = ToolRunner()
        program = (
            'my $f; print(open($f,"<",$ARGV[0]) ? "READ" : "DENIED"); '
            'print(open($f,">",$ARGV[1]) ? " WRITE" : " BLOCKED");'
        )
        result = await runner.run(
            [str(perl), "-e", program, str(outside), str(self.root / "outside-write")],
            self.workspace,
            self.workspace,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "DENIED BLOCKED")
        self.assertFalse((self.root / "outside-write").exists())


class ToolIsolationTests(unittest.IsolatedAsyncioTestCase):
    """The private Biber toolchain: host slice, per-preparation directory, sealing and grants.

    A universal Biber image is split in-process rather than with ``lipo``, from either
    container width, while unusable containers and unsupported hosts are refused and thin
    images pass through. Each preparation copies Biber into its own private, inert
    directory, refusing an oversized Biber or a path with unsafe characters; sealing leaves
    no writable entry and refuses links; and on macOS only commands that may run Biber
    receive Seatbelt grants for the executable and its payload, which must not have been
    replaced.
    """

    @staticmethod
    def universal(entries: tuple[tuple[int, bytes], ...], *, wide: bool = False) -> bytes:
        """Build a Mach-O universal container that holds ``entries``.

        Args:
            entries: Pairs of CPU type and slice image, in container order.
            wide: Whether to use the 64-bit layout (magic ``0xCAFEBABF``) instead of the 32-bit one
                (``0xCAFEBABE``).

        Returns:
            The container: its header, one architecture record per entry, then the images.
        """
        width = 32 if wide else 20
        records = payload = b""
        start = 8 + len(entries) * width
        for cpu_type, image in entries:
            offset = start + len(payload)
            records += (
                struct.pack(">IIQQII", cpu_type, 0, offset, len(image), 14, 0)
                if wide
                else struct.pack(">IIIII", cpu_type, 0, offset, len(image), 14)
            )
            payload += image
        magic = 0xCAFEBABF if wide else 0xCAFEBABE
        return struct.pack(">II", magic, len(entries)) + records + payload

    def test_host_slice_is_extracted_from_both_container_widths(self) -> None:
        arm, intel = b"arm64 slice image", b"x86_64 slice image"
        for wide in (False, True):
            image = self.universal(((0x01000007, intel), (0x0100000C, arm)), wide=wide)
            for machine, expected in (("arm64", arm), ("x86_64", intel)):
                with (
                    self.subTest(wide=wide, machine=machine),
                    patch("latexprep.biber_toolchain.platform.machine", return_value=machine),
                ):
                    self.assertEqual(host_macho_slice(image), expected)

    def test_unusable_containers_are_refused_and_thin_images_pass_through(self) -> None:
        thin = b"\xcf\xfa\xed\xfe single architecture executable"
        self.assertEqual(host_macho_slice(thin), thin)
        with patch("latexprep.biber_toolchain.platform.machine", return_value="arm64"):
            for description, image in (
                ("absent host slice", self.universal(((0x01000007, b"x86_64 only"),))),
                ("truncated records", struct.pack(">II", 0xCAFEBABE, 4) + b"\x01\x00\x00\x0c"),
                ("empty container", struct.pack(">II", 0xCAFEBABE, 0)),
                ("short image", b"\xca\xfe\xba\xbe"),
            ):
                with self.subTest(description=description), self.assertRaises(PreparationError):
                    host_macho_slice(image)
            escaping = bytearray(self.universal(((0x0100000C, b"arm64 slice image"),)))
            escaping[16:20] = (len(escaping) - 4).to_bytes(4, "big")
            with self.assertRaisesRegex(PreparationError, "outside its container"):
                host_macho_slice(bytes(escaping))
        with (
            patch("latexprep.biber_toolchain.platform.machine", return_value="riscv64"),
            self.assertRaisesRegex(PreparationError, "Unsupported host architecture"),
        ):
            host_macho_slice(self.universal(((0x0100000C, b"arm64 slice image"),)))

    def test_each_preparation_gets_its_own_private_inert_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / "installed-biber"
            source.write_bytes(b"\xcf\xfa\xed\xfe single architecture executable")
            first = create_biber_toolchain(source, root / ".toolchain", max_file_bytes=1024)
            second = create_biber_toolchain(source, root / ".toolchain", max_file_bytes=1024)
            self.assertNotEqual(first.executable.parent, second.executable.parent)
            for toolchain in (first, second):
                self.assertEqual(toolchain.executable.read_bytes(), source.read_bytes())
                self.assertEqual(stat.S_IMODE(toolchain.executable.stat().st_mode), 0o500)
                self.assertEqual(stat.S_IMODE(toolchain.payload.stat().st_mode), 0o700)
                self.assertRegex(toolchain.executable.parent.name, r"^biber-[a-z0-9_]+$")
            with self.assertRaisesRegex(PreparationError, "file size limit"):
                create_biber_toolchain(source, root / ".toolchain", max_file_bytes=8)
            (root / "odd name").mkdir()
            with self.assertRaisesRegex(PreparationError, "letters, digits"):
                create_biber_toolchain(source, root / "odd name", max_file_bytes=1024)

    def test_sealing_leaves_no_writable_entry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve() / "biber-0"
            (base / "payload/inner").mkdir(parents=True)
            (base / "biber").write_bytes(b"thin image")
            (base / "biber").chmod(0o700)
            (base / "payload/inner/module.pm").write_bytes(b"payload module")
            seal_toolchain(base)
            for path in (base, *base.rglob("*")):
                with self.subTest(path=path.name):
                    self.assertFalse(path.stat().st_mode & 0o222, path)
            self.assertTrue((base / "biber").stat().st_mode & 0o100)
            (base / "payload").chmod(0o700)
            (base / "payload/link").symlink_to(base / "biber")
            with self.assertRaisesRegex(PreparationError, "unsupported entry"):
                seal_toolchain(base)

    @unittest.skipUnless(sys.platform == "darwin", "Seatbelt profiles are macOS-only")
    async def test_only_commands_that_may_run_biber_receive_payload_grants(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            base, workspace = root / ".toolchain/biber-0", root / "work"
            base.mkdir(parents=True)
            workspace.mkdir()
            executable, payload = base / "biber", base / "payload"
            executable.write_bytes(b"thin biber image")
            executable.chmod(0o500)
            payload.mkdir()
            (payload / "libperl.dylib").write_bytes(b"extracted payload")
            runner = ToolRunner()
            runner._biber.toolchain = BiberToolchain(
                executable,
                payload,
                regular_file_identity(executable),
                DirectoryIdentity.from_stat(payload.stat()),
            )
            profile = runner._sandbox_command(
                ["/bin/sh", "-e", f"$biber='{executable} --noconf %O %B';"], workspace
            )[2]
            for rule in ("process-exec", "file-read*", "file-map-executable"):
                self.assertIn(
                    f"(allow {rule} (literal {json.dumps(str(executable))}) "
                    f"(subpath {json.dumps(str(payload))}))",
                    profile,
                )
            blocked = runner._sandbox_command(
                ["/bin/sh", "-e", "$biber='internal fledge_backend_blocked';"], workspace
            )[2]
            self.assertNotIn(str(payload), blocked)
            self.assertNotIn(str(executable), blocked)
            self.assertNotIn("(allow process-exec (literal", blocked.rsplit("\n", 1)[-1])
            shutil.rmtree(payload)
            payload.mkdir()
            with self.assertRaisesRegex(PreparationError, "payload was replaced"):
                runner._sandbox_command(["/bin/sh", "-e", str(executable)], workspace)


class ControlBuildRunner(FakeBuildRunner):
    """A fake build runner that also writes bibliography control files and can lack one tool.

    Attributes:
        controls: File contents by name, relative to the workspace's ``output`` directory, that
            every build writes after the faked latexmk output.
        unavailable: The tool whose commands raise :class:`ToolUnavailableError`, or ``None``.
    """

    def __init__(self, controls: dict[str, str], unavailable: str | None = None) -> None:
        super().__init__()
        self.controls = controls
        self.unavailable = unavailable

    async def run(
        self,
        argv: list[str],
        cwd: Path,
        workspace: Path,
        *,
        readonly_inputs: tuple[Path, ...] = (),
        memory_mb: int | None = None,
    ) -> CommandResult:
        """Fake a command and, for a build, write the control files.

        The arguments are those of :meth:`CommandRunner.run`. A last argument ending in ``.tex``
        marks a build, whose control files go to the ``output`` directory of ``workspace``;
        ``readonly_inputs`` and ``memory_mb`` are not passed on to the faked run.

        Returns:
            The result of :meth:`FakeBuildRunner.run`.

        Raises:
            ToolUnavailableError: ``argv[0]`` is the tool named by ``unavailable``.
        """
        if argv[0] == self.unavailable:
            raise ToolUnavailableError(
                f"Required tool is unavailable: {self.unavailable}", self.unavailable
            )
        result = await super().run(argv, cwd, workspace)
        if argv[-1].endswith(".tex"):
            for name, text in self.controls.items():
                path = workspace / "output" / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
        return result


class UnpreparedBiberRunner(ControlBuildRunner):
    """Acts like macOS, where Biber runs only after its toolchain copy is prepared.

    Attributes:
        controls: The control files every build writes, as for :class:`ControlBuildRunner`.
        prepared: Whether :meth:`prepare_biber` has run.
    """

    def __init__(self, controls: dict[str, str]) -> None:
        super().__init__(controls)
        self.prepared = False

    @property
    def biber_needs_preparation(self) -> bool:
        """Whether Biber still needs its toolchain copy, true until :meth:`prepare_biber` runs."""
        return not self.prepared

    async def prepare_biber(self, root: Path) -> Path | None:
        """Count the preparation and mark Biber as prepared without copying anything.

        Args:
            root: Accepted for the interface and not used.

        Returns:
            ``None``, as no toolchain copy is made.
        """
        self.biber_preparations += 1
        self.prepared = True
        return None


class DeferredBiberTests(unittest.IsolatedAsyncioTestCase):
    """Deferred Biber preparation under the automatic bibliography strategy.

    A host that must prepare Biber before running it prepares it only when the sources may use
    biblatex, when its toolchain copy is already ready, or when a fresh ``.bcf`` from a deferred
    build asks for Biber; the biblatex hint reads only TeX sources.
    """

    async def asyncSetUp(self) -> None:
        """Create a temporary ``source`` folder whose ``main.tex`` names a bibliography."""
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "main.tex").write_text("\\bibliography{references}\n")

    async def asyncTearDown(self) -> None:
        """Remove the temporary directory."""
        self.temporary.cleanup()

    @staticmethod
    def build_rules(runner: FakeBuildRunner) -> list[str]:
        """Return the ``-e`` rules argument of each build command ``runner`` recorded, in order.

        A build command is one whose last argument ends in ``.tex``.
        """
        builds = [command for command in runner.commands if command[-1].endswith(".tex")]
        return [command[command.index("-e") + 1] for command in builds]

    async def test_bibtex_source_never_prepares_biber_under_the_automatic_strategy(self) -> None:
        runner = UnpreparedBiberRunner({"main.aux": "\\bibdata{references}\n"})
        result = await build_project(
            self.source, "main.tex", self.root / "build", "pdflatex", runner
        )
        self.assertTrue(result.success, result.findings)
        self.assertEqual(runner.biber_preparations, 0)
        [rules] = self.build_rules(runner)
        self.assertIn("$biber='internal fledge_biber_deferred'", rules)
        self.assertNotIn("--noconf", rules)
        bibliography = result.tools.bibliography
        assert bibliography is not None
        self.assertIn("no source mentions biblatex", bibliography.biber_preparation or "")

    async def test_fresh_bcf_from_a_deferred_build_rebuilds_with_biber_prepared(self) -> None:
        runner = UnpreparedBiberRunner({"main.bcf": _BCF})
        work = self.root / "build"
        result = await build_project(self.source, "main.tex", work, "pdflatex", runner)
        self.assertTrue(result.success, result.findings)
        self.assertEqual(runner.biber_preparations, 1)
        deferred, prepared = self.build_rules(runner)
        self.assertIn("fledge_biber_deferred", deferred)
        self.assertIn("$biber='biber --noconf %O %B'", prepared)
        self.assertNotIn("fledge_biber_deferred", prepared)
        self.assertEqual(result.pdf, work.resolve() / "output" / "main.pdf")
        bibliography = result.tools.bibliography
        assert bibliography is not None
        self.assertIn("that build was discarded", bibliography.biber_preparation or "")
        self.assertIn("biber", result.tools.versions)

    async def test_biblatex_sources_or_a_ready_toolchain_prepare_biber_before_one_build(
        self,
    ) -> None:
        (self.source / "local.sty").write_text("\\RequirePackage[backend=biber]{biblatex}\n")
        runner = UnpreparedBiberRunner({"main.bcf": _BCF})
        result = await build_project(
            self.source, "main.tex", self.root / "first", "pdflatex", runner
        )
        self.assertTrue(result.success, result.findings)
        self.assertEqual(runner.biber_preparations, 1)
        [rules] = self.build_rules(runner)
        self.assertIn("$biber='biber --noconf %O %B'", rules)
        bibliography = result.tools.bibliography
        assert bibliography is not None
        self.assertIsNone(bibliography.biber_preparation)
        self.assertNotIn("biber_preparation", bibliography.to_report())
        (self.source / "local.sty").unlink()
        runner.commands.clear()
        await build_project(self.source, "main.tex", self.root / "second", "pdflatex", runner)
        [rules] = self.build_rules(runner)
        self.assertNotIn("fledge_biber_deferred", rules)

    def test_biblatex_hint_reads_only_tex_sources_and_answers_yes_when_uncertain(self) -> None:
        self.assertFalse(sources_may_use_biber(self.source))
        (self.source / "references.bib").write_text("% generated for biblatex\n")
        (self.source / "notes.md").write_text("biblatex")
        self.assertFalse(sources_may_use_biber(self.source))
        (self.source / "linked.sty").symlink_to(self.source / "notes.md")
        self.assertFalse(sources_may_use_biber(self.source))
        self.assertTrue(sources_may_use_biber(self.source, max_bytes=4))
        (self.source / "style").mkdir()
        (self.source / "style" / "paper.cls").write_text("\\RequirePackage{biblatex}\n")
        self.assertTrue(sources_may_use_biber(self.source))


class BibliographyBackendTests(unittest.IsolatedAsyncioTestCase):
    """Bibliography backend selection, Biber preparation and the checks of control files.

    The requested backend (``bibtex``, ``biber`` or ``auto``) must match the fresh control files,
    the other backend is blocked, and an unavailable or unpreparable tool is reported as
    inconclusive instead of raising.
    """

    async def asyncSetUp(self) -> None:
        """Create a ``source`` folder with a placeholder ``main.tex``; reset the build count."""
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "main.tex").write_text("source")
        self.build_count = 0

    async def asyncTearDown(self) -> None:
        """Remove the temporary directory."""
        self.temporary.cleanup()

    async def build(self, backend: str, controls: dict[str, str], unavailable: str | None = None):
        """Build ``main.tex`` in a fresh directory with a :class:`ControlBuildRunner`.

        Args:
            backend: The requested ``bibliography_backend``.
            controls: The control files the runner writes, as for :class:`ControlBuildRunner`.
            unavailable: The tool the runner reports as unavailable, or ``None``.

        Returns:
            The build result and the runner that ran it.
        """
        self.build_count += 1
        runner = ControlBuildRunner(controls, unavailable)
        result = await build_project(
            self.source,
            "main.tex",
            self.root / f"build-{self.build_count}",
            "pdflatex",
            runner,
            bibliography_backend=backend,
        )
        return result, runner

    async def test_requested_backend_matches_fresh_controls(self) -> None:
        for backend, controls in (
            ("bibtex", {"main.aux": "\\relax\n\\bibdata{references}\n"}),
            ("biber", {"main.bcf": _BCF, "main.aux": "\\relax\n"}),
            ("auto", {"main.bcf": _BCF, "sub/other.aux": "\\bibdata{references}\n"}),
        ):
            with self.subTest(backend=backend):
                result, runner = await self.build(backend, controls)
                self.assertTrue(result.success, result.findings)
                finding = next(
                    item for item in result.findings if item.rule == "build.bibliography_backend"
                )
                self.assertEqual(finding.code, "BLD201")
                self.assertEqual(finding.status, "passed")
                self.assertEqual(finding.details["requested_backend"], backend)
                build = next(command for command in runner.commands if command[-1].endswith(".tex"))
                rules = build[build.index("-e") + 1]
                if backend != "auto":
                    other = "biber" if backend == "bibtex" else "bibtex"
                    self.assertIn(f"${other}='internal fledge_backend_blocked'", rules)
                    self.assertIn("return 1", rules)
                else:
                    self.assertNotIn("fledge_backend_blocked", rules)
        result, _ = await self.build("bibtex", {"main.aux": "\\relax\n"})
        finding = next(
            item for item in result.findings if item.rule == "build.bibliography_backend"
        )
        self.assertTrue(result.success)
        self.assertEqual(finding.status, "not_applicable")

    async def test_bibtex_strategy_never_prepares_or_names_the_biber_payload(self) -> None:
        for backend, preparations in (("bibtex", 0), ("auto", 1), ("biber", 1)):
            with self.subTest(backend=backend):
                _, runner = await self.build(backend, {"main.aux": "\\relax\n"})
                self.assertEqual(runner.biber_preparations, preparations)
                build = next(command for command in runner.commands if command[-1].endswith(".tex"))
                rules = build[build.index("-e") + 1]
                self.assertEqual("$biber='biber --noconf %O %B'" in rules, backend != "bibtex")
                if backend == "bibtex":
                    self.assertIn("$biber='internal fledge_backend_blocked'", rules)
                    self.assertNotIn("--noconf", rules)

    async def test_unpreparable_biber_is_reported_instead_of_raising(self) -> None:
        self.build_count += 1
        runner = ControlBuildRunner({"main.bcf": _BCF})
        with patch.object(
            runner,
            "prepare_biber",
            AsyncMock(side_effect=PreparationError("no arm64 slice")),
        ):
            result = await build_project(
                self.source,
                "main.tex",
                self.root / f"build-{self.build_count}",
                "pdflatex",
                runner,
                bibliography_backend="biber",
            )
        tool = next(item for item in result.findings if item.rule == "build.bibliography_tool")
        self.assertEqual((tool.code, tool.status), ("BLD202", "inconclusive"))
        self.assertIn("no arm64 slice", tool.message)
        self.assertFalse(result.success)

    async def test_mismatching_and_malformed_controls_block(self) -> None:
        for backend, controls, status in (
            ("bibtex", {"main.bcf": _BCF}, "failed"),
            ("biber", {"main.aux": "\\bibdata{refs}"}, "failed"),
            ("auto", {"main.bcf": "broken XML"}, "inconclusive"),
            ("biber", {"main.bcf": "<!DOCTYPE doc><controlfile/>"}, "inconclusive"),
            ("bibtex", {"main.aux": "\\bibdata{}"}, "inconclusive"),
        ):
            with self.subTest(backend=backend, controls=controls):
                result, _ = await self.build(backend, controls)
                self.assertFalse(result.success)
                self.assertIsNone(result.pdf)
                finding = next(
                    item for item in result.findings if item.rule == "build.bibliography_backend"
                )
                self.assertEqual(finding.code, "BLD201")
                self.assertEqual(finding.status, status)
        with self.assertRaisesRegex(
            PreparationError, "bibliography_backend must be one of auto, bibtex, biber"
        ):
            await self.build("bibtex;unexpected-command", {})

    async def test_required_backend_version_is_reported_and_unavailable_is_inconclusive(
        self,
    ) -> None:
        result, runner = await self.build("biber", {"main.bcf": _BCF})
        tool = next(item for item in result.findings if item.rule == "build.bibliography_tool")
        self.assertEqual(tool.code, "BLD202")
        self.assertEqual(tool.status, "passed")
        self.assertIn("biber", result.tools.versions)
        self.assertIn(["biber", "--noconf", "--version"], runner.commands)
        missing, _ = await self.build("biber", {"main.bcf": _BCF}, unavailable="biber")
        self.assertFalse(missing.success)
        tool = next(item for item in missing.findings if item.rule == "build.bibliography_tool")
        self.assertEqual(tool.code, "BLD202")
        self.assertEqual(tool.status, "inconclusive")


if __name__ == "__main__":
    unittest.main()
