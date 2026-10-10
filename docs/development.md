# Development and documentation

Read the [hosted documentation](https://nicolasschuler.github.io/fledge/) without
installing Fledge or its documentation tools.

Work from a source checkout or extracted source distribution. Keep private papers
outside the repository or in `.local/`. Review the paths to be staged: generated
outputs and environments are ignored, while manuscript PDFs, images, and other
source assets remain trackable. The project has no selected license.

## Install from source

Python 3.11 or later is required. Create a virtual environment and install the
checkout in editable mode with the development tools:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev,docs,build]'
fledge inspect examples/nested-paper
fledge check examples/nested-paper --main main.tex
fledge prepare examples/nested-paper --main main.tex --layout flat \
  --output /tmp/prepared-paper
```

`uv tool install .` or another package manager also exposes the `fledge`
executable.

The `dev` extra provides pytest with pytest-xdist, Ruff and ty; `docs` provides Sphinx and MyST; `build`
provides the Python distribution builder. None installs TeX or PDF tools.

## Build the documentation

From the source root, install the documentation extra and build HTML:

```sh
python -m pip install -e '.[docs]'
python -m sphinx -n -W --keep-going -b html docs docs/_build/html
```

Open `docs/_build/html/index.html` in a browser. The build uses Sphinx's built-in
Alabaster theme and MyST Markdown, with warnings treated as errors and no warning
suppression. This build needs no TeX, online checks, or external theme. Its local
source/test/example links are copied into the HTML output as downloads. The
HTML build does not validate the current availability of external websites.

The [Sphinx Markdown documentation](https://www.sphinx-doc.org/en/master/usage/markdown.html)
describes enabling MyST, and the
[MyST cross-reference documentation](https://myst-parser.readthedocs.io/en/latest/syntax/cross-referencing.html)
describes document and download links.

## Publish the documentation

The [Deploy documentation workflow](https://github.com/NicolasSchuler/fledge/actions/workflows/docs.yml)
publishes this site to GitHub Pages from `main` when documentation or its included
source, tests, examples, scripts, or project metadata change. It installs the
`docs` extra and runs the strict Sphinx build above, then deploys the HTML in a
separate job only after the build succeeds. Only `main` can publish.

The repository's **Settings → Pages → Build and deployment → Source** must be
**GitHub Actions**. To publish manually, open the workflow link, choose **Run
workflow**, select `main`, and run it. The same page shows build and deployment
status; the deployed site is at <https://nicolasschuler.github.io/fledge/>.

## Run checks

```sh
python -m pytest tests -n auto
ruff check src tests docs/conf.py scripts
ruff format --check src tests docs/conf.py scripts
ty check src/latexprep
```

`tests/__init__.py` puts `src/` first on the import path, so no `PYTHONPATH` is
needed, and it fails loudly if a stale installed `latexprep` would shadow the
checkout. Remove it with `python -m pip uninstall fledge latex-preparation`, then
run `python -m pip install -e .`.

Live tests are opt-in with `FLEDGE_RUN_INTEGRATION=1`. Run them separately
where application isolation and the relevant tools can actually start:

```sh
FLEDGE_RUN_INTEGRATION=1 python -m pytest tests/test_integration.py \
  tests/test_integration_checks.py tests/test_integration_remaining.py \
  tests/test_integration_backends.py tests/test_integration_realistic.py \
  tests/test_runtime_parallel.py -v
```

`tests.test_integration_realistic` builds, checks and prepares projects with
constructs found in ordinary papers and journal templates (template classes,
`[0,1)` intervals, several matching graphics, a spare `.bib`, Biber).

Tests distinguish component and workflow tests with controlled tool doubles from
real tool execution. Controlled doubles establish component behavior, not live
toolchain support, and skipped or blocked live checks must remain visible in
validation results. Every registered code links to tests that exercise detection
or measurement with source fixtures or controlled tool output; a catalogue
integrity test rejects missing associations. Each pytest-xdist worker is a
separate process that runs one test at a time, so Click's process-global stream
capture stays isolated. Online tests use
fake transports, so they verify request handling, not provider availability.
Optional `qpdf`/`mutool` adapters must remain inconclusive when their tools or
evidence are absent. The [limitations](limitations.md) page lists what has not
been validated live.

## Continuous integration

The [Tests workflow](https://github.com/NicolasSchuler/fledge/actions/workflows/tests.yml)
runs on pushes to `main`, pull requests, manual dispatch and weekly. It lints with
Ruff and `ty`, then runs the unit tests with pytest on macOS and Ubuntu with Python 3.11 and
3.13. Manual and weekly runs add a live macOS job that installs BasicTeX, Poppler,
`tex-fmt` and Biber and runs the opt-in integration and sandbox tests. Every
third-party action in both workflows is pinned to a commit SHA.

## Agent skill

`skills/fledge/SKILL.md` is the Agent Skill described in
[use Fledge from an AI agent](agents.md). A test checks that every command and
option it names exists in the CLI, so update the skill together with CLI changes.

## Build local distributions

```sh
python -m build
```

This creates a wheel and source archive in `dist/`; it does not publish them.
The wheel installs the CLI. The source archive also carries the documentation,
examples, tests, agent skill, architecture, requirements, and benchmark script, so it can be
used to build these docs and run the documented source checks. It also includes
the opt-in macOS installer; installer tests use controlled commands, not native
Homebrew or MacTeX installation.

## Design and planning references

These source documents remain available without claiming that every planned
capability is implemented:

- {download}`Architecture <../architecture.md>`: component responsibilities,
  stage boundaries, isolation, and parallel execution design, including future
  interface work.
- {download}`Requirements <../requirements.md>`: the broader planned release and
  acceptance criteria; use [what prepare does](workflow.md) and the
  [workflow reference](workflow-reference.md) for current scope.
- [Check backlog](check-backlog.md): implemented subsets, partial behavior,
  outstanding implementation, and missing validation.
- [Parallel execution design](parallelization-plan.md): rationale and acceptance
  criteria, with current implementation status separated from historical seams.
- [Bounded benchmark](parallelization-benchmark.md): recorded local synthetic
  measurements and their limits.

`fledge rule CODE` lists the behavioral tests associated with each check.

## Extend an implemented check

Follow the current option and dispatch paths rather than treating the design
documents as an implemented plugin API. A check usually needs its typed options,
family rule metadata, execution dispatch, and selection policy in
{download}`check_policy.py <../src/latexprep/check_policy.py>` to agree. If it is
eligible for a reviewed exception, check the review policy too.

Add an explicit code, descriptive title, scope and fix text to the applicable
data-only `*_rules.py` module, registered by
{download}`rules.py <../src/latexprep/rules.py>`. Codes are never reused. Link
the rule to a unit test that runs the actual checker on a meaningful fixture and
asserts both the code and the intended behavior. Include a valid counterpart, a
violating case, and an unavailable or ambiguous evidence case when applicable.
Reuse an existing behavior test when it already proves the contract; do not add
a test that only constructs a `Finding`. Keep evidence types and failure states
intact when changing execution order, and check scheduling and serial/parallel
equivalence separately from rule behavior.

After changing a rule, regenerate the catalogue with
`python scripts/generate_check_docs.py`. It derives each code's group (on by
default, opt-in or always enforced) and its enabling settings from
`check_policy.py`, and shows the rule's fix text. A test fails while
`docs/checks.md` is stale; the generated tables are never edited by hand.

For normal preparation, online checks run once on the fresh archive extraction;
source inspection and dry runs use the initial snapshot. Preserve the request
budget per document when changing this dispatch. Controlled tests should make
the inspected content and phase explicit.

The architecture document includes target design: per-task timeout/artifact
descriptors and a structured progress-event schema are not implemented as
described there. Current execution uses progress strings and a shared whole-job
deadline alongside per-tool limits. Some evidence is parsed repeatedly, and
checker modules share private parsers. Extracting shared evidence or parser
interfaces is a future maintenance tradeoff; extending a check does not require
a new plugin framework.
