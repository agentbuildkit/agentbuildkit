"""Reject a harness that does not provide the capabilities these templates call.

The published ``agentbuildkit`` 0.1.0 package does not provide these
capabilities. ``HARNESS_RELEASE_BOUND`` is the templates dependency for the
0.2.0 release. The check still runs so a forced install of an older harness
fails before the templates import missing modules.
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

HARNESS_RELEASE_BOUND = "agentbuildkit>=0.2.0,<0.3.0"


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
        f"These templates require {HARNESS_RELEASE_BOUND}. "
        "The published agentbuildkit 0.1.0 package does not provide them. "
        "Install the 0.2.0 harness and templates together. Until that release is "
        "published, install the harness and templates built from the same revision."
    )
