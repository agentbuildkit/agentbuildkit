# Customer-service example

This package demonstrates an agent team built with AgentBuildKit. It is fixture data, not a connection to a CRM or a live customer-service backend.

- Alex classifies a typed request.
- Python routes that decision to Sam or Taylor, and rejects any other name.
- Sam reads account data. Taylor reads service data and may propose one action.
- Jordan approves, rejects, or escalates the draft.
- An informational answer is returned immediately.
- A service change pauses for a person, is stored, and runs once after approval.

The business system is an in-process HTTP mock at `http://business.test`. It does not call a network. `LOCAL_MOCK_TOKEN` is a fixture marker. The only customer reference is `A-100`, with fixture account name Northwind.

Each role declares its own provider and model. The runnable default uses `openai` and `gpt-4.1-mini` for every role, so a live run needs `OPENAI_API_KEY` only. `production_clients()` asks the provider registry for one client per role. Offline tests pass a `ScriptedChatClient` per role to `build_team(clients=...)` and do not construct a live client.

The default team asks the registry for an OpenAI client per role:

```python
from customer_service import build_team, handle_request, production_clients

team = build_team(clients=production_clients(), api_token=token)
```

Switch Taylor to Anthropic without changing the workflow. Sam stays on OpenAI. That live client also needs `ANTHROPIC_API_KEY`.

```python
from agentbuildkit.providers import ProviderConfiguration
from customer_service import build_team, production_clients

roles = {"taylor": ProviderConfiguration(provider="anthropic", model="claude-sonnet-4-5")}
team = build_team(clients=production_clients(roles=roles), api_token=token)
```

A local live run:

```bash
PYTHONPATH=examples uv run python -m customer_service
```

A live run of the default team needs `OPENAI_API_KEY`. The model for each role is the model on that agent's definition.
