# Deeper parallel execution plan

Extend the existing local scheduler first to overlap independent PDF work, then
to compare pages and format files within one shared resource budget. Keep the
baseline, transformation, prepared-build, and archive-rebuild gates ordered.
Declared independent documents now use the same shared budget, each with its own
workspace and verified source package. Publication waits for every document.

The scheduling and declared-document delivery steps below are implemented: shared admission and
runtime accounting, dependency-driven single-document PDF work, and bounded
page/file workers. Whole-job deadlines, immutable PDF inputs, coordinator-only
mutation and cancellation draining are included. The
[bounded benchmark](parallelization-benchmark.md) records measured results and
limitations; default CPU/build/render limits remain 2/1/1. A shared parsed-PDF
evidence cache and broader performance corpora remain future work. The independent
document release check is `PKG301`; scheduling itself adds no check codes.

This document retains the design rationale and acceptance criteria. It refines the
{download}`architecture <../architecture.md>` and
{download}`requirements <../requirements.md>`; the
[workflow reference](workflow.md) remains the description of shipped behavior. No distributed
queue, web server, network lookup, new PDF parser, or additional tool installation
is needed for the steps below.

## Starting seams and constraints

The following table records the implementation **before** these changes. Current
behavior is described above and in the workflow reference; these are the issues the plan addressed.

| Code seam | Observed behavior | Consequence for parallel execution |
| --- | --- | --- |
| `core.run_job` | Schedules baseline compilation, source analysis, bibliography checks and configured manuscript checks; awaits the whole group before PDF work | PDF checks cannot begin as soon as the baseline alone finishes |
| `scheduler.Task` and `Scheduler.execute` | Validate dependency cycles and CPU/memory admission; distinguish successful prerequisites (`requires`) from completion prerequisites (`after`) | Reuse these semantics; add tool-class capacity and explicit artifact success rather than replacing the scheduler |
| `core.run_job` and `config.Settings` | Default `jobs=2`, shared admission budget 2048 MiB and build limit 1024 MiB; source/bibliography/manuscript tasks each reserve 64 MiB | Reservations are declarations, not measured peaks; they need calibration before larger fan-out |
| `runtime.build_project` | Uses a fresh project/output tree, runs ordered `latexmk` work and returns `BuildResult(success=False)` on many failures | A normal coroutine return currently counts as scheduler success even when no usable build artifact exists |
| `pdf.inspect_pdf` | Runs PDF properties, geometry and inventories sequentially | Independent inventory tools can overlap after valid input/page preflight |
| `pdf.compare_pdfs` | Prepares each side sequentially, then renders each page and side sequentially | Page pairs offer bounded independent work, but current `left-page.ppm` and `right-page.ppm` names would collide |
| `formatting.format_project` | Processes files serially; each file has two ordered formatter passes; returns proposed contents | Parallelize file-level computation while retaining both passes and coordinator-only application |
| `runtime.ToolRunner` | Holds mutable `resource_roots` and `tool_versions`; sets one source-date epoch per runner | Parallel workers need stable per-job tool state and the same epoch, not independently created timestamps |
| `pdf._run` and tool findings | Check a shared version dictionary before awaiting a probe; findings retain the same dictionary object | Concurrent probes can duplicate work, and earlier findings can change when later tools are recorded |
| `ToolRunner.run` | Kills a process group on timeout/cancellation and samples group RSS, process count and workspace size | Preserve this boundary; individual command limits alone do not bound the aggregate of concurrent commands and Python buffers |
| `asyncio.to_thread` source checks | Source, bibliography and manuscript parsing run in worker threads against the imported snapshot | Cancelling an await does not stop its running thread; temporary-directory cleanup must wait for readers to stop |
| `Report` and the collector | Findings are sorted, but stages arrive in completion order and the finding key omits stage/document/page | Define deterministic result ordering before adding more tasks |

These are code-derived constraints, not measured performance findings. Existing
scheduler and workflow tests establish bounded overlap, basic failure propagation
and controlled serial equivalence. They do not establish end-to-end speedup,
aggregate runtime memory enforcement, parallel rendering, or formatter isolation.

## Task graph and required gates

Use stable task identifiers derived from stage, document, operation and, where
applicable, relative file path or numeric page. Display descriptive task names.
Do not derive identity or output paths from task start/completion order.

```text
flowchart TD
    input["Import and freeze inputs"] --> source["Source analysis"]
    input --> bibliography["Bibliography analysis"]
    input --> baseline["Baseline build"]
    baseline --> preflight["Baseline PDF preflight"]
    preflight --> inspect["Independent PDF inventories"]
    preflight --> reference["Supplied-PDF comparison, when requested"]
    source --> plan["Reconcile evidence and resolve change plan"]
    bibliography --> plan
    inspect --> plan
    reference --> plan
    plan --> changes["Apply ordered project transformations"]
    changes --> format["Compute independent formatting patches"]
    format --> freeze["Apply patches and freeze prepared sources"]
    freeze --> prepared["Prepared build"]
    prepared --> preserve["Compare baseline and prepared PDF"]
    preserve --> archive["Write exact archive and extract afresh"]
    archive --> final["Archive build"]
    final --> finalInspect["Final PDF inspection"]
    final --> finalCompare["Prepared and archive comparison"]
    final --> finalReference["Supplied-PDF and archive comparison"]
    finalInspect --> release["Collect required results and publish"]
    finalCompare --> release
    finalReference --> release
```

The diagram shows successful artifact flow. Diagnostic collection depends on
completion and still runs after failed or blocked nodes. An omitted optional
branch has an explicit not-requested state and cannot become a missing required
dependency. A supplied-PDF comparison remains required when requested, matching
the current CLI behavior.

Implement the graph in this order:

1. Add baseline PDF inspection and the optional baseline/reference comparison as
   consumers of a successful baseline artifact in the initial graph. Source and
   bibliography/manuscript checks continue independently. Planning waits for all
   applicable evidence, including observed dependencies; no cleanup or rewrite
   runs early.
2. After the archive build, run final inspection, prepared/archive comparison and
   the optional reference/archive comparison concurrently. Publication waits for
   all required checks, not the first successful branch.
3. Split inspection into preflight and independent font, text, attachment and
   image inventory operations. Reuse one validated properties/geometry result
   for the relevant immutable PDF. A new checker should consume this evidence
   when compatible instead of invoking the same parser or tool again.
4. Split each comparison into side preflight/text extraction, ordered comparison
   assembly, bounded page-pair work, and a final collector. Geometry validation
   precedes rendering; compare only common pages and report missing pages
   explicitly. Preserve the current 144 DPI, exact RGB comparison, page/dimension
   caps and text normalization. Concurrency must not weaken comparison policy.

The outer workflow can retain explicit phase boundaries and several scheduler
calls. Within a phase, downstream tasks must start when their own prerequisites
finish. Page count determines a later bounded set of leaf operations; do not
require a general dynamic workflow engine to express it.

Never overlap a build or archive reader with mutations to its input tree. Keep
passes within one `latexmk` build ordered. Dry runs still stop before prepared and
archive verification. Scoped review decisions are supplied in configuration;
a later interactive approval flow must hold no execution capacity while waiting.

## One resource budget for every expensive operation

Introduce one per-job admission controller in `scheduler.py`, shared by phase
schedulers and adapter helpers. Reserve CPU units, memory and optional build or
render slots atomically; release them in `finally` after children have exited and
owned buffers have been released. Reject requests larger than any configured
total before launching work. No adapter creates a separate unconstrained pool.

A leaf operation owns one lease. A composite PDF/formatting task that merely
coordinates leaves owns no CPU or tool lease while waiting for them. A build leaf
can hold its lease for copy, ordered tool execution and artifact validation; its
`ToolRunner` calls use that lease instead of reserving it again. This avoids a
parent holding all capacity while its children wait for that same capacity.

| Budget | Proposed behavior |
| --- | --- |
| CPU (`jobs`) | Retain `--jobs 1` as genuinely serial expensive execution across all phases. Charge subprocess work and Python workers to the same total. Keep the current default until benchmarks justify a change. |
| Builds (`build_jobs`) | Add an explicit cap, initially one. It becomes useful when multiple declared roots/packages are supported; it does not parallelize passes inside one build. |
| Rendering (`render_jobs`) | Add an explicit cap, initially one until measured. A page-pair leaf renders its two sides sequentially, so one lease means at most one active renderer; different page pairs can overlap when enabled. |
| Memory (`memory_mb`) | Count parent/parser/buffer reservations plus command-group limits. A command's configured RSS cap cannot exceed the lease backing it. Report estimates separately from sampled observations. |
| Per-operation limits | Preserve wall time, captured-output, process-count, file-size and workspace limits. Add an explicit overall job deadline so hundreds of individually bounded pages cannot extend execution indefinitely. Queue wait counts toward the job deadline, not the command's execution timeout. |
| Disk and queued artifacts | Retain existing per-workspace limits and enforce a job-wide temporary-byte ceiling. Bound active/ready raster buffers; do not retain every page image in memory or fill the disk before collecting results. |

At the current maximum of 16 million pixels, two RGB rasters contain about
96 million bytes before Python object overhead. `_read_raster` also copies slices
while decoding, and Poppler needs additional memory. A 64 MiB generic task
reservation is therefore not an adequate page-pair budget. Calculate raster
storage from validated dimensions, add measured decoding/tool headroom, and admit
only as many page pairs as fit. Stream or release each completed pair after its
small comparison result is recorded.

Replace one full `ps` scan per active tool with one job monitor when concurrency
grows. Track registered process groups, the coordinating process and any parser
workers without double counting; retain per-command failures and add an explicit
job-budget failure. Measure monitor overhead. Sampling remains a reactive bound
with an overshoot interval, not an operating-system hard RSS guarantee. Keep
single-thread environment settings for supported subprocess libraries; record
and budget tools that still create their own workers.

## Workspace and shared-state ownership

The coordinator creates task directories exclusively and supplies immutable
inputs. Each build retains its own project tree, output tree and generated
bibliography. Final verification always uses a new extraction of the exact ZIP
and fresh build outputs, regardless of earlier cached analysis.

For PDF work, copy each completed input PDF once into an immutable job artifact
location. Copy the user-supplied reference PDF once as well, so different stages
cannot silently compare different versions of a file changed during the job.
Every rendering operation owns a path containing the comparison, page and side,
for example `compare-prepared/page-0001/left/page.ppm`. Each invocation also owns
its tool-state/temp directory; filename changes alone do not isolate cache writes.

The current sandbox gives write access to its entire `workspace`. Before running
many PDF leaves, extend `ToolRunner` narrowly to accept explicit read-only input
files outside an operation's writable workspace. Validate regular files and
containment in coordinator-owned artifact locations; never allow a caller to
grant a parent tree or arbitrary user directory. Give a rendering leaf only its
output directory plus declared PDF/toolchain reads. This avoids copying a large
PDF for every page while retaining exclusive write ownership. Test denied sibling
writes and undeclared reads through the actual supported sandbox.

Prepare an immutable per-job tool context containing resolved executables,
declared resource roots and the shared source-date epoch. Resolve/verify versions
once per tool with an in-flight future or lock so concurrent callers await the
same probe. Key results by the selected executable and operation-relevant version
flags; preserve Poppler's existing `pdfdetach` version-exit handling. Failed or
cancelled probes must not publish success entries. Snapshot tool dictionaries
into results, and use each command's declared roots for recorder validation
instead of a timing-dependent union accumulated from other operations.

Reuse parsed evidence only within a known immutable input state and compatible
settings/tool context. Simple stage/artifact identifiers are sufficient inside one
job; a persistent content cache and manifest system are unnecessary. Publish a
complete immutable result atomically. Do not share mutable parser/PDF handles or
return a live shared dictionary in a finding. Account for retained parsed data
and optional raster reuse in the same memory budget.

## Formatting and parser execution

Refactor `format_project` into project enumeration, one per-file formatter worker,
and deterministic result assembly. Each worker reads one frozen source file and
writes only its own workspace. The first pass must finish before its second
idempotence pass. Return relative path, original bytes or an equivalent local
precondition, proposed bytes, diff and findings. The coordinator verifies that
each target still matches the expected input, rejects duplicate/overlapping
targets and applies all accepted results in sorted order after required workers
finish. Any required failure prevents partial patch application.

Keep cleanup, DOI normalization, flattening and reviewed cross-file citation
changes in their existing ordered transformation chain. Schedule formatting on
the resulting frozen paths, then apply patches with `_apply_contents`. Readers
start only after this coordinator step. Formatter-directive removal, if later
implemented, follows formatting. No formatter worker renames files or updates
cross-file references.

Keep tool orchestration in asyncio and use the existing isolated subprocesses;
wrapping Poppler or `tex-fmt` in Python process pools adds no useful computation
parallelism. Initially retain bounded threads for source/bibliography analysis
and file I/O. More Python threads do not establish CPU scaling for Python parsing.
Share immutable parse results between rules before adding parser workers.

Make thread cancellation cooperative at bounded file/record boundaries and keep
the snapshot alive until active readers stop. Preserve the underlying worker
future so cancelling its asyncio waiter cannot lose lifecycle ownership. If
profiling shows a significant CPU-bound parser bottleneck, evaluate a small
spawn-based process worker design with serializable immutable inputs/results,
memory accounting and explicit stop/join behavior. A Python 3.11 process-pool
future alone cannot terminate already running parsing. Do not introduce that
complexity without a measured benefit or a required hard cancellation boundary.

## Result, failure and cancellation semantics

Add an explicit adapter-to-scheduler outcome contract. In particular, preserve
the diagnostics in `BuildResult(success=False)` while marking its artifact
producer unsuccessful; dependent PDF tasks must never run merely because the
coroutine returned normally. Keep execution success separate from check success:
a completed comparison that detects changed pages has valid diagnostic output,
but its failed preservation finding blocks the release gate.

The coordinator alone mutates `Report`, applies changes and publishes output.
Workers return values and structured events. Sort final findings by stable
document, stage, path, page, line, registered code where applicable, descriptive
rule name and a deterministic tie-breaker. Sort changes, tool metadata and final
stage records independently of completion order; keep live event ordering and
timings separate. Do not turn ordinary inventories or progress events into coded
checks. Registered diagnostic codes retain their descriptive rule names and
existing severity/status/evidence semantics.

Failures block only consumers that need the unsuccessful artifact. Independent
analysis and diagnostic assembly continue. Required missing tools, timeouts,
resource violations and incomplete checks block publication; optional unavailable
work stays visible without erasing other findings. On the first cancellation,
stop admission, mark queued work cancelled, stop active tool process groups,
signal parser workers, await cleanup, and release leases exactly once. Preserve
completed findings and explicit incomplete task states. Repeated cancellation
must not interrupt mandatory child reaping or allow temporary-directory cleanup
while a worker still owns it. Never publish an incomplete package.

## Delivery steps and acceptance checks

| Step | Concrete files to change | Required evidence before continuing |
| --- | --- | --- |
| Establish safe scheduling contracts | `src/latexprep/scheduler.py`, `runtime.py`, `config.py`, `cli.py`, `models.py`; `tests/test_scheduler.py`, `test_runtime.py`, `test_config.py` | Atomic shared admission, unsatisfiable request rejection, no nested-lease deadlock, failed build artifact propagation, stable tool context, cancellation cleanup and honest memory-accounting status |
| Extend the single-document graph | `src/latexprep/core.py`, `pdf.py`; `tests/test_workflow.py`, `test_pdf.py` | PDF work begins after its build without waiting for unrelated analysis; independent final branches overlap; release still waits for every required branch |
| Add bounded page and file workers | `src/latexprep/pdf.py`, `formatting.py`, targeted runner input isolation; `tests/test_pdf.py`, `test_formatting.py`, `test_integration.py` | Unique outputs/temp state, bounded memory/disk, ordered formatter passes, no partial mutation, serial/concurrent result equivalence and actual sandbox isolation |
| Measure and tune | `scripts/benchmark_parallelism.py`, generated fixtures and [documented results](parallelization-benchmark.md); update defaults only when justified | Recorded end-to-end and stage timings, observed resource peaks, unchanged findings/prepared contents, supported conclusions or an explicit no-benefit result |
| Support declared independent documents (implemented) | `workflow_options.py`, `config.py`, `core.py`, `tests/test_multi_document.py`, `test_integration_remaining.py` | Explicit file selections, isolated workspaces and independently rebuilt exact packages; no package is published if another fails; shared budgets and deterministic collection |

Each registered diagnostic code needs a meaningful behavioral unit test in its
owning component, not just a registry-membership assertion. Exercise the triggering
fixture and assert the code plus relevant status, severity, location and evidence;
include valid or ambiguous controls where applicable. New scheduler/runtime codes
need concrete failure or cancellation fixtures. For concurrency-sensitive paths,
run controlled fixtures serially and concurrently and compare the same coded
diagnostic behavior, including inconclusive states. Pure inventory/progress
messages do not count toward this coverage requirement.

Use event/barrier-controlled tool doubles, not timing guesses, to test overlap
and ordering. Exercise page results completed out of order, equal job names in
separate workspaces, one missing renderer, failed build values, interrupted tool
version probes, first/second formatter-pass failures, resource exhaustion and
cancellation with queued plus active work. Assert no reader sees partially
applied transformations and no blocked/cancelled job releases output. For fixed
prepared inputs, serial and concurrent packaging must produce identical ZIP
bytes; findings and changes must match apart from documented runtime fields.

Run the existing unit suite plus changed-file lint, formatting and type checks.
Keep Click/Rich CLI tests isolated with `CliRunner` or captured stdout/stderr;
verify exit codes and JSON-only stdout separately from Rich progress on stderr.
Do not run concurrent `CliRunner` invocations in the same process, where their
temporary stream/environment changes would share state.
Real sandbox/build tests are a separate gate: use an environment where macOS
`sandbox-exec` can start, preserve unsupported-platform failures, and label absent
tools or sandbox refusal as skipped/blocked rather than a pass. No automatic
installation or unsandboxed fallback is part of this plan.

## Benchmark design and decision rules

Start with a declared bounded corpus: a small single-file article; a multi-file
project; a many-page/figure-heavy PDF; a bibliography-heavy project; and many
independent formatting files. Include mixed page dimensions and resource-limit
fixtures. Use the existing nested-paper example as a smoke fixture, not as a
representative performance corpus. Test multiple documents/packages only after
that product capability exists.

Compare `jobs=1` with explicit bounded settings such as two and four CPU units,
keeping build/render caps, input bytes, TeX engine, tool versions, source-date
epoch, comparison policy and transformations controlled and reported. Change one
parallelization step at a time. First run one pilot per corpus/settings pair;
estimate and approve a finite local benchmark time budget before larger repeats.
Within that budget, use at least three measured repetitions per selected pair,
alternate execution order, and record warm/cold cache conditions. Do not purge
user caches. Report individual timings and medians/ranges; small samples do not
support latency guarantees.

Record end-to-end time, phase/task execution and queue wait, time to first useful
finding, command counts, tool-probe counts, active CPU/build/render reservations,
sampled aggregate/command/parent memory peaks, temporary disk high-water mark,
monitor overhead and cancellation latency. Mark unavailable metrics explicitly.
Compare findings, outcome, prepared contents and deterministic archive bytes
alongside timing. Separate tool-double orchestration results from real tool runs.

The hypotheses are that independent PDF inventories reduce waiting, page-pair
work helps long comparisons, and per-file formatting helps larger projects.
Process startup, copying, memory pressure or the ordered build chain may dominate
instead. Retain serial mode and current defaults unless measured improvements
justify a change without correctness, resource or cancellation regressions.
Remove or defer complexity whose benefit is absent on the declared corpus; do
not generalize results to other toolchains, document types or machines.
