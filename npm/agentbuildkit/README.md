# agentbuildkit

Bootstrapper for [AgentBuildKit](https://agentbuildkit.com).

**Build Agents You Trust**

AgentBuildKit is an open-source harness for building, running and governing AI agent teams. This package is the Node installer. The harness and the templates are Python.

The current published release is npm `agentbuildkit` 0.0.3. It installs PyPI `agentbuildkit` 0.2.0 and `agentbuildkit-templates` 0.2.0. npm `0.0.2` remains available and installs the harness only.

```bash
npx agentbuildkit
```

Source and issues: [github.com/agentbuildkit/agentbuildkit](https://github.com/agentbuildkit/agentbuildkit)

## Requirements

- Node.js 18 or newer
- Python 3.12, which uv can install
- macOS (Apple Silicon or Intel) or Linux (x64 or arm64)

The bootstrapper does not change system Python and does not ask for API keys. `agentkit demo` is offline. Opening the dashboard does not call a model.

Dashboard and MCP startup need POSIX `flock`. They fail with a platform message when that lock is unavailable. Windows has not been verified.

## What `npx agentbuildkit` does

1. Checks the operating system and the Node.js version.
2. Uses `uv` when it is already installed.
3. If `uv` is missing, an interactive terminal asks before running Astral's official installer. A non-interactive run does not download `uv` unless you pass `--install-uv`.
4. Installs `agentbuildkit>=0.2.0,<0.3.0` and `agentbuildkit-templates>=0.2.0,<0.3.0` with `uv tool install`. A harness-only tool install is upgraded.
5. Runs `agentkit doctor`.
6. Explains the state directory. A `--state-directory` path is kept. Without it, a `.env` file in the current directory wins over the shell. With none of those, dashboard state lasts until the process stops.
7. In an interactive terminal, offers the local dashboard. It runs in the foreground. Press Ctrl+C to stop it.
8. In an interactive terminal, offers `agentkit demo`.

A non-interactive run says that it skipped the dashboard and the demo. It does not wait and it does not start either one.

`--no-demo` skips the demo. `--no-dashboard` skips the dashboard. `--upgrade` reinstalls both packages. `--state-directory` is passed to the dashboard when you accept it. Composio and Foundry stay optional and are not installed by this command.

From a clone of the repository, `--source local` installs that checkout and `examples/`:

```bash
node npm/agentbuildkit/bin/agentbuildkit.js --source local --no-demo --no-dashboard
```

`AGENTBUILDKIT_SOURCE` accepts the same values as `--source`.

The published 0.0.3 package prints the previous tagline in `--help`, together with a sentence written before publication. The installer behaviour above is the behaviour of that published package.

## After installation

```bash
agentkit doctor
agentkit demo
agentkit version
agentkit dashboard --state-directory /ABSOLUTE/PATH/agentbuildkit-state
agentkit mcp serve --state-directory /ABSOLUTE/PATH/agentbuildkit-state
```

Harness-only installation stays a Python command. See [Install](../../docs/install.md).

If `agentkit` is not on `PATH`, the bootstrapper prints the uv tool directory and the `export PATH=...` line to add. It does not edit shell startup files.

Remove the tool:

```bash
uv tool uninstall agentbuildkit
```

Licensed under the Apache License 2.0.
