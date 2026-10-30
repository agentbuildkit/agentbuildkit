# Agent instructions

This repository is AgentBuildKit, a Python harness for reliable AI agents on Microsoft Agent Framework 1.x. Follow these rules for every change.

## Framework

- Microsoft Agent Framework is the only agent framework. Use `agent_framework` and its first-party packages.
- Each agent declares `provider` and `model`. The OpenAI provider constructs `OpenAIChatClient`. The Anthropic provider constructs `AnthropicClient`. Agent execution receives an injected client and does not branch on the provider.
- Microsoft Foundry stays an optional extra (`uv sync --extra foundry`). Do not add it to the default dependencies.
- Leave AutoGen, Semantic Kernel, and the experimental functional `@workflow` API out of this repository.
- Prefer stable graph workflows (`WorkflowBuilder`) and the orchestration builders in `agent_framework.orchestrations`.

## Design

- Prefer deterministic Python where the step does not need model judgment. Use an agent for judgment, tool selection, or language generation.
- Agent and workflow inputs and outputs use Pydantic models.
- Reusable harness code belongs in `src/agentbuildkit`. The customer-service demonstration belongs in `examples/customer_service`. Do not add customer-service concepts to the harness.
- New behaviour ships with a test that passes without a live model. Mark live-service tests with `@pytest.mark.integration`.

## Side effects, secrets, and audit

- External side effects require an allowlisted tool and human approval before they run. Use `@tool(approval_mode="always_require")` for writes and other side effects.
- MCP servers and HTTP APIs come from an explicit allowlist. Pass credentials through a header provider or client configuration, never through model-supplied tool arguments.
- Important actions are observable and auditable. Export OpenTelemetry traces and record approval decisions. Keep sensitive prompt and tool payloads out of shared telemetry.
- Never commit secrets. `.env` is local. Update `.env.example` with empty placeholders when adding a setting.
- Do not log or print `SecretString` values. Call `get_secret_value()` only at the client boundary that requires a plain string.

## Checks

Before handing work back, run:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
```
