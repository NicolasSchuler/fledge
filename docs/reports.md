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
| 4 | `error` | Input, configuration, execution, or report-writing error |
| 130 | `cancelled` | Job interrupted |

Exit 2 also means a CLI syntax/usage error; check stderr or the JSON `outcome`.
For automation, check both `command` and `outcome`, not just the exit code.

## Understand a finding

A finding has a code, location, evidence, and suggested next step. `severity`
is `error`, `warning`, or `info`; `status` is `failed`, `inconclusive`, `skipped`,
or `passed`. Incomplete or disabled checks do not become passes. Inventories
without thresholds are measurements, not compliance claims. Operational findings
may have `code: null`.

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
and settings; compact output is suitable for agent handoff. Inspect reports before
sharing: redaction does not sanitize source files. Diagnostic ZIPs are unverified
material. See [detailed report behavior](workflow.md#readable-output-and-agent-handoff)
and [review decisions](preparation-options.md#preservation-and-explicit-exceptions).
