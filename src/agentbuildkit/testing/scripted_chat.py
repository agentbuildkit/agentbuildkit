"""Offline chat client that returns scripted responses and never calls a model."""

from collections.abc import AsyncIterable, Mapping, Sequence
from typing import Any, cast

from agent_framework import (
    BaseChatClient,
    ChatResponse,
    ChatResponseUpdate,
    Content,
    FunctionInvocationLayer,
    Message,
    RoleLiteral,
)


class ScriptedChatClient(FunctionInvocationLayer, BaseChatClient[Any]):
    """Return prepared responses in order.

    A string script becomes one assistant text message. The client records each
    request and raises when the script is exhausted. Function calls in a script
    go through the framework's tool loop, including its approval pause.
    """

    def __init__(self, responses: Sequence[str | ChatResponse]) -> None:
        super().__init__()
        self._responses: list[ChatResponse] = [_as_response(response) for response in responses]
        self.requests: list[list[Message]] = []
        self.invocations: list[dict[str, Any]] = []

    def queued_responses(self) -> tuple[ChatResponse, ...]:
        """Return the unused script, so a test can combine several scenarios."""
        return tuple(self._responses)

    def _inner_get_response(
        self,
        *,
        messages: Sequence[Message],
        stream: bool,
        options: Mapping[str, Any],
        **kwargs: Any,
    ) -> Any:
        del kwargs
        if stream:
            return self._build_response_stream(self._stream(messages, options))
        return self._respond(messages, options)

    async def _respond(
        self, messages: Sequence[Message], options: Mapping[str, Any]
    ) -> ChatResponse:
        await self._validate_options(options)
        self._note(messages, options)
        return self._pop()

    def _stream(
        self,
        messages: Sequence[Message],
        options: Mapping[str, Any],
    ) -> AsyncIterable[ChatResponseUpdate]:
        async def generate() -> AsyncIterable[ChatResponseUpdate]:
            await self._validate_options(options)
            self._note(messages, options)
            response = self._pop()
            for message in response.messages:
                yield ChatResponseUpdate(
                    role=_role(message.role),
                    contents=[Content.from_text(message.text)],
                    response_id=response.response_id,
                )

        return generate()

    def _note(self, messages: Sequence[Message], options: Mapping[str, Any]) -> None:
        self.requests.append(list(messages))
        tool_names: list[str] = []
        tools = options.get("tools")
        if isinstance(tools, Sequence) and not isinstance(tools, str | bytes):
            for item in cast(Sequence[object], tools):
                name = getattr(item, "name", None)
                if isinstance(name, str):
                    tool_names.append(name)
        instructions = options.get("instructions")
        self.invocations.append(
            {
                "instructions": instructions if isinstance(instructions, str) else None,
                "tool_names": tool_names,
            }
        )

    def _pop(self) -> ChatResponse:
        if not self._responses:
            raise RuntimeError("ScriptedChatClient has no responses left.")
        return self._responses.pop(0)

    def service_url(self) -> str:
        return "scripted://offline"


def _role(role: str) -> RoleLiteral:
    if role in {"system", "user", "assistant", "tool"}:
        return cast(RoleLiteral, role)
    return "assistant"


def _as_response(response: str | ChatResponse) -> ChatResponse:
    if isinstance(response, ChatResponse):
        return response
    return ChatResponse(
        messages=[Message(role="assistant", contents=[response])],
        response_id="scripted",
    )
