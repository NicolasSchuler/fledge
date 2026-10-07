# Troubleshooting

Read the first blocking finding and its next step. Save details with
`--report NEW_FILE` outside the input; use `fledge rule CODE` for an explanation.

| Problem | Next step |
| --- | --- |
| Command not found | With the installer, use `~/.local/bin/fledge`; with manual setup, activate the environment and try `python -m latexprep --help`. |
| Installer refuses an existing destination | Choose a fresh `--prefix` and a `--bin-dir` without a `fledge` launcher; existing prefixes and launchers are not overwritten. |
| Ambiguous document root | Set `--main main.tex`, or configure independent `workflow.documents`. |
| Unexpected configuration | Inspect `execution.config_path`; use `--config FILE` or `--isolated`. |
| Invalid setting or selector | Check the [configuration guide](configuration.md) and `fledge rules`. |
| Output already exists | Choose a new path outside the input. |
| Missing tool, isolation failure, or Biber cache error | Check [installation requirements](installation.md); source-only `inspect` remains available. |
| Missing/incomplete build trace or ambiguous dependency graph | Resolve the reported build/path evidence. Preparation cannot safely choose package contents without it. |
| A wanted extra file is omitted | Add its source-relative path to [required deliverables](configuration.md#package-contents). `--no-cleanup` does not retain unrelated files. |
| Time or resource limit | Inspect execution accounting; try `--jobs 1` or adjust explicit budgets to fit the host. |
| Flattening or source edit blocked | Review the unsupported path/syntax finding; use `--layout preserve` if flattening is unnecessary. |
| PDF comparison fails | Review differences and [preservation rules](preparation-options.md#preservation-and-explicit-exceptions). |
| Online check skipped | Select the individual check and grant `--online`; selecting `NET` alone is insufficient. |

For unresolved problems, retain the exact command, versions, effective settings,
and relevant findings. [Support boundaries](workflow.md#support-boundaries)
describe unsupported syntax and measurements. Inspect diagnostics before sharing;
the tool sends no report automatically.
