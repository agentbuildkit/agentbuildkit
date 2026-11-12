"""HTML for the local dashboard. Every value is escaped; nothing here changes state.

The pages read a ``Snapshot`` built by ``DashboardService``. Forms post to the
existing ``/run``, ``/decide`` and ``/evaluate`` handlers.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from html import escape

from agentbuildkit.evaluation import EvaluationReport
from agentbuildkit.workflows import PendingRequest
from customer_service.dashboard_model import (
    STATES,
    AuditItem,
    Category,
    NodeState,
    Pause,
    Route,
    RouteNode,
    Run,
    State,
    build_route,
    clock,
    day_and_time,
    link_on,
    outcome_state,
)


@dataclass(frozen=True)
class AgentInfo:
    """One agent as the team bundle declares it."""

    name: str
    role: str
    job: str
    provider: str
    model: str
    input_schema: str
    output_schema: str
    tools: tuple[str, ...]
    instructions: str


@dataclass(frozen=True)
class Snapshot:
    """Everything one page needs, read on the database owner thread."""

    team_name: str
    agents: tuple[AgentInfo, ...]
    runs: tuple[Run, ...]
    approvals: tuple[Pause, ...]
    audit: tuple[AuditItem, ...]
    report: EvaluationReport | None
    error: str
    draft: tuple[str, str] | None
    persistent: bool


@dataclass(frozen=True)
class Page:
    status: int
    body: str


EXAMPLES: tuple[tuple[str, str, str], ...] = (
    (
        "Explain my latest invoice",
        "Usually Sam",
        "Can you explain the charges on my latest invoice?",
    ),
    (
        "Is my service ticket still open?",
        "Usually Taylor",
        "Is my service ticket still open, and what happens next?",
    ),
    (
        "Book an engineer visit",
        "May need approval",
        "Our engineer didn't turn up today. Can someone visit tomorrow morning?",
    ),
)
_CORRELATION = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_CATEGORY_LABELS: dict[Category, str] = {
    "agent": "Agents",
    "tool": "Tools",
    "approval": "Approvals",
    "effect": "Side effects",
    "workflow": "Workflow",
}
_RECENT = 10


def e(value: object) -> str:
    return escape(str(value), quote=True)


# Icons: one stroke set, 16px grid.
_ICONS = {
    "plus": '<path d="M8 3v10M3 8h10"/>',
    "chev": '<path d="M6 3.5 10.5 8 6 12.5"/>',
    "back": '<path d="M10 3.5 5.5 8 10 12.5"/>',
    "clock": '<circle cx="8" cy="8" r="6"/><path d="M8 4.8V8l2.2 1.4"/>',
    "check": '<circle cx="8" cy="8" r="6"/><path d="m5.4 8.2 1.8 1.8 3.4-3.6"/>',
    "x": '<circle cx="8" cy="8" r="6"/><path d="m6 6 4 4M10 6l-4 4"/>',
    "alert": '<path d="M8 2.5 14 13H2z"/><path d="M8 6.5v3M8 11.3v.2"/>',
    "help": (
        '<circle cx="8" cy="8" r="6"/>'
        '<path d="M6.4 6.3a1.7 1.7 0 0 1 3.2.6c0 1.1-1.6 1.4-1.6 2.4M8 11.3v.2"/>'
    ),
    "person": (
        '<circle cx="8" cy="5.5" r="2.5"/>'
        '<path d="M3.5 13.5c.6-2.4 2.3-3.6 4.5-3.6s3.9 1.2 4.5 3.6"/>'
    ),
    "slash": '<circle cx="8" cy="8" r="6"/><path d="m4 12 8-8"/>',
    "info": '<circle cx="8" cy="8" r="6"/><path d="M8 7.3v4M8 4.8v.2"/>',
    "home": '<path d="M2.5 7 8 2.5 13.5 7v6.5h-11z"/><path d="M6.5 13.5v-4h3v4"/>',
    "list": '<path d="M5.5 4h8M5.5 8h8M5.5 12h8M2.5 4h.6M2.5 8h.6M2.5 12h.6"/>',
    "shield": (
        '<path d="M8 2 13 4v4c0 3-2.2 5.2-5 6-2.8-.8-5-3-5-6V4z"/><path d="m5.8 8 1.5 1.5 3-3"/>'
    ),
    "log": (
        '<rect x="3" y="2.5" width="10" height="11" rx="1.5"/>'
        '<path d="M5.5 6h5M5.5 8.5h5M5.5 11h3"/>'
    ),
    "flask": (
        '<path d="M6 2.5h4M6.8 2.5v4L3.5 12.3a.9.9 0 0 0 .8 1.2h7.4a.9.9 0 0 0 .8-1.2L9.2 6.5v-4"/>'
    ),
    "team": (
        '<circle cx="5.5" cy="6" r="2"/><circle cx="11" cy="6.5" r="1.6"/>'
        '<path d="M2 13c.4-2 1.8-3 3.5-3s3.1 1 3.5 3'
        'M9.5 12.8c.3-1.5 1.2-2.3 2.4-2.3 1.1 0 2 .8 2.3 2.3"/>'
    ),
    "disk": (
        '<rect x="2.5" y="2.5" width="11" height="11" rx="1.5"/>'
        '<path d="M5 2.5v3.5h6V2.5M5 13.5V9.5h6v4"/>'
    ),
    "close": '<path d="m4 4 8 8M12 4l-8 8"/>',
}

_LOGO = (
    '<svg width="22" height="22" viewBox="0 0 256 256" aria-hidden="true">'
    '<path fill="#1D2733" fill-rule="evenodd" d="M78.08 84 L177.92 84 L156 24 L100 24 Z '
    "M59.08 136 L113.08 136 L128 95.16 L142.92 136 L196.92 136 L181.58 94 L74.42 94 Z "
    'M24 232 L78 232 L93.35 190 L39.35 190 Z M216.65 190 L162.65 190 L178 232 L232 232 Z"/>'
    '<path fill="#2B4CF2" d="M159 180 L213 180 L200.58 146 L55.42 146 L43 180 L97 180 Z"/></svg>'
)


def icon(name: str, size: int = 16, extra: str = "") -> str:
    cls = f"ic {extra}".strip()
    return (
        f'<svg class="{cls}" width="{size}" height="{size}" viewBox="0 0 16 16" fill="none" '
        'stroke="currentColor" stroke-width="1.5" stroke-linecap="round" '
        f'stroke-linejoin="round" aria-hidden="true">{_ICONS[name]}</svg>'
    )


def status(state: State, label: str | None = None) -> str:
    style = STATES[state]
    return (
        f'<span class="st tone-{style.tone}">{icon(style.icon, 15)}{e(label or style.label)}</span>'
    )


def short(correlation_id: str) -> str:
    return correlation_id[:8]


def run_href(correlation_id: str) -> str:
    return f"/runs/{e(correlation_id)}"


def title_name(name: str) -> str:
    return name.title() if name else ""


# ---------------------------------------------------------------- layout

_NAV: tuple[tuple[str, str, str], ...] = (
    ("Overview", "home", "/"),
    ("Runs", "list", "/runs"),
    ("Approvals", "shield", "/approvals"),
    ("Audit", "log", "/audit"),
    ("Evaluation", "flask", "/evaluation"),
    ("Team details", "team", "/team"),
)


def _sidebar(snap: Snapshot, current: str) -> str:
    links: list[str] = []
    for label, name, href in _NAV:
        mark = ' aria-current="page"' if label == current else ""
        count = ""
        if label == "Approvals" and snap.approvals:
            n = len(snap.approvals)
            count = f'<span class="count" aria-label="{n} waiting">{n}</span>'
        links.append(f'<a href="{href}"{mark}>{icon(name)}{label}{count}</a>')
    providers = sorted({agent.provider for agent in snap.agents})
    provider_text = ", ".join(p.title() if p != "openai" else "OpenAI" for p in providers)
    if snap.persistent:
        storage = (
            f'<span class="row">{icon("disk", 15)}Saved to disk</span>'
            "<span>Approvals reopen after a restart.</span>"
        )
    else:
        storage = (
            f'<span class="row">{icon("alert", 15)}Memory only</span>'
            "<span>Approvals are lost when the dashboard stops.</span>"
        )
    return f"""<nav class="side" aria-label="Dashboard">
  <div class="brand">{_LOGO}AgentBuildKit</div>
  <div class="teamcard">{e(snap.team_name)}<small>{len(snap.agents)} agents,
{e(provider_text)}</small></div>
  <div class="nav">{"".join(links)}</div>
  <div class="sidefoot">{storage}<span>Customer data here is demo data.</span></div>
</nav>"""


def layout(snap: Snapshot, *, title: str, current: str, content: str, sheet: str = "") -> str:
    return f"""<!DOCTYPE html>
<html lang="en-GB">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)} · {e(snap.team_name)}</title>
<link rel="stylesheet" href="/static/dashboard.css">
<link rel="preload" href="/static/fonts/instrument-sans-latin-400-normal.woff2" as="font"
type="font/woff2" crossorigin>
<script src="/static/dashboard.js" defer></script>
</head>
<body>
<a class="skip" href="#content">Skip to content</a>
<div class="app">
{_sidebar(snap, current)}
<main class="main" id="content"><div class="wrap">
{content}
</div></main>
{sheet}
</div>
</body>
</html>
"""


def error_banner(message: str) -> str:
    if not message:
        return ""
    return f"""<div class="banner is-error" role="alert" data-notice>
  {icon("alert")}
  <div><p class="strong">That didn't work.</p><p>{e(message)}</p></div>
  <button class="banner-close" type="button" data-dismiss
aria-label="Dismiss">{icon("close", 14)}</button>
</div>"""


# ---------------------------------------------------------------- overview


def _attention(snap: Snapshot) -> str:
    rows: list[str] = []
    for pause in snap.approvals:
        rows.append(_attention_approval(snap, pause))
    for run in snap.runs:
        if run.state == "unresolved":
            what = e(run.pause.action) if run.pause and run.pause.action else "a service action"
            rows.append(
                f"""<div class="attn-row">
  <span class="badge badge-unres">{icon("help", 18)}</span>
  <div class="attn-text">
    <h3>Unknown outcome for {what} on {e(run.reference or "an unknown account")}</h3>
    <p class="muted sm">It was started, but never confirmed as finished. It won't be sent again.
Check the business system before acting.</p>
  </div>
  <a class="btn btn-secondary" href="{run_href(run.correlation_id)}">View run</a>
</div>"""
            )
        elif run.state == "failed":
            rows.append(
                f"""<div class="attn-row">
  <span class="badge badge-fail">{icon("alert", 18)}</span>
  <div class="attn-text">
    <h3>Service action failed for {e(run.reference or "an unknown account")}</h3>
    <p class="muted sm">The attempt ended with an error and won't be repeated. Check the business
system if you need to confirm what happened.</p>
  </div>
  <a class="btn btn-secondary" href="{run_href(run.correlation_id)}">View run</a>
</div>"""
            )
    count = len(rows)
    if not rows:
        body = """<div class="empty"><h3>Nothing needs attention</h3>
<p class="muted sm">Approvals and actions with an unknown outcome appear here.</p></div>"""
    else:
        body = "".join(rows)
    noun = "item" if count == 1 else "items"
    return f"""<section class="card" aria-labelledby="attention-heading">
  <div class="card-head"><h2 id="attention-heading">Needs attention</h2><span
class="muted sm">{count} {noun}</span></div>
  {body}
</section>"""


def _attention_approval(snap: Snapshot, pause: Pause) -> str:
    run = _run_for(snap, pause.request.correlation_id)
    specialist = title_name(run.specialist) if run and run.specialist else "The specialist"
    paused = day_and_time(pause.request.created_at)
    when = f" Paused {e(paused)} UTC." if paused else ""
    if pause.action is None:
        title = "Approval waiting, details not recovered"
        text = "Its arguments were not recovered, so it can only be rejected."
    else:
        title = f"Approve {e(pause.action)} for {e(pause.reference or 'an unknown account')}"
        text = (
            f'{e(specialist)} proposed <span class="mono">{e(pause.action)}</span>. '
            f"Nothing has been sent to the business system.{when}"
        )
    return f"""<div class="attn-row">
  <span class="badge badge-wait">{icon("clock", 18)}</span>
  <div class="attn-text"><h3>{title}</h3><p class="muted sm">{text}</p></div>
  <a class="btn btn-primary" href="/approvals#approval-{e(pause.request.request_id)}">Review and
decide</a>
</div>"""


def _run_for(snap: Snapshot, correlation_id: str) -> Run | None:
    return next((run for run in snap.runs if run.correlation_id == correlation_id), None)


def _runs_table(runs: Sequence[Run], *, caption: str) -> str:
    if not runs:
        return """<div class="empty"><h3>No runs yet</h3>
<p class="muted sm">Send a request, or start from one of the examples.</p>
<a class="btn btn-secondary" href="/?new=1">New request</a></div>"""
    rows: list[str] = []
    for run in runs:
        changes = len(run.history)
        extra = f'<div class="xs muted">{changes} status changes</div>' if changes > 1 else ""
        agent = (
            e(title_name(run.specialist)) if run.specialist else '<span class="muted">None</span>'
        )
        raw = e(run.history[-1].status)
        rows.append(
            f"""<tr data-status="{raw}">
  <td>{status(run.state)}</td>
  <td>{e(run.reference) if run.reference else '<span class="muted">Unknown</span>'}</td>
  <td>{agent}</td>
  <td><div class="outcome">{e(run.response)}</div>{extra}</td>
  <td><a class="row-link" href="{run_href(run.correlation_id)}"
aria-label="Open run {e(short(run.correlation_id))}">{e(short(run.correlation_id))}</a></td>
</tr>"""
        )
    return f"""<div class="table-scroll"><table>
  <caption class="visually-hidden">{e(caption)}</caption>
  <thead><tr><th scope="col">Status</th><th scope="col">Customer</th><th scope="col">Agent</th><th
scope="col">Outcome</th><th scope="col">Run</th></tr></thead>
  <tbody>{"".join(rows)}</tbody>
</table></div>"""


def _team_summary(snap: Snapshot) -> str:
    items: list[str] = []
    for agent in snap.agents:
        tools = (
            " ".join(f'<code class="tool">{e(tool)}</code>' for tool in agent.tools)
            if agent.tools
            else '<span class="muted">None</span>'
        )
        items.append(
            f"""<details class="agent">
  <summary><span class="avatar" aria-hidden="true">{e(agent.name[:1].upper())}</span>
    <span class="who"><span class="strong">{e(title_name(agent.name))}</span><span
class="xs muted">{e(agent.role)}</span></span>
    {icon("chev", 14, "chev")}</summary>
  <div class="agent-detail">
    <p class="sm">{e(agent.job)}</p>
    <dl class="kv tight">
      <dt>Provider</dt><dd>{e(_provider(agent.provider))}</dd>
      <dt>Model</dt><dd class="mono">{e(agent.model)}</dd>
      <dt>Tools</dt><dd>{tools}</dd>
    </dl>
  </div>
</details>"""
        )
    return f"""<section class="card" aria-labelledby="team-heading">
  <div class="card-head"><h2 id="team-heading">Team</h2><a class="sm" href="/team">Team
details</a></div>
  {"".join(items)}
  <p class="card-foot">Side effects aren't on any agent's tool list. They run only after you
approve.</p>
</section>"""


def _provider(name: str) -> str:
    return {"openai": "OpenAI", "anthropic": "Anthropic"}.get(name, name)


def _evaluation_card(snap: Snapshot) -> str:
    report = snap.report
    if report is None:
        result = '<p class="muted sm">Not run in this session.</p>'
    else:
        result = f'<p class="big-number">{report.passed} of {report.total} passed.</p>'
    return f"""<section class="card card-pad" aria-labelledby="eval-heading">
  <div class="row-s"><h2 id="eval-heading">Offline evaluation</h2><span
class="tag">Scripted</span></div>
  {result}
  <p class="xs muted">Scripted replies and a mock business system. Not results from live
requests.</p>
  <form method="post" action="/evaluate" data-busy-label="Running evaluation">
    <button class="btn btn-secondary" type="submit">Run evaluation</button>
  </form>
  <a class="sm" href="/evaluation">View scenarios</a>
</section>"""


def overview(snap: Snapshot, query: Mapping[str, str]) -> Page:
    open_sheet = query.get("new") == "1" or snap.draft is not None
    banner = "" if snap.draft is not None else error_banner(snap.error)
    title = snap.team_name.replace("-", " ").capitalize()
    recent = snap.runs[:_RECENT]
    more = (
        f'<a class="sm" href="/runs">All {len(snap.runs)} runs</a>'
        if len(snap.runs) > _RECENT
        else ""
    )
    content = f"""{banner}
<header class="head">
  <div class="stack-xs">
    <div class="titlerow"><h1>{e(title)}</h1><span class="tag">{icon("info", 13)}Demo
data</span></div>
    <p class="muted">Four agents answer customer requests. Changes to the business system wait for
your approval.</p>
  </div>
  <a class="btn btn-primary" href="/?new=1">{icon("plus")}New request</a>
</header>
{_attention(snap)}
<div class="grid-main">
  <section class="card" aria-labelledby="runs-heading">
    <div class="card-head">
      <div><h2 id="runs-heading">Recent runs</h2><p class="xs muted">Newest first. Each row shows
the latest status of one request.</p></div>
      {more}
    </div>
    {_runs_table(recent, caption="Recent runs")}
  </section>
  <div class="stack-l">
    {_team_summary(snap)}
    {_evaluation_card(snap)}
  </div>
</div>"""
    sheet = _sheet(snap, query) if open_sheet else ""
    return Page(
        200, layout(snap, title="Overview", current="Overview", content=content, sheet=sheet)
    )


def _sheet(snap: Snapshot, query: Mapping[str, str]) -> str:
    reference, message = snap.draft if snap.draft is not None else ("A-100", "")
    chosen = -1
    example = query.get("example", "")
    if snap.draft is None and example.isdigit() and int(example) < len(EXAMPLES):
        chosen = int(example)
        message = EXAMPLES[chosen][2]
    error = ""
    if snap.draft is not None and snap.error:
        error = f"""<div class="banner is-error" role="alert">{icon("alert")}
<div><p class="strong">The request didn't run.</p><p>{e(snap.error)}</p>
<p class="xs">No run was recorded. Your message is kept below.</p></div><span></span></div>"""
    links: list[str] = []
    for index, (label, hint, text) in enumerate(EXAMPLES):
        current = "true" if index == chosen else "false"
        links.append(
            f'<a class="example" href="/?new=1&amp;example={index}" data-example="{e(text)}" '
            f'aria-current="{current}"><span>{e(label)}</span>'
            f'<span class="xs muted">{e(hint)}</span></a>'
        )
    return f"""<div class="scrim" aria-hidden="true"></div>
<aside class="sheet" aria-labelledby="new-request-heading" data-sheet>
  <div class="sheet-head"><h2 id="new-request-heading">New request</h2>
    <a class="iconbtn" href="/" data-close aria-label="Close">{icon("close")}</a></div>
  <form method="post" action="/run" data-busy-label="Sending request">
    <div class="sheet-body">
      {error}
      <div>
        <label for="reference">Customer reference</label>
        <input type="text" id="reference" name="reference" value="{e(reference)}" required
autocomplete="off">
        <p class="field-help">A-100 (Northwind) is the demo account.</p>
      </div>
      <div>
        <label for="message">Message</label>
        <textarea id="message" name="message" required autofocus
placeholder="What does the customer need?">{e(message)}</textarea>
      </div>
      <div>
        <span class="medium sm">Start from an example</span>
        <div class="examples">{"".join(links)}</div>
      </div>
    </div>
    <div class="sheet-foot">
      <button class="btn btn-primary" type="submit">Send request</button>
      <a class="btn btn-secondary" href="/">Cancel</a>
      <p
data-busy-status="Waiting for the team to finish. This page updates when the run ends.">Runs the
live team. Any change to the business system waits for your approval first.</p>
    </div>
  </form>
</aside>"""


# ---------------------------------------------------------------- runs


def runs_page(snap: Snapshot) -> Page:
    content = f"""{error_banner(snap.error)}
<header class="head"><div class="stack-xs"><h1>Runs</h1>
<p class="muted">Every request in this dashboard's history, newest first.</p></div>
<a class="btn btn-primary" href="/?new=1">{icon("plus")}New request</a></header>
<section class="card">{_runs_table(snap.runs, caption="All runs")}</section>"""
    return Page(200, layout(snap, title="Runs", current="Runs", content=content))


def _trace(route: Route) -> str:
    cells: list[str] = []
    nodes = route.main
    for index, node in enumerate(nodes):
        link = ""
        if index < len(nodes) - 1:
            link = " has-link" + (" is-on" if link_on(node, nodes[index + 1]) else "")
        cells.append(_node(node, f"col-{index + 1} row-1{link}"))
    cells.append(_node(route.other, "col-3 row-2", branch=True))
    return (
        '<div class="trace" role="list" aria-label="Route for this run">'
        + "".join(cells)
        + "</div>"
    )


_NODE_WORDS: dict[NodeState, str] = {
    "recorded": "recorded",
    "waiting": "waiting",
    "failed": "failed",
    "unknown": "outcome unknown",
    "idle": "not used in this run",
}


def _node(node: RouteNode, classes: str, *, branch: bool = False) -> str:
    name = f'<span class="mono">{e(node.name)}</span>' if node.mono else e(node.name)
    extra = '<span class="branch" aria-hidden="true"></span>' if branch else ""
    return (
        f'<div class="node is-{node.state} {classes}" role="listitem">{extra}'
        f"<b>{name}</b><small>{e(node.caption)}</small>"
        f'<span class="visually-hidden">, {_NODE_WORDS[node.state]}</span></div>'
    )


def _history(run: Run) -> str:
    latest = run.history[-1]
    items = "".join(
        f'<li><span class="step">{index}</span><div>{status(outcome_state(item))}'
        f'<p class="sm muted">{e(item.response)}</p></div></li>'
        for index, item in enumerate(run.history, start=1)
    )
    count = len(run.history)
    noun = "change" if count == 1 else "changes"
    derived = ""
    if outcome_state(latest) != run.state:
        derived = (
            '<p class="xs muted">The latest status comes from the workflow store, '
            "which was updated after the last recorded outcome.</p>"
        )
    return f"""<section class="card" aria-labelledby="outcome-heading">
  <div class="card-head"><h2 id="outcome-heading">Outcome</h2></div>
  <div class="card-body stack-m">
    <div class="stack-s"><span class="xs muted">Latest
status</span><div>{status(run.state)}</div><p>{e(run.response)}</p>{derived}</div>
    <details class="more"{" open" if count > 1 else ""}>
      <summary>{icon("chev", 12, "chev")}Status history, {count} {noun}</summary>
      <ol class="hist">{items}</ol>
      <p class="xs muted">Outcomes are listed in the order they were recorded. They don't carry
times.</p>
    </details>
  </div>
</section>"""


def _event(item: AuditItem) -> str:
    when = clock(item.recorded_at)
    when_html = e(when) if when else '<span class="xs">No time</span>'
    dot = ""
    if item.outcome == "failed":
        dot = "d-fail"
    elif item.category == "approval":
        dot = "d-approval"
    elif item.category == "effect":
        dot = "d-ok" if item.kind == "side_effect_completed" else "d-effect"
    who = _who(item.agent)
    if item.is_agent_run:
        detail = ", ".join(part for part in (who, item.model or "") if part)
    else:
        detail = ", ".join(part for part in (who, item.tool) if part)
    result_cls = " is-fail" if item.outcome == "failed" else ""
    recorded = (
        f'<dd class="mono">{e(item.recorded_at)}</dd>'
        if item.recorded_at
        else "<dd>Not in the saved ledger.</dd>"
    )
    provider = ", ".join(part for part in (item.provider or "", item.model or "") if part)
    provider_html = (
        e(provider) if provider else '<span class="muted">Not recorded for this event</span>'
    )
    tech = f"""<dl class="kv">
  <dt>Event type</dt><dd class="mono">{e(item.kind)}</dd>
  <dt>Tool</dt><dd class="mono">{e(item.tool)}</dd>
  <dt>Agent</dt><dd>{e(item.agent) if item.agent else '<span class="muted">None</span>'}</dd>
  <dt>Approval</dt><dd>{e(item.approval)}</dd>
  <dt>Outcome</dt><dd>{e(item.outcome)}</dd>
  <dt>Recorded at</dt>{recorded}
  <dt>Event ID</dt><dd
class="mono">{e(item.event_id) if item.event_id else '<span class="muted">None</span>'}</dd>
  <dt>Run ID</dt><dd
class="mono">{e(item.run_id) if item.run_id else '<span class="muted">None</span>'}</dd>
  <dt>Argument hash</dt><dd class="mono">{e(item.argument_hash)}</dd>
  <dt>Provider,
model</dt><dd>{provider_html}</dd>
</dl>"""
    return f"""<li class="ev">
  <span class="when">{when_html}</span><span class="dot {dot}" aria-hidden="true"></span>
  <div><div class="what"><span class="medium">{e(item.label)}</span><span
class="muted sm">{e(detail)}</span></div>
    <details class="more"><summary>{icon("chev", 12, "chev")}Details</summary><div
class="techbox">{tech}</div></details>
  </div>
  <span class="res{result_cls}">{e(item.outcome_label)}</span>
</li>"""


def _who(agent: str | None) -> str:
    """Title-case the team's own agents. Other sources keep their recorded name."""
    if not agent:
        return ""
    return agent.title() if agent.isalpha() else agent


def _timeline(items: Sequence[AuditItem], *, empty: str) -> str:
    if not items:
        return f'<div class="empty"><p class="muted sm">{e(empty)}</p></div>'
    return '<ol class="tl">' + "".join(_event(item) for item in items) + "</ol>"


def run_page(snap: Snapshot, correlation_id: str, query: Mapping[str, str]) -> Page:
    run = _run_for(snap, correlation_id)
    if run is None:
        return not_found(snap)
    items = [item for item in snap.audit if item.correlation_id == correlation_id]
    category = query.get("events", "")
    tabs: list[str] = []
    counts: dict[str, int] = {}
    for item in items:
        counts[item.category] = counts.get(item.category, 0) + 1
    base = run_href(correlation_id)
    all_mark = ' aria-current="true"' if category not in _CATEGORY_LABELS else ""
    tabs.append(f'<a href="{base}#audit-heading"{all_mark}>All {len(items)}</a>')
    for key, label in _CATEGORY_LABELS.items():
        if counts.get(key):
            mark = ' aria-current="true"' if key == category else ""
            tabs.append(
                f'<a href="{base}?events={key}#audit-heading"{mark}>{label} {counts[key]}</a>'
            )
    shown = [
        item for item in items if category not in _CATEGORY_LABELS or item.category == category
    ]
    specialist = title_name(run.specialist)
    if run.state == "not_routed":
        sub = f"Customer {e(run.reference or 'unknown')}. No specialist was chosen."
    elif specialist:
        sub = f"Customer {e(run.reference or 'unknown')}, handled by {e(specialist)}."
    else:
        sub = f"Customer {e(run.reference or 'unknown')}."
    callout = _callout(run)
    decision = ""
    if run.state == "waiting" and run.pause is not None and run.pause.request.status == "pending":
        decision = decision_card(snap, run.pause)
    unrecorded = _effect_note(run, items)
    content = f"""{error_banner(snap.error)}
<a class="crumb" href="/runs">{icon("back", 14)}Runs</a>
<header class="head"><div class="stack-xs">
  <div class="titlerow"><h1>Run <span
class="mono">{e(short(correlation_id))}</span></h1>{status(run.state)}</div>
  <p class="muted">{sub}</p>
</div></header>
{callout}
{decision}
<section class="card card-pad" aria-labelledby="route-heading">
  <div class="row-s head"><h2 id="route-heading">Route</h2>
    <div class="legend"><span><span class="ln"></span>Recorded in this run</span><span><span
class="ln cfg"></span>Configured, not used</span></div></div>
  {_trace(build_route(run, snap.audit))}
</section>
<div class="grid-detail">
  <div class="stack-l">
    {_history(run)}
    <section class="card" aria-labelledby="audit-heading">
      <div class="card-head"><h2 id="audit-heading">Audit timeline</h2><span class="muted xs">Times
are UTC, from the saved ledger</span></div>
      <nav class="tabs" aria-label="Filter events">{"".join(tabs)}</nav>
      {_timeline(shown, empty="No audit events match this filter.")}
      {unrecorded}
    </section>
  </div>
  <div class="stack-l">
    {_decision_summary(run, items)}
    <section class="card card-pad" aria-labelledby="details-heading">
      <h2 id="details-heading">Run details</h2>
      <dl class="kv narrow">
        <dt>Customer</dt><dd>{e(run.reference or "Unknown")}</dd>
        <dt>Specialist</dt><dd>{e(specialist or "None")}</dd>
        <dt>Correlation</dt><dd class="mono">{e(correlation_id)}</dd>
      </dl>
      <details class="more"><summary>{icon("chev", 12, "chev")}What this page can't show</summary>
        <p class="sm muted">The customer's original message isn't saved with the run. Outcomes have
no timestamps, only their order. Jordan's verdict is inferred from the recorded outcome. The
audit ledger keeps argument hashes, not values.</p>
      </details>
    </section>
  </div>
</div>"""
    return Page(
        200, layout(snap, title=f"Run {short(correlation_id)}", current="Runs", content=content)
    )


def _effect_note(run: Run, items: Sequence[AuditItem]) -> str:
    """Say what the audit log shows when the store could not confirm the action."""
    if run.state not in {"unresolved", "failed"}:
        return ""
    kinds = {item.kind for item in items}
    if run.state == "unresolved" and "side_effect_completed" in kinds:
        text = (
            "The audit log recorded the side effect completing, but the workflow store did not. "
            "The action stays unresolved and won't be sent again."
        )
    elif "side_effect_completed" in kinds or "side_effect_failed" in kinds:
        return ""
    else:
        text = "No completion or failure event was recorded after the side effect started."
    return f'<p class="endrow">{e(text)}</p>'


def _callout(run: Run) -> str:
    action = (
        f'<span class="mono">{e(run.pause.action)}</span>'
        if run.pause and run.pause.action
        else "the service action"
    )
    if run.state == "unresolved":
        return f"""<section class="card callout" aria-labelledby="callout-heading">
  <span class="badge badge-unres">{icon("help", 20)}</span>
  <div class="callout-body">
    <h2 id="callout-heading">The outcome of {action} is unknown</h2>
    <p>It was approved and started, but the workflow store never marked it as finished. This
happens when the dashboard stops part-way through sending.</p>
    <p class="sm">The audit timeline below shows how far it got. Treat it as unconfirmed either
way.</p>
    <div><h3>What to do</h3><ol class="todo">
      <li>Check account {e(run.reference or "")} in the business system.</li>
      <li>If nothing happened and the customer still needs it, send a new request.</li></ol></div>
    <p class="sm muted">It won't be retried automatically: sending again could repeat the change. A
failed run is different: there the attempt ended with an error.</p>
    <div class="row-s"><a class="btn btn-secondary" href="#audit-heading">See audit events</a><a
class="btn btn-secondary" href="/?new=1">{icon("plus")}New request</a></div>
  </div>
</section>"""
    if run.state == "failed":
        return f"""<section class="card callout" aria-labelledby="callout-heading">
  <span class="badge badge-fail">{icon("alert", 20)}</span>
  <div class="callout-body">
    <h2 id="callout-heading">{action} ended with an error</h2>
    <p>The approved action was started and the attempt failed. It won't be repeated.</p>
    <p class="sm muted">The error may have happened before or after the business system received the
request. Check account {e(run.reference or "")} if you need to confirm.</p>
    <div class="row-s"><a class="btn btn-secondary" href="#audit-heading">See audit events</a></div>
  </div>
</section>"""
    return ""


def _when(value: str | None) -> str:
    return e(value + " UTC") if value else '<span class="muted">Not recorded</span>'


def _decision_summary(run: Run, items: Sequence[AuditItem]) -> str:
    if run.pause is None:
        return ""
    pause = run.pause
    decided_event = next(
        (item for item in items if item.kind in {"approval_accepted", "approval_rejected"}),
        None,
    )
    if run.state == "waiting":
        label: State = "waiting"
        text = "Waiting for your decision."
        word = None
    elif run.state == "declined":
        label = "declined"
        text = "Rejected. Nothing was sent."
        word = "Rejected"
    else:
        label = "completed"
        word = "Approved"
        text = {
            "completed": "Sent. Completion recorded.",
            "failed": "Ended with an error. Not sent again.",
            "unresolved": "Unknown. Not sent again.",
        }.get(run.state, "Approved.")
    decided = day_and_time(decided_event.recorded_at) if decided_event else None
    paused = day_and_time(pause.request.created_at)
    return f"""<section class="card card-pad" aria-labelledby="decision-heading">
  <h2 id="decision-heading">Approval decision</h2>
  <div>{status(label, word)}</div>
  <dl class="kv narrow">
    <dt>Action</dt><dd class="mono">{e(pause.action or "Not recovered")}</dd>
    <dt>Paused</dt><dd>{_when(paused)}</dd>
    <dt>Decided</dt><dd>{_when(decided)}</dd>
    <dt>Result</dt><dd>{e(text)}</dd>
  </dl>
</section>"""


# ---------------------------------------------------------------- approvals


def decision_card(snap: Snapshot, pause: Pause) -> str:
    request = pause.request
    run = _run_for(snap, request.correlation_id)
    specialist = title_name(run.specialist) if run and run.specialist else "The specialist"
    paused = day_and_time(request.created_at)
    paused_html = e(paused + " UTC") if paused else '<span class="muted">Not recorded</span>'
    request_id = e(request.request_id)
    if pause.action is None:
        return f"""<section class="card decision is-blocked" id="approval-{request_id}"
aria-labelledby="h-{request_id}">
  <div class="decision-head">{status("not_run", "Can't approve")}
    <h2 id="h-{request_id}">Approval waiting, details not recovered</h2>
    <p class="muted">The pause was found, but its action details weren't saved: the arguments were
not recovered. You can reject it. You can't approve it, because there is nothing safe to
send.</p></div>
  <form class="bar" method="post" action="/decide" data-busy-label="Sending decision">
    <input type='hidden' name='request_id' value='{request_id}'>
    <button class="btn" type="button" disabled>Approve</button>
    <button class="btn btn-secondary" type="submit" name="decision" value="reject">Reject</button>
  </form>
  {_technical(request)}
</section>"""
    reference = e(pause.reference or "Unknown")
    return f"""<section class="card decision" id="approval-{request_id}"
aria-labelledby="h-{request_id}">
  <div class="decision-head">{status("waiting")}
    <h2 id="h-{request_id}"><span class="mono">{e(pause.action)}</span> for {reference}</h2>
    <p class="muted">{e(specialist)} proposed this action and Jordan approved the response. Nothing
has been sent yet.</p></div>
  <div class="section"><dl class="kv">
    <dt>Action</dt><dd class="mono">{e(pause.action)}</dd>
    <dt>Customer</dt><dd>{reference} <span class="muted">(demo data)</span></dd>
    <dt>What runs</dt><dd><span class="mono">{e(request.action)}</span> changes the business
system</dd>
    <dt>Proposed by</dt><dd>{e(specialist)}</dd>
    <dt>Paused</dt><dd>{paused_html}</dd>
  </dl></div>
  <div class="section stack-m">
    <div class="two">
      <div class="box"><h3>If you approve</h3><p class="sm">The action is sent to the business
system. If the dashboard stops while sending, it's marked unresolved and not sent again.</p></div>
      <div class="box"><h3>If you reject</h3><p class="sm">Nothing is sent. Your decision is saved
in the audit log and the run closes as declined.</p></div>
    </div>
    <div class="note">{icon("info")}<span>The customer's message and {e(specialist)}'s draft reply
aren't saved with the pause, so they can't be shown here.</span></div>
  </div>
  <form class="bar" method="post" action="/decide" data-busy-label="Sending decision">
    <input type='hidden' name='request_id' value='{request_id}'>
    <button class="btn btn-primary" type="submit" name="decision" value="approve">Approve and
send</button>
    <button class="btn btn-secondary" type="submit" name="decision" value="reject">Reject</button>
    <span class="sm muted"
data-busy-status="Waiting for the workflow to report back. Nothing is confirmed yet.">The result
appears once the workflow reports back.</span>
  </form>
  {_technical(request)}
</section>"""


def _technical(request: PendingRequest) -> str:
    correlation = request.correlation_id
    request_id = request.request_id
    checkpoint = request.checkpoint_id
    workflow = request.workflow_name
    return f"""<div class="section">
  <details class="more"><summary>{icon("chev", 12, "chev")}Technical details</summary>
    <dl class="kv">
      <dt>Correlation ID</dt><dd class="mono"><a
href="{run_href(correlation)}">{e(correlation)}</a></dd>
      <dt>Request ID</dt><dd class="mono">{e(request_id)}</dd>
      <dt>Checkpoint ID</dt><dd class="mono">{e(checkpoint)}</dd>
      <dt>Workflow</dt><dd class="mono">{e(workflow)}</dd>
    </dl>
  </details>
</div>"""


def approvals_page(snap: Snapshot) -> Page:
    if snap.approvals:
        cards = "".join(decision_card(snap, pause) for pause in snap.approvals)
    else:
        cards = """<section class="card"><div class="empty"><h3>Nothing waiting for approval</h3>
<p class="muted sm">When a specialist proposes a change to the business system, it waits here before
anything is sent.</p></div></section>"""
    content = f"""{error_banner(snap.error)}
<header class="head"><div class="stack-xs"><h1>Approvals</h1>
<p class="muted">Actions that change the business system wait here until you
decide.</p></div></header>
{cards}"""
    return Page(200, layout(snap, title="Approvals", current="Approvals", content=content))


# ---------------------------------------------------------------- audit


def audit_page(snap: Snapshot, query: Mapping[str, str]) -> Page:
    run_filter = query.get("run", "")
    agent_filter = query.get("agent", "")
    category_filter = query.get("category", "")
    outcome_filter = query.get("outcome", "")
    items = list(snap.audit)
    shown = [
        item
        for item in items
        if (not run_filter or item.correlation_id == run_filter)
        and (not agent_filter or (item.agent or "") == agent_filter)
        and (not category_filter or item.category == category_filter)
        and (not outcome_filter or item.outcome == outcome_filter)
    ]
    order: dict[str, int] = {}
    for index, item in enumerate(shown):
        order[item.correlation_id] = index
    groups: list[str] = []
    for correlation_id in sorted(order, key=lambda key: order[key], reverse=True):
        group = [item for item in shown if item.correlation_id == correlation_id]
        run = _run_for(snap, correlation_id)
        run_status = status(run.state) if run else ""
        reference = f"{e(run.reference)}, " if run and run.reference else ""
        noun = "event" if len(group) == 1 else "events"
        link = (
            f'<a class="mono" href="{run_href(correlation_id)}">{e(short(correlation_id))}</a>'
            if run
            else f'<span class="mono">{e(short(correlation_id))}</span>'
        )
        groups.append(
            f"""<section class="card" aria-label="Run {e(short(correlation_id))}">
  <div class="group-head"><h2>{link}</h2>{run_status}<span class="muted sm">{reference}{len(group)}
{noun}</span></div>
  {_timeline(group, empty="")}
</section>"""
        )
    filtered = any((run_filter, agent_filter, category_filter, outcome_filter))
    if not groups:
        if filtered:
            body = """<section class="card"><div class="empty"><h3>No events match these
filters</h3>
<a class="btn btn-secondary" href="/audit">Clear filters</a></div></section>"""
        else:
            body = """<section class="card"><div class="empty"><h3>No audit events yet</h3>
<p class="muted sm">Events appear when a request runs.</p></div></section>"""
    else:
        body = "".join(groups)
    correlations = list(dict.fromkeys(item.correlation_id for item in reversed(items)))
    agents = sorted({item.agent for item in items if item.agent})
    run_options = _options(
        [("", "All runs"), *[(cid, short(cid)) for cid in correlations]], run_filter
    )
    agent_options = _options(
        [("", "Any agent"), *[(name, name.title()) for name in agents]], agent_filter
    )
    category_options = _options([("", "Any event"), *_CATEGORY_LABELS.items()], category_filter)
    outcome_options = _options(
        [
            ("", "Any outcome"),
            ("completed", "Completed"),
            ("pending_approval", "Pending approval"),
            ("rejected", "Rejected"),
            ("failed", "Failed"),
        ],
        outcome_filter,
    )
    clear = '<a class="btn btn-secondary" href="/audit">Clear</a>' if filtered else ""
    form = f"""<form class="card filters" method="get" action="/audit" data-autosubmit
aria-label="Filter audit events">
  <div><label for="f-run">Run</label><select id="f-run"
name="run">{run_options}</select></div>
  <div><label for="f-agent">Agent</label><select id="f-agent"
name="agent">{agent_options}</select></div>
  <div><label for="f-category">Event</label><select id="f-category"
name="category">{category_options}</select></div>
  <div><label for="f-outcome">Outcome</label><select id="f-outcome"
name="outcome">{outcome_options}</select></div>
  <div class="actions"><button class="btn btn-secondary"
type="submit">Apply</button>{clear}</div>
</form>"""
    content = f"""{error_banner(snap.error)}
<header class="head"><div class="stack-xs"><h1>Audit</h1>
<p class="muted">Agent runs, tool calls, pauses, approvals and side effects, grouped by request.
Times are UTC, from the saved ledger.</p></div></header>
{form}
{body}"""
    return Page(200, layout(snap, title="Audit", current="Audit", content=content))


def _options(choices: Sequence[tuple[str, str]], selected: str) -> str:
    return "".join(
        f'<option value="{e(value)}"{" selected" if value == selected else ""}>{e(label)}</option>'
        for value, label in choices
    )


# ---------------------------------------------------------------- evaluation


def evaluation_page(snap: Snapshot, query: Mapping[str, str]) -> Page:
    note = f"""<div class="scripted" role="note">{icon("info")}<p><span class="strong">Scripted
offline evaluation.</span> Model replies come from a script and the business system is an in-process
mock. No API key is used. These results describe the harness, not the requests on the Runs
page.</p></div>"""
    report = snap.report
    if report is None:
        body = """<section class="card"><div class="empty"><h3>No evaluation has been run</h3>
<p class="muted sm">It needs no API key. Results are kept until the dashboard stops.</p>
<form method="post" action="/evaluate" data-busy-label="Running evaluation"><button
class="btn btn-primary" type="submit">Run evaluation</button></form></div></section>"""
    else:
        failures_only = query.get("show") == "failed"
        cases = [case for case in report.cases if not failures_only or not case.passed]
        rows: list[str] = []
        for case in cases:
            checks = "".join(
                f"<li>{status('completed', 'Pass') if check.passed else status('failed', 'Fail')}"
                f'<span class="mono">{e(check.name)}</span><span>{e(check.message)}</span></li>'
                for check in case.assertions
            )
            failed_checks = sum(1 for check in case.assertions if not check.passed)
            summary = f"{len(case.assertions)} checks" + (
                f", {failed_checks} failed" if failed_checks else ""
            )
            agents = ", ".join(case.agents_invoked) or "None"
            opened = " open" if not case.passed else ""
            chev = icon("chev", 12, "chev")
            tools = ", ".join(case.tools_called) or "None"
            rows.append(
                f"""<tr{' class="is-failed"' if not case.passed else ""}>
  <td>{status("completed", "Passed") if case.passed else status("failed", "Failed")}</td>
  <td><span class="medium">{e(case.name)}</span>
    <details
class="more"{opened}><summary>{chev}{e(summary)}</summary><ul class="checks">{checks}</ul>
</details></td>
  <td class="sm">{e(agents)}</td><td class="sm">{e(tools)}</td><td
class="sm">{case.tool_call_count}</td>
</tr>"""
            )
        failing = report.failed
        toggle = (
            '<a class="sm" href="/evaluation">Show all scenarios</a>'
            if failures_only
            else (
                '<a class="sm" href="/evaluation?show=failed">Show failures only</a>'
                if failing
                else ""
            )
        )
        breakdown = ", ".join(
            f"{name} ({count})" for name, count in sorted(report.failures_by_assertion.items())
        )
        empty_row = (
            '<tr><td colspan="5" class="muted">No scenarios match.</td></tr>' if not rows else ""
        )
        body = f"""<section class="card summary" aria-label="Summary">
  <div><p class="total">{report.passed} of {report.total} passed.</p><p
class="muted sm">{report.pass_rate:.0%} pass rate, {report.elapsed_seconds:.2f} s</p></div>
  <div class="sm">{("Failed checks: " + e(breakdown)) if breakdown else "No failed checks."}</div>
  <form method="post" action="/evaluate" data-busy-label="Running evaluation"><button
class="btn btn-secondary" type="submit">Run again</button></form>
</section>
<section class="card" aria-labelledby="cases-heading">
  <div class="card-head"><h2 id="cases-heading">Scenarios</h2>{toggle}</div>
  <div class="table-scroll"><table>
    <thead><tr><th scope="col">Result</th><th scope="col">Scenario</th><th scope="col">Agents
invoked</th><th scope="col">Tools called</th><th scope="col">Tool calls</th></tr></thead>
    <tbody>{"".join(rows)}{empty_row}</tbody>
  </table></div>
</section>"""
    content = f"""{error_banner(snap.error)}
<header class="head"><div class="stack-xs"><h1>Evaluation</h1>
<p class="muted">Checks routing, tool limits, approvals, duplicates and secrets against fixed
scenarios.</p></div></header>
{note}
{body}"""
    return Page(200, layout(snap, title="Evaluation", current="Evaluation", content=content))


# ---------------------------------------------------------------- team


def team_page(snap: Snapshot) -> Page:
    rows: list[str] = []
    for agent in snap.agents:
        tools = (
            " ".join(f'<code class="tool">{e(tool)}</code>' for tool in agent.tools)
            if agent.tools
            else '<span class="muted">None</span>'
        )
        rows.append(
            f"""<tr>
  <td><span class="strong">{e(title_name(agent.name))}</span><div
class="xs muted">{e(agent.role)}</div></td>
  <td class="sm">{e(agent.job)}
    <details class="more"><summary>{icon("chev", 12, "chev")}Instructions</summary><p
class="sm muted">{e(agent.instructions)}</p></details></td>
  <td class="sm">{e(_provider(agent.provider))}<div class="mono">{e(agent.model)}</div></td>
  <td class="sm"><span class="mono">{e(agent.input_schema)}</span><div class="muted">to <span
class="mono">{e(agent.output_schema)}</span></div></td>
  <td>{tools}</td>
</tr>"""
        )
    storage = (
        "Approvals, outcomes and the audit ledger are saved under "
        '<span class="mono">APP_STATE_DIRECTORY</span> and reopen after a restart.'
        if snap.persistent
        else "State is in memory only. Set "
        '<span class="mono">APP_STATE_DIRECTORY</span> to keep approvals, outcomes and '
        "the audit ledger across restarts."
    )
    content = f"""<header class="head"><div class="stack-xs"><h1>Team details</h1>
<p class="muted">How the {e(snap.team_name)} team is configured. Loaded from <span
class="mono">team.json</span>.</p></div></header>
<section class="card" aria-labelledby="agents-heading">
  <div class="card-head"><h2 id="agents-heading">Agents</h2></div>
  <div class="table-scroll"><table>
    <thead><tr><th scope="col">Agent</th><th scope="col">Job</th><th scope="col">Provider and
model</th><th scope="col">Reads and returns</th><th scope="col">Allowed tools</th></tr></thead>
    <tbody>{"".join(rows)}</tbody>
  </table></div>
</section>
<div class="grid-main">
  <section class="card card-pad" aria-labelledby="routing-heading">
    <h2 id="routing-heading">How a request is handled</h2>
    <ol class="todo sm">
      <li>Alex classifies the request. Python routes it to Sam or Taylor and rejects any other
name.</li>
      <li>The specialist reads the business record with its allowed tool and drafts a response.</li>
      <li>Jordan approves, rejects or escalates the draft.</li>
      <li>If the specialist proposed a service action, the run pauses until you approve or reject
it. <span class="mono">schedule_service</span> is on no agent's tool list.</li>
      <li>An approved action is reserved before it is sent. If completion isn't recorded, it stays
unresolved and is not sent again. This is not exactly-once delivery.</li>
    </ol>
  </section>
  <section class="card card-pad" aria-labelledby="storage-heading">
    <h2 id="storage-heading">Storage and privacy</h2>
    <p class="sm">{storage}</p>
    <p class="sm muted">The audit ledger keeps argument hashes, never argument values. The customer
reference and action arguments for a pending approval are kept in a separate local file so a
restarted dashboard can resume it. API keys are never shown.</p>
  </section>
</div>"""
    return Page(200, layout(snap, title="Team details", current="Team details", content=content))


# ---------------------------------------------------------------- routing


def not_found(snap: Snapshot) -> Page:
    content = """<header class="head"><div class="stack-xs"><h1>Page not found</h1>
<p class="muted">That page or run doesn't exist in this dashboard.</p></div></header>
<p><a class="btn btn-secondary" href="/">Back to overview</a></p>"""
    return Page(404, layout(snap, title="Not found", current="", content=content))


def render_page(snap: Snapshot, path: str, query: Mapping[str, str] | None = None) -> Page:
    """Pick the page for a GET path. Unknown paths and run IDs return 404."""
    params: Mapping[str, str] = query or {}
    if path in {"", "/"}:
        return overview(snap, params)
    if path == "/runs":
        return runs_page(snap)
    if path.startswith("/runs/"):
        correlation_id = path.removeprefix("/runs/")
        if not _CORRELATION.match(correlation_id):
            return not_found(snap)
        return run_page(snap, correlation_id, params)
    if path == "/approvals":
        return approvals_page(snap)
    if path == "/audit":
        return audit_page(snap, params)
    if path == "/evaluation":
        return evaluation_page(snap, params)
    if path == "/team":
        return team_page(snap)
    return not_found(snap)
