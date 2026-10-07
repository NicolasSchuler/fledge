# Read reports and exit codes

Read the report's **scope and outcome first**, then resolve blocking findings.
A successful `inspect` covers sources and bibliography; it does not establish
that TeX compiles. A successful `prepare` covers the selected checks, preservation
comparisons, and a fresh rebuild of the delivered ZIP. Neither establishes
publisher acceptance, scientific correctness, anonymity, or general accessibility.

## Outcomes and findings

| Outcome | Meaning |
| --- | --- |
| `passed` | The command passed its stated scope |
| `passed_with_advisories` | The required scope passed, with findings still to review |
| `accepted_exceptions` | Eligible, explicitly scoped review decisions were used; this is not an unqualified pass |
| `planned` | A dry run produced a plan, without a verified bundle |
| `blocked` | Required checks failed or lacked sufficient evidence |
| `error` | An input, configuration, execution, or report-writing error prevented normal completion |
| `cancelled` | The job was interrupted |

A finding separates `severity` (`error`, `warning`, or `info`) from `status`:

| Status | How to read it |
| --- | --- |
| `failed` | The reported condition was found or a constraint was violated |
| `inconclusive` | Evidence was missing, ambiguous, or outside the supported measurement scope |
| `skipped` | The check did not run, for example because online permission was absent |
| `passed` | The particular check or inventory completed; read its stated scope |

An inventory without a configured threshold is not a compliance pass. Disabled
checks do not become passes, and missing tools do not become successful checks.
The default terminal report puts blockers first, then incomplete checks and
warnings; use `--show-passed` to reveal passed findings and inventories.

Each registered diagnostic has a `code`, descriptive `rule`, location, evidence,
and suggested next step. Operational diagnostics and inventories can have
`code: null`. For an explanation, run `latex-prep rule CODE` using the reported
code, or browse the [check catalogue](checks.md). Suggestions are guidance;
they do not automatically edit manuscript content.

## Export and share

Run these examples against your own `./paper` directory. Output files supplied
through `--report`, `--html-report`, or `--diagnostics` must be new and outside
the input tree.

```sh
latex-prep inspect ./paper --offline --output-format compact --quiet
latex-prep inspect ./paper --offline --output-format json --quiet > inspection.json
latex-prep check ./paper --offline --report build-report.json
latex-prep check ./paper --offline --html-report build-report.html
latex-prep check ./paper --offline --output-format ci --quiet
```

Compact output contains one plain line per finding, suitable for an agent handoff.
JSON includes findings, settings, changes and diffs, stages, tools, artifacts, and
execution accounting. `--json` is an alias for `--output-format json`; progress
uses stderr and `--quiet` suppresses it. Shell redirection can overwrite a file;
use `--report` when you want the CLI's new-file check.

HTML is an escaped offline report with source diffs and recorded-region diagrams;
it does not embed or annotate PDF pages. `--diagnostics NEW_ZIP` exports sanitized
reports and diffs as **unverified diagnostic material**, not a submission bundle.

Known credential patterns and credential-bearing URLs are redacted in all report
formats. This does not sanitize the source bundle or prove anonymity. Inspect
material before sharing. For multiple selected documents, findings identify the
document and stage, and every package must verify before any is released.

Review decisions require exact codes, optional location constraints, and reasons.
The original findings remain visible. Missing evidence, build/isolation failures,
and archive mismatches cannot be waived. See
[preservation and explicit exceptions](preparation-options.md#preservation-and-explicit-exceptions).

## Exit codes

| Exit code | Meaning |
| --- | --- |
| 0 | `passed` or `planned` |
| 1 | `passed_with_advisories`; for `fmt`, this includes formatting differences |
| 2 | `accepted_exceptions`, or Click CLI syntax/usage error |
| 3 | `blocked`; no verified bundle |
| 4 | `error` |
| 130 | `cancelled` |

Exit code 2 has two meanings in the current CLI. Inspect the JSON `outcome` for a
completed job or the usage error on stderr. Do not treat zero alone as proof of
preparation: a dry run also returns zero with `outcome: planned`.

For automation, decide explicitly whether advisories or accepted exceptions are
acceptable, and check both `command` and `outcome` in the JSON report. Read its
`scope`, `findings`, and `artifacts` before delivering a bundle.
