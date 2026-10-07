<img src="docs/_static/logo.png" width="128" height="128" alt="Fledge logo: an ink-blue origami swallow with an orange fold">

# Fledge

Prepare and verify a LaTeX submission ZIP containing the selected paper's needed
inputs. Your original files remain unchanged.

[Read the documentation](https://nicolasschuler.github.io/fledge/) for installation,
usage, configuration, and troubleshooting.

## Install on macOS

From this checkout or an extracted source distribution:

```sh
bash install.sh
```

The installer reuses available tools, shows its plan, and asks before installing.
See [installation](https://nicolasschuler.github.io/fledge/installation.html) for a preview, requirements, manual setup, and upgrading or uninstalling.

## Prepare your paper

Replace the paths and `main.tex` with your project and root document:

```sh
~/.local/bin/fledge prepare /path/to/paper --main main.tex \
  --output /path/to/submission
```

The output directory must be new and outside the input; existing ZIPs are never
overwritten. A verified run writes
`sources/`, `submission.zip`, `manuscript.pdf`, and `report.json` after checking
PDF preservation and rebuilding the exact ZIP. Unrelated files are omitted from
the copy; [list extra files to ship](https://nicolasschuler.github.io/fledge/configuration.html#package-contents) under `[package] include`.

- [Quick start](https://nicolasschuler.github.io/fledge/quickstart.html) · [offline demo](https://nicolasschuler.github.io/fledge/examples.html)
- [Configuration](https://nicolasschuler.github.io/fledge/configuration.html) · [reports and exit codes](https://nicolasschuler.github.io/fledge/reports.html) · [troubleshooting](https://nicolasschuler.github.io/fledge/troubleshooting.html)
- [Use Fledge from an AI agent](https://nicolasschuler.github.io/fledge/agents.html): the Agent Skill in `skills/fledge`
- [Detailed workflow and support limits](https://nicolasschuler.github.io/fledge/workflow.html) · [all documentation](https://nicolasschuler.github.io/fledge/index.html)
- [Development, documentation builds, and design references](https://nicolasschuler.github.io/fledge/development.html) · [source repository](https://github.com/NicolasSchuler/fledge)
