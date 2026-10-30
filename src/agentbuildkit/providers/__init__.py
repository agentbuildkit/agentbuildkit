"""Chat providers selected per agent.

The built-in registry contains OpenAI and Anthropic. Another provider is added
by registering an adapter. Agent execution does not choose a provider itself.
"""

from agentbuildkit.providers.anthropic import ANTHROPIC_PROVIDER_ID, AnthropicProvider
from agentbuildkit.providers.base import (
    ChatProvider,
    ProviderConfiguration,
    ProviderError,
)
from agentbuildkit.providers.openai import OPENAI_PROVIDER_ID, OpenAIProvider
from agentbuildkit.providers.registry import ProviderRegistry

__all__ = [
    "ANTHROPIC_PROVIDER_ID",
    "OPENAI_PROVIDER_ID",
    "AnthropicProvider",
    "ChatProvider",
    "OpenAIProvider",
    "ProviderConfiguration",
    "ProviderError",
    "ProviderRegistry",
    "builtin_registry",
]


def builtin_registry(*, env_file_path: str | None = None) -> ProviderRegistry:
    """Return a registry containing the providers shipped in this version.

    A later provider is added with ``register`` on a registry. This function
    does not search the environment for plugins.
    """
    registry = ProviderRegistry()
    registry.register(OpenAIProvider(env_file_path=env_file_path))
    registry.register(AnthropicProvider(env_file_path=env_file_path))
    return registry
