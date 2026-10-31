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
        """Return ``target`` unchanged, since there is no pytest marker to apply."""
        return target


_T = TypeVar("_T")

MINIMAL_PDF = b"%PDF-controlled-test\n"
MINIMAL_DOCUMENT = "\\documentclass{article}\n\\begin{document}Hello.\\end{document}\n"


def write_file(root: Path, name: str, content: str | bytes = "") -> Path:
    """Write ``content`` (UTF-8 for text) to ``root / name``, creating parent directories.

    Args:
        root: The directory the name is relative to.
        name: The relative path of the file.
        content: The text or bytes of the file.

    Returns:
        The path of the written file.
    """
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

    Attributes:
        root: The resolved temporary directory of the current test.
    """

    root: Path

    def setUp(self) -> None:
        """Create the temporary ``root`` and register its removal."""
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def write(self, name: str, content: str | bytes = "") -> Path:
        """Write a file below ``root``; the same helper every test module used to define.

        Args:
            name: The path of the file relative to ``root``.
            content: The text (written as UTF-8) or bytes of the file.

        Returns:
            The path of the written file.
        """
        return write_file(self.root, name, content)

    def write_project(self, files: Mapping[str, str | bytes], name: str = "project") -> Path:
        """Create the directory ``root / name`` holding ``files``.

        Args:
            files: The text or bytes of each file, by path relative to the project.
            name: The name of the project directory below ``root``.

        Returns:
            The project directory.
        """
        return write_project(self.root / name, files)


class ProjectTestCase(TemporaryRoot):
    """A test case with a temporary ``root`` and ``write`` / ``write_project`` helpers."""


class AsyncProjectTestCase(TemporaryRoot, unittest.IsolatedAsyncioTestCase):
    """The asynchronous counterpart of ``ProjectTestCase``."""


def write_project(root: Path, files: Mapping[str, str | bytes]) -> Path:
    """Create ``root`` and write each relative path with its text or bytes.

    Args:
        root: The project directory, created with its parents if needed.
        files: The text or bytes of each file, by path relative to ``root``.

    Returns:
        ``root``.

    Raises:
        ValueError: A path resolves to a place outside ``root``.
    """
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

    Args:
        work: The build directory that receives ``built.pdf``.
        main: The main file of the build.
        success: Whether the build succeeded.
        pdf_bytes: The contents of the PDF a successful build writes.
        findings: The findings of the build.
        dependencies: The project inputs the recorder reports, or ``None`` for ``main`` alone.
        submission_inputs: The submission inputs, or ``None`` for ``main`` alone.
        tools: The tools of the build, or ``None`` for an empty ``BuildTools()``.
        command: The command line of the build.
        log: The text of the build log.

    Returns:
        The build result.
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
    """Act as a controlled build backend that succeeds with ``main`` as the only dependency.

    It writes the PDF through :func:`fake_build_result` into ``work``; the other arguments
    are ignored.
    """
    return fake_build_result(work, main)


def _backend(behaviour: object) -> Callable[..., Awaitable[object]]:
    """Return an async backend that does what ``behaviour`` says.

    A callable is the backend itself (an async function or an ``AsyncMock``), an exception
    instance is raised on every call, and any other value is returned on every call; lists
    are copied so one job cannot change what the next receives.
    """
    if callable(behaviour):
        return behaviour

    async def backend(*_arguments: object, **_options: object) -> object:
        """Raise or return ``behaviour``, whatever the arguments."""
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
    """Build document backends that never start a tool, for ``run_job(..., backends=...)``.

    By default a build succeeds with ``main`` as its only dependency, and a PDF inspection
    and comparison find nothing. Each argument replaces one backend: pass an async callable
    (it receives the arguments the real function would), a ``BuildResult`` or a list of
    findings to return every time, or an exception to raise.

    Args:
        build: The behaviour of the build backend.
        inspect_pdf: The behaviour of the PDF inspection backend.
        compare_pdfs: The behaviour of the PDF comparison backend.

    Returns:
        The document backends.
    """
    return DocumentBackends(
        build=_backend(build),
        inspect_pdf=_backend(inspect_pdf),
        compare_pdfs=_backend(compare_pdfs),
    )


def sectioned_project(root: Path) -> None:
    """Write a project whose ``main.tex`` inputs two ``results.tex`` in different directories."""
    (root / "sections").mkdir(parents=True)
    (root / "appendix").mkdir()
    (root / "main.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\n"
        "\\input{sections/results}\n\\input{appendix/results}\n\\end{document}\n"
    )
    (root / "sections/results.tex").write_text("Main results.\n")
    (root / "appendix/results.tex").write_text("Additional results.\n")


def minimal_project(root: Path, main: str = "main.tex") -> None:
    """Create ``root``, with its parents if needed, holding a minimal article as ``main``."""
    root.mkdir(parents=True, exist_ok=True)
    (root / main).write_text("\\documentclass{article}\n\\begin{document}Hello.\\end{document}\n")


def single_file_project(root: Path, body: str = "Body.", preamble: str = "") -> None:
    """Create ``root`` holding one article, ``main.tex``.

    Args:
        root: The project directory; its parent must exist and it must not.
        body: The text of the document environment.
        preamble: Text inserted after the document class line.
    """
    root.mkdir()
    (root / "main.tex").write_text(
        "\\documentclass{article}\n"
        + preamble
        + "\n\\begin{document}\n"
        + body
        + "\n\\end{document}\n"
    )


async def build_with_loaded_class(tree, main, work, engine, runner, **_options):
    """Write ``work/paper.pdf`` and report a build that loaded ``article.cls`` from the toolchain.

    Orchestration double only; live TeX evidence belongs to opt-in integration tests. ``main``
    is the only project and submission input; ``tree``, ``engine`` and ``runner`` are ignored.
    """
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
    """Return the bytes of every file below ``root``, by POSIX path relative to it."""
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


class RecordedBuild:
    """A build backend that supplies fixed per-document traces.

    Fake PDFs validate orchestration only.

    Attributes:
        traces: The recorded project inputs of each successive build, by main file; ``None``
            gives no project inputs and ``None`` as the submission inputs.
        generated_reads: The generated files every build reports as read.
        fail_archive_main: The main file whose build in an ``archive-build`` directory fails,
            if any.
        counts: How many builds of each main file have run.
        visits: The main file, the work directory name and the project contents of each
            build.
    """

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
    """Format with the formatting settings of ``settings``, as the job core passes them.

    Args:
        source: The project whose TeX documents are formatted.
        work: The directory, which must not exist, that holds the formatter's workspaces.
        runner: The runner of the formatter.
        settings: The settings whose ``formatting_options`` apply, ``Settings()`` if omitted.
        **options: Further keyword arguments of ``format_project``.

    Returns:
        The result of ``format_project``.
    """
    settings = settings or Settings()
    return await format_project(source, work, runner, settings.formatting_options, **options)


def live_tests_enabled(*legacy: str) -> bool:
    """Return whether opt-in live tool tests run.

    They run when ``FLEDGE_RUN_INTEGRATION``, ``LATEX_PREP_RUN_INTEGRATION`` or one of the
    ``legacy`` variables is ``1``.
    """
    return any(
        os.environ.get(name) == "1"
        for name in ("FLEDGE_RUN_INTEGRATION", "LATEX_PREP_RUN_INTEGRATION", *legacy)
    )


def live_test(reason: str, *legacy: str, platform: str | None = None) -> Callable[[_T], _T]:
    """Mark a test, or a test class, as live: it drives real tools and is opt-in.

    It is skipped unless ``FLEDGE_RUN_INTEGRATION=1`` (or a ``legacy`` variable) is set, and
    on any platform other than ``platform`` when one is given. Under pytest the ``live``
    marker also selects it with ``-m live``.

    Args:
        reason: The skip message.
        *legacy: Further environment variables that enable live tests when set to ``1``.
        platform: The ``sys.platform`` the test requires, or ``None`` for any.

    Returns:
        The decorator.
    """
    enabled = live_tests_enabled(*legacy) and (platform is None or sys.platform == platform)

    def decorate(target: _T) -> _T:
        """Apply the skip condition and the ``live`` marker to ``target``."""
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

    Args:
        case: The test case that reports the failure.
        name: The path of the golden file relative to ``tests/golden``.
        actual: The output to compare.
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
    """Replace what varies between runs in a report with placeholders.

    Each text in ``replacements`` (a path, a version) is replaced inside every string, longest
    first. Numbers under a timing key (``elapsed_seconds``, ``queue_seconds``,
    ``duration_seconds``), and every number inside a section named in ``measured`` (resource
    accounting, which depends on timing and the host), become ``"<seconds>"`` and
    ``"<measured>"``. Keys, structure and all other values, including configured limits such as
    ``timeout_seconds``, are kept.

    Args:
        value: The report data, or a part of it.
        replacements: The placeholder of each text that varies.
        measured: The keys of the sections whose numbers are measured.
        in_measured: Whether ``value`` lies inside a measured section, as the recursion
            passes it.

    Returns:
        A normalised copy of ``value``.
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
    """List the places where ``schema`` uses a keyword that ``schema_violations`` cannot check.

    Args:
        schema: The schema to inspect, with its nested schemas.
        where: The JSON pointer of ``schema``, which prefixes each problem.

    Returns:
        One readable line per unsupported keyword.
    """
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
    """List how ``instance`` breaks ``schema``.

    A small validator for the subset of JSON Schema that the published report schema uses:
    ``type`` (one name or a list), ``required``, ``properties``, ``additionalProperties`` (a
    boolean or a schema), ``enum``, ``items``, ``const`` and local ``$ref`` pointers such as
    ``#/$defs/finding``. Annotation keywords are ignored.

    Args:
        instance: The value to validate.
        schema: The schema the value must conform to.
        root: The document that local ``$ref`` pointers resolve in, ``schema`` if omitted.

    Returns:
        The violations as readable lines; empty when ``instance`` conforms.

    Raises:
        ValueError: The schema uses any other keyword, so a schema that outgrows this
            validator fails loudly instead of being checked only in part.
    """
    root = schema if root is None else root
    if problems := schema_keyword_problems(schema):
        raise ValueError(
            "The schema uses keywords this validator does not check: " + "; ".join(problems)
        )
    return _violations(instance, schema, root, "$")


def _referenced(schema: Mapping[str, Any], root: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Return the schema a local ``$ref`` points at, or ``None`` when it does not resolve."""
    target: object = root
    for step in str(schema["$ref"]).removeprefix("#/").split("/"):
        target = target[step] if isinstance(target, Mapping) else {}
    return target if isinstance(target, Mapping) else None


def _type_violations(instance: object, schema: Mapping[str, Any], where: str) -> list[str]:
    """Check the ``type`` keyword, reporting at ``where`` a value of none of its types."""
    if "type" not in schema:
        return []
    names = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
    if any(_SCHEMA_TYPES[str(name)](instance) for name in names):
        return []
    return [f"{where}: expected {' or '.join(map(str, names))}, got {instance!r}"]


def _value_violations(instance: object, schema: Mapping[str, Any], where: str) -> list[str]:
    """Check the ``const`` and ``enum`` keywords, reporting at ``where``."""
    problems = []
    if "const" in schema and instance != schema["const"]:
        problems.append(f"{where}: expected {schema['const']!r}, got {instance!r}")
    if "enum" in schema and instance not in schema["enum"]:
        problems.append(f"{where}: {instance!r} is not one of {schema['enum']!r}")
    return problems


def _violations(
    instance: object, schema: Mapping[str, Any], root: Mapping[str, Any], where: str
) -> list[str]:
    """List how ``instance`` at ``where`` breaks ``schema``, following local references.

    Args:
        instance: The value to validate.
        schema: The schema the value must conform to.
        root: The document that local ``$ref`` pointers resolve in.
        where: The path of ``instance`` in the validated document, such as ``$.items[0]``.

    Returns:
        The violations as readable lines.
    """
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
    """Check the ``required``, ``properties`` and ``additionalProperties`` keywords.

    Args:
        instance: The object to validate.
        schema: The schema the object must conform to.
        root: The document that local ``$ref`` pointers resolve in.
        where: The path of ``instance`` in the validated document.

    Returns:
        The violations as readable lines.
    """
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
