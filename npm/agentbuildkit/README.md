# agentbuildkit

One-command bootstrapper for the AgentBuildKit Python harness.

```bash
npx agentbuildkit
```

This package is not on npm yet. From this repository, before any publish:

```bash
node npm/agentbuildkit/bin/agentbuildkit.js --source local
```

## What it installs

The bootstrapper is Node. AgentBuildKit itself is Python. The bootstrapper uses [uv](https://docs.astral.sh/uv/) to install the `agentkit` command into an isolated uv tool environment. It does not change system Python, and it does not ask for API keys.

The offline demo does not need an OpenAI or Anthropic key.

Supported systems are macOS (Apple Silicon and Intel) and Linux (x64 and arm64). Node.js 18 or newer is required.

## Sources

The default source is the Python package name `agentbuildkit` (`uv tool install agentbuildkit`). That package is not on PyPI yet.

`--source local`, or `AGENTBUILDKIT_SOURCE=local`, installs the repository you are in. A path works too: `--source /path/to/agentbuildkit`.

`--upgrade` reinstalls from the selected source. A second run without `--upgrade` leaves an existing installation in place.

If `uv` is missing, an interactive terminal asks before running Astral's installer. A non-interactive run does not download uv unless you pass `--install-uv`.

## After installation

Skip the demo:

```bash
npx agentbuildkit --no-demo
```

Check the installation:

```bash
agentkit doctor
```

Run the harness demo:

```bash
agentkit demo
```

If `agentkit` is not on `PATH`, the bootstrapper prints the uv tool directory and the `export PATH=...` line to add. It does not edit shell startup files.

Remove the tool:

```bash
uv tool uninstall agentbuildkit
```

Licensed under the Apache License 2.0. See the repository `LICENSE` file.
