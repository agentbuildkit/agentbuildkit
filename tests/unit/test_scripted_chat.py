"""The scripted chat client answers from memory and does not call a provider."""

import pytest
from agent_framework import Message

from agent_platform.testing import ScriptedChatClient


async def test_scripted_client_returns_responses_in_order() -> None:
    client = ScriptedChatClient(["first", "second"])

    first = await client.get_response([Message(role="user", contents=["hello"])])
    second = await client.get_response([Message(role="user", contents=["again"])])

    assert first.messages[0].text == "first"
    assert second.messages[0].text == "second"
    assert [message.contents[0].text for request in client.requests for message in request] == [
        "hello",
        "again",
    ]


async def test_scripted_client_raises_when_the_script_is_exhausted() -> None:
    client = ScriptedChatClient(["only"])
    await client.get_response([Message(role="user", contents=["hello"])])

    with pytest.raises(RuntimeError, match="no responses left"):
        await client.get_response([Message(role="user", contents=["hello"])])


async def test_scripted_client_can_stream_a_response() -> None:
    client = ScriptedChatClient(["streamed"])

    chunks = [
        update.text
        async for update in client.get_response(
            [Message(role="user", contents=["hello"])],
            stream=True,
        )
    ]

    assert chunks == ["streamed"]
    assert client.service_url() == "scripted://offline"
