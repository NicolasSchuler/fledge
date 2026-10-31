"""What every tool command starts with: resource-limit wrapper, environment and refusal check.

``with_ulimits`` wraps the final argv in a fixed shell that lowers the kernel limits
before it replaces itself with the tool. ``create_tool_environment`` builds the
variables a tool sees, from nothing: no variable of the caller is inherited, and every
cache or temporary location points into a fresh per-command state directory inside the
tool's own workspace. ``sandbox_refusal`` recognises the messages a sandbox backend
prints when it cannot isolate a command. Choosing and building the sandbox itself is
``sandbox``'s job, and running the command is ``runtime``'s.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import NamedTuple

from .limits import KIB
from .toolchain import ToolContext, texlive_root

# Open descriptors one tool process may hold. A TeX run holds the source, its inputs,
# fonts and outputs at once; this is far above that and far below the system limit.
OPEN_FILE_LIMIT = 256
# The name the limit shell reports for itself in its own error messages.
_LIMIT_SHELL_NAME = "fledge-limits"
_REFUSAL_MARKERS = ("sandbox_apply:", "sandbox initialization failed", "sandbox_compile:")
_BUBBLEWRAP_PREFIX = "bwrap:"
# TeX wraps log lines at 79 columns unless told otherwise; wrapping splits long citation
# and label names mid-word and hides them from the log parser.
_TEX_LOG_LINE_COLUMNS = 1000
_TEX_ERROR_CONTEXT_COLUMNS = 254
_TEX_ERROR_HALF_CONTEXT_COLUMNS = 238
_STATE_DIRECTORIES = ("tmp", "config", "cache", "texmf", "texmf-config", "texmf-var")


class ToolEnvironment(NamedTuple):
    """The environment of one tool command.

    Attributes:
        variables: The variables the tool runs with; nothing else is in its environment.
        state_directory: The fresh per-command directory that holds the tool's temporary,
            configuration and cache locations.
    """

    variables: dict[str, str]
    state_directory: Path


def sandbox_refusal(stderr: str) -> bool:
    """Return whether a backend reported that it could not apply its isolation.

    Seatbelt names its failures inside a longer line; Bubblewrap prefixes every
    message it prints itself (such as a refused user namespace) with ``bwrap:``.

    Args:
        stderr: The standard error output of the sandboxed command.

    Returns:
        ``True`` when ``stderr`` starts with the Bubblewrap prefix after leading whitespace
        or contains one of the refusal markers.
    """
    return stderr.lstrip().startswith(_BUBBLEWRAP_PREFIX) or any(
        marker in stderr for marker in _REFUSAL_MARKERS
    )


def with_ulimits(command: list[str], *, max_file_bytes: int, timeout_seconds: int) -> list[str]:
    """Return ``command`` behind a shell that lowers core, file-size, CPU and descriptor limits.

    The shell script is fixed and trusted; the command's own arguments are only ever
    passed on as arguments, never interpreted as shell source. Core files are disabled
    and the descriptor limit is ``OPEN_FILE_LIMIT``. The shell is ``/bin/bash`` where it
    exists, because the file-size argument is computed in the 1024-byte units bash uses,
    and ``/bin/sh`` otherwise.

    Args:
        command: The argv of the tool.
        max_file_bytes: The largest file the tool may write, in bytes; the limit is set in
            whole KiB, rounded down.
        timeout_seconds: The CPU time limit, in seconds.

    Returns:
        The argv that runs the limit shell, which then replaces itself with ``command``.
    """
    script = (
        'ulimit -c 0 || exit 125; ulimit -f "$1" || exit 125; '
        'ulimit -t "$2" || exit 125; ulimit -n "$3" || exit 125; '
        'shift 3; exec "$@"'
    )
    # `ulimit -f` counts 1024-byte units in bash outside POSIX mode, which is
    # what the computed argument assumes. Dash, the Debian/Ubuntu /bin/sh,
    # counts 512-byte blocks instead and would halve the limit; macOS /bin/sh
    # is bash and already agrees with /bin/bash here.
    shell = "/bin/bash" if Path("/bin/bash").is_file() else "/bin/sh"
    return [
        shell,
        "-c",
        script,
        _LIMIT_SHELL_NAME,
        str(max_file_bytes // KIB),
        str(timeout_seconds),
        str(OPEN_FILE_LIMIT),
        *command,
    ]


def tool_state_directory(workspace: Path) -> Path:
    """Return the directory that holds the state of the commands run in ``workspace``.

    :func:`create_tool_environment` creates each command's state directory there. Tools
    write their own files there, for example the font cache luaotfload keeps under
    ``TEXMFVAR``, and read them back; the TeX recorder lists such reads as inputs.
    """
    return workspace / ".tool-state"


def create_tool_environment(
    workspace: Path, context: ToolContext, *, source_date_epoch: str
) -> ToolEnvironment:
    """Create a fresh state directory under ``workspace`` and the environment that uses it.

    Nothing is inherited from the caller. The state directory (``.tool-state/invocation-*``)
    holds the tool's temporary, configuration and cache locations; the caller removes it
    when the command ends, because a later command must never find an earlier one's files.
    When ``context`` names a TeX Live root, the TeX search variables point into it.

    Args:
        workspace: The tool's workspace, under which the state directory is created.
        context: The toolchain whose search paths become ``PATH`` and whose resource roots
            locate TeX Live.
        source_date_epoch: The value of ``SOURCE_DATE_EPOCH`` for reproducible output.

    Returns:
        The tool's variables and its new state directory.
    """
    container = tool_state_directory(workspace)
    container.mkdir(exist_ok=True)
    state = Path(tempfile.mkdtemp(prefix="invocation-", dir=container))
    for name in _STATE_DIRECTORIES:
        (state / name).mkdir()
    # HOME is deliberately neither inherited nor repurposed.
    environment = {
        "PATH": os.pathsep.join(context.search_paths),
        # A neutral account name: latexmk warns when USER is unset, and the
        # real name never needs to reach a tool.
        "USER": "fledge",
        "LANG": "C",
        "LC_ALL": "C",
        "TMPDIR": str(state / "tmp"),
        "TMP": str(state / "tmp"),
        "TEMP": str(state / "tmp"),
        "XDG_CONFIG_HOME": str(state / "config"),
        "XDG_CACHE_HOME": str(state / "cache"),
        "TEXMFHOME": str(state / "texmf"),
        "TEXMFCONFIG": str(state / "texmf-config"),
        "TEXMFVAR": str(state / "texmf-var"),
        "TEXMFOUTPUT": str(state / "tmp"),
        "TEXMFLOCAL": str(state / "texmf"),
        "VARTEXFONTS": str(state / "texmf-var"),
        "TEXINPUTS": ".:",
        "BIBINPUTS": ".:",
        "BSTINPUTS": ".:",
        "shell_escape": "f",
        "openin_any": "r",
        "openout_any": "p",
        "MKTEXPK": "0",
        "MKTEXTFM": "0",
        "MKTEXFMT": "0",
        "MKTEXMF": "0",
        "max_print_line": str(_TEX_LOG_LINE_COLUMNS),
        "error_line": str(_TEX_ERROR_CONTEXT_COLUMNS),
        "half_error_line": str(_TEX_ERROR_HALF_CONTEXT_COLUMNS),
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "VECLIB_MAXIMUM_THREADS": "1",
        "RAYON_NUM_THREADS": "1",
        "PAR_TMPDIR": str(state / "tmp"),
        "SOURCE_DATE_EPOCH": source_date_epoch,
        "FORCE_SOURCE_DATE": "1",
    }
    root = texlive_root(context.resource_roots)
    if root is not None:
        search = (
            state / "texmf-config",
            state / "texmf-var",
            state / "texmf",
            root / "texmf-config",
            root / "texmf-var",
            root / "texmf-dist",
        )
        environment.update(
            {
                "TEXMFCNF": str(root / "texmf-dist/web2c"),
                "TEXMFSYSCONFIG": str(root / "texmf-config"),
                "TEXMFSYSVAR": str(root / "texmf-var"),
                "TEXMF": "{" + ",".join(map(str, search)) + "}",
            }
        )
    return ToolEnvironment(environment, state)
