"""The folders publication writes: the output marker, ownership checks and safe moves.

Owns the low-level steps that let publication write only into directories it created
itself: recognising an earlier Fledge output by ``OUTPUT_MARKER`` and a Fledge report by
how it begins, checking that a folder is a real directory of the current user's, creating
exclusive hidden siblings, moving a staged output into the place of an existing folder
after checking that the folder is unchanged, and removing a tree without ever following a
link. ``publication`` composes these steps into one transaction.
"""

from __future__ import annotations

import errno
import fcntl
import os
import re
import secrets
import shutil
import stat
from collections.abc import Generator, Iterable
from contextlib import ExitStack, contextmanager, suppress
from pathlib import Path
from typing import NamedTuple

from .file_safety import DIRECTORY_FLAGS, DirectoryIdentity, read_file_prefix

# The file in every directory publication writes; a later run may replace only such a
# directory, and only when asked to.
OUTPUT_MARKER = ".fledge-output"
MARKER_TEXT = (
    "Fledge {version} wrote this folder. A later run with --force may replace it once the "
    "new run has succeeded; Fledge refuses to replace a folder without this file.\n"
)
# How much of an existing report file is read to recognise it as a Fledge report, and the
# beginnings that recognise one: the JSON report and the HTML page, as Fledge writes them.
_REPORT_PREFIX_BYTES = 4096
_REPORT_BEGINNINGS = (
    re.compile(rb'\A\s*\{\s*"schema_version"\s*:\s*\d+\s*,\s*"command"\s*:\s*"'),
    re.compile(rb"\A<!doctype html><html[^>]*><head>.*?<title>Fledge [^<]*report", re.DOTALL),
)
# The names of the sibling directories that receive a new output and the replaced one.
STAGING_PREFIX = ".fledge-staging-"
REPLACED_PREFIX = ".fledge-replaced-"
# Whether this platform removes a directory tree through directory descriptors, so a link
# planted in the tree while it is removed is unlinked, never followed (macOS and Linux).
# Read once, so a test that replaces ``shutil.rmtree`` does not change the answer.
SAFE_TREE_REMOVAL = bool(getattr(shutil.rmtree, "avoids_symlink_attacks", False))


def is_fledge_output(path: Path) -> bool:
    """Return whether ``path`` is a real directory that publication wrote: it holds the marker."""
    marker = path / OUTPUT_MARKER
    return path.is_dir() and not path.is_symlink() and marker.is_file() and not marker.is_symlink()


def is_fledge_report(path: Path) -> bool:
    """Return whether ``path`` is a regular file that begins like a Fledge JSON or HTML report."""
    try:
        beginning = read_file_prefix(path, _REPORT_PREFIX_BYTES)
    except OSError:
        return False
    return any(pattern.match(beginning) for pattern in _REPORT_BEGINNINGS)


def is_own_directory(path: Path) -> bool:
    """Return whether ``path`` is a real directory, not a link, that the current user owns.

    Without user ids (outside POSIX), every real directory counts as one's own.
    """
    try:
        status = path.lstat()
    except OSError:
        return False
    user = getattr(os, "getuid", None)
    return stat.S_ISDIR(status.st_mode) and (user is None or status.st_uid == user())


def is_empty_directory(path: Path) -> bool:
    """Return whether ``path`` is a real, readable directory of the user's with nothing in it."""
    if not is_own_directory(path):
        return False
    try:
        with os.scandir(path) as entries:
            return next(entries, None) is None
    except OSError:
        return False


def is_replaceable_output(path: Path) -> bool:
    """Return whether ``path`` is an earlier Fledge output of the user's, which force replaces."""
    return is_fledge_output(path) and is_own_directory(path)


def existing_identity(path: Path) -> DirectoryIdentity | None:
    """Identify an existing entry without following a link, or return None if absent."""
    try:
        return DirectoryIdentity.from_stat(path.lstat())
    except FileNotFoundError:
        return None


@contextmanager
def lock_output_parents(destinations: Iterable[Path]) -> Generator[None]:
    """Serialize cooperating commits in each destination parent without creating files."""
    with ExitStack() as descriptors:
        for parent in sorted({path.parent for path in destinations}):
            descriptor = os.open(parent, DIRECTORY_FLAGS)
            descriptors.callback(os.close, descriptor)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


# A staging or set-aside directory is private while the transaction uses it.
_PRIVATE_DIRECTORY_MODE = 0o700


class StagedOutput(NamedTuple):
    """An output written beside an existing directory, to take its place once complete.

    Attributes:
        staging: The new sibling directory, created by the transaction, that receives the
            output.
        destination: The existing directory whose place the output takes.
        replaces: Whether ``destination`` is an earlier Fledge output that is replaced;
            otherwise it is an empty directory.
        mode: The permission bits of ``destination`` when it was accepted, which the output
            receives as it takes its place, so a folder the user kept private stays private.
            Access control lists and extended attributes are not carried over.
        identity: The identity of the accepted destination directory.
    """

    staging: Path
    destination: Path
    replaces: bool
    mode: int
    identity: DirectoryIdentity


def remove_tree(path: Path) -> None:
    """Remove a directory tree that the transaction owns, never following a link.

    Raises:
        OSError: The tree could not be removed; ``path`` is a link; or this platform's
            removal could follow a link planted while it runs, so the tree is left.
    """
    if not SAFE_TREE_REMOVAL:
        raise OSError(
            errno.EPERM,
            "this platform cannot remove a folder without risking following a link in it",
            str(path),
        )
    # Removing through directory descriptors: a link is unlinked, never followed, and a
    # link in place of ``path`` itself is refused.
    shutil.rmtree(path)


def new_sibling(path: Path, prefix: str) -> Path:
    """Create a new hidden directory beside ``path`` whose name starts with ``prefix``.

    The directory is readable by its owner only while the transaction uses it.

    Returns:
        The new, empty directory, created exclusively.
    """
    while True:
        sibling = path.parent / f"{prefix}{secrets.token_hex(4)}"
        try:
            sibling.mkdir(mode=_PRIVATE_DIRECTORY_MODE)
        except FileExistsError:
            continue
        return sibling


def _require_unchanged(staged: StagedOutput) -> None:
    """Check that the existing directory of a staged output is still what was accepted.

    It must be the same real directory of the user's; an earlier output must still
    hold its marker. Emptiness is left to ``rmdir``, which refuses a non-empty directory.

    Raises:
        OSError: The directory is gone, is a link or something else, or lost its marker.
    """
    if not is_own_directory(staged.destination):
        raise NotADirectoryError(
            errno.ENOTDIR, "it is no longer a folder of yours", str(staged.destination)
        )
    current = staged.destination.lstat()
    if (
        DirectoryIdentity.from_stat(current) != staged.identity
        or stat.S_IMODE(current.st_mode) != staged.mode
    ):
        raise OSError(errno.EBUSY, "the accepted output folder changed", str(staged.destination))
    if staged.replaces and not is_fledge_output(staged.destination):
        raise FileNotFoundError(
            errno.ENOENT, "it no longer holds the Fledge output marker", str(staged.destination)
        )


def _restore_empty_directory(staged: StagedOutput) -> None:
    """Put back the empty directory whose place the output was to take, with its mode.

    ``mkdir`` applies the umask, which only removes permissions, so the directory is never
    more open than it was; ``chmod`` then restores the recorded bits exactly.

    Raises:
        OSError: The directory could not be created or given its mode.
    """
    staged.destination.mkdir(mode=staged.mode)
    staged.destination.chmod(staged.mode)


def move_into_place(staged: StagedOutput) -> Path | None:
    """Move a complete output from its sibling into the place of the existing directory.

    An empty directory is removed with ``rmdir``, which fails if anything, such as a
    planted link, appeared in it. An earlier output is moved aside into a new hidden
    sibling. A directory renamed onto an existing path replaces only an empty directory,
    so a link or file put in place of the existing directory makes the move fail instead
    of being followed or overwritten.

    Args:
        staged: The staged output.

    Returns:
        Where the earlier output now is, a hidden sibling directory, or ``None`` when the
        output took the place of an empty directory.

    Raises:
        OSError: The existing directory changed, or a move failed; the existing directory
            is back in place when it can be.
    """
    _require_unchanged(staged)
    # The output takes the permissions of the directory whose place it takes.
    staged.staging.chmod(staged.mode)
    if not staged.replaces:
        # rmdir neither follows a link nor removes a directory that is no longer empty.
        staged.destination.rmdir()
        try:
            staged.staging.rename(staged.destination)
        except OSError:
            with suppress(OSError):
                _restore_empty_directory(staged)
            raise
        return None
    previous = new_sibling(staged.destination, REPLACED_PREFIX)
    try:
        # Renaming a directory onto an empty one replaces it in one step.
        staged.destination.replace(previous)
    except OSError:
        previous.rmdir()
        raise
    try:
        if existing_identity(previous) != staged.identity:
            raise OSError(
                errno.EBUSY, "the accepted output folder changed", str(staged.destination)
            )
        staged.staging.rename(staged.destination)
    except OSError as error:
        try:
            previous.rename(staged.destination)
        except OSError:
            error.add_note(f"The earlier output is now in {previous}.")
        raise
    return previous


def move_back(staged: StagedOutput, previous: Path | None) -> bool:
    """Undo a completed move: the new output goes back beside, the existing one returns.

    The rollback that follows removes the sibling, never the existing directory.

    Args:
        staged: The staged output that was moved into place.
        previous: Where the earlier output was moved, or ``None`` for an empty directory.

    Returns:
        Whether the existing directory is back in place; when a move fails, the
        directories stay where they are.
    """
    try:
        staged.destination.rename(staged.staging)
        if previous is None:
            _restore_empty_directory(staged)
        else:
            previous.rename(staged.destination)
    except OSError:
        return False
    return True
