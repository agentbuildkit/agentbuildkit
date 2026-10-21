# Implementation plan — Organisation 001, Travelodge App

This document is the plan. It does not authorise code changes to `src/organisation` or `src/agent_platform`. The structure to implement later is [ORGANISATION_SPEC.md](ORGANISATION_SPEC.md). The business input is [CLIENT_DISCOVERY.md](CLIENT_DISCOVERY.md).

## 1. What the repository is

`src/agent_platform` is shared platform IP. `src/organisation` is the replaceable client package. The package that is in the repository today is a fictional B2B customer-service team (Alex, Sam, Taylor, Jordan) used to show the pattern. Microsoft Agent Framework 1.x is the only agent framework. `OpenAIChatClient` is the live chat client. Foundry is an optional extra and stays out of the default dependencies.

The platform provides:

- `AgentDefinition` and `create_agent`, which check the input model, call the chat client, and parse the output model.
- Four graphs: `manager_workflow`, `conditional_workflow`, `sequential_workflow`, `concurrent_workflow`. Unknown switch values raise `RouteRejected`.
- `prepare_tool` with `ToolEffect.READ` or `ToolEffect.SIDE_EFFECT`. A side effect uses `approval_mode="always_require"`. `execute_tool` blocks a write until an `ApprovalDecision` matches.
- `approval_workflow` and `resume_approval`, with at-most-once behaviour inside the workflow store for workflow name, correlation id, request id, and action.
- `sqlite_workflow_store` under `APP_STATE_DIRECTORY`, scope string unique to the organisation. Checkpoints store hashes and identifiers.
- `HttpClient` for one fixed base URL, credentials from a header provider, relative paths only.
- `McpServerConfig` for an allowlisted MCP server. Organisation 001 does not use MCP.
- `bind_correlation_id`, audit events, and OpenTelemetry settings that omit prompts and secrets unless a caller deliberately enables sensitive data.
- `EvaluationCase` and `run_evaluation`, driven by `ScriptedChatClient`, with optional `minimum_scores` supplied by the organisation's score provider. The runner does not call a model to score.

`AgentDefinition` carries name, purpose, instructions, input type, output type, tools, and an optional model id. A run cannot add tools.

Python routes, validates, and decides whether a write may start. The model does not choose credentials, URLs, or permission to write.

## 2. How a new organisation is supposed to be created

The playbook in `CREATE_NEW_ORGANISATION.md`, `NEW_ORGANISATION_PROMPT.md`, and `ORGANISATION_SPEC.md` is the procedure.

1. Keep `src/agent_platform`. Replace `src/organisation` when the client implementation is ready. Remove the fictional team at that moment, not before.
2. Fill client discovery, then fill the organisation specification. An engineer implements from the specification.
3. One process per organisation. Unrelated processes become separate engagements.
4. Use an agent where a person currently uses judgment, chooses among that agent's tools, or writes language a template cannot produce. Use Python for routing, allowlists, thresholds, status transitions, and "this write may run".
5. One agent, one job. The agent does not receive another specialist's tool.
6. Every agent input and output is a frozen Pydantic model with `extra="forbid"`. Python trusts the output model. A field Python routes on is an allowlisted value, checked in Python after the model returns.
7. Pick a platform graph whose shape matches the process. Compose review and approval in Python after the graph, as `service.py` does today.
8. Systems that are not live in tests get an in-process `HttpClient` mock, as `organisation.business` does. The fixture token stays out of logs.
9. Classify every tool read or side effect. The side-effect tool is held by the approval workflow, not by the specialist.
10. Name the human approver, what they see, and the rejection outcome. The caller re-supplies arguments at resume. The store checks the hash and does not keep the raw arguments.
11. Bind a correlation id around the run. Persist pauses with `sqlite_workflow_store` and a scope unique to this organisation.
12. Add evaluation cases for each happy route, an unknown route, a forbidden tool, a pause, a rejected side effect, one approved execution, a duplicate approval, and secrets absent from the report.
13. Pass `uv run ruff check .`, `uv run ruff format --check .`, `uv run pyright`, and `uv run pytest` with no API key and no network.
14. A person runs the live model later. That command is not part of agent implementation.
15. Walk `DEPLOYMENT_CHECKLIST.md` before any client use.
16. If the specification cannot be implemented with the existing platform API, stop before editing a platform file. Describe the gap, the platform function that was expected, and why an organisation-level change is insufficient. A justification after the edit is not acceptable.

Organisation 001 is specified so that step 16 does not fire. The gaps in section 4 are absorbed in `src/organisation` or accepted as scope boundaries. None of them require a platform change to create this organisation as one delivery process.

## 3. Scope chosen for Travelodge App

Travelodge App is the agentic software organisation for the Travelodge native mobile application. This template can instantiate that organisation as **one process**:

> Deliver one allowlisted GitHub work item for `travelodge/mobile-app` to a single GitHub delivery record, after typed team verdicts and one human approval.

GitHub is the source of truth for the issue and for the delivery record. Agent instructions, chat, and the audit log are not the record of the decision. The audit log is the record of the run: correlation id, agent, tool, approval state, outcome, and an argument hash.

The mobile application is not built. The platform is not modified. Dependencies are not added.

Nine teams each have one agent. That matches "one agent, one job" and keeps the evaluation suite able to name every participant. Splitting mobile into separate iOS and Android agents, or adding a second process for store release, is a later specification. It is not required to instantiate organisation 001.

## 4. Gaps

Each gap is something the platform or the playbook does not provide. The response is what organisation 001 will do. No row authorises an edit under `src/agent_platform`.

| Id | Gap | Effect if ignored | Response in organisation 001 |
| --- | --- | --- | --- |
| G1 | The playbook admits one process per organisation. | Design, build, test, release, and continuous improvement as five always-on processes would be five engagements, or an unimplementable specification. | One process: governed delivery of one work item. Improvement is re-running the offline suite and comparing the pass rate. |
| G2 | `AgentDefinition` has no manager, knowledge, objectives, permissions, or scorecard field. | Those required attributes would be prose only, and the runtime would not notice drift. | Frozen charter models in `src/organisation` hold them. Tests assert that each built agent's name, purpose, instructions, and tools equal the charter. The model is not given the charter as a tool. |
| G3 | Human approval is `approval_workflow` around one side-effect tool. A team sign-off with no write has no pause primitive. Checkpoints do not store argument values. There is no approval timeout. | Per-team click-through that survives a restart, with nothing written to GitHub, cannot be expressed. A 5-day SLA cannot be enforced by the store. | Team gates are typed verdicts applied in Python. The only durable pause is `record_delivery`. The business SLA is a human follow-up. Intermediate GitHub checkpoint writes are deferred so the first organisation has one side effect. |
| G4 | `conditional_workflow` and `manager_workflow` end at one specialist. `concurrent_workflow` requires the task keys to equal the agent names exactly. `sequential_workflow` requires each output to validate as the next input. Graphs do not nest. `max_iterations` is 20. Group chat, handoff, and Magentic are excluded on purpose. | A nine-stage branched company cannot be one graph, and a mobile-only defect cannot drop the backend agent from a shared concurrent panel. | Python composes stages in the organisation service, which is how the reference applies Jordan after the manager graph. Feature and defect always run both implementation agents; "unchanged" is a typed output. Release skips that panel in Python. Unknown `change_class` or repository returns `route_rejected` and runs no agent. |
| G5 | There is no knowledge store, repository index, or document retriever. | Agents cannot browse the Travelodge codebase or invent a search index. | Knowledge is the instruction string plus one read tool whose Python path is fixed. The tool accepts an issue id. It does not accept a URL, a path, or a repository. |
| G6 | The template has no GitHub client and no GitHub token setting. | A live GitHub integration is not already present. | `HttpClient` named `github-api`, base URL `https://api.github.com`, header provider for `GITHUB_API_TOKEN`, mock transport in tests. MCP is not added. No new package. |
| G7 | Tool assignment is the permission mechanism. There is no RBAC service. | A permission matrix in a document would not bind the agent. | Charter permissions are the tool tuple, the forbidden-tool tuple, and the Python rule that no agent holds `record_delivery`. The forbidden-tool evaluation case proves a foreign call is rejected. |
| G8 | At-most-once is inside this workflow store, not exactly-once delivery to GitHub. | A crash after GitHub accepts a POST and before the store records completion can disagree with GitHub. | The specification states that limit. The offline test proves the store does not call the tool twice. Live reconciliation stays on the deployment checklist for a person. |
| G9 | The organisation is a command and a Python API. The playbook forbids adding a web UI, REST API, authentication, PostgreSQL, Kubernetes, Azure, or another agent framework. | An interactive software-company product does not fit this repository. | `python -m organisation` remains the local entry. Health check behaviour stays as it is. |
| G10 | The current tests and CLI encode Alex, Sam, Taylor, Jordan, and customer reference `A-100`. | Replacing the organisation without rewriting those tests fails the suite. | Implementation rewrites `tests/unit/test_organisation.py` and `tests/unit/test_organisation_cli.py`. Platform tests stay untouched. |
| G11 | `PlatformAgent` can create a session, and nothing in the platform runs a schedule or a webhook. | "Continuously improving" is not a resident process. | One work item, one correlation id, one run. |

Platform functions that were considered and are not required:

- A nested team graph. Organisation Python already composes graphs.
- Charter fields on `AgentDefinition`. Organisation models and tests cover them.
- A human pause with no tool. Team verdicts cover the gate. The GitHub write covers the durable pause.
- A knowledge index. Fixed read tools cover the records in scope.
- An exactly-once GitHub adapter. The store's at-most-once rule is the platform guarantee.

## 5. Organisation structure

```text
App Director (human, approves record_delivery)
Engineering Manager (human, receives escalations, no tool)

product_lead
├── ux_lead
└── analytics_lead

architecture_lead
├── mobile_lead
└── backend_lead

qa_lead            manager: App Director
security_lead      manager: App Director
release_lead       manager: App Director
```

`product_lead` and `architecture_lead` report to the App Director. The manager edge is accountability stored on the charter. It is not `manager_workflow`, and it does not let an agent call another agent.

Change classes:

| Class | Agents, in order | Skipped |
| --- | --- | --- |
| `feature` | product, ux, architecture, mobile ∥ backend, qa ∥ security ∥ analytics, release, then human approval | none |
| `defect` | product, architecture, mobile ∥ backend, qa ∥ security ∥ analytics, release, then human approval | ux |
| `release` | product, qa ∥ security ∥ analytics, release, then human approval | ux, architecture, mobile, backend |

`∥` is a `concurrent_workflow`. The approval step is `approval_workflow`. The class and repository checks are Python and fail closed.

## 6. Implementation sequence

Do this only when building the specification. Leave it undone in the plan change.

1. Replace the root `CLIENT_DISCOVERY.md` and `ORGANISATION_SPEC.md` worked example with the documents in this folder. Until that implementation change, the root templates still describe the fictional reference, which still matches the code.
2. Replace `src/organisation` and rewrite the two organisation test modules. Do not edit files under `src/agent_platform`. Do not add dependencies in `pyproject.toml`.
3. Add the modules below.

| Module | Responsibility |
| --- | --- |
| `src/organisation/models.py` | Frozen request, verdict, and outcome models from the specification. |
| `src/organisation/charter.py` | Team and agent charters: role, manager, tools, knowledge, objectives, permissions, scorecard, strategy, workflow, KPIs, governance, human approval. |
| `src/organisation/github.py` | Mock GitHub `HttpClient`, nine read tools, one side-effect tool. |
| `src/organisation/team.py` | `build_team`, `production_clients`, scope `travelodge-app`. |
| `src/organisation/service.py` | Allowlist, stage order, concurrent panels, release rule, approval pause, resume. |
| `src/organisation/evaluation.py` | Scripted cases, including the required safety cases. |
| `src/organisation/cli.py` | One local work item. Prints the typed outcome and the approval request. Does not print prompts, tokens, or the audit log. |
| `src/organisation/health.py` | Keep the current behaviour: import the application and, when configured, check the state directory. |
| `src/organisation/README.md` | Describe Travelodge App in place of the fictional team. |

4. Add `GITHUB_API_TOKEN=` to `.env.example` with the other empty placeholders. Read it through settings at the client boundary. Tests keep using `SecretString` of the fixture token and do not read a real secret.
5. Point the root `README.md` reference-organisation section at Travelodge App.
6. Run `uv run ruff check .`, `uv run ruff format --check .`, `uv run pyright`, and `uv run pytest`. Do not call OpenAI. Do not call GitHub.
7. Leave the live smoke test and `DEPLOYMENT_CHECKLIST.md` signatures for a person.

Charter tests to include:

- Every team in the specification has one agent, a strategy, a workflow, KPIs, a governance statement, and a human approval point.
- Every agent charter has a role, a manager, at least one knowledge item, at least one objective, a permission set, and a scorecard.
- The tools on `create_agent` equal the charter tools.
- No agent charter lists `record_delivery`.
- The approval workflow is the only holder of `record_delivery`.

## 7. Decisions this plan treats as accepted

1. Organisation 001 is one delivery process, not a resident software company with many processes.
2. Nine agents, one per named team.
3. One GitHub write, `record_delivery`, approved by the App Director.
4. A team `block`, a release `no_go`, and a release `hold` do not open that approval.
5. The App Director cannot override those stops inside the workflow.
6. GitHub access in tests is an in-process mock. The fixture issues are `TL-100`, `TL-101`, and `TL-102`.
7. `src/agent_platform` stays as released in 1.0.0.
8. The Travelodge mobile application is not a deliverable of this repository.

A later request that needs a platform change should stop at the gap, name the missing platform function, and wait for an explicit acceptance before any platform edit.
