# Installation

Fledge is a Python 3.11+ command-line tool. Building a paper (`check` and
`prepare`) also needs TeX and Poppler; see [Tools](#tools).

| System | Route |
| --- | --- |
| macOS | The [installer](#macos-installer), which also sets up TeX and Poppler, or [pipx or uv](#install-with-pipx-or-uv) if you have them |
| Linux | [pipx or uv](#install-with-pipx-or-uv), plus the [Linux packages](#linux) |
| Windows | [WSL 2](#windows) with the Linux route |

## macOS installer

From the [source checkout](https://github.com/NicolasSchuler/fledge) or an
extracted source distribution, preview the plan and then install:

```sh
bash install.sh --dry-run
bash install.sh
```

The installer reuses tools you already have, shows its plan and asks before
changing anything. It supplies Python 3.11+, `latexmk`, pdfLaTeX, BibTeX and
Poppler as needed. If TeX is absent, the plan includes MacTeX without GUI apps
(`mactex-no-gui`), a multi-gigabyte installation that can require administrator
access. A conflicting partial TeX installation stops with advice instead of being
replaced. Homebrew is needed only when tools are missing; the installer does not
install Homebrew itself.

Fledge goes into `~/.local/share/fledge`, and the launcher is
`~/.local/bin/fledge`. The installer refuses an existing prefix or launcher,
including a symlink, and edits no shell files. **If `~/.local/bin` is not on your
`PATH`, type `~/.local/bin/fledge` wherever these pages show `fledge`.** The
launcher's own `PATH` contains only the detected tool directories, fallback
directories and system directories, so a virtual environment active in your shell
does not leak into builds.

| Option | Purpose |
| --- | --- |
| `--dry-run` | Preview; make no changes |
| `--yes` | Accept the displayed plan without a prompt |
| `--with-optional` | Also install missing `qpdf`, MuPDF tools and `tex-fmt` |
| `--prefix DIR --bin-dir DIR` | Choose installation and launcher directories |
| `--uninstall` | Remove the installation directory and launcher; works with `--dry-run` and `--yes` |

## Upgrade or uninstall

```sh
bash install.sh --uninstall --dry-run
bash install.sh --uninstall
```

Use the same `--prefix` and `--bin-dir` as for the installation. Uninstalling
removes the prefix and the launcher, and only when the installer wrote that
launcher for that prefix; otherwise nothing is removed. It refuses a symlinked
prefix or one containing your home directory, and it leaves Homebrew, TeX,
Poppler, shell files and your projects untouched. To upgrade, uninstall, then run
the installer from the newer source.

## Install with pipx or uv

Fledge is published on PyPI as `fledge-latex`. Install it into its own
environment with either tool:

```sh
pipx install fledge-latex
# or
uv tool install fledge-latex
fledge --version
```

This installs the `fledge` command, and `latex-prep` for older scripts. It does
not install TeX or Poppler; add them as described under [Tools](#tools), and for
[Linux](#linux) and [Windows](#windows) below. The command finds them through
your shell's `PATH`, unlike the macOS installer's launcher, which saves the tool
directories it detected. To upgrade or remove it, use `pipx upgrade fledge-latex`
or `uv tool upgrade fledge-latex`, and `pipx uninstall fledge-latex` or
`uv tool uninstall fledge-latex`.

```{warning}
Install `fledge-latex`, not `fledge`. The PyPI package named `fledge` is an
unrelated project, so `pip install fledge` does not install this tool.
```

## Manual installation from source

Use Python 3.11+ and install the external tools yourself:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
fledge --help
```

For a wheel, use `python -m pip install /path/to/package.whl`. Installation
downloads Click and Rich unless local copies are available. The wheel contains
the CLI; the source archive also includes the installer, docs, examples, tests
and the [agent skill](agents.md).

Builds run in a sandbox: `sandbox-exec` on macOS, or Bubblewrap with working user
namespaces on Linux. Windows has no build sandbox. See
[limitations](limitations.md#platforms-and-tools) for what has been tested where.

## Linux

Fledge's Linux sandbox is tested in CI: a weekly job runs the live integration
tests under Bubblewrap on a GitHub-hosted Ubuntu 24.04 runner. Other
distributions are not tested. On Debian or Ubuntu, install the same base
packages as that job:

```sh
sudo apt-get install bubblewrap latexmk biber poppler-utils \
  texlive-latex-base texlive-latex-recommended texlive-fonts-recommended \
  texlive-bibtex-extra
```

Add `texlive-xetex` or `texlive-luatex` for those engines, and the TeX Live
packages your paper needs. `tex-fmt` is not packaged by Ubuntu; to use it, put a
[release binary](https://github.com/WGUNDERWOOD/tex-fmt/releases) in a system
directory such as `/usr/local/bin`.

Fledge refuses tools installed in your home directory or in the project, so use
distribution packages or another installation under `/usr`.

Ubuntu 24.04 can block unprivileged user namespaces with AppArmor
(`kernel.apparmor_restrict_unprivileged_userns=1`), and Bubblewrap needs them. If
the sandbox cannot start, Fledge blocks the build and never falls back to running
unsandboxed. Allowing user namespaces is a security decision for your host: relax
that setting, or add an AppArmor profile that permits `bwrap`. Fledge's own Linux
test job sets it to `0` only on its disposable runner.

## Windows

Native Windows has no build sandbox, so `check` and `prepare` cannot build there;
`inspect` and `bib check` need only Python. To build on Windows, use WSL 2 with a
Linux distribution such as Ubuntu and follow the [Linux](#linux) instructions
inside it. That route has not been tested live.

## Tools

| Operation | Tools |
| --- | --- |
| `inspect`, `bib check` | Python package only |
| `check`, `prepare` | `latexmk`, the selected TeX engine and packages, the bibliography backend (BibTeX or [Biber](workflow-reference.md#biber-on-macos)), plus Poppler |
| PDF analysis and comparison | `pdfinfo`, `pdftotext`, `pdftoppm`, `pdffonts`, `pdfdetach`, `pdfimages`; `pdftohtml` for text geometry |
| Formatting and optional PDF checks | `tex-fmt`; `qpdf` and MuPDF's `mutool` |

A tool is needed only for the checks that use it. When a configured check's tool
is missing, that check is reported as incomplete rather than passed. Fledge never
installs tools during a run.

## Migrating from latex-prep

Fledge was previously named `latex-prep`. Existing setups keep working:

- The `fledge-latex` distribution also installs a `latex-prep` command. The
  macOS installer exposes only `fledge`.
- Configuration files named `.latex-prep.toml` and `latex-prep.toml`, and the
  `[tool.latex-prep]` table in `pyproject.toml`, are still read. In one directory
  the order is `.fledge.toml`, `fledge.toml`, `.latex-prep.toml`, `latex-prep.toml`
  and then `pyproject.toml`, so a leftover `latex-prep.toml` shadows a
  `[tool.fledge]` table beside it (see
  [discovery and precedence](configuration.md#discovery-and-precedence)). A
  `pyproject.toml` with both `[tool.fledge]` and `[tool.latex-prep]` is an error.
- Formatter-off markers with the `latex-prep` prefix are still accepted.
- Installations under the former name are left untouched by the installer.
- For development, `LATEX_PREP_RUN_INTEGRATION=1` and
  `LATEXPREP_RUN_SANDBOX_TESTS=1` still enable the live tests.
- Do not install the former `latex-preparation` distribution next to
  `fledge-latex`: both provide the `latexprep` module. Use a fresh Python
  environment, or remove it with `python -m pip uninstall latex-preparation`.
- Before this package was published, installations from a source checkout were
  registered as `fledge`. Remove such an installation with
  `python -m pip uninstall fledge` before installing `fledge-latex` into the same
  environment. The `fledge` package on PyPI is an unrelated project.
