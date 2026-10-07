# LaTeX preparation

A Python CLI that checks a LaTeX project and prepares a separate submission copy.
It preserves the input, builds in isolation, compares the prepared PDF with the
baseline, and rebuilds the exact source ZIP before releasing a verified bundle.
Checks use your explicit constraints; the tool does not maintain publisher rules
or guarantee acceptance.

## Install and try it

Python 3.11 or later is required. From this source checkout or an extracted source
distribution, create an environment and install the package:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
latex-prep --help
latex-prep inspect examples/nested-paper --isolated --offline
latex-prep bib check examples/nested-paper --isolated --offline
```

These two checks need no TeX installation or network access after installation.
A successful inspection covers sources and references only. The
[quick start](docs/quickstart.md) also provides a copy-paste example for wheel users.
See [installation](docs/installation.md) for local wheel installation and optional
tools.

## Prepare a submission

With `latexmk`, a TeX engine, Poppler, and a working isolation backend installed:

```sh
latex-prep check ./paper --main main.tex --offline
latex-prep prepare ./paper --main main.tex --layout flat \
  --output ./prepared-paper --offline
```

Use a **new output directory outside the input**. A successful preparation writes
`sources/`, `submission.zip`, `manuscript.pdf`, and `report.json`. Inspect the
report's scope, outcome, and findings before using the bundle. Transformations
such as formatting and bibliography edits are opt-in; conservative auxiliary-file
cleanup is enabled by default.

Build isolation requires macOS `sandbox-exec` or Linux Bubblewrap. Linux has
controlled-command tests but no live validation; Windows isolation is absent.
The self-extracting Biber launcher is incompatible with isolation on the tested
macOS host. `qpdf` and `mutool` adapters have controlled-output tests, without
live-tool validation. The [workflow reference](docs/workflow.md) retains the full
support boundaries and verification qualifications.

## Documentation

- [Quick start](docs/quickstart.md): offline inspection, checking, and preparation.
- [Configuration](docs/configuration.md) and [preparation options](docs/preparation-options.md).
- [Reports and exit codes](docs/reports.md) and [troubleshooting](docs/troubleshooting.md).
- [Check catalogue](docs/checks.md) and [complete workflow reference](docs/workflow.md).
- [Development and documentation builds](docs/development.md).
- [Architecture](architecture.md), [requirements](requirements.md), and
  [remaining-work backlog](docs/check-backlog.md): design and planning material,
  not a claim that the full planned release is implemented.

Keep private manuscripts outside the repository or in `.local/`; review paths
before staging. Generated outputs and local environments are ignored, while PDFs
and other source assets remain trackable. No license has been selected.
