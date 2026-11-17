"""Team draft pages. The revision service decides what is valid and active."""

import uuid
from collections.abc import Mapping

from agentbuildkit.revisions import (
    FieldChange,
    Readiness,
    RevisionError,
    RevisionService,
    TeamTemplate,
)
from agentbuildkit_templates.customer_service.dashboard_view import (
    Page,
    Snapshot,
    e,
    layout,
    loc,
    not_found,
    status,
)

_NOTICES = {
    "stale": "This draft was saved somewhere else. Reload it before changing it.",
    "invalid": "Validation failed. Activation stays off.",
    "unevaluated": "Offline evaluation is missing, failed, or does not match this draft.",
    "forbidden": "That draft belongs to another team.",
    "incompatible": "That revision no longer matches the template, so it was not restored.",
    "saved": "Draft saved. Saving does not activate it.",
    "validated": "Validation finished. This does not activate the draft.",
    "evaluated": (
        "Offline evaluation finished. Scripted replies do not measure a live model "
        "or prove the instructions are effective. This does not activate the draft."
    ),
    "activated": (
        "This revision is now active. New runs use it. Runs already started keep "
        "the revision they began with."
    ),
    "rolledback": "That revision is active again. History was kept and no action was repeated.",
    "reset": (
        "This draft is now the shipped template. Compare it with the active "
        "revision. Published revisions were kept."
    ),
    "discarded": "The draft was discarded. Published revisions were kept.",
    "failed": "Offline evaluation did not finish. Nothing was activated.",
    "conflict": (
        "That creation request conflicts with an existing team. The existing team was not changed."
    ),
    "inactive": "This team is not active. Activate a revision before running it.",
}


def maybe_revision_page(
    snap: Snapshot,
    service: RevisionService | None,
    path: str,
    query: Mapping[str, str],
) -> Page | None:
    """Render a draft or history page, or leave other paths to the existing router."""
    if service is None:
        return None
    if path == "/drafts":
        return _drafts(snap, service, query)
    if path == "/revisions":
        return _history(snap, service, query)
    if path.startswith("/drafts/"):
        draft_id = path.removeprefix("/drafts/")
        if not draft_id or "/" in draft_id:
            return not_found(snap)
        try:
            service.draft(snap.team_id, draft_id)
        except RevisionError:
            return not_found(snap)
        return _editor(snap, service, draft_id, query)
    return None


def _drafts(snap: Snapshot, service: RevisionService, query: Mapping[str, str]) -> Page:
    team_id = snap.team_id
    runnable = service.runnable(team_id)
    active = service.active(team_id) if runnable else None
    draft = service.drafts(team_id)
    notice = _notice(query.get("notice", ""))
    preview = _shipped_preview(service, team_id)
    if draft:
        version = str(draft[0].version)
        action = (
            f'<a class="btn btn-primary" href="{loc(snap, f"/drafts/{draft[0].draft_id}")}">'
            "Continue draft</a>"
            f'<form method="post" action="{loc(snap, "/drafts/reset")}">{_team(snap)}'
            f'<input type="hidden" name="expected_version" value="{e(version)}">'
            '<button class="btn btn-secondary" type="submit">'
            "Reset from shipped template</button></form>"
        )
        reset_note = (
            "Reset replaces this open draft with the shipped template. "
            "It does not discard published revisions."
        )
    else:
        label = "Open draft" if active is None else "Edit active version"
        action = (
            f'<form method="post" action="{loc(snap, "/drafts")}">{_team(snap)}'
            f'<button class="btn btn-primary" type="submit">{label}</button>'
            "</form>"
            f'<form method="post" action="{loc(snap, "/drafts/reset")}">{_team(snap)}'
            '<button class="btn btn-secondary" type="submit">'
            "Reset from shipped template</button></form>"
        )
        reset_note = (
            "Open draft copies the shipped template."
            if active is None
            else "Edit active version copies the revision in use. "
            "Reset copies the shipped template instead."
        )
    current = draft[0].draft_id if draft else ""
    state = "Draft open" if draft else "No open draft"
    if active is None:
        active_block = (
            '<p class="sm"><span class="tag">Not active</span> No revision is active. '
            "New runs stay off until you activate a draft.</p>"
        )
    else:
        marker = status("completed", "Active")
        active_block = (
            f'<p class="sm">{marker} <span class="mono">r{active.number}</span> '
            f"{e(active.config.title)}</p>"
            f'<p class="sm muted">{e(active.evaluation_note)}</p>'
        )
    intro = (
        "These pages version this installed team. Editing this team does not create "
        "another team. Saving, validating, and evaluating do not activate."
    )
    content = f"""{notice}
<header class="head"><div class="stack-xs"><h1>Team drafts</h1>
<p class="muted">{intro}</p>
<p class="sm">{e(reset_note)}</p></div><div class="row-s">{action}</div></header>
<p class="sm muted">{e(preview)}</p>
<section class="card card-pad" aria-labelledby="active-revision">
  <h2 id="active-revision">Active revision</h2>
  {active_block}
  <p class="sm"><a href="{loc(snap, "/revisions")}">Revision history</a></p>
</section>
<section class="card card-pad"><h2>Draft</h2>
<p class="sm">{e(state)}</p>
<p class="xs muted">{e(current)}</p>
<p class="sm muted">{_storage(snap)}</p>
</section>"""
    return Page(200, layout(snap, title="Team drafts", current="Drafts", content=content))


def _editor(
    snap: Snapshot,
    service: RevisionService,
    draft_id: str,
    query: Mapping[str, str],
) -> Page:
    team_id = snap.team_id
    draft = service.draft(team_id, draft_id)
    template = service.template(team_id)
    readiness = service.readiness_for(team_id, draft_id)
    compared, changes = service.comparison(team_id, draft.config)
    notes = service.credential_notes(draft.config)
    notice = _notice(query.get("notice", ""))
    fields: list[str] = []
    contracts = {agent.name: agent for agent in template.agents}
    for agent in draft.config.agents:
        contract = contracts[agent.name]
        tools: list[str] = []
        for tool in contract.permitted_tools:
            required = tool in contract.required_tools
            checked = " checked" if tool in agent.tools or required else ""
            disabled = " disabled" if required else ""
            hidden = (
                f'<input type="hidden" name="tool-{e(agent.name)}-{e(tool)}" value="on">'
                if required
                else ""
            )
            tools.append(
                f'<label>{hidden}<input type="checkbox" name="tool-{e(agent.name)}-{e(tool)}" '
                f'value="on"{checked}{disabled}> <span class="mono">{e(tool)}</span>'
                f"{' required' if required else ''}</label>"
            )
        tool_block = "".join(tools) or '<p class="sm muted">This role has no selectable tools.</p>'
        provider = "".join(
            f'<option value="{item}"{" selected" if item == agent.provider else ""}>{item}</option>'
            for item in ("openai", "anthropic")
        )
        fields.append(
            f"""<section class="card card-pad" aria-labelledby="agent-{e(agent.name)}">
<h2 id="agent-{e(agent.name)}">{e(agent.name)}</h2>
<p class="xs muted">Role id and schemas stay fixed. Schemas: {e(contract.input_schema)} to
{e(contract.output_schema)}.</p>
<label for="purpose-{e(agent.name)}">Purpose</label>
<input id="purpose-{e(agent.name)}" name="purpose-{e(agent.name)}" value="{e(agent.purpose)}">
<label for="instructions-{e(agent.name)}">Instructions</label>
<textarea id="instructions-{e(agent.name)}" name="instructions-{e(agent.name)}">
{e(agent.instructions)}</textarea>
<label for="provider-{e(agent.name)}">Provider</label>
<select id="provider-{e(agent.name)}" name="provider-{e(agent.name)}">{provider}</select>
<label for="model-{e(agent.name)}">Model</label>
<input id="model-{e(agent.name)}" name="model-{e(agent.name)}" value="{e(agent.model)}">
<div class="stack-xs">{tool_block}</div>
</section>"""
        )
    diff = _changes(changes, compared)
    ready = _readiness_block(readiness, notes)
    version = str(draft.version)
    digest = draft.content_hash
    activate = ""
    matches = (
        readiness is not None
        and readiness.validation_ok
        and readiness.evaluation_ok is True
        and readiness.content_hash == digest
        and readiness.version == draft.version
    )
    if matches:
        activate_path = loc(snap, f"/drafts/{draft.draft_id}/activate")
        activate = f"""<form method="post" action="{activate_path}">
{_team(snap)}
<input type="hidden" name="expected_version" value="{e(version)}">
<input type="hidden" name="expected_hash" value="{e(digest)}">
<button class="btn btn-primary" type="submit">Activate this revision</button>
</form>"""
    drafts = loc(snap, "/drafts")
    content = f"""{notice}
<header class="head"><div class="stack-xs"><p class="muted">
<a class="crumb" href="{drafts}">Team drafts</a></p>
<h1>Draft</h1>
<p class="muted">Version {e(version)}. Hash <span class="mono">{e(digest[:12])}</span>.
This edits the installed team. Editing this team does not create another team.
This is not the active revision until you activate it.</p></div></header>
<section class="card card-pad" aria-labelledby="compare-heading">
<h2 id="compare-heading">Compared with the {e(compared)}</h2>
{diff}</section>
{ready}
<form method="post" action="{loc(snap, f"/drafts/{draft.draft_id}")}" class="stack-s">
{_team(snap)}
<input type="hidden" name="expected_version" value="{e(version)}">
<label for="title">Display name</label>
<input id="title" name="title" value="{e(draft.config.title)}" required>
<label for="description">Description</label>
<textarea id="description" name="description">{e(draft.config.description)}</textarea>
{"".join(fields)}
<div class="row-s">
<button class="btn btn-primary" type="submit">Save draft</button>
</div>
<p class="xs muted">Saving invalidates an older evaluation. It does not activate the draft.</p>
</form>
<div class="row-s">
<form method="post" action="{loc(snap, f"/drafts/{draft.draft_id}/validate")}">{_team(snap)}
<input type="hidden" name="expected_version" value="{e(version)}">
<button class="btn btn-secondary" type="submit">Validate</button></form>
<form method="post" action="{loc(snap, f"/drafts/{draft.draft_id}/evaluate")}">{_team(snap)}
<input type="hidden" name="expected_version" value="{e(version)}">
<button class="btn btn-secondary" type="submit">Run offline evaluation</button></form>
{activate}
<form method="post" action="{loc(snap, f"/drafts/{draft.draft_id}/discard")}">{_team(snap)}
<button class="btn btn-secondary" type="submit">Discard draft</button></form>
</div>"""
    return Page(200, layout(snap, title="Draft", current="Drafts", content=content))


def _history(snap: Snapshot, service: RevisionService, query: Mapping[str, str]) -> Page:
    team_id = snap.team_id
    active = service.active(team_id) if service.runnable(team_id) else None
    notice = _notice(query.get("notice", ""))
    rows: list[str] = []
    published = service.revisions(team_id)
    if not published:
        rows.append('<p class="sm">No revision is active. Activation publishes the first one.</p>')
    for revision in reversed(published):
        if active is not None and revision.revision_id == active.revision_id:
            marker = status("completed", "Active")
            action = ""
        else:
            marker = status("not_run", "Inactive")
            rollback = loc(snap, f"/revisions/{revision.revision_id}/rollback")
            action = (
                f'<form method="post" action="{rollback}">'
                f'{_team(snap)}<button class="btn btn-secondary" type="submit">'
                "Roll back to this revision</button></form>"
            )
        rows.append(
            f"""<section class="card card-pad">
<p class="sm">{marker} <span class="mono">r{revision.number}</span> {e(revision.config.title)}</p>
<p class="xs muted">Hash <span class="mono">{e(revision.content_hash[:12])}</span>.
Suite {e(revision.suite_version)}.</p>
<p class="sm">{e(revision.evaluation_note)}</p>
{action}
</section>"""
        )
    events: list[str] = []
    for item in reversed(service.activations(team_id)):
        previous = item.previous_revision_id[:8] or "none"
        events.append(
            '<li><span class="mono">'
            + e(item.kind)
            + '</span> revision <span class="mono">'
            + e(item.revision_id[:8])
            + '</span> from <span class="mono">'
            + e(previous)
            + '</span>. Hash <span class="mono">'
            + e(item.content_hash[:12])
            + "</span>.</li>"
        )
    drafts = loc(snap, "/drafts")
    content = f"""{notice}
<header class="head"><div class="stack-xs"><p class="muted">
<a href="{drafts}">Team drafts</a></p>
<h1>Revision history</h1>
<p class="muted">Rollback selects an older revision.
It does not erase history or repeat an action.</p>
</div></header>
{"".join(rows)}
<section class="card card-pad" aria-labelledby="audit-heading">
<h2 id="audit-heading">Activation history</h2>
<p class="sm muted">This list records hashes and revision ids, not instruction text or secrets.</p>
<ol class="sm">{"".join(events)}</ol></section>"""
    return Page(200, layout(snap, title="Revision history", current="Drafts", content=content))


def _shipped_preview(service: RevisionService, team_id: str) -> str:
    if not service.runnable(team_id):
        return "No revision is active yet. The draft is compared with the shipped template."
    changes = service.shipped_changes(team_id)
    if not changes:
        return "The shipped template matches the active revision."
    labels = ", ".join(change.label for change in changes)
    return f"The shipped template differs from the active revision in {labels}."


def _changes(changes: tuple[FieldChange, ...], compared: str = "active revision") -> str:
    if not changes:
        return f'<p class="sm">No differences from the {e(compared)}.</p>'
    rows: list[str] = []
    before_label = "Active" if compared == "active revision" else "Template"
    for change in changes:
        label = change.label
        before = change.before
        after = change.after
        rows.append(
            f"""<div class="stack-xs"><h3>{e(label)}</h3>
<div class="diff"><div><p class="xs muted">{before_label}</p><p class="sm">{e(before)}</p></div>
<div><p class="xs muted">Draft</p><p class="sm">{e(after)}</p></div></div></div>"""
        )
    return "".join(rows)


def _readiness_block(readiness: Readiness | None, notes: tuple[str, ...]) -> str:
    note_list = "".join(f"<li>{e(item)}</li>" for item in notes)
    live = (
        '<h2>Live readiness</h2><ul class="sm">'
        + note_list
        + '</ul><p class="xs muted">Live readiness is not the offline suite. '
        "A missing key is reported and is not printed.</p>"
    )
    if readiness is None:
        body = (
            status("not_run", "Not evaluated")
            + '<p class="sm">No validation is stored for this version.</p>'
        )
    else:
        validation = (
            status("completed", "Valid") if readiness.validation_ok else status("failed", "Invalid")
        )
        if readiness.evaluation_ok is True:
            evaluation = status("completed", "Offline passed")
        elif readiness.evaluation_ok is False:
            evaluation = status("failed", "Offline failed")
        else:
            evaluation = status("not_run", "Not evaluated")
        findings = "".join(f"<li>{e(item.message)}</li>" for item in readiness.findings)
        finding_html = f'<ul class="sm">{findings}</ul>' if findings else ""
        stored = f'<p class="sm">{e(readiness.note)}</p>' if readiness.note else ""
        body = f"<p>{validation} {evaluation}</p>{finding_html}{stored}"
    return (
        '<section class="card card-pad" aria-labelledby="ready-heading">'
        '<h2 id="ready-heading">Checks</h2>' + body + live + "</section>"
    )


def _notice(code: str) -> str:
    text = _NOTICES.get(code, "")
    if not text:
        return ""
    return f'<div class="note" role="status"><p>{e(text)}</p></div>'


def _team(snap: Snapshot) -> str:
    if not snap.team_id:
        return ""
    return f'<input type="hidden" name="team_id" value="{e(snap.team_id)}">'


def _storage(snap: Snapshot) -> str:
    if snap.persistent:
        return "Drafts and revisions are saved in this dashboard's state directory."
    return "This dashboard is memory only. Drafts last until it stops."


def create_team_page(
    snap: Snapshot,
    templates: tuple[TeamTemplate, ...],
    query: Mapping[str, str],
) -> Page:
    """Choose a supported template and create an inactive team."""
    key = uuid.uuid4().hex
    cards: list[str] = []
    for index, template in enumerate(templates):
        agents = ", ".join(agent.name for agent in template.agents)
        required = tuple(tool for agent in template.agents for tool in agent.required_tools)
        tools = ", ".join(required) or "none"
        selected = " checked" if index == 0 else ""
        cards.append(
            f"""<label class="card card-pad">
<input type="radio" name="template_id" value="{e(template.template_id)}" required{selected}>
<span class="strong">{e(template.title)}</span>
<span class="sm">{e(template.description)}</span>
<span class="xs muted">Agents: {e(agents)}. Required tools: {e(tools)}.
Suite {e(template.suite_version)}.</span>
</label>"""
        )
    content = f"""{_notice(query.get("notice", ""))}
<header class="head"><div class="stack-xs"><h1>Create team</h1>
<p class="muted">Choose a supported template. The new team starts as an inactive draft.
It does not copy connection assignments, spreadsheet windows, or another team's data.
It becomes runnable only after validation, offline evaluation, and activation.</p>
<p class="sm muted">Offline evaluation uses scripted replies. It does not measure a live model
or show that a connected account is ready.</p></div></header>
<form method="post" action="/teams/new" class="stack-s">
<input type="hidden" name="idempotency_key" value="{e(key)}">
<div class="stack-s">{"".join(cards)}</div>
<label for="title">Display name</label>
<input id="title" name="title" maxlength="80" required>
<label for="description">Description</label>
<textarea id="description" name="description"></textarea>
<button class="btn btn-primary" type="submit">Create inactive draft</button>
</form>"""
    return Page(200, layout(snap, title="Create team", current="Drafts", content=content))
