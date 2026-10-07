# Troubleshooting

Read the first blocking finding and its next step. Save details with
`--report NEW_FILE` outside the input; use `fledge rule CODE` for an explanation.

| Problem | Next step |
| --- | --- |
| Command not found | With the installer, use `~/.local/bin/fledge`; with manual setup, activate the environment and try `python -m latexprep --help`. |
| Installer refuses an existing destination | Choose a fresh `--prefix` and a `--bin-dir` without a `fledge` launcher, or [uninstall](installation.md#upgrade-or-uninstall) first; existing prefixes and launchers are not overwritten. |
| Exit code 64 | Invalid command-line syntax; see `fledge COMMAND --help`. [Other exit codes](reports.md). |
| `execution.internal` finding (exit 4) | A defect, not a problem with your paper. Keep the command and the JSON report when reporting it. |
| Ambiguous document root | Set `--main main.tex`, or configure independent `workflow.documents`. |
| Unexpected configuration | Inspect `execution.config_path`; use `--config FILE` or `--isolated`. |
| Invalid setting or selector | Check the [configuration guide](configuration.md) and `fledge rules`. |
| Output already exists | Choose a new path outside the input. |
| Missing tool, isolation failure, or Biber preparation failure | Check [installation requirements](installation.md) and [Biber on macOS](workflow.md#biber-on-macos); source-only `inspect` remains available. |
| Build failed | Read `details.log_tail` of the `build.failed` finding in the JSON or HTML report for the end of the TeX log. |
| Missing or incomplete build trace | Resolve the reported build evidence. Preparation cannot safely choose package contents without a complete trace. |
| A wanted extra file is omitted | List its source-relative path under `[package] include` ([package contents](configuration.md#package-contents)). `--no-cleanup` does not retain unrelated files. |
| `project.nonportable_filename` warning | A name that is not valid on every operating system was kept unchanged; rename it if the submission system rejects it. |
| Time or resource limit | Inspect execution accounting; try `--jobs 1` or adjust explicit budgets to fit the host. A Biber job on macOS also needs [temporary space](workflow.md#biber-on-macos). |
| Flattening or source edit blocked | Review the unsupported path/syntax finding; use `--layout preserve` if flattening is unnecessary. |
| PDF comparison fails | Review differences and [preservation rules](preparation-options.md#preservation-and-explicit-exceptions). |
| Online check skipped | The note is listed only with `--show-passed`. Select the individual check and grant `--online`; selecting `NET` alone is insufficient. |

For unresolved problems, retain the exact command, versions, effective settings,
and relevant findings. [Support boundaries](workflow.md#support-boundaries)
describe unsupported syntax and measurements. Inspect diagnostics before sharing;
the tool sends no report automatically.
