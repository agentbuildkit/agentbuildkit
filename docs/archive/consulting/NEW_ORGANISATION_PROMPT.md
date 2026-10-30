# New organisation prompt

Create a repository from this template, fill [CLIENT_DISCOVERY.md](CLIENT_DISCOVERY.md) and [ORGANISATION_SPEC.md](ORGANISATION_SPEC.md), then paste the prompt below into Cursor with those two files.

---

Implement the client organisation described in `ORGANISATION_SPEC.md` and `CLIENT_DISCOVERY.md`.

Work only in `src/organisation` and in tests that belong to that organisation. `src/agent_platform` is shared platform IP. Do not modify it, do not add packages, and do not add a new runtime abstraction.

If the specification cannot be implemented with the existing platform API, stop before editing any platform file. Describe the gap, the platform function you expected, and why an organisation-level change is insufficient. Wait for an explicit acceptance of that change. A justification after the edit is not acceptable.

Use the platform as it exists:

- Declare agents with `AgentDefinition` and build them with `create_agent`. Inputs and outputs are Pydantic models.
- Pass `ScriptedChatClient` in tests. Keep a `production_clients` function that builds one shared `OpenAIChatClient` from the existing OpenAI settings.
- Route with `manager_workflow`, `conditional_workflow`, `sequential_workflow`, or `concurrent_workflow`. Unknown routes fail closed.
- Mark tools with `prepare_tool` as `ToolEffect.READ` or `ToolEffect.SIDE_EFFECT`. Give each agent only the tools named in the specification.
- Call HTTP through `HttpClient` with a fixed base URL and a header provider. Call MCP only through an allowlisted `McpServerConfig`.
- Pause side effects on `approval_workflow` and resume with `resume_approval`. Persist pauses with `sqlite_workflow_store` under `APP_STATE_DIRECTORY`.
- Bind a correlation id around each run. Do not log secrets, prompts, raw model responses, or raw tool arguments.
- Add `EvaluationCase` coverage for every route, an unknown route, a forbidden tool, pause, rejection, one approved execution, a duplicate approval, and secrets absent from the report.

The fictional customer-service organisation is the example. Replace it. Do not add a web UI, a REST API, authentication, PostgreSQL, Kubernetes, Azure, or a new agent framework.

Before you finish, run:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
```

Do not call the OpenAI API. Report the files you changed, any platform change you refused to make, and the offline test result.
