"""Resolve a provider id to the adapter that can build its chat client."""

from agent_framework import SupportsChatGetResponse

from agentbuildkit.providers.base import (
    ChatProvider,
    ProviderConfiguration,
    ProviderError,
    require_provider_id,
)


class ProviderRegistry:
    """Providers registered by id.

    Registering the same id twice raises. This registry does not discover
    plugins and does not replace an existing provider.
    """

    def __init__(self) -> None:
        self._providers: dict[str, ChatProvider] = {}

    def register(self, provider: ChatProvider) -> None:
        """Add a provider. A second registration of the same id fails."""
        provider_id = require_provider_id(provider.id)
        if provider_id in self._providers:
            raise ProviderError(f"Provider '{provider_id}' is already registered.")
        self._providers[provider_id] = provider

    def resolve(self, provider_id: str) -> ChatProvider:
        """Return the adapter registered under ``provider_id``."""
        key = require_provider_id(provider_id)
        provider = self._providers.get(key)
        if provider is None:
            raise ProviderError(f"Unknown provider: {key}")
        return provider

    def create_client(self, configuration: ProviderConfiguration) -> SupportsChatGetResponse:
        """Build the chat client described by ``configuration``."""
        provider = self.resolve(configuration.provider)
        requested = ProviderConfiguration(provider=provider.id, model=configuration.model)
        return provider.create_client(requested)
