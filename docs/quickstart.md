# Prepare your paper

## 1. Install on macOS

From the tool's checkout or extracted source distribution:

```sh
bash install.sh
```

Review the installer plan and confirm. See [installation](installation.md) for
manual setup and other platforms.

## 2. Prepare your existing project

Replace the paths and root filename:

```sh
~/.local/bin/fledge prepare /path/to/paper --main main.tex \
  --output /path/to/submission
```

Choose a new output directory outside the input. Existing ZIPs are never
overwritten. Preparation copies the paper's
needed inputs and [explicit extras](configuration.md#package-contents), verifies
PDF preservation, and rebuilds the exact ZIP. Add `--layout flat` when required.

## 3. Review the output

- `sources/` — prepared project files.
- `submission.zip` — source bundle, without a wrapper directory.
- `manuscript.pdf` — rebuilt from that ZIP.
- `report.json` — findings, changes, and verification results.

Read the [report outcome](reports.md) before submitting. A blocked run releases
no verified bundle; originals remain unchanged.

For a source-only check, use `~/.local/bin/fledge inspect /path/to/paper
--main main.tex --offline`. `prepare --dry-run` still builds the baseline.
Next: [configuration](configuration.md), [transformations](preparation-options.md),
or the [offline demo](examples.md).
