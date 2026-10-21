# Agent Framework Starter

Private template for building a client AI organisation on Microsoft Agent Framework 1.x. The current release is [1.0.0](CHANGELOG.md).

A consulting engagement copies this repository, keeps the shared platform, and replaces the sample organisation with that client's agents, tools, and workflows. The platform handles typed agent runs, allowlisted tools, human approval, SQLite pause state, HTTP and MCP allowlists, audit, correlation, and offline evaluation. The client package decides what the organisation is allowed to do.

## Architecture

`src/agent_platform` is reusable platform IP. `src/organisation` is the replaceable client implementation. Agents return typed Pydantic models. Python routes, validates, and decides when a side effect may run. The model does not choose credentials, URLs, or whether a write is permitted.

The diagram and the rules for tools, approval, persistence, and evaluation are in [ARCHITECTURE.md](ARCHITECTURE.md).

Platform state version 2 adds a durable audit ledger, a run registry, and an organisation directory with work items to the same SQLite file. The same version now also stores workflow delegations, child-run outcomes, named gates, and bounded grants. Version 1 files still open for approval checkpoints. Ledger, run, and directory calls fail closed until `migrate_platform_state()` runs. The reference organisation does not require that migration and still uses its anonymous approval decision.

## Quick start

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/) 0.12 or newer.

```bash
uv python install 3.12
uv sync --locked
cp .env.example .env
uv run python -m organisation
```

Put `OPENAI_API_KEY` in `.env` only when you run a live model. Leave the file uncommitted. `OPENAI_CHAT_MODEL` selects the model. An empty value uses `gpt-4.1-mini` for the reference command.

Microsoft Foundry is an optional extra and is not installed by a normal sync:

```bash
uv sync --locked --extra foundry
```

## Reference organisation

`src/organisation` contains a fictional B2B customer-service team: Alex classifies, Sam reads accounts, Taylor reads services, and Jordan reviews the draft. A service change pauses for a person. The business system is an in-process mock with one customer, `A-100`. This package is the working example. Replace it for a client deployment. Details are in [src/organisation/README.md](src/organisation/README.md).

## Docker

The image installs the locked dependencies, runs as uid 10001, and does not contain `.env`. Approval state is SQLite on `/data`.

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

The health check imports the application and confirms the data directory is writable. It does not call OpenAI.

## Create a new organisation

1. Copy this repository for the client.
2. Complete [CLIENT_DISCOVERY.md](CLIENT_DISCOVERY.md) with the business.
3. Turn that into [ORGANISATION_SPEC.md](ORGANISATION_SPEC.md).
4. Follow [CREATE_NEW_ORGANISATION.md](CREATE_NEW_ORGANISATION.md).
5. In Cursor, use [NEW_ORGANISATION_PROMPT.md](NEW_ORGANISATION_PROMPT.md). The prompt keeps changes inside `src/organisation` unless a platform gap is accepted first.
6. Walk [DEPLOYMENT_CHECKLIST.md](DEPLOYMENT_CHECKLIST.md) before the client uses it.

## Security and governance

Secrets stay in the environment. Side effects use an allowlisted tool and wait for a person. HTTP and MCP targets come from configuration, and credentials are supplied by a header provider or client settings. Audit records and checkpoints store hashes and identifiers. They omit prompts, raw model responses, raw tool arguments, and credentials. Unknown routes fail closed. A repeated approval does not run the side effect again.

## Testing and evaluation

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
```

The default suite runs offline. Organisation cases live with the client package and use a scripted chat client. A live model run is a separate smoke test performed by a person.

## Layout

| Path | Purpose |
| --- | --- |
| `src/agent_platform` | Reusable platform |
| `src/organisation` | Client organisation, currently the reference example |
| `tests/unit` | Offline tests |
| `tests/integration` | Live-service tests, marked and kept out of the default run |
| `AGENTS.md` | Rules for engineers and Cursor agents working in this repository |
