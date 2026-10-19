# Agent Framework Starter

Reusable Python starter for enterprise AI agent teams built on Microsoft Agent Framework 1.x.

This repository is a private GitHub template. Copy it for a client, then put that client's agents in `src/organisation`. Shared infrastructure stays in `src/agent_platform`.

The current tree is the Phase 1 skeleton. It installs the framework and checks the package layout. It does not define agents yet.

## Requirements

- macOS on Apple Silicon, or any platform with Python 3.12
- [uv](https://docs.astral.sh/uv/) 0.12 or newer

## Setup

```bash
uv python install 3.12
uv sync --locked
cp .env.example .env
```

Fill in `OPENAI_API_KEY` in `.env` when a later phase needs a live model. The default local path uses `OpenAIChatClient`.

Microsoft Foundry is an optional extra and is not installed by `uv sync`:

```bash
uv sync --locked --extra foundry
```

## Checks

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
```

These checks run without an API key or network access.

## Layout

| Path | Purpose |
| --- | --- |
| `src/agent_platform` | Reusable platform code |
| `src/organisation` | Client-specific agents and workflows |
| `tests/unit` | Offline tests |
| `tests/integration` | Live-service tests, excluded from the default CI path until they exist |
| `tests/evaluation` | Offline evaluator cases |
| `AGENTS.md` | Instructions for Cursor agents working in this repository |
