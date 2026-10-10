"""Exercise the installer with fake external tools; never install system packages."""

from __future__ import annotations

import json
import os
import pty
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

INSTALLER = Path(__file__).resolve().parents[1] / "install.sh"
TEX_TOOLS = ("latexmk", "pdflatex", "bibtex")
PDF_TOOLS = (
    "pdfinfo",
    "pdftotext",
    "pdftoppm",
    "pdffonts",
    "pdfdetach",
    "pdfimages",
    "pdftohtml",
)
SYSTEM_DIRECTORIES = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")
INSTALLER_FALLBACKS = "local tool_dirs=(/Library/TeX/texbin /opt/homebrew/bin /usr/local/bin)"


def resolved_unique_directories(*directories):
    """Resolve directories in order and drop repeats, as the launcher does.

    On merged-/usr Linux, /bin and /sbin resolve to /usr/bin and /usr/sbin, and
    the launcher keeps only the first entry naming each directory.
    """
    return list(dict.fromkeys(os.path.realpath(directory) for directory in directories))


MOCK_PROGRAM = r"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

name = Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["MOCK_LOG"], "a") as stream:
    stream.write(json.dumps([name, *args]) + "\n")
if name == os.environ.get("MOCK_FAIL"):
    sys.exit(17)
if name.startswith("python"):
    if args[:1] == ["-c"]:
        sys.exit(1 if os.environ.get("MOCK_OLD_PYTHON") and name == "python3" else 0)
    if args[:2] == ["-m", "venv"]:
        target = Path(args[2]) / "bin"
        target.mkdir(parents=True)
        for tool in ("python", "fledge", "latex-prep"):
            shutil.copy2(os.environ["MOCK_PROGRAM"], target / tool)
    elif args[:2] == ["-m", "pip"] and os.environ.get("MOCK_PIP_FAIL"):
        sys.exit(19)
elif name == "brew":
    if os.environ.get("MOCK_BREW_NO_TOOLS"):
        sys.exit(0)
    binaries = {
        "mactex-no-gui": ("latexmk", "pdflatex", "bibtex"),
        "python@3.13": ("python3.13",),
        "poppler": (
            "pdfinfo", "pdftotext", "pdftoppm", "pdffonts",
            "pdfdetach", "pdfimages", "pdftohtml",
        ),
        "tex-fmt": ("tex-fmt",),
        "qpdf": ("qpdf",),
        "mupdf-tools": ("mutool",),
    }
    for package in args:
        for tool in binaries.get(package, ()):
            shutil.copy2(os.environ["MOCK_PROGRAM"], Path(os.environ["MOCK_BIN"]) / tool)
elif name in ("fledge", "latex-prep"):
    if args == ["--tool-locations"]:
        for tool in ("latexmk", "pdflatex", "pdfinfo"):
            subprocess.run([tool, "--selected-tool"], check=True)
    else:
        print(f"Usage: {name} [OPTIONS] COMMAND [ARGS]...")
elif args == ["--selected-tool"]:
    print(str(Path(sys.argv[0]).absolute()))
"""

# Use the actual shell logic; replace just host/tool discovery so missing-tool
# cases are deterministic even on machines with a complete TeX installation.
HARNESS = r"""
source "$1"
shift
uname() { printf '%s\n' "$MOCK_PLATFORM"; }
id() { printf '%s\n' "$MOCK_UID"; }
find_tool() {
    if [[ -x "$MOCK_BIN/$1" ]]; then
        printf '%s\n' "$MOCK_BIN/$1"
    else
        return 1
    fi
}
main "$@"
"""


class InstallerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="fledge installer tests ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source with spaces"
        (self.source / "src/latexprep").mkdir(parents=True)
        (self.source / "src/latexprep/cli.py").touch()
        (self.source / "pyproject.toml").touch()
        self.script = self.source / "install.sh"
        shutil.copy2(INSTALLER, self.script)
        self.home = self.root / "home with spaces"
        self.home.mkdir()
        self.tools = self.root / "fake tools"
        self.tools.mkdir()
        self.log = self.root / "commands.jsonl"
        self.program = self.root / "mock-program"
        self.program.write_text(f"#!{sys.executable}\n" + MOCK_PROGRAM)
        self.program.chmod(0o755)
        self.env = {
            **os.environ,
            "HOME": str(self.home),
            "MOCK_PROGRAM": str(self.program),
            "MOCK_LOG": str(self.log),
            "MOCK_BIN": str(self.tools),
            "MOCK_PLATFORM": "Darwin",
            "MOCK_UID": "501",
        }
        self.add_tools("sandbox-exec")

    @property
    def prefix(self):
        return self.home / ".local/share/fledge"

    @property
    def launcher(self):
        return self.home / ".local/bin/fledge"

    def add_tools(self, *names):
        for name in names:
            shutil.copy2(self.program, self.tools / name)

    def complete_tools(self):
        self.add_tools("python3", *TEX_TOOLS, *PDF_TOOLS)

    def run_installer(self, *args):
        return subprocess.run(
            ["/bin/bash", "-c", HARNESS, "installer-test", str(self.script), *args],
            cwd=self.root,
            env=self.env,
            text=True,
            capture_output=True,
            timeout=30,
        )

    def run_interactive(self, answer, *args):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        process = subprocess.Popen(
            ["/bin/bash", "-c", HARNESS, "installer-test", str(self.script), *args],
            cwd=self.root,
            env=self.env,
            stdin=slave,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        os.write(master, answer)
        stdout, stderr = process.communicate(timeout=10)
        return process.returncode, stdout, stderr

    def run_with_real_discovery(self, path, fallbacks, *args):
        """Run the installer's own find_tool with PATH and the fallback directories chosen here."""
        # Replace only the host-specific fallback paths in this isolated script copy, so no
        # machine's installed TeX is involved. Discovery itself is the unmodified function.
        original = self.script.read_text()
        self.assertIn(INSTALLER_FALLBACKS, original)
        quoted = " ".join(shlex.quote(str(directory)) for directory in fallbacks)
        self.script.write_text(original.replace(INSTALLER_FALLBACKS, f"local tool_dirs=({quoted})"))
        harness = HARNESS[: HARNESS.index("find_tool() {")] + 'main "$@"\n'
        return subprocess.run(
            ["/bin/bash", "-c", harness, "installer-test", str(self.script), *args],
            cwd=self.root,
            env={**self.env, "PATH": path},
            capture_output=True,
            text=True,
            timeout=30,
        )

    def launcher_path(self, launcher=None):
        """Return the PATH entries saved in a generated launcher, resolved for comparison."""
        for line in (launcher or self.launcher).read_text().splitlines():
            if line.startswith("export PATH="):
                value = shlex.split(line)[1].removeprefix("PATH=")
                return [os.path.realpath(entry) for entry in value.split(":")]
        self.fail("The launcher does not export PATH")

    def commands(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def assert_no_install(self):
        self.assertFalse(self.prefix.exists())
        self.assertFalse(self.launcher.exists())
        self.assertTrue(all(command[1:2] == ["-c"] for command in self.commands()))

    def test_help_and_invalid_options(self):
        for args in (
            ("--help",),
            ("--unknown",),
            ("--prefix",),
            ("--bin-dir", "--yes"),
            ("--uninstall", "--with-optional"),
        ):
            with self.subTest(args=args):
                result = self.run_installer(*args)
                self.assertEqual(result.returncode, 0 if args == ("--help",) else 1)
                if args == ("--help",):
                    self.assertIn("Install Fledge", result.stdout)
                    self.assertIn("Default: ~/.local/share/fledge", result.stdout)
                    self.assertIn("--uninstall", result.stdout)
                    self.assertIn("To upgrade, run --uninstall", result.stdout)
                self.assert_no_install()

    def test_unsupported_platforms_and_root_are_rejected(self):
        for platform in ("Linux", "MINGW64_NT-10.0"):
            self.env["MOCK_PLATFORM"] = platform
            result = self.run_installer("--yes")
            self.assertEqual(result.returncode, 1)
            self.assertIn("macOS only", result.stderr)
            self.assert_no_install()
        self.env.update(MOCK_PLATFORM="Darwin", MOCK_UID="0")
        result = self.run_installer("--yes")
        self.assertIn("without sudo", result.stderr)
        self.assert_no_install()

    def test_missing_sandbox_fails_before_installation(self):
        (self.tools / "sandbox-exec").unlink()
        result = self.run_installer("--yes")
        self.assertEqual(result.returncode, 1)
        self.assertIn("sandbox-exec is missing", result.stderr)
        self.assert_no_install()

    def test_complete_toolchain_dry_run_needs_no_homebrew(self):
        self.complete_tools()
        result = self.run_installer("--dry-run", "--yes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Reuse Python", result.stdout)
        self.assertIn("Installation plan for Fledge", result.stdout)
        self.assertIn(f"Private environment: {self.prefix}/venv", result.stdout)
        self.assertIn(f"Launcher: {self.launcher}", result.stdout)
        self.assertIn("Dry run complete", result.stdout)
        self.assertNotIn("install --cask", result.stdout)
        self.assert_no_install()

    def test_missing_dependencies_show_large_download_and_optional_plan(self):
        self.add_tools("brew")
        result = self.run_installer("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("multi-GB", result.stdout)
        self.assertIn("administrator password", result.stdout)
        self.assertIn("install --cask mactex-no-gui", result.stdout)
        self.assertIn("install python@3.13 poppler", result.stdout)
        self.assertNotIn("install python@3.13 poppler tex-fmt", result.stdout)
        optional = self.run_installer("--dry-run", "--with-optional")
        self.assertIn("install python@3.13 poppler tex-fmt qpdf mupdf-tools", optional.stdout)
        self.assert_no_install()

    def test_missing_homebrew_is_a_prerequisite_not_a_bootstrap(self):
        result = self.run_installer("--dry-run")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Installation plan", result.stdout)
        self.assertIn("https://brew.sh", result.stderr)
        self.assert_no_install()

    def test_partial_tex_is_never_replaced(self):
        for tool in ("pdflatex", "tlmgr"):
            with self.subTest(tool=tool):
                self.add_tools(tool)
                result = self.run_installer("--yes")
                self.assertEqual(result.returncode, 1)
                self.assertIn("Existing TeX installation is incomplete", result.stderr)
                self.assertIn("latexmk", result.stderr)
                self.assert_no_install()
                (self.tools / tool).unlink()

    def test_existing_targets_including_broken_symlinks_are_preserved(self):
        for path in (self.prefix, self.launcher):
            path.parent.mkdir(parents=True, exist_ok=True)
            for symlink in (False, True):
                with self.subTest(path=path, symlink=symlink):
                    if symlink:
                        path.symlink_to(self.root / "unrelated absent target")
                    else:
                        path.write_text("unrelated content")
                    result = self.run_installer("--yes")
                    self.assertEqual(result.returncode, 1)
                    self.assertIn("already exists", result.stderr)
                    if symlink:
                        self.assertTrue(path.is_symlink())
                    else:
                        self.assertEqual(path.read_text(), "unrelated content")
                    self.assertEqual(self.commands(), [])
                    path.unlink()

    def test_noninteractive_install_requires_explicit_yes(self):
        self.complete_tools()
        result = self.run_installer()
        self.assertEqual(result.returncode, 1)
        self.assertIn("use --yes", result.stderr)
        self.assert_no_install()

    def test_interactive_decline_makes_no_changes(self):
        self.complete_tools()
        returncode, stdout, stderr = self.run_interactive(b"n\n")
        self.assertEqual(returncode, 0, stderr)
        self.assertIn("Cancelled. No changes made.", stdout)
        self.assert_no_install()

    def test_existing_python_is_validated_and_newer_python_reused(self):
        self.complete_tools()
        self.env["MOCK_OLD_PYTHON"] = "1"
        self.add_tools("python3.12")
        result = self.run_installer("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"Reuse Python: {self.tools}/python3.12", result.stdout)
        self.assert_no_install()

    def test_mock_install_handles_spaces_and_relative_destinations(self):
        self.complete_tools()
        result = self.run_installer("--yes", "--prefix", "private dir", "--bin-dir", "bin dir")
        self.assertEqual(result.returncode, 0, result.stderr)
        launcher = self.root / "bin dir/fledge"
        self.assertTrue(launcher.is_file())
        self.assertTrue(os.access(launcher, os.X_OK))
        self.assertIn("Fledge installed. Run from any directory", result.stdout)
        pip = next(command for command in self.commands() if command[1:3] == ["-m", "pip"])
        self.assertEqual(Path(pip[-1]), self.source.resolve())
        run = subprocess.run(
            [str(launcher), "--help"],
            cwd=self.home,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("Usage: fledge", run.stdout)
        self.assertFalse(any(self.home.glob(".*rc")))
        self.assertEqual(
            self.launcher_path(launcher)[0], os.path.realpath(self.tools), "selected tools first"
        )

    def test_new_install_preserves_legacy_environment_and_launcher(self):
        self.complete_tools()
        legacy_prefix = self.home / ".local/share/latex-preparation"
        legacy_environment = legacy_prefix / "venv"
        legacy_environment.mkdir(parents=True)
        legacy_marker = legacy_environment / "existing-installation"
        legacy_marker.write_text("Keep the previous installation.")
        legacy_launcher = self.home / ".local/bin/latex-prep"
        legacy_launcher.parent.mkdir(parents=True)
        legacy_launcher.write_text("#!/bin/bash\nexit 23\n")
        legacy_launcher.chmod(0o755)

        result = self.run_installer("--yes")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.prefix / "venv/bin/fledge").is_file())
        self.assertTrue(self.launcher.is_file())
        self.assertEqual(legacy_marker.read_text(), "Keep the previous installation.")
        self.assertEqual(list(legacy_environment.iterdir()), [legacy_marker])
        self.assertEqual(legacy_launcher.read_text(), "#!/bin/bash\nexit 23\n")
        self.assertTrue(os.access(legacy_launcher, os.X_OK))
        self.assertEqual(
            [command for command in self.commands() if command[0] in ("fledge", "latex-prep")],
            [["fledge", "--help"], ["fledge", "--help"]],
        )

    def test_mock_homebrew_install_and_post_install_verification(self):
        self.add_tools("brew")
        result = self.run_installer("--yes", "--with-optional")
        self.assertEqual(result.returncode, 0, result.stderr)
        brew = [command for command in self.commands() if command[0] == "brew"]
        self.assertEqual(
            brew,
            [
                ["brew", "install", "--cask", "mactex-no-gui"],
                ["brew", "install", "python@3.13", "poppler", "tex-fmt", "qpdf", "mupdf-tools"],
            ],
        )
        self.assertTrue(self.launcher.exists())
        again = self.run_installer("--yes")
        self.assertEqual(again.returncode, 1)
        self.assertIn("already exists", again.stderr)

    def test_launcher_preserves_discovery_order_across_overlapping_tool_directories(self):
        # PATH chooses one engine, while ordered fallback directories choose
        # latexmk and Poppler. Those fallback directories contain other versions
        # of the same tools: sorting by the first required tool would change them.
        self.complete_tools()
        (self.tools / "latexmk").unlink()
        preferred = self.root / "preferred tools"
        preferred.mkdir()
        fallback = self.root / "second fallback"
        fallback.mkdir()
        transient = self.root / "transient venv/bin"
        transient.mkdir(parents=True)
        shutil.copy2(self.program, transient / "unrelated-tool")
        for name in ("python3", "pdflatex"):
            shutil.copy2(self.program, preferred / name)
        for name in ("latexmk", "pdflatex", "pdfinfo"):
            shutil.copy2(self.program, fallback / name)
        result = self.run_with_real_discovery(
            f"{transient}:preferred tools:/usr/bin:/bin", (self.tools, fallback), "--yes"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"Reuse pdflatex: {preferred.resolve()}/pdflatex", result.stdout)
        self.assertIn(f"Reuse latexmk: {fallback}/latexmk", result.stdout)
        self.assertIn(f"Reuse pdfinfo: {self.tools}/pdfinfo", result.stdout)
        # Only the directories that supplied selected tools are saved, in PATH-first
        # search order (not tool order), then fallbacks and system directories. The
        # caller's other PATH entries, including the transient one, are left out.
        self.assertEqual(
            self.launcher_path(),
            resolved_unique_directories(preferred, self.tools, fallback, *SYSTEM_DIRECTORIES),
        )
        # Invoke the installed launcher from a different directory and a PATH
        # that otherwise selects the unwanted copies. Execute tools, not merely
        # parse the generated launcher text.
        run = subprocess.run(
            [str(self.launcher), "--tool-locations"],
            cwd=self.home,
            env={**self.env, "PATH": f"{fallback}:/usr/bin:/bin"},
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(
            [Path(line).resolve() for line in run.stdout.splitlines()],
            [
                (fallback / "latexmk").resolve(),
                (preferred / "pdflatex").resolve(),
                (self.tools / "pdfinfo").resolve(),
            ],
        )

    def test_launcher_saves_only_selected_directories_and_removes_duplicates(self):
        # The mocked discovery selects everything from one directory; the caller's
        # PATH (a project virtual environment and a relative entry) is not saved.
        self.complete_tools()
        project_environment = self.root / "project/.venv/bin"
        project_environment.mkdir(parents=True)
        self.env["PATH"] = (
            f"{project_environment}:relative entry:/usr/bin:{self.tools}:/usr/bin:/bin"
        )
        result = self.run_installer("--yes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.launcher_path(),
            resolved_unique_directories(
                self.tools,
                "/Library/TeX/texbin",
                "/opt/homebrew/bin",
                "/usr/local/bin",
                *SYSTEM_DIRECTORIES,
            ),
        )
        self.assertNotIn(str(self.root / "relative entry"), self.launcher.read_text())

    def test_launcher_keeps_already_installed_optional_tools_reachable(self):
        # Optional tools are not required without --with-optional, but a copy that
        # exists must stay reachable from the launcher. Missing ones add nothing.
        self.complete_tools()
        python_directory = self.root / "python bin"
        optional_directory = self.root / "optional bin"
        for directory in (python_directory, optional_directory):
            directory.mkdir()
        shutil.copy2(self.program, python_directory / "python3")
        shutil.copy2(self.program, optional_directory / "tex-fmt")
        path = f"{python_directory}:{optional_directory}:/usr/bin:/bin"
        result = self.run_with_real_discovery(path, (self.tools,), "--yes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Reuse tex-fmt", result.stdout)
        self.assertEqual(
            self.launcher_path(),
            resolved_unique_directories(optional_directory, self.tools, *SYSTEM_DIRECTORIES),
        )

    def test_failed_dependency_command_stops_before_environment_creation(self):
        self.add_tools("brew")
        self.env["MOCK_FAIL"] = "brew"
        result = self.run_installer("--yes")
        self.assertEqual(result.returncode, 17)
        self.assertIn("installing external dependencies", result.stderr)
        self.assertFalse(self.prefix.exists())
        self.assertFalse(self.launcher.exists())

    def test_successful_brew_without_required_tools_is_not_claimed_as_complete(self):
        self.add_tools("brew", "python3")
        self.env["MOCK_BREW_NO_TOOLS"] = "1"
        result = self.run_installer("--yes")
        self.assertEqual(result.returncode, 1)
        self.assertIn("latexmk is still unavailable", result.stderr)
        self.assertFalse(self.prefix.exists())
        self.assertFalse(self.launcher.exists())

    def test_pip_failure_retains_partial_environment_without_publishing_launcher(self):
        self.complete_tools()
        self.env["MOCK_PIP_FAIL"] = "1"
        result = self.run_installer("--yes")
        self.assertEqual(result.returncode, 19)
        self.assertIn("installing the local Python package", result.stderr)
        self.assertTrue(self.prefix.exists())
        self.assertFalse(self.launcher.exists())

    def install(self, *args):
        self.complete_tools()
        result = self.run_installer("--yes", *args)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_uninstall_removes_installation_and_launcher_only(self):
        self.install()
        bystander = self.launcher.parent / "unrelated-tool"
        bystander.write_text("keep")
        sibling = self.prefix.parent / "other-application"
        sibling.mkdir()
        before = self.commands()
        result = self.run_installer("--uninstall", "--yes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Fledge removed", result.stdout)
        self.assertFalse(self.prefix.exists())
        self.assertFalse(self.launcher.exists())
        self.assertEqual(bystander.read_text(), "keep")
        self.assertTrue(sibling.is_dir())
        self.assertEqual(self.commands(), before, "uninstalling must not run any tool")
        # Upgrading is uninstall followed by a fresh install.
        again = self.run_installer("--yes")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertTrue(self.launcher.is_file())

    def test_uninstall_matches_custom_relative_destinations(self):
        self.install("--prefix", "private dir", "--bin-dir", "bin dir")
        launcher, prefix = self.root / "bin dir/fledge", self.root / "private dir"
        wrong = self.run_installer("--uninstall", "--yes", "--prefix", "private dir")
        self.assertEqual(wrong.returncode, 1)
        self.assertTrue(prefix.is_dir() and launcher.is_file())
        result = self.run_installer(
            "--uninstall", "--yes", "--prefix", "private dir", "--bin-dir", "bin dir"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(prefix.exists() or launcher.exists())
        self.assertTrue(launcher.parent.is_dir())

    def test_uninstall_refuses_foreign_or_unrelated_launchers(self):
        other = self.root / "other prefix"
        self.prefix.mkdir(parents=True)
        marker = self.prefix / "venv/keep"
        marker.parent.mkdir()
        marker.write_text("keep")
        self.launcher.parent.mkdir(parents=True)
        foreign = {
            "unrelated program": '#!/bin/bash\nexec /usr/bin/true "$@"\n',
            "another prefix": f'#!/bin/bash\nexec {shlex.quote(str(other))}/venv/bin/fledge "$@"\n',
            "escaping path": f'#!/bin/bash\nexec {self.prefix}/venv/../../elsewhere "$@"\n',
            "mentions prefix only": f"#!/bin/bash\n# {self.prefix}/venv/bin/fledge\n",
        }
        for name, content in foreign.items():
            with self.subTest(name):
                self.launcher.write_text(content)
                result = self.run_installer("--uninstall", "--yes")
                self.assertEqual(result.returncode, 1)
                self.assertIn("Nothing was removed", result.stderr)
                self.assertEqual(self.launcher.read_text(), content)
                self.assertEqual(marker.read_text(), "keep")
        with self.subTest("missing launcher"):
            self.launcher.unlink()
            result = self.run_installer("--uninstall", "--yes")
            self.assertEqual(result.returncode, 1)
            self.assertEqual(marker.read_text(), "keep")

    def test_uninstall_refuses_symbolic_links_to_the_installation(self):
        self.install()
        real_launcher = self.root / "real launcher"
        real_prefix = self.root / "real prefix"
        self.launcher.rename(real_launcher)
        self.launcher.symlink_to(real_launcher)
        result = self.run_installer("--uninstall", "--yes")
        self.assertEqual(result.returncode, 1)
        self.assertTrue(self.launcher.is_symlink() and real_launcher.is_file())
        self.launcher.unlink()
        real_launcher.rename(self.launcher)
        self.prefix.rename(real_prefix)
        self.prefix.symlink_to(real_prefix)
        result = self.run_installer("--uninstall", "--yes")
        self.assertEqual(result.returncode, 1)
        self.assertIn("symbolic link", result.stderr)
        self.assertTrue(self.prefix.is_symlink() and self.launcher.is_file())
        self.assertTrue((real_prefix / "venv/bin/fledge").is_file())

    def test_uninstall_refuses_home_directory_and_its_parents(self):
        keep = self.home / "keep"
        keep.write_text("keep")
        for prefix in (self.home, self.home.parent):
            with self.subTest(prefix=prefix):
                result = self.run_installer("--uninstall", "--yes", "--prefix", str(prefix))
                self.assertEqual(result.returncode, 1)
                self.assertIn("home directory", result.stderr)
                self.assertEqual(keep.read_text(), "keep")

    def test_uninstall_dry_run_and_declined_confirmation_change_nothing(self):
        self.install()
        before = self.commands()
        for args in (("--dry-run",), ("--dry-run", "--yes")):
            with self.subTest(args=args):
                result = self.run_installer("--uninstall", *args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f"Remove launcher: {self.launcher}", result.stdout)
                self.assertIn(str(self.prefix), result.stdout)
                self.assertIn("Dry run complete", result.stdout)
                self.assertTrue(self.launcher.is_file() and (self.prefix / "venv").is_dir())
        noninteractive = self.run_installer("--uninstall")
        self.assertEqual(noninteractive.returncode, 1)
        self.assertIn("use --yes", noninteractive.stderr)
        returncode, stdout, stderr = self.run_interactive(b"n\n", "--uninstall")
        self.assertEqual(returncode, 0, stderr)
        self.assertIn("Cancelled. No changes made.", stdout)
        self.assertTrue(self.launcher.is_file() and (self.prefix / "venv").is_dir())
        self.assertEqual(self.commands(), before)
        returncode, stdout, stderr = self.run_interactive(b"y\n", "--uninstall")
        self.assertEqual(returncode, 0, stderr)
        self.assertFalse(self.launcher.exists() or self.prefix.exists())

    def test_uninstall_removes_stale_launcher_after_environment_is_gone(self):
        self.install()
        shutil.rmtree(self.prefix)
        result = self.run_installer("--uninstall", "--yes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already absent", result.stdout)
        self.assertFalse(self.launcher.exists())


if __name__ == "__main__":
    unittest.main()
