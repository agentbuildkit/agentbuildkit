# AgentBuildKit

**Build agents. Not demos.**

AgentBuildKit is the production layer around your agent framework.

A model alone is not an agent. AgentBuildKit is the harness that turns a model into something you can route, constrain, approve, audit, and evaluate.

**Model + Harness = Agent**

It currently builds on Microsoft Agent Framework. Each agent declares its own provider and model. OpenAI and Anthropic are the built-in providers, and one workflow can mix them. Deterministic Python owns routing, tool permission, and whether a side effect may run.

## What it adds

- **Models.** Per-agent provider and model selection.
- **Agents.** Typed inputs and outputs.
- **Tools.** An allowlist on each agent. Reads can run. Side effects wait.
- **Workflows.** Deterministic routing across agents.
- **Guardrails and human approval.** A side effect pauses until a person decides.
- **State and audit.** SQLite pause and resume, idempotent side effects, and an audit trail that records the provider and model.
- **Evaluation.** The same run can be checked against expectations.

## Quick start

### Public install

```bash
npx agentbuildkit
```

The command installs the Python CLI with [uv](https://docs.astral.sh/uv/) and then runs `agentkit doctor`. In an interactive terminal it offers the offline demo.

### Development install

From a clone of this repository:

```bash
node npm/agentbuildkit/bin/agentbuildkit.js --source local
```

That installs this checkout into an isolated uv tool environment. `--upgrade` reinstalls it. `--no-demo` skips the demo prompt. If `uv` is missing, an interactive terminal asks before running Astral's installer. A non-interactive run installs uv only when you pass `--install-uv`.

To work on the Python package directly:

```bash
uv sync
uv run agentkit demo
```

Python 3.12 is required. uv manages that interpreter. The bootstrapper does not change the system Python.

### Commands

```bash
agentkit version
agentkit doctor
agentkit demo
```

`agentkit doctor` checks the installation. Missing OpenAI or Anthropic credentials are reported and are not a failure.

`agentkit demo` is offline. It needs no API key and it does not call a model provider. It shows the harness: an agent routes the request, a specialist reads an allowed tool, an unauthorised tool is rejected, a side effect pauses for approval, the run resumes after that decision, a reviewer checks the result, and the evaluation passes or fails from the audit trail.

`agentkit demo --approve` and `agentkit demo --reject` make that decision without a prompt.

## Architecture

```text
Model
  ↓
Agent                 provider and model belong to that agent
  ↓
Tools                 allowlist; reads run, side effects wait
  ↓
Workflow              deterministic routing
  ↓
Guardrails / approval human decision before a side effect
  ↓
State / audit         SQLite pause, resume, and audit events
  ↓
Evaluation            expectations checked against the trace
```

A workflow can mix providers:

```text
alex    → OpenAI / one model
taylor  → OpenAI / another model
jordan  → Anthropic / another model
```

The names above are the offline demo's agents. The provider and model are fields on each agent definition. The workflow that receives the clients does not branch on the provider. The offline demo uses scripted clients, so those calls are not OpenAI or Anthropic requests.

## The offline demo

The demo is a loading-bay request: book a technician for tomorrow.

1. Alex, the orchestrator, chooses the `service` route.
2. Taylor, the service specialist, calls `read_ticket`. That read is allowed.
3. `cancel_ticket` is not on Taylor's allowlist, so it is rejected.
4. `schedule_visit` is a side effect. The run pauses and stores the approval in SQLite.
5. A person approves or rejects it. Approval runs the visit once. Rejection does not.
6. Jordan reviews the outcome.
7. The evaluation checks the trace, including the route, the rejected tool, the approval, and the audit record.

That is the production harness running locally, with the model text supplied by a script.

## Current scope

This release includes the Python package `agentbuildkit`, the `agentkit` command, and the Node bootstrapper in `npm/agentbuildkit`. Microsoft Foundry is an optional extra and is not installed by a normal sync.

Skills and persistent agent memory are not in this release. The harness loads declarative team bundles. One local dashboard installs Customer Service and Operations Reporting, and Create team can add another inactive instance of either template. Each instance keeps its own runs, approvals, audit, revisions, and spreadsheet window. A new instance cannot run until its draft is validated, evaluated offline, and activated. Set `APP_STATE_DIRECTORY` to keep instances and drafts in `revisions.sqlite`. When that name is in `.env`, it overrides the shell. Use `--state-directory` to choose a different directory for one process. The scripted check does not measure a live model or account readiness. Google Sheets stays optional, and the key stays on the server.

`examples/customer_service` is a separate example with fixture data. It is not the offline demo and it is not part of the installed package.

Further detail is in [docs/architecture.md](docs/architecture.md).

## Development checks

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
```

Bootstrapper tests, from `npm/agentbuildkit`:

```bash
npm test
```

## License

Apache License 2.0. See [LICENSE](LICENSE).
