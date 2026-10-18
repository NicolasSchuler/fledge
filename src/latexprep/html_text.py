"""Escaping of report values for the HTML renderers.

Every value a report puts into HTML, whether file name, message or tool output, is
untrusted text and passes through :func:`escape_html`.
"""

from __future__ import annotations

import json

from .text_safety import escape_html_text


def escape_html(value: object) -> str:
    """Escape untrusted text for HTML content and attributes; controls become visible.

    A value that is not text is shown as its JSON form.
    """
    return escape_html_text(
        value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    )
