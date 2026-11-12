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

### `TEX324`: ambiguous main file

The finding is named `project.main_selection`. The project contains more than one
file with `\documentclass`, such as a paper and its slides, and Fledge does not
guess. Pass `--main main.tex`, set `main = "main.tex"` in `fledge.toml`, or configure
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
show empty citations. By default this is a not-applicable note, listed only
with `--show-passed`, that does not change the outcome;
`[submission_checks] require_bbl = true`, which the `arxiv` preset sets, makes
it a warning. To fix it, build locally and keep the generated
`main.bbl` (and, for biblatex, `main.run.xml`) next to `main.tex`. `prepare`
ships them automatically, also with `--layout flat`. With BibTeX you can instead
inline the bibliography with `source_transforms.inline_bibliography`. A `.bbl`
from Biber must match the biblatex version of the compiling service.

`BIB109`, which the `arxiv` preset also enables, checks that the shipped `.bbl`
contains every cited key. A missing key is an error: rebuild the bibliography
after your last citation change. Without a shipped `.bbl`, `BIB109` is
inconclusive and defers to `TEX009`.

`BIB110` compares the format version on the second line of a shipped biblatex
`.bbl` with `submission_checks.biblatex_bbl_format_version`. biblatex rejects a
`.bbl` written in another format, so a mismatch is an error. Look up the TeX
Live release in the service's current instructions, then build with that
release's biblatex; TeX Live 2026, for example, writes format `3.3`. The
`arxiv` preset contains this setting as a commented placeholder.

### `PDF104`: fonts not embedded

A font in the PDF is not embedded; this check runs when `require_embedded_fonts`
is on, as in every preset. The finding names the font. Fonts missing from a
figure are fixed by re-exporting that figure with embedded fonts (in matplotlib,
for example, set `pdf.fonttype` to `42`). For the document itself, use fonts
that pdfLaTeX embeds, such as Latin Modern via `\usepackage{lmodern}`.

### `TEX001`, `PRV007`: TODO and private-note markers

`TEX001` finds `TODO`, `FIXME`, `XXX` and `??` in the active text; `PRV007` finds
private notes, such as reviewer correspondence or "do not submit", in comments.
Both are advisories. Finish or remove the text.

`source_transforms.comment_policy = "private"`, which the `arxiv` preset sets,
removes only comments that contain `TODO`, `FIXME`, `XXX`, `private`, `internal`,
`confidential` or `note to self`. It keeps a comment such as "Reviewer 2 wants
this shortened" or "do not submit", so the notes `PRV007` reports can remain in
the prepared copy. To drop every comment from the prepared copy, set
`comment_policy = "all"`; comment blocks that carry a license notice or a
formatter directive are kept either way. Otherwise, delete or rewrite the
reported comments in your sources. A deliberate marker can be accepted with a
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

## Build failed

The first `BLD009` finding usually names the cause, and its next step says what to do when
one of Fledge's own restrictions caused it: builds never run shell escape, never generate
bitmap fonts, and see only the project, the TeX distribution and the system font folders.
A document that builds in your editor can therefore fail here.

### See the whole TeX log

The terminal shows each TeX error with its continuation lines. The last 60 lines of the
final log are in `details.log_tail` of the `build.failed` finding (or the `build.<stage>`
finding, such as `build.baseline`). Save the report and print them:

```sh
fledge check paper --report report.json
jq -r '.findings[] | select(.details.log_tail) | .details.log_tail' report.json
```

The HTML report (`--html-report report.html`) shows the same tail. `--debug` adds every
tool command, its exit status and its duration on stderr.

### A missing class, package or style

`! LaTeX Error: File 'acmart-custom.cls' not found.` means the file is in neither the
project nor the TeX distribution. Find and install its package:

```sh
tlmgr search --global --file /acmart-custom.cls
sudo tlmgr install <package>
```

On Debian or Ubuntu, `apt-file search acmart-custom.cls` names the `texlive-*` package to
install. A copy in your personal TeX tree (`TEXMFHOME`, such as `~/Library/texmf` or
`~/texmf`) or in `TEXMFLOCAL` is invisible to Fledge's builds, as it would be to the
receiving service: put the file into the project instead.

### A missing font

`Font tcrm1000 at 600 not found` means TeX has only a METAFONT source for the font, and
Fledge never generates bitmap fonts. For names starting with `ec`, `tc` or `la` (the EC and
text-companion fonts that T1 encoding and list bullets use), install `cm-super`
(`sudo tlmgr install cm-super`, or `sudo apt install cm-super`), or load outline fonts with
`\usepackage{lmodern}`. `fledge doctor` builds a list item, so it finds this before your
first paper does.

`The font "Fira Code" cannot be found` from fontspec means XeLaTeX or LuaLaTeX could not see
the font. Builds see the project, the TeX distribution and the system font folders
(`/System/Library/Fonts` and `/Library/Fonts` on macOS, `/usr/share/fonts` on Linux), not
fonts installed only for your account. Copy the font files into the project and load them
by file name, for example `\setmainfont{FiraCode-Regular.ttf}[Path=fonts/]`, if the font's
license allows. `BLD108` warns about a font that the build found in the system folders:
the archive rebuilds on your computer but not on one without that font.

### The wrong engine

`The fontspec package requires either XeTeX or LuaTeX` means pdfLaTeX built a document
written for XeLaTeX or LuaLaTeX. Fledge follows a `% !TEX program = xelatex` comment in the
first lines of the main file, as most editors do, or `--engine` and the
[engine setting](configuration.md#choose-the-tex-engine). `BLD106` records which engine
built the document and why. A project `latexmkrc` is never executed; `BLD107` says what it
would have changed.

### Shell escape, SVG and EPS figures

minted, the svg package, TikZ externalization and similar packages run external programs,
which Fledge's builds cannot. For minted, build once locally with `-shell-escape`, using
the TeX Live release of the receiving service, and keep the `_minted` folder in the project;
Fledge then uses the cached highlighting. Convert SVG figures to PDF yourself
(`rsvg-convert -f pdf -o figure.pdf figure.svg`) and EPS figures with
`epstopdf figure.eps`, then include the PDF files.

### A tool refused because it lives in your home folder

On Linux, Bubblewrap runs only tools installed under the system prefixes (`/usr`, `/bin`,
`/lib`): a TeX Live or `tex-fmt` installed in your home folder is refused with "Linux tool
executables must be installed under the supported system prefixes". Install them system-wide,
for example TeX Live under `/usr/local/texlive`, which is under `/usr`.

### `fledge` is not found after `pipx install`

pipx puts commands in `~/.local/bin`. Run `pipx ensurepath`, then open a new terminal.

## By symptom

| Problem | Next step |
| --- | --- |
| Command not found | With the installer, use `~/.local/bin/fledge`; with manual setup, activate the environment and try `python -m latexprep --help`. |
| Installer refuses an existing destination | Choose a fresh `--prefix` and a `--bin-dir` without a `fledge` launcher, or [uninstall](installation.md#upgrade-or-uninstall) first. |
| Exit code 64 | Invalid command-line syntax; see `fledge COMMAND --help`. [Other exit codes](reports.md#exit-codes). |
| "Request error" (exit 4) | The command cannot run as given: an unusable `--output` or report path, a missing input, a `--main` that names no document (the message lists the ones found) or an invalid setting value. The `execution.request` finding names the source of the problem and the fix; nothing was built. |
| `execution.internal` finding, or an `execution.task` finding with a `traceback` detail (exit 4) | A defect, not a problem with your paper. Keep the command and the JSON report when reporting it, and rerun with `--debug` to print the traceback. The other checks' results are still in the report. |
| Unexpected configuration | Check `execution.config_path` and `execution.preset` in the JSON report; use `--config FILE` or `--isolated`. |
| Unknown setting or selector | The request error suggests the closest valid name, such as `max_pages` for `max_page`, or the closest codes for a selector. See [choose checks](configuration.md#choose-checks) and `fledge rules`. |
| Output already exists | Choose a new path outside the input. |
| Isolation failure or Biber preparation failure | See [installation](installation.md#tools) and [Biber on macOS](workflow-reference.md#biber-on-macos). |
| Build failed | See [build failed](#build-failed): the first `BLD009` finding and its next step, and `details.log_tail` in the JSON or HTML report for the last 60 lines of the TeX log. |
| Missing or incomplete build trace | Resolve the reported build evidence. Preparation cannot choose package contents safely without a complete trace. |
| A wanted extra file is omitted | List its source-relative path under [`[package] include`](configuration.md#package-contents). `--no-cleanup` does not retain unrelated files. |
| `project.nonportable_filename` warning | A name that is not valid on every operating system was kept unchanged; rename it if the submission system rejects it. |
| Time or resource limit | Try `--jobs 1` or raise the [limits](workflow-reference.md#execution-and-resource-limits). A Biber job on macOS needs about 250 MB of temporary space. |
| Flattening or source edit blocked | Review the unsupported path or syntax finding; use `--layout preserve` if flattening is unnecessary. |
| PDF comparison fails | Review the differences and the [preservation rules](preparation-options.md#preservation-and-explicit-exceptions). |
| Online check skipped | The note is listed only with `--show-passed`. Enable the individual check and pass `--online`; selecting `NET` alone is not enough. |
| An unexplained wait, skip or tool failure | Rerun with `--debug` (or `FLEDGE_DEBUG=1`) and read the [diagnostic log](#diagnostic-log) on stderr. |
| Online checks cannot connect | Online checks connect directly and ignore proxy settings such as `HTTPS_PROXY`, so a network that allows only proxied access blocks them. Run them from a network with direct access, or leave them off. |

## Diagnostic log

`--debug`, or `FLEDGE_DEBUG=1` in the environment, makes any command print log lines
to stderr while it runs. The option is hidden from `--help` because it is meant for
investigating a problem or reporting one; it also prints the traceback of an internal
error. Standard output is unchanged, so `--output-format json` still prints only the
report and can be piped while you watch stderr.

```text
+0.412s runtime: tool start: pdflatex -halt-on-error +1 more argument
+3.870s runtime: tool end: pdflatex -halt-on-error +1 more argument (exit 0, 3.45 s)
+0.415s scheduler: task bibliography checks: blocked by failed source checks
```

Each line shows the time since the process started, the module that wrote it and a
message. The log covers:

- every tool command: the tool, the names of its options (no values and no paths),
  its exit status and how long it ran. A limit (time, output or resources) that
  stopped the tool is marked `[info]`;
- online requests: the host, method, status and duration, and the outcome of a fetch.
  URLs, query strings and the contact address are never logged;
- scheduled tasks: when each started and ended, how long it waited for resources and
  which failed prerequisite blocked it;
- PDF measurements that ran a tool or reused an earlier output;
- which settings file was discovered (its name, not its directory) and whether the name
  is a legacy `latex-prep` one;
- when the shared source analysis was built or reused.

Lines show this machine's home directory as `~` and its temporary directory as
`<tmp>`; credential-shaped text and terminal control characters are removed, and the
traceback that `--debug` prints for a defect gets the same treatment. Still, read the
log before sharing it. Without the option Fledge prints no log
lines at all. When you embed Fledge as a library, the `latexprep` logger has only a
`NullHandler`; attach your own handler to receive the records.

## Getting help

Keep the exact command, the Fledge and tool versions (recorded in the JSON
report's `tools`), the effective settings and the relevant findings. Add the
[diagnostic log](#diagnostic-log) of a rerun with `--debug` when the problem is a
wait, a skipped check or a tool failure. Read diagnostics before sharing them;
Fledge sends nothing automatically.
