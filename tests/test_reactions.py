"""Unit tests for the reaction tools against a fake client (and once through the real guard)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from slack_mcp.client import SlackClient
from slack_mcp.config import Settings
from slack_mcp.errors import SlackApiError
from slack_mcp.guard import WriteNotAllowedError, WritesDisabledError
from slack_mcp.server import mcp
from slack_mcp.tools.reactions import ReactionInput, slack_add_reaction, slack_remove_reaction

TOKEN = "xoxp-FAKE-0123456789abcdef"
CHANNEL = "C0123ABCDEF"
TS = "1712345678.123456"


class _FakeClient:
    """Routes by method name; records every call as (method, params)."""

    def __init__(self, routes: dict[str, Any] | None = None) -> None:
        self._routes = routes or {}
        self.calls: list[tuple[str, Any]] = []

    async def call(self, method: str, params: Any = None, **kw: Any) -> dict[str, Any]:
        self.calls.append((method, params))
        payload = self._routes.get(method)
        if isinstance(payload, Exception):
            raise payload
        return dict(payload) if payload is not None else {"ok": True}


def _patch(monkeypatch: pytest.MonkeyPatch, fake: Any) -> None:
    monkeypatch.setattr("slack_mcp.tools.reactions.get_client", lambda: fake)


def _input(name: str = "thumbsup", **overrides: Any) -> ReactionInput:
    return ReactionInput(**{"channel": CHANNEL, "timestamp": TS, "name": name, **overrides})


# -- slack_add_reaction ---------------------------------------------------------------


async def test_add_reaction_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(routes={"reactions.add": {"ok": True}})
    _patch(monkeypatch, fake)

    result = await slack_add_reaction(_input("eyes"))

    assert result == f"Added reaction :eyes: to message {TS} in {CHANNEL}."
    assert fake.calls == [("reactions.add", {"channel": CHANNEL, "timestamp": TS, "name": "eyes"})]


@pytest.mark.parametrize(
    ("raw", "sent"),
    [
        (":white_check_mark:", "white_check_mark"),
        ("  :Thumbsup: ", "thumbsup"),
        (":thumbsup::skin-tone-3:", "thumbsup::skin-tone-3"),
    ],
)
async def test_add_reaction_strips_colons_from_the_sent_name(
    monkeypatch: pytest.MonkeyPatch, raw: str, sent: str
) -> None:
    fake = _FakeClient()
    _patch(monkeypatch, fake)

    result = await slack_add_reaction(_input(raw))

    assert fake.calls == [("reactions.add", {"channel": CHANNEL, "timestamp": TS, "name": sent})]
    assert result == f"Added reaction :{sent}: to message {TS} in {CHANNEL}."


async def test_add_reaction_already_reacted_is_a_success_like_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(routes={"reactions.add": SlackApiError("already_reacted", method="reactions.add")})
    _patch(monkeypatch, fake)

    result = await slack_add_reaction(_input())

    assert not result.startswith("Error")
    assert result == (
        f"Reaction :thumbsup: was already on message {TS} in {CHANNEL} (Slack: already_reacted) — nothing changed; "
        "the message already shows your reaction."
    )
    assert fake.calls == [("reactions.add", {"channel": CHANNEL, "timestamp": TS, "name": "thumbsup"})]


async def test_add_reaction_named_error_is_rendered_with_its_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(routes={"reactions.add": SlackApiError("invalid_name", method="reactions.add")})
    _patch(monkeypatch, fake)

    result = await slack_add_reaction(_input("not_an_emoji_here"))

    assert result == "Error (invalid_name): Slack refused reactions.add: invalid_name. Unknown emoji short name."


async def test_add_reaction_guard_refusal_names_the_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    refusal = WriteNotAllowedError(
        f"reactions.add to #general ({CHANNEL}) refused: the channel is not in SLACK_WRITE_CHANNELS. Add its id "
        f"({CHANNEL}) to that list to allow writes there."
    )
    fake = _FakeClient(routes={"reactions.add": refusal})
    _patch(monkeypatch, fake)

    result = await slack_add_reaction(_input())

    assert result.startswith("Error: reactions.add to #general")
    assert "SLACK_WRITE_CHANNELS" in result


async def test_add_reaction_surfaces_slack_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(routes={"reactions.add": {"ok": True, "warning": "superfluous_charset"}})
    _patch(monkeypatch, fake)

    result = await slack_add_reaction(_input())

    assert result.splitlines() == [
        f"Added reaction :thumbsup: to message {TS} in {CHANNEL}.",
        "Slack warning(s): `superfluous_charset`",
    ]


# -- slack_remove_reaction ------------------------------------------------------------


async def test_remove_reaction_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(routes={"reactions.remove": {"ok": True}})
    _patch(monkeypatch, fake)

    result = await slack_remove_reaction(_input(":eyes:"))

    assert result == f"Removed your reaction :eyes: from message {TS} in {CHANNEL}."
    assert fake.calls == [("reactions.remove", {"channel": CHANNEL, "timestamp": TS, "name": "eyes"})]


async def test_remove_reaction_no_reaction_is_a_success_like_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(routes={"reactions.remove": SlackApiError("no_reaction", method="reactions.remove")})
    _patch(monkeypatch, fake)

    result = await slack_remove_reaction(_input())

    assert not result.startswith("Error")
    assert "was not on message" in result
    assert "(Slack: no_reaction) — nothing to remove" in result


async def test_remove_reaction_does_not_swallow_already_reacted(monkeypatch: pytest.MonkeyPatch) -> None:
    # Only the method's own "end state reached" code is an outcome; any other code is an error.
    fake = _FakeClient(routes={"reactions.remove": SlackApiError("message_not_found", method="reactions.remove")})
    _patch(monkeypatch, fake)

    result = await slack_remove_reaction(_input())

    assert result.startswith("Error (message_not_found): Slack refused reactions.remove: message_not_found.")
    assert "check channel AND ts" in result


async def test_remove_reaction_writes_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(
        routes={"reactions.remove": WritesDisabledError("reactions.remove would change … SLACK_ALLOW_WRITES=1")}
    )
    _patch(monkeypatch, fake)

    result = await slack_remove_reaction(_input())

    assert result.startswith("Error: reactions.remove would change")
    assert "SLACK_ALLOW_WRITES=1" in result


# -- local validation ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"timestamp": "1712345678"},
        {"timestamp": "yesterday"},
        {"channel": "#general"},
        {"channel": "U0123ABCDEF"},
        {"name": "thumbs up"},
        {"name": "::"},
        {"ts": TS},
    ],
)
def test_reaction_input_rejects_bad_values_locally(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _input(**overrides)


# -- annotations + the real guard ---------------------------------------------------------


async def test_reaction_tools_are_registered_with_write_annotations() -> None:
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    add = tools["slack_add_reaction"].annotations
    remove = tools["slack_remove_reaction"].annotations
    assert add is not None
    assert remove is not None
    assert (add.readOnlyHint, add.destructiveHint, add.idempotentHint, add.openWorldHint) == (False, False, True, True)
    assert (remove.readOnlyHint, remove.destructiveHint, remove.idempotentHint, remove.openWorldHint) == (
        False,
        True,
        True,
        True,
    )
    for name in ("slack_add_reaction", "slack_remove_reaction"):
        description = tools[name].description or ""
        assert "AS YOU" in description
        assert "Refused unless `SLACK_ALLOW_WRITES=1` AND the target is allowed by" in description
        assert "never retried" in description


async def test_add_reaction_through_the_real_client_is_guarded_and_form_encoded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)

    closed = SlackClient(settings=Settings(slack_user_token=TOKEN), transport=transport)
    _patch(monkeypatch, closed)
    refused = await slack_add_reaction(_input(":eyes:"))
    assert refused.startswith("Error: reactions.add would change your Slack workspace AS YOU")
    assert requests == []

    allowed = SlackClient(
        settings=Settings(slack_user_token=TOKEN, slack_allow_writes=True, slack_write_channels=CHANNEL),
        transport=transport,
    )
    _patch(monkeypatch, allowed)
    result = await slack_add_reaction(_input(":eyes:"))

    assert result == f"Added reaction :eyes: to message {TS} in {CHANNEL}."
    assert len(requests) == 1
    assert requests[0].url.path.endswith("/reactions.add")
    assert requests[0].headers["content-type"].startswith("application/x-www-form-urlencoded")
    assert requests[0].content.decode() == f"channel={CHANNEL}&timestamp={TS}&name=eyes"
