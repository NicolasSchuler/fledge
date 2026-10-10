# Prepare your paper

## 1. Install

On macOS, run `bash install.sh` from the source checkout and confirm the plan.
[Installation](installation.md) covers other platforms and the launcher path.

## 2. Prepare your project

Replace the paths and the root filename:

```sh
fledge prepare /path/to/paper --main main.tex --output /path/to/submission
```

The output directory must not exist yet and must lie outside the project.
Fledge refuses to write anywhere else, so it never overwrites an earlier result
or ZIP; the same rule applies to report, preview and diagnostic paths. Add
`--layout flat` if the venue wants all files in one folder.

A successful run writes:

- `sources/`: the prepared project files;
- `submission.zip`: those files, without a wrapper directory;
- `manuscript.pdf`: the PDF rebuilt from that ZIP;
- `report.json`: findings, changes and verification results.

Your original project is never changed, and unrelated files are left out of the
copy. To ship a file the build never reads, list it under
[`[package] include`](configuration.md#package-contents).

## 3. Read the result

A passing run ends like this:

```text
Passed — selected preparation checks, visual/text preservation and fresh ZIP rebuild
0 errors, 0 warnings, 0 incomplete, 3 not applicable, 23 passed results
Document: main.tex

TEX009  Generated bibliography shipped with the sources  [INFO / failed]
  Where: main.tex:14 [submission readiness, final source checks]
  main.tex reads references/library.bib through BibTeX, but main.bbl is not in the package. This
only matters for services that compile without running BibTeX or Biber, such as arXiv; set
submission_checks.require_bbl = true to require it.
  Next: Only needed when the receiving service does not run BibTeX or Biber: build locally and keep
the generated <main>.bbl next to the main file; prepare retains an existing <main>.bbl in the
package automatically. Biber-generated .bbl files must match the biblatex version of the compiling
service. For BibTeX, source_transforms.inline_bibliography (TEX203) can inline the .bbl instead.
sources: /private/tmp/submission/sources
archive: /private/tmp/submission/submission.zip
pdf: /private/tmp/submission/manuscript.pdf
report: /private/tmp/submission/report.json

Not checked (needs your settings): anonymity, page limit, ZIP size limit, forbidden packages, strict font embedding, image resolution, 92 other opt-in checks. Set them in fledge.toml (start with fledge init --preset arxiv|anonymous-review|camera-ready); see https://nicolasschuler.github.io/fledge/configuration.html
```

The `TEX009` note is information and does not change the outcome: it matters
only for services such as arXiv that compile without running BibTeX or Biber.
For those, keep the generated `main.bbl` next to `main.tex`; `prepare` ships it
automatically. The last line lists opt-in checks that did not run; step 4 below
switches on the useful ones.

A blocked run lists what to fix, errors first. Here the citation key
`knuth1948` is misspelled (output shortened):

```text
Blocked — selected source, bibliography, privacy, isolated build and PDF checks
4 errors, 1 warning, 1 incomplete, 5 not applicable, 4 passed results
Document: main.tex
…
BIB101  Source citation coverage  [ERROR / failed]
  Where: main.tex: sections/results.tex:5 [bibliography coverage and fields]
  Citation key 'knuth1948' has no entry in this root's inspected resources.
  Next: Correct the citation key or declared bibliography resource, then compile to verify the
rendered citations. Resolve reported source uncertainty before claiming coverage.
…
3 checks did not run because baseline build failed: baseline PDF inspection, baseline configured PDF checks, select preparation inputs
```

The fix is to correct the key to `knuth1984` in `sections/results.tex` and run
the command again. The other errors in this run (`BLD001` undefined citation,
`BLD002` undefined references) have the same cause and disappear with it, as
does the `BIB102` warning that the entry is unused. Checks that depend on a
successful build are listed as not run rather than passed. One mistake often
produces several findings, so fix the one that names the cause and rerun before
working down the list. [Troubleshooting](troubleshooting.md#common-findings) explains the
findings new users meet most often, and `fledge rule BIB101` explains any code.

A blocked run writes no output directory. Add `--report /path/to/report.json`
to keep its full report. In `report.json`, read `outcome` first, then each entry
in `findings`: its `code`, `severity`, `status`, `path`, `line`, `message` and
`suggestion`. [Reports and exit codes](reports.md) explains every outcome and
why a run that passed with warnings exits with code 1.

## 4. Add a preset for your venue

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
`prepare`. See [start from a preset](configuration.md#start-from-a-preset).

## Faster checks while you write

`fledge inspect /path/to/paper --main main.tex` checks sources and bibliography
in seconds without compiling, so it cannot see build or PDF problems.
`fledge check` adds the isolated build and PDF checks without packaging, and
`prepare --dry-run` previews requested changes without writing a bundle.

Next: [configuration](configuration.md), [preparation options](preparation-options.md)
or the [offline example](examples.md).
