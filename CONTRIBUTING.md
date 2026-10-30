# Contributing

AgentBuildKit is a Python 3.12 project. [uv](https://docs.astral.sh/uv/) installs the interpreter and the dependencies.

```bash
uv sync
```

Run the checks before opening a pull request:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
```

The offline demo needs no API key:

```bash
uv run agentkit demo --approve
```

Bootstrapper tests live in `npm/agentbuildkit` and do not download uv or Python:

```bash
cd npm/agentbuildkit
npm test
```

Pull requests should keep the existing behaviour covered by an offline test. Live provider tests belong behind `@pytest.mark.integration`. Do not commit `.env` or credentials. Do not add a dependency on a second agent framework.
