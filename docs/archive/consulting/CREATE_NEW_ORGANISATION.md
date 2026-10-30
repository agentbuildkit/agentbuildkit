# Create a new organisation

Use this playbook when a client engagement starts from this template. Keep `src/agent_platform`. Replace `src/organisation`. The fictional customer-service team already in that package is the working example. Remove it when the client implementation is ready.

Complete [CLIENT_DISCOVERY.md](CLIENT_DISCOVERY.md) with the client, then fill [ORGANISATION_SPEC.md](ORGANISATION_SPEC.md). Open the repository in Cursor and give it [NEW_ORGANISATION_PROMPT.md](NEW_ORGANISATION_PROMPT.md) together with those two documents. Before anything is deployed, walk [DEPLOYMENT_CHECKLIST.md](DEPLOYMENT_CHECKLIST.md).

## 1. Understand the business process

Sit with the people who do the work. Write the process as it happens today: trigger, inputs, systems touched, hand-offs, and the decision that ends the case. Record volumes, cost, and the SLA in the discovery template. One process per organisation. If the client has several unrelated processes, split them into separate engagements.

## 2. Identify where AI judgment is required

Mark a step as a model step only when a person currently uses judgment, chooses among tools, or writes language a template cannot produce. Classification, a draft answer grounded in a record, and a quality review are typical model steps.

## 3. Identify deterministic business logic

Mark routing, allowlists, threshold checks, status transitions, and "this write may run" as Python. If the rule can be stated as a field comparison, it does not belong in an instruction prompt. Unknown routes must fail closed through the platform's route rejection.

## 4. Define agents and responsibilities

One agent, one job. Name the role, the decision it owns, and the decisions it must not make. The reference team is four roles: Alex classifies, Sam reads accounts, Taylor reads services and may propose one action, Jordan reviews the draft. A new organisation can have fewer roles. Do not give a specialist the tool that performs another specialist's job.

## 5. Define typed input and output schemas

Add a Pydantic model for every agent input and output, with `frozen=True` and `extra="forbid"`. The output model is the only channel the next Python step may trust. Free-text fields that Python will route on must be an allowlisted value, checked in Python after the model returns.

## 6. Define routing

Choose one platform graph:

- `manager_workflow` when one agent classifies and Python sends the case to an allowlisted specialist.
- `conditional_workflow` when the route is already a field on the input.
- `sequential_workflow` when every case visits the same agents in order.
- `concurrent_workflow` when named tasks run independently and a Python aggregator joins them.

Write the route table in the specification: field, allowed values, destination, and the failure outcome for anything else.

## 7. Identify required business systems and data

List each system, the records the agent may see, and the identifier the caller must supply. For each record, name the response model. If the system does not exist yet, build an in-process `httpx.MockTransport` behind `HttpClient`, as `organisation.business` does, and keep the fixture token out of logs.

## 8. Classify tools as read or side effect

Every tool is `ToolEffect.READ` or `ToolEffect.SIDE_EFFECT`. A GET that does not change the business is a read. A POST, a message send, a booking, or a record update is a side effect. Prepare tools with `prepare_tool`. Attach a tool only to the agent that needs it. Keep the side-effect tool off the specialist and on the approval workflow.

## 9. Determine human approval requirements

Name the person who may approve each side effect, what they must see, and what happens on rejection. The caller re-supplies the arguments at resume time. The store checks the hash. It does not keep the raw arguments. Agree the idempotency rule with the client: a repeated approval will not call the tool again, and a crash after the key is reserved also will not call it again.

## 10. Implement integrations

For HTTP, construct one `HttpClient` per system with a fixed `base_url` and a header provider for the credential. Relative paths only. Reads call the client directly. Writes call `HttpClient.write` with an `ApprovalDecision` after the human gate has invoked the tool.

For MCP, add an `McpServerConfig` to the allowlist. Declare each remote tool's effect. Leave the command, URL, and headers in configuration.

## 11. Create workflows

Implement the composition in `src/organisation`, following `service.py`. Call the platform graph, then apply review and approval in Python. Bind `correlation_id` around the run. Use `sqlite_workflow_store` for any pause that must survive a restart, with a scope string unique to this organisation. Pass that store into `approval_workflow`.

## 12. Build evaluation cases

Add `EvaluationCase` rows for the paths the business cares about. At a minimum cover: the happy path for each route, an unknown route, a forbidden tool, a pause before a side effect, a rejected side effect that does not run, an approved side effect that runs once, a duplicate approval, and a case that asserts secrets are absent from the report. Drive the agents with `ScriptedChatClient`. Function-calling agents need a tool-call response followed by the output model.

## 13. Test offline

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
```

These commands must pass with no API key and no network. Fix the organisation until they do. Leave `src/agent_platform` unchanged unless a platform gap has been accepted in writing.

## 14. Test with a live model

Copy `.env.example` to `.env` and set `OPENAI_API_KEY` locally. Set `OPENAI_CHAT_MODEL` when the client has chosen a model. The reference command uses `gpt-4.1-mini` when that variable is empty.

```bash
uv run python -m organisation
```

Run one request per route, one request that must pause, and one rejection. Confirm the typed status, the specialist, and the correlation id. Confirm a side effect appears in the business system only after approval, and only once. Do not print prompts or the key.

## 15. Package and deploy

```bash
docker build -t agent-platform:local .
docker run --rm --entrypoint python agent-platform:local -m organisation.health
mkdir -p .data
docker run --rm -it \
  --env-file .env \
  -e APP_STATE_DIRECTORY=/data \
  -v "$PWD/.data:/data" \
  agent-platform:local
```

Pass secrets with `--env-file` or the host environment. Mount a durable directory on `/data`. Complete [DEPLOYMENT_CHECKLIST.md](DEPLOYMENT_CHECKLIST.md) before the client uses it.

## 16. Measure business outcomes

Compare the live results with the baseline in the discovery template: cycle time, cost per case, SLA attainment, and the share of cases completed without a person. Keep the offline suite as the regression gate. A drop in pass rate blocks a release. Review the audit log for approval decisions and correlation ids when a case is disputed. Revisit the specification when a metric misses the agreed target.
