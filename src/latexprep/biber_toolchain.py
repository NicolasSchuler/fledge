"""A sealed single-architecture Biber that macOS Seatbelt may execute.

TeX Live ships Biber on macOS as a PAR-packed universal binary. The universal
wrapper shells out to the Xcode ``lipo`` stub, and the packed payload execs
itself out of ``$PAR_GLOBAL_TEMP``; neither is reachable under a fail-closed
profile. A single-architecture slice plus a warmed, then read-only, payload
directory is, and both live outside every tool workspace.
"""

from __future__ import annotations

import asyncio
import hashlib
import platform
import re
import stat
from dataclasses import dataclass, replace
from pathlib import Path

from .errors import PreparationError
from .file_safety import DirectoryIdentity, FileIdentity, regular_file_identity

# Mach-O universal ("fat") container headers, always big-endian. The 32-bit form
# uses fat_arch records and the 64-bit form fat_arch_64 records.
_FAT_MAGIC_32 = 0xCAFEBABE
_FAT_MAGIC_64 = 0xCAFEBABF
_FAT_CPU_TYPES = {"arm64": 0x0100000C, "x86_64": 0x01000007}
_MAX_FAT_ARCHITECTURES = 64
# A runtime-owned toolchain copy is interpolated into latexmk's Perl configuration
# and into the command line latexmk builds for it, so its path must be inert in
# both. Application-generated temporary paths satisfy this; nothing else is used.
_INERT_PATH = re.compile(r"[A-Za-z0-9_./-]+")


def _host_macho_slice(data: bytes) -> bytes:
    """Return the host-architecture slice of a Mach-O executable image.

    Universal binaries are split here rather than with ``lipo``: the Xcode
    command-line stub that ``lipo`` resolves to is never a permitted grant, and
    a universal Biber wrapper would shell out to it inside the sandbox. A
    single-architecture image is already the slice and is returned unchanged.
    """
    if len(data) < 8:
        raise PreparationError("The tool executable is too small to inspect")
    magic = int.from_bytes(data[:4], "big")
    if magic not in {_FAT_MAGIC_32, _FAT_MAGIC_64}:
        return data
    wide = magic == _FAT_MAGIC_64
    width = 32 if wide else 20
    count = int.from_bytes(data[4:8], "big")
    if not 1 <= count <= _MAX_FAT_ARCHITECTURES or 8 + count * width > len(data):
        raise PreparationError("The universal executable header is truncated or unusable")
    machine = platform.machine()
    if machine not in _FAT_CPU_TYPES:
        raise PreparationError(f"Unsupported host architecture for tool isolation: {machine}")
    for index in range(count):
        record = data[8 + index * width : 8 + (index + 1) * width]
        if int.from_bytes(record[:4], "big") != _FAT_CPU_TYPES[machine]:
            continue
        offset, size = (
            (int.from_bytes(record[8:16], "big"), int.from_bytes(record[16:24], "big"))
            if wide
            else (int.from_bytes(record[8:12], "big"), int.from_bytes(record[12:16], "big"))
        )
        if size < 8 or offset < 8 + count * width or offset + size > len(data):
            raise PreparationError("The universal executable slice lies outside its container")
        return data[offset : offset + size]
    raise PreparationError(f"The universal executable has no {machine} slice")


def _seal_toolchain(root: Path) -> None:
    """Drop every write bit so granted execution cannot be redirected later."""
    for path in sorted(root.rglob("*"), reverse=True):
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            path.chmod(0o500)
        elif stat.S_ISREG(info.st_mode):
            path.chmod(0o500 if info.st_mode & 0o111 else 0o400)
        else:
            raise PreparationError("The prepared tool payload contains an unsupported entry")
    root.chmod(0o500)


@dataclass(frozen=True, slots=True)
class BiberToolchain:
    """A runtime-owned single-architecture Biber with its extracted PAR payload.

    ``executable`` lives beside ``payload`` rather than inside it, so no granted
    program is ever reachable from its own writable workspace during warm-up.
    """

    executable: Path
    payload: Path
    identity: FileIdentity
    payload_identity: DirectoryIdentity
    sealed: bool = False

    def validate(self) -> None:
        """Confirm the granted copies are still the ones this runner prepared."""
        info = self.executable.lstat()
        if not stat.S_ISREG(info.st_mode) or FileIdentity.from_stat(info) != self.identity:
            raise PreparationError("The prepared bibliography tool was modified or replaced")
        info = self.payload.lstat()
        if not stat.S_ISDIR(info.st_mode) or DirectoryIdentity.from_stat(info) != (
            self.payload_identity
        ):
            raise PreparationError("The prepared bibliography payload was replaced")

    def seal(self) -> BiberToolchain:
        """Make the warmed copy read-only; it is ready to grant from then on."""
        _seal_toolchain(self.executable.parent)
        return replace(self, identity=regular_file_identity(self.executable), sealed=True)


def create_biber_toolchain(source: Path, directory: Path, *, max_file_bytes: int) -> BiberToolchain:
    """Copy the host slice of ``source`` into a new directory below ``directory``.

    The copy still needs one warm-up run, which unpacks its payload, before it
    is sealed.
    """
    if source.stat().st_size > max_file_bytes:
        raise PreparationError("The installed biber exceeds the tool file size limit")
    image = _host_macho_slice(source.read_bytes())
    base = directory / f"biber-{hashlib.sha256(image).hexdigest()[:16]}"
    executable, payload = base / "biber", base / "payload"
    if not _INERT_PATH.fullmatch(str(executable)):
        raise PreparationError(
            "An isolated bibliography tool needs a path of letters, digits, underscores, "
            f"dots, slashes or hyphens: {executable}"
        )
    try:
        base.mkdir(mode=0o700, parents=True, exist_ok=False)
    except FileExistsError as error:
        raise PreparationError(
            "An isolated bibliography tool requires a new runtime-owned directory"
        ) from error
    with executable.open("xb") as outgoing:
        outgoing.write(image)
    executable.chmod(0o500)
    payload.mkdir(mode=0o700)
    return BiberToolchain(
        executable,
        payload,
        regular_file_identity(executable),
        DirectoryIdentity.from_stat(payload.stat()),
    )


class BiberPreparation:
    """Whether this runner's sealed Biber is absent, warming up, ready or failed for good."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.toolchain: BiberToolchain | None = None
        self.failure: str | None = None

    @property
    def ready(self) -> bool:
        return self.toolchain is not None and self.toolchain.sealed

    def fail(self, message: str) -> None:
        self.failure = message
        self.toolchain = None

    def scope(self, command: list[str]) -> BiberToolchain | None:
        """Return the prepared payload only when this command may execute Biber.

        The runtime-owned path appears in the version probe and in the ``$biber``
        rule of a latexmk build that permits Biber. A build that disabled Biber
        never mentions it and therefore never receives these grants.
        """
        toolchain = self.toolchain
        if toolchain is None or not any(str(toolchain.executable) in arg for arg in command):
            return None
        toolchain.validate()
        return toolchain
