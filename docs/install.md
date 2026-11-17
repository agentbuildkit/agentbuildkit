# Install from local artifacts

The published `agentbuildkit` 0.1.0 package and the published `npx agentbuildkit`
package install the harness only. They provide `agentkit version`, `agentkit
doctor`, and `agentkit demo`. They do not provide the dashboard or the drafting
server. The commands in this page are for wheels built from this repository.
They are not PyPI or `npx` commands until a later release.

## Packages

`agentbuildkit` is the harness. It does not contain the customer-service or
operations-reporting applications.

`agentbuildkit-templates` is the first-party package built from `examples/`.
Its import name is `agentbuildkit_templates`. That package contains
`agentbuildkit_templates.office`, `agentbuildkit_templates.customer_service`,
and `agentbuildkit_templates.operations_reporting`, including the dashboard
files, team bundles, fonts, and fixture data. The harness imports
`agentbuildkit_templates` by that name. It does not search the environment
for other template packages and it does not install a top-level `office`,
`customer_service`, or `operations_reporting` module.

The two local artifacts currently share version 0.1.0 with each other and
with the published harness. That shared version is not a compatibility
promise. The templates call harness capabilities that the published 0.1.0
package does not have: packaged team JSON loading, the revision service, the
POSIX startup lock error, the drafting server, and rendered dashboard pages.
Importing `agentbuildkit_templates` checks for those capabilities and stops
with an install instruction when any are missing.

`HARNESS_RELEASE_BOUND` is empty on purpose. Before `agentbuildkit-templates`
is published, its dependency must require at least the first harness release
that includes those capabilities. That version has not been chosen or
released. There is no public install command for this pair of packages. Do
not install the published `agentbuildkit` 0.1.0 package and expect it to run
these templates.

Composio and Microsoft Foundry stay optional extras of `agentbuildkit`. The
templates package does not require either one.

The revision database startup lock uses POSIX `flock`. A platform without
`fcntl` gets a clear error. Windows has not been verified.

## Build

From the repository root:

```bash
uv build --all-packages --out-dir dist
```

That writes `dist/agentbuildkit-0.1.0-*.whl`, its source distribution, and the
same two artifacts for `agentbuildkit-templates`.

## Install both local artifacts

Use one resolver operation. The shell expands each glob to one file. Do not
add `--no-deps` and do not install the packages in separate commands.

```bash
uv venv /tmp/agentbuildkit-clean --python 3.12
uv pip install --python /tmp/agentbuildkit-clean/bin/python \
  dist/agentbuildkit-0.1.0-*.whl \
  dist/agentbuildkit_templates-0.1.0-*.whl
```

The same command works with the two `.tar.gz` source distributions. Installing
from a source distribution builds it from the archive and its declared
dependencies. It does not use this checkout. This command is for artifacts
built from this repository. It is not a public install command. The published
`agentbuildkit` 0.1.0 package cannot satisfy these templates, even though the
local artifacts use the same version string.

Optional Composio support, still without publishing. Install the extra from
the local harness artifact in the same command as the templates package:

```bash
uv pip install --python /tmp/agentbuildkit-clean/bin/python \
  "agentbuildkit[composio] @ dist/agentbuildkit-0.1.0-py3-none-any.whl" \
  dist/agentbuildkit_templates-0.1.0-py3-none-any.whl
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

`npx agentbuildkit` installs the published harness. It does not install
`agentbuildkit-templates`.

From this repository, the local bootstrapper installs both the checkout and
`examples/`:

```bash
node npm/agentbuildkit/bin/agentbuildkit.js --source local --no-demo
```

That is a checkout install. It is not proof that the published npm package
can start the dashboard.
