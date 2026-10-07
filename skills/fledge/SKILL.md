---
name: fledge
description: Check and prepare LaTeX papers for submission with the Fledge CLI. Use when the user wants to check a LaTeX project before submitting it, build a verified submission ZIP (optionally flat, without subfolders), find problems such as missing files, undefined citations, unembedded fonts, page limits or leftover TODOs, or understand a Fledge finding code like TEX005 or PDF104.
---

# Fledge: check and prepare LaTeX submissions

Fledge inspects a LaTeX project (a folder or ZIP), builds it in an isolated
sandbox, checks the PDF, and writes a verified submission bundle to a **new**
output directory. It never edits the user's original files.

Run it as `fledge` (the macOS installer puts it at `~/.local/bin/fledge`; from a
source checkout, `python -m latexprep` works too). Check it is available with
`fledge --version`.

## Workflow

1. **Inspect** sources without building (fast, no TeX needed):
   `fledge inspect PAPER --main main.tex --output-format compact --quiet`
2. **Fix the reported problems in the user's sources** (only when the user asked
   you to edit their paper), then re-run.
3. **Check** with an isolated build and PDF inspection:
   `fledge check PAPER --main main.tex --output-format compact --quiet`
4. **Prepare** the submission once `check` passes:
   `fledge prepare PAPER --main main.tex --output NEW_DIR --output-format compact --quiet`
   Add `--layout flat` when the venue requires all files in one folder. Use
   `--dry-run` instead of `--output` to preview changes without a bundle.

`PAPER` is the project folder or a ZIP. Always pass `--main` when the project has
more than one `.tex` file with `\documentclass`; Fledge never guesses the root.

## Read the results

Prefer `--output-format compact --quiet`: the first line is
`OUTCOME | counts | scope`, then one line per finding:

```text
TEX005 error/failed sections/intro.tex:12 | Missing source dependencies: Literal dependency 'fig/plot' is missing from the project. | evidence: … | next: Include the missing dependency or correct the literal file path.
```

Each finding has a code (or a rule name for operational diagnostics), a
`severity/status`, a location, the message, optional evidence and a suggested
next step. Use `--json` when you need complete evidence (`findings[].details`),
the applied `changes` with diffs, or tool versions. Progress goes to stderr; with
`--quiet` stdout contains only the report.

Exit codes:

| Code | Outcome | What to do |
| --- | --- | --- |
| 0 | `passed` / `planned` | Done (`planned` = dry run, no bundle). |
| 1 | `passed_with_advisories` | Usable; review the warnings with the user. |
| 2 | `accepted_exceptions` | Passed only because of explicit review decisions in the config. |
| 3 | `blocked` | Fix the `error` findings first; no bundle was written. |
| 4 | `error` | Bad input, configuration or tool problem; read the `execution.*` finding. |
| 64 | (none) | Invalid command line; fix the flags. |
| 130 | `cancelled` | Interrupted. |

Explain any code with `fledge rule CODE` (add `--json` for structured output);
list all codes with `fledge rules --json`.

## Act on findings

- Work through `error` findings first, then `inconclusive` ones (missing
  evidence, unsupported syntax), then warnings.
- `failed` means a detected problem; `inconclusive` means Fledge could not decide
  (for example an unsupported macro or a missing tool) — it is never a pass.
- `BLD0xx` codes come from the TeX log: fix the source construct named in the
  message, then rebuild. A `build.failed` finding carries the end of the TeX log in
  its evidence (`details.log_tail` in JSON).
- `TEX005`–`TEX007`, `BIB001`–`BIB005`, `CMP*` and other safety guards stay
  enforced even if deselected; fix the cause instead of ignoring them.
- Do not change scientific content, citations or wording to silence a heuristic
  (`TEX001` TODO markers, `MAN*` style advice) without asking the user.

## Configure instead of working around

Project policy lives in `fledge.toml` next to the paper (or `[tool.fledge]` in
`pyproject.toml`; the older `latex-prep.toml` name still works). Command-line
options override it; `--isolated` ignores it; `--config FILE` selects one.

```toml
main = "main.tex"
max_pages = 10                      # only if the venue states a limit

[checks]
ignore = ["TEX001"]                 # deselect optional checks by code or prefix

[package]
include = ["data/results.csv"]      # extra files to ship although TeX never reads them
```

Never invent publisher limits; only configure constraints the user supplied.
Online reference checks are off by default and need both an individual switch in
`[online_checks]` and `--online`; ask before enabling network access.

## Safety rules

- Fledge never modifies the input. The `--output`, `--report` and
  `--html-report` paths must not exist yet and must be outside the input folder.
- A `blocked` run publishes nothing. Do not report a submission as ready unless
  `prepare` exited 0 or 1 and wrote `NEW_DIR/submission.zip`.
- Builds run sandboxed with shell escape and network disabled. If a build fails
  with an isolation or missing-tool error, report it to the user; do not try to
  bypass the sandbox.
- No result guarantees publisher acceptance; say so when summarizing.
