# Installation

## Install the Python CLI

Use Python 3.11 or later. From a local source checkout or an extracted source
distribution containing `pyproject.toml`:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
latex-prep --version
latex-prep --help
```

The commands in this guide use a POSIX shell. In Windows PowerShell, use
`.venv\Scripts\Activate.ps1` to activate the environment; isolated TeX builds are
not implemented on Windows.

For development, replace the install command with `python -m pip install -e .`.
An editable installation picks up local source changes. `uv tool install .` is
another option if you already use uv. The installed command is `latex-prep`;
`python -m latexprep --help` uses the same CLI from the active Python environment.

A locally supplied wheel can be installed by path:

```sh
python -m pip install /path/to/latex_preparation-0.1.0-py3-none-any.whl
```

Replace that path with the actual wheel. Installation may download the declared
Python dependencies, Click and Rich. The application's offline mode controls
its reference checks, not pip. A fully offline installation also needs those
dependencies available locally.

The source distribution includes documentation, example projects, and tests.
The wheel installs the CLI package; use the companion source distribution for
those supporting files, or copy the small fixture in the [quick start](quickstart.md).
No TeX tools are needed for that first inspection.

## Add tools for the operations you need

The application detects local executables; it never installs them automatically.
Use your platform's tool installation process and make the executables available
on `PATH` in the shell where you run `latex-prep`.

| Operation | Tools |
| --- | --- |
| `inspect`, `bib check` with local checks | Python package only |
| Isolated compilation | `latexmk`, the selected `pdflatex`, `xelatex`, or `lualatex` engine, required TeX packages, and the bibliography backend used by the document |
| PDF inspection and preservation comparisons | Poppler: `pdfinfo`, `pdftotext`, `pdftoppm`, `pdffonts`, `pdfdetach`, `pdfimages`; `pdftohtml` for text geometry |
| `fmt` or `prepare --format` | `tex-fmt` |
| Selected PDF object/structure checks | `qpdf` |
| Selected drawing, stroke, and color checks | MuPDF's `mutool` |

An unavailable tool produces incomplete evidence for its configured check.
Required build or comparison evidence blocks a verified preparation; an absent
tool is never treated as a pass. Standalone `pdf check` needs the applicable PDF
tools but does not compile the manuscript.

## Isolation and validation boundaries

Builds require **macOS `sandbox-exec`** or **Linux Bubblewrap** with operational
user-namespace support. A surrounding sandbox or host policy can prevent the
backend from starting even when the executable exists. There is no unsandboxed
fallback. Source and bibliography inspection remain available when builds are
blocked.

The development host has live macOS fixture coverage for pdfLaTeX/BibTeX
preparation and simple XeLaTeX/LuaLaTeX builds. This does not validate every
document or toolchain combination. Linux isolation has controlled-command tests
but no live Linux validation. Windows isolation is not implemented.

The self-extracting Biber launcher on the tested macOS host fails in isolation
because it executes binaries from a writable cache. It remains blocked; the
application does not grant executable-cache permission. `qpdf` and `mutool`
adapters have controlled-output tests but no live-tool validation on that host.
See the [complete support boundaries](workflow.md#support-boundaries).
