# Glossary

These are the terms reports and these pages rely on.

```{glossary}
finding
  One result in a report: a code (or an operational name such as
  `build.failed`), a severity, a status, a location, a message and a suggested
  next step. A clean run still has findings; passed ones are listed only with
  `--show-passed`.

code
  The stable identifier of a check, such as `BIB101`: a three-letter family
  plus a number. Codes appear in reports, in `fledge rule CODE` and in
  `[checks] select` and `ignore`. Operational findings, such as a failed build,
  have a name instead (`code: null` in JSON).

check
  One test Fledge can run, identified by its code. Checks are
  [on by default, opt-in, suggestions or always enforced](checks.md).

rule
  The catalogue entry that describes a check: its code, title, scope and typical
  fix. In JSON, a finding's `rule` field holds its descriptive name, such as
  `bibliography.citation_coverage`.

severity
  How much a finding matters: `error` blocks a release, `warning` is an
  advisory, `info` is informational and `suggestion` is optional style advice.

status
  What happened: `failed` (a problem was found), `inconclusive` (Fledge could not
  decide), `skipped` (it did not run), `not_applicable` (nothing to examine) or
  `passed`.

advisory
  A warning that does not block a release, such as a TODO marker. A run whose
  only problems are advisories ends `passed_with_advisories` with exit code 1.

suggestion
  Optional advice, such as a style convention, from a check that runs only when
  selected (`--select ALL --select STY`). Suggestions are listed in a section of
  their own and never change the outcome or the exit code; a
  `% fledge: ignore CODE` comment silences one where it occurs.

blocked
  The outcome when the paper needs a fix: a required check failed, or its
  evidence is incomplete because of the sources. A blocked `prepare` publishes
  no output; exit code 3.

error (outcome)
  The outcome when Fledge could not check the paper: a missing or unusable tool,
  a sandbox that cannot start, a time or resource limit, an unreadable input,
  no single main file or invalid settings. Nothing is published; exit code 4.
  Fix the setup or the command, not the paper.

internal error
  The outcome `internal_error`: a defect in Fledge stopped the run. Exit code 70;
  report it with the command and the JSON report.

incomplete
  A check that ran but could not reach a decision (status `inconclusive`), for
  example because a tool is missing or the source uses an unsupported macro. It
  is never counted as a pass, and in a required check it blocks. The JSON
  report's `incomplete` field is broader: it marks any result, including a
  confirmed failure, whose evidence was partial, so it cannot be waived.

scope note
  An `info` finding that is `skipped` or `not_applicable`. It states what was
  not examined, such as an online check without network permission. It is
  neither a failure nor missing evidence.

accepted exception
  A reviewed decision in `[[reporting.accepted_exceptions]]` that lets an
  eligible failed check pass with a written reason. The original failure stays
  in the report, and the outcome is `accepted_exceptions` with exit code 2.

suppression
  A reviewed decision in `[[reporting.suppressions]]` that annotates an
  advisory at a specific location. It cannot waive an error.

preset
  A shipped starting configuration (`arxiv`, `anonymous-review` or
  `camera-ready`) that switches on generic checks. Apply it with `--preset NAME`
  or copy it with `fledge init --preset NAME`. It is not a venue policy.

baseline build
  The isolated build of your unchanged project. It provides the reference PDF
  and the build trace that decides which files the package needs.

preservation
  The requirement that the prepared copy and the rebuilt ZIP produce the same
  PDF as the baseline: same page count, same extracted text and matching page
  renders.

always-enforced guard
  A safety or preservation check that runs whenever its stage runs and cannot be
  switched off with `[checks] ignore`, such as `TEX005` (missing dependency) or
  `CMP003` (render comparison).
```
