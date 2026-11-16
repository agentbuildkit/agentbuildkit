# Team bundles (version 1)

A JSON bundle describes agents independently of provider clients. Validate its
structure without credentials or network access:

```sh
agentkit team validate examples/customer_service/team.json
```

Each agent declares its name, purpose, instructions, provider, model, input and
output schema names, and tool allowlist. Version 1 supports `openai` and
`anthropic`; agents can mix them. Unknown fields, empty required text, duplicate
agent names and duplicate tool references are rejected. Optional `id` and
`title` separate the stable team id from the name shown in the dashboard. When
they are omitted, both stay equal to `name`.

Applications load a bundle with `agentbuildkit.teams.load_bundle(path)` and call
`bundle.definitions(schemas=schemas, tools=tools)`. Both registries are explicit
application-owned mappings: schema names to Pydantic model classes, and tool
names to prepared framework tools. No module paths, code, credentials, or approval
settings are loaded from JSON. Missing bindings fail before definitions are
returned. Structural CLI validation does not check runtime bindings.

Pass each resulting definition to the existing `create_agent` with an injected
client and audit log. It retains the prepared-tool checks, allowlist, approval
middleware, and provider/model audit metadata. A bundle cannot turn a side effect
into a read. The application still owns routing, persistence, approval decisions,
and trace evaluation using the existing harness APIs.

The customer-service example loads `examples/customer_service/team.json` with
`load_bundle` and resolves it through `definitions`. Typed models and prepared
tools stay in the application. `build_team` passes those definitions to
`create_agent`. Routing, the approval workflow, SQLite persistence, audit, and
evaluation stay on the existing service. The local dashboard calls that service.
Recovered action arguments stay in a local file, not in checkpoints or the audit
ledger. A pending approval whose arguments were not recovered cannot be approved.
An interrupted reservation is not replayed. The store does not provide exactly-once
delivery to an external system.
