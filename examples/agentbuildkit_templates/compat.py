"""Reject a harness that does not provide the capabilities these templates call.

The published ``agentbuildkit`` 0.1.0 package uses the same version string as a
locally built harness and does not provide these capabilities. A version
specifier cannot tell those artifacts apart. ``HARNESS_RELEASE_BOUND`` stays
empty until the first harness release that includes the capabilities is
published. Set the templates dependency to that version before publishing
``agentbuildkit-templates``.
"""

import importlib
from importlib.metadata import PackageNotFoundError, version

REQUIRED_CAPABILITIES = (
    ("agentbuildkit.applications", "RenderedPage"),
    ("agentbuildkit.teams", "load_bundle_json"),
    ("agentbuildkit.revisions", "RevisionService"),
    ("agentbuildkit.revisions.store", "StartupLockError"),
    ("agentbuildkit.drafting", "build_server"),
)

# Release preparation: replace this empty bound with the first harness version
# that provides REQUIRED_CAPABILITIES. Do not publish a dependency on 0.1.0.
HARNESS_RELEASE_BOUND = ""


class HarnessIncompatible(RuntimeError):
    """The installed harness cannot run these templates."""


def require_harness() -> None:
    """Raise when the installed harness is missing a required capability."""
    missing = tuple(
        f"{module_name}.{attribute}"
        for module_name, attribute in REQUIRED_CAPABILITIES
        if not _provides(module_name, attribute)
    )
    if missing:
        raise HarnessIncompatible(_message(missing))


def _provides(module_name: str, attribute: str) -> bool:
    try:
        module = importlib.import_module(module_name)
    except ImportError:
        return False
    return hasattr(module, attribute)


def _installed_version() -> str:
    try:
        return version("agentbuildkit")
    except PackageNotFoundError:
        return "unknown"


def _message(missing: tuple[str, ...]) -> str:
    installed = _installed_version()
    needed = ", ".join(missing)
    return (
        "agentbuildkit-templates cannot use this harness. "
        f"The installed agentbuildkit package is {installed}. "
        f"These templates need {needed}. "
        "The published agentbuildkit 0.1.0 package does not provide them. "
        "Install the harness and the templates package built from the same revision. "
        "Before agentbuildkit-templates is published, its dependency must require "
        "at least the first harness release that includes those capabilities. "
        "That version has not been released, so there is no public install command yet."
    )
