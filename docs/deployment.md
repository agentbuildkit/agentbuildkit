# Deployment checklist

Use this before running AgentBuildKit or the customer-service example. The example backend is fixture data. Check each item before a deployment.

## Secrets

- [ ] `.env` is present only on the host that will run the process.
- [ ] `.env` is not committed, not copied into the image, and not mounted as a file inside the container.
- [ ] `OPENAI_API_KEY` is set in the process environment or via `docker run --env-file`.
- [ ] Business-system credentials are passed through settings or a header provider.

## Environment

- [ ] `APP_SERVICE_NAME` and `APP_ENVIRONMENT` name this deployment.
- [ ] `APP_ENABLE_CONSOLE_EXPORTERS` is false unless a named person is debugging on a private machine.
- [ ] `APP_OTLP_ENDPOINT` points at the collector, or is empty.
- [ ] Sensitive telemetry remains off.

## Persistence

- [ ] `APP_STATE_DIRECTORY` is set. A value in `.env` overrides the shell. A process that must use another directory passes `--state-directory`. That flag was verified to receive every database and a revision migration backup. A shell assignment alone was not shown to do so when `.env` already sets the variable.
- [ ] That directory is a mounted volume, and the container filesystem is not the store.
- [ ] `revisions.sqlite` stays on that volume beside the workflow, recovery, and connection files. Deleting it drops installed teams and published revisions. It does not hold credentials or spreadsheet assignments.
- [ ] Opening a version 1 revision file writes a backup, then adds the installed-team table and marks the file version 2. Existing revisions stay. An unsupported file is left unchanged after its backup copy is written. Do not replace it with an empty file to force a reset.
- [ ] A restart still lists a pending approval that was paused before the restart, on the revision that run started with.
- [ ] A second approval of the same request does not repeat the side effect.

## Model

- [ ] Each agent declares its own provider and model. The example roles declare `openai` and `gpt-4.1-mini`.
- [ ] `OPENAI_API_KEY` is set for a live OpenAI agent. `ANTHROPIC_API_KEY` is set only when a role uses Anthropic.
- [ ] Foundry is installed only when the deployment is configured to use it.

## Tool permissions

- [ ] Every tool is classified read or side effect.
- [ ] Each agent holds only the tools in the specification.
- [ ] HTTP base URLs and MCP servers match the allowlist. No model-supplied URL, command, or header.

## Approvals

- [ ] Each side effect has a named approver.
- [ ] The approval prompt shows the action, the business identifier, and the correlation id.
- [ ] Rejection leaves the business system unchanged.

## Evaluation

- [ ] The offline suite pass rate is 1.0.
- [ ] Record the required pass rate: ______.
- [ ] A failing case blocks release.

## Offline tests

- [ ] `uv run ruff check .`
- [ ] `uv run ruff format --check .`
- [ ] `uv run pyright`
- [ ] `uv run pytest`
- [ ] `docker build` succeeded.
- [ ] `python -m customer_service.health` inside the image exited 0 and did not call a model.

## Live smoke tests

- [ ] One request per route returned a typed outcome and a correlation id.
- [ ] One side effect paused, was approved, and completed once.
- [ ] One side effect was rejected and did not run.
- [ ] The SQLite file on the volume does not contain prompts, credentials, raw model responses, or raw arguments.

## Logging and audit

- [ ] Audit events exist for the smoke-test correlation ids.
- [ ] Logs and traces show no API key, authorization header, prompt, or raw tool arguments.

## Rollback

- [ ] The previous image tag or git revision is recorded: ______.
- [ ] The data volume is retained across the rollback.
- [ ] The team knows that an in-flight approval resumes only when the workflow definition still matches the stored hash.

## Sign-off

- [ ] The smoke-test outcomes were reviewed.
- [ ] Name, date, and decision:
