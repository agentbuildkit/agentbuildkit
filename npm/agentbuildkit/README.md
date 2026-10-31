# agentbuildkit

Bootstrapper for [AgentBuildKit](https://agentbuildkit.com), a Python harness for reliable agents.

```bash
npx agentbuildkit
```

This package is Node. AgentBuildKit itself is Python. The bootstrapper uses [uv](https://docs.astral.sh/uv/) to install the `agentbuildkit` Python package into an isolated tool environment, then runs the Python CLI:

```bash
agentkit
```

Source and issues: [github.com/gluzio/agentbuildkit](https://github.com/gluzio/agentbuildkit)

## Requirements

- Node.js 18 or newer
- Python 3.12, which uv can install
- macOS (Apple Silicon or Intel) or Linux (x64 or arm64)

The bootstrapper does not change system Python and does not ask for API keys. `agentkit demo` is offline and does not need an OpenAI or Anthropic key.

## What the command does

1. Checks the operating system and the Node.js version.
2. Uses `uv` when it is already installed.
3. If `uv` is missing, an interactive terminal asks before running Astral's official installer. A non-interactive run does not download `uv` unless you pass `--install-uv`.
4. Installs the Python package with `uv tool install`.
5. Runs `agentkit doctor`.
6. In an interactive terminal, offers `agentkit demo`.

`--no-demo` skips the demo prompt. `--upgrade` reinstalls the Python package.

From a clone of the repository, `--source local` installs that checkout:

```bash
node npm/agentbuildkit/bin/agentbuildkit.js --source local
```

`AGENTBUILDKIT_SOURCE` accepts the same values as `--source`.

## After installation

```bash
agentkit doctor
agentkit demo
agentkit version
```

If `agentkit` is not on `PATH`, the bootstrapper prints the uv tool directory and the `export PATH=...` line to add. It does not edit shell startup files.

Remove the tool:

```bash
uv tool uninstall agentbuildkit
```

Licensed under the Apache License 2.0.
