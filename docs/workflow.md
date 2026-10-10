# What prepare does and its limits

`fledge prepare` turns your project into a submission ZIP that is proven to
rebuild the same PDF. It works on a separate copy and never edits your files.
It releases the ZIP only when every required step succeeds.

## The steps

```text
your project ──copy──▶ snapshot ──build──▶ baseline PDF
                                               │
          needed inputs + requested changes ◀──┘   (build trace decides "needed")
                          │
                     prepared copy ──build──▶ prepared PDF ──compare with baseline
                          │
                     submission.zip ──extract fresh──build──▶ archive PDF ──compare
                          │
                   publish output only if every required step passed
```

1. **Snapshot.** Fledge imports the folder or ZIP into a private workspace. It
   rejects paths that escape the project, links and oversized inputs, and drops
   operating-system metadata.
2. **Baseline build.** Fledge compiles the original in a sandbox with shell
   escape and network access disabled. The build trace (the TeX recorder plus
   the `latexmk` database) records every file TeX read.
3. **Select needed inputs.** The trace and the literal dependency graph decide
   which files ship. Unrelated drafts, images and data are left out. Files the
   build never reads but must still ship go in
   [`[package] include`](configuration.md#package-contents). A conservative
   cleanup also removes rebuildable debris such as `.aux`, `.log` and editor
   backups, but never a `.bbl`; `--no-cleanup` skips only that cleanup. A
   generated `main.bbl` (and `main.run.xml`) beside the main file is kept
   automatically for services that do not run BibTeX or Biber.
4. **Checks.** Source, bibliography and PDF checks run on the selected inputs
   and the baseline PDF: missing files, citations, labels, TODO markers and
   anything you configured.
5. **Apply requested changes.** Only changes you asked for are applied, such
   as `--layout flat`, comment removal or bibliography formatting. See
   [preparation options](preparation-options.md).
6. **Prepared build and comparison.** The changed copy is rebuilt and compared
   with the baseline by page count, extracted text and page renders.
7. **Archive rebuild.** Fledge writes the ZIP, extracts it into a fresh
   directory, builds it again and requires an exact match. Source and
   bibliography policies run again on the extracted files.
8. **Publish.** Only now does the output directory appear. A blocked, expired or
   cancelled run publishes nothing.

## What each command covers

| Command | Covers |
| --- | --- |
| `fledge inspect INPUT` | Source and bibliography checks on the snapshot; no build, so no TeX needed |
| `fledge check INPUT` | Those checks plus the baseline build and PDF checks |
| `fledge prepare INPUT --dry-run` | `check` plus a preview of requested changes; no bundle |
| `fledge prepare INPUT --output DIR` | Every step above |
| `fledge bib check INPUT` | Bibliography checks only |
| `fledge fmt INPUT --check` | Read-only `tex-fmt` formatting check |
| `fledge pdf check FILE` | Configured PDF checks on an existing PDF |

Without `workflow.documents`, Fledge builds one document. To verify a paper and
its supplement separately, see
[independent packages](preparation-options.md#independent-manuscript-and-supplement-packages).

## What a pass means

A passing `prepare` means the ZIP contains what the build needed, rebuilds
from a fresh extraction and matches your original PDF within the stated
comparison. It does not mean the paper meets a venue's rules unless you
configured those rules, and it does not judge the content. The
[limitations](limitations.md) page lists what Fledge cannot establish, and the
[workflow reference](workflow-reference.md) covers package selection, report
formats, redaction and resource limits in detail.
