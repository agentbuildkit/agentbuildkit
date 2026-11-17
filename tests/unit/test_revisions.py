"""Drafts, immutable revisions, activation, and rollback."""

import sqlite3
from pathlib import Path
from typing import cast

import pytest

from agentbuildkit.evaluation import EvaluationReport
from agentbuildkit.revisions import (
    MemoryRevisionStore,
    RevisionError,
    RevisionService,
    SqliteRevisionStore,
)
from agentbuildkit.revisions.models import Activation
from agentbuildkit.testing import ScriptedChatClient
from customer_service.connections import MemoryConnectionStore, StoredConnection
from customer_service.evaluation import scripted_clients
from customer_service.sheets import SheetDesk
from office.installation import build_installation
from office.templates import credential_ready, installed_evaluators, installed_templates
from operations_reporting.sheets import JobWindow
from operations_reporting.team import TEAM_ID
from operations_reporting.team import build_team as build_reporting_team

_CUSTOMER = "customer-service"


def _reporting() -> dict[str, ScriptedChatClient]:
    return {"analyst": ScriptedChatClient([]), "reviewer": ScriptedChatClient([])}


def _service() -> RevisionService:
    service = RevisionService(
        MemoryRevisionStore(),
        installed_templates(),
        credentials=lambda _provider: False,
    )
    service.seed()
    return service


def _report(*, failed: int = 0) -> EvaluationReport:
    passed = 0 if failed else 1
    return EvaluationReport(
        total=1,
        passed=passed,
        failed=failed,
        pass_rate=0 if failed else 1,
        elapsed_seconds=0,
        failures_by_assertion={},
        cases=(),
    )


def _with_instructions(payload: dict[str, object], text: str) -> dict[str, object]:
    agents = payload["agents"]
    assert isinstance(agents, list)
    first = cast(dict[str, object], agents[0])
    agents[0] = {**first, "instructions": text}
    return payload


def _payload(service: RevisionService, team_id: str, **title: str) -> dict[str, object]:
    config = service.active(team_id).config.model_dump(mode="json")
    config.update(title)
    return config


def _activate(
    service: RevisionService,
    team_id: str,
    payload: dict[str, object] | None = None,
) -> str:
    draft = service.create_draft(team_id)
    if payload is not None:
        draft = service.update_draft(team_id, draft.draft_id, draft.version, payload)
    prepared = service.prepare_evaluation(team_id, draft.draft_id, draft.version)
    service.record_evaluation(
        team_id, draft.draft_id, prepared.version, prepared.content_hash, _report()
    )
    current = service.draft(team_id, draft.draft_id)
    service.activate(team_id, current.draft_id, current.version, current.content_hash)
    return service.active(team_id).revision_id


def test_both_templates_open_a_draft_without_copying_access() -> None:
    service = _service()
    customer = service.create_draft(_CUSTOMER)
    reporting = service.create_draft(TEAM_ID)
    assert customer.team_id == _CUSTOMER
    assert reporting.team_id == TEAM_ID
    assert customer.draft_id != reporting.draft_id
    assert [agent.name for agent in customer.config.agents] == ["alex", "sam", "taylor", "jordan"]
    assert [agent.name for agent in reporting.config.agents] == ["analyst", "reviewer"]
    assert service.create_draft(_CUSTOMER).draft_id == customer.draft_id
    assert "credential" not in customer.model_dump_json()


def test_invalid_shape_provider_role_and_tool_are_rejected() -> None:
    service = _service()
    draft = service.create_draft(_CUSTOMER)
    payload = draft.config.model_dump(mode="json")
    payload["agents"][0]["provider"] = "grok"
    with pytest.raises(RevisionError, match="supported configuration") as invalid:
        service.update_draft(_CUSTOMER, draft.draft_id, draft.version, payload)
    assert invalid.value.code == "invalid"
    payload = draft.config.model_dump(mode="json")
    payload["agents"][0]["name"] = "casey"
    with pytest.raises(RevisionError, match="required roles"):
        service.update_draft(_CUSTOMER, draft.draft_id, draft.version, payload)
    payload = draft.config.model_dump(mode="json")
    payload["approval_mode"] = "never_require"
    with pytest.raises(RevisionError, match="supported configuration"):
        service.update_draft(_CUSTOMER, draft.draft_id, draft.version, payload)
    payload = draft.config.model_dump(mode="json")
    payload["agents"][1]["tools"] = ["schedule_service"]
    saved = service.update_draft(_CUSTOMER, draft.draft_id, draft.version, payload)
    readiness = service.validate(_CUSTOMER, saved.draft_id, saved.version)
    assert not readiness.validation_ok
    assert any(item.code == "tools" for item in readiness.findings)
    with pytest.raises(RevisionError, match="Validation failed"):
        service.prepare_evaluation(_CUSTOMER, saved.draft_id, saved.version)


def test_secret_text_is_not_stored() -> None:
    service = _service()
    draft = service.create_draft(_CUSTOMER)
    payload = draft.config.model_dump(mode="json")
    payload["agents"][0]["instructions"] = "Use api_key sk-secret-value"
    with pytest.raises(RevisionError, match="credential") as rejected:
        service.update_draft(_CUSTOMER, draft.draft_id, draft.version, payload)
    assert rejected.value.code == "invalid"
    kept = service.draft(_CUSTOMER, draft.draft_id)
    assert kept.version == draft.version
    assert "sk-secret" not in kept.model_dump_json()
    assert "sk-secret" not in str(rejected.value)


def test_stale_edit_and_stale_evaluation_do_not_activate() -> None:
    service = _service()
    draft = service.create_draft(TEAM_ID)
    first = draft.config.model_dump(mode="json")
    updated = service.update_draft(TEAM_ID, draft.draft_id, draft.version, first)
    with pytest.raises(RevisionError, match="somewhere else") as stale:
        service.update_draft(TEAM_ID, draft.draft_id, draft.version, first)
    assert stale.value.code == "stale"
    prepared = service.prepare_evaluation(TEAM_ID, updated.draft_id, updated.version)
    service.record_evaluation(
        TEAM_ID, updated.draft_id, updated.version, prepared.content_hash, _report()
    )
    changed = dict(first)
    changed["title"] = "Operations desk"
    current = service.update_draft(TEAM_ID, updated.draft_id, updated.version, changed)
    assert service.readiness_for(TEAM_ID, current.draft_id) is None
    with pytest.raises(RevisionError, match="changed") as old_hash:
        service.activate(TEAM_ID, current.draft_id, current.version, prepared.content_hash)
    assert old_hash.value.code == "stale"
    with pytest.raises(RevisionError, match="Offline evaluation"):
        service.activate(TEAM_ID, current.draft_id, current.version, current.content_hash)
    failed = service.prepare_evaluation(TEAM_ID, current.draft_id, current.version)
    service.record_evaluation(
        TEAM_ID, current.draft_id, current.version, failed.content_hash, _report(failed=1)
    )
    with pytest.raises(RevisionError, match="failed"):
        service.activate(TEAM_ID, current.draft_id, current.version, current.content_hash)
    assert service.draft(TEAM_ID, current.draft_id).config.title == "Operations desk"
    assert service.active(TEAM_ID).number == 1


def test_activation_survives_restart_and_rollback_keeps_history(tmp_path: Path) -> None:
    path = tmp_path / "revisions.sqlite"
    service = RevisionService(
        SqliteRevisionStore(path), installed_templates(), credentials=lambda _provider: False
    )
    service.seed()
    original = service.active(_CUSTOMER).revision_id
    payload = _payload(service, _CUSTOMER, title="Desk")
    activated = _activate(service, _CUSTOMER, payload)
    service.close()
    reopened = RevisionService(
        SqliteRevisionStore(path), installed_templates(), credentials=lambda _provider: False
    )
    try:
        assert reopened.active(_CUSTOMER).revision_id == activated
        assert reopened.active(_CUSTOMER).config.title == "Desk"
        rolled = reopened.rollback(_CUSTOMER, original)
        assert rolled.kind == "rollback"
        assert rolled.previous_revision_id == activated
        assert reopened.active(_CUSTOMER).revision_id == original
        assert len(reopened.revisions(_CUSTOMER)) == 2
        assert [item.kind for item in reopened.activations(_CUSTOMER)] == [
            "baseline",
            "activate",
            "rollback",
        ]
        assert "Desk" not in reopened.activations(_CUSTOMER)[-1].model_dump_json()
    finally:
        reopened.close()


def test_unknown_revision_file_is_backed_up_and_left_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "revisions.sqlite"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE revisions (old TEXT)")
    connection.execute("INSERT INTO revisions (old) VALUES ('keep')")
    connection.commit()
    connection.close()
    with pytest.raises(RuntimeError, match="backup"):
        SqliteRevisionStore(path)
    with pytest.raises(RuntimeError, match="backup"):
        SqliteRevisionStore(path)
    opened = sqlite3.connect(path)
    try:
        row = opened.execute("SELECT old FROM revisions").fetchone()
        assert row == ("keep",)
        names = {
            item[0]
            for item in opened.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert "meta" not in names
        assert "active_revision" not in names
        columns = {item[1] for item in opened.execute("PRAGMA table_info(revisions)")}
        assert columns == {"old"}
    finally:
        opened.close()
    assert list(tmp_path.glob("revisions.sqlite.bak-*"))


def test_editing_follows_the_active_revision_and_reset_is_explicit() -> None:
    service = _service()
    shipped = service.active(_CUSTOMER).config
    payload = _with_instructions(
        _payload(service, _CUSTOMER, title="Desk two"),
        "Revision two instructions.",
    )
    _activate(service, _CUSTOMER, payload)
    opened = service.drafts(_CUSTOMER)[0]
    service.discard_draft(_CUSTOMER, opened.draft_id)
    copied = service.create_draft(_CUSTOMER)
    assert copied.config.title == "Desk two"
    assert copied.config.agents[0].instructions == "Revision two instructions."
    baseline = service.baseline(_CUSTOMER)
    service.rollback(_CUSTOMER, baseline.revision_id)
    assert service.draft(_CUSTOMER, copied.draft_id).config.title == "Desk two"
    service.discard_draft(_CUSTOMER, copied.draft_id)
    after_rollback = service.create_draft(_CUSTOMER)
    assert after_rollback.config.title == shipped.title
    assert after_rollback.config.agents[0].instructions == shipped.agents[0].instructions
    service.discard_draft(_CUSTOMER, after_rollback.draft_id)
    service.rollback(_CUSTOMER, service.revisions(_CUSTOMER)[1].revision_id)
    active = service.create_draft(_CUSTOMER)
    changed = active.config.model_dump(mode="json")
    changed["title"] = "Unsaved title"
    current = service.update_draft(_CUSTOMER, active.draft_id, active.version, changed)
    with pytest.raises(RevisionError, match="somewhere else") as stale:
        service.reset_from_template(_CUSTOMER, active.version)
    assert stale.value.code == "stale"
    assert service.draft(_CUSTOMER, current.draft_id).config.title == "Unsaved title"
    reset = service.reset_from_template(_CUSTOMER, current.version)
    assert reset.draft_id == current.draft_id
    assert reset.version == current.version + 1
    assert reset.config.title == shipped.title
    assert reset.config.agents[0].instructions == shipped.agents[0].instructions
    assert service.active(_CUSTOMER).config.title == "Desk two"
    assert any(change.label == "Title" for change in service.shipped_changes(_CUSTOMER))
    assert service.create_draft(_CUSTOMER).content_hash == reset.content_hash


def test_rollback_refuses_a_revision_the_template_no_longer_allows(tmp_path: Path) -> None:
    path = tmp_path / "revisions.sqlite"
    service = RevisionService(
        SqliteRevisionStore(path), installed_templates(), credentials=lambda _provider: False
    )
    service.seed()
    original = service.active(_CUSTOMER).revision_id
    _activate(service, _CUSTOMER, _payload(service, _CUSTOMER, title="Desk"))
    service.close()
    narrowed = tuple(
        item.model_copy(update={"forbidden_tools": (*item.forbidden_tools, "read_account")})
        if item.team_id == _CUSTOMER
        else item
        for item in installed_templates()
    )
    reopened = RevisionService(
        SqliteRevisionStore(path), narrowed, credentials=lambda _provider: False
    )
    try:
        active = reopened.active(_CUSTOMER).revision_id
        with pytest.raises(RevisionError, match="no longer matches") as refused:
            reopened.rollback(_CUSTOMER, original)
        assert refused.value.code == "incompatible"
        assert reopened.active(_CUSTOMER).revision_id == active
        assert len(reopened.revisions(_CUSTOMER)) == 2
        assert [item.kind for item in reopened.activations(_CUSTOMER)] == [
            "baseline",
            "activate",
        ]
    finally:
        reopened.close()


def test_activation_refuses_a_draft_the_template_no_longer_allows(tmp_path: Path) -> None:
    path = tmp_path / "revisions.sqlite"
    service = RevisionService(
        SqliteRevisionStore(path), installed_templates(), credentials=lambda _provider: False
    )
    service.seed()
    original = service.active(_CUSTOMER).revision_id
    draft = service.create_draft(_CUSTOMER)
    prepared = service.prepare_evaluation(_CUSTOMER, draft.draft_id, draft.version)
    service.record_evaluation(
        _CUSTOMER, draft.draft_id, prepared.version, prepared.content_hash, _report()
    )
    service.close()
    narrowed = tuple(
        item.model_copy(update={"forbidden_tools": (*item.forbidden_tools, "read_account")})
        if item.team_id == _CUSTOMER
        else item
        for item in installed_templates()
    )
    reopened = RevisionService(
        SqliteRevisionStore(path), narrowed, credentials=lambda _provider: False
    )
    try:
        current = reopened.drafts(_CUSTOMER)[0]
        with pytest.raises(RevisionError, match="Activation stays off") as refused:
            reopened.activate(_CUSTOMER, current.draft_id, current.version, current.content_hash)
        assert refused.value.code == "invalid"
        assert reopened.active(_CUSTOMER).revision_id == original
        assert len(reopened.revisions(_CUSTOMER)) == 1
        readiness = reopened.readiness_for(_CUSTOMER, current.draft_id)
        assert readiness is not None
        assert readiness.validation_ok is False
        assert reopened.draft(_CUSTOMER, current.draft_id).config == current.config
    finally:
        reopened.close()


def test_a_failed_publish_or_rollback_keeps_the_active_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "revisions.sqlite"
    store = SqliteRevisionStore(path)
    service = RevisionService(store, installed_templates(), credentials=lambda _provider: False)
    service.seed()
    original = service.active(_CUSTOMER).revision_id
    draft = service.create_draft(_CUSTOMER)
    prepared = service.prepare_evaluation(_CUSTOMER, draft.draft_id, draft.version)
    service.record_evaluation(
        _CUSTOMER, draft.draft_id, prepared.version, prepared.content_hash, _report()
    )

    def fail(_item: Activation) -> None:
        raise RuntimeError("disk")

    monkeypatch.setattr(store, "add_activation", fail)
    with pytest.raises(RuntimeError, match="disk"):
        service.activate(_CUSTOMER, draft.draft_id, draft.version, draft.content_hash)
    assert service.active(_CUSTOMER).revision_id == original
    assert len(service.revisions(_CUSTOMER)) == 1
    monkeypatch.undo()
    activated = service.activate(_CUSTOMER, draft.draft_id, draft.version, draft.content_hash)
    assert service.active(_CUSTOMER).revision_id == activated.revision_id
    monkeypatch.setattr(store, "add_activation", fail)
    with pytest.raises(RuntimeError, match="disk"):
        service.rollback(_CUSTOMER, original)
    assert service.active(_CUSTOMER).revision_id == activated.revision_id
    assert [item.kind for item in service.activations(_CUSTOMER)] == ["baseline", "activate"]
    service.close()


def test_cross_team_draft_and_revision_are_rejected() -> None:
    service = _service()
    draft = service.create_draft(_CUSTOMER)
    with pytest.raises(RevisionError, match="another team") as other:
        service.draft(TEAM_ID, draft.draft_id)
    assert other.value.code == "forbidden"
    with pytest.raises(RevisionError, match="another team"):
        service.rollback(TEAM_ID, service.active(_CUSTOMER).revision_id)


@pytest.mark.asyncio
async def test_scripted_suite_passes_without_claiming_live_quality() -> None:
    service = _service()
    draft = service.create_draft(TEAM_ID)
    payload = _with_instructions(
        draft.config.model_dump(mode="json"),
        "Summarise only the supplied rows. Mention the as-of date.",
    )
    updated = service.update_draft(TEAM_ID, draft.draft_id, draft.version, payload)
    prepared = service.prepare_evaluation(TEAM_ID, updated.draft_id, updated.version)
    built = build_reporting_team(
        clients=_reporting(),
        read_jobs=JobWindow(MemoryConnectionStore(), None).tool(),
        bundle=prepared.bundle,
    )
    assert built.analyst.definition.instructions.startswith("Summarise only")
    report = await installed_evaluators()[TEAM_ID](prepared.bundle)
    assert report.failed == 0
    readiness = service.record_evaluation(
        TEAM_ID, updated.draft_id, prepared.version, prepared.content_hash, report
    )
    assert readiness.evaluation_ok is True
    assert readiness.suite_version == "operations-reporting-offline-1"
    assert "does not measure a live model" in readiness.note
    assert "does not prove" in readiness.note
    notes = service.credential_notes(updated.config)
    assert any("no API key" in note for note in notes)
    assert credential_ready("missing") is False
    assert "sk-" not in " ".join(notes)


@pytest.mark.asyncio
async def test_new_runs_use_the_active_revision_and_pending_runs_keep_theirs() -> None:
    installation = build_installation(
        customer_clients=scripted_clients("service-action-pauses-for-approval"),
        reporting_clients=_reporting(),
    )
    try:
        customer = installation.customer
        baseline = installation.revisions.baseline(_CUSTOMER)
        original = baseline.config.agents[0].instructions
        outcome = await customer.submit("A-100", "The service request is still open.")
        assert outcome.status == "pending_approval"
        assert customer.last_revision_id == baseline.revision_id
        assert customer.last_built_instructions == original
        pinned = installation.revisions.revision_for_correlation(_CUSTOMER, outcome.correlation_id)
        assert pinned.revision_id == baseline.revision_id
        payload = _payload(installation.revisions, _CUSTOMER, title="Renamed desk")
        payload = _with_instructions(payload, "Revised instructions for a later run.")
        _activate(installation.revisions, _CUSTOMER, payload)
        pending = customer.store.list_pending()
        assert pending
        await customer.decide(pending[0].request_id, approved=False)
        assert customer.last_revision_id == baseline.revision_id
        assert customer.last_built_instructions == original
        with pytest.raises(RuntimeError, match="no responses left"):
            await customer.submit("A-100", "Please explain the latest invoice.")
        assert customer.last_built_instructions == "Revised instructions for a later run."
        assert customer.last_revision_id == installation.revisions.active(_CUSTOMER).revision_id
    finally:
        installation.close()


@pytest.mark.asyncio
async def test_restart_keeps_the_pinned_approval_and_the_revoked_sheet(tmp_path: Path) -> None:
    first = build_installation(
        customer_clients=scripted_clients("service-action-pauses-for-approval"),
        reporting_clients=_reporting(),
        directory=tmp_path,
    )
    try:
        customer = first.customer
        revision_a = first.revisions.active(_CUSTOMER)
        instructions_a = revision_a.config.agents[0].instructions
        outcome = await customer.submit("A-100", "The service request is still open.")
        assert outcome.status == "pending_approval"
        request_id = customer.store.list_pending()[0].request_id
        payload = _with_instructions(
            _payload(first.revisions, _CUSTOMER, title="Revision B"),
            "Revision B instructions.",
        )
        _activate(first.revisions, _CUSTOMER, payload)
        desk = customer.sheets
        assert isinstance(desk, SheetDesk)
        first.connections.save_connection(
            StoredConnection(
                connection_id="shared",
                toolkit="googlesheets",
                connected_account_id="ca_account",
                user_id=first.connections.operator_id(),
                status="ACTIVE",
                label="Sheet",
            )
        )
        desk.assign("sam", "shared", "1" * 40, "Accounts!A1:C2", mode="connected")
        desk.unassign("sam")
    finally:
        first.close()

    second = build_installation(
        customer_clients=scripted_clients("billing-routes-to-sam"),
        reporting_clients=_reporting(),
        directory=tmp_path,
    )
    try:
        customer = second.customer
        pinned = second.revisions.revision_for_request(_CUSTOMER, request_id)
        assert pinned.revision_id == revision_a.revision_id
        await customer.decide(request_id, approved=False)
        assert customer.last_revision_id == revision_a.revision_id
        assert customer.last_built_instructions == instructions_a
        desk = customer.sheets
        assert isinstance(desk, SheetDesk)
        assert second.connections.assignment(_CUSTOMER, "sam") is None
        with pytest.raises(Exception, match="not assigned"):
            desk.account_for("sam", "read_sheet_range")
        desk.assign("sam", "shared", "1" * 40, "Accounts!A1:C2", mode="demo")
        completed = await customer.submit("A-100", "Please explain the latest invoice.")
        assert completed.status == "completed"
        assert customer.last_built_instructions == "Revision B instructions."
        assert customer.last_revision_id == second.revisions.active(_CUSTOMER).revision_id
        assert second.connections.assignment(_CUSTOMER, "sam") is None
        second.revisions.rollback(_CUSTOMER, revision_a.revision_id)
        assert second.connections.assignment(_CUSTOMER, "sam") is None
        with pytest.raises(Exception, match="not assigned"):
            desk.account_for("sam", "read_sheet_range")
    finally:
        second.close()


@pytest.mark.asyncio
async def test_pending_approval_uses_the_revision_pinned_before_its_request_id() -> None:
    installation = build_installation(
        customer_clients=scripted_clients("service-action-pauses-for-approval"),
        reporting_clients=_reporting(),
    )
    try:
        customer = installation.customer
        payload = _with_instructions(
            _payload(installation.revisions, _CUSTOMER, title="Pinned early"),
            "Pinned before the request id.",
        )
        _activate(installation.revisions, _CUSTOMER, payload)
        original = installation.revisions.pin_run

        def keep_early_pin(
            team_id: str, correlation_id: str, request_id: str, revision_id: str
        ) -> None:
            if request_id:
                return
            original(team_id, correlation_id, request_id, revision_id)

        installation.revisions.pin_run = keep_early_pin
        outcome = await customer.submit("A-100", "The service request is still open.")
        assert outcome.status == "pending_approval"
        pending = customer.store.list_pending()[0]
        assert (
            installation.revisions.revision_for_request(_CUSTOMER, pending.request_id).revision_id
            == installation.revisions.baseline(_CUSTOMER).revision_id
        )
        await customer.decide(pending.request_id, approved=False)
        assert customer.last_built_instructions == "Pinned before the request id."
        assert customer.last_revision_id == installation.revisions.active(_CUSTOMER).revision_id
    finally:
        installation.close()


def test_rollback_does_not_restore_a_revoked_assignment() -> None:
    installation = build_installation(
        customer_clients=scripted_clients("billing-routes-to-sam"),
        reporting_clients=_reporting(),
    )
    try:
        gateway_user = installation.connections.operator_id()
        installation.connections.save_connection(
            StoredConnection(
                connection_id="shared",
                toolkit="googlesheets",
                connected_account_id="ca_account",
                user_id=gateway_user,
                status="ACTIVE",
                label="Sheet",
            )
        )
        desk = installation.customer.sheets
        assert isinstance(desk, SheetDesk)
        desk.assign("sam", "shared", "1" * 40, "Accounts!A1:C2", mode="connected")
        original = installation.revisions.active(_CUSTOMER).revision_id
        renamed = _payload(installation.revisions, _CUSTOMER, title="Desk")
        _activate(installation.revisions, _CUSTOMER, renamed)
        desk.unassign("sam")
        installation.revisions.rollback(_CUSTOMER, original)
        assert installation.connections.assignment(_CUSTOMER, "sam") is None
        assert installation.connections.connection("shared") is not None
        assert desk.uses_sheet("sam")
        with pytest.raises(Exception, match="not assigned"):
            desk.account_for("sam", "read_sheet_range")
    finally:
        installation.close()


def test_draft_pages_keep_activation_separate() -> None:
    installation = build_installation(
        customer_clients=scripted_clients("billing-routes-to-sam"),
        reporting_clients=_reporting(),
    )
    try:
        page = installation.customer.render("/drafts")
        reporting = installation.reporting.render("/drafts")
        assert "Edit active version" in page
        assert "Reset from shipped template" in page
        assert "does not create another team" in page
        assert 'href="/teams/customer-service/drafts"' in page
        assert "Revision history" in page
        assert "Edit active version" in reporting
        assert 'href="/teams/operations-reporting/drafts"' in reporting
        assert "Create draft from template" not in page
        history = installation.customer.render("/revisions")
        assert "Revision history" in history
        assert "Activation history" in history
        assert "not instruction text" in history
        instructions = installation.revisions.active(_CUSTOMER).config.agents[0].instructions
        assert instructions not in history
        draft = installation.revisions.create_draft(_CUSTOMER)
        editor = installation.customer.render(f"/drafts/{draft.draft_id}")
        assert "does not activate" in editor
        assert "Not evaluated" in editor
        assert "Live readiness" in editor
        other = installation.reporting.render(f"/drafts/{draft.draft_id}")
        assert other.startswith("<!DOCTYPE html>")
        assert "Page not found" in other
    finally:
        installation.close()
