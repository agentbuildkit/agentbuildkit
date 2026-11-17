"""Declarative agent configuration; execution remains in the existing harness."""

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Literal, Self

from agent_framework import FunctionTool
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from agentbuildkit.agents import AgentDefinition

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
_TEAM_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


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
    """Version one configures agents, without defining a new workflow engine.

    ``id`` is the stable installation id. ``title`` is the display name. When a
    version-one file omits them, both fall back to ``name`` so existing bundles
    keep their current identity.
    """

    schema_version: Literal["1"]
    name: Text
    id: Text | None = None
    title: Text | None = None
    agents: Annotated[tuple[BundleAgent, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def unique_agents(self) -> Self:
        names = [agent.name for agent in self.agents]
        if len(set(names)) != len(names):
            raise ValueError("Agent names must be unique.")
        chosen = self.id or self.name
        if _TEAM_ID.fullmatch(chosen) is None:
            raise ValueError("Team id must be a lowercase slug.")
        return self

    @property
    def team_id(self) -> str:
        """Stable id used for storage and routes. Independent of the title."""
        return self.id or self.name

    @property
    def display_name(self) -> str:
        """Name shown in the dashboard."""
        return self.title or self.name

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
    return load_bundle_json(Path(path).read_bytes())


def load_bundle_json(data: bytes) -> TeamBundle:
    """Read JSON bytes from a caller. The caller chooses the file or package resource."""
    return TeamBundle.model_validate_json(data)


class RegisteredTeam(BundleModel):
    """One installed team. The id does not change when the title changes."""

    team_id: Text
    title: Text
    bundle_name: Text


class TeamRegistry:
    """The teams installed in one local AgentBuildKit process.

    This is a registry of explicit applications. It does not import modules from
    configuration, and it does not accept uploaded or model-written code.
    """

    def __init__(self) -> None:
        self._records: dict[str, RegisteredTeam] = {}

    def add(self, bundle: TeamBundle) -> RegisteredTeam:
        """Install one validated bundle. A repeated id is rejected."""
        record = RegisteredTeam(
            team_id=bundle.team_id,
            title=bundle.display_name,
            bundle_name=bundle.name,
        )
        if record.team_id in self._records:
            raise ValueError("A team with this id is already installed.")
        self._records[record.team_id] = record
        return record

    def get(self, team_id: str) -> RegisteredTeam | None:
        """Return one installed team, or nothing when the id is unknown."""
        return self._records.get(team_id)

    def teams(self) -> tuple[RegisteredTeam, ...]:
        """Installed teams in id order."""
        return tuple(self._records[key] for key in sorted(self._records))
