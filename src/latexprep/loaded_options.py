"""Bounded post-document LaTeX kernel option evidence from disposable builds.

The hook reads kernel registers, not literal source declarations.  These lists
do not describe package defaults, every consumed global option, or later setup
commands.  Recorder agreement is required before reporting a complete inventory.
"""

from __future__ import annotations

import os
import re
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

from .models import Finding, PreparationError

_MAX_PACKAGES = 2048
_MAX_FIELD_UNITS = 16_384
_MAX_EVIDENCE_BYTES = 2_097_152
_MAX_MAIN_BYTES = 8_388_608
_PACKAGE = re.compile(r"[A-Za-z0-9_.-]+\.(?:cls|sty)")

# Application-owned TeX only.  The substitutions below are generated lowercase
# hexadecimal, never source text or paths.  Detokenization expands each register
# once, not the commands held in its option values; transport is native character
# units encoded as UTF-32BE hex so braces, pipes and newlines cannot forge rows.
# The kernel clears @filelist at begin-document unless listfiles was requested.
# File hooks retain our own bounded names without changing that user setting;
# the values themselves are still read only after document execution.
_HOOK = r"""\ifdefined\ExplSyntaxOn\else\expandafter\endinput\fi
\ifdefined\AddToHook\else\expandafter\endinput\fi
\ifdefined\CurrentFileUsed\else\expandafter\endinput\fi
\ExplSyntaxOn
\iow_new:N \g__latexprep_options_stream
\str_new:N \l__latexprep_options_value_str
\str_new:N \l__latexprep_options_encoded_str
\seq_new:N \g__latexprep_options_files_seq
\bool_new:N \g__latexprep_options_unsupported_bool
\int_new:N \l__latexprep_options_units_int
\iow_open:Nn \g__latexprep_options_stream { latexprep-loaded-options-TOKEN.lpo }
\iow_now:Nn \g__latexprep_options_stream { START|TOKEN }
\sys_if_engine_pdftex:TF
  { \iow_now:Nn \g__latexprep_options_stream { ENGINE|pdflatex } }
  {
    \sys_if_engine_xetex:TF
      { \iow_now:Nn \g__latexprep_options_stream { ENGINE|xelatex } }
      {
        \sys_if_engine_luatex:TF
          { \iow_now:Nn \g__latexprep_options_stream { ENGINE|lualatex } }
          { \iow_now:Nn \g__latexprep_options_stream { ENGINE|unsupported } }
      }
  }
\cs_new_protected:Npn \__latexprep_options_file:
  {
    \group_begin:
    \str_set:Nx \l__latexprep_options_value_str { \tl_to_str:V \CurrentFileUsed }
    \exp_args:NnV \regex_match:nnT { \.(cls|sty)\Z } \l__latexprep_options_value_str
      {
        \exp_args:NnV \regex_match:nnTF { \A [A-Za-z0-9_.-]+ \.(cls|sty)\Z }
          \l__latexprep_options_value_str
          {
            \seq_if_in:NVF \g__latexprep_options_files_seq \l__latexprep_options_value_str
              {
                \int_compare:nNnTF { \seq_count:N \g__latexprep_options_files_seq } < { 2048 }
                  { \seq_gput_right:NV \g__latexprep_options_files_seq
                      \l__latexprep_options_value_str }
                  { \bool_gset_true:N \g__latexprep_options_unsupported_bool }
              }
          }
          { \bool_gset_true:N \g__latexprep_options_unsupported_bool }
      }
    \group_end:
  }
\cs_new_protected:Npn \__latexprep_options_field:nn #1#2
  {
    \cs_if_exist:cTF {#2}
      {
        \exp_args:Nc \token_if_macro:NTF {#2}
          {
            \tl_if_empty:eTF { \exp_args:Nc \cs_parameter_spec:N {#2} }
              {
                \str_set:Nx \l__latexprep_options_value_str { \tl_to_str:v {#2} }
                \int_add:Nn \l__latexprep_options_units_int
                  { \str_count:N \l__latexprep_options_value_str }
                \bool_lazy_or:nnTF
                  { \int_compare_p:nNn
                      { \str_count:N \l__latexprep_options_value_str } > { 16384 } }
                  { \int_compare_p:nNn { \l__latexprep_options_units_int } > { 200000 } }
                  { \iow_now:Nn \g__latexprep_options_stream { #1|! } }
                  {
                    \exp_args:NNV \str_set_convert:Nnnn
                      \l__latexprep_options_encoded_str
                      \l__latexprep_options_value_str { } { utf32be/hex }
                    \iow_now:Nx \g__latexprep_options_stream
                      { #1|\l__latexprep_options_encoded_str }
                  }
              }
              { \iow_now:Nn \g__latexprep_options_stream { #1|! } }
          }
          { \iow_now:Nn \g__latexprep_options_stream { #1|! } }
      }
      { \iow_now:Nn \g__latexprep_options_stream { #1|! } }
  }
\cs_new_protected:Npn \__latexprep_options_finish:
  {
    \group_begin:
    \int_zero:N \l__latexprep_options_units_int
    \__latexprep_options_field:nn { FORMAT } { fmtversion }
    \__latexprep_options_field:nn { GLOBAL } { @classoptionslist }
    \__latexprep_options_field:nn { UNUSED } { @unusedoptionlist }
    \bool_if:NT \g__latexprep_options_unsupported_bool
      {
        \iow_now:Nn \g__latexprep_options_stream
          { UNSUPPORTED|package-filename-or-count }
      }
    \seq_map_inline:Nn \g__latexprep_options_files_seq
      {
        \iow_now:Nn \g__latexprep_options_stream { PACKAGE|##1 }
        \__latexprep_options_field:nn { OPTIONS } { opt@##1 }
        \__latexprep_options_field:nn { RAW } { @raw@opt@##1 }
      }
    \iow_now:Nn \g__latexprep_options_stream { END|TOKEN }
    \iow_close:N \g__latexprep_options_stream
    \group_end:
  }
\AddToHook{file/after}[latexprep-loaded-options]{\__latexprep_options_file:}
\AddToHook{enddocument/end}[latexprep-loaded-options]{\__latexprep_options_finish:}
\ExplSyntaxOff
\endinput
"""


@dataclass(frozen=True)
class LoadedOptionsProbe:
    token: str
    hook: Path
    sidecar: Path
    main: Path
    hook_bytes: bytes
    staged_bytes: bytes


def stage_loaded_options(project: Path, main: str, work: Path) -> LoadedOptionsProbe:
    """Add a line-neutral hook only to the existing disposable project copy."""
    main_path = project / main
    if main_path.stat().st_size > _MAX_MAIN_BYTES:
        raise PreparationError("Loaded-option instrumentation supports main files up to 8 MiB")
    original = main_path.read_bytes()
    token = secrets.token_hex(16)
    hook = work / f"latexprep-loaded-options-{token}.tex"
    hook_bytes = _HOOK.replace("TOKEN", token).encode("ascii")
    hook.write_bytes(hook_bytes)
    relative_hook = Path(os.path.relpath(hook, main_path.parent)).as_posix()
    # Workspaces and all path components from main are runtime-validated.  Only
    # the generated basename and ../ segments enter this TeX argument.
    if not re.fullmatch(r"(?:\.\./)+latexprep-loaded-options-[0-9a-f]{32}\.tex", relative_hook):
        raise PreparationError("Loaded-option hook is not outside the disposable project")
    prefix = (r"\input{" + relative_hook + r"}\relax ").encode("ascii")
    # Preserve a UTF-8 BOM and every original newline, including diagnostics on
    # the first source line.  No wrapper changes jobname or selected main file.
    offset = 3 if original.startswith(b"\xef\xbb\xbf") else 0
    staged = original[:offset] + prefix + original[offset:]
    main_path.write_bytes(staged)
    return LoadedOptionsProbe(
        token, hook, work / "output" / f"{hook.stem}.lpo", main_path, hook_bytes, staged
    )


def _native_string(value: str, engine: str) -> str:
    if len(value) > 8 * _MAX_FIELD_UNITS or len(value) % 8 or re.search(r"[^0-9A-F]", value):
        raise PreparationError("Option field is unavailable, oversized or malformed")
    units = [int(value[index : index + 8], 16) for index in range(0, len(value), 8)]
    try:
        if engine == "pdflatex":
            return bytes(units).decode("utf-8")
        return "".join(chr(unit) for unit in units).encode("utf-8").decode("utf-8")
    except (ValueError, UnicodeError) as error:
        raise PreparationError(
            "Option field contains unsupported native character units"
        ) from error


def _parse_options(text: str, token: str, engine: str) -> dict[str, object]:
    rows = text.splitlines()
    if not rows or rows[0] != f"START|{token}" or rows[-1] != f"END|{token}":
        raise PreparationError("Complete current-build option start/end markers are absent")
    if len(rows) < 6 or rows[1] != f"ENGINE|{engine}":
        raise PreparationError("Option instrumentation used an unsupported or different engine")
    result: dict[str, object] = {}
    for index, (tag, field) in enumerate(
        (
            ("FORMAT", "format_date"),
            ("GLOBAL", "global_class_options"),
            ("UNUSED", "unused_options"),
        ),
        2,
    ):
        if not rows[index].startswith(tag + "|"):
            raise PreparationError("Kernel option metadata is incomplete or reordered")
        result[field] = _native_string(rows[index][len(tag) + 1 :], engine)
    if (
        not re.fullmatch(r"20\d{2}[-/]\d{2}[-/]\d{2}", str(result["format_date"]))
        or str(result["format_date"]).replace("/", "-") < "2020-10-01"
    ):
        raise PreparationError("This LaTeX format does not establish supported kernel option state")
    records: list[dict[str, object]] = []
    names: set[str] = set()
    index = 5
    while index < len(rows) - 1:
        if rows[index].startswith("UNSUPPORTED|"):
            raise PreparationError(f"Unsupported option instrumentation: {rows[index][12:200]}")
        name = rows[index].removeprefix("PACKAGE|")
        if (
            not rows[index].startswith("PACKAGE|")
            or not _PACKAGE.fullmatch(name)
            or name in names
            or len(names) >= _MAX_PACKAGES
            or index + 3 > len(rows) - 1
        ):
            raise PreparationError("Kernel package option records are invalid or duplicated")
        names.add(name)
        record: dict[str, object] = {"name": name}
        for shift, tag, key in ((1, "OPTIONS", "options"), (2, "RAW", "raw_options")):
            if not rows[index + shift].startswith(tag + "|"):
                raise PreparationError("Kernel package option values are absent or reordered")
            record[key] = _native_string(rows[index + shift][len(tag) + 1 :], engine)
        records.append(record)
        index += 3
    if not any(name.endswith(".cls") for name in names):
        raise PreparationError("No loaded document class was established by the kernel hook")
    result["packages"] = records
    return result


def loaded_options_unavailable(reason: str, main: str) -> Finding:
    return Finding(
        "build.loaded_options",
        f"Actual loaded class/package options are inconclusive: {reason}.",
        "error",
        "inconclusive",
        path=main or None,
        details={"coverage_complete": False, "uncertainty": [reason]},
    )


def _unchanged_regular_file(path: Path, expected: bytes) -> bool:
    info = path.lstat()
    return (
        stat.S_ISREG(info.st_mode)
        and info.st_size == len(expected)
        and path.read_bytes() == expected
    )


def collect_loaded_options(
    probe: LoadedOptionsProbe,
    packages: list[dict[str, str | None]],
    recorder_inputs: set[Path],
    *,
    main: str,
    engine: str,
    recorder_complete: bool,
    build_complete: bool,
    max_bytes: int,
) -> Finding:
    """Require fresh complete evidence and a one-to-one recorder/kernel graph."""
    try:
        if not build_complete or not recorder_complete:
            raise PreparationError("A successful build and complete recorder are required")
        if probe.hook not in recorder_inputs:
            raise PreparationError("The final recorder did not observe the trusted options hook")
        if not _unchanged_regular_file(probe.hook, probe.hook_bytes) or not _unchanged_regular_file(
            probe.main, probe.staged_bytes
        ):
            raise PreparationError("The instrumented disposable source changed during execution")
        info = probe.sidecar.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > min(max_bytes, _MAX_EVIDENCE_BYTES):
            raise PreparationError("Option evidence is not a bounded regular output file")
        data = _parse_options(probe.sidecar.read_text(encoding="ascii"), probe.token, engine)
        recorded = {item["name"]: item for item in packages}
        records = data["packages"]
        assert isinstance(records, list)
        observed = {item["name"] for item in records}
        if len(recorded) != len(packages):
            raise PreparationError("Multiple recorder paths make a package origin ambiguous")
        if observed != set(recorded):
            raise PreparationError("Kernel class/package graph does not match the final recorder")
        for record in records:
            origin = recorded[record["name"]]
            if origin.get("origin") not in {"project", "toolchain"} or not origin.get("path"):
                raise PreparationError("A loaded package lacks an established recorder origin")
            record.update({"path": origin["path"], "origin": origin["origin"]})
        unresolved = [record["name"] for record in records if "\\" in str(record["options"])]
        if unresolved or "\\" in str(data["global_class_options"]):
            raise PreparationError(
                "Unexpanded control sequences remain in normalized kernel options"
            )
        data.update(
            {
                "coverage_complete": True,
                "engine": engine,
                "scope": "Post-document detokenized kernel opt@ and raw option lists; "
                "global class options are separate. Options include explicit and forwarded "
                "kernel state, not every consumed global option, package defaults, token "
                "category codes, or later setup commands. File origins come from the recorder.",
            }
        )
        return Finding(
            "build.loaded_options",
            f"Recorded post-document kernel options for {len(records)} loaded classes/packages.",
            "info",
            "passed",
            path=main,
            details=data,
        )
    except (OSError, UnicodeError, PreparationError) as error:
        return loaded_options_unavailable(str(error), main)
