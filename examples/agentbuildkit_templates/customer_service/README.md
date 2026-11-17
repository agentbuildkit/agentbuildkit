# Customer-service example

This package demonstrates an agent team built with AgentBuildKit. It is fixture data, not a connection to a CRM or a live customer-service backend.

- Alex classifies a typed request.
- Python routes that decision to Sam or Taylor, and rejects any other name.
- Sam reads account data. Taylor reads service data and may propose one action.
- Jordan approves, rejects, or escalates the draft.
- An informational answer is returned immediately.
- A service change pauses for a person and is stored. Approval runs it at most once.

The business system is an in-process HTTP mock at `http://business.test`. It does not call a network. `LOCAL_MOCK_TOKEN` is a fixture marker. The only customer reference is `A-100`, with fixture account name Northwind.

Each role declares its own provider and model. The runnable default uses `openai` and `gpt-4.1-mini` for every role, so a live run needs `OPENAI_API_KEY` only. `production_clients()` asks the provider registry for one client per role. Offline tests pass a `ScriptedChatClient` per role to `build_team(clients=...)` and do not construct a live client.

The default team asks the registry for an OpenAI client per role:

```python
from agentbuildkit_templates.customer_service import build_team, handle_request, production_clients

team = build_team(clients=production_clients(), api_token=token)
```

Switch Taylor to Anthropic without changing the workflow. Sam stays on OpenAI. That live client also needs `ANTHROPIC_API_KEY`.

```python
from agentbuildkit.providers import ProviderConfiguration
from agentbuildkit_templates.customer_service import build_team, production_clients

roles = {"taylor": ProviderConfiguration(provider="anthropic", model="claude-sonnet-4-5")}
team = build_team(clients=production_clients(roles=roles), api_token=token)
```

A local live run:

```bash
PYTHONPATH=examples uv run python -m customer_service
```

The same team can be opened in a local dashboard beside Operations Reporting. The selector chooses which team is on screen. The server still checks that a run or approval belongs to that team. SQLite files are opened on the dashboard's owner thread; browser requests only dispatch work to that thread. Set `APP_STATE_DIRECTORY` so a restarted process can recover pending approvals and the audit ledger. Opening an older database writes a backup copy, then keeps the existing customer-service rows under that team id. It does not delete the database. The customer reference, action arguments, and shown outcome text are stored beside that directory. They are not written to the audit ledger and are not exported telemetry. If those arguments were not recovered, approval does not run the action. If the process stops after the action is reserved and before completion is recorded, it stays unresolved and is not sent again. This is not exactly-once delivery to the business system.

```bash
PYTHONPATH=examples uv run python -m customer_service.dashboard
```

That module command is the checkout wrapper. After `agentbuildkit-templates` is installed from a local wheel, the same dashboard is `agentkit dashboard`. See [Install](../../docs/install.md). The published 0.1.0 package does not include it. The upcoming 0.2.0 install is documented there and is not published yet.

`APP_STATE_DIRECTORY` in `.env` overrides the same variable in the shell. `--state-directory` selects the directory for one process and leaves `.env` unchanged. Provider settings still come from `.env` and the process environment. A check with a fake `.env` pointed at a sentinel directory wrote the workflow, revision, recovery, and connection databases, and a revision migration backup, only in the directory named by the flag. The sentinel was unchanged. A shell assignment alone was not shown to select a temporary directory when `.env` already sets `APP_STATE_DIRECTORY`. Earlier preview processes started that way do not have an established storage location.

```bash
mkdir -p /tmp/agentbuildkit-try
PYTHONPATH=src:examples uv run python -m customer_service.dashboard --state-directory /tmp/agentbuildkit-try
```

Create team, at `/teams/new`, adds an inactive instance of one of the two registered templates, Customer Service or Operations Reporting. It does not generate a workflow or accept another template. The instance can take a request only after you validate the draft, run the offline evaluation, and activate that revision. The evaluation uses scripted clients. It does not measure a live model or show that a provider account or a spreadsheet connection is ready. Repeating the same creation request returns that team. A different name, description, template, or team id for the same request conflicts and does not change the original. Editing a draft versions that instance. Edit active version copies the revision in use. Reset from shipped template is a separate action. Editing one team does not create another team. A new instance has no spreadsheet assignment until one is saved for it. With `APP_STATE_DIRECTORY` or `--state-directory` set, drafts and published revisions stay in `revisions.sqlite` in that directory. Without it they last only until the process stops. Saving, validating, and the scripted suite do not activate a revision. The overview lists what needs attention (approvals waiting, and actions that failed or whose outcome is unknown), recent runs, the team, and the offline evaluation. A run page shows the route recorded for that request, its status history, the approval decision, and its audit events. The page shows only what the service, workflow store, recovery file, and audit ledger record: outcomes have no timestamps, and the customer's message is not stored. The styles, script, and fonts are served from `static/`; the fonts are Instrument Sans and Geist Mono under the SIL Open Font Licence.

A live run of the default team needs `OPENAI_API_KEY`. The model for each role is the model on that agent's definition.

Google Sheets is an optional connected read for Sam. Demo mode still answers account questions from fixture account A-100 and the name Northwind. Connected mode answers those questions from one spreadsheet id and one sheet range that you save on Sam's page. A missing connection, a failed read, or a result larger than the saved window stays an error. The server does not return a clipped sheet, and it does not return the fixture.

Local setup, still on this machine:

1. `uv sync --extra composio`. A normal `uv sync` does not install this extra.
2. Put `COMPOSIO_API_KEY` in `.env`. Leave it empty in `.env.example`. The dashboard reads it on the server and does not show it.
3. Set `APP_STATE_DIRECTORY` so the connection reference and assignments are kept in `connections.sqlite` beside the workflow database. Without that directory they last only for the process. Restart the dashboard after changing `.env`.
4. In Google Cloud, enable the Google Sheets API. On the OAuth consent screen, add only `https://www.googleapis.com/auth/spreadsheets.readonly` and add your Google account as a test user. Create a web OAuth client. Leave Authorized JavaScript origins empty. Set the authorized redirect URI to `https://backend.composio.dev/api/v3/toolkits/auth/callback`.
5. In Composio, create a custom Google Sheets auth config, not the managed Google app. The managed app cannot request the read-only Sheets scope. Use the same redirect URI, and request only `https://www.googleapis.com/auth/spreadsheets.readonly`. Put that config's `ac_` id in `COMPOSIO_GOOGLESHEETS_AUTH_CONFIG_ID`. Leave the placeholder empty in `.env.example`. Connect stays off until it is set.
6. Open the dashboard and use Connections. Connect Google Sheets. Composio hosts the sign-in and sends the browser back to `http://127.0.0.1:<port>/connections/callback`. The server checks that return against the browser session and the Composio account before saving a connection reference. It does not store the Google token. Before opening sign-in it reads the auth config metadata and requires the Google Sheets toolkit and only the read-only scope. A missing toolkit, custom-app flag, or scope list does not start sign-in.
7. On Sam's page, enter the spreadsheet id and one range that contains every column Sam should see, such as `Accounts!A1:C2`, then choose connected mode. `Accounts!A1:B20` does not include column C. The only read is `GOOGLESHEETS_BATCH_GET`, pinned to toolkit version `20261101_00`. The server rejects another spreadsheet, a range outside that window, and every write. The window is at most 100 rows, 26 columns, and 200 cells.

Limiting Sam to that read does not change the Google grant. The consent screen is the list you approve. Continue only when it asks to see spreadsheets for your own app. Stop if it asks to edit spreadsheets, to see or change Drive files, or to reach another Google product. An External app in Testing can show an unverified-app warning, and Google may expire that test grant after seven days. This local milestone does not submit the app for verification.

Connecting does not assign the account. Demo mode stays on Sam's page and is labelled separately. Removing the assignment on Sam's page clears only that window. Disconnect removes the local connection and every assignment that uses it, including another team's window. It does not revoke Google access. Connected mode does not answer from the demo fixture after that disconnect until you explicitly choose demo data. Sheet values are untrusted data and cannot change tools or instructions. There is no public or multi-user mode. Keep the dashboard on `127.0.0.1`. The page allows the https sign-in redirect that follows Connect, and it still refuses form posts to any other http host.
