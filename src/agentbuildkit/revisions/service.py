"""Shared draft, validation, activation, and rollback operations.

The dashboard calls this service. A future MCP server can call it too. The
service does not import example packages, connect accounts, or run tools.
"""

import re
import sqlite3
import threading
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Literal

from pydantic import ValidationError

from agentbuildkit.evaluation import EvaluationReport
from agentbuildkit.revisions.models import (
    Activation,
    Draft,
    FieldChange,
    PreparedEvaluation,
    Readiness,
    Revision,
    RevisionError,
    RunPin,
    TeamConfig,
    TeamInstance,
    TeamTemplate,
)
from agentbuildkit.revisions.store import RevisionStore
from agentbuildkit.revisions.validate import (
    config_from_template,
    content_hash,
    to_bundle,
    validate_config,
)
from agentbuildkit.teams import TeamBundle

_TEAM_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_KEY = re.compile(r"^[A-Za-z0-9_.:-]{8,80}$")
_RESERVED = frozenset({"new", "static", "connections", "teams", "drafts", "revisions"})
_OFFLINE_NOTE = (
    "Scripted offline suite. This does not measure a live model and does not "
    "prove that changed instructions are effective."
)
CredentialProbe = Callable[[str], bool]


class RevisionService:
    """One local operator's drafts and published revisions."""

    def __init__(
        self,
        store: RevisionStore,
        templates: tuple[TeamTemplate, ...],
        *,
        credentials: CredentialProbe | None = None,
    ) -> None:
        self._store = store
        self._templates = {item.template_id: item for item in templates}
        self._credentials = credentials if credentials is not None else _unconfigured
        self._creation = threading.Lock()

    def close(self) -> None:
        self._store.close()

    def seed(self) -> None:
        """Register each shipped team once. Existing revisions and drafts are kept."""
        for template in self.templates():
            self._ensure_shipped(template)

    def _ensure_shipped(self, template: TeamTemplate) -> None:
        team_id = template.team_id
        if self._store.instance(team_id) is None:
            self._store.add_instance(
                TeamInstance(
                    team_id=team_id,
                    template_id=template.template_id,
                    title=template.title,
                    description=template.description,
                    idempotency_key=f"shipped:{template.template_id}",
                    created_at=_now(),
                )
            )
        if self._store.revisions(team_id):
            return
        self._publish(
            template,
            config_from_template(template),
            team_id=team_id,
            suite_version="shipped",
            evaluation_passed=True,
            evaluation_note="Shipped template. Not an operator evaluation.",
            kind="baseline",
            previous="",
        )

    def templates(self) -> tuple[TeamTemplate, ...]:
        return tuple(self._templates[key] for key in sorted(self._templates))

    def template(self, team_id: str) -> TeamTemplate:
        """The trusted workflow for one installed team."""
        found = self._store.instance(team_id)
        if found is None:
            raise RevisionError("forbidden", "That team is not installed.")
        template = self._templates.get(found.template_id)
        if template is None:
            raise RevisionError("forbidden", "That template is not supported.")
        return template

    def instances(self) -> tuple[TeamInstance, ...]:
        return self._store.instances()

    def instance(self, team_id: str) -> TeamInstance | None:
        return self._store.instance(team_id)

    def runnable(self, team_id: str) -> bool:
        """True only after a revision has been activated."""
        self.template(team_id)
        return self._store.active_id(team_id) is not None

    def presentation(self, team_id: str) -> tuple[TeamBundle, str, bool]:
        """The bundle and title to show. An inactive team uses its draft."""
        template = self.template(team_id)
        if self._store.active_id(team_id) is not None:
            revision = self.active(team_id)
            return self.bundle_for(revision), revision.config.title, True
        draft = self._store.draft_for_team(team_id)
        config = draft.config if draft is not None else config_from_template(template)
        return to_bundle(template, config, team_id), config.title, False

    def roster(self) -> tuple[tuple[str, str, bool], ...]:
        """Installed teams in creation order, including inactive drafts."""
        rows: list[tuple[str, str, bool]] = []
        for item in self.instances():
            _bundle, title, runnable = self.presentation(item.team_id)
            rows.append((item.team_id, title, runnable))
        return tuple(rows)

    def create_instance(
        self,
        template_id: str,
        title: str,
        description: str,
        idempotency_key: str,
        team_id: str | None = None,
    ) -> tuple[TeamInstance, Draft | None]:
        """Create one inactive team and its first draft.

        The same key and the same normalized template, name, description, and
        explicit team id return that team. They do not create another instance
        or draft. A different template, name, description, or explicit team id
        for that key is a conflict and leaves the original team unchanged.
        No revision is activated and no connection assignment is copied.
        """
        template = self._templates.get(template_id)
        if template is None:
            raise RevisionError("forbidden", "That template is not supported.")
        if _KEY.fullmatch(idempotency_key) is None:
            raise RevisionError("invalid", "The creation request is not valid.")
        config = _named_config(template, title, description)
        with self._creation:
            existing = self._store.instance_for_key(idempotency_key)
            if existing is not None:
                return self._repeat_creation(existing, template_id, config, team_id)
            chosen = team_id if team_id is not None else self._new_id(template_id)
            if not _allowed_id(chosen) or chosen in self._templates:
                raise RevisionError("invalid", "That team id is not allowed.")
            installed = self._store.instance(chosen)
            if installed is not None:
                if installed.idempotency_key == idempotency_key:
                    return self._repeat_creation(installed, template_id, config, team_id)
                raise RevisionError("conflict", "That team id is already installed.")
            instance = TeamInstance(
                team_id=chosen,
                template_id=template.template_id,
                title=config.title,
                description=config.description,
                idempotency_key=idempotency_key,
                created_at=_now(),
            )
            draft = _draft_for(template, chosen, config)
            try:
                with self._store.transaction():
                    self._store.add_instance(instance)
                    self._store.save_draft(draft)
            except (sqlite3.IntegrityError, ValueError):
                winner = self._store.instance_for_key(idempotency_key)
                if winner is not None:
                    return self._repeat_creation(winner, template_id, config, team_id)
                raise RevisionError("conflict", "That team id is already installed.") from None
            return instance, draft

    def _repeat_creation(
        self,
        existing: TeamInstance,
        template_id: str,
        config: TeamConfig,
        team_id: str | None,
    ) -> tuple[TeamInstance, Draft | None]:
        same_template = existing.template_id == template_id
        same_name = existing.title == config.title and existing.description == config.description
        same_id = team_id is None or team_id == existing.team_id
        if not (same_template and same_name and same_id):
            raise RevisionError("conflict", "That request already created a different team.")
        return existing, self._store.draft_for_team(existing.team_id)

    def _new_id(self, template_id: str) -> str:
        prefix = template_id if len(template_id) <= 40 else "team"
        for _attempt in range(5):
            candidate = f"{prefix}-{uuid.uuid4().hex[:8]}"
            if self._store.instance(candidate) is None and candidate not in self._templates:
                return candidate
        raise RevisionError("conflict", "A team id could not be chosen.")

    def active(self, team_id: str) -> Revision:
        self.template(team_id)
        revision_id = self._store.active_id(team_id)
        if revision_id is None:
            raise RevisionError("missing", "This team has no active revision.")
        found = self._store.revision(revision_id)
        if found is None or found.team_id != team_id:
            raise RevisionError("missing", "This team has no active revision.")
        return found

    def baseline(self, team_id: str) -> Revision:
        rows = self._store.revisions(team_id)
        if not rows:
            raise RevisionError("missing", "This team has no shipped revision.")
        return rows[0]

    def revisions(self, team_id: str) -> tuple[Revision, ...]:
        self.template(team_id)
        return self._store.revisions(team_id)

    def activations(self, team_id: str) -> tuple[Activation, ...]:
        self.template(team_id)
        return self._store.activations(team_id)

    def bundle_for(self, revision: Revision) -> TeamBundle:
        return to_bundle(self.template(revision.team_id), revision.config, revision.team_id)

    def create_draft(self, team_id: str) -> Draft:
        """Copy the active revision. An open draft is returned unchanged."""
        template = self.template(team_id)
        existing = self._store.draft_for_team(team_id)
        if existing is not None:
            return existing
        if self._store.active_id(team_id) is None:
            return self._open_draft(template, config_from_template(template))
        return self._open_draft(template, self.active(team_id).config)

    def reset_from_template(self, team_id: str, expected_version: int | None = None) -> Draft:
        """Replace the open draft with the shipped template, or open that draft.

        An existing draft is changed only when ``expected_version`` matches.
        A mismatch leaves the newer draft in place.
        """
        template = self.template(team_id)
        config = config_from_template(template)
        existing = self._store.draft_for_team(team_id)
        if existing is None:
            return self._open_draft(template, config)
        if existing.version != expected_version:
            raise RevisionError(
                "stale",
                "This draft was saved somewhere else. Reload it before changing it.",
            )
        updated = Draft(
            draft_id=existing.draft_id,
            team_id=existing.team_id,
            template_id=template.template_id,
            version=existing.version + 1,
            config=config,
            content_hash=content_hash(config),
            updated_at=_now(),
        )
        self._store.save_draft(updated)
        self._store.clear_readiness(updated.draft_id)
        return updated

    def shipped_changes(self, team_id: str) -> tuple[FieldChange, ...]:
        """Differences between the shipped template and the active revision."""
        if self._store.active_id(team_id) is None:
            return ()
        return self.changes(team_id, config_from_template(self.template(team_id)))

    def comparison(self, team_id: str, config: TeamConfig) -> tuple[str, tuple[FieldChange, ...]]:
        """Diff a draft against the active revision, or the shipped template."""
        if self._store.active_id(team_id) is None:
            base = config_from_template(self.template(team_id))
            return "shipped template", _diff(base, config)
        return "active revision", self.changes(team_id, config)

    def draft(self, team_id: str, draft_id: str) -> Draft:
        found = self._require_draft(team_id, draft_id)
        return found

    def readiness_for(self, team_id: str, draft_id: str) -> Readiness | None:
        current = self._require_draft(team_id, draft_id)
        return self._store.readiness(current.draft_id)

    def drafts(self, team_id: str) -> tuple[Draft, ...]:
        self.template(team_id)
        found = self._store.draft_for_team(team_id)
        if found is None:
            return ()
        return (found,)

    def update_draft(
        self,
        team_id: str,
        draft_id: str,
        expected_version: int,
        payload: Mapping[str, object],
    ) -> Draft:
        """Save a new draft version. A mismatched version does not write."""
        current = self._require_draft(team_id, draft_id)
        if current.version != expected_version:
            raise RevisionError(
                "stale",
                "This draft was saved somewhere else. Reload it before changing it.",
            )
        config = _parse_config(payload)
        self._reject_secrets(self.template(team_id), config)
        self._require_roles(self.template(team_id), config)
        updated = Draft(
            draft_id=current.draft_id,
            team_id=current.team_id,
            template_id=current.template_id,
            version=current.version + 1,
            config=config,
            content_hash=content_hash(config),
            updated_at=_now(),
        )
        self._store.save_draft(updated)
        self._store.clear_readiness(updated.draft_id)
        return updated

    def discard_draft(self, team_id: str, draft_id: str) -> None:
        current = self._require_draft(team_id, draft_id)
        self._store.delete_draft(current.draft_id)

    def validate(self, team_id: str, draft_id: str, expected_version: int) -> Readiness:
        current = self._checked(team_id, draft_id, expected_version)
        findings = validate_config(self.template(team_id), current.config)
        previous = self._store.readiness(current.draft_id)
        kept = (
            previous
            if previous is not None and previous.content_hash == current.content_hash
            else None
        )
        item = Readiness(
            draft_id=current.draft_id,
            version=current.version,
            content_hash=current.content_hash,
            validation_ok=not findings,
            findings=findings,
            evaluation_ok=None if kept is None else kept.evaluation_ok,
            suite_version="" if kept is None else kept.suite_version,
            evaluated_at="" if kept is None else kept.evaluated_at,
            passed=0 if kept is None else kept.passed,
            failed=0 if kept is None else kept.failed,
            note="" if kept is None else kept.note,
        )
        self._store.save_readiness(item)
        return item

    def prepare_evaluation(
        self, team_id: str, draft_id: str, expected_version: int
    ) -> PreparedEvaluation:
        """Validate and return the exact bundle. The caller runs the suite."""
        current = self._checked(team_id, draft_id, expected_version)
        template = self.template(team_id)
        findings = validate_config(template, current.config)
        if findings:
            self._store.save_readiness(
                Readiness(
                    draft_id=current.draft_id,
                    version=current.version,
                    content_hash=current.content_hash,
                    validation_ok=False,
                    findings=findings,
                )
            )
            raise RevisionError("invalid", "Validation failed. Activation stays off.")
        return PreparedEvaluation(
            draft_id=current.draft_id,
            team_id=team_id,
            version=current.version,
            content_hash=current.content_hash,
            bundle=to_bundle(template, current.config, team_id),
        )

    def record_evaluation(
        self,
        team_id: str,
        draft_id: str,
        expected_version: int,
        expected_hash: str,
        report: EvaluationReport,
    ) -> Readiness:
        """Store the suite result only when the draft is still that hash."""
        current = self._checked(team_id, draft_id, expected_version)
        if current.content_hash != expected_hash:
            raise RevisionError(
                "stale",
                "The draft changed after evaluation started. Evaluate this version again.",
            )
        template = self.template(team_id)
        findings = validate_config(template, current.config)
        if findings:
            self._store.save_readiness(
                Readiness(
                    draft_id=current.draft_id,
                    version=current.version,
                    content_hash=current.content_hash,
                    validation_ok=False,
                    findings=findings,
                )
            )
            raise RevisionError("invalid", "Validation failed. Activation stays off.")
        passed = report.failed == 0 and report.total > 0
        note = (
            f"{_OFFLINE_NOTE} Suite {template.suite_version}: "
            f"{report.passed} passed, {report.failed} failed."
        )
        item = Readiness(
            draft_id=current.draft_id,
            version=current.version,
            content_hash=current.content_hash,
            validation_ok=True,
            findings=(),
            evaluation_ok=passed,
            suite_version=template.suite_version,
            evaluated_at=_now(),
            passed=report.passed,
            failed=report.failed,
            note=note,
        )
        self._store.save_readiness(item)
        return item

    def activate(
        self,
        team_id: str,
        draft_id: str,
        expected_version: int,
        expected_hash: str,
    ) -> Activation:
        """Publish the draft and move the active pointer. Saving does not do this."""
        current = self._checked(team_id, draft_id, expected_version)
        if current.content_hash != expected_hash:
            raise RevisionError(
                "stale",
                "This draft changed. Review the new version before activating it.",
            )
        readiness = self._store.readiness(current.draft_id)
        if readiness is None or readiness.content_hash != current.content_hash:
            raise RevisionError(
                "unevaluated",
                "Offline evaluation is missing or does not match this draft.",
            )
        if not readiness.validation_ok:
            raise RevisionError("invalid", "Validation failed. Activation stays off.")
        if readiness.evaluation_ok is not True:
            raise RevisionError(
                "unevaluated",
                "Offline evaluation is missing, failed, or does not match this draft.",
            )
        template = self.template(team_id)
        findings = validate_config(template, current.config)
        if findings:
            self._store.save_readiness(
                Readiness(
                    draft_id=current.draft_id,
                    version=current.version,
                    content_hash=current.content_hash,
                    validation_ok=False,
                    findings=findings,
                    evaluation_ok=readiness.evaluation_ok,
                    suite_version=readiness.suite_version,
                    evaluated_at=readiness.evaluated_at,
                    passed=readiness.passed,
                    failed=readiness.failed,
                    note=readiness.note,
                )
            )
            raise RevisionError("invalid", "Validation failed. Activation stays off.")
        previous = ""
        if self._store.revisions(team_id):
            previous = self.active(team_id).revision_id
        return self._publish(
            template,
            current.config,
            team_id=team_id,
            suite_version=readiness.suite_version,
            evaluation_passed=True,
            evaluation_note=readiness.note,
            kind="activate",
            previous=previous,
        )

    def rollback(self, team_id: str, revision_id: str) -> Activation:
        """Select a previous revision. History and external actions stay as they are."""
        template = self.template(team_id)
        found = self._store.revision(revision_id)
        if found is None or found.team_id != team_id:
            raise RevisionError("forbidden", "That revision belongs to another team.")
        findings = validate_config(template, found.config)
        if findings or not found.evaluation_passed:
            raise RevisionError(
                "incompatible",
                "This revision no longer matches the template, so it was not restored.",
            )
        current = self.active(team_id)
        if current.revision_id == found.revision_id:
            raise RevisionError("invalid", "That revision is already active.")
        item = Activation(
            activation_id=uuid.uuid4().hex,
            team_id=team_id,
            revision_id=found.revision_id,
            previous_revision_id=current.revision_id,
            content_hash=found.content_hash,
            kind="rollback",
            created_at=_now(),
        )
        with self._store.transaction():
            self._store.set_active(team_id, found.revision_id)
            self._store.add_activation(item)
        return item

    def changes(self, team_id: str, config: TeamConfig) -> tuple[FieldChange, ...]:
        return _diff(self.active(team_id).config, config)

    def credential_notes(self, config: TeamConfig) -> tuple[str, ...]:
        """Report missing accounts without revealing secret values."""
        notes: list[str] = []
        for provider in sorted({agent.provider for agent in config.agents}):
            if self._configured(provider):
                continue
            label = "OpenAI" if provider == "openai" else "Anthropic"
            notes.append(
                f"{label} is selected and no API key is configured on this server. "
                "The key is not shown. A declared model is not proof the account can use it."
            )
        notes.append(
            "Spreadsheet assignments are not part of a revision. A run checks the "
            "current assignment and does not restore one that was removed."
        )
        return tuple(notes)

    def pin_run(
        self,
        team_id: str,
        correlation_id: str,
        request_id: str,
        revision_id: str,
    ) -> None:
        revision = self._store.revision(revision_id)
        if revision is None or revision.team_id != team_id:
            raise RevisionError("forbidden", "That revision belongs to another team.")
        self._store.pin(
            RunPin(
                team_id=team_id,
                correlation_id=correlation_id,
                request_id=request_id,
                revision_id=revision_id,
            )
        )

    def revision_for_request(self, team_id: str, request_id: str) -> Revision:
        """The revision a pending approval started with, or the shipped baseline."""
        return self._pinned(team_id, self._store.pin_for_request(team_id, request_id))

    def revision_for_pending(self, team_id: str, request_id: str, correlation_id: str) -> Revision:
        """The revision recorded for this run, even before its approval id was stored."""
        pin = self._store.pin_for_request(team_id, request_id)
        if pin is None and correlation_id:
            pin = self._store.pin_for_correlation(team_id, correlation_id)
        return self._pinned(team_id, pin)

    def revision_for_correlation(self, team_id: str, correlation_id: str) -> Revision:
        return self._pinned(team_id, self._store.pin_for_correlation(team_id, correlation_id))

    def _publish(
        self,
        template: TeamTemplate,
        config: TeamConfig,
        *,
        team_id: str,
        suite_version: str,
        evaluation_passed: bool,
        evaluation_note: str,
        kind: Literal["baseline", "activate", "rollback"],
        previous: str,
    ) -> Activation:
        number = len(self._store.revisions(team_id)) + 1
        revision = Revision(
            revision_id=uuid.uuid4().hex,
            team_id=team_id,
            template_id=template.template_id,
            number=number,
            content_hash=content_hash(config),
            config=config,
            suite_version=suite_version,
            evaluation_passed=evaluation_passed,
            evaluation_note=evaluation_note,
            created_at=_now(),
        )
        item = Activation(
            activation_id=uuid.uuid4().hex,
            team_id=team_id,
            revision_id=revision.revision_id,
            previous_revision_id=previous,
            content_hash=revision.content_hash,
            kind=kind,
            created_at=revision.created_at,
        )
        with self._store.transaction():
            self._store.add_revision(revision)
            self._store.set_active(team_id, revision.revision_id)
            self._store.add_activation(item)
        return item

    def _checked(self, team_id: str, draft_id: str, expected_version: int) -> Draft:
        current = self._require_draft(team_id, draft_id)
        hash_matches = content_hash(current.config) == current.content_hash
        if current.version != expected_version or not hash_matches:
            raise RevisionError(
                "stale",
                "This draft was saved somewhere else. Reload it before changing it.",
            )
        return current

    def _pinned(self, team_id: str, pin: RunPin | None) -> Revision:
        if pin is None:
            return self.baseline(team_id)
        found = self._store.revision(pin.revision_id)
        if found is None or found.team_id != team_id:
            return self.baseline(team_id)
        return found

    def _open_draft(self, template: TeamTemplate, config: TeamConfig) -> Draft:
        draft = Draft(
            draft_id=uuid.uuid4().hex,
            team_id=template.team_id,
            template_id=template.template_id,
            version=1,
            config=config,
            content_hash=content_hash(config),
            updated_at=_now(),
        )
        self._store.save_draft(draft)
        return draft

    def _require_draft(self, team_id: str, draft_id: str) -> Draft:
        self.template(team_id)
        found = self._store.draft(draft_id)
        if found is None or found.team_id != team_id:
            raise RevisionError("forbidden", "That draft belongs to another team.")
        return found

    def _require_roles(self, template: TeamTemplate, config: TeamConfig) -> None:
        expected = [agent.name for agent in template.agents]
        actual = [agent.name for agent in config.agents]
        if actual != expected:
            raise RevisionError(
                "invalid",
                "The draft must keep this template's agents, in their required roles.",
            )

    def _reject_secrets(self, template: TeamTemplate, config: TeamConfig) -> None:
        findings = [item for item in validate_config(template, config) if item.code == "secret"]
        if findings:
            raise RevisionError("invalid", findings[0].message)

    def _configured(self, provider: str) -> bool:
        try:
            return bool(self._credentials(provider))
        except Exception:
            return False


def _unconfigured(_provider: str) -> bool:
    return False


def _allowed_id(value: str) -> bool:
    return (
        len(value) <= 80
        and _TEAM_ID.fullmatch(value) is not None
        and value not in _RESERVED
        and "/" not in value
        and "\\" not in value
        and ".." not in value
    )


def _named_config(template: TeamTemplate, title: str, description: str) -> TeamConfig:
    """Apply the template, then the same whitespace rules as ``TeamConfig``."""
    payload = config_from_template(template).model_dump()
    payload["title"] = title
    payload["description"] = description
    try:
        config = TeamConfig.model_validate(payload)
    except ValidationError:
        raise RevisionError("invalid", "The team name is not supported.") from None
    findings = [item for item in validate_config(template, config) if item.code == "secret"]
    if findings:
        raise RevisionError("invalid", findings[0].message)
    return config


def _draft_for(template: TeamTemplate, team_id: str, config: TeamConfig) -> Draft:
    return Draft(
        draft_id=uuid.uuid4().hex,
        team_id=team_id,
        template_id=template.template_id,
        version=1,
        config=config,
        content_hash=content_hash(config),
        updated_at=_now(),
    )


def _diff(current: TeamConfig, config: TeamConfig) -> tuple[FieldChange, ...]:
    rows: list[FieldChange] = []
    if current.title != config.title:
        rows.append(FieldChange(label="Title", before=current.title, after=config.title))
    if current.description != config.description:
        rows.append(
            FieldChange(label="Description", before=current.description, after=config.description)
        )
    for before, after in zip(current.agents, config.agents, strict=False):
        label = before.name
        if before.purpose != after.purpose:
            rows.append(
                FieldChange(label=f"{label} purpose", before=before.purpose, after=after.purpose)
            )
        if before.instructions != after.instructions:
            rows.append(
                FieldChange(
                    label=f"{label} instructions",
                    before=before.instructions,
                    after=after.instructions,
                )
            )
        if before.provider != after.provider:
            rows.append(
                FieldChange(label=f"{label} provider", before=before.provider, after=after.provider)
            )
        if before.model != after.model:
            rows.append(FieldChange(label=f"{label} model", before=before.model, after=after.model))
        if before.tools != after.tools:
            rows.append(
                FieldChange(
                    label=f"{label} tools",
                    before=", ".join(before.tools) or "none",
                    after=", ".join(after.tools) or "none",
                )
            )
    return tuple(rows)


def _parse_config(payload: Mapping[str, object]) -> TeamConfig:
    try:
        return TeamConfig.model_validate(payload)
    except ValidationError:
        raise RevisionError("invalid", "The draft is not a supported configuration.") from None


def _now() -> str:
    return datetime.now(UTC).isoformat()
