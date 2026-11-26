"""The folders publication writes: the output marker, ownership checks and safe moves.

Owns the low-level steps that let publication write only into directories it created
itself: recognising an earlier Fledge output by ``OUTPUT_MARKER`` and a Fledge report by
how it begins, checking that a folder is a real directory of the current user's, creating
exclusive hidden siblings, making no-replace moves, and removing trees without following
links. ``publication`` composes these steps into one transaction.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import os
import re
import secrets
import shutil
import stat
import sys
from collections.abc import Generator, Iterable
from contextlib import ExitStack, contextmanager
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
# The name of the private sibling that holds the new output and any set-aside directory.
STAGING_PREFIX = ".fledge-staging-"
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


# Keep the payload inaccessible even when it already has its final permissions.
_PRIVATE_DIRECTORY_MODE = 0o700


class StagedOutput(NamedTuple):
    """A payload and optional set-aside directory in one private sibling of the destination.

    Attributes:
        staging: Where the new output is written, below the private sibling.
        destination: Where the new output belongs.
        replaces: Whether an earlier Fledge output may be removed after commit, rather than
            only an empty directory.
        mode: The accepted directory's permissions, or the normal creation mode for a new one.
            The output takes those bits, not the earlier directory's ACLs or extended attributes.
        identity: Identity of the accepted directory, or ``None`` if absent.
        staging_identity: Identity of the payload, which stays the same after a move.
        wrapper_identity: Identity of the private sibling, which never moves.
    """

    staging: Path
    destination: Path
    replaces: bool
    mode: int
    identity: DirectoryIdentity | None
    staging_identity: DirectoryIdentity
    wrapper_identity: DirectoryIdentity

    @property
    def wrapper(self) -> Path:
        """Return the private sibling containing the payload and any set-aside directory."""
        return self.staging.parent

    @property
    def previous(self) -> Path:
        """Return the reserved path for the accepted directory when moved aside."""
        return self.wrapper / "previous"

    def require_wrapper(self) -> None:
        """Refuse to use a private sibling that no longer belongs to the transaction."""
        _require_identity(self.wrapper, self.wrapper_identity, "the staging wrapper changed")

    def remove_wrapper(self) -> None:
        """Remove the private sibling only if it is still ours and empty."""
        self.require_wrapper()
        self.wrapper.rmdir()


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


def _new_sibling(path: Path) -> Path:
    """Create an initially private staging sibling without replacing any existing entry."""
    while True:
        sibling = path.parent / f"{STAGING_PREFIX}{secrets.token_hex(4)}"
        if sibling == path:
            continue
        try:
            sibling.mkdir(mode=_PRIVATE_DIRECTORY_MODE)
        except FileExistsError:
            continue
        return sibling


def _set_mode(path: Path, current: int, wanted: int) -> None:
    """Change permission bits only when needed and verify the filesystem retained them."""
    if current != wanted:
        path.chmod(wanted, follow_symlinks=False)
        # Some filesystems can silently clear setgid when chmod is called by a nonmember.
        if stat.S_IMODE(path.lstat().st_mode) != wanted:
            raise OSError(errno.EPERM, "could not apply the directory permissions", str(path))


def stage_output(
    destination: Path, *, replaces: bool, mode: int | None, identity: DirectoryIdentity | None
) -> StagedOutput:
    """Create a new payload behind a private wrapper beside its destination."""
    wrapper = _new_sibling(destination)
    info = wrapper.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise OSError(errno.EBUSY, "the staging wrapper changed", str(wrapper))
    wrapper_identity = DirectoryIdentity.from_stat(info)
    staging = wrapper / "payload"
    staging_identity = None
    try:
        wrapper_mode = stat.S_IMODE(info.st_mode)
        # Keep the wrapper private without losing the parent's group inheritance.
        _set_mode(wrapper, wrapper_mode, _PRIVATE_DIRECTORY_MODE | (wrapper_mode & stat.S_ISGID))
        # Let mkdir apply the umask and default permissions behind the private wrapper.
        staging.mkdir()
        info = staging.lstat()
        staging_identity = DirectoryIdentity.from_stat(info)
        created_mode = stat.S_IMODE(info.st_mode)
        if mode is None:
            mode = created_mode
        # The wrapper provides privacy; the payload only needs enough owner access to write it.
        _set_mode(staging, created_mode, created_mode | stat.S_IRWXU)
        return StagedOutput(
            staging, destination, replaces, mode, identity, staging_identity, wrapper_identity
        )
    except BaseException as error:
        try:
            _require_identity(wrapper, wrapper_identity, "the staging wrapper changed")
            if staging_identity is not None:
                _require_identity(staging, staging_identity, "the staging directory changed")
                staging.rmdir()
            wrapper.rmdir()
        except OSError as cleanup_error:
            error.add_note(
                f"Could not remove the incomplete staging wrapper {wrapper} ({cleanup_error})."
            )
        raise


def _rename_exclusive(source: Path, destination: Path) -> None:
    """Move ``source`` without ever replacing ``destination``, even if it is empty.

    The Python rename API has no such option for directories. Fail closed if the platform
    or filesystem does not support the native no-replace operation.

    Args:
        source: The entry to move.
        destination: The new path, which must not exist.

    Raises:
        OSError: The move failed or the operation is unsupported.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    origin, target = os.fsencode(source), os.fsencode(destination)
    try:
        if sys.platform == "darwin":
            rename = libc.renamex_np
            rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
            result = rename(origin, target, 0x00000004)  # RENAME_EXCL
        elif sys.platform == "linux":
            rename = libc.renameat2
            rename.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            ]
            result = rename(-100, origin, -100, target, 1)  # AT_FDCWD, RENAME_NOREPLACE
        else:
            raise AttributeError("no native no-replace rename")
    except AttributeError as error:
        raise OSError(
            errno.ENOTSUP, "this platform cannot move an output without replacing it"
        ) from error
    if result != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(source), None, str(destination))


def _require_identity(path: Path, expected: DirectoryIdentity | None, message: str) -> None:
    """Refuse to move or remove an entry that is not the recorded one.

    Args:
        path: The entry to check.
        expected: Its recorded identity.
        message: What changed, for the error.

    Raises:
        OSError: The entry is missing or has a different identity.
    """
    if expected is None or existing_identity(path) != expected:
        raise OSError(errno.EBUSY, message, str(path))


def _require_unchanged(staged: StagedOutput, path: Path) -> None:
    """Check that a directory to be replaced is still the same folder of the user's.

    Args:
        staged: The staged output replacing the accepted directory.
        path: The accepted directory, either in place or set aside.

    Raises:
        OSError: The directory changed or lost its marker.
    """
    if not is_own_directory(path):
        raise NotADirectoryError(errno.ENOTDIR, "it is no longer a folder of yours", str(path))
    _require_identity(path, staged.identity, "the accepted output folder changed")
    if stat.S_IMODE(path.lstat().st_mode) != staged.mode:
        raise OSError(errno.EBUSY, "the accepted output folder changed", str(path))
    if staged.replaces and not is_fledge_output(path):
        raise FileNotFoundError(
            errno.ENOENT, "it no longer holds the Fledge output marker", str(path)
        )


def _restore(source: Path, destination: Path, cause: BaseException) -> None:
    """Try to return a moved directory without clobbering a new arrival."""
    try:
        _rename_exclusive(source, destination)
    except OSError as error:
        cause.add_note(f"Could not restore {destination} from {source} ({error}).")


def move_into_place(staged: StagedOutput) -> None:
    """Move a complete output into place without clobbering any concurrent arrival.

    The payload receives its final permissions while still behind the private wrapper.
    An accepted directory is moved into the same wrapper and checked there. The caller
    must undo the moves if this call or a later installation fails.

    Args:
        staged: The staged output.

    Raises:
        OSError: The output could not be moved into place.
    """
    staged.require_wrapper()
    _require_identity(staged.staging, staged.staging_identity, "the staging directory changed")
    _set_mode(staged.staging, stat.S_IMODE(staged.staging.lstat().st_mode), staged.mode)
    if staged.identity is not None:
        _require_unchanged(staged, staged.destination)
        _rename_exclusive(staged.destination, staged.previous)
        _require_unchanged(staged, staged.previous)
        if not staged.replaces and not is_empty_directory(staged.previous):
            raise OSError(errno.ENOTEMPTY, "the accepted output folder is no longer empty")
    _rename_exclusive(staged.staging, staged.destination)


def _return_payload(staged: StagedOutput, cause: BaseException) -> None:
    """Return this run's output to staging if its installation took effect."""
    if existing_identity(staged.staging) == staged.staging_identity:
        return
    _require_identity(staged.destination, staged.staging_identity, "the published output changed")
    _rename_exclusive(staged.destination, staged.staging)
    try:
        _require_identity(staged.staging, staged.staging_identity, "the published output changed")
    except OSError:
        _restore(staged.staging, staged.destination, cause)
        raise


def move_back(staged: StagedOutput, cause: BaseException) -> None:
    """Undo the attempted installation using on-disk evidence of the moves that occurred.

    An interrupt can arrive after a rename took effect but before it returned to Python.
    The known staging identity and reserved path for the old directory let us undo either
    move, without relying on a return value or overwriting a concurrent arrival.

    Args:
        staged: The output whose installation was attempted.
        cause: The original error, which receives notes for moves that could not be undone.
    """
    staged.require_wrapper()
    try:
        _return_payload(staged, cause)
    except OSError as error:
        cause.add_note(f"Could not undo publication at {staged.destination} ({error}).")
    # Restore whatever was moved aside, even if the verification after that move failed.
    if staged.identity is not None and existing_identity(staged.previous) is not None:
        _restore(staged.previous, staged.destination, cause)


def discard_previous(staged: StagedOutput) -> None:
    """Remove an accepted directory after a successful commit, if one was set aside."""
    if staged.identity is None:
        return
    staged.require_wrapper()
    _require_identity(staged.previous, staged.identity, "the set-aside directory changed")
    if staged.replaces:
        remove_tree(staged.previous)
    else:
        # Do not delete anything that was added after the emptiness check.
        staged.previous.rmdir()
