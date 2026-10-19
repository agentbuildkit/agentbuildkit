"""Structured audit events for tool execution.

Events store a hash of the arguments. They do not store argument values or secrets.
"""

import hashlib
import json
from collections.abc import Mapping
from typing import Any, Literal, cast

from agent_framework import SecretString
from pydantic import BaseModel, ConfigDict

ApprovalState = Literal["not_required", "pending", "approved", "rejected"]
AuditOutcome = Literal["completed", "pending_approval", "rejected", "failed"]
AuditKind = Literal[
    "tool",
    "checkpoint_created",
    "workflow_paused",
    "workflow_resumed",
    "approval_accepted",
    "approval_rejected",
    "side_effect_attempted",
    "side_effect_completed",
    "side_effect_failed",
]
_REDACTED_ARGUMENT = "<redacted>"


class AuditEvent(BaseModel):
    """One auditable tool action."""

    model_config = ConfigDict(frozen=True)

    correlation_id: str
    agent_name: str | None
    tool_name: str
    approval: ApprovalState
    outcome: AuditOutcome
    argument_hash: str
    kind: AuditKind = "tool"


class AuditLog:
    """In-memory audit sink for the current process."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def record(self, event: AuditEvent) -> None:
        self.events.append(event)


def redact_arguments(value: Any) -> Any:
    """Replace secret values before hashing or showing arguments to a person."""
    if isinstance(value, SecretString):
        return _REDACTED_ARGUMENT
    if isinstance(value, Mapping):
        nested: dict[str, Any] = {}
        for key, item in cast(Mapping[Any, Any], value).items():
            nested[str(key)] = redact_arguments(item)
        return nested
    if isinstance(value, list | tuple):
        redacted_items: list[Any] = []
        for item in cast(list[Any] | tuple[Any, ...], value):
            redacted_items.append(redact_arguments(item))
        return redacted_items
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise TypeError(f"Unsupported argument type: {type(value).__name__}")


def redact_mapping(arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Return a JSON-ready copy of tool arguments with secrets removed."""
    redacted: dict[str, Any] = {}
    for key, item in arguments.items():
        redacted[str(key)] = redact_arguments(item)
    return redacted


def argument_hash(arguments: Mapping[str, Any]) -> str:
    """Hash canonical, redacted arguments. The secret value does not affect the hash."""
    payload = json.dumps(
        redact_mapping(arguments),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
