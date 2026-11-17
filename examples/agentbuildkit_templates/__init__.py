"""First-party Customer Service and Operations Reporting templates.

The harness imports this package by name. It does not search for other
template packages. Importing this package checks that the installed harness
provides the capabilities these templates call.
"""

from agentbuildkit_templates.compat import HARNESS_RELEASE_BOUND, require_harness

require_harness()

__all__ = ["HARNESS_RELEASE_BOUND", "require_harness"]
