# Organisation specification

Fill this file from [CLIENT_DISCOVERY.md](CLIENT_DISCOVERY.md). An engineer implements `src/organisation` from this document and leaves `src/agent_platform` unchanged. The worked example at the bottom is the fictional reference organisation. Replace that example with the client.

## Engagement

- Client:
- Process:
- Scope name (used as the SQLite scope):
- Replace reference organisation: yes

## Agents

Repeat this block for each agent.

### Agent

- Name:
- Purpose:
- Instructions (the exact system text):
- Input schema (fields and types):
- Output schema (fields and types):
- Tools this agent may call:
- Tools this agent must not call:
- Model judgment this agent is allowed to make:
- Decisions Python will make from the output:

## Tools

| Tool name | Effect (`read` or `side_effect`) | System | Operation | Request schema | Response schema | Held by |
| --- | --- | --- | --- | --- | --- | --- |
|  |  |  |  |  |  |  |

## Routes

- Workflow name:
- Graph (`manager_workflow`, `conditional_workflow`, `sequential_workflow`, or `concurrent_workflow`):
- Route field:
- Allowed values and destination agent:
- Outcome when the value is not allowlisted:

## Workflows

- Entry input:
- Steps after the graph, in Python:
- Final outcome schema and status values:
- Correlation id source:

## Approvals

| Workflow name | Tool | Arguments the approver sees | Approve outcome | Reject outcome | Store |
| --- | --- | --- | --- | --- | --- |
|  |  |  |  |  | `sqlite` under `APP_STATE_DIRECTORY` |

- Idempotency: at-most-once inside the workflow store for workflow name, correlation id, request id, and action.

## Integrations

| Name | Kind | Base URL or MCP server | Credential setting | Header or transport | Mock available for offline tests |
| --- | --- | --- | --- | --- | --- |
|  |  |  |  |  |  |

## Persistence

- Scope:
- Database file: `workflows.sqlite` inside `APP_STATE_DIRECTORY`
- Pauses that must survive a process restart:
- Fields that must not be stored: prompts, credentials, raw model responses, raw approval arguments

## Evaluation cases

| Case name | Route | Expected status | Agents | Tools required | Tools forbidden | Approval | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
|  |  |  |  |  |  |  |  |

Required rows: one happy path per route, unknown route, forbidden tool, pause, rejected side effect, approved side effect once, duplicate approval, secrets absent from the report.

## Acceptance criteria

- Offline `ruff`, `ruff format`, `pyright`, and `pytest` pass with no API key.
- Each evaluation case passes on `ScriptedChatClient`.
- A live smoke test, run by a person, covers each route, one approval, and one rejection.
- A side effect is visible in the business system only after approval, and only once.
- A mounted data directory contains the SQLite file and does not contain prompts, credentials, or raw arguments.
- The business owner has signed the deployment checklist.

---

## Worked example: reference customer-service organisation

This example is fictional. Replace it for a client deployment.

- Scope: `customer-service`
- Process: answer an account or service question, and pause before a service change.

### Agents

| Name | Purpose | Input | Output | Tools |
| --- | --- | --- | --- | --- |
| alex | Classify billing versus service | `CustomerRequest` | `RouteDecision` | none |
| sam | Answer from the account record | `RouteDecision` | `ProposedResponse` | `read_account` |
| taylor | Answer from the service record and propose at most one action | `RouteDecision` | `ProposedResponse` | `read_service` |
| jordan | Approve, reject, or escalate the draft | `ProposedResponse` | `QualityDecision` | none |

Alex may name only `sam` or `taylor`. Python routes on `specialist`. Jordan's `verdict` is applied in Python. Taylor's `needs_action` does not execute the action.

### Tools

| Tool | Effect | Operation |
| --- | --- | --- |
| `read_account` | read | GET `/accounts` |
| `read_service` | read | GET `/services` |
| `schedule_service` | side_effect | POST `/service-actions` |

`schedule_service` is held by the `service-action` approval workflow. Sam and Taylor do not have it.

### Routes and workflows

- `customer-route` uses `manager_workflow`. `sam` and `taylor` are the only destinations. Any other specialist becomes `route_rejected`.
- Python then runs Jordan. `approve` with no action completes. `reject` completes as rejected. `escalate` completes as escalated.
- `needs_action` with an action name opens `service-action`. The caller must approve before `schedule_service` runs.

### Integrations and persistence

The business system is an in-process mock at `http://business.test`. The only customer reference is `A-100`. `LOCAL_MOCK_TOKEN` is a fixture marker. Durable pauses use scope `customer-service` and `workflows.sqlite` when `APP_STATE_DIRECTORY` is set.

### Evaluation cases

`billing-routes-to-sam`, `service-routes-to-taylor`, `unknown-route-fails-safely`, `jordan-rejects-poor-response`, `informational-completes-without-approval`, `service-action-pauses-for-approval`, `rejected-service-action-is-never-executed`, `approved-service-action-executes-once`, `duplicate-approval-does-not-duplicate`, `specialist-cannot-use-other-tools`, `secrets-stay-out-of-audit-and-evaluation`, `correlation-survives-the-organisation`, `escalation-does-not-execute`.

### Acceptance

The offline suite passes, including the organisation evaluation at pass rate 1.0. A person has run one billing request and one approved service change against OpenAI. The service change paused, resumed once, and completed.
