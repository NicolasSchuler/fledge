# Reports and exit codes

Every command ends with a report. Read three things in order: the **outcome**
(did it pass?), the **scope** (what was examined) and then the findings, errors
first. `inspect` covers sources and bibliography only; `prepare` also covers the
build, PDF preservation and the fresh ZIP rebuild.

## The terminal summary

This run passed with one advisory, a TODO marker: the bundle was written, but
the warning needs a human decision. The `TEX009` entry is an information note
and does not affect the outcome.

```text
Passed with advisories — selected preparation checks, visual/text preservation and fresh ZIP rebuild
0 errors, 1 warning, 0 incomplete, 3 not applicable, 23 passed results
Document: main.tex

TEX001  Unfinished text markers  [WARNING / failed]
  Where: sections/results.tex:4
  Possible unfinished editing or placeholder text: 'TODO'.
  Next: Finish or remove the marked draft text after reviewing its context.

TEX009  Generated bibliography shipped with the sources  [INFO / failed]
  Where: main.tex:14 [submission readiness, final source checks]
  main.tex reads references/library.bib through BibTeX, but main.bbl is not in the package. This
only matters for services that compile without running BibTeX or Biber, such as arXiv; set
submission_checks.require_bbl = true to require it.
  Next: Only needed when the receiving service does not run BibTeX or Biber: build locally and keep
the generated <main>.bbl next to the main file; prepare retains an existing <main>.bbl in the
package automatically. Biber-generated .bbl files must match the biblatex version of the compiling
service. For BibTeX, source_transforms.inline_bibliography (TEX203) can inline the .bbl instead.
sources: /private/tmp/submission-advisory-final/sources
archive: /private/tmp/submission-advisory-final/submission.zip
pdf: /private/tmp/submission-advisory-final/manuscript.pdf
report: /private/tmp/submission-advisory-final/report.json

Not checked (needs your settings): anonymity, page limit, ZIP size limit, forbidden packages, strict font embedding, image resolution, 92 other opt-in checks. Set them in fledge.toml (start with fledge init --preset arxiv|anonymous-review|camera-ready); see https://nicolasschuler.github.io/fledge/configuration.html

Explain a check: fledge rule CODE
```

The first line gives the outcome and scope, the second the counts. Each finding
then shows its code and name, `[SEVERITY / status]`, where it is, what was
observed and the next step. A finding repeated in several stages is shown once
with all stage names in brackets; the counts line counts it once. Passed results
and scope notes are counted but listed only with `--show-passed`.

When a failed step prevents later checks, one line per failed step names the
checks that did not run, as in the [blocked example](quickstart.md#3-read-the-result).
The final **Not checked** line lists opt-in areas that did not run because they
need your settings, or because selection turned them off. Without a settings
file it suggests starting from a [preset](configuration.md#start-from-a-preset);
with one, it names that file as the place to add them. This line appears in
terminal output only; JSON reports list the same areas in `execution.not_checked`.

## Exit codes

| Exit | Outcome | Bundle written? | Meaning |
| --- | --- | --- | --- |
| 0 | `passed` | Yes | Everything in the stated scope passed |
| 0 | `planned` | No | A `--dry-run` plan; nothing was verified |
| 1 | `passed_with_advisories` | Yes | Required checks passed; warnings need review |
| 2 | `accepted_exceptions` | Yes | Passed only because of reviewed exceptions in your configuration |
| 3 | `blocked` | No | A required check failed or lacked evidence |
| 4 | `error` | No | Input, configuration, execution or report-writing problem, or an internal error |
| 64 | none | No | Invalid command-line syntax; no report |
| 130 | `cancelled` | No | Interrupted |

The *Bundle written?* column applies to `prepare --output`; `inspect` and
`check` never write a bundle.

**Why advisories exit with 1.** A TODO marker or an unused bibliography entry
does not stop the release, but nobody has looked at it yet. A
nonzero code makes scripts and CI notice instead of shipping silently. Once you
have reviewed the warnings, the bundle is ready to use.

**Why accepted exceptions exit with 2.** An accepted exception means a check
failed and your configuration says that is acceptable, with a written reason. A
separate code lets automation tell "clean" from "clean because we decided so".
The original failure stays in the report.

**In CI**, treat 0 as success and 3, 4, 64 and 130 as failure. Choose a policy for
the others: accept 1 and surface its annotations (`--output-format ci`), or fail
on it if you want zero warnings; accept 2 only when the exceptions are committed
in the repository's `fledge.toml`. For example, to accept 0, 1 and 2:

```sh
status=0
fledge check paper --output-format ci --quiet || status=$?
[ "$status" -le 2 ] || exit "$status"
```

For automation that needs more than the exit code, read `command` and `outcome`
from the JSON report. An internal error still writes the report, as an
`execution.internal` finding with exit 4.

## The JSON report

`prepare` writes `report.json` with a successful output; `--report FILE` saves
the same JSON for any run, including a blocked one. This excerpt is from the
blocked run in the [quick start](quickstart.md#3-read-the-result), with most
findings, settings and tool details removed:

```json
{
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
      "evidence": "derived",
      "suggestion": "Correct the citation key or declared bibliography resource, then compile to verify the rendered citations. Resolve reported source uncertainty before claiming coverage.",
      "details": {
        "key": "knuth1948",
        "document": "main.tex",
        "resources": [
          "references/library.bib"
        ],
        "uncertainty": [],
        "stage": "bibliography coverage and fields"
      },
      "code": "BIB101"
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
      "ignore": []
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
| `outcome`, `scope` | The result and what it covers |
| `findings` | Every result, including passed ones and scope notes |
| `changes` | Proposed or applied edits, with diffs |
| `settings` | The effective settings after preset, file and command line |
| `tools` | Versions and paths of the tools used |
| `stages` | Each build and verification step with its status |
| `artifacts` | Paths of the published output; empty when blocked |
| `execution` | Configuration file, preset, check selection, opt-in checks that did not run (`not_checked`), resource use and timing |

A finding's `code` is `null` for operational results such as `build.failed`;
its `rule` always holds a descriptive name. A failed build's finding carries the
last 60 lines of the TeX log (at most 8000 characters) in `details.log_tail`.
The [glossary](glossary.md) defines severity, status and the other terms.

## Other formats

```sh
fledge rule TEX001
fledge check /path/to/paper --report report.json
fledge check /path/to/paper --html-report report.html
fledge check /path/to/paper --output-format compact --quiet
```

HTML is a self-contained offline page with source diffs. Compact output writes
one line per finding, which suits pasting into an AI agent; checks that did not
run after a failed step appear on lines starting with `not run:`. HTML and CI
output list every stage's copy of a finding and have no Not checked line. Reports redact known
credential patterns but do not sanitize your sources, so read a report before
sharing it. The [workflow reference](workflow-reference.md#report-formats)
describes every format and the redaction rules, and
[preparation options](preparation-options.md#preservation-and-explicit-exceptions)
describes accepted exceptions and suppressions.
