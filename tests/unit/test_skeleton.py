"""Offline checks for the Phase 1 package skeleton."""

import importlib

import agent_platform

MODULES = [
    "agent_platform.agents",
    "agent_platform.config",
    "agent_platform.evaluation",
    "agent_platform.external_execution",
    "agent_platform.guardrails",
    "agent_platform.integrations",
    "agent_platform.mcp",
    "agent_platform.observability",
    "agent_platform.orchestrations",
    "agent_platform.runtime",
    "agent_platform.schemas",
    "agent_platform.testing",
    "agent_platform.tools",
    "agent_platform.workflows",
]


def test_version_is_set() -> None:
    assert agent_platform.__version__ == "1.0.0"


def test_submodules_import() -> None:
    for module_name in MODULES:
        imported = importlib.import_module(module_name)
        assert imported.__doc__
