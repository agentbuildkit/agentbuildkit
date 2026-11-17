"""Load the first-party templates package by name.

This module does not search installed distributions and does not import a
module named by configuration. ``agentbuildkit_templates`` is the only
accepted package.
"""

import importlib.util

TEMPLATES_MODULE = "agentbuildkit_templates"
# Release preparation. The templates package records the same empty bound.
# Set both to the first harness release that includes the template host
# capabilities. The published 0.1.0 harness is not that release.
HARNESS_RELEASE_BOUND = ""


def templates_available() -> bool:
    """True when the first-party templates package can be imported."""
    return importlib.util.find_spec(TEMPLATES_MODULE) is not None


def templates_missing_message() -> str:
    """Tell the operator how to add the dashboard and drafting server."""
    return (
        "The dashboard and local drafting server are in the agentbuildkit-templates "
        "package. Install that package in this environment. The published "
        "agentbuildkit 0.1.0 package and the published npx agentbuildkit package "
        "do not include it."
    )


def flock_available() -> bool:
    """True when this interpreter can import the POSIX flock module."""
    return importlib.util.find_spec("fcntl") is not None
