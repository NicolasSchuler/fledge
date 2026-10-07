"""Configurable checks of literal declarations in one selected source graph.

This module does not execute TeX or inventory packages loaded by the toolchain.
Counts use an explicitly documented lexical convention, not rendered PDF words.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .models import Finding, PreparationError
from .scheduler import cancellation_point
from .source import (
    _FILE_COMMANDS,
    _IMPORT_COMMANDS,
    _LIST_COMMANDS,
    _SECOND_FILE_ARGUMENT,
    _Command,
    _commands,
    _Inspection,
    _items,
    _ParseLimit,
    _Source,
    mask_literals,
)


@dataclass(frozen=True)
class ManuscriptOptions:
    expected_document_class: str | None = None
    required_class_options: tuple[str, ...] = ()
    forbidden_class_options: tuple[str, ...] = ()
    forbidden_packages: tuple[str, ...] = ()
    require_abstract: bool = False
    abstract_min_words: int | None = None
    abstract_max_words: int | None = None
    keywords_min_count: int | None = None
    keywords_max_count: int | None = None
    required_sections: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.require_abstract) is not bool:
            raise PreparationError("require_abstract must be a boolean")
        if self.expected_document_class is not None and (
            not isinstance(self.expected_document_class, str)
            or _literal_list(self.expected_document_class) != [self.expected_document_class]
        ):
            raise PreparationError("Expected document class must be a nonempty string")
        for names in (
            self.required_class_options,
            self.forbidden_class_options,
            self.forbidden_packages,
            self.required_sections,
        ):
            if (
                not isinstance(names, tuple)
                or any(
                    not isinstance(name, str) or not name.strip() or name != name.strip()
                    for name in names
                )
                or len(set(names)) != len(names)
            ):
                raise PreparationError(
                    "Manuscript name lists must be tuples of unique nonempty strings"
                )
        for name in (
            *self.required_class_options,
            *self.forbidden_class_options,
            *self.forbidden_packages,
        ):
            if _literal_list(name) != [name]:
                raise PreparationError(
                    "Class options and package names must be single literal names"
                )
        for lower, upper in (
            (self.abstract_min_words, self.abstract_max_words),
            (self.keywords_min_count, self.keywords_max_count),
        ):
            if any(
                value is not None and (type(value) is not int or value < 0)
                for value in (lower, upper)
            ):
                raise PreparationError("Manuscript count limits must be nonnegative integers")
            if lower is not None and upper is not None and lower > upper:
                raise PreparationError("Manuscript minimum count must not exceed its maximum")
        if set(self.required_class_options) & set(self.forbidden_class_options):
            raise PreparationError("A class option cannot be both required and forbidden")


_TEXT_WRAPPERS = {
    "emph",
    "textbf",
    "textit",
    "textmd",
    "textnormal",
    "textrm",
    "textsc",
    "textsf",
    "textsl",
    "texttt",
    "textup",
    "mbox",
    "underline",
    "mathrm",
    "mathbf",
}
_OMIT_ARGUMENTS = {
    "label",
    "cite",
    "citep",
    "citet",
    "citeauthor",
    "citeyear",
    "parencite",
    "textcite",
    "ref",
    "pageref",
    "eqref",
    "autoref",
    "cref",
    "Cref",
    "nocite",
}
_HEADING_COMMANDS = {
    "part",
    "chapter",
    "section",
    "subsection",
    "subsubsection",
    "paragraph",
    "subparagraph",
}
_KEYWORD_ENVS = {"keywords", "keyword", "IEEEkeywords"}
_PACKAGE_COMMANDS = {"usepackage", "RequirePackage", "RequirePackageWithOptions"}
_MATH_ENVS = "equation|equation\\*|align|align\\*|displaymath|math"
_COUNT_METHOD = (
    "Unicode alphanumeric tokens; internal hyphens and apostrophes stay in one word. "
    "Comments, common literal/code regions, math, and citation/reference/label arguments "
    "count as zero. Common text-formatting wrappers retain their text. Unresolved macros "
    "make the count inconclusive. This is a source count, not a rendered word count."
)


def _plain_text(value: str) -> tuple[str, list[str]]:
    """Return countable literal text and unresolved constructs, without expanding TeX."""
    value = mask_literals(value)
    uncertain: set[str] = set()
    value = re.sub(rf"\\begin\{{({_MATH_ENVS})\}}.*?\\end\{{\1\}}", " ", value, flags=re.S)
    for pattern in (
        r"\$\$.*?\$\$",
        r"(?<!\\)\$(?:\\.|[^$])*?(?<!\\)\$",
        r"\\\(.*?\\\)",
        r"\\\[.*?\\\]",
    ):
        value = re.sub(pattern, " ", value, flags=re.S)
    if re.search(r"(?<!\\)\$|\\[()[\]]", value):
        uncertain.add("unbalanced math delimiters")
    try:
        commands = _commands(value)
    except _ParseLimit:
        return "", ["unclosed or oversized argument"]
    edits: list[tuple[int, int, str]] = []
    covered_until = -1
    for command in commands:
        if command.start < covered_until:
            continue
        token_end = command.start + len(command.name) + 1
        if command.name in _OMIT_ARGUMENTS:
            stop = max(
                (arg.end + 1 for arg in (*command.arguments[:1], *command.options)),
                default=token_end,
            )
            edits.append((command.start, stop, " "))
            covered_until = stop
        elif command.name in _TEXT_WRAPPERS:
            edits.append((command.start, token_end, ""))
        elif command.name in {"TeX", "LaTeX"}:
            edits.append((command.start, token_end, command.name))
        elif command.name in {
            "par",
            "newline",
            "linebreak",
            "and",
            "sep",
            "\\",
            ",",
            ";",
            "!",
            " ",
        }:
            edits.append((command.start, token_end, " "))
        elif command.name in {"%", "&", "#", "_", "{", "}"}:
            edits.append((command.start, token_end, command.name))
        else:
            uncertain.add("\\" + command.name)
            edits.append((command.start, token_end, " "))
    for start, stop, replacement in reversed(edits):
        value = value[:start] + replacement + value[stop:]
    return value.replace("{", "").replace("}", "").replace("~", " "), sorted(uncertain)


def _normalize_heading(value: str) -> str:
    return " ".join(value.split()).casefold()


def _literal_list(value: str) -> list[str] | None:
    if re.search(r"[\\{}#$^~&%\x00]", value):
        return None
    items = [item.strip() for item in value.split(",")]
    return items if all(items) else None


def _commands_before_end(source: _Source) -> list[_Command]:
    result: list[_Command] = []
    for command in source.commands:
        if (
            command.name == "end"
            and command.depth == 0
            and command.arguments
            and command.arguments[0].value.strip() == "document"
        ):
            break
        result.append(command)
        if command.name == "endinput" and command.depth == 0:
            # TeX may still consume the current physical line. Keep the stop
            # token for uncertainty reporting, but never treat its suffix as
            # definitive evidence without interpreting TeX's token buffering.
            break
    return result


def _reachable_sources(inspection: _Inspection, kinds: set[str] | None = None) -> set[str]:
    reached = {inspection.main} if inspection.main else set()
    active_keys = {
        (name, item.start, item.end)
        for name in inspection.visited
        for command in _commands_before_end(inspection.sources[name])
        if command.name in _FILE_COMMANDS and command.arguments
        for argument in command.arguments[
            (1 if command.name in _SECOND_FILE_ARGUMENT else 0) : (
                2 if command.name in _SECOND_FILE_ARGUMENT else 1
            )
        ]
        for item in _items(argument, command.name in _LIST_COMMANDS)
    }
    active = {
        (name, start, end): target
        for (name, start, end), (target, kind) in inspection.references.items()
        if (kinds is None or kind in kinds) and (name, start, end) in active_keys
    }
    while True:
        additions = {target for (name, _, _), target in active.items() if name in reached}
        if additions <= reached:
            return reached & inspection.visited
        reached.update(additions)


@dataclass(frozen=True)
class _Block:
    path: str
    line: int
    value: str
    uncertain: tuple[str, ...] = ()


def _blocks(name: str, source: _Source, kind: str, conditional: bool) -> list[_Block]:
    names = {"abstract"} if kind == "abstract" else _KEYWORD_ENVS
    result: list[_Block] = []
    pending: dict[str, list[_Command]] = {}
    nested: set[int] = set()
    active_count = 0

    def reasons(command: _Command) -> tuple[str, ...]:
        return tuple(
            message
            for enabled, message in (
                (conditional, "conditional source control flow"),
                (bool(command.depth), "declaration inside a group or macro argument"),
                (command.start in nested, "nested manuscript environments"),
            )
            if enabled
        )

    for command in _commands_before_end(source):
        if command.name == ("abstract" if kind == "abstract" else "keywords"):
            value = command.arguments[0].value if command.arguments else ""
            uncertainty = reasons(command)
            if not command.arguments:
                uncertainty += ("missing literal argument",)
            result.append(_Block(name, command.line, value, uncertainty))
        elif command.name in {"begin", "end"} and command.arguments:
            env = command.arguments[0].value.strip()
            if env not in names:
                continue
            if command.name == "begin":
                if active_count:
                    if active_count == 1:
                        nested.add(next(stack[0].start for stack in pending.values() if stack))
                    nested.add(command.start)
                pending.setdefault(env, []).append(command)
                active_count += 1
            elif pending.get(env):
                opening = pending[env].pop()
                active_count -= 1
                # Do not copy overlapping ambiguous bodies: even maliciously
                # nested input keeps memory proportional to the bounded source.
                value = (
                    ""
                    if opening.start in nested
                    else source.masked[opening.arguments[0].end + 1 : command.start]
                )
                result.append(_Block(name, opening.line, value, reasons(opening)))
    for stack in pending.values():
        for command in stack:
            result.append(
                _Block(
                    name,
                    command.line,
                    "",
                    (*reasons(command), "environment has no local closing delimiter"),
                )
            )
    return sorted(result, key=lambda item: item.line)


def _result(
    rule: str,
    message: str,
    status: str,
    details: dict[str, object],
    path: str | None = None,
    line: int | None = None,
) -> Finding:
    if status == "inconclusive":
        reported = details.get("uncertainty", [])
        uncertainty = list(reported) if isinstance(reported, list) else []
        for collection in ("declarations", "abstracts", "keywords", "headings"):
            entries = details.get(collection, [])
            if isinstance(entries, list):
                for item in entries:
                    uncertainty.extend(item.get("uncertainty", []))
                    uncertainty.extend(item.get("content_uncertainty", []))
        message += (
            " Inconclusive: "
            + (str(uncertainty[0]) if uncertainty else "no unique supported literal interpretation")
            + "."
        )
    collection = {
        "manuscript.document_class": "declarations",
        "manuscript.class_options": "declarations",
        "manuscript.forbidden_packages": "matches",
        "manuscript.abstract_required": "abstracts",
        "manuscript.abstract_words": "abstracts",
        "manuscript.keyword_count": "keywords",
    }.get(rule)
    locations = details.get(collection, []) if collection is not None else []
    if isinstance(locations, list) and len(locations) == 1:
        path, line = locations[0]["path"], locations[0]["line"]
    return Finding(
        rule,
        message,
        "info" if status == "passed" else "error",
        status,
        path=path,
        line=line,
        evidence="derived",
        details=details,
        # Confirmed violations use the registry's code-specific next step.
        suggestion=(
            "Resolve the ambiguous source construct described above, including input "
            "boundaries or macro-generated declarations, and repeat this check."
            if status == "inconclusive"
            else None
        ),
    )


def check_manuscript(root: Path, main: str, options: ManuscriptOptions) -> list[Finding]:
    """Check explicit constraints; only the selected graph supplies manuscript evidence."""
    if options == ManuscriptOptions():
        return []
    inspection = _Inspection(root, main)
    selected = _reachable_sources(inspection)
    # Content follows input/include/import edges. Definitions inside class/style files are
    # package evidence, not manuscript abstracts, keywords, or section headings.
    content_commands = {"input", "include", *_IMPORT_COMMANDS}
    content = _reachable_sources(inspection, content_commands)
    graph_reasons: list[str] = []
    if inspection.main not in selected:
        graph_reasons.append("selected main source could not be inspected")
    if inspection.subfile_targets:
        graph_reasons.append(
            "subfile body/preamble content interpretation is not supported by this check"
        )
    occurrences = Counter(
        target
        for (name, _, _), (target, kind) in inspection.references.items()
        if name in content and kind in content_commands
    )
    for name, count in occurrences.items():
        if count > 1:
            graph_reasons.append(f"{name}: repeated source inclusion is not expanded for counts")
    incomplete_rules = {
        "source-encoding",
        "source-size-limit",
        "source-argument-limit",
        "source-command-limit",
        "source-literal-unclosed",
        "source-input-cycle",
        "source-input-depth",
        "source-expansion-limit",
        "source-dynamic-path",
        "source-nested-file-command",
        "source-conditional-dependency",
        "source-path-literal-context",
        "source-external-path",
        "source-ambiguous-dependency",
        "source-case-mismatch",
        "source-custom-path",
        "source-unsupported-command",
        "source-missing-dependency",
        "source-subfile-structure",
        "source-font-options",
    }
    scope = (
        selected
        | {inspection.main}
        | {
            target
            for (name, _, _), (target, _) in inspection.references.items()
            if name in selected
        }
    )
    for item in inspection.findings:
        if item.path in scope and item.rule in incomplete_rules:
            graph_reasons.append(f"{item.path}: {item.message}")
    classes: list[dict[str, object]] = []
    packages: list[dict[str, object]] = []
    abstracts: list[_Block] = []
    keywords: list[_Block] = []
    headings: list[dict[str, object]] = []
    for name in sorted(selected):
        cancellation_point()
        source = inspection.sources[name]
        commands = _commands_before_end(source)
        conditional = any(command.name.startswith(("if", "If")) for command in commands)
        for command in commands:
            if command.name == "endinput":
                if command.depth or conditional:
                    graph_reasons.append(
                        f"{name}:{command.line}: grouped, macro-defined, or conditional "
                        "\\endinput has an unresolved execution context"
                    )
                else:
                    suffix = source.masked[command.start + len("\\endinput") :].split("\n", 1)[0]
                    if suffix.strip():
                        graph_reasons.append(
                            f"{name}:{command.line}: tokens after \\endinput on the same "
                            "physical line have unresolved execution semantics"
                        )
            if command.name == "documentclass" or command.name in _PACKAGE_COMMANDS:
                names = _literal_list(command.arguments[0].value) if command.arguments else None
                class_options = (
                    _literal_list(command.options[0].value)
                    if command.options and command.options[0].value.strip()
                    else []
                )
                uncertainty = []
                if command.depth or conditional:
                    uncertainty.append("macro, grouped, or conditional declaration")
                if names is None or (command.name == "documentclass" and class_options is None):
                    uncertainty.append("nonliteral declaration or options")
                target = classes if command.name == "documentclass" else packages
                target.append(
                    {
                        "path": name,
                        "line": command.line,
                        "names": names,
                        "options": class_options,
                        "uncertainty": uncertainty,
                    }
                )
            if name in content and command.name == "PassOptionsToClass":
                graph_reasons.append(
                    f"{name}: class options are also passed outside the documentclass declaration"
                )
            if name in content and command.name in {"csname", "catcode", "scantokens"}:
                graph_reasons.append(
                    f"{name}: \\{command.name} can generate or change source syntax"
                )
            if name in content and command.name in _HEADING_COMMANDS:
                title, unknown = (
                    _plain_text(command.arguments[0].value)
                    if command.arguments
                    else ("", ["missing heading argument"])
                )
                if command.depth or conditional:
                    unknown.append("macro, grouped, or conditional heading")
                headings.append(
                    {
                        "path": name,
                        "line": command.line,
                        "heading": " ".join(title.split()),
                        "command": command.name,
                        "uncertainty": unknown,
                    }
                )
        if name in content:
            abstracts.extend(_blocks(name, source, "abstract", conditional))
            keywords.extend(_blocks(name, source, "keywords", conditional))
    graph_reasons = sorted(set(graph_reasons))
    abstract_counts = []
    for block in abstracts:
        text, unknown = _plain_text(block.value)
        count = len(re.findall(r"[^\W_]+(?:['’\-‑][^\W_]+)*", text))
        nonempty_text = bool(text.strip())
        content_uncertainty = []
        if not nonempty_text and re.search(
            rf"(?<!\\)\$|\\[([]|\\begin\{{(?:{_MATH_ENVS})\}}",
            block.value,
        ):
            content_uncertainty.append(
                "abstract has no literal text outside mathematics; nonempty rendered "
                "content is not established by the lexical count"
            )
        if not nonempty_text and any(
            match[1] in _OMIT_ARGUMENTS - {"label", "nocite"}
            for match in re.finditer(r"\\([A-Za-z@]+|[^\r\n])", block.value)
        ):
            content_uncertainty.append(
                "abstract has no literal text outside citation/reference commands"
            )
        abstract_counts.append(
            {
                "path": block.path,
                "line": block.line,
                "words": count,
                "nonempty_literal_text": nonempty_text,
                "content_uncertainty": content_uncertainty,
                "uncertainty": [*block.uncertain, *unknown],
            }
        )
    keyword_counts = []
    for block in keywords:
        # Semicolons, commas, and common explicit separator macros separate phrases.
        parts = re.split(r"[,;]|\\(?:sep|and)\b", block.value)
        count = 0
        uncertainty = list(block.uncertain)
        for part in parts:
            text, unknown = _plain_text(part)
            count += bool(text.strip())
            uncertainty.extend(unknown)
        keyword_counts.append(
            {
                "path": block.path,
                "line": block.line,
                "count": count,
                "uncertainty": sorted(set(uncertainty)),
            }
        )
    findings = [
        Finding(
            "manuscript.inventory",
            "Collected literal manuscript structures and direct package declarations "
            "in the selected source graph.",
            "info",
            "passed",
            evidence="derived",
            details={
                "sources": sorted(selected),
                "content_sources": sorted(content),
                "document_classes": classes,
                "package_declarations": packages,
                "abstracts": abstract_counts,
                "keywords": keyword_counts,
                "headings": headings,
                "uncertainty": graph_reasons,
                "word_count_method": _COUNT_METHOD,
                "scope": "Literal source declarations only; no TeX execution "
                "or toolchain package inventory.",
            },
        )
    ]
    class_uncertain = bool(
        graph_reasons or len(classes) != 1 or any(item["uncertainty"] for item in classes)
    )
    actual_class = classes[0]["names"] if len(classes) == 1 else None
    if options.expected_document_class is not None:
        status = (
            "inconclusive"
            if class_uncertain or actual_class is None
            else "passed"
            if actual_class == [options.expected_document_class]
            else "failed"
        )
        findings.append(
            _result(
                "manuscript.document_class",
                f"Direct document class: {actual_class or 'unavailable'}; "
                f"expected: {options.expected_document_class}.",
                status,
                {
                    "expected": options.expected_document_class,
                    "declarations": classes,
                    "uncertainty": graph_reasons,
                },
                main,
            )
        )
    if options.required_class_options or options.forbidden_class_options:
        actual_options = classes[0]["options"] if len(classes) == 1 else None
        actual = set(actual_options) if isinstance(actual_options, list) else set()
        missing = sorted(set(options.required_class_options) - actual)
        forbidden = sorted(set(options.forbidden_class_options) & actual)
        status = (
            "inconclusive" if class_uncertain else "failed" if missing or forbidden else "passed"
        )
        findings.append(
            _result(
                "manuscript.class_options",
                f"Missing required class options: {missing or 'none'}; "
                f"forbidden options present: {forbidden or 'none'}.",
                status,
                {
                    "required": list(options.required_class_options),
                    "forbidden": list(options.forbidden_class_options),
                    "missing": missing,
                    "present_forbidden": forbidden,
                    "declarations": classes,
                    "uncertainty": graph_reasons,
                },
                main,
            )
        )
    if options.forbidden_packages:
        matches = [
            item
            for item in packages
            if isinstance(item["names"], list)
            and set(item["names"]) & set(options.forbidden_packages)
            and not item["uncertainty"]
        ]
        uncertain = bool(graph_reasons or any(item["uncertainty"] for item in packages))
        status = "failed" if matches else "inconclusive" if uncertain else "passed"
        findings.append(
            _result(
                "manuscript.forbidden_packages",
                f"Forbidden package declarations: {len(matches)}; "
                f"configured forbidden names: {', '.join(options.forbidden_packages)}.",
                status,
                {
                    "forbidden": list(options.forbidden_packages),
                    "matches": matches,
                    "declarations": packages,
                    "uncertainty": graph_reasons,
                    "scope": "Does not establish the transitive packages loaded by TeX.",
                },
                main,
            )
        )
    abstract_uncertain = bool(
        graph_reasons or len(abstracts) > 1 or any(item["uncertainty"] for item in abstract_counts)
    )
    if options.require_abstract:
        status = (
            "inconclusive"
            if abstract_uncertain or any(item["content_uncertainty"] for item in abstract_counts)
            else "passed"
            if len(abstract_counts) == 1 and abstract_counts[0]["nonempty_literal_text"]
            else "failed"
        )
        findings.append(
            _result(
                "manuscript.abstract_required",
                f"Found {len(abstracts)} supported literal abstract declarations; "
                "one nonempty abstract is required.",
                status,
                {"abstracts": abstract_counts, "uncertainty": graph_reasons},
                main,
            )
        )
    if options.abstract_min_words is not None or options.abstract_max_words is not None:
        count = abstract_counts[0]["words"] if len(abstract_counts) == 1 else None
        status = (
            "inconclusive"
            if abstract_uncertain or any(item["uncertainty"] for item in abstract_counts)
            else "failed"
        )
        if status != "inconclusive" and isinstance(count, int):
            status = (
                "failed"
                if (options.abstract_min_words is not None and count < options.abstract_min_words)
                or (options.abstract_max_words is not None and count > options.abstract_max_words)
                else "passed"
            )
        findings.append(
            _result(
                "manuscript.abstract_words",
                f"Abstract source word count: {count if count is not None else 'unavailable'}; "
                f"configured minimum/maximum: {options.abstract_min_words}/"
                f"{options.abstract_max_words}.",
                status,
                {
                    "minimum": options.abstract_min_words,
                    "maximum": options.abstract_max_words,
                    "abstracts": abstract_counts,
                    "counting_method": _COUNT_METHOD,
                    "uncertainty": graph_reasons,
                },
                main,
            )
        )
    if options.keywords_min_count is not None or options.keywords_max_count is not None:
        count = keyword_counts[0]["count"] if len(keyword_counts) == 1 else 0
        assert isinstance(count, int)
        uncertain = bool(
            graph_reasons
            or len(keyword_counts) > 1
            or any(item["uncertainty"] for item in keyword_counts)
        )
        status = (
            "inconclusive"
            if uncertain
            else "failed"
            if (options.keywords_min_count is not None and count < options.keywords_min_count)
            or (options.keywords_max_count is not None and count > options.keywords_max_count)
            else "passed"
        )
        findings.append(
            _result(
                "manuscript.keyword_count",
                f"Literal keyword count: {count}; configured minimum/maximum: "
                f"{options.keywords_min_count}/{options.keywords_max_count}.",
                status,
                {
                    "minimum": options.keywords_min_count,
                    "maximum": options.keywords_max_count,
                    "count": count,
                    "keywords": keyword_counts,
                    "counting_method": "Nonempty phrases separated by commas, semicolons, "
                    "\\sep, or \\and in \\keywords{} "
                    "or keywords/keyword/IEEEkeywords environments.",
                    "uncertainty": graph_reasons,
                },
                main,
            )
        )
    if options.required_sections:
        present = {
            _normalize_heading(str(item["heading"])) for item in headings if not item["uncertainty"]
        }
        missing = [
            heading
            for heading in options.required_sections
            if _normalize_heading(heading) not in present
        ]
        uncertain = bool(graph_reasons or any(item["uncertainty"] for item in headings))
        status = "inconclusive" if uncertain else "failed" if missing else "passed"
        findings.append(
            _result(
                "manuscript.required_sections",
                f"Missing required literal headings: {missing or 'none'}; "
                "content adequacy is not assessed.",
                status,
                {
                    "required": list(options.required_sections),
                    "missing": missing,
                    "headings": headings,
                    "matching": "Case-insensitive, collapsed whitespace, "
                    "common literal formatting wrappers removed.",
                    "uncertainty": graph_reasons,
                },
                main,
            )
        )
    return findings
