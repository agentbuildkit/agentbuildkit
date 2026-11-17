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

Drafts version the two installed teams, Customer Service and Operations
Reporting. They do not install another team or another copy of a team. Edit
active version copies the revision that is active now. Reset from shipped
template is a separate action. It does not replace an open draft unless that
action is submitted, and the editor then shows how the template differs from
the active revision. An open draft is otherwise left as it is.

A draft edits only the display name, description, purpose, instructions, provider,
model, and tool selection of a registered template. Role ids, schemas, approval
policy, and workflow code stay in the application. Published revisions are
immutable. The content hash binds validation and the scripted offline suite to
one draft version. Saving or evaluating does not activate. Activation checks
the current template again and does not publish a draft that no longer matches.
Activation and rollback append an audit event that stores hashes and revision
ids, not instruction text. Those two writes share one database transaction.
The revision is recorded before a run starts, so a pending approval keeps it
after a restart. Rollback is refused when that revision no longer matches the
current template, and the active pointer stays where it is.

The scripted suite does not measure a live model and does not prove that
changed instructions are effective. A missing provider key is reported and is
not printed. A pattern check can refuse text that looks like a credential and
does not store that text. It does not prove that other wording contains no
secret. Spreadsheet assignments are checked again at execution and are not
restored by rollback. Set `APP_STATE_DIRECTORY` to keep drafts and revisions
in `revisions.sqlite` beside the other local databases. Without that directory
they last until the process stops. The revision database is new. An unsupported
`revisions.sqlite` is copied to a backup and left unchanged. Older workflow,
recovery, and connection files are not rewritten.

The customer-service example loads `examples/customer_service/team.json` with
`load_bundle` and resolves it through `definitions`. Typed models and prepared
tools stay in the application. `build_team` passes those definitions to
`create_agent`. Routing, the approval workflow, SQLite persistence, audit, and
evaluation stay on the existing service. The local dashboard calls that service.
Recovered action arguments stay in a local file, not in checkpoints or the audit
ledger. A pending approval whose arguments were not recovered cannot be approved.
An interrupted reservation is not replayed. The store does not provide exactly-once
delivery to an external system.
