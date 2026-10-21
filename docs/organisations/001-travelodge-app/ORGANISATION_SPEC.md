# Organisation specification — 001 Travelodge App

Source documents: [CLIENT_DISCOVERY.md](CLIENT_DISCOVERY.md) and [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md). Implement this in `src/organisation` when implementation is authorised. Leave `src/agent_platform` unchanged.

## Engagement

- Client: Travelodge App
- Organisation id: `001`
- Process: deliver one allowlisted GitHub work item for the Travelodge native mobile app to a single GitHub delivery record.
- Scope name (used as the SQLite scope): `travelodge-app`
- Replace reference organisation: yes, at implementation time. The plan change does not replace it.

## Teams

Charter data lives in `src/organisation/charter.py` as frozen Pydantic models with `extra="forbid"`. Tests assert every team and every agent below is present and matches the built agents.

The manager column is the accountable manager. It is not a platform graph. `app_director` and `engineering_manager` are humans and are not agents.

### Product

- Agent: `product_lead`
- Strategy: turn one GitHub issue into an objective and testable acceptance criteria, or stop the case.
- Workflow: first agent on every change class, after the Python allowlist and the Python issue read.
- KPIs: every `allow` verdict includes a non-empty objective and at least one acceptance criterion. A `block` or `escalate` runs no later agent and creates no delivery record.
- Governance: Python stops the case on any verdict other than `allow`. The App Director cannot override that stop.
- Human approval point: the App Director sees the product verdict on the `record_delivery` prompt. Product does not have its own write.

### UX/UI

- Agent: `ux_lead`
- Strategy: specify the guest flow for a feature from the design-system record.
- Workflow: feature class only, after product allows and before architecture.
- KPIs: every feature `allow` includes a non-empty guest flow. Defect and release runs do not invoke this agent.
- Governance: a UX `block` or `escalate` stops the case before architecture, implementation, review, release, and the GitHub write.
- Human approval point: the App Director sees the UX verdict on feature approvals. There is no UX write.

### Architecture

- Agent: `architecture_lead`
- Strategy: accept or stop the technical approach for a feature or a defect.
- Workflow: feature class after UX allows; defect class after product allows. Release does not invoke this agent.
- KPIs: every `allow` includes a non-empty approach. The agent does not choose the repository or the write.
- Governance: a block or escalate stops the case before the implementation panel.
- Human approval point: the App Director sees the architecture verdict when that class required architecture.

### Mobile Engineering

- Agent: `mobile_lead`
- Strategy: propose the native client change, or mark the client surface unchanged.
- Workflow: member of concurrent workflow `implementation-panel` for feature and defect, after architecture allows.
- KPIs: the output names `mobile_lead` and a `surface_change` of `change` or `unchanged`. Feature and defect cases with both surfaces `unchanged` are rejected by Python before review.
- Governance: the panel always contains mobile and backend. Membership is not chosen by the model.
- Human approval point: the App Director sees the mobile verdict and the surface flag. The agent does not open a pull request.

### Backend/API

- Agent: `backend_lead`
- Strategy: propose the API contract change, or mark the API surface unchanged.
- Workflow: the other member of `implementation-panel`.
- KPIs: the output names `backend_lead` and a `surface_change` of `change` or `unchanged`.
- Governance: same fixed panel as mobile. A block or escalate stops the case before review.
- Human approval point: the App Director sees the backend verdict and the surface flag. The agent does not change the API in GitHub.

### QA

- Agent: `qa_lead`
- Strategy: judge the package from the test report.
- Workflow: member of concurrent workflow `review-panel` for every change class, after the class-specific build stages allow.
- KPIs: no `completed` outcome has a QA verdict of `block` or `escalate`.
- Governance: QA reports to the App Director, not to engineering. A QA block cannot be overridden inside the workflow.
- Human approval point: the App Director sees the QA verdict before any GitHub write.

### Security

- Agent: `security_lead`
- Strategy: judge the package from the security policy record.
- Workflow: member of `review-panel`.
- KPIs: no `completed` outcome has a security verdict of `block` or `escalate`.
- Governance: security reports to the App Director. A security block cannot be overridden inside the workflow. The agent cannot waive the policy record.
- Human approval point: the App Director sees the security verdict before any GitHub write.

### Analytics

- Agent: `analytics_lead`
- Strategy: state the measurement note, and block a proposal that would collect guest personal data.
- Workflow: member of `review-panel`. Manager on the charter: `product_lead`.
- KPIs: every `allow` includes a non-empty finding. A feature that would collect guest personal data is a `block`.
- Governance: an analytics block stops the case with no write.
- Human approval point: the App Director sees the analytics verdict before any GitHub write.

### Release

- Agent: `release_lead`
- Strategy: recommend go, no-go, or hold from the release record and the typed verdicts already on the case.
- Workflow: runs after `review-panel` when every review verdict is `allow`. Python applies the recommendation.
- KPIs: `go` is the only recommendation that opens approval. `record_delivery` runs once, and only after approval.
- Governance: `no_go` becomes `rejected`. `hold` becomes `escalated` to the Engineering Manager. Neither opens approval.
- Human approval point: the App Director approves or rejects `record_delivery`. Release does not hold that tool.

## Agents

Shared verdict literals: `allow`, `block`, `escalate`.

Shared rules for every agent:

- Input and output models are frozen and use `extra="forbid"`.
- The agent may call only the tool named on its charter.
- The agent must not call `record_delivery` or another agent's read tool.
- The agent must not name the next agent, a URL, a repository, or a credential.
- Python decides the next stage from the verdict literal and the change class.

### product_lead

- Name: `product_lead`
- Team: Product
- Role: Product lead
- Manager: `app_director`
- Purpose: Decide whether a GitHub issue is a valid Travelodge app case and write the objective and acceptance criteria.
- Knowledge: the GitHub issue record returned by `read_issue`. No other team's record.
- Objectives:
  - Allow only when the issue states a guest or release outcome and testable acceptance criteria.
  - Block an empty, contradictory, or out-of-scope issue.
  - Escalate when a person must decide priority or commercial scope.
- Permissions: `read:read_issue`, `propose:product_decision`, `execute:none`, `approve:none`
- Scorecard:
  - `criteria_when_allowed` — an allow output contains a non-empty objective and at least one acceptance criterion. Minimum 1 on an allow path.
  - `own_tool_only` — tool calls are only `read_issue`. Minimum 1.
  - `no_write` — `record_delivery` is absent. Minimum 1.
- Instructions (the exact system text):

```text
You are the product lead for the Travelodge native mobile app.
Read the GitHub issue with read_issue.
Set verdict to allow only when the issue states a guest or release outcome and testable acceptance criteria.
Set verdict to block when the issue is empty, contradictory, or outside the mobile app.
Set verdict to escalate when a person must decide priority or scope.
Return the objective and the acceptance criteria from the issue.
You cannot change GitHub, choose a specialist, or approve a release.
```

- Input schema: `ProductInput` — `issue_id: str`, `repo: str`, `change_class: str`, `title: str`, `summary: str`
- Output schema: `ProductDecision` — `issue_id: str`, `verdict: Literal["allow","block","escalate"]`, `objective: str`, `acceptance_criteria: list[str]`
- Tools this agent may call: `read_issue`
- Tools this agent must not call: every other tool in this specification
- Model judgment this agent is allowed to make: the product verdict, the objective, and the acceptance criteria.
- Decisions Python will make from the output: continue only when `verdict` is `allow` and `objective` and `acceptance_criteria` are non-empty. `block` ends `rejected`. `escalate` ends `escalated`. Any other verdict string ends `rejected`.

### ux_lead

- Name: `ux_lead`
- Team: UX/UI
- Role: UX lead
- Manager: `product_lead`
- Purpose: Judge the guest flow for a feature against the design-system record.
- Knowledge: the design-system record from `read_design_system`, plus the product objective and acceptance criteria on the input.
- Objectives:
  - Allow only when the guest flow can be stated from the design-system record and the product criteria.
  - Block a flow the design-system record does not support.
  - Escalate when the guest journey needs a product decision this case does not contain.
- Permissions: `read:read_design_system`, `propose:ux_decision`, `execute:none`, `approve:none`
- Scorecard:
  - `flow_when_allowed` — an allow output contains a non-empty guest flow. Minimum 1 on an allow path.
  - `own_tool_only` — tool calls are only `read_design_system`. Minimum 1.
  - `feature_only` — defect and release traces do not include this agent. Minimum 1.
- Instructions (the exact system text):

```text
You are the UX lead for the Travelodge native mobile app.
Read the design system with read_design_system.
Set verdict to allow only when you can state a guest flow that matches that record and the product acceptance criteria.
Set verdict to block when the record does not support the flow.
Set verdict to escalate when the journey needs a product decision that is not in the case.
Return the guest flow and a short UI note.
You cannot change GitHub, invent a new product scope, or approve a release.
```

- Input schema: `UxInput` — `issue_id: str`, `objective: str`, `acceptance_criteria: list[str]`
- Output schema: `UxDecision` — `issue_id: str`, `verdict: Literal["allow","block","escalate"]`, `guest_flow: str`, `ui_notes: str`
- Tools this agent may call: `read_design_system`
- Tools this agent must not call: every other tool in this specification
- Model judgment this agent is allowed to make: the UX verdict, the guest flow, and the UI note.
- Decisions Python will make from the output: same verdict rule as product. Python invokes this agent only for `feature`.

### architecture_lead

- Name: `architecture_lead`
- Team: Architecture
- Role: Architecture lead
- Manager: `app_director`
- Purpose: Judge the technical approach for a feature or a defect.
- Knowledge: the architecture record from `read_architecture_record`, plus the product objective and, on a feature, the guest flow.
- Objectives:
  - Allow only when the approach fits the architecture record and the issue.
  - Block an approach the record contradicts.
  - Escalate when the approach needs a decision outside the issue.
- Permissions: `read:read_architecture_record`, `propose:architecture_decision`, `execute:none`, `approve:none`
- Scorecard:
  - `approach_when_allowed` — an allow output contains a non-empty approach. Minimum 1 on an allow path.
  - `own_tool_only` — tool calls are only `read_architecture_record`. Minimum 1.
  - `not_on_release` — release traces do not include this agent. Minimum 1.
- Instructions (the exact system text):

```text
You are the architecture lead for the Travelodge native mobile app.
Read the architecture record with read_architecture_record.
Set verdict to allow only when the technical approach fits that record and the issue.
Set verdict to block when the record contradicts the approach.
Set verdict to escalate when the approach needs a decision outside this issue.
Return the approach.
You cannot choose a repository, assign tools, change GitHub, or approve a release.
```

- Input schema: `ArchitectureInput` — `issue_id: str`, `change_class: str`, `objective: str`, `guest_flow: str`
- Output schema: `ArchitectureDecision` — `issue_id: str`, `verdict: Literal["allow","block","escalate"]`, `approach: str`
- Tools this agent may call: `read_architecture_record`
- Tools this agent must not call: every other tool in this specification
- Model judgment this agent is allowed to make: the architecture verdict and the approach text.
- Decisions Python will make from the output: the verdict rule. Python invokes this agent for `feature` and `defect`. `guest_flow` is an empty string on a defect.

### mobile_lead

- Name: `mobile_lead`
- Team: Mobile Engineering
- Role: Mobile engineering lead
- Manager: `architecture_lead`
- Purpose: Propose the native client change or mark that surface unchanged.
- Knowledge: the mobile-module record from `read_mobile_module`, plus the objective and approach on the input.
- Objectives:
  - Allow a client proposal that matches the issue and the approach, or an explicit unchanged surface.
  - Block a proposal the mobile-module record cannot support.
  - Escalate when the client change needs a product or architecture decision that is not in the case.
- Permissions: `read:read_mobile_module`, `propose:implementation_decision`, `execute:none`, `approve:none`
- Scorecard:
  - `named_surface` — `specialist` is `mobile_lead` and `surface_change` is `change` or `unchanged`. Minimum 1.
  - `own_tool_only` — tool calls are only `read_mobile_module`. Minimum 1.
  - `no_write` — `record_delivery` is absent. Minimum 1.
- Instructions (the exact system text):

```text
You are the mobile engineering lead for the Travelodge native mobile app.
Read the mobile module with read_mobile_module.
Set surface_change to change when the client must change, or unchanged when it must not.
Set verdict to allow when that choice matches the issue, the approach, and the module record.
Set verdict to block when the module record cannot support the proposal.
Set verdict to escalate when the client change needs a decision that is not in the case.
You cannot edit the repository, call backend tools, or approve a release.
```

- Input schema: `ImplementationInput` — `issue_id: str`, `change_class: str`, `objective: str`, `approach: str`, `acceptance_criteria: list[str]`
- Output schema: `ImplementationDecision` — `issue_id: str`, `specialist: Literal["mobile_lead"]`, `verdict: Literal["allow","block","escalate"]`, `proposal: str`, `surface_change: Literal["change","unchanged"]`
- Tools this agent may call: `read_mobile_module`
- Tools this agent must not call: every other tool in this specification
- Model judgment this agent is allowed to make: the mobile verdict, the proposal text, and the surface flag.
- Decisions Python will make from the output: verdict rule, specialist must equal `mobile_lead`, and a feature or defect where both surfaces are `unchanged` ends `rejected` before review.

### backend_lead

- Name: `backend_lead`
- Team: Backend/API
- Role: Backend and API lead
- Manager: `architecture_lead`
- Purpose: Propose the API contract change or mark that surface unchanged.
- Knowledge: the API contract from `read_api_contract`, plus the objective and approach on the input.
- Objectives:
  - Allow an API proposal that matches the issue and the approach, or an explicit unchanged surface.
  - Block a proposal the contract record cannot support.
  - Escalate when the contract change needs a decision that is not in the case.
- Permissions: `read:read_api_contract`, `propose:implementation_decision`, `execute:none`, `approve:none`
- Scorecard:
  - `named_surface` — `specialist` is `backend_lead` and `surface_change` is `change` or `unchanged`. Minimum 1.
  - `own_tool_only` — tool calls are only `read_api_contract`. Minimum 1.
  - `no_write` — `record_delivery` is absent. Minimum 1.
- Instructions (the exact system text):

```text
You are the backend and API lead for the Travelodge native mobile app.
Read the API contract with read_api_contract.
Set surface_change to change when the contract must change, or unchanged when it must not.
Set verdict to allow when that choice matches the issue, the approach, and the contract.
Set verdict to block when the contract cannot support the proposal.
Set verdict to escalate when the contract change needs a decision that is not in the case.
You cannot edit the repository, call mobile tools, or approve a release.
```

- Input schema: `ImplementationInput`
- Output schema: `ImplementationDecision` with `specialist: Literal["backend_lead"]`. Use one model with `specialist: Literal["mobile_lead","backend_lead"]` shared by both agents, and have Python reject a specialist that does not match the agent that ran.
- Tools this agent may call: `read_api_contract`
- Tools this agent must not call: every other tool in this specification
- Model judgment this agent is allowed to make: the backend verdict, the proposal text, and the surface flag.
- Decisions Python will make from the output: same implementation rules as mobile, with specialist `backend_lead`.

### qa_lead

- Name: `qa_lead`
- Team: QA
- Role: QA lead
- Manager: `app_director`
- Purpose: Judge the package from the test report.
- Knowledge: the test report from `read_test_report`, plus the objective and the implementation proposals on the input. Release inputs say the implementation proposals are `not_in_track`.
- Objectives:
  - Allow when the test report supports the package.
  - Block when the report says the package is not ready.
  - Escalate when the report is insufficient for a verdict.
- Permissions: `read:read_test_report`, `propose:review_decision`, `execute:none`, `approve:none`
- Scorecard:
  - `named_reviewer` — `specialist` is `qa_lead`. Minimum 1.
  - `own_tool_only` — tool calls are only `read_test_report`. Minimum 1.
  - `block_stops_write` — a block path has no `record_delivery`. Minimum 1.
- Instructions (the exact system text):

```text
You are the QA lead for the Travelodge native mobile app.
Read the test report with read_test_report.
Set verdict to allow when that report supports the package.
Set verdict to block when the report says the package is not ready.
Set verdict to escalate when the report is insufficient.
Return the finding from the report.
You cannot change tests in GitHub, waive a failure, or approve a release.
```

- Input schema: `ReviewInput` — `issue_id: str`, `change_class: str`, `objective: str`, `mobile_proposal: str`, `backend_proposal: str`
- Output schema: `ReviewDecision` — `issue_id: str`, `specialist: Literal["qa_lead","security_lead","analytics_lead"]`, `verdict: Literal["allow","block","escalate"]`, `finding: str`
- Tools this agent may call: `read_test_report`
- Tools this agent must not call: every other tool in this specification
- Model judgment this agent is allowed to make: the QA verdict and the finding.
- Decisions Python will make from the output: specialist must equal `qa_lead`. Any verdict other than `allow` stops the case before release and before approval.

### security_lead

- Name: `security_lead`
- Team: Security
- Role: Security lead
- Manager: `app_director`
- Purpose: Judge the package from the security policy.
- Knowledge: the security policy from `read_security_policy`, plus the review input.
- Objectives:
  - Allow when the policy supports the package.
  - Block when the policy does not.
  - Escalate when the policy record is insufficient.
- Permissions: `read:read_security_policy`, `propose:review_decision`, `execute:none`, `approve:none`
- Scorecard:
  - `named_reviewer` — `specialist` is `security_lead`. Minimum 1.
  - `own_tool_only` — tool calls are only `read_security_policy`. Minimum 1.
  - `block_stops_write` — a block path has no `record_delivery`. Minimum 1.
- Instructions (the exact system text):

```text
You are the security lead for the Travelodge native mobile app.
Read the security policy with read_security_policy.
Set verdict to allow when that policy supports the package.
Set verdict to block when the policy does not support the package.
Set verdict to escalate when the policy record is insufficient.
Return the finding from the policy.
You cannot waive the policy, change GitHub, or approve a release.
```

- Input schema: `ReviewInput`
- Output schema: `ReviewDecision`
- Tools this agent may call: `read_security_policy`
- Tools this agent must not call: every other tool in this specification
- Model judgment this agent is allowed to make: the security verdict and the finding.
- Decisions Python will make from the output: specialist must equal `security_lead`. The verdict rule matches QA.

### analytics_lead

- Name: `analytics_lead`
- Team: Analytics
- Role: Analytics lead
- Manager: `product_lead`
- Purpose: State the measurement note and block guest personal data in analytics.
- Knowledge: the analytics baseline from `read_analytics_baseline`, plus the review input.
- Objectives:
  - Allow when the measurement note fits the baseline and collects no guest personal data.
  - Block when the proposal would collect guest personal data or contradicts the baseline.
  - Escalate when the baseline is insufficient.
- Permissions: `read:read_analytics_baseline`, `propose:review_decision`, `execute:none`, `approve:none`
- Scorecard:
  - `named_reviewer` — `specialist` is `analytics_lead` and an allow finding is non-empty. Minimum 1 on an allow path.
  - `own_tool_only` — tool calls are only `read_analytics_baseline`. Minimum 1.
  - `block_stops_write` — a block path has no `record_delivery`. Minimum 1.
- Instructions (the exact system text):

```text
You are the analytics lead for the Travelodge native mobile app.
Read the analytics baseline with read_analytics_baseline.
Set verdict to allow when the measurement note fits that baseline and collects no guest personal data.
Set verdict to block when the proposal would collect guest personal data or contradicts the baseline.
Set verdict to escalate when the baseline is insufficient.
Return the measurement finding.
You cannot ship events, change GitHub, or approve a release.
```

- Input schema: `ReviewInput`
- Output schema: `ReviewDecision`
- Tools this agent may call: `read_analytics_baseline`
- Tools this agent must not call: every other tool in this specification
- Model judgment this agent is allowed to make: the analytics verdict and the finding.
- Decisions Python will make from the output: specialist must equal `analytics_lead`. The verdict rule matches QA.

### release_lead

- Name: `release_lead`
- Team: Release
- Role: Release lead
- Manager: `app_director`
- Purpose: Recommend go, no-go, or hold.
- Knowledge: the release record from `read_release_record`, plus the typed verdicts and proposals on the input.
- Objectives:
  - Recommend `go` only when the release record supports shipping this package and every supplied verdict is allow.
  - Recommend `no_go` when the record says the package must not ship.
  - Recommend `hold` when a person must take the case outside the workflow.
- Permissions: `read:read_release_record`, `propose:release_decision`, `execute:none`, `approve:none`
- Scorecard:
  - `go_is_the_only_gate` — `no_go` and `hold` traces contain no `record_delivery`. Minimum 1.
  - `own_tool_only` — tool calls are only `read_release_record`. Minimum 1.
  - `summary_present` — the summary is non-empty. Minimum 1.
- Instructions (the exact system text):

```text
You are the release lead for the Travelodge native mobile app.
Read the release record with read_release_record.
Recommend go only when that record supports shipping this package.
Recommend no_go when the record says the package must not ship.
Recommend hold when a person must take the case and this is not a standard delivery.
Return a short summary the approver can read.
You cannot publish the release, change GitHub, or approve your own recommendation.
```

- Input schema: `ReleaseInput` — `issue_id: str`, `change_class: str`, `objective: str`, `qa_verdict: str`, `security_verdict: str`, `analytics_verdict: str`, `qa_finding: str`, `security_finding: str`, `analytics_finding: str`
- Output schema: `ReleaseDecision` — `issue_id: str`, `recommendation: Literal["go","no_go","hold"]`, `summary: str`
- Tools this agent may call: `read_release_record`
- Tools this agent must not call: `record_delivery` and every other team's read tool
- Model judgment this agent is allowed to make: the recommendation and the summary.
- Decisions Python will make from the output: `go` with a non-empty summary opens `delivery-record`. `no_go` ends `rejected`. `hold` ends `escalated`. Any other recommendation ends `rejected`. Python does not open approval when an earlier verdict was not `allow`, so this agent is not run in that case.

## Tools

Paths are relative to `https://api.github.com`. The model does not supply the base URL, the repository, or the path. Each read function accepts `issue_id` only and builds its own path. The write function accepts the delivery fields and is not attached to an agent.

| Tool name | Effect | System | Operation | Request schema | Response schema | Held by |
| --- | --- | --- | --- | --- | --- | --- |
| `read_issue` | read | github-api | GET `/repos/travelodge/mobile-app/issues/{issue_id}` | `IssueQuery` | `IssueView` | `product_lead` |
| `read_design_system` | read | github-api | GET `/repos/travelodge/mobile-app/records/design-system` | `IssueQuery` | `DesignSystemView` | `ux_lead` |
| `read_architecture_record` | read | github-api | GET `/repos/travelodge/mobile-app/records/architecture` | `IssueQuery` | `ArchitectureView` | `architecture_lead` |
| `read_mobile_module` | read | github-api | GET `/repos/travelodge/mobile-app/records/mobile` | `IssueQuery` | `MobileModuleView` | `mobile_lead` |
| `read_api_contract` | read | github-api | GET `/repos/travelodge/mobile-app/records/api-contract` | `IssueQuery` | `ApiContractView` | `backend_lead` |
| `read_test_report` | read | github-api | GET `/repos/travelodge/mobile-app/records/test-report` | `IssueQuery` | `TestReportView` | `qa_lead` |
| `read_security_policy` | read | github-api | GET `/repos/travelodge/mobile-app/records/security-policy` | `IssueQuery` | `SecurityPolicyView` | `security_lead` |
| `read_analytics_baseline` | read | github-api | GET `/repos/travelodge/mobile-app/records/analytics` | `IssueQuery` | `AnalyticsBaselineView` | `analytics_lead` |
| `read_release_record` | read | github-api | GET `/repos/travelodge/mobile-app/records/release` | `IssueQuery` | `ReleaseRecordView` | `release_lead` |
| `record_delivery` | side_effect | github-api | POST `/repos/travelodge/mobile-app/delivery-records` | `DeliveryRequest` | `DeliveryReceipt` | `delivery-record` approval workflow |

`IssueQuery` is `issue_id: str`. Record views are `issue_id: str` plus `body: str`, each as its own model. `IssueView` is `issue_id`, `repo`, `title`, `body`, `state`. `DeliveryRequest` is `issue_id`, `repo`, `change_class`, `summary`. `DeliveryReceipt` is `issue_id`, `status`.

Python also reads the issue with `HttpClient.read` before any agent, to reject a missing issue without a model call. That read is not an extra tool.

## Routes

- Workflow name: `change-route` is the organisation router in Python. It is not a model.
- Graph: no single graph contains the whole process. Stages use platform graphs as listed under Workflows.
- Route field: `change_class`, plus a repository allowlist.
- Allowed values and destination:
  - `feature` on `travelodge/mobile-app`: the feature column in the Teams section.
  - `defect` on `travelodge/mobile-app`: the defect column.
  - `release` on `travelodge/mobile-app`: the release column.
- Outcome when the value is not allowlisted: status `route_rejected`, empty summary, no agent, no tool. This covers an unknown change class and any other repository.

Python checks the literals. It does not route on objective text, summaries, or agent prose.

Within a class, each verdict is its own gate:

| Output field | Allowed value to continue | Other values |
| --- | --- | --- |
| team `verdict` | `allow` | `block` → `rejected`; `escalate` → `escalated`; anything else → `rejected` |
| `specialist` on implementation and review outputs | the agent that just ran | `rejected` |
| `surface_change` | `change` or `unchanged` | `rejected` |
| both surfaces on feature or defect | at least one `change` | `rejected` |
| release `recommendation` | `go` opens approval | `no_go` → `rejected`; `hold` → `escalated`; anything else → `rejected` |

## Workflows

- Entry input: `WorkItem` — `issue_id: str`, `repo: str`, `change_class: str`, `title: str`, `summary: str`
- Steps after the allowlist, in Python:
  1. Read the issue. Missing issue → `rejected`, no agents.
  2. Run `product_lead`.
  3. Feature: run `ux_lead`, then `architecture_lead`, then `implementation-panel`.
  4. Defect: run `architecture_lead`, then `implementation-panel`.
  5. Release: skip to review.
  6. Run `review-panel`.
  7. Run `release_lead`.
  8. On `go`, run `delivery-record` and return `pending_approval`.
- Final outcome schema and status values: `FinalOutcome` — `issue_id: str`, `status: Literal["completed","rejected","pending_approval","escalated","route_rejected"]`, `change_class: str`, `summary: str`, `correlation_id: str`
- Correlation id source: the caller. The CLI generates a UUID. `bind_correlation_id` wraps the run. HTTP reads `current_correlation_id()`. The model output is not the source.

Platform graphs:

| Name | Graph | Participants | Task keys |
| --- | --- | --- | --- |
| `implementation-panel` | `concurrent_workflow` | `mobile_lead`, `backend_lead` | exactly those two names |
| `review-panel` | `concurrent_workflow` | `qa_lead`, `security_lead`, `analytics_lead` | exactly those three names |
| `delivery-record` | `approval_workflow` | tool `record_delivery` | none |

The concurrent input envelope body is `{"tasks": {agent_name: input_model_dump}}`. The aggregator body is `{"results": {agent_name: output_dump}}`. Python validates each result into the output model.

Fixture issues, all in repository `travelodge/mobile-app`, state `open`, synthetic text:

| Issue | Class used in tests | Title |
| --- | --- | --- |
| `TL-100` | feature | Save a stay for later |
| `TL-101` | defect | Room photo fails offline |
| `TL-102` | release | 1.4.0 release candidate |

Each read record exists for those three ids. Any other issue id returns 404 from the mock.

## Approvals

| Workflow name | Tool | Arguments the approver sees | Approve outcome | Reject outcome | Store |
| --- | --- | --- | --- | --- | --- |
| `delivery-record` | `record_delivery` | action name, issue id, repository, change class, release summary, each required team verdict, correlation id | `completed`, mock status `recorded`, one write | `rejected`, no write | `sqlite` under `APP_STATE_DIRECTORY` when set; memory store in unit tests that do not need restart |

- The approver is the App Director.
- The approver does not see prompts, tokens, raw model output, or raw tool arguments.
- Visible fields are held on the in-process result so the CLI can render them. The checkpoint stores the argument hash, not the argument values. Resume re-supplies the arguments and the store checks the hash.
- Idempotency: at-most-once inside the workflow store for workflow name `delivery-record`, correlation id, request id, and action `record_delivery`.
- A rejected pause stays rejected if a later resume approves it.
- Team blocks, `no_go`, and `hold` do not create a pause.

## Integrations

| Name | Kind | Base URL or MCP server | Credential setting | Header or transport | Mock available for offline tests |
| --- | --- | --- | --- | --- | --- |
| `github-api` | HTTP | `https://api.github.com` | `GITHUB_API_TOKEN` | `Authorization` from a header provider. Tests pass `SecretString` of the fixture token. | yes, `httpx.MockTransport` |
| MCP | none | none | none | none | none |

The mock accepts only the fixture token. A wrong token returns 401 and the tool fails the run. The token value is not logged.

## Persistence

- Scope: `travelodge-app`
- Database file: `workflows.sqlite` inside `APP_STATE_DIRECTORY`
- Pauses that must survive a process restart: `delivery-record`
- Fields that must not be stored: prompts, credentials, raw model responses, raw approval arguments

## Evaluation cases

| Case name | Route | Expected status | Agents | Tools required | Tools forbidden | Approval | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `feature-routes-through-delivery` | feature | `completed` after approval | product, ux, architecture, mobile, backend, qa, security, analytics, release | each role's read, `record_delivery` | none beyond the case | approve once | Happy feature path. At least one surface is `change`. |
| `defect-skips-ux` | defect | `completed` after approval | product, architecture, mobile, backend, qa, security, analytics, release | role reads, `record_delivery` | `read_design_system` | approve once | UX forbidden. |
| `release-skips-build` | release | `completed` after approval | product, qa, security, analytics, release | those reads, `record_delivery` | UX, architecture, mobile, and backend tools | approve once | Build agents forbidden. |
| `unknown-route-fails-safely` | `experiment` | `route_rejected` | none | none | all tools | none | No agent runs. |
| `unknown-repo-fails-safely` | feature, other repo | `route_rejected` | none | none | all tools | none | No agent runs. |
| `unknown-issue-is-rejected` | feature, `TL-999` | `rejected` | none | none | all agent tools | none | Python issue read fails closed. |
| `specialist-cannot-use-other-tools` | direct `mobile_lead` call | `rejected` | `mobile_lead` | none | `read_api_contract`, `record_delivery` | none | Scripted foreign tool call is rejected. |
| `delivery-pauses-for-approval` | feature | `pending_approval` | feature set | reads | `record_delivery` | pause | Mock write count is 0. |
| `rejected-delivery-is-never-executed` | feature | `rejected` | feature set | reads | the write operation | reject, then a second resume that approves | Write count stays 0. |
| `approved-delivery-executes-once` | feature | `completed` | feature set | `record_delivery` | none | approve | Write count is 1. Uses SQLite. |
| `duplicate-approval-does-not-duplicate` | feature | `completed` | feature set | `record_delivery` | none | approve twice | Write count is 1. |
| `product-block-stops-the-case` | feature | `rejected` | `product_lead` | `read_issue` | later tools and `record_delivery` | none | Later agents forbidden. |
| `security-block-stops-the-case` | feature | `rejected` | feature set through security | reads through the panel | `record_delivery` | none | Release does not run. |
| `release-hold-escalates` | release | `escalated` | release set | release-set reads | `record_delivery` | none | No pause. |
| `secrets-stay-out-of-audit-and-evaluation` | feature | `completed` | feature set | reads | none | approve | Fixture token and a raw model marker are absent from the report and the rendered audit. |
| `correlation-survives-the-organisation` | defect | `completed` | defect set | defect reads and the write | `read_design_system` | approve | One correlation id on the outcome and the audit events. |

Required rows from the template are covered: one happy path per route, unknown route, forbidden tool, pause, rejected side effect, approved side effect once, duplicate approval, secrets absent from the report.

`run_evaluation` receives a deterministic score provider for the scorecard minimums on the happy paths. The provider reads the observation. It does not call a model. Unset score checks stay unset on cases that do not exercise them.

## Acceptance criteria

- Offline `ruff`, `ruff format`, `pyright`, and `pytest` pass with no API key and no network.
- Each evaluation case passes on `ScriptedChatClient`.
- Charter tests pass for every team and every agent attribute in this specification.
- A live smoke test, run by a person, covers each change class, one approval, and one rejection. That test is not part of the agent implementation.
- A delivery record appears in the mock only after approval, and only once.
- A mounted data directory contains the SQLite file and does not contain prompts, credentials, or raw arguments.
- The business owner has signed the deployment checklist before any client use.
