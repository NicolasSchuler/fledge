# Reports and exit codes

Read **scope**, **outcome**, then blocking findings. `inspect` covers sources and
bibliography; `prepare` also verifies PDF preservation and a fresh ZIP rebuild.

| Exit | Outcome | Meaning |
| --- | --- | --- |
| 0 | `passed` | Passed the command's stated scope |
| 0 | `planned` | Dry-run plan only; no verified bundle |
| 1 | `passed_with_advisories` | Required checks passed; review advisories (including `fmt` differences) |
| 2 | `accepted_exceptions` | Explicit, eligible review decisions were used |
| 3 | `blocked` | Required checks failed or lacked evidence |
| 4 | `error` | Input, configuration, execution, report-writing or unexpected internal error |
| 64 | none | Invalid command-line syntax; no report is produced |
| 130 | `cancelled` | Job interrupted |

Exit 2 means only accepted exceptions. An unexpected internal error becomes an
`execution.internal` error finding with exit 4, and the report is still written
in the requested format, so automation can read the `outcome`. For automation,
check both `command` and `outcome`, not just the exit code.

## Understand a finding

A finding has a code, location, evidence, and suggested next step. `severity`
is `error`, `warning`, or `info`; `status` is `failed`, `inconclusive`, `skipped`,
`not_applicable`, or `passed`. Incomplete or disabled checks do not become
passes. Inventories without thresholds are measurements, not compliance claims.
Operational findings may have `code: null`. A scope note is an `info` finding that is `skipped` or
`not_applicable`: it states what was not examined and is neither a failure nor
missing evidence, so a clean `check` or `prepare` can exit 0. Which formats list
scope notes and passed results is described under
[readable output](workflow.md#readable-output-and-agent-handoff).

```sh
fledge rule TEX001
fledge inspect /path/to/paper --offline --show-passed
```

## Save or share

```sh
fledge check /path/to/paper --offline --report report.json
fledge check /path/to/paper --offline --html-report report.html
fledge inspect /path/to/paper --offline --output-format compact --quiet
```

Report destinations must be new and outside the input. JSON retains full evidence
and settings. A failed build carries the last 60 lines of the TeX log (at most
8000 characters) in the finding's `details.log_tail`, which also appears in the
HTML report and diagnostic exports. Compact output is suitable for agent handoff.
Inspect reports before sharing: redaction does not sanitize source files.
Diagnostic ZIPs are unverified material. See [detailed report behavior](workflow.md#readable-output-and-agent-handoff)
and [review decisions](preparation-options.md#preservation-and-explicit-exceptions).
