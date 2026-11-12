"""Bundles stay data-only and use the existing guarded execution boundary."""

import json
from pathlib import Path

import pytest
from agent_framework import ChatResponse, Content, Message
from pydantic import BaseModel, ValidationError

from agentbuildkit.agents import AgentApprovalRequired, create_agent
from agentbuildkit.cli import run
from agentbuildkit.guardrails import ToolEffect, prepare_tool
from agentbuildkit.teams import TeamBundle, load_bundle
from agentbuildkit.testing import ScriptedChatClient


class Payload(BaseModel):
    text: str


def bundle_data() -> dict[str, object]:
    return {
        "schema_version": "1",
        "name": "example",
        "agents": [
            {
                "name": "writer",
                "purpose": "Write",
                "instructions": "Return JSON",
                "provider": "openai",
                "model": "offline",
                "input_schema": "payload",
                "output_schema": "payload",
                "tools": ["send"],
            }
        ],
    }


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": "2"},
        {"agents": []},
        {"approval_mode": "never_require"},
        {"name": " "},
        {"module": "os.system"},
    ],
)
def test_rejects_unsupported_configuration(change: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        TeamBundle.model_validate(bundle_data() | change)


def test_duplicate_agents_rejected() -> None:
    bundle = TeamBundle.model_validate(bundle_data())
    data = bundle.model_dump()
    data["agents"] = [data["agents"][0], data["agents"][0]]
    with pytest.raises(ValidationError):
        TeamBundle.model_validate(data)


def test_unknown_bindings_rejected() -> None:
    bundle = TeamBundle.model_validate(bundle_data())
    with pytest.raises(ValueError, match="schema"):
        bundle.definitions(schemas={}, tools={})
    with pytest.raises(ValueError, match="tool"):
        bundle.definitions(schemas={"payload": Payload}, tools={})


async def test_bundle_side_effect_still_pauses_for_approval() -> None:
    effects: list[str] = []

    def send(text: str) -> str:
        """Send text."""
        effects.append(text)
        return "sent"

    prepared = prepare_tool(send, effect=ToolEffect.SIDE_EFFECT)
    bundle = TeamBundle.model_validate(bundle_data())
    definition = bundle.definitions(schemas={"payload": Payload}, tools={"send": prepared})[0]
    client = ScriptedChatClient(
        [
            ChatResponse(
                messages=[
                    Message(
                        role="assistant",
                        contents=[
                            Content.from_function_call(
                                call_id="send-1", name="send", arguments={"text": "hello"}
                            )
                        ],
                    )
                ]
            )
        ]
    )
    agent = create_agent(definition, client=client)
    with pytest.raises(AgentApprovalRequired):
        await agent.run(Payload(text="hello"))
    assert effects == []
    assert definition.tools[0] is prepared


def test_cli_validation_and_safe_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "team.json"
    path.write_text(json.dumps(bundle_data()))
    assert load_bundle(path).name == "example"
    assert run(["team", "validate", str(path)]) == 0
    assert "Runtime bindings have not been checked" in capsys.readouterr().out
    path.write_text('{"secret": "sensitive-value"}')
    assert run(["team", "validate", str(path)]) == 1
    assert "sensitive-value" not in capsys.readouterr().err
