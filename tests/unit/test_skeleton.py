"""Offline checks for the AgentBuildKit package surface."""

import importlib

import agentbuildkit

MODULES = [
    "agentbuildkit.agents",
    "agentbuildkit.config",
    "agentbuildkit.evaluation",
    "agentbuildkit.external_execution",
    "agentbuildkit.guardrails",
    "agentbuildkit.integrations",
    "agentbuildkit.mcp",
    "agentbuildkit.observability",
    "agentbuildkit.orchestrations",
    "agentbuildkit.providers",
    "agentbuildkit.runtime",
    "agentbuildkit.state",
    "agentbuildkit.testing",
    "agentbuildkit.workflows",
]


def test_version_is_set() -> None:
    assert agentbuildkit.__version__ == "0.1.0"


def test_submodules_import() -> None:
    for module_name in MODULES:
        imported = importlib.import_module(module_name)
        assert imported.__doc__
