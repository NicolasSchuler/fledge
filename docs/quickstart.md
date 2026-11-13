# Prepare your paper

## 1. Install

| System | What to install |
| --- | --- |
| macOS | Run `bash install.sh` from a clone of Fledge and confirm the plan; it also sets up TeX and Poppler |
| Linux | Fledge with pipx or uv, plus TeX, Poppler and Bubblewrap, the build sandbox, from your distribution's packages |
| Windows | The Linux route inside WSL 2 |

[Installation](installation.md) gives the commands for each system and the
launcher path. Then run `fledge doctor`, which exercises the installed tools with
a tiny built-in document; see [setup diagnostics](doctor.md) for the scope of
that check.

## 2. Inspect the sources

Replace the path and the root filename:

```sh
fledge inspect /path/to/paper --main main.tex
```

`inspect` reads your sources and bibliography in seconds without compiling, so
it needs no TeX and finds missing files, undefined citations and leftover TODO
markers before the first build. It cannot see build or PDF problems; the next
step adds them. Fix what it reports, or move on when it passes.

The input can also be a ZIP of the project. From Overleaf, download the
project's source as a ZIP and pass that file instead of the folder. Fledge
compiles with your local TeX installation, which may be a different TeX Live
release than the one Overleaf uses.

## 3. Prepare your project

```sh
fledge prepare /path/to/paper --main main.tex --output /path/to/submission
```

The output folder must be new or empty and must lie outside the project. Fledge
never overwrites an earlier result: to rerun into the same folder, add `--force`,
which replaces only an output that Fledge wrote there itself, and only once the
new run has passed. The same rules apply to report and preview paths. Add
`--layout flat` if the venue wants all files in one folder.

A successful run writes:

- `sources/`: the prepared project files;
- `submission.zip`: those files, without a wrapper directory;
- `manuscript.pdf`: the PDF rebuilt from that ZIP;
- `report.json`: findings, changes and verification results.

Your original project is never changed, and unrelated files are left out of the
copy. To ship a file the build never reads, list it under
[`[package] include`](configuration.md#package-contents).
The terminal summarizes the included files, renames and compressed size. Add
`--show-files` for the full inventory, or `--html-report review.html` to keep a
report with file details and visual PDF differences when a comparison changes.

## 4. Read the result

A passing run ends like this (the package summary is shortened):

```text
Passed — selected preparation checks, visual/text preservation and fresh ZIP rebuild
0 errors, 0 warnings, 0 incomplete, 4 not applicable, 25 passed results
Document: main.tex
Settings: defaults (no settings file)
No issues found within this command's stated scope.
…
sources: /private/tmp/submission/sources
archive: /private/tmp/submission/submission.zip
pdf: /private/tmp/submission/manuscript.pdf
report: /private/tmp/submission/report.json

Not checked (needs your settings): anonymity, page limit, ZIP size limit, forbidden packages, strict font embedding, image resolution, 95 other opt-in checks. Set them in fledge.toml (start with fledge init --preset anonymous-review|arxiv|camera-ready); see https://nicolasschuler.github.io/fledge/configuration.html

Passed: no problems found within this command's scope. The submission was written.
```

The `Not checked` line lists opt-in checks that did not run; a
[preset](#5-add-a-preset-for-your-venue) switches on the useful ones.
Not-applicable results are notes about checks outside this run's scope. One of
them, `TEX009`, records that no generated `main.bbl` is shipped, which matters
only for a service that compiles without running BibTeX or Biber. Add
`--show-passed` to list these notes.

A blocked run lists what to fix, errors first. Here the citation key
`knuth1948` is misspelled (output shortened):

```text
Blocked — selected source, bibliography, privacy, isolated build and PDF checks
3 errors, 1 warning, 1 incomplete, 6 not applicable, 5 passed results
Document: main.tex
…
BIB101  Source citation coverage  [ERROR / failed]
  Where: main.tex: sections/results.tex:5 [bibliography coverage and fields]
  Citation key 'knuth1948' has no entry in this root's inspected resources.
  Next: Correct the citation key or declared bibliography resource, then compile to verify the
  rendered citations. Resolve reported source uncertainty before claiming coverage.
…
3 checks did not run because baseline build failed: baseline PDF inspection, baseline configured PDF checks, select preparation inputs

Blocked: 4 problems to fix. Start with BLD001 (Undefined citations in build log) at project. Nothing was published.
```

The fix is to correct the key to `knuth1984` in `sections/results.tex` and run
the command again. The other findings in this run (`BLD001` undefined citation,
`BLD002` undefined references, the incomplete baseline build) have the same cause
and disappear with it, as does the `BIB102` warning that the entry is unused.
Checks that depend on a successful build are listed as not run rather than
passed. One mistake often produces several findings, so fix the one that names
the cause and rerun before working down the list.
[Troubleshooting](troubleshooting.md#common-findings) explains the findings new
users meet most often, and `fledge rule BIB101` explains any code.

A blocked run writes no output directory and exits with code 3. Add
`--report /path/to/report.json` to keep its full report. In `report.json`, read
`outcome` first, then each entry in `findings`: its `code`, `severity`, `status`,
`path`, `line`, `message` and `next_step`. Exit code 4 means something else:
Fledge could not check the paper, for example because a tool is missing, so the
fix is on your computer rather than in the paper.
[Reports and exit codes](reports.md) explains every outcome and why a run that
passed with warnings exits with code 1.

## 5. Add a preset for your venue

Once the basic run passes, switch on the checks that matter for where the paper
is going. Presets are starting points for an `arxiv` upload, an
`anonymous-review` submission or a `camera-ready` version:

```sh
fledge init --list
fledge init /path/to/paper --preset arxiv
fledge prepare /path/to/paper --main main.tex --output /path/to/submission-2
```

`fledge init` writes a commented `fledge.toml` into the project; edit its
placeholders, such as a page limit, with your venue's current values. To try a
preset without writing a file, pass `--preset arxiv` to `inspect`, `check` or
`prepare`. See [start from a preset](configuration.md#start-from-a-preset) and the
venue guides for [arXiv](venue-arxiv.md), [anonymous review](venue-anonymous-review.md)
and the [camera-ready version](venue-camera-ready.md).

## Faster checks while you write

`fledge inspect` stays the quickest check while you edit. `fledge check` adds
the isolated build and PDF checks without packaging, and `prepare --dry-run`
previews requested changes without writing a bundle.

## Copy the abstract into a submission form

```sh
fledge submission-text /path/to/paper --field abstract
```

This exports one line with supported LaTeX formatting removed. Omit `--field`
to preview the title, abstract and keywords with any interpretation issues.
Unknown macros and complex notation require review before export; see
[submission text](submission-text.md).

Next: [configuration](configuration.md), [preparation options](preparation-options.md),
the [FAQ](faq.md) or the [offline example](examples.md).
