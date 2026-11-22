# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Developers evaluating AgentBuildKit on their own machine. They install it, run the offline demo and the customer-service example, and use the local dashboard to see how a team routes a request, what each agent may use, when a person must approve a side effect, what the audit trail recorded and whether the evaluation suite passes. They are deciding whether to build their own agent teams on the harness.

## Product Purpose

AgentBuildKit is the production layer around an agent framework. A model alone is not an agent: the harness turns it into something you can route, constrain, approve, audit and evaluate. Success is a developer trusting the harness enough to build a real team on it, because they have seen each guarantee hold in a run they can inspect.

## Positioning

Model + Harness = Agent. Deterministic Python owns routing, tool permission and whether a side effect may run; the model only supplies judgment and language. Each agent declares its own provider and model, and one workflow can mix OpenAI and Anthropic without branching on the provider. It builds on Microsoft Agent Framework 1.x.

## Operating Context

- Install with `npx agentbuildkit` (installs the Python CLI with uv, then runs `agentkit doctor`) or `uv sync` from a clone. Python 3.12.
- `agentkit demo` is offline: scripted model replies, no API key, no provider calls.
- `examples/agentbuildkit_templates/customer_service` is a separate four-agent example (Alex manager, Sam and Taylor specialists, Jordan reviewer) over fixture data. It has a local dashboard served by the Python standard library at `127.0.0.1:8765`.
- A live run of the example needs `OPENAI_API_KEY`. `APP_STATE_DIRECTORY` makes pending approvals, outcomes and the audit ledger survive a restart; without it state is in memory only.
- Team bundles are JSON (`team.json`) validated with `agentkit team validate`.

## Capabilities and Constraints

- Per-agent provider and model; typed Pydantic inputs and outputs; tool allowlists per agent.
- Side effects require an allowlisted tool and human approval before they run. Approval runs an action at most once; an interrupted reservation is marked unresolved and is not replayed. This is not exactly-once delivery to an external system.
- SQLite pause and resume, idempotent side effects, and an audit ledger that records provider and model. The ledger stores argument hashes, never argument values or secrets.
- Provider errors are shown as safe, classified messages; raw exception payloads and API keys are never displayed.
- Reusable harness code lives in `src/agentbuildkit`; customer-service concepts stay in `examples/agentbuildkit_templates/customer_service`.
- Microsoft Agent Framework is the only agent framework. Foundry stays an optional extra. New dependencies or a frontend build system need explaining before they are introduced.
- Dashboard data limits: outcomes have no timestamps; a pending approval does not store the customer's message or the specialist's draft; evaluation reports are not persisted.
- Not in this release: skills and persistent agent memory.
- Planned, not built: agent team bundles sold through a store in the dashboard. Never present planned features as available.

## Brand Commitments

- Name: AgentBuildKit. Open source under Apache 2.0. The project author is AgentBuildKit.
- Current headline on agentbuildkit.com: "Build agents you trust". The README still uses "Build agents. Not demos."
- Logo: the "Courses" mark, an A built from stacked courses with the Signal Blue crossbar. Logo kit and usage guide are in the AgentBuildKit project files.
- The dashboard follows the website palette: Ice White #F5F7FA background, Soft Slate Blue #EBF0F5 surfaces, Ink #0F141C text, Muted Blue-Gray #5C6B7A secondary text, Brand Blue #2B4CF2 primary, Deep Charcoal #111111 secondary. Warm cream backgrounds were rejected as looking AI-generated.
- Voice: plain and factual. Lead with what ships today; say what something does not do.

## Evidence on Hand

- Offline demo (`agentkit demo`) and the customer-service example with its dashboard.
- 13 scripted evaluation scenarios in `examples/agentbuildkit_templates/customer_service/evaluation.py`, plus the unit test suite under `tests/unit`.
- v0.1.0 is the published PyPI release. npm `agentbuildkit` 0.0.2 is the published bootstrapper and installs the harness only. The 0.2.0 Python packages and npm 0.0.3 are prepared in this repository and are not published yet.
- Fixture data only: reference A-100, account name Northwind, mock business API at `http://business.test`.
- Absent, and must not be fabricated: customers, testimonials, case studies, benchmarks, production deployments, live CRM integrations, token costs or timings the backend does not record.

## Product Principles

1. Show the guarantee, not the claim. Every safety property should be visible in a run the developer can inspect.
2. Never imply an action ran. Submitted, reserved, completed, declined and unresolved are different states and stay distinct.
3. Display only what the backend records. Missing data is stated plainly, not filled in.
4. Keep the harness general. Example-specific behaviour stays in the example.
5. Configuration and recorded activity are different things and are shown as different things.

## Accessibility & Inclusion

Accessible contrast, full keyboard navigation and responsive layouts down to phone width for the dashboard. Status is never conveyed by colour alone.
