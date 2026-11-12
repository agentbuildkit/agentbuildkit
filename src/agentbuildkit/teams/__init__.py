"""Declarative agent configuration; execution remains in the existing harness."""

from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Literal, Self

from agent_framework import FunctionTool
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from agentbuildkit.agents import AgentDefinition

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class BundleModel(BaseModel):
    """Reject unknown settings, including attempts to override approval policy."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class BundleAgent(BundleModel):
    """Schema and tool names resolve only against application-owned registries."""

    name: Text
    purpose: Text
    instructions: Text
    provider: Literal["openai", "anthropic"]
    model: Text
    input_schema: Text
    output_schema: Text
    tools: tuple[Text, ...] = ()

    @model_validator(mode="after")
    def unique_tools(self) -> Self:
        if len(set(self.tools)) != len(self.tools):
            raise ValueError("Tool names must be unique within an agent.")
        return self


class TeamBundle(BundleModel):
    """Version one configures agents, without defining a new workflow engine."""

    schema_version: Literal["1"]
    name: Text
    agents: Annotated[tuple[BundleAgent, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def unique_agents(self) -> Self:
        names = [agent.name for agent in self.agents]
        if len(set(names)) != len(names):
            raise ValueError("Agent names must be unique.")
        return self

    def definitions(
        self,
        *,
        schemas: Mapping[str, type[BaseModel]],
        tools: Mapping[str, FunctionTool],
    ) -> tuple[AgentDefinition[BaseModel, BaseModel], ...]:
        """Resolve trusted objects without importing code or constructing clients.

        Pass results to create_agent, which enforces prepared tool policy and
        installs the existing approval and audit middleware.
        """
        definitions: list[AgentDefinition[BaseModel, BaseModel]] = []
        for agent in self.agents:
            if agent.input_schema not in schemas or agent.output_schema not in schemas:
                raise ValueError("Bundle references an unregistered schema.")
            if any(name not in tools for name in agent.tools):
                raise ValueError("Bundle references an unregistered tool.")
            if any(tools[name].name != name for name in agent.tools):
                raise ValueError("Registered tool name does not match its bundle reference.")
            definitions.append(
                AgentDefinition(
                    name=agent.name,
                    purpose=agent.purpose,
                    instructions=agent.instructions,
                    provider=agent.provider,
                    model=agent.model,
                    input_type=schemas[agent.input_schema],
                    output_type=schemas[agent.output_schema],
                    tools=tuple(tools[name] for name in agent.tools),
                )
            )
        return tuple(definitions)


def load_bundle(path: str | Path) -> TeamBundle:
    """Read JSON data only. Validation errors can contain input; do not log them."""
    return TeamBundle.model_validate_json(Path(path).read_bytes())
