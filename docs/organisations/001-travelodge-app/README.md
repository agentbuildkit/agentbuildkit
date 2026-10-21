# Organisation 001 — Travelodge App

Proposed organisation. The reference customer-service team in `src/organisation` is still the code that runs. `src/agent_platform` is unchanged.

| Document | What it decides |
| --- | --- |
| [CLIENT_DISCOVERY.md](CLIENT_DISCOVERY.md) | Business input: one delivery process, systems, approvals, baseline. |
| [ORGANISATION_SPEC.md](ORGANISATION_SPEC.md) | Structure an engineer can implement: teams, agents, tools, routes, evaluation. |
| [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) | How this template expects an organisation to be created, the gaps, and the sequence. |

Read the plan before changing code. Implementation replaces `src/organisation` only after this specification is the one being built. It does not build the Travelodge mobile app and it does not change the platform.
