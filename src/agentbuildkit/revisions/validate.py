"""Structural checks for a draft. Live credentials are a separate report."""

import hashlib
import json
import re

from agentbuildkit.revisions.models import (
    AgentConfig,
    Finding,
    TeamConfig,
    TeamTemplate,
)
from agentbuildkit.teams import BundleAgent, TeamBundle

_SECRET = re.compile(
    r"(?i)(api[_ -]?key|secret|token|password|bearer\s|sk-[a-z0-9]|begin private|composio_)"
)
_SIDE_EFFECTS = frozenset({"schedule_service", "service.schedule"})


def content_hash(config: TeamConfig) -> str:
    """Hash the canonical configuration. Argument values and secrets are absent."""
    payload = config.model_dump(mode="json")
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def config_from_template(template: TeamTemplate) -> TeamConfig:
    """The shipped configuration. Tools are the template's permitted bindings."""
    return TeamConfig(
        title=template.title,
        description=template.description,
        agents=tuple(
            AgentConfig(
                name=agent.name,
                purpose=agent.purpose,
                instructions=agent.instructions,
                provider=agent.provider,
                model=agent.model,
                tools=agent.permitted_tools,
            )
            for agent in template.agents
        ),
    )


def to_bundle(template: TeamTemplate, config: TeamConfig) -> TeamBundle:
    """Build a version-one bundle. Schemas and role names come from the template."""
    by_name = {agent.name: agent for agent in config.agents}
    agents: list[BundleAgent] = []
    for contract in template.agents:
        agent = by_name[contract.name]
        agents.append(
            BundleAgent(
                name=contract.name,
                purpose=agent.purpose,
                instructions=agent.instructions,
                provider=agent.provider,
                model=agent.model,
                input_schema=contract.input_schema,
                output_schema=contract.output_schema,
                tools=tuple(tool for tool in agent.tools if tool in contract.permitted_tools),
            )
        )
    return TeamBundle(
        schema_version="1",
        name=template.team_id,
        id=template.team_id,
        title=config.title,
        agents=tuple(agents),
    )


def validate_config(template: TeamTemplate, config: TeamConfig) -> tuple[Finding, ...]:
    """Check structure and template compatibility. Do not read credentials.

    A pattern match can refuse text that looks like a credential. Matching
    text is not stored. The check does not prove that other text has no secret.
    """
    if template.schema_version != "1":
        return (Finding(code="schema", message="This template schema version is not supported."),)
    findings: list[Finding] = []
    if _looks_secret(config.title) or _looks_secret(config.description):
        findings.append(_secret_finding("team"))
    expected = [agent.name for agent in template.agents]
    actual = [agent.name for agent in config.agents]
    if actual != expected:
        findings.append(
            Finding(
                code="roles",
                message="The draft must keep this template's agents, in their required roles.",
            )
        )
        return tuple(findings)
    contracts = {agent.name: agent for agent in template.agents}
    forbidden = set(template.forbidden_tools) | _SIDE_EFFECTS
    for agent in config.agents:
        contract = contracts[agent.name]
        if _looks_secret(agent.purpose) or _looks_secret(agent.instructions):
            findings.append(_secret_finding(agent.name))
        if _looks_secret(agent.model):
            findings.append(_secret_finding(agent.name))
        if agent.provider not in {"openai", "anthropic"}:
            findings.append(
                Finding(code="provider", message=f"{agent.name} uses an unregistered provider.")
            )
        unknown = [tool for tool in agent.tools if tool not in contract.permitted_tools]
        blocked = [tool for tool in agent.tools if tool in forbidden]
        if unknown or blocked:
            findings.append(
                Finding(
                    code="tools",
                    message=(
                        f"{agent.name} can only use this template's permitted tools. "
                        "Approval policy and new endpoints cannot be set from a draft."
                    ),
                )
            )
        missing = [tool for tool in contract.required_tools if tool not in agent.tools]
        if missing:
            findings.append(
                Finding(
                    code="capability",
                    message=f"{agent.name} is missing a tool this template requires.",
                )
            )
    return tuple(findings)


def _secret_finding(owner: str) -> Finding:
    return Finding(
        code="secret",
        message=(
            f"The {owner} configuration looks like it contains a credential. It was not stored."
        ),
    )


def _looks_secret(value: str) -> bool:
    return _SECRET.search(value) is not None
