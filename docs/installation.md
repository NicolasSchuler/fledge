# Installation

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

## Other platforms and manual installation

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

- The `fledge` distribution also installs a `latex-prep` command. The macOS
  installer exposes only `fledge`.
- Configuration files named `.latex-prep.toml` and `latex-prep.toml`, and the
  `[tool.latex-prep]` table in `pyproject.toml`, are still read, after the
  `fledge` names in the same directory. A `pyproject.toml` with both
  `[tool.fledge]` and `[tool.latex-prep]` is an error.
- Formatter-off markers with the `latex-prep` prefix are still accepted.
- Installations under the former name are left untouched by the installer.
- For development, `LATEX_PREP_RUN_INTEGRATION=1` and
  `LATEXPREP_RUN_SANDBOX_TESTS=1` still enable the live tests.
- Do not install the former `latex-preparation` distribution next to `fledge`:
  both provide the `latexprep` module. Use a fresh Python environment, or remove
  it with `python -m pip uninstall latex-preparation`.
