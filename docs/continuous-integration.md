# Check your paper in CI

Run Fledge on every push to a paper repository on GitHub and you get the findings
as GitHub annotations, plus the verified submission ZIP, the rebuilt PDF and the
reports as a downloadable artifact. A broken citation or a missing file then shows
up when you push it, not on submission day.

The workflow below uses a macOS runner because macOS is the platform on which
Fledge's sandbox and builds are validated; see [limitations](limitations.md#platforms-and-tools).
Linux runners will be documented once the Linux sandbox passes on a live Linux
host.

## The workflow

Save this as `.github/workflows/fledge.yml` in the paper repository. It assumes
the repository root is the paper and `main.tex` is the main file; change both to
match your project.

```yaml
name: Check paper

on:
  push:
  pull_request:
  workflow_dispatch:

permissions:
  contents: read

jobs:
  fledge:
    runs-on: macos-latest
    timeout-minutes: 45
    env:
      HOMEBREW_NO_AUTO_UPDATE: "1"
      HOMEBREW_NO_INSTALL_CLEANUP: "1"
    steps:
      - name: Check out the paper
        uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7
        with:
          persist-credentials: false
      - name: Set up Python
        uses: actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97 # v7
        with:
          python-version: "3.13"
      - name: Install TeX and Poppler
        run: |
          brew install --cask basictex
          brew install poppler
          echo "/Library/TeX/texbin" >> "$GITHUB_PATH"
      # Add any other packages your paper needs to the tlmgr install line.
      - name: Install TeX packages
        run: |
          sudo /Library/TeX/texbin/tlmgr update --self
          sudo /Library/TeX/texbin/tlmgr install latexmk biber biblatex hyperref
      - name: Install Fledge
        run: python -m pip install fledge-latex
      - name: Check the paper
        run: |
          mkdir -p "$RUNNER_TEMP/fledge"
          status=0
          fledge check . --main main.tex --output-format ci --quiet \
            --report "$RUNNER_TEMP/fledge/check.json" \
            --html-report "$RUNNER_TEMP/fledge/check.html" || status=$?
          [ "$status" -le 2 ] || exit "$status"
      - name: Prepare the submission
        run: |
          status=0
          fledge prepare . --main main.tex \
            --output "$RUNNER_TEMP/fledge/submission" \
            --output-format ci --quiet \
            --html-report "$RUNNER_TEMP/fledge/prepare.html" || status=$?
          [ "$status" -le 2 ] || exit "$status"
      - name: Upload the submission and reports
        if: ${{ !cancelled() }}
        uses: actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9 # v7
        with:
          name: fledge-submission
          path: |
            ${{ runner.temp }}/fledge/submission/submission.zip
            ${{ runner.temp }}/fledge/submission/manuscript.pdf
            ${{ runner.temp }}/fledge/submission/report.json
            ${{ runner.temp }}/fledge/check.json
            ${{ runner.temp }}/fledge/check.html
            ${{ runner.temp }}/fledge/prepare.html
          if-no-files-found: ignore
          retention-days: 14
```

Each action is pinned to a full commit SHA with its version in a comment, as in
Fledge's own workflows; a tool such as Dependabot can keep them current.

## What the steps do

- **TeX and Poppler.** BasicTeX is a small TeX Live distribution, and `tlmgr` adds
  `latexmk`, Biber and BibLaTeX. A package your paper needs but the runner lacks
  is reported as a build failure; add it to the `tlmgr install` line.
  Pin the version of Fledge, for example `fledge-latex==0.3.0`, if you want the
  same checks on every run.
- **`--output-format ci`** prints each finding as a GitHub annotation, and
  `--quiet` leaves out the progress messages. Fledge also reads a `fledge.toml` in
  the repository, so [settings and presets](configuration.md) apply in CI exactly
  as they do on your machine.
- **Reports and output** go to the runner's temporary directory because Fledge
  never writes into the project it reads. `--output` must name a directory that
  does not exist yet.
- **`prepare`** repeats the checks, then rebuilds the PDF from the submission ZIP
  it wrote and compares it with the original. `check` comes first for a quicker
  verdict; `prepare` builds again, so the job takes longer.
- **The upload** runs even when a step fails, so the reports are available for the
  failing run. Files that were never written, such as the ZIP after a blocked
  check, are skipped.

## Exit codes

Fledge's exit code decides whether the job passes. The workflow above continues
on 0, 1 and 2 and fails on everything else:

| Exit | Meaning | Job |
| --- | --- | --- |
| 0 | Everything in scope passed | Passes |
| 1 | Passed with advisories: warnings need review | Passes; the warnings stay visible as annotations |
| 2 | Passed only because of exceptions you accepted in `fledge.toml` | Passes |
| 3 | Blocked: a required check failed or lacked evidence | Fails |
| 4, 64, 130 | Error (including an internal failure), invalid command line, or interrupted | Fails |

To fail the job on advisories as well, change `-le 2` to `-le 0`. The
[reports page](reports.md#exit-codes) explains every code.

## Cost and other runners

GitHub-hosted runners are free for public repositories. For private
repositories, a macOS minute costs more than a Linux minute; see
[GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions).
Every run installs BasicTeX and its packages again, which adds to the minutes used.

Fledge cannot build on a Windows runner because Windows has no build sandbox. A
Linux runner is the cheaper option once the Linux sandbox has been validated; this
page will show that workflow then.
