"""Unit constants and the limits that more than one module shares.

A limit that only one module uses stays beside the code it bounds. A constant lives
here when two or more modules would otherwise repeat the same number for the same
purpose, where the copies could drift apart. Every name carries its unit
(``_BYTES``, ``_ENTRIES``, ``_CHARS``, ``_MB``, ``_LEVELS``, ``_SECONDS``), and a comment states
what the number protects.

This module imports nothing, so any module may use it.
"""

from __future__ import annotations

KIB = 1024
MIB = 1024 * KIB

# One read or write when copying a file or draining a pipe: large enough for
# throughput, small enough that a hostile stream cannot grow a buffer in one step.
IO_CHUNK_BYTES = 64 * KIB

# Characters a scanning loop processes between two ``cancellation_point`` calls, so
# a cancelled analysis stops promptly without paying for a check per character.
CANCELLATION_STRIDE_CHARS = 4096

# Files and folders together in one project. This is the default of the ``max_files``
# import limit, and the bound on a project inventory, which lists an imported project.
MAX_PROJECT_ENTRIES = 10_000
# Defaults of the ``max_bytes`` and ``max_depth`` import limits: the combined size of
# the imported files, and the folder nesting accepted from an archive. ``max_bytes`` is
# also the fallback of ``max_import_bytes`` and ``max_generated_file_bytes``, so one
# number is the default of the import total and of the size of any one tool file.
DEFAULT_MAX_PROJECT_BYTES = 256 * MIB
DEFAULT_MAX_GENERATED_FILE_BYTES = DEFAULT_MAX_PROJECT_BYTES
DEFAULT_MAX_FOLDER_LEVELS = 32

# Defaults of the per-command tool limits that ``timeout_seconds`` and ``build_memory_mb``
# set: wall time of one tool command, and the memory of one build command.
DEFAULT_TOOL_TIMEOUT_SECONDS = 120
DEFAULT_BUILD_MEMORY_MB = 1024

# Deepest folder nesting walked when the caller sets no depth limit, and the depth a
# build copy accepts. Each level holds one open descriptor and one stack frame; macOS
# allows 256 descriptors by default.
MAX_WALKED_FOLDER_LEVELS = 128

# Files and folders together in one tool workspace. A build copy imports at most this
# many, and the resource monitor stops a tool whose workspace grows beyond it, so a
# freshly imported build copy is always within what the monitor accepts.
MAX_WORKSPACE_ENTRIES = 20_000

# Levels of nested objects, or links of an indirect-reference chain, that PDF
# inspection follows before it treats the file as cyclic or hostile.
MAX_PDF_NESTING_LEVELS = 100

# Memory reserved for one light tool call or task when its caller states no other
# need: a single qpdf, Poppler or MuPDF query, or a small scheduled task.
DEFAULT_LEASE_MEMORY_MB = 64
