# Installation

## macOS installer

From the [source checkout](https://github.com/NicolasSchuler/fledge) or extracted source distribution:

```sh
bash install.sh --dry-run
bash install.sh
```

The first command previews without changes. The installer reuses installed tools,
shows its plan, and asks for confirmation. If TeX is absent, the plan includes
MacTeX without GUI apps (`mactex-no-gui`): a multi-gigabyte installation that can
require administrator access.
A conflicting partial TeX installation stops with advice instead of replacement.
Homebrew is required only when external tools are missing; the script does not
install Homebrew itself.

The default prefix is `~/.local/share/fledge`, with a `venv` child;
the launcher is `~/.local/bin/fledge`. An existing prefix or launcher, including
a symlink, is refused. There is no overwrite or upgrade mode. The launcher supplies
detected tool paths; no shell files are edited.
Use that full launcher path wherever these docs show `fledge` unless its
directory is already on your `PATH`. Existing installations under the former
name are left untouched.

| Option | Purpose |
| --- | --- |
| `--dry-run` | Preview; make no changes |
| `--yes` | Explicitly accept the displayed installation plan without a prompt |
| `--with-optional` | Also install missing `qpdf`, MuPDF tools, and `tex-fmt` |
| `--prefix DIR --bin-dir DIR` | Choose installation and launcher directories |

The installer supplies Python 3.11+, `latexmk`, pdfLaTeX, BibTeX, and Poppler as
needed. Installer logic has mock/dry-run coverage; live Homebrew and MacTeX
installation remains unverified.

## Manual Python installation

Use Python 3.11+ and install external tools separately:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
fledge --help
```

For a wheel, use `python -m pip install /path/to/package.whl`; for development,
use `python -m pip install -e .`. Installation may download Click and Rich; offline
installation needs local copies. The wheel contains the CLI, while the source
archive also includes the installer, docs, examples, and tests.

The `fledge` distribution installs both `fledge` and the compatible `latex-prep`
console entrypoints. The macOS installer exposes only the `fledge` launcher; the
legacy entrypoint remains inside its private virtual environment. Existing
[configuration names and tables](configuration.md#discovery-and-precedence) are
retained. When replacing the former `latex-preparation` distribution, use a fresh
Python environment: both distributions provide the same `latexprep` module and
should not be installed together.

## Tools and platform support

| Operation | Tools |
| --- | --- |
| `inspect`, `bib check` | Python package only |
| `check`, `prepare` | `latexmk`, selected TeX engine/packages and bibliography backend, plus Poppler |
| PDF analysis/comparison | `pdfinfo`, `pdftotext`, `pdftoppm`, `pdffonts`, `pdfdetach`, `pdfimages`; `pdftohtml` for text geometry |
| Formatting / optional PDF checks | `tex-fmt` / `qpdf` and MuPDF's `mutool` |

The installer is macOS-only. Builds require macOS `sandbox-exec` or, with manual
Linux setup, Bubblewrap and operational user namespaces. Host policy can block
isolation; there is no unsandboxed fallback. Linux has no live validation;
Windows build isolation is absent. The tested self-extracting Biber launcher is
incompatible with isolation. `qpdf`/`mutool` lack live-tool validation on the
development host. See [coverage and limitations](workflow.md#run-locally).
