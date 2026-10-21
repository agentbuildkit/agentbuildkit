# Client discovery — Organisation 001, Travelodge App

Filled from the engagement brief for the first organisation on this template. Named people, volumes, and cost were not supplied. Those sections stay unset rather than estimated. "None" means this plan records an empty confirmed scope.

## Organisation

- Client: Travelodge App
- Business unit: Native mobile application (guest iOS and Android app)
- Engagement owner: unset — role is App Director until a person is named on the deployment checklist
- Business owner: App Director (human). This role approves the one GitHub write.
- Technical owner: unset — the platform technical owner remains the owner of `src/agent_platform`; the organisation technical owner signs the organisation specification at implementation time

Organisation id: `001`. SQLite scope to use at implementation: `travelodge-app`.

## Business objective

- Outcome this organisation must produce: a governed delivery decision for one Travelodge mobile-app work item, recorded in GitHub only after a human approves, and only once.
- Process in scope: take one allowlisted GitHub issue for `travelodge/mobile-app` through product, the teams that the change class requires, a fixed review panel, and a release recommendation.
- Processes explicitly out of scope:
  - Building, compiling, or shipping the native mobile application.
  - Hotel operations: bookings, payments, property management, guest support.
  - App Store or Play Store submission.
  - Merging code, pushing commits, or editing repository files.
  - A second runtime process (incident response, continuous browsing of the codebase, an always-on improvement loop).
  - Changes to `src/agent_platform`.

## Current process

This is the target operating process for organisation 001. A live Travelodge delivery process was not observed in this engagement, so the steps below are the process the organisation will run, grounded in the template's rules.

- Trigger: a caller submits one work item that names a GitHub issue, the repository, a change class, a title, and a summary.
- Steps, in order:
  1. Python checks the repository and the change class against the allowlist. Anything else stops as `route_rejected` before any agent or tool runs.
  2. Python reads the issue from the GitHub allowlist. A missing issue stops as `rejected` before any agent runs.
  3. Product reads the issue and writes a verdict, an objective, and acceptance criteria.
  4. Feature work then goes to UX, then architecture. Defect work goes to architecture and skips UX. Release work skips UX, architecture, and implementation.
  5. Feature and defect work runs the mobile lead and the backend lead together. Each must say whether their surface changes.
  6. QA, security, and analytics review every class together.
  7. Release recommends go, no-go, or hold.
  8. Go, with every prior verdict allow, pauses for the App Director. Approval runs the GitHub delivery record once.
- Handoff between people: agents hand typed verdicts to Python. Python is the only router. The App Director is the only person in the loop, and only at the GitHub write.
- Where the case ends: a typed outcome — `completed`, `rejected`, `pending_approval`, `escalated`, or `route_rejected` — with the issue id and the correlation id.
- Where work waits today: on the App Director's approval of `record_delivery`. The pause is stored in SQLite under scope `travelodge-app`.

## People and roles

Human roles are not agents. Agent roles are specified in [ORGANISATION_SPEC.md](ORGANISATION_SPEC.md).

| Role | Person or team | Decision they make | Systems they use |
| --- | --- | --- | --- |
| App Director | Human, person unset | Approve or reject `record_delivery`. Cannot override a team block, a no-go, or a hold. | Approval prompt: issue id, repository, change class, release summary, each team verdict, correlation id |
| Engineering Manager | Human, person unset | Takes escalated cases outside this workflow. No tool and no write. | The typed outcome and correlation id |
| Product lead | Agent `product_lead` | Allow, block, or escalate the issue as a product case. Writes the objective and acceptance criteria. | GitHub issue read |
| UX lead | Agent `ux_lead` | Allow, block, or escalate the guest flow for a feature. | Design-system read |
| Architecture lead | Agent `architecture_lead` | Allow, block, or escalate the technical approach for a feature or defect. | Architecture-record read |
| Mobile lead | Agent `mobile_lead` | Allow, block, or escalate the client proposal, and mark the client surface changed or unchanged. | Mobile-module read |
| Backend lead | Agent `backend_lead` | Allow, block, or escalate the API proposal, and mark the API surface changed or unchanged. | API-contract read |
| QA lead | Agent `qa_lead` | Allow, block, or escalate from the test report. | Test-report read |
| Security lead | Agent `security_lead` | Allow, block, or escalate from the security policy. | Security-policy read |
| Analytics lead | Agent `analytics_lead` | Allow, block, or escalate the measurement note, including a block when the proposal would collect guest personal data. | Analytics-baseline read |
| Release lead | Agent `release_lead` | Go, no-go, or hold. | Release-record read |

## Volumes, cost, and SLA

- Cases per day / week: unset
- Time per case: unset
- Cost per case, if known: unset
- SLA: a paused approval is a business wait of 5 business days. The platform has no approval timer. A missed window is handled by a person outside the workflow.
- Current SLA attainment: none
- Hours the process must run: on demand, when a person runs the organisation command. There is no scheduler.

## Pain points

- A mobile change can be proposed without a recorded product objective, a security verdict, or a release decision.
- A write to GitHub must be attributable to a person and must not repeat when approval is submitted twice.
- The source of truth for the work item and the delivery decision is GitHub. Agent chat is not a record.

## Systems

| System | What it holds | Identifier used | Read / write | Owner |
| --- | --- | --- | --- | --- |
| GitHub | Issues, design-system record, architecture record, mobile module record, API contract, test report, security policy, analytics baseline, release record, delivery record | Repository `travelodge/mobile-app` and issue id `TL-100`, `TL-101`, or `TL-102` in the offline fixture | Read of those records. One write: create a delivery record. | App Director for the write. Repository admin for the real repository, unset. |

No other system is in scope. The mobile app binary, store consoles, and hotel systems are out of scope.

## APIs and MCP

| System | Interface | Base URL or server | Auth available | Contact |
| --- | --- | --- | --- | --- |
| GitHub | HTTP | `https://api.github.com` | `GITHUB_API_TOKEN`, header provider, empty in `.env.example` when implementation adds the setting | unset |
| MCP | none | none | none | none |

Offline tests use an in-process `httpx.MockTransport`. They do not call GitHub. The fixture token is `LOCAL_MOCK_TOKEN` and must not appear in logs, audit exports, or evaluation reports.

## Data sensitivity

- Data classes involved: issue text, design notes, architecture notes, module and contract summaries, test status, security policy summary, anonymous analytics event names, release metadata. All fixture content is synthetic.
- Fields that must never enter logs, prompts, or checkpoints: `GITHUB_API_TOKEN`, `OPENAI_API_KEY`, authorization headers, prompts, raw model responses, raw tool arguments. Guest personal data, payment data, and passwords are not inputs to this process.
- Retention limit: workflow state stays in `workflows.sqlite` on the mounted state directory. Audit export follows the collector behind `APP_OTLP_ENDPOINT` when a person sets it. The organisation does not add a retention service.
- Residency or tenant constraint: one repository, `travelodge/mobile-app`. The model cannot supply a URL, a repository, or a credential.

## Decisions

### Requiring judgment

- Whether the issue states a guest outcome and testable acceptance criteria.
- Whether a feature's guest flow matches the design-system record.
- Whether the technical approach is coherent for a feature or defect.
- Whether the client or API proposal matches the issue.
- Whether the test report, security policy, and analytics baseline allow the change.
- Whether the package should go, stop, or wait.

### Deterministic rules

- Repository must be `travelodge/mobile-app`.
- Change class must be `feature`, `defect`, or `release`.
- Issue id must be present in the GitHub fixture (offline: `TL-100`, `TL-101`, `TL-102`).
- Feature runs UX and architecture. Defect runs architecture and skips UX. Release skips UX, architecture, mobile, and backend.
- Feature and defect always run both implementation agents. A panel member is never omitted.
- QA, security, and analytics always run together after the earlier gates allow.
- Any team verdict other than `allow` stops the case. `block` completes as `rejected`. `escalate` completes as `escalated`.
- Release `no_go` completes as `rejected`. Release `hold` completes as `escalated`.
- `go` opens the approval pause only when every required verdict is `allow`.
- A team block, a no-go, and a hold do not open the approval pause.
- The App Director's approval is required before `record_delivery` runs.
- A second approval of the same pause does not run the write again.
- Unknown routes and unknown repositories call no agent and no tool.

### Permitted AI decisions

- The verdicts and the written fields on that agent's output model.
- Which of its own read tools to call. Each agent has one.

### Prohibited AI decisions

- The next agent, the route, or whether a stage is skipped.
- Repository, URL, credential, header, or tool effect.
- Executing `record_delivery` or any other write.
- Overriding another team's verdict.
- Treating free text as a route.

## Side effects

| Action | System changed | Reversible | Who may approve |
| --- | --- | --- | --- |
| `record_delivery` | GitHub delivery record on `travelodge/mobile-app` | A person can close or revert the record in GitHub outside this workflow. The workflow itself will not delete it. | App Director |

Agents do not hold this tool. The `delivery-record` approval workflow holds it.

## Approval owners and escalation

- Approver for each side effect: App Director for `record_delivery`. No other side effect exists.
- What the approver must see: action name, issue id, repository, change class, release summary, every team verdict required for that class, correlation id.
- Rejection outcome: status `rejected`, GitHub unchanged, and a later approval of that same pause still does not write.
- Escalation path: status `escalated` to the Engineering Manager. GitHub unchanged. The workflow does not continue.
- Hours an approval may wait: 5 business days as a business rule. The store does not expire the pause.

## Success and baseline

Baseline figures were not supplied. Targets below are properties the offline suite can prove. They are not claimed improvements over a measured live process.

| Metric | Baseline today | Target | How it will be measured |
| --- | --- | --- | --- |
| Cases with an allowlisted route or a safe rejection | none | Every unknown class or repository ends `route_rejected` with no tool call | Evaluation case |
| Delivery records created without approval | none | Zero | `no_unapproved_write` on every case |
| Duplicate delivery records for one approval | none | Zero | Duplicate-approval case asserts one mock write |
| Completed case with a blocking team verdict | none | Zero | Block cases assert status `rejected` and no write |
| Secrets in the evaluation report or audit text | none | Zero | Secrets case |

- Expected ROI: unset
- Time window for the comparison: the first live smoke test a person runs after implementation. Not part of this plan.

## Regulatory and security constraints

- Regulation or policy: the security-policy record and the analytics baseline are the only policy texts the agents may read. Analytics proposals that would collect guest personal data are a block. Store-listing law and hotel regulation are out of scope.
- Identity of the caller: the person who runs the organisation command. The platform does not add authentication.
- Secrets owner: the person who places `OPENAI_API_KEY` and `GITHUB_API_TOKEN` in the host environment. The image does not contain `.env`.
- Environments (local, test, production): local offline tests with the mock; a live smoke test is a person's later step. No production deployment is authorised by this plan.
- Audit retention: platform audit events for the correlation id. Payloads omit prompts, credentials, raw model responses, and raw tool arguments.
