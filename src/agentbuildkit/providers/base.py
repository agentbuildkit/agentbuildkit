"""Provider identity and the contract a chat adapter implements.

An agent asks for a provider id and a model id. An adapter turns that pair
into a chat client. Credentials stay inside the adapter.
"""

from dataclasses import dataclass
from typing import Protocol

from agent_framework import SupportsChatGetResponse


class ProviderError(Exception):
    """A provider id, model id, or provider configuration cannot be used."""


def require_provider_id(value: object) -> str:
    """Return a provider id, or raise when it is missing."""
    return _require_id(value, "Provider id")


def require_model_id(value: object) -> str:
    """Return a model id, or raise when it is missing."""
    return _require_id(value, "Model id")


def _require_id(value: object, label: str) -> str:
    if not isinstance(value, str) or value.strip() == "":
        raise ProviderError(f"{label} must not be empty.")
    return value.strip()


@dataclass(frozen=True)
class ProviderConfiguration:
    """The provider and model an agent asked for.

    This is not a credential store. API keys stay with the adapter that reads
    them from its own settings.
    """

    provider: str
    model: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", require_provider_id(self.provider))
        object.__setattr__(self, "model", require_model_id(self.model))


class ChatProvider(Protocol):
    """One chat backend. The registry selects it. The adapter builds the client."""

    @property
    def id(self) -> str:
        """Stable provider id, such as ``openai``."""
        ...

    def create_client(self, configuration: ProviderConfiguration) -> SupportsChatGetResponse:
        """Build a chat client for the requested model."""
        ...
