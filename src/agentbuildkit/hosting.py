"""Load the first-party templates package by name.

This module does not search installed distributions and does not import a
module named by configuration. ``agentbuildkit_templates`` is the only
accepted package.
"""

import importlib.util

TEMPLATES_MODULE = "agentbuildkit_templates"
HARNESS_RELEASE_BOUND = "agentbuildkit>=0.2.0,<0.3.0"


def templates_available() -> bool:
    """True when the first-party templates package can be imported."""
    return importlib.util.find_spec(TEMPLATES_MODULE) is not None


def templates_missing_message() -> str:
    """Tell the operator how to add the dashboard and drafting server."""
    return (
        "The dashboard and local drafting server are in the agentbuildkit-templates "
        "package. Install agentbuildkit>=0.2.0,<0.3.0 and "
        "agentbuildkit-templates>=0.2.0,<0.3.0 together. The published "
        "agentbuildkit 0.1.0 package and the published npx agentbuildkit 0.0.2 "
        "package do not include the templates. Those 0.2.0 commands are the "
        "upcoming release and are not on the public index yet."
    )


def flock_available() -> bool:
    """True when this interpreter can import the POSIX flock module."""
    return importlib.util.find_spec("fcntl") is not None
