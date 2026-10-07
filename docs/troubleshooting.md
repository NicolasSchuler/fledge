# Troubleshooting

Start with the report's first blocking finding and its suggested next step.
Use `--output-format json --quiet` or `--report NEW_FILE` to retain settings,
tool diagnostics, and stage results. The [report guide](reports.md) explains
which outcomes describe incomplete evidence.

| Symptom | Next step |
| --- | --- |
| `latex-prep` is not found | Activate the environment where you installed it. Try `python -m latexprep --help` to confirm that Python sees the package. |
| The root document is ambiguous | Set `--main main.tex` to a project-relative root. For separately verified paper/supplement packages, use `workflow.documents`. |
| Unexpected checks or settings | Inspect `execution.config_path` in JSON. Use `--config FILE` for one explicit settings file, or `--isolated` to disable discovery. Do not combine them. |
| Unknown TOML keys, check codes, or invalid budgets | Use the [configuration guide](configuration.md), `latex-prep rules`, and `latex-prep rule CODE`. CLI options override the selected file; configurations are not merged. |
| Output/report destination already exists | Choose a new destination outside the input. The CLI deliberately refuses to overwrite these outputs. |
| Tool missing or check inconclusive | Install the applicable tool separately and ensure it is on `PATH`, or leave the result incomplete. Do not interpret an unavailable measurement as a pass. |
| Build isolation cannot start | Confirm that macOS `sandbox-exec` or Linux Bubblewrap and user namespaces work under the current host policy. A parent sandbox can block them. There is no unsandboxed fallback; use `inspect` for source-only feedback. |
| Biber fails from a writable cache | The tested self-extracting launcher is incompatible with isolation. Do not grant executable-cache access. BibTeX is an alternative only when the manuscript and bibliography setup actually support it. |
| Build times out or exceeds resources | Inspect execution accounting. Use `--jobs 1` to reduce overlap; adjust explicit time/memory/disk budgets only to fit the host. Total memory does not raise fixed renderer/formatter subprocess caps. |
| Flattening or an edit is blocked | Inspect the path or syntax finding. Dynamic TeX paths and unsupported contexts are not guessed. Use `--layout preserve` if flat layout is unnecessary, or simplify the input deliberately. |
| PDF comparison fails | Review the rendered/text evidence and source diffs. A supplied reference PDF, repeated baseline, and archive rebuild use exact comparisons. Only baseline-versus-prepared comparison supports configured tolerances or eligible review exceptions. |
| An online check is skipped | Select the individual online check and authorize network access with `--online` or `online_checks.online = true`. Selecting the `NET` family alone does neither. |
| A missing or unsupported PDF measurement looks like a warning | Read both severity and status. Bounded adapters may leave evidence inconclusive; passing other checks does not fill that gap. |

The source scanner handles supported literal TeX commands, not arbitrary TeX
execution. Analysis and transformations require UTF-8 source text. Build entry
paths accept ASCII letters, digits, underscores, dots, and hyphens with a `.tex`
suffix. See [support boundaries](workflow.md#support-boundaries) before adapting
a complex manuscript.

If a problem remains, retain the exact command, package/tool versions, effective
configuration, outcome, and relevant finding. A sanitized diagnostic ZIP can
help reproduce a problem, but inspect it before sharing; it is never a verified
submission package. No report or diagnostic is sent automatically.
