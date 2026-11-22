# Install

The current public release is PyPI `agentbuildkit` 0.2.0, PyPI
`agentbuildkit-templates` 0.2.0, and npm `agentbuildkit` 0.0.4.

`agentbuildkit` 0.2.0 is the harness. `agentbuildkit-templates` 0.2.0 is the
first-party package. Its import name is `agentbuildkit_templates`. The
templates package requires `agentbuildkit>=0.2.0,<0.3.0`. Importing it also
checks for packaged team JSON loading, the revision service, the POSIX
startup lock error, the drafting server, and rendered dashboard pages. The
older `agentbuildkit` 0.1.0 release remains on PyPI and does not provide
those capabilities. npm `agentbuildkit` 0.0.2 remains available and installs
the harness only.

Composio and Microsoft Foundry stay optional extras of `agentbuildkit`. The
bootstrapper and the default Python install do not install them.

The revision database startup lock uses POSIX `flock`. Dashboard and MCP
startup fail with an actionable message on a platform without `fcntl`.
Windows has not been verified. Other harness commands that do not open that
lock are unchanged.

## Current install

Default npm installation and dashboard launch:

```bash
npx agentbuildkit
```

An interactive terminal explains the state directory, offers the local
dashboard, and then offers the offline demo. The dashboard stays in that
terminal. Press Ctrl+C to stop it. Neither step calls a model. A
non-interactive run prints that it skipped both and does not wait.

Python installation of the harness and the templates:

```bash
uv tool install --python 3.12 --managed-python \
  --with "agentbuildkit-templates>=0.2.0,<0.3.0" \
  "agentbuildkit>=0.2.0,<0.3.0"
```

Harness-only installation stays available through Python:

```bash
uv tool install --python 3.12 --managed-python "agentbuildkit>=0.2.0,<0.3.0"
```

Optional Composio installation:

```bash
uv tool install --python 3.12 --managed-python \
  --with "agentbuildkit-templates>=0.2.0,<0.3.0" \
  "agentbuildkit[composio]>=0.2.0,<0.3.0"
```

Dashboard and MCP with an explicit durable state directory:

```bash
agentkit dashboard --state-directory /ABSOLUTE/PATH/agentbuildkit-state
agentkit mcp serve --state-directory /ABSOLUTE/PATH/agentbuildkit-state
```

MCP requires an absolute path and does not read `APP_STATE_DIRECTORY`.

Upgrading a harness-only installation. npm 0.0.4 detects a tool install that
does not report the templates package and reinstalls both:

```bash
npx agentbuildkit
```

The same upgrade through Python:

```bash
uv tool install --force --python 3.12 --managed-python \
  --with "agentbuildkit-templates>=0.2.0,<0.3.0" \
  "agentbuildkit>=0.2.0,<0.3.0"
```

## Build local candidate artifacts

From the repository root:

```bash
uv build --all-packages --out-dir dist
```

That writes `dist/agentbuildkit-0.2.0-*.whl`, its source distribution, and the
same two artifacts for `agentbuildkit-templates`.

## Install both local artifacts

Use one resolver operation. The shell expands each glob to one file. Do not
add `--no-deps` and do not install the packages in separate commands.

```bash
uv venv /tmp/agentbuildkit-clean --python 3.12
uv pip install --python /tmp/agentbuildkit-clean/bin/python \
  dist/agentbuildkit-0.2.0-*.whl \
  dist/agentbuildkit_templates-0.2.0-*.whl
```

The same command works with the two `.tar.gz` source distributions. Installing
from a source distribution builds it from the archive and its declared
dependencies. It does not use this checkout. This command uses artifacts
built from this repository. It is not a public install command.

Optional Composio support from the local harness artifact:

```bash
uv pip install --python /tmp/agentbuildkit-clean/bin/python \
  "agentbuildkit[composio] @ dist/agentbuildkit-0.2.0-py3-none-any.whl" \
  dist/agentbuildkit_templates-0.2.0-py3-none-any.whl
```

A missing `composio` package does not stop the dashboard. Connect stays
unavailable until the extra is installed and `COMPOSIO_API_KEY` is set.

## Commands

```bash
/tmp/agentbuildkit-clean/bin/agentkit version
/tmp/agentbuildkit-clean/bin/agentkit doctor
/tmp/agentbuildkit-clean/bin/agentkit demo --approve
/tmp/agentbuildkit-clean/bin/agentkit team validate /path/to/team.json
/tmp/agentbuildkit-clean/bin/agentkit dashboard --state-directory /tmp/agentbuildkit-state
/tmp/agentbuildkit-clean/bin/agentkit mcp serve --state-directory /tmp/agentbuildkit-state
```

`doctor` reports whether the templates package is installed and whether POSIX
`flock` is available. A missing templates package does not fail `doctor`.
`dashboard` and `mcp serve` exit with an install instruction instead of an
import traceback.

MCP requires an absolute `--state-directory`. It does not read
`APP_STATE_DIRECTORY`.

Dashboard precedence:

1. `--state-directory`, when the flag is present, is used as given and overrides
   `.env` and the shell.
2. Without the flag, a `.env` file in the current directory wins over the shell.
3. Without that file, the shell value of `APP_STATE_DIRECTORY` is used.
4. With none of those, the process keeps state in memory until it stops.

Provider keys still come from `.env` and the process environment. Drafting,
validation, scripted evaluation, and the offline demo do not need them. The
dashboard does not construct a live model client or a Composio client while it
starts. A live run fails with a clear message when the provider credential is
missing.

## Checkout commands

These still work inside this repository and are not the installed commands:

```bash
PYTHONPATH=src:examples uv run agentkit mcp serve --state-directory /tmp/agentbuildkit-drafts
PYTHONPATH=src:examples uv run python -m customer_service.dashboard --state-directory /tmp/agentbuildkit-drafts
```

`python -m customer_service`, `python -m customer_service.dashboard`,
`python -m customer_service.health`, and `python -m office.mcp` remain thin
checkout wrappers. They call `agentbuildkit_templates` and are not part of
the installed distribution.

## Local MCP client

Use the absolute installed `agentkit`. The placeholder paths are not a specific
machine. Cursor discovered this installed command separately from the checkout
entry. See [MCP](mcp.md). Claude Desktop was not tested.

```json
{
  "mcpServers": {
    "agentbuildkit-drafts-installed": {
      "command": "/ABSOLUTE/PATH/agentkit",
      "args": ["mcp", "serve", "--state-directory", "/ABSOLUTE/PATH/agentbuildkit-drafts"]
    }
  }
}
```

## npm bootstrapper

`npx agentbuildkit` resolves to npm 0.0.4. It installs
`agentbuildkit>=0.2.0,<0.3.0` and `agentbuildkit-templates>=0.2.0,<0.3.0`,
runs `agentkit doctor`, explains the state directory, and can open the
dashboard in the foreground. It does not install Composio or Foundry and it
does not start a live model call. npm 0.0.2 remains the older harness-only
bootstrapper.

From this repository:

```bash
node npm/agentbuildkit/bin/agentbuildkit.js --source local --no-demo --no-dashboard
```

`--source local` installs this checkout and `examples/`. It is a checkout
install. It is not the published npm package.
