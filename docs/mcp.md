# Local drafting server

A compatible local MCP client can draft an inactive team, validate it, and run
the scripted offline suite. The server calls the same revision service as the
dashboard. It does not activate a team, roll one back, run it, approve an
action, or connect an account.

This is stdio only. The MCP Python client drove stdio sessions for the tool
contract, idempotency, stale edits, cancellation, and a fresh process. Cursor's
MCP client, using the configuration shape below, discovered the tools and
completed an inactive Operations Reporting draft. The dashboard on that same
temporary directory showed the team as not active, with the edited instructions
and the scripted result, and without a request form. Claude Desktop was not
tested. A client that can only connect to a remote HTTP or OAuth server cannot
use this milestone.

The server needs POSIX file locking (`flock`) for the revision database.
Dashboard and MCP startup fail with an actionable message when `fcntl` is
unavailable. Windows has not been verified. Harness commands that do not open
that lock are unchanged.

The published `agentbuildkit` 0.1.0 package and the published
`npx agentbuildkit` 0.0.2 package do not include `agentkit mcp` or the
templates. The upcoming 0.2.0 commands in [Install](install.md) are not on
the public index yet. A checkout command uses this repository. An installed
command uses wheels built from this repository.

## Launch

From a locally built install, with an absolute directory you choose. This is
not a PyPI or `npx` command until a later release:

```bash
agentkit mcp serve --state-directory /tmp/agentbuildkit-drafts
```

From the repository root, the same server still starts through the checkout:

```bash
mkdir -p /tmp/agentbuildkit-drafts
PYTHONPATH=src:examples uv run agentkit mcp serve --state-directory /tmp/agentbuildkit-drafts
```

The same process can be started with the checkout's virtualenv Python:

```bash
PYTHONPATH=src:examples .venv/bin/python -m office.mcp --state-directory /tmp/agentbuildkit-drafts
```

`--state-directory` is required and must be absolute. The process does not
read `APP_STATE_DIRECTORY` from `.env` or the shell. Provider keys are not
loaded for drafting or for the scripted suite.

Stdout is MCP protocol traffic. Diagnostics go to stderr and do not include
credentials, instruction bodies, or customer records.

## Cursor or Claude Desktop

Cursor loaded two separate configurations. The checkout entry below discovered
the drafting tools and completed an inactive Operations Reporting draft.
A second entry, whose command is the installed `agentkit` and whose arguments
are `mcp serve` plus an absolute state directory, was also discovered by
Cursor. That process created, edited, validated, and evaluated another
inactive Operations Reporting draft. The dashboard started from that same
`agentkit` showed the draft and the scripted result, and the team stayed
inactive. Claude Desktop was not tested. The paths below are placeholders.
Do not set `PYTHONPATH` on the installed entry.

Checkout configuration:

```json
{
  "mcpServers": {
    "agentbuildkit-drafts": {
      "command": "/ABSOLUTE/PATH/agent-framework-starter/.venv/bin/python",
      "args": [
        "-m",
        "office.mcp",
        "--state-directory",
        "/ABSOLUTE/PATH/agentbuildkit-drafts"
      ],
      "env": {
        "PYTHONPATH": "/ABSOLUTE/PATH/agent-framework-starter/src:/ABSOLUTE/PATH/agent-framework-starter/examples"
      }
    }
  }
}
```

`python -m office.mcp` is a checkout wrapper. It is not installed. The
installed Cursor entry used for verification pointed at a temporary virtual
environment and a temporary state directory. Those `/tmp` paths are not a
permanent installation.

Installed configuration. Use the absolute `agentkit` from the environment
where `agentbuildkit-templates` is installed:

```json
{
  "mcpServers": {
    "agentbuildkit-drafts-installed": {
      "command": "/ABSOLUTE/PATH/agentkit",
      "args": [
        "mcp",
        "serve",
        "--state-directory",
        "/ABSOLUTE/PATH/agentbuildkit-drafts"
      ]
    }
  }
}
```

## Ask for a draft

> Create an inactive Operations Reporting team named Facilities Operations for
> plant jobs. Set the reviewer instructions to name only job ids present in
> the records. Validate the draft and run the scripted evaluation. Leave the
> team inactive.

The client should call `list_templates`, `create_team` with an idempotency
key, `update_draft` with the returned version, `validate_draft`,
`evaluate_draft`, and `compare_draft`. A repeated create with the same key
and the same name, description, and template returns that team. A different
name, description, template, or team id for the key conflicts and leaves the
original unchanged.

## Activate in the dashboard

Point the dashboard at the same absolute directory.

Installed command, after the templates package is installed:

```bash
agentkit dashboard --state-directory /tmp/agentbuildkit-drafts
```

Checkout command:

```bash
PYTHONPATH=src:examples uv run python -m customer_service.dashboard --state-directory /tmp/agentbuildkit-drafts
```

Open the new team. It stays inactive, with no request form, until you validate
if needed and activate that draft in the dashboard. The next page request
shows a team created while the dashboard is already running. The page shows
it as a draft, not as an active team. Activation and rollback are dashboard
actions. The scripted result does not prove that the instructions work with a
live model or that an account is connected.

## Shared state

The dashboard and this server may use one state directory at the same time.
Each process opens its own SQLite connection on the thread that owns it.
`revisions.sqlite.init.lock` is held while a process migrates the file and
registers the shipped teams, so a second process waits instead of writing a
partial catalog. After that, SQLite WAL mode serializes writes. Draft saves
require the expected version, so a stale edit does not overwrite newer work.
Creation retries use the idempotency key. A field the tool does not accept is
rejected, and that error does not repeat the submitted text. Do not copy the
database file while either process has it open.

The drafting server writes `revisions.sqlite` only. It does not open workflow,
recovery, or connection files, and it does not copy spreadsheet assignments.

## Limits

The tools are `list_templates`, `list_teams`, `read_team`, `read_draft`,
`read_revision`, `create_team`, `open_draft`, `update_draft`,
`validate_draft`, `evaluate_draft`, `read_evaluation`, and `compare_draft`.
There is no tool for activation, rollback, discard, template reset, live
execution, approval, account connection, or arbitrary code. Annotations mark
reads and writes; the server still enforces those limits.

Evaluation waits at most 120 seconds. A timeout, cancellation, or error does
not store a pass. An edit after evaluation makes the older result not
activation-ready. `agentbuildkit://drafting/guide` repeats this for the client.
