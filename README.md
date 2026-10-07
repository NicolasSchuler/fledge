# LaTeX preparation

Prepare and verify a LaTeX submission ZIP containing the selected paper's needed
inputs. Your original files remain unchanged.

## Install on macOS

From this checkout or an extracted source distribution:

```sh
bash install.sh
```

The installer reuses available tools, shows its plan, and asks before installing.
See [installation](docs/installation.md) for a preview, requirements, and manual setup.

## Prepare your paper

Replace the paths and `main.tex` with your project and root document:

```sh
~/.local/bin/latex-prep prepare /path/to/paper --main main.tex \
  --output /path/to/submission
```

The output directory must be new and outside the input; existing ZIPs are never
overwritten. A verified run writes
`sources/`, `submission.zip`, `manuscript.pdf`, and `report.json` after checking
PDF preservation and rebuilding the exact ZIP. Unrelated files are omitted from
the copy; [configure extra deliverables](docs/configuration.md#package-contents) explicitly.

- [Quick start](docs/quickstart.md) · [offline demo](docs/examples.md)
- [Configuration](docs/configuration.md) · [reports](docs/reports.md) · [troubleshooting](docs/troubleshooting.md)
- [Detailed workflow and support limits](docs/workflow.md) · [all documentation](docs/index.md)
- [Development, documentation builds, and design references](docs/development.md)
