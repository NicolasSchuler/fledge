# Bounded local parallelization benchmark

On two generated documents, the parallel configuration reduced median preparation
time by 17.9 percent and 16.4 percent while preserving findings, prepared source
bytes, and exact ZIP bytes. This is local synthetic evidence, not a performance
guarantee or a reason to change the default settings.

## Method

The opt-in {download}`driver <../scripts/benchmark_parallelism.py>` generates two disposable
projects: a one-page article with one source file, and a six-page article with a
root, six included source files, and one generated raster image placed on every
page. Each run performs the complete `prepare` workflow, including two formatting
passes per source file, baseline and prepared builds, exact text/rendered
comparisons, and a fresh build from the final ZIP. Outputs are outside the input
projects and are removed when the benchmark finishes.

The compared configurations are `jobs=1, render_jobs=1` and
`jobs=4, render_jobs=2`. Both retain one build slot, a 2,048 MiB admission budget,
a 1,024 MiB build limit, the same `pdflatex` engine and formatting policy, and
`SOURCE_DATE_EPOCH=1700000000`. The application sandbox remains enabled. The
benchmark does not install tools, clear system caches, or reuse build trees.

Each corpus/configuration pair receives one pilot followed by three measured
repetitions. The measured order alternates between serial/parallel and
parallel/serial. Pilots prime unpurged local caches and are excluded from the
reported timing summaries. Admission stops before the finite wall budget to
allow child reaping and cleanup. A corpus is skipped if its complete repetition
set does not fit the remaining time with a 25 percent pilot-based margin.

The driver compares outcomes, every normalized finding and change, stage status,
tool metadata, prepared source bytes, and exact ZIP bytes against the first
successful pilot for that corpus. Normalization replaces disposable workspace
paths and excludes stage timing/queue fields from equality; evidence, stage
labels, statuses, severities, and tool versions remain in the comparison. Any
preparation error or equivalence difference stops subsequent measurements.

## Measurements

The JSON report records every run, stage timing, leaf operation and queue wait,
command and version-probe counts, peak CPU/build/render reservations, sampled
parent/tool/aggregate RSS, sampled temporary bytes, and monitor elapsed time.
End-to-end elapsed time includes application publication and cleanup; the core's
execution time is also retained separately. Summed queue and operation durations
can overlap and must not be interpreted as additional wall time.

Memory and temporary-byte peaks are reactive samples, not operating-system hard
limits or exact instantaneous maxima. Monitor time is measured elapsed time spent
sampling, not CPU time. The report explicitly marks time to first useful finding
and cancellation latency unavailable: successful preparations do not inject a
cancellation workload, and the public report API does not timestamp findings.

## Observed local results

The complete run on 2026-10-06 used macOS 27.0 on arm64 with 10 logical CPUs,
Python 3.13.5, TeX Live 2026/pdfTeX 1.40.29, latexmk 4.88, Poppler 26.10.0, and
tex-fmt 0.5.7. All four pilots and twelve measured preparations completed with
advisories. The only findings with a non-passing status were the existing
annotation/media scope warnings, once per PDF inspection. Every equality check
passed; neither original source tree changed.

| Corpus / mode | Three measured times (s) | Median (s) |
| --- | --- | --- |
| One page, serial | 3.383, 3.372, 3.406 | 3.383 |
| One page, parallel | 2.781, 2.776, 2.762 | 2.776 |
| Six pages, serial | 8.958, 9.211, 8.876 | 8.958 |
| Six pages, parallel | 7.490, 7.613, 7.475 | 7.490 |

For the six-page corpus, median baseline PDF inspection fell from 0.448 to
0.253 seconds, and the final prepared/archive comparison fell from 1.107 to
0.541 seconds. Baseline build time remained about 2.06 seconds, and the three
ordered build calls together occupied about 5.9 seconds. These stage observations
are consistent with reduced PDF waiting; the experiment does not isolate the
contribution of each scheduling change.

The table below gives ranges across the three measured runs. RSS includes the
coordinator and registered tool process groups. Temporary-byte peaks are sampled
and can miss short-lived rasters; the lower sampled one-page parallel values do
not establish lower disk requirements.

| Corpus / mode | RSS peak (MiB) | Temp peak (MiB) | Monitor time (s) |
| --- | --- | --- | --- |
| One page, serial | 124.23–129.72 | 0.872–2.642 | 0.525–0.563 |
| One page, parallel | 129.56–134.08 | 0.183–0.183 | 0.491–0.517 |
| Six pages, serial | 125.13–128.89 | 0.685–5.982 | 1.500–1.613 |
| Six pages, parallel | 130.55–131.00 | 5.982–5.982 | 1.276–1.317 |

Every serial run peaked at one active command; every parallel run reached four.
The six-page renderer reservation peaked at one versus two, and builds remained
capped at one. Peak total reservations, including the 128 MiB coordinator reserve,
were 1,152 MiB serial and 1,344 MiB parallel. These reservations are distinct from
the observed RSS above. Command counts remained 42 for one page and 74 for six
pages, with nine version probes per job in both configurations. Median summed
queue waits were 2.393 versus 0.212 seconds for one page and 6.486 versus
1.173 seconds for six pages; these overlapping waits are not wall-time savings.

The measured run took 90.690 seconds including pilots and cleanup. A previous
attempt was deliberately interrupted after 60.501 seconds to incorporate the
renderer command-memory cap; its incomplete measurements are excluded. Combined
benchmark execution was 151.191 seconds, within the 180-second allowance. No tool
failure or equivalence failure occurred before that deliberate interruption.

The measured process included the 128 MiB renderer cap. The final 128 MiB command
cap for formatter passes was added after this process imported its modules, so
these timings describe the snapshot before that formatter-cap tightening. A
separate full-workflow smoke test passed after both final caps were applied
(12.7 seconds); performance under the final formatter cap was not remeasured.

## Reproduction

From the repository root, using an existing environment with the project and
required tools available:

```sh
PYTHONPATH=src python scripts/benchmark_parallelism.py \
  --max-seconds 180 --output /tmp/parallelization-benchmark.json
```

The report path must not already exist. Click handles options and Rich writes
progress to stderr; omitting `--output` writes plain JSON to stdout. Run where
the application's macOS `sandbox-exec` can start. A refused nested sandbox blocks
the run, with no fallback to unrestricted application commands.

## Scope and decision

The benchmark changes CPU and render limits together, so it cannot assign any
observed improvement to an individual scheduling change. It does not evaluate
bibliography-heavy projects, mixed page geometry, resource-limit failures,
multiple selected document roots, cold caches, or cancellation latency. Those
correctness and failure cases remain separate tests. Three repetitions on two
small synthetic documents do not establish latency guarantees or support a
default-setting change.
