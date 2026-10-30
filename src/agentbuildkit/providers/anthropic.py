"""Anthropic chat clients.

Credentials come from ``ANTHROPIC_API_KEY``. The model id comes from the agent
configuration. ``AnthropicSettings`` is not part of the public client export, so
this module loads only the API key.
"""

from typing import TypedDict

from agent_framework import SecretString, load_settings
from agent_framework.anthropic import AnthropicClient

from agentbuildkit.providers.base import ProviderConfiguration, ProviderError

ANTHROPIC_PROVIDER_ID = "anthropic"


class AnthropicCredentials(TypedDict, total=False):
    """The Anthropic API key. The agent model is not stored here."""

    api_key: SecretString | None


class AnthropicProvider:
    """Build one ``AnthropicClient`` for the model an agent requested."""

    def __init__(
        self,
        *,
        env_file_path: str | None = None,
        settings: AnthropicCredentials | None = None,
    ) -> None:
        self._env_file_path = env_file_path
        self._settings = settings

    @property
    def id(self) -> str:
        """Registry id for this adapter."""
        return ANTHROPIC_PROVIDER_ID

    def create_client(self, configuration: ProviderConfiguration) -> AnthropicClient:
        """Return a client for ``configuration.model`` without calling the API."""
        if configuration.provider != self.id:
            raise ProviderError(f"Unknown provider: {configuration.provider}")
        return AnthropicClient(model=configuration.model, api_key=self._api_key())

    def _api_key(self) -> SecretString | str:
        settings = self._settings
        if settings is None:
            settings = load_settings(
                AnthropicCredentials,
                env_prefix="ANTHROPIC_",
                env_file_path=self._env_file_path,
            )
        api_key = settings.get("api_key")
        if isinstance(api_key, SecretString) and api_key.get_secret_value() != "":
            return api_key
        if isinstance(api_key, str) and api_key != "":
            return api_key
        raise ProviderError("ANTHROPIC_API_KEY is not configured.")
