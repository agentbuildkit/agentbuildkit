# Changelog

## npm 0.0.2

- The bootstrapper stops reading the terminal before `agentkit demo`. An approval entered after a fresh install reaches the Python CLI.

## 0.2.0 (prepared, not published)

- Python packages `agentbuildkit` 0.2.0 and `agentbuildkit-templates` 0.2.0. The templates package requires `agentbuildkit>=0.2.0,<0.3.0` and still checks the harness capabilities. The published 0.1.0 harness does not satisfy that requirement.
- npm bootstrapper 0.0.3 installs both packages, runs diagnostics, explains the state directory, and can open the dashboard in the foreground. A harness-only tool install is upgraded. Composio and Foundry stay optional. This npm version is not published yet. The published npm package remains 0.0.2.
- Release validation builds the candidate once. Publication reuses that candidate and does not run from a branch push, pull request, merge, or GitHub Release.
- Release notes and the publication checklist are in `docs/releases/`.

## Unreleased

- `agentbuildkit-templates` packages Customer Service and Operations Reporting separately from the harness, under the import name `agentbuildkit_templates`. The templates dependency is `agentbuildkit>=0.2.0,<0.3.0`. Importing the package still checks for harness capabilities that the published `agentbuildkit` 0.1.0 package does not provide, and stops when they are missing. `agentkit dashboard` and `agentkit mcp serve` import that package by name. A locally built install can run version, doctor, the offline demo, the dashboard, and drafting without a checkout or `PYTHONPATH`. The dashboard does not construct a live provider or Composio client at startup. A live run fails clearly when the credential is missing. The revision startup lock still requires POSIX `flock` and reports that limit on other platforms. The published `agentbuildkit` 0.1.0 package and the published `npx agentbuildkit` package do not include the templates. A passing scripted suite does not prove live readiness.
- A repository checkout can serve local stdio MCP tools for drafting an inactive team, validating the exact draft, and running its scripted evaluation. The server uses the revision service, requires an absolute `--state-directory`, and uses POSIX file locking. It does not read `APP_STATE_DIRECTORY` or provider credentials. It does not activate, roll back, run, approve, or connect.
- A local operator can create another inactive instance of Customer Service or Operations Reporting. The installed team id is separate from the template id and the display name. Creation does not copy connections or activate a revision. The same idempotency key with the same normalized name, description, template, and team id returns that team and does not open another draft. A different name, description, template, or team id for that key is a conflict and leaves the original team unchanged. The team can run only after validation, offline evaluation, and explicit activation. Two instances of the same template keep separate runs, approvals, revisions, and spreadsheet windows. Installed teams are stored in `revisions.sqlite`. A version 1 file is backed up and opened as version 2. An unsupported file is left unchanged. `python -m customer_service.dashboard --state-directory PATH` opens that directory for one process. A value in `.env` still overrides a shell assignment of `APP_STATE_DIRECTORY`.
- A local operator can edit the active revision of Customer Service or Operations Reporting. The draft copies that revision. Resetting from the shipped template is a separate action and shows the difference. Validation and a scripted offline suite are bound to that exact revision. The suite does not measure a live model or prove that changed instructions are effective. Activation checks the current template again and does not publish a draft that no longer matches. Rollback is explicit and is refused when the old revision no longer matches the template. The revision is stored before a run starts, and a pending approval keeps it across a restart. This does not install another team. Drafts stay in `revisions.sqlite` when `APP_STATE_DIRECTORY` is set, and in memory otherwise. Existing workflow, recovery, and connection databases are not migrated. An unsupported revision database is left unchanged.
- The customer-service example builds its agents from `team.json` through the team bundle loader.
- A local dashboard runs that team, records approval decisions, and shows the audit log and evaluation report.
- A restarted dashboard recovers a pending approval, its customer reference, and its action arguments. An uncertain service action is not run again. Approval does not run when those arguments were not recovered.
- The dashboard is redesigned around everyday operation: a needs-attention list, recent runs, and a run page with the recorded route, status history, approval decision, and filterable audit timeline. Approvals, Audit, Evaluation, and Team details have their own pages.
- The dashboard names failed and unresolved service actions separately, from the stored effect status. Execution and approval behaviour are unchanged.
- Instrument Sans and Geist Mono are bundled under `examples/agentbuildkit_templates/customer_service/static/fonts` with their SIL Open Font Licence files. The dashboard needs no network access and sends a restrictive Content-Security-Policy.
- A run's route diagram comes from that team's registered workflow. Recorded stages come from the run's audit events. Operations Reporting shows Request, Analyst, Reviewer, and Report.
- One local dashboard can run Customer Service and Operations Reporting. Each team has its own id, runs, approvals, audit, and spreadsheet assignment. A request or approval from one team is rejected on the other team's route. Removing an assignment clears only that team and agent. Disconnecting the Google account removes every assignment that uses it and does not revoke Google access. This is one local operator, not hosted multi-user tenancy. Older connection and recovery rows are kept and marked as customer-service after a backup copy is written. The dashboard opens those SQLite files on its owner thread.
- The customer-service dashboard can connect one Google Sheets account through a custom Composio auth config limited to `spreadsheets.readonly`. Sam's connected mode reads one saved spreadsheet and range with `GOOGLESHEETS_BATCH_GET`. A read outside that window, a write, or an oversized result is rejected. Demo mode stays labelled. A missing connection does not return the demo fixture. Disconnect removes the local connection and does not revoke Google access. The Composio key stays in the server environment. Connect does not use Composio's managed Google app.

## 0.1.0

Initial public release.

- Python package `agentbuildkit` 0.1.0 and command `agentkit`.
- Per-agent OpenAI and Anthropic provider and model selection. One workflow can mix both. A scripted client remains available for tests and the offline demo.
- Typed agent inputs and outputs.
- Deterministic routing, per-agent tool allowlists, and read versus side-effect classification.
- Human approval before a side effect, SQLite pause and resume, at-most-once side-effect execution, and persisted rejections.
- Audit events that record the provider and model and omit credentials, plus an offline evaluation runner.
- Governed GitHub repository operations and one external-execution grant for Cursor. Credentials stay on the authorization header.
- `agentkit demo`, an offline run of that harness. It needs no API key.
- Node bootstrapper `agentbuildkit` 0.0.1. `npx agentbuildkit` installs the Python CLI with uv into an isolated tool environment. Installing uv requires a prompt, or `--install-uv` when the terminal is not interactive.
- Apache License 2.0.

## 1.0.0

Earlier reusable release of the agent organisation template, before this product name.

- Typed agent boundaries, with Pydantic inputs and outputs.
- Deterministic orchestration for routing, review, and aggregation.
- Governed tools, classified as read or side effect.
- Human approval before a side effect runs, with at-most-once resume inside the workflow store.
- Durable workflow state in SQLite, mounted as a volume in the container.
- Allowlisted HTTP and MCP integrations. Credentials stay in configuration.
- An offline evaluation framework for organisation-defined cases.
- OpenTelemetry export, correlation ids, and audit events that omit prompts and secrets.
- A fictional B2B customer-service organisation as the working example.
- A non-root Docker image that runs the existing command.
