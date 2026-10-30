"""Offline harness demonstration shipped with the AgentBuildKit package.

This sample uses the harness directly. It is not the customer-service example,
and it does not call a model provider.
"""

from agentbuildkit.demo.scenario import DemoError, DemoResult, run_demo

__all__ = ["DemoError", "DemoResult", "run_demo"]
