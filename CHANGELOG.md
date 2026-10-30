# Changelog

## 0.1.0

Initial public release. This version is prepared for publication. It has not been published to PyPI or npm.

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
- A client playbook: discovery, specification, Cursor prompt, and deployment checklist.
