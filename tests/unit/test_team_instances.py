"""Additional instances of the registered team templates."""

import argparse
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

import pytest
from agent_framework import ChatResponse, Content, Message

from agentbuildkit.evaluation import EvaluationReport
from agentbuildkit.revisions import (
    Draft,
    MemoryRevisionStore,
    RevisionError,
    RevisionService,
    SqliteRevisionStore,
    TeamInstance,
)
from agentbuildkit.testing import ScriptedChatClient
from agentbuildkit.workflows.diagram import DiagramStep
from agentbuildkit_templates.customer_service.connections import StoredConnection
from agentbuildkit_templates.customer_service.evaluation import scripted_clients
from agentbuildkit_templates.office.installation import Installation, build_installation
from agentbuildkit_templates.office.reporting import ReportingDashboard
from agentbuildkit_templates.office.server import serve_installation
from agentbuildkit_templates.office.templates import installed_evaluators, installed_templates
from agentbuildkit_templates.operations_reporting.jobs import DEMO_AS_OF

_CUSTOMER = "customer-service"
_REPORTING = "operations-reporting"
_FACILITIES = "facilities-operations"
_DELIVERY = "delivery-operations"
_DESK = "desk-extra"


def _service() -> RevisionService:
    service = RevisionService(
        MemoryRevisionStore(),
        installed_templates(),
        credentials=lambda _provider: False,
    )
    service.seed()
    return service


def _report() -> EvaluationReport:
    return EvaluationReport(
        total=1,
        passed=1,
        failed=0,
        pass_rate=1,
        elapsed_seconds=0,
        failures_by_assertion={},
        cases=(),
    )


def _tool_call(name: str, arguments: dict[str, object]) -> ChatResponse:
    return ChatResponse(
        messages=[
            Message(
                role="assistant",
                contents=[
                    Content.from_function_call(
                        call_id=f"call-{name}",
                        name=name,
                        arguments=arguments,
                    )
                ],
            )
        ],
        response_id="scripted",
    )


def _reporting_clients() -> dict[str, ScriptedChatClient]:
    records = "job_id, customer, due_date, status\nJ-100, Northwind, 2026-11-01, open"
    summary = "J-100 is overdue."
    return {
        "analyst": ScriptedChatClient(
            [
                _tool_call("read_jobs", {"as_of": DEMO_AS_OF}),
                json.dumps({"as_of": DEMO_AS_OF, "records": records, "summary": summary}),
            ]
        ),
        "reviewer": ScriptedChatClient(
            [
                json.dumps(
                    {
                        "as_of": DEMO_AS_OF,
                        "records": "J-100",
                        "summary": summary,
                        "supported": True,
                        "notes": "",
                    }
                )
            ]
        ),
    }


def _activate(service: RevisionService, team_id: str, draft_id: str) -> str:
    draft = service.draft(team_id, draft_id)
    prepared = service.prepare_evaluation(team_id, draft.draft_id, draft.version)
    service.record_evaluation(
        team_id,
        draft.draft_id,
        prepared.version,
        prepared.content_hash,
        _report(),
    )
    current = service.draft(team_id, draft.draft_id)
    activated = service.activate(team_id, current.draft_id, current.version, current.content_hash)
    assert activated.previous_revision_id == ""
    assert activated.kind == "activate"
    return service.active(team_id).revision_id


def test_templates_stay_separate_from_installed_team_ids() -> None:
    service = _service()
    listed = service.templates()
    assert [item.template_id for item in listed] == [_CUSTOMER, _REPORTING]
    facilities, facilities_draft = service.create_instance(
        _REPORTING,
        "Facilities Operations",
        "Plant jobs.",
        "facilities-key",
        team_id=_FACILITIES,
    )
    delivery, delivery_draft = service.create_instance(
        _REPORTING,
        "Delivery Operations",
        "Route jobs.",
        "delivery-key",
        team_id=_DELIVERY,
    )
    desk, _desk_draft = service.create_instance(
        _CUSTOMER,
        "Desk Extra",
        "Another service desk.",
        "desk-extra-key",
        team_id=_DESK,
    )
    assert facilities_draft is not None
    assert delivery_draft is not None
    assert _desk_draft is not None
    assert facilities.team_id != facilities.template_id
    assert delivery.template_id == facilities.template_id
    assert desk.template_id == _CUSTOMER
    assert facilities_draft.team_id == _FACILITIES
    assert delivery_draft.team_id == _DELIVERY
    assert facilities_draft.draft_id != delivery_draft.draft_id
    assert [agent.name for agent in facilities_draft.config.agents] == ["analyst", "reviewer"]
    assert [agent.name for agent in _desk_draft.config.agents] == [
        "alex",
        "sam",
        "taylor",
        "jordan",
    ]
    assert service.runnable(_FACILITIES) is False
    assert service.runnable(_DELIVERY) is False
    assert service.runnable(_CUSTOMER) is True
    assert service.active(_CUSTOMER).revision_id
    with pytest.raises(RevisionError, match="no active revision"):
        service.active(_FACILITIES)


def test_ids_names_and_repeated_creation_are_strict() -> None:
    service = _service()
    created, draft = service.create_instance(
        _REPORTING,
        "Facilities <script>",
        "Plant & jobs",
        "facilities-key",
        team_id=_FACILITIES,
    )
    assert created.title == "Facilities <script>"
    assert draft is not None
    repeated, same_draft = service.create_instance(
        _REPORTING,
        "  Facilities <script>  ",
        "  Plant & jobs  ",
        "facilities-key",
    )
    assert repeated.team_id == created.team_id
    assert repeated.title == "Facilities <script>"
    assert repeated.description == "Plant & jobs"
    assert same_draft is not None
    assert same_draft.draft_id == draft.draft_id
    assert same_draft.version == draft.version
    assert len(service.instances()) == 3
    assert len(service.drafts(_FACILITIES)) == 1
    with pytest.raises(RevisionError, match="different team") as renamed:
        service.create_instance(
            _REPORTING,
            "A different name",
            "Plant & jobs",
            "facilities-key",
        )
    assert renamed.value.code == "conflict"
    with pytest.raises(RevisionError, match="different team") as redescribed:
        service.create_instance(
            _REPORTING,
            "Facilities <script>",
            "A different description",
            "facilities-key",
        )
    assert redescribed.value.code == "conflict"
    with pytest.raises(RevisionError, match="different team") as moved:
        service.create_instance(
            _REPORTING,
            "Facilities <script>",
            "Plant & jobs",
            "facilities-key",
            team_id="desk-other",
        )
    assert moved.value.code == "conflict"
    with pytest.raises(RevisionError, match="different team") as conflict:
        service.create_instance(_CUSTOMER, "Desk", "Other", "facilities-key", team_id="desk-other")
    assert conflict.value.code == "conflict"
    kept = service.instance(_FACILITIES)
    assert kept is not None
    assert kept.title == "Facilities <script>"
    assert kept.description == "Plant & jobs"
    assert service.instance("desk-other") is None
    with pytest.raises(RevisionError, match="already installed") as taken:
        service.create_instance(
            _REPORTING,
            "Delivery",
            "Routes",
            "delivery-key",
            team_id=_FACILITIES,
        )
    assert taken.value.code == "conflict"
    for index, team_id in enumerate(
        ("Facilities", "../x", "a/b", "new", "", "a" * 81, _CUSTOMER, _REPORTING)
    ):
        with pytest.raises(RevisionError, match="not allowed") as invalid:
            service.create_instance(
                _REPORTING,
                "Name",
                "Text",
                f"invalid-key-{index:02d}",
                team_id=team_id,
            )
        assert invalid.value.code == "invalid"
    with pytest.raises(RevisionError, match="not valid") as key:
        service.create_instance(_REPORTING, "Name", "Text", "short", team_id="valid-team")
    assert key.value.code == "invalid"


def test_failed_creation_leaves_no_partial_instance(tmp_path: Path) -> None:
    memory = _service()
    before = {item.team_id for item in memory.instances()}

    def fail(_draft: object) -> None:
        raise RuntimeError("disk")

    memory._store.save_draft = fail  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="disk"):
        memory.create_instance(
            _REPORTING,
            "Facilities Operations",
            "Plant jobs.",
            "facilities-key",
            team_id=_FACILITIES,
        )
    assert {item.team_id for item in memory.instances()} == before
    assert memory.instance(_FACILITIES) is None
    assert all(item.idempotency_key != "facilities-key" for item in memory.instances())
    restored = MemoryRevisionStore.save_draft.__get__(
        memory._store,  # pyright: ignore[reportPrivateUsage]
        MemoryRevisionStore,
    )
    memory._store.save_draft = restored  # type: ignore[method-assign]
    created, opened = memory.create_instance(
        _REPORTING,
        "Facilities Operations",
        "Plant jobs.",
        "facilities-key",
        team_id=_FACILITIES,
    )
    assert created.team_id == _FACILITIES
    assert opened is not None
    assert len([item for item in memory.instances() if item.team_id == _FACILITIES]) == 1

    path = tmp_path / "revisions.sqlite"
    store = SqliteRevisionStore(path)
    service = RevisionService(store, installed_templates(), credentials=lambda _provider: False)
    service.seed()
    store.save_draft = fail  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="disk"):
        service.create_instance(
            _REPORTING,
            "Facilities Operations",
            "Plant jobs.",
            "facilities-key",
            team_id=_FACILITIES,
        )
    service.close()
    reopened = RevisionService(
        SqliteRevisionStore(path),
        installed_templates(),
        credentials=lambda _provider: False,
    )
    try:
        assert reopened.instance(_FACILITIES) is None
        assert all(item.idempotency_key != "facilities-key" for item in reopened.instances())
        assert {item.team_id for item in reopened.instances()} == {_CUSTOMER, _REPORTING}
        created, _draft = reopened.create_instance(
            _REPORTING,
            "Facilities Operations",
            "Plant jobs.",
            "facilities-key",
            team_id=_FACILITIES,
        )
        assert created.team_id == _FACILITIES
        assert reopened.runnable(_FACILITIES) is False
    finally:
        reopened.close()


def test_reopened_draft_stays_on_the_instance() -> None:
    service = _service()
    created, draft = service.create_instance(
        _REPORTING,
        "Facilities Operations",
        "Plant jobs.",
        "facilities-key",
        team_id=_FACILITIES,
    )
    assert draft is not None
    service.discard_draft(created.team_id, draft.draft_id)
    reopened = service.create_draft(created.team_id)
    assert reopened.team_id == created.team_id
    assert reopened.template_id == _REPORTING
    assert reopened.config.title == "Facilities Operations"
    assert reopened.config.description == "Plant jobs."
    assert service.drafts(_REPORTING) == ()

    revision_id = _activate(service, created.team_id, reopened.draft_id)
    service.discard_draft(created.team_id, reopened.draft_id)
    edited = service.create_draft(created.team_id)
    assert edited.team_id == created.team_id
    assert edited.config.title == "Facilities Operations"
    assert service.drafts(_REPORTING) == ()
    assert service.active(created.team_id).revision_id == revision_id

    service.discard_draft(created.team_id, edited.draft_id)
    reset = service.reset_from_template(created.team_id)
    assert reset.team_id == created.team_id
    assert reset.template_id == _REPORTING
    assert reset.config.title == service.template(created.team_id).title
    assert service.drafts(_REPORTING) == ()
    record = service.instance(created.team_id)
    assert record is not None
    assert record.title == "Facilities Operations"


def test_repeat_after_discard_does_not_open_another_draft() -> None:
    service = _service()
    created, draft = service.create_instance(
        _REPORTING,
        "Facilities Operations",
        "Plant jobs.",
        "facilities-key",
        team_id=_FACILITIES,
    )
    assert draft is not None
    service.discard_draft(created.team_id, draft.draft_id)
    again, again_draft = service.create_instance(
        _REPORTING,
        " Facilities Operations ",
        " Plant jobs. ",
        "facilities-key",
        team_id=_FACILITIES,
    )
    assert again.team_id == created.team_id
    assert again_draft is None
    assert service.drafts(created.team_id) == ()
    assert len([item for item in service.instances() if item.team_id == _FACILITIES]) == 1


def test_concurrent_identical_creation_creates_one_instance() -> None:
    service = _service()
    barrier = threading.Barrier(2)
    results: list[tuple[TeamInstance, Draft | None]] = []
    errors: list[BaseException] = []

    def create() -> None:
        try:
            barrier.wait(timeout=5)
            results.append(
                service.create_instance(
                    _REPORTING,
                    "Facilities Operations",
                    "Plant jobs.",
                    "facilities-key",
                    team_id=_FACILITIES,
                )
            )
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=create) for _index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert errors == []
    assert len(results) == 2
    first_instance, first_draft = results[0]
    second_instance, second_draft = results[1]
    assert first_instance.team_id == second_instance.team_id == _FACILITIES
    assert first_draft is not None and second_draft is not None
    assert first_draft.draft_id == second_draft.draft_id
    assert len([item for item in service.instances() if item.team_id == _FACILITIES]) == 1
    assert len(service.drafts(_FACILITIES)) == 1


def test_raced_duplicate_key_returns_the_original_instance(tmp_path: Path) -> None:
    path = tmp_path / "revisions.sqlite"
    store = _RacingStore(path)
    service = RevisionService(
        store,
        installed_templates(),
        credentials=lambda _provider: False,
    )
    service.seed()
    try:
        created, draft = service.create_instance(
            _REPORTING,
            "Facilities Operations",
            "Plant jobs.",
            "facilities-key",
            team_id=_FACILITIES,
        )
        assert draft is not None
        store.arm()
        again, again_draft = service.create_instance(
            _REPORTING,
            "Facilities Operations",
            "Plant jobs.",
            "facilities-key",
            team_id=_FACILITIES,
        )
        assert again.team_id == created.team_id
        assert again_draft is not None
        assert again_draft.draft_id == draft.draft_id
        assert store.key_calls >= 2
        assert len(service.drafts(_FACILITIES)) == 1
        with pytest.raises(RevisionError, match="different team") as conflict:
            service.create_instance(
                _REPORTING,
                "Other name",
                "Plant jobs.",
                "facilities-key",
                team_id=_FACILITIES,
            )
        assert conflict.value.code == "conflict"
        kept = service.instance(_FACILITIES)
        assert kept is not None
        assert kept.title == "Facilities Operations"
        assert kept.description == "Plant jobs."
    finally:
        service.close()


def test_version_one_revisions_are_kept_when_instances_are_added(tmp_path: Path) -> None:
    path = tmp_path / "revisions.sqlite"
    service = RevisionService(
        SqliteRevisionStore(path),
        installed_templates(),
        credentials=lambda _provider: False,
    )
    service.seed()
    draft = service.create_draft(_CUSTOMER)
    payload = draft.config.model_dump(mode="json")
    payload["title"] = "Desk kept"
    service.update_draft(_CUSTOMER, draft.draft_id, draft.version, payload)
    current = service.draft(_CUSTOMER, draft.draft_id)
    prepared = service.prepare_evaluation(_CUSTOMER, current.draft_id, current.version)
    service.record_evaluation(
        _CUSTOMER,
        current.draft_id,
        prepared.version,
        prepared.content_hash,
        _report(),
    )
    current = service.draft(_CUSTOMER, draft.draft_id)
    service.activate(_CUSTOMER, current.draft_id, current.version, current.content_hash)
    kept = service.active(_CUSTOMER).revision_id
    count = len(service.revisions(_CUSTOMER))
    service.close()
    connection = sqlite3.connect(path)
    connection.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
    connection.execute("DROP TABLE instances")
    connection.commit()
    connection.close()
    reopened = RevisionService(
        SqliteRevisionStore(path),
        installed_templates(),
        credentials=lambda _provider: False,
    )
    try:
        reopened.seed()
        reopened.seed()
        assert reopened.active(_CUSTOMER).revision_id == kept
        assert reopened.active(_CUSTOMER).config.title == "Desk kept"
        assert len(reopened.revisions(_CUSTOMER)) == count
        assert reopened.instance(_CUSTOMER) is not None
        assert reopened.instance(_REPORTING) is not None
    finally:
        reopened.close()
    version = (
        sqlite3.connect(path)
        .execute("SELECT value FROM meta WHERE key = 'schema_version'")
        .fetchone()
    )
    assert version == ("2",)
    assert list(tmp_path.glob("revisions.sqlite.bak-*"))


@pytest.mark.asyncio
async def test_instances_stay_inactive_until_activation_and_then_version() -> None:
    installation = build_installation(
        customer_clients=scripted_clients("billing-routes-to-sam"),
        reporting_clients={"analyst": ScriptedChatClient([]), "reviewer": ScriptedChatClient([])},
        reporting_client_factory=_reporting_clients,
    )
    try:
        service = installation.revisions
        facilities, facilities_draft = service.create_instance(
            _REPORTING,
            "Facilities Operations",
            "Plant jobs.",
            "facilities-key",
            team_id=_FACILITIES,
        )
        _delivery, _delivery_draft = service.create_instance(
            _REPORTING,
            "Delivery Operations",
            "Route jobs.",
            "delivery-key",
            team_id=_DELIVERY,
        )
        assert facilities_draft is not None
        team = installation.open_instance(facilities.team_id)
        before = len(team.outcomes)
        with pytest.raises(RevisionError, match="not active") as inactive:
            await team.submit(DEMO_AS_OF, "")
        assert inactive.value.code == "inactive"
        assert len(team.outcomes) == before
        service.record_evaluation(
            _FACILITIES,
            facilities_draft.draft_id,
            facilities_draft.version,
            facilities_draft.content_hash,
            _report(),
        )
        assert service.runnable(_FACILITIES) is False
        first = _activate(service, _FACILITIES, facilities_draft.draft_id)
        assert service.active(_FACILITIES).number == 1
        opened = service.create_draft(_FACILITIES)
        assert opened.config.title == "Facilities Operations"
        payload = opened.config.model_dump(mode="json")
        payload["title"] = "Facilities revised"
        service.update_draft(_FACILITIES, opened.draft_id, opened.version, payload)
        revised = service.draft(_FACILITIES, opened.draft_id)
        prepared = service.prepare_evaluation(_FACILITIES, revised.draft_id, revised.version)
        service.record_evaluation(
            _FACILITIES,
            revised.draft_id,
            prepared.version,
            prepared.content_hash,
            _report(),
        )
        second = service.activate(
            _FACILITIES, revised.draft_id, revised.version, revised.content_hash
        )
        assert second.previous_revision_id == first
        assert service.active(_FACILITIES).number == 2
        rolled = service.rollback(_FACILITIES, first)
        assert rolled.kind == "rollback"
        assert service.active(_FACILITIES).revision_id == first
        home = team.render("/")
        assert "Not active" not in home
        assert "New request" in home
    finally:
        installation.close()


@pytest.mark.asyncio
async def test_two_operations_instances_run_and_assign_separately(tmp_path: Path) -> None:
    installation = build_installation(
        customer_clients=scripted_clients("billing-routes-to-sam"),
        reporting_clients={"analyst": ScriptedChatClient([]), "reviewer": ScriptedChatClient([])},
        directory=tmp_path,
        reporting_client_factory=_reporting_clients,
        customer_client_factory=lambda: scripted_clients("service-action-pauses-for-approval"),
    )
    try:
        service = installation.revisions
        original = service.active(_CUSTOMER).revision_id
        original_count = len(service.revisions(_CUSTOMER))
        facilities, facilities_draft = service.create_instance(
            _REPORTING,
            "Facilities <script>",
            "Plant jobs.",
            "facilities-key",
            team_id=_FACILITIES,
        )
        delivery, delivery_draft = service.create_instance(
            _REPORTING,
            "Delivery Operations",
            "Route jobs.",
            "delivery-key",
            team_id=_DELIVERY,
        )
        desk, desk_draft = service.create_instance(
            _CUSTOMER,
            "Desk Extra",
            "Another service desk.",
            "desk-extra-key",
            team_id=_DESK,
        )
        assert facilities_draft is not None
        assert delivery_draft is not None
        assert desk_draft is not None
        _activate(service, facilities.team_id, facilities_draft.draft_id)
        _activate(service, delivery.team_id, delivery_draft.draft_id)
        _activate(service, desk.team_id, desk_draft.draft_id)
        facilities_team = installation.open_instance(_FACILITIES)
        delivery_team = installation.open_instance(_DELIVERY)
        desk_team = installation.open_instance(_DESK)
        assert isinstance(facilities_team, ReportingDashboard)
        assert isinstance(delivery_team, ReportingDashboard)
        assert installation.connections.assignment(_FACILITIES, "analyst") is None
        assert installation.connections.assignment(_DELIVERY, "analyst") is None
        facilities_page = facilities_team.render("/")
        assert "Facilities &lt;script&gt;" in facilities_page
        assert "<script>" not in facilities_page
        labels = [
            step.label
            for step in facilities_team.snapshot().diagram.stages
            if isinstance(step, DiagramStep)
        ]
        assert labels == ["Request", "Analyst", "Reviewer", "Report"]
        facilities_run = await facilities_team.submit(DEMO_AS_OF, "")
        delivery_run = await delivery_team.submit(DEMO_AS_OF, "")
        assert facilities_run.status == "completed"
        assert delivery_run.status == "completed"
        assert facilities_run.correlation_id != delivery_run.correlation_id
        assert "J-100" in facilities_run.response
        assert facilities_team.owns_correlation(facilities_run.correlation_id)
        assert not delivery_team.owns_correlation(facilities_run.correlation_id)
        assert not facilities_team.owns_correlation(delivery_run.correlation_id)
        installation.connections.save_connection(
            StoredConnection(
                connection_id="shared",
                toolkit="googlesheets",
                connected_account_id="ca_account",
                user_id=installation.connections.operator_id(),
                status="ACTIVE",
                label="Jobs",
            )
        )
        installation.connections.assign(
            _FACILITIES,
            "analyst",
            "shared",
            ("read_jobs",),
            "1" * 40,
            "Jobs!A1:D5",
            "connected",
        )
        assert installation.connections.assignment(_DELIVERY, "analyst") is None
        with pytest.raises(Exception, match="not assigned"):
            delivery_team.window.desk.account_for("analyst", "read_jobs")
        paused = await desk_team.submit("A-100", "The service request is still open.")
        assert paused.status == "pending_approval"
        request_id = desk_team.store.list_pending()[0].request_id
        pinned = service.revision_for_request(_DESK, request_id).revision_id
        assert installation.owner_of_request(request_id) == _DESK
        with pytest.raises(KeyError):
            await installation.customer.decide(request_id, approved=False)
        assert service.active(_CUSTOMER).revision_id == original
        assert len(service.revisions(_CUSTOMER)) == original_count
    finally:
        installation.close()

    restored = build_installation(
        customer_clients=scripted_clients("billing-routes-to-sam"),
        reporting_clients={"analyst": ScriptedChatClient([]), "reviewer": ScriptedChatClient([])},
        directory=tmp_path,
        reporting_client_factory=_reporting_clients,
        customer_client_factory=lambda: scripted_clients("service-action-pauses-for-approval"),
    )
    try:
        assert restored.revisions.instance(_FACILITIES) is not None
        assert restored.revisions.runnable(_FACILITIES) is True
        assert restored.revisions.runnable(_DELIVERY) is True
        assert restored.revisions.active(_CUSTOMER).revision_id == original
        assert restored.connections.assignment(_FACILITIES, "analyst") is not None
        assert restored.connections.assignment(_DELIVERY, "analyst") is None
        desk_team = restored.open_instance(_DESK)
        facilities_team = restored.open_instance(_FACILITIES)
        assert facilities_team.owns_correlation(facilities_run.correlation_id)
        assert not desk_team.owns_correlation(facilities_run.correlation_id)
        assert restored.owner_of_request(request_id) == _DESK
        other, other_draft = restored.revisions.create_instance(
            _CUSTOMER,
            "Desk Two",
            "Separate desk.",
            "desk-two-key",
            team_id="desk-two",
        )
        assert other_draft is not None
        _activate(restored.revisions, other.team_id, other_draft.draft_id)
        other_team = restored.open_instance(other.team_id)
        with pytest.raises(KeyError):
            await other_team.decide(request_id, approved=False)
        await desk_team.decide(request_id, approved=False)
        assert desk_team.last_revision_id == pinned
    finally:
        restored.close()


@pytest.mark.asyncio
async def test_offline_evaluation_uses_the_candidate_instance_without_live_data() -> None:
    service = _service()
    facilities, draft = service.create_instance(
        _REPORTING,
        "Facilities Operations",
        "Plant jobs.",
        "facilities-key",
        team_id=_FACILITIES,
    )
    assert draft is not None
    prepared = service.prepare_evaluation(_FACILITIES, draft.draft_id, draft.version)
    assert prepared.bundle.team_id == facilities.team_id
    report = await installed_evaluators()[_REPORTING](prepared.bundle)
    assert report.failed == 0
    assert service.runnable(_FACILITIES) is False
    desk, desk_draft = service.create_instance(
        _CUSTOMER,
        "Desk Extra",
        "Another service desk.",
        "desk-extra-key",
        team_id=_DESK,
    )
    assert desk_draft is not None
    customer_ready = service.prepare_evaluation(_DESK, desk_draft.draft_id, desk_draft.version)
    assert customer_ready.bundle.team_id == desk.team_id
    customer_report = await installed_evaluators()[_CUSTOMER](customer_ready.bundle)
    assert customer_report.failed == 0


def test_create_page_and_inactive_run_endpoint(tmp_path: Path) -> None:
    def open_saved() -> Installation:
        return build_installation(
            customer_clients=scripted_clients("billing-routes-to-sam"),
            reporting_clients={
                "analyst": ScriptedChatClient([]),
                "reviewer": ScriptedChatClient([]),
            },
            directory=tmp_path,
        )

    binding = serve_installation(open_saved, port=0)
    try:
        host, port = binding.server_address
        base = f"http://{host}:{port}"
        with urlopen(base + "/teams/new", timeout=15) as response:
            page = response.read().decode()
        assert "Operations Reporting" in page
        assert "Customer Service" in page
        assert "inactive draft" in page
        assert "does not measure a live model" in page
        token = page.split('name="idempotency_key" value="', 1)[1].split('"', 1)[0]
        body = (
            "template_id=operations-reporting&title=Facilities+%3Cscript%3E"
            f"&description=Plant+jobs&idempotency_key={token}"
        )
        request = Request(base + "/teams/new", data=body.encode(), method="POST")
        with urlopen(request, timeout=15) as response:
            created = str(response.geturl())
            editor = response.read().decode()
        team_id = urlsplit(created).path.split("/")[2]
        assert team_id.startswith("operations-reporting-")
        assert "Facilities &lt;script&gt;" in editor
        assert "<script>" not in editor
        repeated = Request(base + "/teams/new", data=body.encode(), method="POST")
        with urlopen(repeated, timeout=15) as response:
            assert response.geturl() == created
        changed = (
            "template_id=operations-reporting&title=Facilities+%3Cscript%3E"
            f"&description=Changed+jobs&idempotency_key={token}"
        )
        conflict = Request(base + "/teams/new", data=changed.encode(), method="POST")
        with urlopen(conflict, timeout=15) as response:
            assert "notice=conflict" in response.geturl()
            conflict_page = response.read().decode()
        assert "was not changed" in conflict_page
        with urlopen(created, timeout=15) as response:
            still = response.read().decode()
        assert "Facilities &lt;script&gt;" in still
        assert "Changed" not in still
        with urlopen(base + f"/teams/{team_id}/", timeout=15) as response:
            home = response.read().decode()
        assert "Not active" in home
        assert "New request" not in home
        run = Request(
            base + f"/teams/{team_id}/run",
            data=f"as_of={DEMO_AS_OF}&note=&team_id={team_id}".encode(),
            method="POST",
        )
        with urlopen(run, timeout=15) as response:
            refused = response.read().decode()
            assert response.geturl().rstrip("/").endswith(f"/teams/{team_id}")
        assert "not active" in refused.lower()
        assert "1" * 40 not in refused
    finally:
        binding.shutdown()


def test_customer_instance_survives_a_process_restart(tmp_path: Path) -> None:
    """Create, reject, and approve a customer instance through a new process."""
    workspace = tmp_path / "workspace"
    sentinel = tmp_path / "sentinel"
    shell = tmp_path / "shell"
    state = tmp_path / "state"
    workspace.mkdir()
    sentinel.mkdir()
    shell.mkdir()
    state.mkdir()
    marker = sentinel / "marker.txt"
    marker.write_bytes(b"keep")
    marker_stamp = marker.stat().st_mtime_ns
    (workspace / ".env").write_text(f"APP_STATE_DIRECTORY={sentinel}\n", encoding="utf-8")
    port_file = tmp_path / "port"
    log_path = tmp_path / "preview.log"
    first = _Preview(state, workspace, shell, port_file, log_path)
    try:
        base = first.base_url()
        _final, page = _post(base + "/teams/new", _form(base))
        team_id = urlsplit(_final).path.split("/")[2]
        draft_id = urlsplit(_final).path.split("/")[4]
        assert team_id.startswith("customer-service-")
        assert "Northwind Desk" in page
        version = _hidden(page, "expected_version")
        validated, page = _post(
            f"{base}/teams/{team_id}/drafts/{draft_id}/validate",
            {"team_id": team_id, "expected_version": version},
        )
        assert "notice=validated" in validated
        evaluated, page = _post(
            f"{base}/teams/{team_id}/drafts/{draft_id}/evaluate",
            {"team_id": team_id, "expected_version": version},
            timeout=90,
        )
        assert "notice=evaluated" in evaluated
        activated, _page = _post(
            f"{base}/teams/{team_id}/drafts/{draft_id}/activate",
            {
                "team_id": team_id,
                "expected_version": _hidden(page, "expected_version"),
                "expected_hash": _hidden(page, "expected_hash"),
            },
        )
        assert "notice=activated" in activated
        _home_url, home = _get(f"{base}/teams/{team_id}/")
        assert "New request" in home
        _connections_url, connections = _get(f"{base}/teams/{team_id}/connections")
        assert "Connect stays off" in connections
        billing_url, billing = _post(
            f"{base}/teams/{team_id}/run",
            {
                "team_id": team_id,
                "reference": "A-100",
                "message": "Please explain the latest invoice.",
            },
            timeout=60,
        )
        assert "account is current." in billing
        billing_correlation = urlsplit(billing_url).path.rstrip("/").split("/")[-1]
        service_url, service_page = _post(
            f"{base}/teams/{team_id}/run",
            {
                "team_id": team_id,
                "reference": "A-100",
                "message": "The service request is still open.",
            },
            timeout=60,
        )
        assert "schedule_visit" in service_page
        _approvals_url, approvals = _get(f"{base}/teams/{team_id}/approvals")
        rejected_id = _hidden(approvals, "request_id")
        _post(
            f"{base}/teams/{team_id}/decide",
            {"team_id": team_id, "request_id": rejected_id, "decision": "reject"},
        )
        _rejected_url, rejected = _get(service_url)
        assert "The service action was not approved." in rejected
        assert "Nothing was sent." in rejected
        second_url, second_page = _post(
            f"{base}/teams/{team_id}/run",
            {
                "team_id": team_id,
                "reference": "A-100",
                "message": "The service request is still open.",
            },
            timeout=60,
        )
        assert "schedule_visit" in second_page
        _waiting_url, waiting = _get(f"{base}/teams/{team_id}/approvals")
        approved_id = _hidden(waiting, "request_id")
        assert approved_id != rejected_id
        second_correlation = urlsplit(second_url).path.rstrip("/").split("/")[-1]
    finally:
        first.stop()
    port_file.unlink(missing_ok=True)
    second = _Preview(state, workspace, shell, port_file, tmp_path / "preview-restart.log")
    try:
        base = second.base_url()
        _restarted_url, restarted = _get(f"{base}/teams/{team_id}/approvals")
        assert _hidden(restarted, "request_id") == approved_id
        _post(
            f"{base}/teams/{team_id}/decide",
            {"team_id": team_id, "request_id": approved_id, "decision": "approve"},
            timeout=60,
        )
        _approved_url, approved = _get(base + urlsplit(second_url).path)
        assert "The service action was completed." in approved
        assert "Sent. Completion recorded." in approved
        _post(
            f"{base}/teams/{team_id}/decide",
            {"team_id": team_id, "request_id": approved_id, "decision": "approve"},
            timeout=60,
        )
        _repeated_url, repeated = _get(base + urlsplit(second_url).path)
        assert "already handled" in repeated
        _original_url, original = _get(f"{base}/teams/customer-service/")
        assert f"/runs/{billing_correlation}" not in original
        assert f"/runs/{second_correlation}" not in original
        _original_approvals, original_approvals = _get(f"{base}/teams/customer-service/approvals")
        assert approved_id not in original_approvals
        assert rejected_id not in original_approvals
        _desk_url, desk = _get(f"{base}/teams/{team_id}/")
        assert f"/runs/{billing_correlation}" in desk
        assert f"/runs/{second_correlation}" in desk
        _audit_url, audit = _get(f"{base}/teams/{team_id}/audit")
        assert billing_correlation in audit
        _original_audit, original_audit = _get(f"{base}/teams/customer-service/audit")
        assert billing_correlation not in original_audit
        assert second_correlation not in original_audit
    finally:
        second.stop()
    effects = _rows(
        state / "workflows.sqlite",
        "SELECT scope, action, status FROM side_effects",
    )
    completed = [row for row in effects if row[2] == "completed"]
    assert completed == [(team_id, "schedule_service", "completed")]
    assert all(row[0] == team_id for row in effects)
    pins = _rows(
        state / "revisions.sqlite",
        "SELECT team_id, correlation_id FROM run_pins",
    )
    assert (team_id, billing_correlation) in pins
    assert (team_id, second_correlation) in pins
    assert ("customer-service", billing_correlation) not in pins
    assert ("customer-service", second_correlation) not in pins
    pauses = _rows(
        state / "action-recovery.sqlite",
        "SELECT team_id, request_id FROM pauses",
    )
    assert (team_id, approved_id) in pauses
    assert all(row[1] != approved_id or row[0] == team_id for row in pauses)
    assert ("customer-service", approved_id) not in pauses
    assignments = _rows(
        state / "connections.sqlite",
        "SELECT team_id FROM assignments",
    )
    assert all(row[0] != team_id for row in assignments)
    assert marker.read_bytes() == b"keep"
    assert marker.stat().st_mtime_ns == marker_stamp
    assert list(shell.iterdir()) == []
    assert list(sentinel.iterdir()) == [marker]
    databases = [
        path
        for path in tmp_path.rglob("*")
        if path.is_file()
        and (path.suffix == ".sqlite" or ".bak-" in path.name)
        and state not in path.parents
        and path != state
    ]
    assert databases == []


class _RacingStore(SqliteRevisionStore):
    """Miss the first lookup after arming, so the unique key rejects the insert."""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self._armed = False
        self.key_calls = 0
        self.id_calls = 0

    def arm(self) -> None:
        self._armed = True

    def instance_for_key(self, key: str) -> TeamInstance | None:
        if self._armed:
            self.key_calls += 1
            if self.key_calls == 1:
                return None
        return super().instance_for_key(key)

    def instance(self, team_id: str) -> TeamInstance | None:
        if self._armed:
            self.id_calls += 1
            if self.id_calls == 1 and team_id == _FACILITIES:
                return None
        return super().instance(team_id)


class _Preview:
    """One dashboard process whose state directory is the explicit argument."""

    def __init__(
        self,
        state: Path,
        workspace: Path,
        shell: Path,
        port_file: Path,
        log_path: Path,
    ) -> None:
        self._port_file = port_file
        self._log_path = log_path
        env = os.environ.copy()
        root = Path(__file__).resolve().parents[2]
        env["PYTHONPATH"] = f"{root / 'src'}:{root / 'examples'}"
        env["APP_STATE_DIRECTORY"] = str(shell)
        self._log = log_path.open("w", encoding="utf-8")
        self._process = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--state-directory",
                str(state),
                "--port-file",
                str(port_file),
            ],
            cwd=workspace,
            env=env,
            stdout=self._log,
            stderr=subprocess.STDOUT,
        )

    def base_url(self) -> str:
        deadline = time.time() + 20
        while time.time() < deadline:
            if self._port_file.is_file():
                text = self._port_file.read_text(encoding="utf-8").strip()
                if text:
                    return f"http://127.0.0.1:{text}"
            if self._process.poll() is not None:
                self._log.flush()
                detail = self._log_path.read_text(encoding="utf-8")[-800:]
                raise AssertionError(detail)
            time.sleep(0.05)
        raise AssertionError("The preview process did not open a port.")

    def stop(self) -> None:
        if self._process.poll() is None:
            self._process.send_signal(signal.SIGTERM)
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=5)
        self._log.close()


def _form(base: str) -> dict[str, str]:
    _url, page = _get(base + "/teams/new")
    return {
        "template_id": "customer-service",
        "title": "Northwind Desk",
        "description": "A second service desk.",
        "idempotency_key": _hidden(page, "idempotency_key"),
    }


def _get(url: str) -> tuple[str, str]:
    with urlopen(url, timeout=30) as response:
        return str(response.geturl()), response.read().decode()


def _post(url: str, fields: dict[str, str], timeout: int = 30) -> tuple[str, str]:
    request = Request(url, data=urlencode(fields).encode(), method="POST")
    with urlopen(request, timeout=timeout) as response:
        return str(response.geturl()), response.read().decode()


def _hidden(page: str, name: str) -> str:
    for quote in ('"', "'"):
        marker = f"name={quote}{name}{quote} value={quote}"
        if marker in page:
            return page.split(marker, 1)[1].split(quote, 1)[0]
    raise AssertionError(name)


def _rows(path: Path, sql: str) -> list[tuple[str, ...]]:
    connection = sqlite3.connect(path)
    try:
        return [tuple(str(item) for item in row) for row in connection.execute(sql)]
    finally:
        connection.close()


def _preview_customer_clients() -> dict[str, ScriptedChatClient]:
    names = ("alex", "sam", "taylor", "jordan")
    merged: dict[str, list[ChatResponse]] = {name: [] for name in names}
    for scenario in (
        "billing-routes-to-sam",
        "service-action-pauses-for-approval",
        "service-action-pauses-for-approval",
    ):
        clients = scripted_clients(scenario)
        for name in names:
            merged[name].extend(clients[name].queued_responses())
    return {name: ScriptedChatClient(rows) for name, rows in merged.items()}


def _serve_preview(state_directory: str, port_file: str) -> None:
    from agentbuildkit_templates.office.server import dashboard_state_directory

    chosen = dashboard_state_directory(["--state-directory", state_directory])
    if chosen is None or Path(chosen).resolve() != Path(state_directory).resolve():
        raise SystemExit(2)

    def open_saved() -> Installation:
        return build_installation(
            customer_clients=_preview_customer_clients(),
            reporting_clients=_reporting_clients(),
            directory=Path(chosen),
            customer_client_factory=_preview_customer_clients,
            reporting_client_factory=_reporting_clients,
        )

    binding = serve_installation(open_saved, host="127.0.0.1", port=0)
    Path(port_file).write_text(f"{binding.server_address[1]}\n", encoding="utf-8")
    stop = threading.Event()

    def _on_signal(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    stop.wait()
    binding.shutdown()


def _preview_main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-directory", required=True)
    parser.add_argument("--port-file", required=True)
    args = parser.parse_args()
    state = args.state_directory
    port_file = args.port_file
    if not isinstance(state, str) or not isinstance(port_file, str):
        raise SystemExit(2)
    _serve_preview(state, port_file)


if __name__ == "__main__":
    _preview_main()
