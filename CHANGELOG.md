# Changelog

## Unreleased

- Platform state version 2 stores a durable audit ledger, workflow runs, an organisation directory, and work items in the existing SQLite workflow file.
- Version 1 files remain readable for checkpoints and approvals. Runs, the ledger, and the directory stay unavailable until an explicit migration. Unknown versions are rejected.

## 1.0.0

First reusable release of the agent organisation template.

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
