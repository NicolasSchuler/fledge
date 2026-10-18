"""Shared test helpers: project fixtures, controlled document backends and golden outputs.

New tests import from here (and from ``tests.fakes`` and ``tests.pdf_fakes``) instead of
redefining ``write``/``build`` helpers or importing one test module from another.
"""

from __future__ import annotations

import base64
import os
import sys
import tempfile
import unittest
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Mapping
from pathlib import Path
from typing import Any, TypeVar

from latexprep.build_result import BuildResult, BuildTools
from latexprep.command_runner import CommandRunner
from latexprep.config import Settings
from latexprep.findings import Finding
from latexprep.formatting import FormattingResult, format_project
from latexprep.job_context import DocumentBackends
from latexprep.package_headers import LoadedPackage

try:
    import pytest

    _LIVE_MARKER = pytest.mark.live
except ImportError:  # the suite also runs under `python -m unittest`

    def _LIVE_MARKER(target):
        return target


_T = TypeVar("_T")

MINIMAL_PDF = b"%PDF-controlled-test\n"
MINIMAL_DOCUMENT = "\\documentclass{article}\n\\begin{document}Hello.\\end{document}\n"


def write_file(root: Path, name: str, content: str | bytes = "") -> Path:
    """Write ``content`` (UTF-8 for text) to ``root / name``, creating parents; return the path."""
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
    return path


class TemporaryRoot(unittest.TestCase):
    """A private temporary directory ``self.root`` for each test, removed afterwards.

    The base of ``ProjectTestCase`` and ``AsyncProjectTestCase``: ``setUp`` runs for
    ``TestCase`` and ``IsolatedAsyncioTestCase`` alike, and cleanup is registered with
    ``addCleanup`` so it runs even when ``setUp`` of a subclass fails afterwards. The root is
    resolved, so that paths compare equal to the real locations the code under test reports.
    """

    root: Path

    def setUp(self) -> None:
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def write(self, name: str, content: str | bytes = "") -> Path:
        """Write a file below ``root``; the same helper every test module used to define."""
        return write_file(self.root, name, content)

    def write_project(self, files: Mapping[str, str | bytes], name: str = "project") -> Path:
        """Create the directory ``root / name`` holding ``files``; return it."""
        return write_project(self.root / name, files)


class ProjectTestCase(TemporaryRoot):
    """A test case with a temporary ``root`` and ``write`` / ``write_project`` helpers."""


class AsyncProjectTestCase(TemporaryRoot, unittest.IsolatedAsyncioTestCase):
    """The asynchronous counterpart of ``ProjectTestCase``."""


def write_project(root: Path, files: Mapping[str, str | bytes]) -> Path:
    """Create ``root`` and write each relative path with its text or bytes; return ``root``."""
    root.mkdir(parents=True, exist_ok=True)
    base = root.resolve()
    for name, value in files.items():
        destination = (base / name).resolve()
        if base not in destination.parents:
            raise ValueError(f"Fixture path escapes the project root: {name}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(value.encode() if isinstance(value, str) else value)
    return root


def fake_build_result(
    work: Path,
    main: str = "main.tex",
    *,
    success: bool = True,
    pdf_bytes: bytes = MINIMAL_PDF,
    findings: Iterable[Finding] = (),
    dependencies: Iterable[str] | None = None,
    submission_inputs: Iterable[str] | None = None,
    tools: BuildTools | None = None,
    command: Iterable[str] = (),
    log: str = "",
) -> BuildResult:
    """Return a ``BuildResult`` as ``latexprep.latexmk_build.build_project`` would after a build.

    Like the controlled builders in the existing tests, this writes ``work/built.pdf`` (creating
    ``work``) and reports ``main`` as both the only dependency and the only submission input,
    unless the arguments say otherwise. A failed build records no PDF. The recorder is reported
    complete with no loaded packages, so dependency checks treat the trace as authoritative.
    """
    work.mkdir(parents=True, exist_ok=True)
    pdf = work / "built.pdf"
    if success:
        pdf.write_bytes(pdf_bytes)
    return BuildResult(
        success=success,
        pdf=pdf if success else None,
        findings=list(findings),
        recorder_project_inputs={main} if dependencies is None else set(dependencies),
        tools=tools or BuildTools(),
        command=list(command),
        log=log,
        loaded_packages=[],
        recorder_complete=True,
        submission_inputs={main} if submission_inputs is None else set(submission_inputs),
    )


async def fake_build(tree, main, work, engine, runner, **_options) -> BuildResult:
    """A controlled build backend: succeeds with ``main`` as the only dependency."""
    return fake_build_result(work, main)


def _backend(behaviour: object) -> Callable[..., Awaitable[object]]:
    """An async backend that does what ``behaviour`` says.

    A callable is the backend itself (an async function or an ``AsyncMock``), an exception
    instance is raised on every call, and any other value is returned on every call; lists
    are copied so one job cannot change what the next receives.
    """
    if callable(behaviour):
        return behaviour

    async def backend(*_arguments: object, **_options: object) -> object:
        if isinstance(behaviour, BaseException):
            raise behaviour
        return list(behaviour) if isinstance(behaviour, list | tuple) else behaviour

    return backend


def controlled_backends(
    *,
    build: object = fake_build,
    inspect_pdf: object = (),
    compare_pdfs: object = (),
) -> DocumentBackends:
    """Document backends that never start a tool, to pass to ``run_job(..., backends=...)``.

    By default a build succeeds with ``main`` as its only dependency, and a PDF inspection
    and comparison find nothing. Each argument replaces one backend: pass an async callable
    (it receives the arguments the real function would), a ``BuildResult`` or a list of
    findings to return every time, or an exception to raise.
    """
    return DocumentBackends(
        build=_backend(build),
        inspect_pdf=_backend(inspect_pdf),
        compare_pdfs=_backend(compare_pdfs),
    )


def sectioned_project(root: Path) -> None:
    (root / "sections").mkdir(parents=True)
    (root / "appendix").mkdir()
    (root / "main.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\n"
        "\\input{sections/results}\n\\input{appendix/results}\n\\end{document}\n"
    )
    (root / "sections/results.tex").write_text("Main results.\n")
    (root / "appendix/results.tex").write_text("Additional results.\n")


def minimal_project(root: Path, main: str = "main.tex") -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / main).write_text("\\documentclass{article}\n\\begin{document}Hello.\\end{document}\n")


def single_file_project(root: Path, body: str = "Body.", preamble: str = "") -> None:
    root.mkdir()
    (root / "main.tex").write_text(
        "\\documentclass{article}\n"
        + preamble
        + "\n\\begin{document}\n"
        + body
        + "\n\\end{document}\n"
    )


async def build_with_loaded_class(tree, main, work, engine, runner, **_options):
    """Orchestration double only; live TeX evidence belongs to opt-in integration tests."""
    work.mkdir()
    pdf = work / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.7\ncontrolled build result\n")
    return BuildResult(
        success=True,
        pdf=pdf,
        recorder_project_inputs={main},
        submission_inputs={main},
        command=["controlled-build"],
        recorder_complete=True,
        loaded_packages=[
            LoadedPackage(
                "article.cls",
                "/toolchain/article.cls",
                "toolchain",
                declared_date="2025/01/01",
                declared_version="fixture",
            )
        ],
    )


# Rules the graph emits that do not leave it incomplete: how a reference was written, which
# directories are searched, and the choice of the main file, which callers report themselves.
SOURCE_GRAPH_ADVISORY_RULES = frozenset(
    {
        "source.path_option",
        "source.path_identity",
        "source.dynamic_search_path",
        "source.scoped_search_path",
        "source.search_path_syntax",
        "source.main_invalid",
        "source.root_ambiguous",
        "source.root_missing",
    }
)


def directory_contents(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


class RecordedBuild:
    """Supply fixed per-document traces; fake PDFs validate orchestration only."""

    def __init__(
        self,
        traces: dict[str, tuple[set[str] | None, ...]],
        *,
        fail_archive_main: str | None = None,
        generated_reads: frozenset[str] = frozenset(),
    ) -> None:
        self.traces = traces
        self.generated_reads = generated_reads
        self.fail_archive_main = fail_archive_main
        self.counts: Counter[str] = Counter()
        self.visits: list[tuple[str, str, dict[str, bytes]]] = []

    async def __call__(self, tree, main, work, _engine, _runner, **_kwargs):
        index = self.counts[main]
        self.counts[main] += 1
        self.visits.append((main, work.name, directory_contents(tree)))
        trace = self.traces[main][index]
        work.mkdir(parents=True)
        pdf = work / "built.pdf"
        pdf.write_bytes(b"%PDF-controlled dependency packaging fixture\n")
        success = not (work.name == "archive-build" and main == self.fail_archive_main)
        result = BuildResult(
            success=success,
            pdf=pdf if success else None,
            findings=[]
            if success
            else [Finding("build.fixture", "Controlled archive rebuild failure", severity="error")],
            recorder_project_inputs=set(trace or ()),
            submission_inputs=None if trace is None else set(trace),
            generated_reads=self.generated_reads,
        )
        return result


async def format_with(
    source: Path,
    work: Path,
    runner: CommandRunner,
    settings: Settings | None = None,
    **options: object,
) -> FormattingResult:
    """Format with the formatting settings of ``settings``, as the job core passes them."""
    settings = settings or Settings()
    return await format_project(source, work, runner, settings.formatting_options, **options)


def live_tests_enabled(*legacy: str) -> bool:
    """Whether opt-in live tool tests run: ``FLEDGE_RUN_INTEGRATION=1`` or a legacy variable."""
    return any(
        os.environ.get(name) == "1"
        for name in ("FLEDGE_RUN_INTEGRATION", "LATEX_PREP_RUN_INTEGRATION", *legacy)
    )


def live_test(reason: str, *legacy: str, platform: str | None = None) -> Callable[[_T], _T]:
    """Mark a test, or a test class, as live: it drives real tools and is opt-in.

    It is skipped unless ``FLEDGE_RUN_INTEGRATION=1`` (or a ``legacy`` variable) is set, and
    on any platform other than ``platform`` when one is given. Under pytest the ``live``
    marker also selects it with ``-m live``. ``reason`` is the skip message.
    """
    enabled = live_tests_enabled(*legacy) and (platform is None or sys.platform == platform)

    def decorate(target: _T) -> _T:
        return _LIVE_MARKER(unittest.skipUnless(enabled, reason)(target))

    return decorate


# A valid 1 x 1 PNG, for tests that capture or embed preview images.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+ap1kAAAAASUVORK5CYII="
)


# Golden files: expected outputs kept under tests/golden and compared as text.
GOLDEN_DIRECTORY = Path(__file__).resolve().parent / "golden"
UPDATE_GOLDEN_VARIABLE = "FLEDGE_UPDATE_GOLDEN"


def assert_matches_golden(case: unittest.TestCase, name: str, actual: str) -> None:
    """Assert that ``actual`` equals the golden file ``tests/golden/<name>``.

    With ``FLEDGE_UPDATE_GOLDEN=1`` the file is written instead, so a deliberate change of an
    output is reviewed as a diff of the golden file rather than edited by hand.
    """
    path = GOLDEN_DIRECTORY / name
    if os.environ.get(UPDATE_GOLDEN_VARIABLE) == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual, encoding="utf-8")
        return
    if not path.is_file():
        case.fail(f"Missing golden file {path}; create it with {UPDATE_GOLDEN_VARIABLE}=1")
    case.assertEqual(
        actual,
        path.read_text(encoding="utf-8"),
        f"{name} differs from its golden file; if the change is intended, "
        f"regenerate it with {UPDATE_GOLDEN_VARIABLE}=1 and review the diff",
    )


# The report keys that hold a measured duration.
TIMING_KEYS = frozenset({"elapsed_seconds", "queue_seconds", "duration_seconds"})


def normalise_report_data(
    value: object,
    replacements: Mapping[str, str],
    *,
    measured: frozenset[str] = frozenset(),
    in_measured: bool = False,
) -> object:
    """A report with what varies between runs replaced by a placeholder.

    Each text in ``replacements`` (a path, a version) is replaced inside every string, longest
    first. Numbers under a timing key (``elapsed_seconds``, ``queue_seconds``), and every number
    inside a section named in ``measured`` (resource accounting, which depends on timing and the
    host), become ``"<seconds>"`` and ``"<measured>"``. Keys, structure and all other values,
    including configured limits such as ``timeout_seconds``, are kept.
    """
    if isinstance(value, str):
        for old in sorted(replacements, key=len, reverse=True):
            value = value.replace(old, replacements[old])
        return value
    if isinstance(value, list):
        return [
            normalise_report_data(item, replacements, measured=measured, in_measured=in_measured)
            for item in value
        ]
    if isinstance(value, dict):
        result: dict[str, object] = {}
        for key, item in value.items():
            is_number = isinstance(item, int | float) and not isinstance(item, bool)
            if is_number and key in TIMING_KEYS:
                result[key] = "<seconds>"
            elif is_number and in_measured:
                result[key] = "<measured>"
            else:
                result[key] = normalise_report_data(
                    item,
                    replacements,
                    measured=measured,
                    in_measured=in_measured or key in measured,
                )
        return result
    return value


# The JSON Schema keywords that validate (the subset the published schemas use, with local
# references) and the ones that only annotate; any other keyword is refused.
_VALIDATING_KEYWORDS = frozenset(
    {"type", "required", "properties", "additionalProperties", "enum", "items", "const", "$ref"}
)
_ANNOTATION_KEYWORDS = frozenset({"$schema", "$id", "$defs", "title", "description"})
_SCHEMA_TYPES: dict[str, Callable[[object], bool]] = {
    "object": lambda value: isinstance(value, dict),
    "array": lambda value: isinstance(value, list),
    "string": lambda value: isinstance(value, str),
    "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "number": lambda value: isinstance(value, int | float) and not isinstance(value, bool),
    "boolean": lambda value: isinstance(value, bool),
    "null": lambda value: value is None,
}


def schema_keyword_problems(schema: Mapping[str, Any], where: str = "#") -> list[str]:
    """The places where ``schema`` uses a keyword that ``schema_violations`` cannot check."""
    problems = [
        f"{where}: unsupported keyword {key!r}"
        for key in schema
        if key not in _VALIDATING_KEYWORDS | _ANNOTATION_KEYWORDS
    ]
    subschemas: list[tuple[str, object]] = []
    for key in ("properties", "$defs"):
        named = schema.get(key)
        if isinstance(named, Mapping):
            subschemas.extend((f"{where}/{key}/{name}", item) for name, item in named.items())
    subschemas.extend(
        (f"{where}/{key}", schema.get(key)) for key in ("items", "additionalProperties")
    )
    for place, item in subschemas:
        if isinstance(item, Mapping):
            problems.extend(schema_keyword_problems(item, place))
    return problems


def schema_violations(
    instance: object, schema: Mapping[str, Any], root: Mapping[str, Any] | None = None
) -> list[str]:
    """How ``instance`` breaks ``schema``, as readable lines; empty when it conforms.

    A small validator for the subset of JSON Schema that the published report schema uses:
    ``type`` (one name or a list), ``required``, ``properties``, ``additionalProperties`` (a
    boolean or a schema), ``enum``, ``items``, ``const`` and local ``$ref`` pointers such as
    ``#/$defs/finding``. Annotation keywords are ignored. A schema that uses any other keyword
    raises ``ValueError``, so a schema that outgrows this validator fails loudly instead of
    being checked only in part.
    """
    root = schema if root is None else root
    if problems := schema_keyword_problems(schema):
        raise ValueError(
            "The schema uses keywords this validator does not check: " + "; ".join(problems)
        )
    return _violations(instance, schema, root, "$")


def _referenced(schema: Mapping[str, Any], root: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The schema a local ``$ref`` points at, or ``None`` when the pointer does not resolve."""
    target: object = root
    for step in str(schema["$ref"]).removeprefix("#/").split("/"):
        target = target[step] if isinstance(target, Mapping) else {}
    return target if isinstance(target, Mapping) else None


def _type_violations(instance: object, schema: Mapping[str, Any], where: str) -> list[str]:
    if "type" not in schema:
        return []
    names = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
    if any(_SCHEMA_TYPES[str(name)](instance) for name in names):
        return []
    return [f"{where}: expected {' or '.join(map(str, names))}, got {instance!r}"]


def _value_violations(instance: object, schema: Mapping[str, Any], where: str) -> list[str]:
    """The ``const`` and ``enum`` keywords."""
    problems = []
    if "const" in schema and instance != schema["const"]:
        problems.append(f"{where}: expected {schema['const']!r}, got {instance!r}")
    if "enum" in schema and instance not in schema["enum"]:
        problems.append(f"{where}: {instance!r} is not one of {schema['enum']!r}")
    return problems


def _violations(
    instance: object, schema: Mapping[str, Any], root: Mapping[str, Any], where: str
) -> list[str]:
    if "$ref" in schema:
        target = _referenced(schema, root)
        if target is None:
            return [f"{where}: unresolved reference {schema['$ref']!r}"]
        return _violations(instance, target, root, where)
    # A value of the wrong type is reported alone: its members would only add noise.
    if wrong_type := _type_violations(instance, schema, where):
        return wrong_type
    problems = _value_violations(instance, schema, where)
    if isinstance(instance, dict):
        problems.extend(_object_violations(instance, schema, root, where))
    if isinstance(instance, list) and isinstance(schema.get("items"), Mapping):
        for index, item in enumerate(instance):
            problems.extend(_violations(item, schema["items"], root, f"{where}[{index}]"))
    return problems


def _object_violations(
    instance: dict[str, object],
    schema: Mapping[str, Any],
    root: Mapping[str, Any],
    where: str,
) -> list[str]:
    problems = [
        f"{where}: missing required property {name!r}"
        for name in schema.get("required", ())
        if name not in instance
    ]
    declared = schema.get("properties", {})
    extra = schema.get("additionalProperties", True)
    for name, item in instance.items():
        if name in declared:
            problems.extend(_violations(item, declared[name], root, f"{where}.{name}"))
        elif extra is False:
            problems.append(f"{where}: unexpected property {name!r}")
        elif isinstance(extra, Mapping):
            problems.extend(_violations(item, extra, root, f"{where}.{name}"))
    return problems
