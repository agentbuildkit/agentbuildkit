# Reference organisation

This directory is the client-specific part of the starter. The platform in
`agent_platform` stays reusable. This reference is a fictional B2B customer
service team:

- Alex classifies a typed request.
- Python routes that decision to Sam or Taylor, and rejects any other name.
- Sam reads account data. Taylor reads service data and may propose one action.
- Jordan approves, rejects, or escalates the draft.
- An informational answer is returned immediately.
- A service change pauses for a person, is stored, and runs once after approval.

The business system is an in-process HTTP mock. It does not call a network or
use a real credential. `LOCAL_MOCK_TOKEN` is a fixture marker for that mock.

Offline tests pass a `ScriptedChatClient` per role to `build_team`. A deployment
can pass `production_clients()`, which builds one `OpenAIChatClient` from
OpenAI settings, without changing the agents or the workflow.

```python
from organisation import build_team, handle_request, production_clients

team = build_team(clients=production_clients(), api_token=token)
```

A local live run uses that same client path:

```bash
uv run python -m organisation
```

`OPENAI_CHAT_MODEL` selects the model. When it is unset, the command uses
`gpt-4.1-mini`. The mock business system only has customer reference `A-100`.
