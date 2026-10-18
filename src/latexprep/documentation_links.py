"""Where the hosted documentation lives, for messages that send a researcher to it.

``DOCUMENTATION_URL`` is the ``Documentation`` URL of ``pyproject.toml``; a test keeps
the two equal. Owns only addresses, not the text of any message.
"""

from __future__ import annotations

DOCUMENTATION_URL = "https://nicolasschuler.github.io/fledge/"
INSTALLATION_GUIDE = f"{DOCUMENTATION_URL}installation.html"
CONFIGURATION_GUIDE = f"{DOCUMENTATION_URL}configuration.html"
