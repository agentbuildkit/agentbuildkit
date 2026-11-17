"""Clients for the installed dashboard.

Startup keeps a description of each provider. A live client is constructed on
the first model call, and the Composio client is constructed on the first
account operation.
"""

import importlib.util
from collections.abc import Callable, Mapping
from typing import Any, cast

from agent_framework import SecretString, SupportsChatGetResponse

from agentbuildkit.integrations.composio import (
    AccountView,
    ComposioGateway,
    LinkRequest,
    LiveComposioGateway,
)
from agentbuildkit.providers import ProviderConfiguration, ProviderError, builtin_registry
from agentbuildkit.teams import TeamBundle


class DeferredChatClient:
    """Forward chat calls after building the provider client."""

    additional_properties: dict[str, object]

    def __init__(self, *, provider: str, model: str, env_file_path: str | None) -> None:
        self.provider = provider
        self.model = model
        self._env_file_path = env_file_path
        self._client: SupportsChatGetResponse | None = None
        self.additional_properties = {}

    def get_response(self, *args: object, **kwargs: object) -> object:
        """Build the provider client, then forward the call."""
        resolved: Any = self._resolve()
        method: Callable[..., object] = resolved.get_response
        return method(*args, **kwargs)

    def _resolve(self) -> SupportsChatGetResponse:
        if self._client is not None:
            return self._client
        try:
            client = builtin_registry(env_file_path=self._env_file_path).create_client(
                ProviderConfiguration(provider=self.provider, model=self.model)
            )
        except ProviderError as exc:
            raise ProviderError(
                f"{self.provider} is not configured. A live run needs that credential."
            ) from exc
        self._client = client
        return client


def dashboard_clients(
    bundle: TeamBundle,
    *,
    env_file_path: str | None,
) -> dict[str, SupportsChatGetResponse]:
    """One deferred client per role. This does not construct a provider client."""
    return {
        agent.name: cast(
            SupportsChatGetResponse,
            DeferredChatClient(
                provider=agent.provider,
                model=agent.model,
                env_file_path=env_file_path,
            ),
        )
        for agent in bundle.agents
    }


class DeferredGateway:
    """Hold the Composio settings until an account operation needs the client."""

    def __init__(self, api_key: SecretString, auth_config_id: str) -> None:
        self._api_key = api_key
        self.auth_config_id = auth_config_id.strip()
        self._inner: LiveComposioGateway | None = None

    def start_link(self, *, user_id: str, toolkit: str, callback_url: str) -> LinkRequest:
        return self._gateway().start_link(
            user_id=user_id,
            toolkit=toolkit,
            callback_url=callback_url,
        )

    def get_account(self, account_id: str) -> AccountView:
        return self._gateway().get_account(account_id)

    def execute(
        self,
        *,
        slug: str,
        version: str,
        account_id: str,
        user_id: str,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return self._gateway().execute(
            slug=slug,
            version=version,
            account_id=account_id,
            user_id=user_id,
            arguments=arguments,
        )

    def _gateway(self) -> LiveComposioGateway:
        if self._inner is None:
            self._inner = LiveComposioGateway(self._api_key, auth_config_id=self.auth_config_id)
        return self._inner


def optional_gateway(
    api_key: SecretString | None,
    auth_config_id: str,
) -> tuple[ComposioGateway | None, bool]:
    """Return a deferred gateway. A missing package does not construct a client."""
    if api_key is None:
        return None, False
    if importlib.util.find_spec("composio") is None:
        return None, True
    return DeferredGateway(api_key, auth_config_id), False
