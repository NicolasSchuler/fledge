r"""Ask the TeX installation which files it holds, through ``kpsewhich`` under isolation.

A project may read a file of the installation by its bare name, as
``\input{glyphtounicode}`` does, and a local class may shadow a system one. Both ask
``kpsewhich`` from an empty folder, so that the project's own files cannot answer, and
both share the bounds and the reading of its output here.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from .command_runner import CommandRunner
from .errors import PreparationError
from .source_resolution import InstalledInputs

logger = logging.getLogger(__name__)

# Most names one lookup asks about; a project that lacks more is reported as it is.
MAX_INSTALLATION_NAMES = 32


async def installed_files(
    names: Sequence[str], work: Path, runner: CommandRunner
) -> dict[str, str]:
    """Return the installation path of each of ``names`` that ``kpsewhich`` finds.

    Args:
        names: Plain file names with their suffixes, at most ``MAX_INSTALLATION_NAMES``.
        work: A new directory in which the version probe and the lookup each get their own
            folder.
        runner: The runner of ``kpsewhich``.

    Returns:
        The absolute path of each name found, by name; a name the installation lacks is
        absent.

    Raises:
        PreparationError: More names than the bound were given, the lookup did not complete
            within its limits or exited with a status that is not a count of the names, or it
            returned a relative path, several paths for one name, or a number of files that
            contradicts its status.
        OSError: A lookup folder cannot be created.
    """
    if len(names) > MAX_INSTALLATION_NAMES:
        raise PreparationError(f"At most {MAX_INSTALLATION_NAMES} names are looked up at once.")
    (work / "version").mkdir(parents=True)
    await runner.tool_version(["kpsewhich", "--version"], work / "version")
    lookup = work / "lookup"
    lookup.mkdir()
    execution = await runner.run(
        ["kpsewhich", "--format=tex", *names], cwd=lookup, workspace=lookup
    )
    # kpsewhich exits with the number of names it found no file for.
    if not execution.within_limits or not 0 <= execution.returncode <= len(names):
        raise PreparationError(
            f"TeX installation lookup did not complete: {execution.failure_reason('kpsewhich')}"
        )
    found: dict[str, str] = {}
    wanted = set(names)
    for line in execution.stdout.splitlines():
        path = line.strip()
        name = PurePosixPath(path).name
        if not path or name not in wanted:
            continue
        if not Path(path).is_absolute() or name in found:
            raise PreparationError("TeX installation lookup returned ambiguous evidence")
        found[name] = path
    # Output that names more or fewer files than the status says contradicts it.
    if len(wanted) - len(found) != execution.returncode:
        raise PreparationError("TeX installation lookup returned ambiguous evidence")
    return found


async def ask_installation(
    names: Sequence[str], work: Path, runner: CommandRunner
) -> InstalledInputs | None:
    r"""Ask which of the bare input names a project lacks the TeX installation holds.

    Args:
        names: The names from ``installation_candidates``; only the first
            ``MAX_INSTALLATION_NAMES`` are asked about.
        work: A new directory for the lookup.
        runner: The runner of ``kpsewhich``.

    Returns:
        The names asked about and those found, or ``None`` when there is nothing to ask or
        the installation cannot answer; the names are then reported missing as before.
    """
    asked = list(names[:MAX_INSTALLATION_NAMES])
    if not asked:
        return None
    try:
        found = await installed_files(asked, work, runner)
    except (PreparationError, OSError) as error:
        logger.info("TeX installation lookup for %d input names failed: %s", len(asked), error)
        return None
    logger.debug("TeX installation holds %d of %d input names", len(found), len(asked))
    return InstalledInputs(frozenset(asked), frozenset(found))
