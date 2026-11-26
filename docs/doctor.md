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

Without INPUT, a project folder or ZIP, the command uses defaults and does not
discover settings from the current directory. An explicit `--config` still
applies. With INPUT, normal nearby configuration discovery applies.
`--isolated` disables discovery. It cannot be combined with `--config`.

A project selects settings, not source files to test. When several documents
select different engines, each distinct engine is exercised. `--engine` changes
the default engine; a document's explicit engine still takes precedence. The
doctor does not read the main file, so a `% !TEX program` comment there does not
choose its engine: with the default `engine = "auto"` it tests pdfLaTeX, and
`fledge doctor --engine xelatex` tests the engine such a comment names.
Configured acceptance constraints, such as page limits and required metadata,
are not applied to the built-in document.

The core tools are `latexmk`, the selected engine and Poppler's `pdfinfo`,
`pdftotext`, `pdftoppm`, `pdffonts`, `pdfdetach` and `pdfimages`. XeLaTeX also
needs `xdvipdfmx`. The built-in document contains a bullet list, whose bullet
comes from the text-companion font: without `cm-super`, which provides that font
in Type 1 form, the build fails with "Font tcrm1000 at 600 not found" here rather
than on your first paper (see [a missing font](troubleshooting.md#a-missing-font)).
The terminal report ends with a table of the tools it used and their versions.
An explicit BibTeX or Biber backend makes that tool required
and selects a tiny bibliography fixture. With the default automatic strategy,
the doctor also compiles a tiny cited document through BibTeX and another
through biblatex and Biber, each when its tool is installed. A missing tool or a
failing fixture is an advisory, because papers that do not use that backend do
not need it. The Biber fixture takes longest, often 15 to 35 seconds on macOS,
where Biber first needs a prepared copy (see
[Biber on macOS](workflow-reference.md#biber-on-macos)). If formatting is
enabled, the built-in source is formatted through `tex-fmt`.

Only enabled additional measurements and optional linters cause advisory probes
of their tools, such as `qpdf`, MuPDF, `pdftohtml` or `chktex` (when `CHK001` is
selected). These probes establish installation and version availability only.
They do not exercise those measurements or prove support for the manuscript's
PDF structures. Missing optional tools produce advisories; missing required
tools fail the diagnostic with exit code 4. Either finding names the package
that provides the tool and how to install it.

The JSON report distinguishes a tool found on `PATH`, a version verified under
isolation, and a tool exercised by the built-in fixture. A present executable
does not establish that its resources or sandbox can start. Failure of the
sandbox is a setup limitation, not a document error, and there is no unrestricted
fallback. Configured tool and job timeouts, memory and temporary-space budgets
also bound the diagnostics. Cancellation stops the normal runtime's subprocesses.

A pass covers this built-in fixture and the named engines. It does not establish
that your custom classes, fonts, bibliography packages or actual manuscript will
build. Use `fledge check path/to/paper` for project-specific evidence.

The normal [exit codes](reports.md#exit-codes) apply: `0` passed, `1` passed with
advisories, `4` a failed diagnostic (a missing or unusable required tool, a sandbox
that cannot start, a failing built-in fixture, a time or resource limit) or an
invalid request, `70` an internal error, and `130` cancelled. The doctor never
ends with `3`, which means that a paper needs a fix, because it does not examine
one. `--quiet` suppresses progress and retains the final report. `--json` prints
the JSON report.
