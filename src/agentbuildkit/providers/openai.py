"""OpenAI chat clients.

Credentials come from ``OPENAI_`` settings. The model id comes from the agent
configuration.
"""

from agent_framework import SecretString
from agent_framework.openai import OpenAIChatClient, OpenAISettings

from agentbuildkit.config import load_openai_settings
from agentbuildkit.providers.base import ProviderConfiguration, ProviderError

OPENAI_PROVIDER_ID = "openai"


class OpenAIProvider:
    """Build one ``OpenAIChatClient`` for the model an agent requested."""

    def __init__(
        self,
        *,
        env_file_path: str | None = None,
        settings: OpenAISettings | None = None,
    ) -> None:
        self._env_file_path = env_file_path
        self._settings = settings

    @property
    def id(self) -> str:
        """Registry id for this adapter."""
        return OPENAI_PROVIDER_ID

    def create_client(self, configuration: ProviderConfiguration) -> OpenAIChatClient:
        """Return a client for ``configuration.model`` without calling the API."""
        if configuration.provider != self.id:
            raise ProviderError(f"Unknown provider: {configuration.provider}")
        return OpenAIChatClient(model=configuration.model, api_key=self._api_key())

    def _api_key(self) -> SecretString | str:
        settings = self._settings
        if settings is None:
            settings = load_openai_settings(env_file_path=self._env_file_path)
        api_key = settings.get("api_key")
        if isinstance(api_key, SecretString) and api_key.get_secret_value() != "":
            return api_key
        if isinstance(api_key, str) and api_key != "":
            return api_key
        raise ProviderError("OPENAI_API_KEY is not configured.")
