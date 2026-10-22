"""Fledge: LaTeX submission preparation with an interface-independent core."""

import logging

__version__ = "0.3.1"

# A library emits log records but does not decide where they go: without a handler of the
# program's own, nothing is printed. The command line opts in with ``--debug``.
logging.getLogger(__name__).addHandler(logging.NullHandler())
