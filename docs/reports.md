# Reports and exit codes

Every command ends with a report. Read three things in order: the **outcome**
(did it pass?), the **scope** (what was examined) and then the findings, errors
first. `inspect` covers sources and bibliography only; `prepare` also covers the
build, PDF preservation and the fresh ZIP rebuild.

## The terminal summary

Rich styles the outcome, groups findings and aligns artifacts without relying
on color to convey status. Live terminals show the current stage and elapsed
time on stderr. Redirected output uses plain stage lines; `--quiet` suppresses
progress. `NO_COLOR` disables color. JSON, compact and selected submission-text
fields remain suitable for scripts.

This run passed with one advisory, a TODO marker: the bundle was written, but
the warning needs a human decision.

```text
Passed with advisories — selected preparation checks, visual/text preservation and fresh ZIP rebuild
0 errors, 1 warning, 0 incomplete, 4 not applicable, 26 passed results
Document: main.tex
Settings: defaults (no settings file)

TEX001  Unfinished text markers  [WARNING / failed]
  Where: sections/results.tex:6
  Possible unfinished editing or placeholder text: 'TODO'.
  Next: Finish or remove the marked draft text after reviewing its context.

Artifacts
sources: /private/tmp/submission/sources
archive: /private/tmp/submission/submission.zip
pdf: /private/tmp/submission/manuscript.pdf
report: /private/tmp/submission/report.json

Not checked (needs your settings): anonymity, page limit, ZIP size limit, forbidden packages, strict font embedding, image resolution, 93 other opt-in checks. Set them in fledge.toml (start with fledge init --preset anonymous-review|arxiv|camera-ready); see https://nicolasschuler.github.io/fledge/configuration.html

Explain a check: fledge rule TEX001

Passed with 1 warning to review. The submission was written.
```

The first line gives the outcome and scope, the second the counts, which count
every result once: errors and warnings are checks that failed, *incomplete*
results reached no decision, whatever their severity, and scope notes count as
not applicable. A finding repeated in several stages is shown once with all
stage names in brackets and counted once. The `Settings:` line names the
settings file the run used, as a path you can open from the current directory,
and the preset, or says that the defaults applied. Each finding then shows its
code and title, `[SEVERITY / status]`, where it is, what was observed and the
next step. Passed results and scope notes are counted but listed only with
`--show-passed`. The example above omits the terminal's decorative borders and
file summary. The last line is the verdict.

A blocked report starts with what to fix. One mistake often produces several
findings: a missing figure stops the build, so the build fails, leaves no PDF,
and TeX reports the missing file again. Each such result is listed under the
finding that caused it, so the problems to fix stand out:

```text
TEX005  Missing source dependencies  [ERROR / failed]
  Where: sections/figs.tex:2
  Literal dependency 'figures/plot' is missing from the project; looked for ...
  Next: Add the file to the project or correct the name in \includegraphics.
  Follow-on results, which usually clear once this is fixed:
    BLD009  TeX errors in build log  [ERROR / failed]
      main.tex: sections/figs.tex:2 [baseline]: ./sections/figs.tex:2: LaTeX Error: File `figures/plot' not found.
    build.failed  Build failed  [ERROR / failed]
      main.tex [baseline]: Build tool exited with status 12.
    ...

BIB101  Source citation coverage  [ERROR / failed]
  Where: main.tex: sections/results.tex:5 [bibliography coverage and fields]
  Citation key 'knuth1948' has no entry in this root's inspected resources.
...
Blocked: 2 problems to fix. Start with TEX005 (Missing source dependencies) at sections/figs.tex:2.
```

A result follows from another when it names the same problem at the same file
and line, when it is the aftermath of a failed build (the build failed, left no
PDF, a rebuild stage did not succeed, TeX's closing "Fatal error occurred" line,
or a citation the stopped build could not resolve), or when its evidence is
incomplete because of a source error it names. Within each severity, failed
results come before incomplete ones. Results without a code, such as
`build.failed`, are shown with a title such as *Build failed*.

When a failed step prevents later checks, one line per failed step names the
checks that did not run. The **Not checked** line appears only after a run that
passed. It lists opt-in areas that did not run because they need your settings,
or because selection turned them off. Without a settings file it suggests
starting from a [preset](configuration.md#start-from-a-preset); with one, it
names that file as the place to add them. This line appears in terminal output
only; JSON reports list the same areas in `execution.not_checked`.

A request that cannot run, such as a missing input, an invalid setting or an
unusable output path, prints two lines on stderr and nothing on stdout:

```text
Error: Input does not exist: paper
Hint: Pass the path of the paper's LaTeX source folder or ZIP file; check the spelling and the current directory.
```

An interrupted run (Ctrl-C) prints one line on stderr, such as `Cancelled during
baseline build; nothing was published.` The JSON report keeps every detail of
both.

## What is in the submission?

Preparation reports summarize included and excluded files, filename changes,
ZIP size and the largest compressed entries. Add `--show-files` to list every
entry and its selection reason:

```sh
fledge prepare paper --output submission --show-files
fledge prepare paper --dry-run --show-files
```

Each document has its own inventory. A `verified` inventory uses the exact ZIP
that was rebuilt, including each entry's original and compressed sizes. ZIP
overhead is reported separately from compressed file data. `prepared` means
files were assembled but final verification has not passed. `proposed` marks a
dry-run or initial selection; `incomplete` means dependency selection failed.
These labels never turn a blocked run into a released submission. Unmeasured
sizes are shown as such, including excluded archive metadata that was not imported.

JSON stores these records in `package_contents`; HTML always includes the full
table. Both include original source paths, final paths and selection reasons.

## Exit codes

| Exit | Outcome | Bundle written? | Meaning |
| --- | --- | --- | --- |
| 0 | `passed` | Yes | Everything in the stated scope passed |
| 0 | `planned` | No | A `--dry-run` plan; nothing was verified |
| 1 | `passed_with_advisories` | Yes | Required checks passed; warnings need review |
| 2 | `accepted_exceptions` | Yes | Passed only because of reviewed exceptions in your configuration |
| 3 | `blocked` | No | The paper needs a fix: a required check failed or its evidence is incomplete because of the sources |
| 4 | `error` | No | Fledge could not check: a tool, the sandbox, a time or resource limit, the input, the settings or writing the report failed |
| 64 | none | No | Invalid command-line syntax; no report |
| 70 | `internal_error` | No | A defect in Fledge |
| 130 | `cancelled` | No | Interrupted |

The *Bundle written?* column applies to `prepare --output`; `inspect` and
`check` never write a bundle. Suggestions never change the exit code.

Exit code 3 always means that the paper needs a fix. A missing or unusable tool
(TeX, Poppler, `tex-fmt`), a sandbox that cannot start, a tool or job time limit,
a resource limit, an input that cannot be read, an input that does not say which
document to check (several or no main files) and a check that stopped on such a
problem end with 4 instead, as does every failed `fledge doctor` diagnostic.
When the paper needs a fix and Fledge also could not run some checks, the run is
blocked (3) and the report lists both.

An unexpected failure inside Fledge ends with 70 and prints one line on stderr.
The JSON report then holds a finding with the rule `execution.internal` that
records the `error_type` and the innermost `frames` (`file.py:line in
function`, without directories). Pass `--debug` or set `FLEDGE_DEBUG=1` to
print the full traceback when you report the defect. A crash while printing or
saving the report also exits with 70.

A defect inside one check does not stop the others. The report keeps their
results and adds an `execution.task` finding for the failed check, whose
`details.traceback` lists the innermost frames, and the run exits with 70. A
check that stops because of your input or machine, such as an unreadable file,
records an `execution.task` finding without a traceback and exits with 4.

**Why advisories exit with 1.** A TODO marker or an unused bibliography entry
does not stop the release, but nobody has looked at it yet. A
nonzero code makes scripts and CI notice instead of shipping silently. Once you
have reviewed the warnings, the bundle is ready to use.

**Why accepted exceptions exit with 2.** An accepted exception means a check
failed and your configuration says that is acceptable, with a written reason. A
separate code lets automation tell "clean" from "clean because we decided so".
The original failure stays in the report.

**In CI**, treat 0 as success and 3, 4, 64, 70 and 130 as failure. Choose a
policy for the others: accept 1 and surface its annotations (`--output-format
ci`), or fail on it if you want zero warnings; accept 2 only when the exceptions
are committed in the repository's `fledge.toml`. A 4 points at the runner (a
missing TeX package or tool, a time limit) rather than at the paper. For
example, to accept 0, 1 and 2:

```sh
status=0
fledge check paper --output-format ci --quiet || status=$?
[ "$status" -le 2 ] || exit "$status"
```

For automation that needs more than the exit code, read `command` and `outcome`
from the JSON report. An internal error still writes the report, as an
`execution.internal` finding with outcome `internal_error`.

## The JSON report

`prepare` writes `report.json` with a successful output; `--json-report FILE` (also
spelled `--report`) saves the same JSON for any run, including a blocked one. This excerpt is from the
blocked run in the [quick start](quickstart.md#4-read-the-result), with most
findings, settings and tool details removed:

```json
{
  "schema_version": 3,
  "command": "prepare",
  "outcome": "blocked",
  "scope": "selected source, bibliography, privacy, isolated build and PDF checks",
  "main": "main.tex",
  "findings": [
    {
      "rule": "bibliography.citation_coverage",
      "message": "Citation key 'knuth1948' has no entry in this root's inspected resources.",
      "severity": "error",
      "status": "failed",
      "path": "sections/results.tex",
      "line": 5,
      "evidence_kind": "derived",
      "incomplete": false,
      "details": {
        "key": "knuth1948",
        "document": "main.tex",
        "resources": [
          "references/library.bib"
        ],
        "uncertainty": [],
        "stage": "bibliography coverage and fields"
      },
      "code": "BIB101",
      "next_step": "Correct the citation key or declared bibliography resource, then compile to verify the rendered citations. Resolve reported source uncertainty before claiming coverage."
    }
  ],
  "changes": [],
  "artifacts": {},
  "execution": {
    "config_path": null,
    "preset": null,
    "check_selection": {
      "select": [
        "ALL"
      ],
      "ignore": [],
      "advisory": []
    },
    "not_checked": [
      {
        "area": "page_limit",
        "label": "page limit",
        "codes": [
          "PDF001"
        ],
        "settings": [
          "max_pages"
        ]
      }
    ]
  }
}
```

| Field | What it holds |
| --- | --- |
| `schema_version` | The report format version, currently `3` |
| `outcome`, `scope` | The result and what it covers |
| `findings` | Every result, including passed ones and scope notes |
| `changes` | Proposed or applied edits, with diffs |
| `settings` | The effective settings after preset, file and command line, under their current names (`apply_formatting`, `rendered_pdf_artwork` and `included_figure_artwork` replace `format`, `pdf_artwork` and `figure_artwork`) |
| `tools` | Versions and paths of the tools used |
| `stages` | Each build and verification step with its status and elapsed seconds |
| `artifacts` | Paths of the published output, as written; empty when blocked |
| `package_contents` | Per-document inventory, verification status, selection reasons and measured file/ZIP sizes |
| `execution` | Configuration file, preset, check selection, opt-in checks that did not run (`not_checked`), suggestions that source comments silenced (`silenced_suggestions`), resource use and timing |

Every other path in a report, such as the settings file, a reference PDF or a tool
location, shows your home directory as `~` and the system temporary directory as
`<tmp>`, so a report can be shared without revealing a user name. Paths inside the
project stay relative, and `artifacts` keeps the real location of the files Fledge
wrote.

A finding's `code` is `null` for operational results such as `build.failed`: results
that describe the run or the tool environment rather than the manuscript. They cannot
be selected or ignored and are always reported. Its `rule` always holds a descriptive
name. `evidence_kind` says how directly
the evidence supports the result (`direct`, `derived` or `heuristic`), and
`next_step` gives the suggested action when one applies. `incomplete` is `true`
when the check's evidence was partial, unavailable or bounded, for example an
unreadable file, an unmeasured page or a capped list of matches. Unlike the
*incomplete* count of the terminal summary, which counts results that reached no
decision, this flag can also mark a confirmed failure; such a failure can be
neither accepted nor suppressed. A failed build's finding carries the
last 60 lines of the TeX log (at most 8000 characters) in `details.log_tail`.
The [glossary](glossary.md) defines severity, status and the other terms.

`schema_version` increases only when a field is removed, renamed or changes
meaning. New fields can appear within a version, so ignore fields you do not
recognize. The {download}`JSON Schema <_static/report.schema.json>` describes the
stable fields: the top-level layout, findings, changes and the allowed
commands, outcomes, severities and statuses. The contents of `settings`,
`tools`, `execution`, `stages`, `package_contents` and finding `details` are
descriptive and may grow.

The JSON of `fledge rules`, `fledge rule CODE` and `fledge submission-text` has
its own `schema_version`, currently `1`, with the same rule: it increases only when
a field is removed, renamed or changes meaning. `fledge rules --json` lists the
checks under `rules`; each check says whether it is `mandatory` (check selection never
removes its findings), whether it is `reviewable` (an explicit review decision may
suppress or accept it), whether it is a `suggestion` and, as `reference_url`, which
page explains it, if any. `activation` says when it runs: `default` (without
settings), `opt-in` (once configured), `suggestion` (only when selected) or
`always` (a guard that ignoring its code does not switch off). `enabled_by` lists
what switches on an opt-in check or a suggestion, such as `"max_pages"`,
`"required_fields in [bibliography_checks]"` or `"--select ALL --select STY101"`,
and is empty for the others.

Version 3 added the outcome `internal_error` (exit 70) and the severity
`suggestion`, and moved runs that Fledge could not check, such as a missing tool,
a timeout or an ambiguous main file, from `blocked` to `error`.

Version 2 renamed the finding fields `evidence` to `evidence_kind` and
`suggestion` to `next_step`, and added `incomplete`. Hyphenated rule names such
as `source-edit-marker` became dotted names such as `source.edit_marker`, and
`settings` records `format`, `pdf_artwork` and `figure_artwork` as
`apply_formatting`, `rendered_pdf_artwork` and `included_figure_artwork`. In
`fledge doctor` reports, a rejected request is now `execution.request` and an
internal error `execution.internal`; they were `configuration.invalid` and
`execution.error`.

## Other formats

```sh
fledge rule TEX001
fledge check /path/to/paper --json-report report.json
fledge check /path/to/paper --html-report report.html
fledge check /path/to/paper --output-format compact --quiet
```

HTML is a self-contained offline page with source diffs and, when PDF
comparisons find differences, page previews. Its header shows the counts and the
verdict, and a result that follows from another is indented and names its cause.
It follows the light or dark setting of the reader's browser, prints cleanly, and
its title names the command and document.

Compact output writes one line per finding, which suits pasting into an AI
agent. The first line holds the outcome, the counts and the scope, then come the
document and a `settings:` line. A result that follows from another comes right
after it and carries `| caused by: CODE file:line |`; checks that did not run
after a failed step, or because the run was cancelled, appear on lines starting
with `not run:`; suggestions follow a `suggestions:` line; the last line starts
with `verdict:`.

CI output writes one GitHub annotation per finding, titled with the code and the
title (`title=TEX005 Missing source dependencies`). A result that follows from
another, a suggestion and a passed result are notices, so the errors and warnings
that need a decision stand out, and a final notice carries the verdict. GitHub
resolves an annotation's file relative to the repository root, so when the
project is a folder of the repository, such as `paper`, its files are named
`paper/main.tex`: relative to `GITHUB_WORKSPACE` in a GitHub Actions job and to
the current directory elsewhere. Files inside a ZIP input keep their paths.

HTML and CI output also show a finding repeated in several stages once, but have
no Not checked line. Reports redact known credential patterns but do not sanitize
your sources, so read a report before sharing it. The
[workflow reference](workflow-reference.md#report-formats) describes every format
and the redaction rules, and
[preparation options](preparation-options.md#preservation-and-explicit-exceptions)
describes accepted exceptions and suppressions.

## Suggestions

A suggestion is optional advice, such as a style convention, that leaves the
decision to you. Suggestions never change the outcome or the exit code. Every
format shows them in a section of their own, after the findings, and counts them
separately; in JSON their `severity` is `suggestion`.

| Suggestions | What they cover | Needs |
| --- | --- | --- |
| `STY1` | Obsolete packages, driver options and commands (`times`, `subfigure`, `\bf`, `$$`, `eqnarray`), `[h]`-only floats and `center` inside floats | Sources only |
| `STY2` | Unused labels, names printed twice (`Figure~\cref`) and `\ref` to an equation | Sources only |
| `STY3` | Missing ties before references, `et al.` spacing, hyphenated number ranges, straight quotes, `...`, a space before `\footnote` and `\cite\cite` | Sources only |
| `STY4` | Two spellings of one term, such as modelling and modeling | Sources only |
| `STY5` | No `\DocumentMetadata` with the document language and tagging | Sources only |
| `BIB111`–`BIB113` | A cited preprint, a URL that repeats the DOI, one venue spelled several ways | Sources only |
| `BLD013`–`BLD015` | hyperref's PDF-string warnings, obsolete packages and commands named in the log, and BibTeX or Biber warnings | A build (`check` or `prepare`) |
| `CHK001` | The warnings of chktex, the LaTeX lint tool; see [lint with chktex](chktex.md) | chktex installed |

Suggestions are off by default: `ALL` in the check selection does not switch them
on. Select a suggestion by its code, family or prefix beside `ALL` in
`[checks] select` of the settings file, or with `--select` on the command line,
which replaces the configured selection. For example, `--select ALL --select STY3`
adds the typography suggestions and `--select ALL --select STY --select BIB11`
all style and bibliography suggestions. An `--ignore` or `[checks] ignore` entry
still wins. `fledge rules STY` lists one family, `fledge rules --json` marks each
suggestion with `"activation": "suggestion"`, and `fledge rule CODE` names the
page that explains it.

To silence one suggestion where it occurs, put a TeX comment
`% fledge: ignore CODE`, with the code the report shows, at the end of its line,
or alone on the line just before it. Several codes are separated by commas or
spaces:

```latex
% fledge: ignore CODE
The sentence the suggestion is about.
Another sentence the suggestions are about. % fledge: ignore CODE, OTHERCODE
```

The same comment works in a `.bib` file, in `inspect`, `check`, `prepare` and
`bib check` alike. A bibliography suggestion is reported at the line where its
entry starts, so put the comment on the line before the entry:

```bibtex
% fledge: ignore BIB112
@article{smith2024,
  title = {An example},
  doi = {10.1000/example},
  url = {https://doi.org/10.1000/example}
}
```

A `\%` is text, not a comment. Such comments silence suggestions only. Warnings
and errors decide the outcome, so their exceptions belong in the settings file,
where they carry a reason and are reviewed (see
[preparation options](preparation-options.md#preservation-and-explicit-exceptions)).
A silenced suggestion leaves the findings; the terminal and compact reports count
it, and the JSON report lists it under `execution.silenced_suggestions`. The
comments stay in the submitted sources unless comment removal is selected.

## Visual PDF differences

Request HTML during the run to retain affected PDF pages:

```sh
fledge prepare paper --output submission --html-report review.html
fledge pdf check revised.pdf --reference-pdf original.pdf --html-report review.html
```

The report shows before/after pages and an expandable difference overlay, with
navigation between affected pages. Text-only differences, added or removed
pages and changed page dimensions are labeled. The original images remain the
reference for interpreting an overlay. Previews are available in a blocked
report even when no submission ZIP is released.

Capture is limited to eight page pairs and 12 MiB of PNG data across the whole
run. The first affected pages in document/stage order receive that budget.
Omitted, partial and unavailable previews are explicit; their absence does not
change the comparison findings. HTML adds bounded rendering work and serializes
comparisons and document workflows where needed for stable allocation. Runs
without HTML do not capture these images.

The HTML embeds its images and needs no server or external resources. JSON
contains descriptors in `execution.pdf_previews`, not image bytes. Re-rendering
JSON alone cannot recover previews. Page images contain manuscript content and
are not credential-redacted; inspect the report before sharing it.
