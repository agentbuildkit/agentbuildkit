"""Correlation id for the current agent or workflow turn.

Integrations read this value. They do not accept it from model output.
"""

from contextvars import ContextVar, Token

_correlation_id: ContextVar[str | None] = ContextVar("agent_platform_correlation_id", default=None)


def bind_correlation_id(correlation_id: str) -> Token[str | None]:
    """Make a correlation id visible to tools and integrations in this turn."""
    return _correlation_id.set(correlation_id)


def reset_correlation_id(token: Token[str | None]) -> None:
    """Restore the previous correlation id."""
    _correlation_id.reset(token)


def current_correlation_id() -> str | None:
    """Return the correlation id for this turn, if one was bound."""
    return _correlation_id.get()
