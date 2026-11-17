"""Container health check.

It confirms the installed application imports and that a configured state
directory can be written. It does not call a model or open a network connection.
"""

import os
from pathlib import Path

from agentbuildkit.config import load_app_settings


def check() -> int:
    """Return 0 when the runtime is operational."""
    import agentbuildkit
    import agentbuildkit_templates.customer_service as customer_service

    if agentbuildkit.__name__ != "agentbuildkit":
        return 1
    if customer_service.__name__ != "agentbuildkit_templates.customer_service":
        return 1
    directory = load_app_settings().get("state_directory")
    if not isinstance(directory, str) or not directory:
        return 0
    path = Path(directory)
    if not path.is_dir() or not os.access(path, os.W_OK):
        return 1
    return 0


def main() -> None:
    """Exit with the health-check status."""
    raise SystemExit(check())


if __name__ == "__main__":
    main()
