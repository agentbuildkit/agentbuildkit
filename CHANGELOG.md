# Changelog

## Unreleased

- Platform state version 2 stores a durable audit ledger, workflow runs, an organisation directory, and work items in the existing SQLite workflow file.
- A parent workflow can start a catalogued child only when an explicit delegation allows it. Short child graphs run through `WorkflowExecutor`. A parent can pause on a checkpoint until a typed child outcome arrives as a signal.
- Named gates issue bounded grants. `execute_tool` checks the stored grant before a governed side effect. Implementation, merge, and release stay separate permission classes. Grant use is ledgered, and a grant ends when it expires or the work item leaves the authorised state.
- Version 1 files remain readable for checkpoints and approvals. Runs, the ledger, and the directory stay unavailable until an explicit migration. Unknown versions are rejected. Opening an existing version 2 file adds the delegation, signal, gate, and grant tables without rewriting checkpoint payloads.
- GitHub repository binding uses the directory allowlist, bounded grants, and `execute_tool`. Implementation, merge, and release stay separate. The platform stores GitHub identifiers and content hashes, and publishes those outcomes as signals. GitHub App credentials stay on the authorization header.
- External engineering execution can delegate one implementation grant to a provider. Cursor Cloud Agents is the first provider. The platform stores the agent and run ids, then pauses. Completion and failure resume the run as signals. Cursor credentials stay on the authorization header. Pull requests, merges, and releases stay on the GitHub grants.

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
