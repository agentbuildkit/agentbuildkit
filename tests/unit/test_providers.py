"""Offline tests for per-agent providers and models."""

import json
from pathlib import Path

import pytest
from agent_framework import SecretString
from agent_framework.anthropic import AnthropicClient
from agent_framework.openai import OpenAIChatClient
from pydantic import BaseModel

from agentbuildkit.agents import (
    AgentDefinition,
    AgentDefinitionError,
    create_agent,
)
from agentbuildkit.observability import AuditEvent, AuditLog
from agentbuildkit.providers import (
    ANTHROPIC_PROVIDER_ID,
    OPENAI_PROVIDER_ID,
    AnthropicProvider,
    OpenAIProvider,
    ProviderConfiguration,
    ProviderError,
    ProviderRegistry,
    builtin_registry,
)
from agentbuildkit.testing import ScriptedChatClient
from agentbuildkit.workflows import memory_workflow_store
from agentbuildkit_templates.customer_service import (
    ORGANISATION_SCOPE,
    CustomerRequest,
    handle_request,
)
from agentbuildkit_templates.customer_service.evaluation import LOCAL_MOCK_TOKEN, scripted_clients
from agentbuildkit_templates.customer_service.team import (
    ALEX,
    JORDAN,
    SAM,
    TAYLOR,
    build_team,
    production_clients,
)


class Task(BaseModel):
    task: str


class Report(BaseModel):
    summary: str
    score: int


def _silent(**_kwargs: object) -> None:
    return None


def _report(summary: str, score: int) -> str:
    return json.dumps({"summary": summary, "score": score})


def _definition(
    *,
    name: str = "researcher",
    provider: str = "openai",
    model: str = "model-x",
) -> AgentDefinition[Task, Report]:
    return AgentDefinition(
        name=name,
        purpose="Research a task.",
        instructions="Return the report.",
        input_type=Task,
        output_type=Report,
        provider=provider,
        model=model,
    )


def _distinct_roles() -> dict[str, ProviderConfiguration]:
    return {
        ALEX: ProviderConfiguration(provider=OPENAI_PROVIDER_ID, model="model-a"),
        SAM: ProviderConfiguration(provider=OPENAI_PROVIDER_ID, model="model-b"),
        TAYLOR: ProviderConfiguration(provider=OPENAI_PROVIDER_ID, model="model-c"),
        JORDAN: ProviderConfiguration(provider=OPENAI_PROVIDER_ID, model="model-d"),
    }


class _CatalogProvider:
    """Stand-in adapter used to prove registration is the extension point."""

    def __init__(self, provider_id: str) -> None:
        self._id = provider_id
        self.models: list[str] = []

    @property
    def id(self) -> str:
        return self._id

    def create_client(self, configuration: ProviderConfiguration) -> ScriptedChatClient:
        self.models.append(configuration.model)
        return ScriptedChatClient([_report("ok", 1)])


def test_provider_and_model_belong_to_the_agent() -> None:
    alex = _definition(name="alex", model="model-a")
    sam = _definition(name="sam", provider="openai", model="model-b")

    assert alex.provider == "openai"
    assert alex.model == "model-a"
    assert sam.model == "model-b"
    assert alex.name == "alex"
    assert sam.name == "sam"


def test_blank_provider_and_model_are_rejected() -> None:
    with pytest.raises(AgentDefinitionError, match="Provider id must not be empty"):
        _definition(provider=" ")
    with pytest.raises(AgentDefinitionError, match="Model id must not be empty"):
        _definition(model="")
    with pytest.raises(ProviderError, match="Provider id must not be empty"):
        ProviderConfiguration(provider="", model="model-a")
    with pytest.raises(ProviderError, match="Model id must not be empty"):
        ProviderConfiguration(provider="openai", model=" ")


def test_model_does_not_change_agent_identity() -> None:
    first = _definition(name="researcher", model="model-x")
    second = _definition(name="researcher", model="model-y")

    assert first.name == second.name
    assert first.model != second.model


def test_builtin_providers_resolve_and_unknown_providers_do_not() -> None:
    registry = builtin_registry()

    assert registry.resolve("openai").id == OPENAI_PROVIDER_ID
    assert registry.resolve("anthropic").id == ANTHROPIC_PROVIDER_ID
    with pytest.raises(ProviderError, match="Unknown provider: xyz"):
        registry.create_client(ProviderConfiguration(provider="xyz", model="model-z"))
    with pytest.raises(ProviderError, match="Provider id must not be empty"):
        registry.resolve(" ")


def test_registration_does_not_overwrite_a_provider() -> None:
    registry = builtin_registry()

    with pytest.raises(ProviderError, match="already registered"):
        registry.register(OpenAIProvider())
    with pytest.raises(ProviderError, match="already registered"):
        registry.register(AnthropicProvider())


def test_a_new_provider_is_added_by_registration() -> None:
    registry = ProviderRegistry()
    added = _CatalogProvider("local")
    registry.register(added)

    client = registry.create_client(ProviderConfiguration(provider="local", model="local-model"))

    assert isinstance(client, ScriptedChatClient)
    assert added.models == ["local-model"]
    with pytest.raises(ProviderError, match="already registered"):
        registry.register(_CatalogProvider("local"))


def test_openai_provider_uses_the_agent_model_without_a_network_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def refuse_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("The OpenAI provider made a network call.")

    monkeypatch.setattr("socket.create_connection", refuse_network)
    env_file = tmp_path / "openai.env"
    env_file.write_text(
        "OPENAI_API_KEY=local-test-key\nOPENAI_CHAT_MODEL=env-model\n",
        encoding="utf-8",
    )

    client = OpenAIProvider(env_file_path=str(env_file)).create_client(
        ProviderConfiguration(provider="openai", model="model-a")
    )

    assert isinstance(client, OpenAIChatClient)
    assert client.model == "model-a"


def test_openai_errors_do_not_reveal_credentials(tmp_path: Path) -> None:
    env_file = tmp_path / "openai.env"
    env_file.write_text("OPENAI_API_KEY=sk-super-secret\n", encoding="utf-8")
    provider = OpenAIProvider(env_file_path=str(env_file))

    with pytest.raises(ProviderError, match="Unknown provider: xyz") as caught:
        provider.create_client(ProviderConfiguration(provider="xyz", model="model-z"))

    assert "sk-super-secret" not in str(caught.value)


def test_four_roles_receive_four_model_ids(tmp_path: Path) -> None:
    env_file = tmp_path / "openai.env"
    env_file.write_text("OPENAI_API_KEY=local-test-key\n", encoding="utf-8")
    roles = _distinct_roles()
    clients = production_clients(env_file_path=str(env_file), roles=roles)

    expected = {
        ALEX: "model-a",
        SAM: "model-b",
        TAYLOR: "model-c",
        JORDAN: "model-d",
    }
    assert len({id(client) for client in clients.values()}) == 4
    for name, model in expected.items():
        client = clients[name]
        assert isinstance(client, OpenAIChatClient)
        assert client.model == model


async def test_injected_clients_keep_declared_models_and_skip_openai(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def refuse_client(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("OpenAIChatClient should not be constructed")

    monkeypatch.setattr("agentbuildkit.providers.openai.OpenAIChatClient", refuse_client)
    roles = _distinct_roles()
    organisation = build_team(
        clients=scripted_clients("billing-routes-to-sam"),
        api_token=SecretString(LOCAL_MOCK_TOKEN),
        configurator=_silent,
        roles=roles,
    )
    try:
        expected = (
            (organisation.alex, "alex", "model-a"),
            (organisation.sam, "sam", "model-b"),
            (organisation.taylor, "taylor", "model-c"),
            (organisation.jordan, "jordan", "model-d"),
        )
        for agent, name, model in expected:
            assert agent.definition.name == name
            assert agent.definition.provider == OPENAI_PROVIDER_ID
            assert agent.definition.model == model
            assert agent.agent.default_options is not None
            assert agent.agent.default_options["model"] == model
    finally:
        await organisation.aclose()

    env_file = tmp_path / "openai.env"
    env_file.write_text("OPENAI_API_KEY=local-test-key\n", encoding="utf-8")
    with pytest.raises(AssertionError, match="OpenAIChatClient"):
        production_clients(env_file_path=str(env_file))


async def test_agent_run_records_provider_and_model_without_secrets() -> None:
    secret = "sk-provider-secret"
    audit_log = AuditLog()
    agent = create_agent(
        _definition(name="researcher", model="model-x"),
        client=ScriptedChatClient([_report("done", 1)]),
        audit_log=audit_log,
    )

    await agent.run(Task(task=secret), correlation_id="provider-run")

    event = audit_log.events[-1]
    rendered = json.dumps(event.model_dump())
    assert event.argument_hash == "agent-run"
    assert event.agent_name == "researcher"
    assert event.provider == "openai"
    assert event.model == "model-x"
    assert secret not in rendered
    assert "local-test-key" not in rendered


def _mixed_roles() -> dict[str, ProviderConfiguration]:
    return {
        ALEX: ProviderConfiguration(provider=OPENAI_PROVIDER_ID, model="openai-model-a"),
        SAM: ProviderConfiguration(provider=OPENAI_PROVIDER_ID, model="openai-model-b"),
        TAYLOR: ProviderConfiguration(provider=ANTHROPIC_PROVIDER_ID, model="anthropic-model-a"),
        JORDAN: ProviderConfiguration(provider=ANTHROPIC_PROVIDER_ID, model="anthropic-model-b"),
    }


def _agent_runs(events: list[AuditEvent], name: str) -> list[AuditEvent]:
    return [
        event for event in events if event.agent_name == name and event.argument_hash == "agent-run"
    ]


_PROVIDER_ENV = (
    "OPENAI_API_KEY",
    "OPENAI_CHAT_MODEL",
    "OPENAI_MODEL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_CHAT_MODEL",
    "ANTHROPIC_BASE_URL",
)


@pytest.fixture(autouse=True)
def _clear_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _PROVIDER_ENV:
        monkeypatch.delenv(key, raising=False)


def _refuse_network(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("A provider made a network call.")


def test_anthropic_provider_uses_the_agent_model_without_a_network_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("socket.create_connection", _refuse_network)
    env_file = tmp_path / "anthropic.env"
    secret = "sk-ant-super-secret"
    env_file.write_text(
        f"ANTHROPIC_API_KEY={secret}\nANTHROPIC_CHAT_MODEL=env-model\n",
        encoding="utf-8",
    )

    client = AnthropicProvider(env_file_path=str(env_file)).create_client(
        ProviderConfiguration(provider="anthropic", model="anthropic-model-a")
    )

    rendered = repr(client)
    assert isinstance(client, AnthropicClient)
    assert client.model == "anthropic-model-a"
    assert secret not in rendered
    assert secret not in str(client)


def test_anthropic_missing_credential_does_not_reveal_a_secret(tmp_path: Path) -> None:
    env_file = tmp_path / "anthropic.env"
    env_file.write_text("ANTHROPIC_CHAT_MODEL=sk-ant-should-not-appear\n", encoding="utf-8")

    with pytest.raises(ProviderError, match="ANTHROPIC_API_KEY is not configured") as caught:
        AnthropicProvider(env_file_path=str(env_file)).create_client(
            ProviderConfiguration(provider="anthropic", model="anthropic-model-a")
        )

    assert "sk-ant" not in str(caught.value)


def test_environment_chat_models_do_not_select_agent_models(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("socket.create_connection", _refuse_network)
    monkeypatch.setenv("OPENAI_CHAT_MODEL", "global-openai-model")
    monkeypatch.setenv("ANTHROPIC_CHAT_MODEL", "global-anthropic-model")
    env_file = tmp_path / "providers.env"
    env_file.write_text(
        "\n".join(
            [
                "OPENAI_API_KEY=local-openai-key",
                "OPENAI_CHAT_MODEL=global-openai-model",
                "ANTHROPIC_API_KEY=sk-ant-local-key",
                "ANTHROPIC_CHAT_MODEL=global-anthropic-model",
            ]
        ),
        encoding="utf-8",
    )
    registry = builtin_registry(env_file_path=str(env_file))

    openai_client = registry.create_client(
        ProviderConfiguration(provider="openai", model="openai-model-a")
    )
    anthropic_client = registry.create_client(
        ProviderConfiguration(provider="anthropic", model="anthropic-model-a")
    )

    assert isinstance(openai_client, OpenAIChatClient)
    assert isinstance(anthropic_client, AnthropicClient)
    assert openai_client.model == "openai-model-a"
    assert anthropic_client.model == "anthropic-model-a"
    assert "local-openai-key" not in repr(openai_client)
    assert "sk-ant-local-key" not in repr(anthropic_client)


def test_default_example_does_not_construct_anthropic(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def refuse_anthropic(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("AnthropicClient should not be constructed")

    monkeypatch.setattr("agentbuildkit.providers.anthropic.AnthropicClient", refuse_anthropic)
    monkeypatch.setattr("socket.create_connection", _refuse_network)
    env_file = tmp_path / "openai.env"
    env_file.write_text("OPENAI_API_KEY=local-test-key\n", encoding="utf-8")

    clients = production_clients(env_file_path=str(env_file))

    assert len(clients) == 4
    assert all(isinstance(client, OpenAIChatClient) for client in clients.values())


def test_mixed_provider_clients_keep_each_model(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("socket.create_connection", _refuse_network)
    env_file = tmp_path / "providers.env"
    env_file.write_text(
        "OPENAI_API_KEY=local-test-key\nANTHROPIC_API_KEY=sk-ant-local-key\n",
        encoding="utf-8",
    )

    clients = production_clients(env_file_path=str(env_file), roles=_mixed_roles())

    alex = clients[ALEX]
    sam = clients[SAM]
    taylor = clients[TAYLOR]
    jordan = clients[JORDAN]
    assert isinstance(alex, OpenAIChatClient)
    assert isinstance(sam, OpenAIChatClient)
    assert isinstance(taylor, AnthropicClient)
    assert isinstance(jordan, AnthropicClient)
    assert alex.model == "openai-model-a"
    assert sam.model == "openai-model-b"
    assert taylor.model == "anthropic-model-a"
    assert jordan.model == "anthropic-model-b"
    rendered = " ".join(repr(client) for client in clients.values())
    assert "local-test-key" not in rendered
    assert "sk-ant-local-key" not in rendered


async def test_mixed_provider_team_runs_one_workflow_with_scripted_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse_client(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("A live provider client should not be constructed")

    monkeypatch.setattr("agentbuildkit.providers.openai.OpenAIChatClient", refuse_client)
    monkeypatch.setattr("agentbuildkit.providers.anthropic.AnthropicClient", refuse_client)
    roles = _mixed_roles()
    organisation = build_team(
        clients=scripted_clients("billing-routes-to-sam"),
        api_token=SecretString(LOCAL_MOCK_TOKEN),
        configurator=_silent,
        roles=roles,
    )
    service = build_team(
        clients=scripted_clients("service-routes-to-taylor"),
        api_token=SecretString(LOCAL_MOCK_TOKEN),
        configurator=_silent,
        roles=roles,
    )
    try:
        empty_tools: set[str] = set()
        expected = (
            (organisation.alex, "alex", "openai", "openai-model-a", "You are Alex", empty_tools),
            (
                organisation.sam,
                "sam",
                "openai",
                "openai-model-b",
                "You are Sam",
                {"read_account"},
            ),
            (
                organisation.taylor,
                "taylor",
                "anthropic",
                "anthropic-model-a",
                "You are Taylor",
                {"read_service"},
            ),
            (
                organisation.jordan,
                "jordan",
                "anthropic",
                "anthropic-model-b",
                "You are Jordan",
                empty_tools,
            ),
        )
        for agent, name, provider, model, instructions, tools in expected:
            assert agent.definition.name == name
            assert agent.definition.provider == provider
            assert agent.definition.model == model
            assert instructions in agent.definition.instructions
            assert {tool.name for tool in agent.definition.tools} == tools
            assert agent.agent.default_options is not None
            assert agent.agent.default_options["model"] == model

        billing = await handle_request(
            organisation,
            CustomerRequest(reference="A-100", message="Please explain the latest invoice."),
            correlation_id="mixed-billing",
            store=memory_workflow_store(ORGANISATION_SCOPE, organisation.audit_log),
        )
        service_result = await handle_request(
            service,
            CustomerRequest(reference="A-100", message="The loading dock door is stuck."),
            correlation_id="mixed-service",
            store=memory_workflow_store(ORGANISATION_SCOPE, service.audit_log),
        )
    finally:
        await organisation.aclose()
        await service.aclose()

    assert billing.outcome.status == "completed"
    assert billing.outcome.specialist == "sam"
    assert service_result.outcome.status == "completed"
    assert service_result.outcome.specialist == "taylor"
    _assert_recorded(organisation.audit_log, "alex", "openai", "openai-model-a")
    _assert_recorded(organisation.audit_log, "sam", "openai", "openai-model-b")
    _assert_recorded(organisation.audit_log, "jordan", "anthropic", "anthropic-model-b")
    assert _agent_runs(organisation.audit_log.events, "taylor") == []
    _assert_recorded(service.audit_log, "alex", "openai", "openai-model-a")
    _assert_recorded(service.audit_log, "taylor", "anthropic", "anthropic-model-a")
    _assert_recorded(service.audit_log, "jordan", "anthropic", "anthropic-model-b")
    rendered = json.dumps(
        [event.model_dump() for event in organisation.audit_log.events]
        + [event.model_dump() for event in service.audit_log.events]
    )
    assert "local-test-key" not in rendered
    assert "sk-ant" not in rendered
    assert LOCAL_MOCK_TOKEN not in rendered


def _assert_recorded(audit_log: AuditLog, name: str, provider: str, model: str) -> None:
    runs = _agent_runs(audit_log.events, name)
    assert runs
    assert all(event.provider == provider and event.model == model for event in runs)
