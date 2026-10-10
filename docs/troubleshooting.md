# Troubleshooting

Start with the first error in the report: one cause often produces several
findings, and fixing it usually clears the rest. `fledge rule CODE` explains any
code, and `--report FILE` saves the full JSON report of a blocked run.

## Common findings

### `TEX005`: missing dependency

A file named by `\input`, `\includegraphics`, `\bibliography` or a similar
command is not in the project. The build then fails too (`build.failed`,
`BLD009` "File not found"). Add the file, or correct the path in the source.
Paths are case-sensitive on many submission systems, so `Fig.pdf` and `fig.pdf`
are different files (`TEX007`).

### `BLD001`, `BLD002`, `BIB101`, `TEX004`: undefined citation or reference

`BLD001` and `BLD002` come from TeX's log; `BIB101` and `TEX004` come from
Fledge's own source scan and point at the line. Correct the misspelled key, add
the missing `\label` or bibliography entry, or declare the right `.bib` file,
then rerun. The [quick start](quickstart.md#3-read-the-result) shows an example.

### `project.main_selection`: ambiguous main file

The project contains more than one file with `\documentclass`, such as a paper
and its slides, and Fledge does not guess. Pass `--main main.tex`, set
`main = "main.tex"` in `fledge.toml`, or configure
[independent packages](preparation-options.md#independent-manuscript-and-supplement-packages)
for several documents.

### `BIB102`: unused bibliography entries

These entries are never cited. This is an advisory: Fledge never removes
entries on its own. If the entries are intentional, add `\nocite{key}` or ignore
the check with `--ignore BIB102`. To ship only cited entries, opt in to
`cited_only` under [bibliography transformations](preparation-options.md#bibliography-and-source-transformations).
While a citation error is present, `BIB102` can be a side effect of it.

### `TEX009`: generated bibliography not shipped

The paper uses BibTeX or Biber, but no generated `main.bbl` sits beside the main
file. Services that compile without running BibTeX or Biber, such as arXiv, then
show empty citations. By default this is an information note that does not
change the outcome; `[submission_checks] require_bbl = true`, which the `arxiv`
preset sets, makes it a warning. To fix it, build locally and keep the generated
`main.bbl` (and, for biblatex, `main.run.xml`) next to `main.tex`. `prepare`
ships them automatically, also with `--layout flat`. With BibTeX you can instead
inline the bibliography with `source_transforms.inline_bibliography`. A `.bbl`
from Biber must match the biblatex version of the compiling service.

`BIB109`, which the `arxiv` preset also enables, checks that the shipped `.bbl`
contains every cited key. A missing key is an error: rebuild the bibliography
after your last citation change. Without a shipped `.bbl`, `BIB109` is
inconclusive and defers to `TEX009`.

### `PDF104`: fonts not embedded

A font in the PDF is not embedded; this check runs when `require_embedded_fonts`
is on, as in every preset. The finding names the font. Fonts missing from a
figure are fixed by re-exporting that figure with embedded fonts (in matplotlib,
for example, set `pdf.fonttype` to `42`). For the document itself, use fonts
that pdfLaTeX embeds, such as Latin Modern via `\usepackage{lmodern}`.

### `TEX001`, `PRV007`: TODO and private-note markers

`TEX001` finds `TODO`, `FIXME`, `XXX` and `??` in the active text; `PRV007` finds
private notes, such as reviewer correspondence or "do not submit", in comments.
Both are advisories. Finish or remove the text. Remove private comments from the
prepared copy with `source_transforms.comment_policy = "private"`, which the
`arxiv` preset sets. A deliberate marker can be accepted with a
[suppression](preparation-options.md#preservation-and-explicit-exceptions).

### `build.execution_unavailable`, `pdf.inspection_unavailable`, `BLD202`: tool unavailable

A required tool is missing, or the build sandbox cannot start; the run is
blocked (exit 3). When TeX itself is missing, one finding names every missing
tool among `latexmk` and the engine and gives the install command for your
system, such as `brew install --cask basictex` on macOS. Missing Poppler tools
appear as `pdf.inspection_unavailable`, and a missing BibTeX or Biber as
`BLD202`. Install the tools as described in [installation](installation.md#tools).
If you run Fledge inside another sandbox, run it outside. `fledge inspect` needs
no external tools and still works meanwhile.

## By symptom

| Problem | Next step |
| --- | --- |
| Command not found | With the installer, use `~/.local/bin/fledge`; with manual setup, activate the environment and try `python -m latexprep --help`. |
| Installer refuses an existing destination | Choose a fresh `--prefix` and a `--bin-dir` without a `fledge` launcher, or [uninstall](installation.md#upgrade-or-uninstall) first. |
| Exit code 64 | Invalid command-line syntax; see `fledge COMMAND --help`. [Other exit codes](reports.md#exit-codes). |
| "Request error" (exit 4) | The command cannot run as given: an unusable `--output` or report path, a missing input, a `--main` that names no document (the message lists the ones found) or an invalid setting value. The `execution.request` finding names the source of the problem and the fix; nothing was built. |
| `execution.internal` finding (exit 4) | A defect, not a problem with your paper. Keep the command and the JSON report when reporting it. |
| Unexpected configuration | Check `execution.config_path` and `execution.preset` in the JSON report; use `--config FILE` or `--isolated`. |
| Unknown setting or selector | The request error suggests the closest valid name, such as `max_pages` for `max_page`, or the closest codes for a selector. See [choose checks](configuration.md#choose-checks) and `fledge rules`. |
| Output already exists | Choose a new path outside the input. |
| Isolation failure or Biber preparation failure | See [installation](installation.md#tools) and [Biber on macOS](workflow-reference.md#biber-on-macos). |
| Build failed | Read `details.log_tail` of the `build.failed` finding in the JSON or HTML report: the last 60 lines of the TeX log. |
| Missing or incomplete build trace | Resolve the reported build evidence. Preparation cannot choose package contents safely without a complete trace. |
| A wanted extra file is omitted | List its source-relative path under [`[package] include`](configuration.md#package-contents). `--no-cleanup` does not retain unrelated files. |
| `project.nonportable_filename` warning | A name that is not valid on every operating system was kept unchanged; rename it if the submission system rejects it. |
| Time or resource limit | Try `--jobs 1` or raise the [limits](workflow-reference.md#execution-and-resource-limits). A Biber job on macOS needs about 250 MB of temporary space. |
| Flattening or source edit blocked | Review the unsupported path or syntax finding; use `--layout preserve` if flattening is unnecessary. |
| PDF comparison fails | Review the differences and the [preservation rules](preparation-options.md#preservation-and-explicit-exceptions). |
| Online check skipped | The note is listed only with `--show-passed`. Enable the individual check and pass `--online`; selecting `NET` alone is not enough. |

## Getting help

Keep the exact command, the Fledge and tool versions (recorded in the JSON
report's `tools`), the effective settings and the relevant findings. Read
diagnostics before sharing them; Fledge sends nothing automatically.
