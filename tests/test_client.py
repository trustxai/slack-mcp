"""Unit tests for SlackClient using httpx.MockTransport."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

import slack_mcp.client as client_module
from slack_mcp.client import (
    JSON_METHODS,
    KNOWN_METHODS,
    READ_METHODS,
    WRITE_METHODS,
    SlackClient,
    UnknownMethodError,
    compact_params,
    encoding_for,
    get_client,
    reset_client,
)
from slack_mcp.config import Settings, TokenTypeError
from slack_mcp.errors import SlackApiError
from slack_mcp.guard import WriteNotAllowedError, WritesDisabledError

TOKEN = "xoxp-FAKE-0123456789abcdef"
ME = "U0SELF00001"
CHANNEL = "C0123ABCDEF"


class _Recorder:
    """A MockTransport handler: routes by method name, records every request."""

    def __init__(
        self,
        routes: dict[str, Any] | None = None,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.routes = routes or {}
        self.status = status
        self.headers = headers or {}
        self.requests: list[httpx.Request] = []

    @property
    def methods(self) -> list[str]:
        return [request.url.path.rsplit("/", 1)[-1] for request in self.requests]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        method = request.url.path.rsplit("/", 1)[-1]
        route = self.routes.get(method, {"ok": True})
        if isinstance(route, httpx.Response):
            return route
        if callable(route):
            return route(request)
        return httpx.Response(self.status, json=route, headers=self.headers)


def _client(handler: Any, **settings_kwargs: Any) -> SlackClient:
    settings_kwargs.setdefault("slack_user_token", TOKEN)
    return SlackClient(settings=Settings(**settings_kwargs), transport=httpx.MockTransport(handler))


def _form(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(request.content.decode(), keep_blank_values=True)


# --- pure helpers -----------------------------------------------------------------------


def test_method_sets() -> None:
    assert JSON_METHODS == frozenset({"chat.postMessage", "chat.update", "chat.scheduleMessage"})
    assert JSON_METHODS <= WRITE_METHODS
    assert "conversations.open" in WRITE_METHODS
    assert "chat.deleteScheduledMessage" in WRITE_METHODS
    assert not READ_METHODS & WRITE_METHODS
    assert KNOWN_METHODS == READ_METHODS | WRITE_METHODS
    # Destructive or membership-changing methods are not reachable at all.
    for method in ("chat.delete", "conversations.join", "conversations.leave", "conversations.kick", "auth.revoke"):
        assert method not in KNOWN_METHODS
    # No v0.1 tool calls reactions.get, and its scope (reactions:read) is not requested.
    assert "reactions.get" not in KNOWN_METHODS


@pytest.mark.parametrize(
    ("method", "encoding"),
    [
        ("chat.postMessage", "json"),
        ("chat.update", "json"),
        ("chat.scheduleMessage", "json"),
        ("chat.deleteScheduledMessage", "form"),
        ("reactions.add", "form"),
        ("reactions.remove", "form"),
        ("conversations.open", "form"),
        ("chat.getPermalink", "form"),
        ("conversations.list", "form"),
        ("search.messages", "form"),
        ("users.lookupByEmail", "form"),
        ("auth.test", "form"),
    ],
)
def test_encoding_table(method: str, encoding: str) -> None:
    assert encoding_for(method) == encoding


def test_compact_params_form() -> None:
    assert compact_params(
        {
            "channel": CHANNEL,
            "cursor": None,
            "exclude_archived": True,
            "inclusive": False,
            "limit": 50,
            "types": ["public_channel", "private_channel"],
            "users": ("U1AAAAAAAA", None, "U2BBBBBBBB"),
            "empty": [],
            "blocks": [{"type": "section"}],
        },
        form=True,
    ) == {
        "channel": CHANNEL,
        "exclude_archived": "true",
        "inclusive": "false",
        "limit": "50",
        "types": "public_channel,private_channel",
        "users": "U1AAAAAAAA,U2BBBBBBBB",
        "blocks": '[{"type": "section"}]',
    }
    assert compact_params(None, form=True) == {}
    assert compact_params({}, form=True) == {}


def test_compact_params_json_keeps_native_types() -> None:
    assert compact_params(
        {"channel": CHANNEL, "text": "hi", "unfurl_links": False, "post_at": 1760000000, "thread_ts": None},
        form=False,
    ) == {"channel": CHANNEL, "text": "hi", "unfurl_links": False, "post_at": 1760000000}
    assert compact_params({"users": ["U1AAAAAAAA", "U2BBBBBBBB"]}, form=False) == {"users": "U1AAAAAAAA,U2BBBBBBBB"}
    assert compact_params({"metadata": {"a": 1}, "blocks": [{"type": "x"}]}, form=False) == {
        "metadata": {"a": 1},
        "blocks": [{"type": "x"}],
    }


# --- requests: auth + encoding ------------------------------------------------------------


async def test_read_is_form_encoded_with_bearer() -> None:
    handler = _Recorder({"conversations.list": {"ok": True, "channels": []}})
    client = _client(handler)

    body = await client.call(
        "conversations.list",
        {"types": ["public_channel", "private_channel"], "exclude_archived": True, "limit": 100, "cursor": None},
    )

    assert body == {"ok": True, "channels": []}
    [request] = handler.requests
    assert request.method == "POST"
    assert str(request.url) == "https://slack.com/api/conversations.list"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert request.headers["accept"] == "application/json"
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    assert _form(request) == {
        "types": ["public_channel,private_channel"],
        "exclude_archived": ["true"],
        "limit": ["100"],
    }
    assert TOKEN not in request.content.decode()  # the token travels in the header only


async def test_call_without_params_sends_no_body() -> None:
    handler = _Recorder({"auth.test": {"ok": True, "user_id": ME}})
    client = _client(handler)
    assert (await client.call("auth.test"))["user_id"] == ME
    assert handler.requests[0].content == b""


async def test_post_message_is_json_encoded_with_charset() -> None:
    handler = _Recorder({"chat.postMessage": {"ok": True, "channel": CHANNEL, "ts": "1712345678.123456"}})
    client = _client(handler, slack_allow_writes=True, slack_write_channels=CHANNEL)

    await client.call(
        "chat.postMessage",
        {"channel": CHANNEL, "text": "héllo <@U0123ABCDEF>", "thread_ts": None, "unfurl_links": False, "mrkdwn": True},
    )

    [request] = handler.requests
    assert request.headers["content-type"] == "application/json; charset=utf-8"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert json.loads(request.content) == {
        "channel": CHANNEL,
        "text": "héllo <@U0123ABCDEF>",
        "unfurl_links": False,
        "mrkdwn": True,
    }


async def test_chat_update_is_json_encoded() -> None:
    handler = _Recorder({"chat.update": {"ok": True, "channel": CHANNEL, "ts": "1712345678.123456", "text": "v2"}})
    client = _client(handler, slack_allow_writes=True, slack_write_channels=CHANNEL)

    await client.call("chat.update", {"channel": CHANNEL, "ts": "1712345678.123456", "text": "v2", "blocks": None})

    [request] = handler.requests
    assert request.headers["content-type"] == "application/json; charset=utf-8"
    assert json.loads(request.content) == {"channel": CHANNEL, "ts": "1712345678.123456", "text": "v2"}


async def test_chat_schedule_message_is_json_encoded_with_an_integer_post_at() -> None:
    handler = _Recorder({"chat.scheduleMessage": {"ok": True, "channel": CHANNEL, "scheduled_message_id": "Q1"}})
    client = _client(handler, slack_allow_writes=True, slack_write_channels=CHANNEL)

    await client.call(
        "chat.scheduleMessage",
        {"channel": CHANNEL, "text": "later", "post_at": 1791000000, "thread_ts": None, "unfurl_links": False},
    )

    [request] = handler.requests
    assert request.headers["content-type"] == "application/json; charset=utf-8"
    assert json.loads(request.content) == {
        "channel": CHANNEL,
        "text": "later",
        "post_at": 1791000000,
        "unfurl_links": False,
    }


async def test_reaction_write_is_form_encoded() -> None:
    handler = _Recorder()
    client = _client(handler, slack_allow_writes=True, slack_write_channels=CHANNEL)

    await client.call("reactions.add", {"channel": CHANNEL, "timestamp": "1712345678.123456", "name": "thumbsup"})

    [request] = handler.requests
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    assert _form(request) == {"channel": [CHANNEL], "timestamp": ["1712345678.123456"], "name": ["thumbsup"]}


async def test_conversations_open_is_form_encoded_with_joined_users() -> None:
    handler = _Recorder({"conversations.open": {"ok": True, "channel": {"id": "G0123ABCDEF"}}})
    client = _client(handler, slack_allow_writes=True, slack_write_users="U1AAAAAAAA,U2BBBBBBBB")

    await client.call("conversations.open", {"users": ["U1AAAAAAAA", "U2BBBBBBBB"], "return_im": True})

    assert handler.methods == ["conversations.open"]  # every user allowlisted: no auth.test needed
    assert _form(handler.requests[0]) == {"users": ["U1AAAAAAAA,U2BBBBBBBB"], "return_im": ["true"]}


# --- the write guard inside the client ---------------------------------------------------


async def test_write_refused_when_writes_disabled_before_any_network_call() -> None:
    handler = _Recorder()
    client = _client(handler)

    with pytest.raises(WritesDisabledError, match="SLACK_ALLOW_WRITES=1"):
        await client.call("chat.postMessage", {"channel": CHANNEL, "text": "hi"})
    assert handler.requests == []


async def test_write_to_unlisted_channel_is_refused_after_the_info_lookup() -> None:
    handler = _Recorder(
        {"conversations.info": {"ok": True, "channel": {"id": CHANNEL, "name": "general", "is_channel": True}}}
    )
    client = _client(handler, slack_allow_writes=True, slack_write_channels="C0999ZZZZZZ")

    with pytest.raises(WriteNotAllowedError, match="SLACK_WRITE_CHANNELS") as info:
        await client.call("chat.postMessage", {"channel": CHANNEL, "text": "hi"})

    assert "#general (C0123ABCDEF)" in str(info.value)
    assert handler.methods == ["conversations.info"]  # chat.postMessage never sent
    assert _form(handler.requests[0]) == {"channel": [CHANNEL]}
    assert handler.requests[0].headers["authorization"] == f"Bearer {TOKEN}"


async def test_write_to_allowlisted_channel_passes_without_a_lookup() -> None:
    handler = _Recorder({"chat.postMessage": {"ok": True, "channel": CHANNEL, "ts": "1712345678.123456"}})
    client = _client(handler, slack_allow_writes=True, slack_write_channels=f"C0999ZZZZZZ,{CHANNEL}")

    body = await client.call("chat.postMessage", {"channel": CHANNEL, "text": "hi"})

    assert body["ts"] == "1712345678.123456"
    assert handler.methods == ["chat.postMessage"]


async def test_write_by_channel_name_is_refused_even_if_the_name_is_listed() -> None:
    handler = _Recorder()
    client = _client(handler, slack_allow_writes=True, slack_write_channels="#general,general")

    with pytest.raises(WriteNotAllowedError, match="conversation ID"):
        await client.call("chat.postMessage", {"channel": "#general", "text": "hi"})
    with pytest.raises(WriteNotAllowedError, match="conversation ID"):
        await client.call("chat.postMessage", {"channel": "U0123ABCDEF", "text": "hi"})
    assert handler.requests == []


async def test_dm_write_checks_membership_minus_self() -> None:
    handler = _Recorder(
        {
            "conversations.info": {"ok": True, "channel": {"id": "D0123ABCDEF", "is_im": True, "user": "U0FRIEND01"}},
            "conversations.members": {"ok": True, "members": [ME, "U0FRIEND01"]},
            "auth.test": {"ok": True, "user_id": ME},
            "chat.postMessage": {"ok": True, "channel": "D0123ABCDEF", "ts": "1712345678.123456"},
        }
    )
    client = _client(handler, slack_allow_writes=True, slack_write_users="U0FRIEND01")

    await client.call("chat.postMessage", {"channel": "D0123ABCDEF", "text": "hi"})

    assert handler.methods == ["conversations.info", "conversations.members", "auth.test", "chat.postMessage"]


async def test_guard_lookups_cannot_call_write_methods() -> None:
    handler = _Recorder()
    client = _client(handler, slack_allow_writes=True)
    with pytest.raises(UnknownMethodError, match="read methods"):
        await client._guard_read("chat.postMessage", {"channel": CHANNEL})
    with pytest.raises(UnknownMethodError):
        await client._guard_read("chat.delete", {"channel": CHANNEL})
    assert handler.requests == []


async def test_unknown_method_is_refused_before_anything_else() -> None:
    handler = _Recorder()
    client = SlackClient(settings=Settings(), transport=httpx.MockTransport(handler))  # no token either
    for method in ("chat.delete", "conversations.join", "auth.revoke", "chat.postMessage/../auth.revoke"):
        with pytest.raises(UnknownMethodError, match="not on this server's method list"):
            await client.call(method, {"channel": CHANNEL})
    assert handler.requests == []


# --- the envelope -------------------------------------------------------------------------


async def test_ok_false_raises_slack_api_error_with_the_envelope() -> None:
    body = {
        "ok": False,
        "error": "missing_scope",
        "needed": "search:read",
        "provided": "chat:write,users:read",
        "response_metadata": {"messages": ["[ERROR] scope missing"]},
    }
    client = _client(_Recorder({"search.messages": body}))

    with pytest.raises(SlackApiError) as info:
        await client.call("search.messages", {"query": "deploy"})

    assert info.value.code == "missing_scope"
    assert info.value.method == "search.messages"
    assert info.value.needed == "search:read"
    assert info.value.provided == "chat:write,users:read"
    assert info.value.messages == ["[ERROR] scope missing"]
    assert info.value.body == body


async def test_ok_false_without_error_code() -> None:
    client = _client(_Recorder({"auth.test": {"ok": False}}))
    with pytest.raises(SlackApiError) as info:
        await client.call("auth.test")
    assert info.value.code == "unknown_error"


async def test_warnings_on_an_ok_body_are_kept_intact() -> None:
    body = {
        "ok": True,
        "channel": CHANNEL,
        "ts": "1712345678.123456",
        "warning": "message_truncated",
        "response_metadata": {"warnings": ["message_truncated"]},
    }
    client = _client(_Recorder({"chat.postMessage": body}), slack_allow_writes=True, slack_write_channels=CHANNEL)
    assert await client.call("chat.postMessage", {"channel": CHANNEL, "text": "x"}) == body


@pytest.mark.parametrize("payload", ["<html>ok</html>", "[1, 2]", "null"])
async def test_non_json_or_non_object_200_raises(payload: str) -> None:
    handler = _Recorder({"auth.test": httpx.Response(200, text=payload)})
    client = _client(handler)
    with pytest.raises(SlackApiError) as info:
        await client.call("auth.test")
    assert info.value.code == "non_json_response"


async def test_http_error_raises_status_error() -> None:
    handler = _Recorder({"auth.test": httpx.Response(500, text="boom")})
    with pytest.raises(httpx.HTTPStatusError) as info:
        await _client(handler).call("auth.test")
    assert info.value.response.status_code == 500


# --- 429 / retry rules ----------------------------------------------------------------------


def _rate_limited(retry_after: str | None) -> httpx.Response:
    headers = {"retry-after": retry_after} if retry_after is not None else {}
    return httpx.Response(429, json={"ok": False, "error": "ratelimited"}, headers=headers)


async def test_read_429_with_short_retry_after_retries_once(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("slack_mcp.client._sleep", fake_sleep)
    responses = iter([_rate_limited("2"), httpx.Response(200, json={"ok": True, "members": []})])
    handler = _Recorder({"users.list": lambda request: next(responses)})
    client = _client(handler)

    body = await client.call("users.list", {"limit": 200})

    assert body == {"ok": True, "members": []}
    assert handler.methods == ["users.list", "users.list"]
    assert slept == [2.0]
    assert client.last_retry_after is None  # captured from the final (successful) response


async def test_read_429_retried_once_only(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr("slack_mcp.client._sleep", fake_sleep)
    handler = _Recorder({"users.list": lambda request: _rate_limited("1")})
    client = _client(handler)

    with pytest.raises(httpx.HTTPStatusError):
        await client.call("users.list")
    assert handler.methods == ["users.list", "users.list"]
    assert client.last_retry_after == "1"


@pytest.mark.parametrize("retry_after", ["60", "11", None, "soon"])
async def test_read_429_with_long_or_missing_retry_after_is_not_retried(
    monkeypatch: pytest.MonkeyPatch, retry_after: str | None
) -> None:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("slack_mcp.client._sleep", fake_sleep)
    handler = _Recorder({"conversations.history": lambda request: _rate_limited(retry_after)})
    client = _client(handler)

    with pytest.raises(httpx.HTTPStatusError) as info:
        await client.call("conversations.history", {"channel": CHANNEL})

    assert info.value.response.status_code == 429
    assert handler.methods == ["conversations.history"]
    assert slept == []
    assert client.last_retry_after == retry_after


async def test_write_429_is_never_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("slack_mcp.client._sleep", fake_sleep)
    handler = _Recorder({"chat.postMessage": lambda request: _rate_limited("1")})
    client = _client(handler, slack_allow_writes=True, slack_write_channels=CHANNEL)

    with pytest.raises(httpx.HTTPStatusError):
        await client.call("chat.postMessage", {"channel": CHANNEL, "text": "hi"})

    assert handler.methods == ["chat.postMessage"]
    assert slept == []


@pytest.mark.parametrize("status", [500, 502, 503])
async def test_write_is_not_retried_after_a_5xx(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("slack_mcp.client._sleep", fake_sleep)
    handler = _Recorder(
        {"chat.postMessage": lambda request: httpx.Response(status, text="upstream", headers={"retry-after": "1"})}
    )
    client = _client(handler, slack_allow_writes=True, slack_write_channels=CHANNEL)

    with pytest.raises(httpx.HTTPStatusError) as info:
        await client.call("chat.postMessage", {"channel": CHANNEL, "text": "hi"})

    assert info.value.response.status_code == status
    assert len(handler.requests) == 1
    assert slept == []


async def test_write_is_not_retried_after_a_timeout() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    handler = _Recorder({"chat.postMessage": timeout})
    client = _client(handler, slack_allow_writes=True, slack_write_channels=CHANNEL)

    with pytest.raises(httpx.ReadTimeout):
        await client.call("chat.postMessage", {"channel": CHANNEL, "text": "hi"})

    assert len(handler.requests) == 1


# --- token checks --------------------------------------------------------------------------


async def test_missing_token_errors_before_any_request() -> None:
    handler = _Recorder()
    client = SlackClient(settings=Settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match="No Slack user token configured"):
        await client.call("auth.test")
    assert handler.requests == []


async def test_bot_token_errors_before_any_request() -> None:
    handler = _Recorder()
    token = "xoxb-FAKE-0123456789abcdef"
    client = _client(handler, slack_user_token=token, slack_allow_writes=True, slack_write_channels=CHANNEL)
    with pytest.raises(TokenTypeError, match="a bot token") as info:
        await client.call("chat.postMessage", {"channel": CHANNEL, "text": "hi"})
    assert token not in str(info.value)
    assert handler.requests == []


# --- diagnostics, base URL, timeout, singleton ------------------------------------------------


async def test_scopes_and_retry_after_are_captured() -> None:
    handler = _Recorder(
        {"auth.test": {"ok": True}},
        headers={"x-oauth-scopes": "chat:write,users:read", "x-accepted-oauth-scopes": "", "retry-after": "12"},
    )
    client = _client(handler)
    assert client.last_scopes is None
    assert client.last_retry_after is None
    await client.call("auth.test")
    assert client.last_scopes == "chat:write,users:read"
    assert client.last_accepted_scopes == ""
    assert client.last_retry_after == "12"


async def test_custom_base_url_join() -> None:
    handler = _Recorder()
    client = _client(handler, slack_api_url="https://proxy.example.test/slack/api/")
    await client.call("auth.test")
    assert str(handler.requests[0].url) == "https://proxy.example.test/slack/api/auth.test"


async def test_timeout_override_is_applied() -> None:
    seen: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions.get("timeout"))
        return httpx.Response(200, json={"ok": True})

    client = _client(handler, slack_request_timeout_seconds=3.0)
    await client.call("auth.test")
    assert seen[-1]["read"] == 3.0
    await client.call("auth.test", timeout=0.5)
    assert seen[-1]["read"] == 0.5


def test_get_client_singleton_and_reset() -> None:
    first = get_client()
    assert get_client() is first
    reset_client()
    assert client_module._client is None
    assert get_client() is not first
