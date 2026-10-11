# Diagnose the local setup

Run `fledge doctor` before checking a paper to find missing tools and failures in
restricted execution. It compiles a tiny built-in document and exercises PDF
inspection, text extraction and rendering through the same isolated runtime as
preparation. It does not compile or change your manuscript, install tools, or
use the network.

```sh
fledge doctor
fledge doctor --engine xelatex
fledge doctor path/to/paper
fledge doctor path/to/paper --config path/to/settings.toml --json
```

Without a project argument, the command uses defaults and does not discover
settings from the current directory. An explicit `--config` still applies.
With a project argument, normal nearby configuration discovery applies.
`--isolated` disables discovery. It cannot be combined with `--config`.

A project selects settings, not source files to test. When several documents
select different engines, each distinct engine is exercised. `--engine` changes
the default engine; a document's explicit engine still takes precedence.
Configured acceptance constraints, such as page limits and required metadata,
are not applied to the built-in document.

The core tools are `latexmk`, the selected engine and Poppler's `pdfinfo`,
`pdftotext`, `pdftoppm`, `pdffonts`, `pdfdetach` and `pdfimages`. XeLaTeX also
needs `xdvipdfmx`. An explicit BibTeX or Biber backend selects a tiny bibliography
fixture. With the default automatic bibliography strategy, bibliography tools
are not exercised. If formatting is enabled, the built-in source is formatted
through `tex-fmt`.

Only enabled additional PDF measurements cause advisory probes of their optional
tools, such as `qpdf`, MuPDF or `pdftohtml`. These probes establish installation
and version availability only. They do not exercise those measurements or prove
support for the manuscript's PDF structures. Missing optional tools produce
advisories; missing required tools block the diagnostic result.

The JSON report distinguishes a tool found on `PATH`, a version verified under
isolation, and a tool exercised by the built-in fixture. A present executable
does not establish that its resources or sandbox can start. Failure of the
sandbox is a setup limitation, not a document error, and there is no unrestricted
fallback. Configured tool and job timeouts, memory and temporary-space budgets
also bound the diagnostics. Cancellation stops the normal runtime's subprocesses.

A pass covers this built-in fixture and the named engines. It does not establish
that your custom classes, fonts, bibliography packages or actual manuscript will
build. Use `fledge check PROJECT` for project-specific evidence.

The normal exit codes apply: `0` passed, `1` passed with advisories, `3` blocked,
`4` invalid request or operational error, and `130` cancelled. `--quiet` suppresses
progress and retains the final report. `--json` prints the JSON report.
