"""Make the suite exercise this checkout's ``latexprep`` and never an installed copy.

An old non-editable install in site-packages would otherwise be imported silently and
the tests would pass or fail against stale code. Putting ``src`` first on ``sys.path``
prevents that for ``python -m unittest discover -s tests -t .`` and for dotted test
names; a copy that was already imported from elsewhere cannot be undone, so fail loudly.
"""

import importlib.util
import sys
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"

if str(SOURCE_ROOT) not in sys.path[:1]:
    sys.path.insert(0, str(SOURCE_ROOT))

_loaded = sys.modules.get("latexprep")
_spec = None if _loaded is not None else importlib.util.find_spec("latexprep")
_origin = getattr(_loaded, "__file__", None) or getattr(_spec, "origin", None)
_origin = Path(_origin).resolve() if _origin else None
if _origin is not None and SOURCE_ROOT not in _origin.parents:
    raise ImportError(
        f"The tests would use latexprep from {_origin.parent}, not this checkout's {SOURCE_ROOT}. "
        "Remove the stale install (python -m pip uninstall fledge latex-preparation) and run "
        "`python -m pip install -e .`, or start Python with a fresh interpreter so that "
        "`import tests` runs before anything imports latexprep."
    )
