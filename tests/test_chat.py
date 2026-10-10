"""Unit tests for the chat tools (post / reply / update / schedule) against a fake client.

No live tests here on purpose: these tools write AS THE USER, and the live write smoke
belongs to the conductor's t6 (self-DM only). Three end-to-end tests at the bottom run the
real `SlackClient` over `httpx.MockTransport` to prove the tools' writes go through the
client's guard and on the wire as JSON.
"""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import ValidationError

from slack_mcp.client import SlackClient
from slack_mcp.config import Settings, get_settings
from slack_mcp.errors import SlackApiError
from slack_mcp.formatters import MESSAGE_CONTINUATION, render_message, ts_to_human
from slack_mcp.guard import WriteNotAllowedError, WritesDisabledError
from slack_mcp.tools.chat import (
    CONTEXT_TEXT_MAX,
    MARKDOWN_BLOCK_MAX,
    MARKDOWN_TEXT_MAX,
    MESSAGE_BLOCKS_MAX,
    POST_TEXT_MAX,
    SCHEDULE_MAX_SECONDS,
    SECTION_TEXT_MAX,
    UPDATE_TEXT_MAX,
    BlockLimitError,
    PostMessageInput,
    ReplyInThreadInput,
    ScheduleMessageInput,
    UpdateMessageInput,
    _build_message,
    _section_chunks,
    _sign,
    _signature_for,
    slack_post_message,
    slack_reply_in_thread,
    slack_schedule_message,
    slack_update_message,
)

TOKEN = "xoxp-FAKE-0123456789abcdef"
CHANNEL = "C0123ABCDEF"
DM = "D0123ABCDEF"
TS = "1791000000.123456"
PARENT_TS = "1790999000.000100"
PERMALINK = "https://acme.slack.com/archives/C0123ABCDEF/p1791000000123456"


class _FakeClient:
    """Routes by method name; records every call as (method, params)."""

    def __init__(self, routes: dict[str, Any] | None = None, exc: Exception | None = None) -> None:
        self._routes = routes or {}
        self._exc = exc
        self.calls: list[tuple[str, Any]] = []

    async def call(self, method: str, params: Any = None, **kw: Any) -> dict[str, Any]:
        assert kw == {}, "the chat tools never pass a timeout or other kwargs"
        self.calls.append((method, params))
        if self._exc is not None:
            raise self._exc
        payload = self._routes.get(method)
        if isinstance(payload, Exception):
            raise payload
        return dict(payload) if payload is not None else {"ok": True}


def _patch(monkeypatch: pytest.MonkeyPatch, fake: Any) -> None:
    monkeypatch.setattr("slack_mcp.tools.chat.get_client", lambda: fake)


def _posted(channel: str = CHANNEL, ts: str = TS, **extra: Any) -> dict[str, Any]:
    return {"ok": True, "channel": channel, "ts": ts, "message": {"type": "message", "ts": ts}, **extra}


PERMALINK_OK = {"ok": True, "channel": CHANNEL, "permalink": PERMALINK}


def _no_none(params: dict[str, Any]) -> None:
    assert None not in params.values()


@pytest.mark.parametrize(
    "tool", [slack_post_message, slack_reply_in_thread, slack_update_message, slack_schedule_message]
)
def test_every_tool_states_identity_gate_and_no_retry(tool: Any) -> None:
    doc = " ".join((tool.__doc__ or "").split())  # collapse line wraps
    assert "as you" in doc.lower()
    # Measured 2026-10-10 (O-3): Slack adds no "via app" line to a user-token post.
    assert "Slack does not label these messages: they look exactly like ones you typed." in doc
    assert "To let teammates tell them apart, set `SLACK_SIGNATURE`" in doc
    assert "Sent using" not in doc
    assert "Refused unless `SLACK_ALLOW_WRITES=1` AND the target is allowed by `SLACK_WRITE_CHANNELS` /" in doc
    assert "(Slack Connect channels only by explicit ID)" in doc
    assert "never retried" in (tool.__doc__ or "")  # on one line, so a plain grep finds it
    assert "chat:write" in doc


# --- slack_post_message ---------------------------------------------------------------


async def test_post_mrkdwn_happy_path_sends_text_and_looks_up_the_permalink(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(routes={"chat.postMessage": _posted(), "chat.getPermalink": PERMALINK_OK})
    _patch(monkeypatch, fake)

    result = await slack_post_message(PostMessageInput(channel=CHANNEL, text="Deploy done *now* <@U0ANA000001>"))

    assert fake.calls == [
        (
            "chat.postMessage",
            {
                "channel": CHANNEL,
                "text": "Deploy done *now* <@U0ANA000001>",
                "unfurl_links": False,
                "unfurl_media": False,
            },
        ),
        ("chat.getPermalink", {"channel": CHANNEL, "message_ts": TS}),
    ]
    _no_none(fake.calls[0][1])
    assert result.startswith("# Message sent")
    assert "SENT — Slack accepted the message (ok), posted as you. Do not send it again." in result
    assert f"- **channel**: {CHANNEL}" in result
    assert f"- **ts**: {TS} ({ts_to_human(TS)})" in result
    assert f"- **permalink**: {PERMALINK}" in result
    assert "warning" not in result.lower()
    assert "thread_ts" not in result


async def test_post_markdown_sends_markdown_text_and_not_text(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(routes={"chat.postMessage": _posted(), "chat.getPermalink": PERMALINK_OK})
    _patch(monkeypatch, fake)
    text = "## Plan\n- **one**\n- [link](https://example.com)"

    result = await slack_post_message(PostMessageInput(channel=CHANNEL, text=text, format="markdown"))

    method, params = fake.calls[0]
    assert method == "chat.postMessage"
    assert params == {"channel": CHANNEL, "markdown_text": text, "unfurl_links": False, "unfurl_media": False}
    assert "text" not in params
    assert "SENT" in result


async def test_post_with_thread_ts_and_unfurls(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _posted(message={"type": "message", "ts": TS, "thread_ts": PARENT_TS})
    fake = _FakeClient(routes={"chat.postMessage": body, "chat.getPermalink": PERMALINK_OK})
    _patch(monkeypatch, fake)

    result = await slack_post_message(
        PostMessageInput(channel=DM, text="hi", thread_ts=PARENT_TS, unfurl_links=True, unfurl_media=True)
    )

    assert fake.calls[0] == (
        "chat.postMessage",
        {"channel": DM, "text": "hi", "thread_ts": PARENT_TS, "unfurl_links": True, "unfurl_media": True},
    )
    # The permalink is looked up on the channel Slack RETURNED, not only the requested one.
    assert fake.calls[1] == ("chat.getPermalink", {"channel": CHANNEL, "message_ts": TS})
    assert f"- **thread_ts**: {PARENT_TS} (the thread's parent" in result


async def test_post_permalink_failure_still_reports_the_message_as_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(
        routes={
            "chat.postMessage": _posted(),
            "chat.getPermalink": SlackApiError("message_not_found", method="chat.getPermalink"),
        }
    )
    _patch(monkeypatch, fake)

    result = await slack_post_message(PostMessageInput(channel=CHANNEL, text="hello"))

    assert not result.startswith("Error")
    assert result.startswith("# Message sent")
    assert "SENT — Slack accepted the message (ok)" in result
    assert f"- **channel**: {CHANNEL}" in result
    assert f"- **ts**: {TS}" in result
    assert (
        "- **permalink**: lookup failed (`message_not_found`) — No message with that ts exists in that conversation "
        "(check channel AND ts). The message itself WAS sent; do not post it again."
    ) in result
    assert "not sent" not in result.lower()
    # One post, one lookup — the post is never repeated.
    assert [method for method, _ in fake.calls] == ["chat.postMessage", "chat.getPermalink"]


_PERMALINK_REQUEST = httpx.Request("POST", "https://slack.com/api/chat.getPermalink")


@pytest.mark.parametrize(
    ("exc", "cause"),
    [
        (httpx.ReadTimeout("slow"), "(ReadTimeout)"),
        (
            httpx.HTTPStatusError(
                "503", request=_PERMALINK_REQUEST, response=httpx.Response(503, request=_PERMALINK_REQUEST)
            ),
            "(HTTPStatusError 503)",
        ),
        (httpx.ConnectError("down"), "(ConnectError)"),
        (SlackApiError("internal_error", method="chat.getPermalink"), "(`internal_error`)"),
        (SlackApiError("non_json_response", method="chat.getPermalink"), "(`non_json_response`)"),
    ],
)
async def test_post_permalink_timeout_or_5xx_says_sent_without_the_unknown_write_advice(
    monkeypatch: pytest.MonkeyPatch, exc: Exception, cause: str
) -> None:
    fake = _FakeClient(routes={"chat.postMessage": _posted(), "chat.getPermalink": exc})
    _patch(monkeypatch, fake)

    result = await slack_post_message(PostMessageInput(channel=CHANNEL, text="hello"))

    assert result.startswith("# Message sent")
    assert f"- **permalink**: lookup failed {cause}. The message itself WAS sent; do not post it again." in result
    # The write-outcome advice belongs to a failed POST, never under "SENT".
    assert "UNKNOWN" not in result
    assert "retrying" not in result
    assert "may or may not" not in result
    assert "Error:" not in result  # no handle_api_error text ("Error: …" / "Error (code): …")
    assert "Error (" not in result
    assert [method for method, _ in fake.calls] == ["chat.postMessage", "chat.getPermalink"]


async def test_post_without_ts_in_the_answer_skips_the_permalink(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(routes={"chat.postMessage": {"ok": True, "channel": CHANNEL}})
    _patch(monkeypatch, fake)

    result = await slack_post_message(PostMessageInput(channel=CHANNEL, text="hello"))

    assert "SENT" in result
    assert "- **ts**: N/A" in result
    assert "permalink**: not looked up" in result
    assert [method for method, _ in fake.calls] == ["chat.postMessage"]


@pytest.mark.parametrize(
    "extra",
    [{"response_metadata": {"warnings": ["message_truncated"]}}, {"warning": "message_truncated"}],
)
async def test_post_renders_the_message_truncated_warning(
    monkeypatch: pytest.MonkeyPatch, extra: dict[str, Any]
) -> None:
    fake = _FakeClient(routes={"chat.postMessage": _posted(**extra), "chat.getPermalink": PERMALINK_OK})
    _patch(monkeypatch, fake)

    result = await slack_post_message(PostMessageInput(channel=CHANNEL, text="x" * POST_TEXT_MAX))

    assert "SENT" in result
    assert "Slack warning(s): `message_truncated` — Slack truncated the text" in result
    assert "the message WAS posted" in result


async def test_post_not_in_channel_is_rendered_with_its_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(routes={"chat.postMessage": SlackApiError("not_in_channel", method="chat.postMessage")})
    _patch(monkeypatch, fake)

    result = await slack_post_message(PostMessageInput(channel=CHANNEL, text="hello"))

    assert result.startswith("Error (not_in_channel): Slack refused chat.postMessage: not_in_channel.")
    assert "Join it in Slack first — this server never auto-joins." in result
    assert [method for method, _ in fake.calls] == ["chat.postMessage"]  # no permalink lookup, no retry


async def test_post_refused_by_the_guard_surfaces_the_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    refusal = WriteNotAllowedError(
        f"chat.postMessage to #general ({CHANNEL}) refused: the channel is not in SLACK_WRITE_CHANNELS. Add its id "
        f"({CHANNEL}) to that list to allow writes there."
    )
    fake = _FakeClient(exc=refusal)
    _patch(monkeypatch, fake)

    result = await slack_post_message(PostMessageInput(channel=CHANNEL, text="hello"))

    assert result.startswith("Error: chat.postMessage to #general")
    assert "SLACK_WRITE_CHANNELS" in result
    assert "SENT" not in result
    assert [method for method, _ in fake.calls] == ["chat.postMessage"]


async def test_post_with_writes_disabled_surfaces_the_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(exc=WritesDisabledError("chat.postMessage would change your Slack workspace AS YOU, ..."))
    _patch(monkeypatch, fake)

    result = await slack_post_message(PostMessageInput(channel=CHANNEL, text="hello"))

    assert result.startswith("Error: chat.postMessage would change your Slack workspace AS YOU")


async def test_post_timeout_is_unknown_and_never_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(exc=httpx.ReadTimeout("slow"))
    _patch(monkeypatch, fake)

    result = await slack_post_message(PostMessageInput(channel=CHANNEL, text="hello"))

    assert result.startswith("Error: the Slack API request timed out.")
    assert "Execution status is UNKNOWN" in result
    assert len(fake.calls) == 1


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"channel": "#general", "text": "x"}, "channel must be a Slack conversation ID"),
        ({"channel": "general", "text": "x"}, "channel must be a Slack conversation ID"),
        ({"channel": "U0ANA000001", "text": "x"}, "channel must be a Slack conversation ID"),
        ({"channel": CHANNEL, "text": ""}, "at least 1 character"),
        ({"channel": CHANNEL, "text": "   \n "}, "at least 1 character"),
        ({"channel": CHANNEL, "text": "x" * (POST_TEXT_MAX + 1)}, "at most 40000 characters"),
        ({"channel": CHANNEL, "text": "x", "thread_ts": "12345"}, "thread_ts must be a Slack message timestamp"),
        ({"channel": CHANNEL, "text": "x", "format": "html"}, "'mrkdwn' or 'markdown'"),
        ({"channel": CHANNEL, "text": "x", "as_user": True}, "Extra inputs are not permitted"),
        ({"channel": CHANNEL, "text": "x", "blocks": []}, "Extra inputs are not permitted"),
    ],
)
def test_post_input_rejections(kwargs: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        PostMessageInput(**kwargs)


def test_post_markdown_is_capped_at_12000_but_mrkdwn_is_not() -> None:
    long_text = "x" * (MARKDOWN_TEXT_MAX + 1)
    with pytest.raises(ValidationError, match="format='markdown' sends it as markdown_text"):
        PostMessageInput(channel=CHANNEL, text=long_text, format="markdown")
    assert PostMessageInput(channel=CHANNEL, text=long_text).text == long_text
    assert PostMessageInput(channel=CHANNEL, text="x" * MARKDOWN_TEXT_MAX, format="markdown").format == "markdown"


def test_post_input_normalises_ids_and_strips_edges() -> None:
    model = PostMessageInput(channel=f"  {CHANNEL} ", text="  hi  ", thread_ts=f" {PARENT_TS}")
    assert model.channel == CHANNEL
    assert model.text == "hi"
    assert model.thread_ts == PARENT_TS


# --- slack_reply_in_thread ------------------------------------------------------------


async def test_reply_happy_path_exact_params(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _posted(message={"type": "message", "ts": TS, "thread_ts": PARENT_TS})
    thread_link = f"{PERMALINK}?thread_ts={PARENT_TS}&cid={CHANNEL}"
    fake = _FakeClient(
        routes={
            "chat.postMessage": body,
            "chat.getPermalink": {"ok": True, "channel": CHANNEL, "permalink": thread_link},
        }
    )
    _patch(monkeypatch, fake)

    result = await slack_reply_in_thread(ReplyInThreadInput(channel=CHANNEL, thread_ts=PARENT_TS, text="On it"))

    assert fake.calls == [
        (
            "chat.postMessage",
            {
                "channel": CHANNEL,
                "thread_ts": PARENT_TS,
                "text": "On it",
                "reply_broadcast": False,
                "unfurl_links": False,
                "unfurl_media": False,
            },
        ),
        ("chat.getPermalink", {"channel": CHANNEL, "message_ts": TS}),
    ]
    _no_none(fake.calls[0][1])
    assert result.startswith("# Reply sent")
    assert "SENT" in result
    assert f"- **ts**: {TS}" in result
    assert f"- **thread_ts**: {PARENT_TS}" in result
    assert f"- **permalink**: {thread_link}" in result


async def test_reply_markdown_with_broadcast(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(routes={"chat.postMessage": _posted(), "chat.getPermalink": PERMALINK_OK})
    _patch(monkeypatch, fake)

    await slack_reply_in_thread(
        ReplyInThreadInput(
            channel=CHANNEL, thread_ts=PARENT_TS, text="**Shipped**", format="markdown", reply_broadcast=True
        )
    )

    assert fake.calls[0][1] == {
        "channel": CHANNEL,
        "thread_ts": PARENT_TS,
        "markdown_text": "**Shipped**",
        "reply_broadcast": True,
        "unfurl_links": False,
        "unfurl_media": False,
    }


async def test_reply_permalink_failure_still_reports_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(
        routes={
            "chat.postMessage": _posted(),
            "chat.getPermalink": SlackApiError("channel_not_found", method="chat.getPermalink"),
        }
    )
    _patch(monkeypatch, fake)

    result = await slack_reply_in_thread(ReplyInThreadInput(channel=CHANNEL, thread_ts=PARENT_TS, text="ok"))

    assert result.startswith("# Reply sent")
    assert "lookup failed (`channel_not_found`) — Wrong conversation id" in result
    assert "WAS sent" in result


async def test_reply_thread_not_found_is_rendered_with_its_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(routes={"chat.postMessage": SlackApiError("thread_not_found", method="chat.postMessage")})
    _patch(monkeypatch, fake)

    result = await slack_reply_in_thread(ReplyInThreadInput(channel=CHANNEL, thread_ts=PARENT_TS, text="ok"))

    assert result.startswith("Error (thread_not_found)")
    assert "No parent message with that thread_ts" in result


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"channel": CHANNEL, "text": "x"}, "thread_ts\n  Field required"),
        ({"channel": CHANNEL, "thread_ts": None, "text": "x"}, "thread_ts must be a Slack message timestamp"),
        ({"channel": CHANNEL, "thread_ts": "1712345678", "text": "x"}, "thread_ts must be a Slack message timestamp"),
        ({"channel": "#dev", "thread_ts": PARENT_TS, "text": "x"}, "channel must be a Slack conversation ID"),
        (
            {"channel": CHANNEL, "thread_ts": PARENT_TS, "text": "x" * (MARKDOWN_TEXT_MAX + 1), "format": "markdown"},
            "Slack limits to 12,000 characters",
        ),
        (
            {"channel": CHANNEL, "thread_ts": PARENT_TS, "text": "x", "username": "bot"},
            "Extra inputs are not permitted",
        ),
        ({"channel": CHANNEL, "thread_ts": PARENT_TS, "text": "x", "blocks": []}, "Extra inputs are not permitted"),
    ],
)
def test_reply_input_rejections(kwargs: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        ReplyInThreadInput(**kwargs)


# --- slack_update_message -------------------------------------------------------------


async def test_update_happy_path_exact_params_and_render(monkeypatch: pytest.MonkeyPatch) -> None:
    body = {
        "ok": True,
        "channel": CHANNEL,
        "ts": TS,
        "text": "Deploy done (v2)",
        "message": {"text": "Deploy done (v2)"},
    }
    fake = _FakeClient(routes={"chat.update": body})
    _patch(monkeypatch, fake)

    result = await slack_update_message(UpdateMessageInput(channel=CHANNEL, ts=TS, text="Deploy done (v2)"))

    assert fake.calls == [("chat.update", {"channel": CHANNEL, "ts": TS, "text": "Deploy done (v2)"})]
    assert result.startswith("# Message updated")
    assert "Slack accepted the edit (ok)." in result
    assert f"- **channel**: {CHANNEL}" in result
    assert f"- **ts**: {TS} ({ts_to_human(TS)})" in result
    assert "- **text** (as Slack stored it):\nDeploy done (v2)" in result
    assert "warning" not in result.lower()


async def test_update_markdown_sends_markdown_text_only(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(routes={"chat.update": {"ok": True, "channel": CHANNEL, "ts": TS}})
    _patch(monkeypatch, fake)

    result = await slack_update_message(UpdateMessageInput(channel=CHANNEL, ts=TS, text="**v2**", format="markdown"))

    assert fake.calls == [("chat.update", {"channel": CHANNEL, "ts": TS, "markdown_text": "**v2**"})]
    assert "- **text**: Slack returned none" in result


async def test_update_renders_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
    body = {"ok": True, "channel": CHANNEL, "ts": TS, "text": "v2", "warning": "superfluous_charset"}
    fake = _FakeClient(routes={"chat.update": body})
    _patch(monkeypatch, fake)

    result = await slack_update_message(UpdateMessageInput(channel=CHANNEL, ts=TS, text="v2"))

    assert "Slack warning(s): `superfluous_charset`" in result


async def test_update_msg_too_long_uses_the_chat_update_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(routes={"chat.update": SlackApiError("msg_too_long", method="chat.update")})
    _patch(monkeypatch, fake)

    result = await slack_update_message(UpdateMessageInput(channel=CHANNEL, ts=TS, text="v2"))

    assert result.startswith("Error (msg_too_long): Slack refused chat.update: msg_too_long.")
    assert "chat.update refuses more than 4,000 characters" in result
    assert len(fake.calls) == 1


async def test_update_someone_elses_message_is_rendered_with_its_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(routes={"chat.update": SlackApiError("cant_update_message", method="chat.update")})
    _patch(monkeypatch, fake)

    result = await slack_update_message(UpdateMessageInput(channel=CHANNEL, ts=TS, text="v2"))

    assert result.startswith("Error (cant_update_message)")
    assert "Only your own messages can be edited" in result


async def test_update_refused_by_the_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(
        exc=WriteNotAllowedError(f"chat.update to #general ({CHANNEL}) refused: ... SLACK_WRITE_CHANNELS")
    )
    _patch(monkeypatch, fake)

    result = await slack_update_message(UpdateMessageInput(channel=CHANNEL, ts=TS, text="v2"))

    assert result.startswith("Error: chat.update to #general")
    assert "SLACK_WRITE_CHANNELS" in result


def test_update_text_over_4000_is_rejected_locally() -> None:
    with pytest.raises(ValidationError, match="at most 4000 characters"):
        UpdateMessageInput(channel=CHANNEL, ts=TS, text="x" * (UPDATE_TEXT_MAX + 1))
    with pytest.raises(ValidationError, match="at most 4000 characters"):
        UpdateMessageInput(channel=CHANNEL, ts=TS, text="x" * (UPDATE_TEXT_MAX + 1), format="markdown")
    assert len(UpdateMessageInput(channel=CHANNEL, ts=TS, text="x" * UPDATE_TEXT_MAX).text) == UPDATE_TEXT_MAX


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"channel": "U0ANA000001", "ts": TS, "text": "x"}, "channel must be a Slack conversation ID"),
        ({"channel": CHANNEL, "ts": "abc", "text": "x"}, "ts must be a Slack message timestamp"),
        ({"channel": CHANNEL, "ts": TS, "text": ""}, "at least 1 character"),
        ({"channel": CHANNEL, "ts": TS, "text": "x", "thread_ts": PARENT_TS}, "Extra inputs are not permitted"),
    ],
)
def test_update_input_rejections(kwargs: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        UpdateMessageInput(**kwargs)


# --- slack_schedule_message -----------------------------------------------------------


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat()


async def test_schedule_happy_path_converts_post_at_and_renders_the_id(monkeypatch: pytest.MonkeyPatch) -> None:
    when = int(time.time()) + 86_400
    body = {"ok": True, "channel": CHANNEL, "scheduled_message_id": "Q1298393284", "post_at": when}
    fake = _FakeClient(routes={"chat.scheduleMessage": body})
    _patch(monkeypatch, fake)

    result = await slack_schedule_message(ScheduleMessageInput(channel=CHANNEL, post_at=_iso(when), text="Standup"))

    assert fake.calls == [
        (
            "chat.scheduleMessage",
            {"channel": CHANNEL, "post_at": when, "text": "Standup", "unfurl_links": False, "unfurl_media": False},
        )
    ]
    sent = fake.calls[0][1]
    assert isinstance(sent["post_at"], int)
    _no_none(sent)
    assert "thread_ts" not in sent
    assert "metadata" not in sent
    assert result.startswith("# Message scheduled")
    assert "- **scheduled_message_id**: Q1298393284" in result
    assert f"- **channel**: {CHANNEL}" in result
    assert f"- **post_at**: {ts_to_human(when)} (epoch {when})" in result
    assert "there is no un-schedule tool in v0.1" in result


async def test_schedule_thread_reply_in_markdown_with_an_offset(monkeypatch: pytest.MonkeyPatch) -> None:
    when = int(time.time()) + 3_600
    offset_iso = datetime.fromtimestamp(when, tz=UTC).astimezone().isoformat()  # local offset, same instant
    fake = _FakeClient(routes={"chat.scheduleMessage": {"ok": True, "channel": CHANNEL, "scheduled_message_id": "Q1"}})
    _patch(monkeypatch, fake)

    result = await slack_schedule_message(
        ScheduleMessageInput(channel=CHANNEL, post_at=offset_iso, text="# Hi", format="markdown", thread_ts=PARENT_TS)
    )

    assert fake.calls[0][1] == {
        "channel": CHANNEL,
        "post_at": when,
        "markdown_text": "# Hi",
        "thread_ts": PARENT_TS,
        "unfurl_links": False,
        "unfurl_media": False,
    }
    assert "- **post_at**: N/A" in result  # Slack did not echo post_at: the tool does not invent it


async def test_schedule_accepts_epoch_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    when = int(time.time()) + 600
    fake = _FakeClient(routes={"chat.scheduleMessage": {"ok": True, "scheduled_message_id": "Q2", "post_at": when}})
    _patch(monkeypatch, fake)

    await slack_schedule_message(ScheduleMessageInput(channel=CHANNEL, post_at=when, text="x"))

    assert fake.calls[0][1]["post_at"] == when


def test_schedule_post_at_in_the_past_is_rejected_locally() -> None:
    with pytest.raises(ValidationError, match="post_at must be in the future"):
        ScheduleMessageInput(channel=CHANNEL, post_at="2020-01-01T00:00:00Z", text="x")


def test_schedule_post_at_more_than_120_days_ahead_is_rejected_locally() -> None:
    too_far = int(time.time()) + 121 * 86_400
    with pytest.raises(ValidationError, match="at most 120 days ahead"):
        ScheduleMessageInput(channel=CHANNEL, post_at=_iso(too_far), text="x")


def test_schedule_window_edges_with_a_pinned_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 1_800_000_000
    monkeypatch.setattr("slack_mcp.tools.chat._now", lambda: float(now))

    with pytest.raises(ValidationError, match="post_at must be in the future"):
        ScheduleMessageInput(channel=CHANNEL, post_at=now, text="x")
    assert ScheduleMessageInput(channel=CHANNEL, post_at=now + 1, text="x").post_at == now + 1
    assert ScheduleMessageInput(channel=CHANNEL, post_at=now + SCHEDULE_MAX_SECONDS, text="x").post_at == (
        now + SCHEDULE_MAX_SECONDS
    )
    with pytest.raises(ValidationError, match="at most 120 days ahead"):
        ScheduleMessageInput(channel=CHANNEL, post_at=now + SCHEDULE_MAX_SECONDS + 1, text="x")


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"channel": CHANNEL, "post_at": "next tuesday", "text": "x"}, "post_at must be ISO-8601"),
        ({"channel": CHANNEL, "post_at": True, "text": "x"}, "not a bool"),
        ({"channel": "#dev", "post_at": "2099-01-01", "text": "x"}, "channel must be a Slack conversation ID"),
        ({"channel": CHANNEL, "post_at": "2099-01-01", "text": "x", "metadata": {}}, "Extra inputs are not permitted"),
        (
            # A valid post_at, so the model-level 12,000 cap is what fails.
            {
                "channel": CHANNEL,
                "post_at": int(time.time()) + 86_400,
                "text": "x" * (MARKDOWN_TEXT_MAX + 1),
                "format": "markdown",
            },
            "Slack limits to 12,000 characters",
        ),
    ],
)
def test_schedule_input_rejections(kwargs: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        ScheduleMessageInput(**kwargs)


async def test_schedule_time_too_far_from_slack_is_rendered_with_its_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(routes={"chat.scheduleMessage": SlackApiError("time_too_far", method="chat.scheduleMessage")})
    _patch(monkeypatch, fake)

    result = await slack_schedule_message(
        ScheduleMessageInput(channel=CHANNEL, post_at=int(time.time()) + 600, text="x")
    )

    assert result.startswith("Error (time_too_far)")
    assert "at most 120 days out" in result


async def test_schedule_refused_by_the_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(
        exc=WriteNotAllowedError(f"chat.scheduleMessage to DM {DM} refused: U0BOB000001 is not in SLACK_WRITE_USERS.")
    )
    _patch(monkeypatch, fake)

    result = await slack_schedule_message(ScheduleMessageInput(channel=DM, post_at=int(time.time()) + 600, text="x"))

    assert result.startswith("Error: chat.scheduleMessage to DM")
    assert "SLACK_WRITE_USERS" in result
    assert len(fake.calls) == 1


# --- end to end through the real client and its guard (MockTransport, no network) -----


class _Recorder:
    """A MockTransport handler: routes by method name, records every request."""

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []

    @property
    def methods(self) -> list[str]:
        return [request.url.path.rsplit("/", 1)[-1] for request in self.requests]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        method = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json=self.routes.get(method, {"ok": True}))


def _real_client(monkeypatch: pytest.MonkeyPatch, recorder: _Recorder, **settings: Any) -> None:
    client = SlackClient(settings=Settings(slack_user_token=TOKEN, **settings), transport=httpx.MockTransport(recorder))
    monkeypatch.setattr("slack_mcp.tools.chat.get_client", lambda: client)


async def test_e2e_writes_disabled_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder({"chat.postMessage": _posted()})
    _real_client(monkeypatch, recorder)

    result = await slack_post_message(PostMessageInput(channel=CHANNEL, text="hello"))

    assert result.startswith("Error: chat.postMessage would change your Slack workspace AS YOU")
    assert "SLACK_ALLOW_WRITES=1" in result
    assert recorder.requests == []


async def test_e2e_channel_outside_the_allowlist_is_never_posted_to(monkeypatch: pytest.MonkeyPatch) -> None:
    info = {"ok": True, "channel": {"id": CHANNEL, "name": "general", "is_channel": True}}
    recorder = _Recorder({"conversations.info": info, "chat.postMessage": _posted()})
    _real_client(monkeypatch, recorder, slack_allow_writes=True, slack_write_channels="C0OTHER0001")

    result = await slack_update_message(UpdateMessageInput(channel=CHANNEL, ts=TS, text="v2"))

    assert result.startswith(f"Error: chat.update to #general ({CHANNEL}) refused")
    assert "SLACK_WRITE_CHANNELS" in result
    assert recorder.methods == ["conversations.info"]  # the guard looked, nothing was written


async def test_e2e_allowlisted_post_goes_out_as_json_then_the_permalink_as_a_form(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _Recorder({"chat.postMessage": _posted(), "chat.getPermalink": PERMALINK_OK})
    _real_client(monkeypatch, recorder, slack_allow_writes=True, slack_write_channels=CHANNEL)

    result = await slack_post_message(PostMessageInput(channel=CHANNEL, text="**hi**", format="markdown"))

    assert recorder.methods == ["chat.postMessage", "chat.getPermalink"]
    post, permalink = recorder.requests
    assert post.headers["content-type"].startswith("application/json")
    assert post.headers["authorization"] == f"Bearer {TOKEN}"
    assert json.loads(post.content) == {
        "channel": CHANNEL,
        "markdown_text": "**hi**",
        "unfurl_links": False,
        "unfurl_media": False,
    }
    assert parse_qs(permalink.content.decode()) == {"channel": [CHANNEL], "message_ts": [TS]}
    assert f"- **permalink**: {PERMALINK}" in result
    assert TOKEN not in result


# --- SLACK_SIGNATURE (measured 2026-10-10, O-3: Slack does not label user-token posts) ---

APP_URL = "https://acme.slack.com/marketplace/A0123456789"
SIG = f"Sent using <{APP_URL}|@Alejandro AI>"  # what SLACK_SIGNATURE holds (mrkdwn)
SIG_MD = f"Sent using [@Alejandro AI]({APP_URL})"  # the same line for markdown_text
SIGNATURES = {"mrkdwn": SIG, "markdown": SIG_MD}
# Both formats: a blank line, so the signature stands apart (and, in markdown, is its own paragraph).
SEP = {"mrkdwn": "\n\n", "markdown": "\n\n"}


def _signed(text: str, fmt: str) -> str:
    return f"{text}{SEP[fmt]}{SIGNATURES[fmt]}"


def _sign_with(monkeypatch: pytest.MonkeyPatch, value: str = SIG) -> None:
    monkeypatch.setattr("slack_mcp.tools.chat.get_settings", lambda: Settings(slack_signature=value))


def test_sign_appends_the_signature_after_a_blank_line() -> None:
    assert _sign("Deploy done", Settings(slack_signature=SIG)) == (f"Deploy done\n\n{SIG}", True)
    assert _sign("Deploy done", Settings(slack_signature=SIG), "markdown") == (f"Deploy done\n\n{SIG_MD}", True)


def test_markdown_signature_cannot_be_pulled_into_a_list_quote_or_table() -> None:
    settings = Settings(slack_signature=SIG)
    for block in ("## Plan\n- **one**\n- two", "> a quote", "| a | b |\n|---|---|\n| 1 | 2 |"):
        signed, appended = _sign(block, settings, "markdown")
        assert appended is True
        assert signed == f"{block}\n\n{SIG_MD}"  # a blank line ends the list / quote / table
    # mrkdwn has no such blocks, but the blank line is there too: it keeps the signature visibly apart.
    assert _sign("- one\n- two", settings) == (f"- one\n- two\n\n{SIG}", True)


def test_sign_without_a_signature_returns_the_text_unchanged() -> None:
    assert _sign("Deploy done", Settings()) == ("Deploy done", False)
    assert _sign("Deploy done", Settings(), "markdown") == ("Deploy done", False)


@pytest.mark.parametrize("fmt", ["mrkdwn", "markdown"])
def test_sign_is_idempotent_on_an_already_signed_text(fmt: Any) -> None:
    settings = Settings(slack_signature=SIG)
    once, appended = _sign("Deploy done", settings, fmt)
    assert appended is True
    assert once == _signed("Deploy done", fmt)
    assert _sign(once, settings, fmt) == (once, False)  # never twice
    assert _sign(f"Deploy done\n   {SIGNATURES[fmt]}  ", settings, fmt)[1] is False  # last line, trimmed
    # Tolerant of the blank line: with or without it (or several), a signed last line is not re-signed.
    for gap in ("\n", "\n\n", "\n\n\n", "\n  \n"):
        assert _sign(f"Deploy done{gap}{SIGNATURES[fmt]}", settings, fmt)[1] is False, repr(gap)
    assert _sign(SIGNATURES[fmt], settings, fmt) == (SIGNATURES[fmt], False)  # the text IS the signature


def test_sign_re_signs_when_the_signature_is_not_at_the_end() -> None:
    settings = Settings(slack_signature=SIG)
    middle = f"Quoting it:\n{SIG}\nand more after"
    assert _sign(middle, settings) == (f"{middle}\n\n{SIG}", True)
    assert _sign(f"Deploy done {SIG} and more", settings) == (f"Deploy done {SIG} and more\n\n{SIG}", True)
    assert _sign(f"x\n\n{SIG_MD}\n\nmore", settings, "markdown") == (f"x\n\n{SIG_MD}\n\nmore\n\n{SIG_MD}", True)
    # A partial signature at the end (its last words only) is not the signature.
    assert _sign("Deploy done <@U0BOT000001>", Settings(slack_signature="*Sent using* <@U0BOT000001>"))[1] is True


# t17 — measured live 2026-10-10 (a real workspace, self-DM, raw conversations.replies): Slack
# stores the `text` fallback of a message posted WITH blocks with every newline turned into a space.
LIVE_SIG = "*Sent using* <@U0BOTUSER01>"
LIVE_BODY = (
    "slack-mcp t13 live write smoke slackmcpt135ba94e36e3 [EDITED by slack_update_message]: *mrkdwn bold*, a "
    "mention of <@U0ME0000001> and a link <https://example.com|example.com link>. Second line: slack_get_thread "
    "must render this as a continuation."
)
LIVE_DOUBLED = f"{LIVE_BODY}  {LIVE_SIG}  {LIVE_SIG}"  # the edit from read-back before t17, read back again
LIVE_REPLY = f"[thread reply by slack_reply_in_thread] slackmcpt135ba94e36e3  {LIVE_SIG}"


@pytest.mark.parametrize(
    ("text", "body"),
    [
        (LIVE_DOUBLED, LIVE_BODY),  # the doubled live text: both copies go
        (f"{LIVE_BODY}  {LIVE_SIG}", LIVE_BODY),  # one signature behind two spaces (a flattened blank line)
        (f"{LIVE_BODY}\n\n{LIVE_SIG}", LIVE_BODY),
        (f"{LIVE_BODY}\n{LIVE_SIG}", LIVE_BODY),
        (f"{LIVE_BODY}\t{LIVE_SIG}  ", LIVE_BODY),
        # No separator at all still counts as trailing: the text ENDS with the signature.
        (f"{LIVE_BODY}{LIVE_SIG}", LIVE_BODY),
        (f"{LIVE_BODY}{LIVE_SIG}{LIVE_SIG} \n {LIVE_SIG}", LIVE_BODY),
        (LIVE_REPLY, "[thread reply by slack_reply_in_thread] slackmcpt135ba94e36e3"),
    ],
)
def test_a_trailing_signature_counts_whatever_separates_it(text: str, body: str) -> None:
    settings = Settings(slack_signature=LIVE_SIG)
    final, appended = _sign(text, settings)
    assert (final, appended) == (f"{body}\n\n{LIVE_SIG}", False)
    fields = _build_message(final, "mrkdwn", settings)
    assert fields["text"].count(LIVE_SIG) == 1
    assert [block["type"] for block in fields["blocks"]] == ["section", "context"]
    assert fields["blocks"][0]["text"]["text"] == body  # no signature inside the section
    assert LIVE_SIG not in json.dumps(fields["blocks"][0])
    assert fields["blocks"][1] == {"type": "context", "elements": [{"type": "mrkdwn", "text": LIVE_SIG}]}


@pytest.mark.parametrize("fmt", ["mrkdwn", "markdown"])
def test_a_flattened_or_glued_signature_is_recognised_in_every_form(fmt: Any) -> None:
    """Both link forms, the mention form and the entity form, behind spaces or nothing."""
    cases = [(SIG, SIG), (SIG, SIG_MD), (MENTION_SIG, MENTION_SIG), (AMP_SIG, AMP_SIG_READ_BACK)]
    for signature, written in cases:
        settings = Settings(slack_signature=signature)
        for gap in ("  ", " ", ""):
            final, appended = _sign(f"v2 of the report{gap}{written}", settings, fmt)
            assert (final, appended) == (f"v2 of the report\n\n{_signature_for(fmt, signature)}", False), (
                signature,
                written,
                gap,
            )
    # Entities are compared decoded, but a cut never lands inside one: the `&amp;` before
    # the signature stays whole in the body.
    settings = Settings(slack_signature=MENTION_SIG)
    assert _sign(f"R&amp;D{MENTION_SIG}", settings, fmt) == (f"R&amp;D\n\n{MENTION_SIG}", False)
    assert _sign(f"tail &lt;{MENTION_SIG}", settings, fmt) == (f"tail &lt;\n\n{MENTION_SIG}", False)


async def test_update_from_the_flattened_live_read_back_keeps_one_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    """The edit flow that doubled the signature live: the read-back text, edited, sent back."""
    _sign_with(monkeypatch, LIVE_SIG)
    fake = _FakeClient(routes={"chat.update": {"ok": True, "channel": CHANNEL, "ts": TS}})
    _patch(monkeypatch, fake)

    result = await slack_update_message(UpdateMessageInput(channel=CHANNEL, ts=TS, text=LIVE_DOUBLED))

    assert fake.calls[0][1] == {"channel": CHANNEL, "ts": TS, **_message(LIVE_BODY, "mrkdwn", LIVE_SIG)}
    assert "- **signature**: already at the end of the text — not added again (context block)" in result


@pytest.mark.parametrize("fmt", ["mrkdwn", "markdown"])
def test_either_signature_form_counts_whatever_the_format(fmt: Any) -> None:
    """Switching format on an edit never leaves two lines: the old form is rewritten in place."""
    settings = Settings(slack_signature=SIG)
    for old_form in (SIG, SIG_MD):
        for gap in ("\n", "\n\n"):
            signed, appended = _sign(f"Deploy done{gap}{old_form}", settings, fmt)
            assert (signed, appended) == (_signed("Deploy done", fmt), False), (old_form, gap)
    assert _sign(SIG_MD, settings, "mrkdwn") == (SIG, False)  # the text IS the (other-form) signature
    assert _sign(SIG, settings, "markdown") == (SIG_MD, False)


AMP_SIG = "Sent by Ana & AI <https://x.example/app?b=1&c=2|@Alejandro AI>"
AMP_SIG_READ_BACK = "Sent by Ana &amp; AI <https://x.example/app?b=1&amp;c=2|@Alejandro AI>"  # as Slack stores it


@pytest.mark.parametrize("fmt", ["mrkdwn", "markdown"])
def test_a_signature_with_an_ampersand_matches_its_read_back_form(fmt: Any) -> None:
    settings = Settings(slack_signature=AMP_SIG)
    expected = _signature_for(fmt, AMP_SIG)
    signed, appended = _sign(f"v2 of the report\n{AMP_SIG_READ_BACK}", settings, fmt)
    assert appended is False  # recognised: not signed a second time
    assert signed == f"v2 of the report{SEP[fmt]}{expected}"
    assert signed.count("@Alejandro AI") == 1
    # The entity-decoded comparison does not make a mid-text quote count.
    assert _sign(f"{AMP_SIG_READ_BACK}\nmore", settings, fmt)[1] is True


@pytest.mark.parametrize("fmt", ["mrkdwn", "markdown"])
def test_trailing_whitespace_after_the_signature_does_not_re_sign(fmt: Any) -> None:
    settings = Settings(slack_signature=SIG)
    for tail in ("\n", "\n\n", "  \n \t", "\r\n"):
        signed, appended = _sign(f"x\n\n{SIGNATURES[fmt]}{tail}", settings, fmt)
        assert (signed, appended) == (_signed("x", fmt), False), repr(tail)
    assert _sign("x \n\n", settings, fmt) == (_signed("x", fmt), True)


@pytest.mark.parametrize("block", ["## Plan\n- **one**\n- two", "> a quoted line", "| a | b |\n|---|---|\n| 1 | 2 |"])
def test_an_existing_signature_right_under_the_text_is_moved_behind_a_blank_line(block: str) -> None:
    """A signature right under the text (single newline) gets its blank line, in both formats.

    In markdown it would otherwise render inside the list / quote / table; in mrkdwn it would
    read as glued to the last line (the separator before t15 — such texts are still around).
    """
    settings = Settings(slack_signature=SIG)
    assert _sign(f"{block}\n{SIG_MD}", settings, "markdown") == (f"{block}\n\n{SIG_MD}", False)
    assert _sign(f"{block}\n{SIG}", settings) == (f"{block}\n\n{SIG}", False)


def test_signature_links_are_converted_only_for_markdown() -> None:
    assert _signature_for("mrkdwn", SIG) == SIG
    assert _signature_for("markdown", SIG) == SIG_MD
    assert _signature_for("markdown", "via <https://a.example/x?y=1&z=2|my [bot]> and <mailto:a@b.example|mail>") == (
        "via [my \\[bot\\]](https://a.example/x?y=1&z=2) and [mail](mailto:a@b.example)"
    )
    # A bare <url> is already a Markdown autolink; mentions, channel links and specials are not URLs.
    untouched = "— <https://a.example> <@U0ANA000001> <#C0123ABCDEF|general> <!here>"
    assert _signature_for("markdown", untouched) == untouched


# The recommended value (t15): a mention of the app's bot user, which Slack renders as the
# `@Alejandro AI` tag and opens the app's profile inside Slack (a URL opens the web).
BOT_USER_ID = "U0BOT000001"
MENTION_SIG = f"Sent using <@{BOT_USER_ID}>"


@pytest.mark.parametrize("fmt", ["mrkdwn", "markdown"])
def test_a_bot_user_mention_signature_is_the_same_line_in_both_formats(fmt: Any) -> None:
    settings = Settings(slack_signature=MENTION_SIG)
    assert _signature_for(fmt, MENTION_SIG) == MENTION_SIG  # `<@U…>` is a mention in markdown_text too
    signed, appended = _sign("Deploy done", settings, fmt)
    assert (signed, appended) == (f"Deploy done\n\n{MENTION_SIG}", True)
    assert _sign(signed, settings, fmt) == (signed, False)  # never twice
    for gap in ("\n", "\n\n\n"):  # rebuilt behind exactly one blank line
        assert _sign(f"Deploy done{gap}{MENTION_SIG}", settings, fmt) == (signed, False), repr(gap)


def test_a_signed_message_reads_back_as_posted_then_the_signature_as_its_last_line() -> None:
    """The shape test_live_write's step 9 expects from slack_get_thread, pinned offline.

    What Slack stores for a signed post (t17, measured): the blocks verbatim and the `text`
    fallback with its newlines flattened to spaces. render_message rebuilds the text from
    the blocks — the section, then the context block's signature as the last line — and
    prefixes every line after the first with `  │ `, the `(ts …)` suffix on the last.
    """
    settings = Settings(slack_signature=MENTION_SIG)
    signed, _ = _sign("Deploy done\nsecond line", settings)
    stored = _build_message(signed, "mrkdwn", settings)
    stored["text"] = " ".join(stored["text"].split("\n"))  # as Slack stores the fallback
    rendered = render_message({"user": "U0ME0000001", "ts": TS, **stored})
    assert rendered.endswith(
        f": Deploy done\n{MESSAGE_CONTINUATION}second line\n{MESSAGE_CONTINUATION}{MENTION_SIG} (ts {TS})"
    ), rendered
    assert rendered.count(MENTION_SIG) == 1
    # Without a signature nothing is rebuilt: plain `text`, stored as sent.
    plain = _build_message("Deploy done\nsecond line", "mrkdwn", Settings())
    assert render_message({"user": "U0ME0000001", "ts": TS, **plain}).endswith(
        f": Deploy done\n{MESSAGE_CONTINUATION}second line (ts {TS})"
    )


# --- t16: a signed message goes out as Block Kit blocks + a `text` fallback --------------
#
# Measured 2026-10-10 (a real workspace, self-DM): a `context` block renders the signature in the small
# grey font of Slack's own labels. With SLACK_SIGNATURE set every write sends `blocks` (the
# text minus its signature line, then a context block with the signature as mrkdwn) plus
# `text` = the whole signed text; without one, nothing changes.

BOLD_SIG = f"*Sent using* <@{BOT_USER_ID}>"  # the recommended value (t16): bold, like the native label


def _context(signature: str = SIG) -> dict[str, Any]:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": signature}]}


def _section(text: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _body_block(body: str, fmt: str) -> dict[str, Any]:
    return {"type": "markdown", "text": body} if fmt == "markdown" else _section(body)


def _message(body: str, fmt: str, signature: str = SIG) -> dict[str, Any]:
    """The message fields of a signed short text: `text` fallback + one body block + the context block."""
    return {
        "text": f"{body}\n\n{_signature_for(fmt, signature)}",
        "blocks": [_body_block(body, fmt), _context(signature)],
    }


def _body_texts(params: dict[str, Any]) -> list[str]:
    """The text of every block except the context block (sections and markdown blocks)."""
    texts = []
    for block in params["blocks"]:
        if block["type"] == "section":
            texts.append(block["text"]["text"])
        elif block["type"] == "markdown":
            texts.append(block["text"])
    return texts


def _when() -> int:
    return int(time.time()) + 3_600


_SCHEDULED = {"ok": True, "channel": CHANNEL, "scheduled_message_id": "Q1", "post_at": 0}
_ALL_ROUTES = {
    "chat.postMessage": _posted(message={"type": "message", "ts": TS, "thread_ts": PARENT_TS}),
    "chat.getPermalink": PERMALINK_OK,
    "chat.update": {"ok": True, "channel": CHANNEL, "ts": TS},
    "chat.scheduleMessage": _SCHEDULED,
}


async def _run_tool(tool: str, text: str, fmt: Any, when: int) -> str:
    if tool == "post":
        return await slack_post_message(PostMessageInput(channel=CHANNEL, text=text, format=fmt))
    if tool == "reply":
        return await slack_reply_in_thread(
            ReplyInThreadInput(channel=CHANNEL, thread_ts=PARENT_TS, text=text, format=fmt)
        )
    if tool == "update":
        return await slack_update_message(UpdateMessageInput(channel=CHANNEL, ts=TS, text=text, format=fmt))
    return await slack_schedule_message(ScheduleMessageInput(channel=CHANNEL, post_at=when, text=text, format=fmt))


def _expected_wire(tool: str, message: dict[str, Any], when: int) -> tuple[str, dict[str, Any]]:
    """The exact (method, params) each tool sends around the message fields."""
    unfurls = {"unfurl_links": False, "unfurl_media": False}
    if tool == "post":
        return "chat.postMessage", {"channel": CHANNEL, **message, **unfurls}
    if tool == "reply":
        return "chat.postMessage", {
            "channel": CHANNEL,
            "thread_ts": PARENT_TS,
            **message,
            "reply_broadcast": False,
            **unfurls,
        }
    if tool == "update":
        return "chat.update", {"channel": CHANNEL, "ts": TS, **message}
    return "chat.scheduleMessage", {"channel": CHANNEL, "post_at": when, **message, **unfurls}


_TOOLS = ["post", "reply", "update", "schedule"]


@pytest.mark.parametrize("fmt", ["mrkdwn", "markdown"])
@pytest.mark.parametrize("tool", _TOOLS)
async def test_without_a_signature_every_tool_sends_plain_text_and_no_blocks(
    monkeypatch: pytest.MonkeyPatch, tool: str, fmt: Any
) -> None:
    fake = _FakeClient(routes=_ALL_ROUTES)
    _patch(monkeypatch, fake)  # default settings: SLACK_SIGNATURE unset
    when = _when()

    result = await _run_tool(tool, "Deploy done", fmt, when)

    key = "markdown_text" if fmt == "markdown" else "text"
    assert fake.calls[0] == _expected_wire(tool, {key: "Deploy done"}, when)
    assert "blocks" not in fake.calls[0][1]
    assert "- **signature**: not configured" in result
    assert "(edited)" not in result  # the blocks note is only for an edit sent with blocks


@pytest.mark.parametrize("fmt", ["mrkdwn", "markdown"])
@pytest.mark.parametrize("tool", _TOOLS)
async def test_with_a_signature_every_tool_sends_blocks_and_the_text_fallback(
    monkeypatch: pytest.MonkeyPatch, tool: str, fmt: Any
) -> None:
    _sign_with(monkeypatch)
    fake = _FakeClient(routes=_ALL_ROUTES)
    _patch(monkeypatch, fake)
    when = _when()

    result = await _run_tool(tool, "Deploy done", fmt, when)

    assert fake.calls[0] == _expected_wire(tool, _message("Deploy done", fmt), when)
    assert "markdown_text" not in fake.calls[0][1]  # Slack refuses markdown_text next to blocks
    assert "- **signature**: appended (context block)" in result


async def test_a_signed_post_is_exactly_this_wire_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    """One literal payload per format, written out by hand (no helper), with the recommended value."""
    _sign_with(monkeypatch, BOLD_SIG)
    fake = _FakeClient(routes=_ALL_ROUTES)
    _patch(monkeypatch, fake)

    await slack_post_message(PostMessageInput(channel=CHANNEL, text="Deploy done *now*\nsecond line"))
    await slack_post_message(PostMessageInput(channel=CHANNEL, text="## Plan\n- **one**", format="markdown"))

    assert fake.calls[0][1] == {
        "channel": CHANNEL,
        "text": "Deploy done *now*\nsecond line\n\n*Sent using* <@U0BOT000001>",
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": "Deploy done *now*\nsecond line"}},
            {"type": "context", "elements": [{"type": "mrkdwn", "text": "*Sent using* <@U0BOT000001>"}]},
        ],
        "unfurl_links": False,
        "unfurl_media": False,
    }
    assert fake.calls[2][1] == {
        "channel": CHANNEL,
        "text": "## Plan\n- **one**\n\n*Sent using* <@U0BOT000001>",
        "blocks": [
            {"type": "markdown", "text": "## Plan\n- **one**"},
            {"type": "context", "elements": [{"type": "mrkdwn", "text": "*Sent using* <@U0BOT000001>"}]},
        ],
        "unfurl_links": False,
        "unfurl_media": False,
    }


async def test_markdown_keeps_the_context_in_mrkdwn_and_the_fallback_in_markdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The context element is a mrkdwn text object: the link stays `<url|label>` even for format='markdown'."""
    _sign_with(monkeypatch)  # the URL fallback form
    fake = _FakeClient(routes=_ALL_ROUTES)
    _patch(monkeypatch, fake)

    await slack_post_message(PostMessageInput(channel=CHANNEL, text="- one\n- two", format="markdown"))

    params = fake.calls[0][1]
    assert params["blocks"][-1] == _context(SIG)
    assert params["text"] == f"- one\n- two\n\n{SIG_MD}"  # the fallback, as before t16


@pytest.mark.parametrize("fmt", ["mrkdwn", "markdown"])
async def test_reply_keeps_thread_and_broadcast_flags_with_blocks(monkeypatch: pytest.MonkeyPatch, fmt: Any) -> None:
    _sign_with(monkeypatch, MENTION_SIG)
    fake = _FakeClient(routes=_ALL_ROUTES)
    _patch(monkeypatch, fake)

    await slack_reply_in_thread(
        ReplyInThreadInput(
            channel=CHANNEL,
            thread_ts=PARENT_TS,
            text="Shipped",
            format=fmt,
            reply_broadcast=True,
            unfurl_links=True,
        )
    )

    assert fake.calls[0] == (
        "chat.postMessage",
        {
            "channel": CHANNEL,
            "thread_ts": PARENT_TS,
            **_message("Shipped", fmt, MENTION_SIG),
            "reply_broadcast": True,
            "unfurl_links": True,
            "unfurl_media": False,
        },
    )


async def test_schedule_with_a_thread_sends_blocks_too(monkeypatch: pytest.MonkeyPatch) -> None:
    _sign_with(monkeypatch, MENTION_SIG)
    fake = _FakeClient(routes=_ALL_ROUTES)
    _patch(monkeypatch, fake)
    when = _when()

    await slack_schedule_message(
        ScheduleMessageInput(channel=CHANNEL, post_at=when, text="Standup", thread_ts=PARENT_TS)
    )

    assert fake.calls == [
        (
            "chat.scheduleMessage",
            {
                "channel": CHANNEL,
                "post_at": when,
                **_message("Standup", "mrkdwn", MENTION_SIG),
                "unfurl_links": False,
                "unfurl_media": False,
                "thread_ts": PARENT_TS,
            },
        )
    ]


@pytest.mark.parametrize("fmt", ["mrkdwn", "markdown"])
async def test_update_always_resends_blocks_and_text(monkeypatch: pytest.MonkeyPatch, fmt: Any) -> None:
    """chat.update keeps old blocks only when `text` is omitted — so every signed edit resends them."""
    _sign_with(monkeypatch, MENTION_SIG)
    fake = _FakeClient(routes=_ALL_ROUTES)
    _patch(monkeypatch, fake)

    result = await slack_update_message(UpdateMessageInput(channel=CHANNEL, ts=TS, text="Deploy done (v2)", format=fmt))

    assert fake.calls == [
        ("chat.update", {"channel": CHANNEL, "ts": TS, **_message("Deploy done (v2)", fmt, MENTION_SIG)})
    ]
    assert "- **signature**: appended (context block)" in result
    assert (
        '- **note**: sent with Block Kit blocks — Slack does not show its "(edited)" mark on an edit made with blocks'
        in result
    )


@pytest.mark.parametrize("fmt", ["mrkdwn", "markdown"])
async def test_a_text_ending_with_the_signature_twice_keeps_one(monkeypatch: pytest.MonkeyPatch, fmt: Any) -> None:
    settings = Settings(slack_signature=SIG)
    for doubled in (f"hello\n\n{SIG}\n\n{SIG}", f"hello\n{SIG_MD}\n{SIG}", f"hello\n\n{SIG}\n\n{SIG_MD}\n\n{SIG}"):
        assert _sign(doubled, settings, fmt) == (_signed("hello", fmt), False), repr(doubled)
    assert _sign(f"{SIG}\n\n{SIG}", settings, fmt) == (SIGNATURES[fmt], False)  # nothing but signatures

    _sign_with(monkeypatch, BOLD_SIG)
    fake = _FakeClient(routes=_ALL_ROUTES)
    _patch(monkeypatch, fake)
    await slack_update_message(
        UpdateMessageInput(channel=CHANNEL, ts=TS, text=f"hello\n\n{BOLD_SIG}\n\n{BOLD_SIG}", format=fmt)
    )
    assert fake.calls[0][1] == {"channel": CHANNEL, "ts": TS, **_message("hello", fmt, BOLD_SIG)}


@pytest.mark.parametrize("fmt", ["mrkdwn", "markdown"])
@pytest.mark.parametrize("signature", [SIG, MENTION_SIG, BOLD_SIG])
async def test_update_of_a_signed_message_stays_signed_exactly_once(
    monkeypatch: pytest.MonkeyPatch, fmt: Any, signature: str
) -> None:
    _sign_with(monkeypatch, signature)
    signed = f"Deploy done (v2)\n\n{_signature_for(fmt, signature)}"  # the old text read back, edited above it
    body = {"ok": True, "channel": CHANNEL, "ts": TS, "text": signed}
    fake = _FakeClient(routes={"chat.update": body})
    _patch(monkeypatch, fake)

    result = await slack_update_message(UpdateMessageInput(channel=CHANNEL, ts=TS, text=signed, format=fmt))

    params = fake.calls[0][1]
    assert params == {"channel": CHANNEL, "ts": TS, **_message("Deploy done (v2)", fmt, signature)}
    assert params["text"].count(_signature_for(fmt, signature)) == 1
    assert all("Sent using" not in text for text in _body_texts(params))  # never inside a body block
    assert "- **signature**: already at the end of the text — not added again (context block)" in result
    assert result.count(_signature_for(fmt, signature)) == 1  # only in Slack's stored text


@pytest.mark.parametrize("fmt", ["mrkdwn", "markdown"])
async def test_update_from_slacks_read_back_text_with_entities_does_not_double_sign(
    monkeypatch: pytest.MonkeyPatch, fmt: Any
) -> None:
    """The edit flow: read the message back (Slack stores `&` as `&amp;`), change a word, update."""
    _sign_with(monkeypatch, AMP_SIG)
    fake = _FakeClient(routes={"chat.update": {"ok": True, "channel": CHANNEL, "ts": TS}})
    _patch(monkeypatch, fake)
    read_back = f"Deploy done &amp; verified\n{AMP_SIG_READ_BACK}"
    edited = read_back.replace("verified", "re-verified")

    result = await slack_update_message(UpdateMessageInput(channel=CHANNEL, ts=TS, text=edited, format=fmt))

    params = fake.calls[0][1]
    assert params["text"] == f"Deploy done &amp; re-verified{SEP[fmt]}{_signature_for(fmt, AMP_SIG)}"
    assert params["text"].count("@Alejandro AI") == 1
    assert _body_texts(params) == ["Deploy done &amp; re-verified"]  # the read-back signature line is cut
    assert params["blocks"][-1] == _context(AMP_SIG)
    assert "- **signature**: already at the end of the text — not added again (context block)" in result


@pytest.mark.parametrize("gap", ["\n", "\n\n", "\n\n\n", "\n  \n"])
async def test_read_back_edit_strips_the_signature_line_whatever_the_gap(
    monkeypatch: pytest.MonkeyPatch, gap: str
) -> None:
    _sign_with(monkeypatch, BOLD_SIG)
    fake = _FakeClient(routes={"chat.update": {"ok": True, "channel": CHANNEL, "ts": TS}})
    _patch(monkeypatch, fake)

    await slack_update_message(UpdateMessageInput(channel=CHANNEL, ts=TS, text=f"line one\nline two{gap}{BOLD_SIG}"))

    assert fake.calls[0][1] == {"channel": CHANNEL, "ts": TS, **_message("line one\nline two", "mrkdwn", BOLD_SIG)}


async def test_update_switching_format_keeps_one_signature_line(monkeypatch: pytest.MonkeyPatch) -> None:
    _sign_with(monkeypatch)
    fake = _FakeClient(routes={"chat.update": {"ok": True, "channel": CHANNEL, "ts": TS}})
    _patch(monkeypatch, fake)

    # Posted as mrkdwn (mrkdwn-form signature), edited as markdown.
    await slack_update_message(UpdateMessageInput(channel=CHANNEL, ts=TS, text=f"- a\n- b\n{SIG}", format="markdown"))

    assert fake.calls[0][1] == {
        "channel": CHANNEL,
        "ts": TS,
        "text": f"- a\n- b\n\n{SIG_MD}",
        "blocks": [{"type": "markdown", "text": "- a\n- b"}, _context(SIG)],
    }


async def test_a_signature_quoted_mid_text_stays_in_the_body_and_the_text_is_signed_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _sign_with(monkeypatch, MENTION_SIG)
    fake = _FakeClient(routes=_ALL_ROUTES)
    _patch(monkeypatch, fake)
    text = f"Quoting it:\n{MENTION_SIG}\nand more after"

    await slack_post_message(PostMessageInput(channel=CHANNEL, text=text))

    assert fake.calls[0][1]["text"] == f"{text}\n\n{MENTION_SIG}"
    assert fake.calls[0][1]["blocks"] == [_section(text), _context(MENTION_SIG)]


@pytest.mark.parametrize("fmt", ["mrkdwn", "markdown"])
async def test_a_text_that_is_only_the_signature_sends_just_the_context_block(
    monkeypatch: pytest.MonkeyPatch, fmt: Any
) -> None:
    _sign_with(monkeypatch, MENTION_SIG)
    fake = _FakeClient(routes={"chat.update": {"ok": True, "channel": CHANNEL, "ts": TS}})
    _patch(monkeypatch, fake)

    await slack_update_message(UpdateMessageInput(channel=CHANNEL, ts=TS, text=MENTION_SIG, format=fmt))

    assert fake.calls[0][1] == {"channel": CHANNEL, "ts": TS, "text": MENTION_SIG, "blocks": [_context(MENTION_SIG)]}


# -- section chunking (3,000 characters per section) and the 50-block cap --


def test_section_chunks_cut_at_line_boundaries_at_3000() -> None:
    assert SECTION_TEXT_MAX == 3_000
    assert _section_chunks("short\ntext") == ["short\ntext"]
    exactly = "a" * 1_499 + "\n" + "b" * 1_500  # 3,000 characters with the newline: still one section
    assert _section_chunks(exactly) == [exactly]
    one_over = exactly + "c"  # 3,001: the last line moves to a new section
    assert _section_chunks(one_over) == ["a" * 1_499, "b" * 1_500 + "c"]
    lines = [f"{i:04d} " + "x" * 995 for i in range(7)]  # seven 1,000-character lines
    chunks = _section_chunks("\n".join(lines))
    assert chunks == ["\n".join(lines[0:2]), "\n".join(lines[2:4]), "\n".join(lines[4:6]), lines[6]]
    assert all(len(chunk) <= SECTION_TEXT_MAX for chunk in chunks)


def test_section_chunks_hard_split_a_line_longer_than_3000() -> None:
    long_line = "".join(chr(ord("a") + i % 26) for i in range(7_000))
    chunks = _section_chunks(f"intro\n{long_line}\noutro")
    assert chunks == ["intro", long_line[:3_000], long_line[3_000:6_000], long_line[6_000:] + "\noutro"]
    assert "".join(chunks[1:]).replace("\noutro", "") == long_line  # nothing lost
    assert _section_chunks("q" * 3_500) == ["q" * 3_000, "q" * 500]  # no whitespace at all: still splits


def test_a_long_line_is_cut_at_a_space_in_the_second_half_of_the_window() -> None:
    words = " ".join(["abcd"] * 700)  # 3,499 characters, a space every 5
    chunks = _section_chunks(words)
    assert chunks == [" ".join(["abcd"] * 600), " ".join(["abcd"] * 100)]  # cut at the space at 2,999
    assert " ".join(chunks) == words  # only that one space is dropped
    # A short spaced prefix before a long unbroken run is not a tiny section of its own.
    probe = "O-2 probe (chat.postMessage, 4001 chars): " + "0123456789" * 397
    assert [len(chunk) for chunk in _section_chunks(probe)] == [3_000, len(probe) - 3_000]


def test_a_mention_or_a_link_across_the_cut_stays_whole() -> None:
    mention = "<@U0ANA000001>"
    line = "x" * 2_996 + mention + " tail"  # no space before the mention; the window ends inside it
    assert _section_chunks(line) == ["x" * 2_996, f"{mention} tail"]

    link = "<https://example.com/path|a label with spaces>"  # its label has spaces inside the window
    line = "y" * 2_969 + " " + link + " end"
    assert _section_chunks(line) == ["y" * 2_969, f"{link} end"]  # cut before the link and its space

    huge = "<https://example.com/" + "p" * 3_500 + "|x>"  # one entity longer than a section: hard cut
    chunks = _section_chunks(huge)
    assert chunks == [huge[:3_000], huge[3_000:]]
    assert "".join(chunks) == huge


def test_section_chunks_drop_blank_edges_and_empty_pieces() -> None:
    first = "a" * 2_000
    second = "b" * 2_000
    # The blank line between them lands at the cut: no section starts or ends with it.
    assert _section_chunks(f"{first}\n\n{second}") == [first, second]
    assert _section_chunks(f"{first}\n  \n{second}") == [first, second]  # whitespace-only, too
    assert _section_chunks("  \nabc\n \t") == ["abc"]
    assert _section_chunks("") == []
    assert _section_chunks("\n \n") == []


async def test_a_long_mrkdwn_post_is_sent_as_several_sections(monkeypatch: pytest.MonkeyPatch) -> None:
    _sign_with(monkeypatch, MENTION_SIG)
    fake = _FakeClient(routes=_ALL_ROUTES)
    _patch(monkeypatch, fake)
    lines = ["*" + f"{i:02d}" + "y" * 1_997 for i in range(5)]  # five 2,000-character lines
    text = "\n".join(lines)

    await slack_post_message(PostMessageInput(channel=CHANNEL, text=text))

    params = fake.calls[0][1]
    assert params["text"] == f"{text}\n\n{MENTION_SIG}"  # the fallback keeps the whole text
    assert params["blocks"] == [*(_section(line) for line in lines), _context(MENTION_SIG)]


def test_build_message_allows_49_sections_and_refuses_50() -> None:
    settings = Settings(slack_signature=MENTION_SIG)
    line = "z" * SECTION_TEXT_MAX
    fits = "\n".join([line] * 49)
    fields = _build_message(f"{fits}\n\n{MENTION_SIG}", "mrkdwn", settings)
    assert len(fields["blocks"]) == MESSAGE_BLOCKS_MAX == 50
    assert fields["blocks"][-1] == _context(MENTION_SIG)

    too_many = "\n".join([line] * 50)
    with pytest.raises(BlockLimitError, match=r"needs 50 section blocks .* at most 50 blocks per message"):
        _build_message(f"{too_many}\n\n{MENTION_SIG}", "mrkdwn", settings)


async def test_a_post_over_the_block_cap_is_refused_before_any_call(monkeypatch: pytest.MonkeyPatch) -> None:
    _sign_with(monkeypatch, MENTION_SIG)
    monkeypatch.setattr("slack_mcp.tools.chat.MESSAGE_BLOCKS_MAX", 3)  # reachable with a short text
    fake = _FakeClient(routes=_ALL_ROUTES)
    _patch(monkeypatch, fake)
    text = "\n".join(["w" * 2_000] * 3)  # three sections + the context block = 4 > 3

    result = await slack_post_message(PostMessageInput(channel=CHANNEL, text=text))

    assert result.startswith("Error: the text needs 3 section blocks of at most 3,000 characters"), result
    assert "Slack allows at most 3 blocks per message" in result
    assert "Nothing was sent." in result
    assert fake.calls == []


def test_build_message_refuses_a_markdown_block_over_12000() -> None:
    settings = Settings(slack_signature=MENTION_SIG)
    assert MARKDOWN_BLOCK_MAX == 12_000
    ok = _build_message("m" * 12_000 + f"\n\n{MENTION_SIG}", "markdown", settings)
    assert ok["blocks"][0] == {"type": "markdown", "text": "m" * 12_000}
    with pytest.raises(BlockLimitError, match="markdown block, which Slack limits to 12,000"):
        _build_message("m" * 12_001 + f"\n\n{MENTION_SIG}", "markdown", settings)


@pytest.mark.parametrize("tool", _TOOLS)
async def test_a_signature_longer_than_a_context_text_is_refused_before_any_call(
    monkeypatch: pytest.MonkeyPatch, tool: str
) -> None:
    assert CONTEXT_TEXT_MAX == 3_000
    _sign_with(monkeypatch, "S" * (CONTEXT_TEXT_MAX + 1))
    fake = _FakeClient(routes=_ALL_ROUTES)
    _patch(monkeypatch, fake)

    result = await _run_tool(tool, "hi", "mrkdwn", _when())

    assert result.startswith(
        "Error: SLACK_SIGNATURE is 3,001 characters, but it is sent as the text of a Block Kit context block, "
        "which Slack limits to 3,000 characters — shorten SLACK_SIGNATURE. Nothing was sent."
    ), result
    assert fake.calls == []


async def test_a_signature_of_exactly_3000_characters_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    signature = "S" * CONTEXT_TEXT_MAX
    _sign_with(monkeypatch, signature)
    fake = _FakeClient(routes=_ALL_ROUTES)
    _patch(monkeypatch, fake)

    result = await slack_update_message(UpdateMessageInput(channel=CHANNEL, ts=TS, text="hi"))

    assert result.startswith("# Message updated")
    assert fake.calls[0][1]["blocks"] == [_section("hi"), _context(signature)]


async def test_every_confirmation_says_when_no_signature_is_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    when = int(time.time()) + 3_600
    fake = _FakeClient(
        routes={
            "chat.postMessage": _posted(),
            "chat.getPermalink": PERMALINK_OK,
            "chat.update": {"ok": True, "channel": CHANNEL, "ts": TS},
            "chat.scheduleMessage": {"ok": True, "channel": CHANNEL, "scheduled_message_id": "Q1", "post_at": when},
        }
    )
    _patch(monkeypatch, fake)  # default settings: SLACK_SIGNATURE unset

    results = [
        await slack_post_message(PostMessageInput(channel=CHANNEL, text="a")),
        await slack_reply_in_thread(ReplyInThreadInput(channel=CHANNEL, thread_ts=PARENT_TS, text="b")),
        await slack_update_message(UpdateMessageInput(channel=CHANNEL, ts=TS, text="c")),
        await slack_schedule_message(ScheduleMessageInput(channel=CHANNEL, post_at=when, text="d")),
    ]

    assert all("- **signature**: not configured" in result for result in results)
    sent = [params.get("text") for method, params in fake.calls if method != "chat.getPermalink"]
    assert sent == ["a", "b", "c", "d"]


def _overflow_message(final: int, limit: int, signature: str) -> str:
    return (
        f"Error: text is {final:,} characters including the {len(signature):,}-character signature "
        f"(SLACK_SIGNATURE); the limit is {limit:,}"
    )


async def test_update_cap_counts_the_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    _sign_with(monkeypatch)
    fake = _FakeClient(routes={"chat.update": {"ok": True, "channel": CHANNEL, "ts": TS}})
    _patch(monkeypatch, fake)
    room = UPDATE_TEXT_MAX - len(SIG) - 2  # the text that exactly fits with "\n\n" + signature

    fits = await slack_update_message(UpdateMessageInput(channel=CHANNEL, ts=TS, text="x" * room))
    assert fits.startswith("# Message updated")
    assert len(fake.calls[0][1]["text"]) == UPDATE_TEXT_MAX

    over = await slack_update_message(UpdateMessageInput(channel=CHANNEL, ts=TS, text="x" * (room + 3)))
    assert over.startswith(_overflow_message(UPDATE_TEXT_MAX + 3, UPDATE_TEXT_MAX, SIG))
    assert "(chat.update refuses longer text with msg_too_long)" in over
    assert "shorten the text by at least 3 characters. Nothing was sent." in over
    assert len(fake.calls) == 1  # the over-long edit never reached Slack


@pytest.mark.parametrize(("fmt", "limit"), [("mrkdwn", POST_TEXT_MAX), ("markdown", MARKDOWN_TEXT_MAX)])
async def test_post_reply_and_schedule_caps_count_the_signature(
    monkeypatch: pytest.MonkeyPatch, fmt: Any, limit: int
) -> None:
    _sign_with(monkeypatch)
    fake = _FakeClient(routes={"chat.postMessage": _posted(), "chat.getPermalink": PERMALINK_OK})
    _patch(monkeypatch, fake)
    signature = SIGNATURES[fmt]
    text = "x" * (limit - len(signature) - len(SEP[fmt]) + 1)  # fits as typed; one over once signed
    when = int(time.time()) + 3_600

    results = [
        await slack_post_message(PostMessageInput(channel=CHANNEL, text=text, format=fmt)),
        await slack_reply_in_thread(ReplyInThreadInput(channel=CHANNEL, thread_ts=PARENT_TS, text=text, format=fmt)),
        await slack_schedule_message(ScheduleMessageInput(channel=CHANNEL, post_at=when, text=text, format=fmt)),
    ]

    for result in results:
        assert result.startswith(_overflow_message(limit + 1, limit, signature)), result
        assert "shorten the text by at least 1 character. Nothing was sent." in result  # singular
    assert fake.calls == []


async def test_an_already_signed_text_at_the_cap_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    _sign_with(monkeypatch)
    fake = _FakeClient(routes={"chat.update": {"ok": True, "channel": CHANNEL, "ts": TS}})
    _patch(monkeypatch, fake)
    signed = "x" * (UPDATE_TEXT_MAX - len(SIG) - 2) + "\n\n" + SIG

    result = await slack_update_message(UpdateMessageInput(channel=CHANNEL, ts=TS, text=signed))

    assert result.startswith("# Message updated")
    assert fake.calls[0][1]["text"] == signed


async def test_a_signed_text_whose_rewritten_blank_line_crosses_the_cap_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Signed with a single newline (the pre-t15 separator) and exactly at the cap: the blank line adds one."""
    _sign_with(monkeypatch)
    fake = _FakeClient(routes={"chat.update": {"ok": True, "channel": CHANNEL, "ts": TS}})
    _patch(monkeypatch, fake)
    signed = "x" * (UPDATE_TEXT_MAX - len(SIG) - 1) + "\n" + SIG

    result = await slack_update_message(UpdateMessageInput(channel=CHANNEL, ts=TS, text=signed))

    assert result.startswith(_overflow_message(UPDATE_TEXT_MAX + 1, UPDATE_TEXT_MAX, SIG)), result
    assert fake.calls == []


async def test_e2e_the_signature_from_the_env_goes_on_the_wire(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_SIGNATURE", f"  {SIG}\n")
    get_settings.cache_clear()
    recorder = _Recorder({"chat.postMessage": _posted(), "chat.getPermalink": PERMALINK_OK})
    _real_client(monkeypatch, recorder, slack_allow_writes=True, slack_write_channels=CHANNEL)

    result = await slack_post_message(PostMessageInput(channel=CHANNEL, text="hello"))

    # Through the real client: the blocks stay a JSON array (chat.postMessage is a JSON method).
    assert json.loads(recorder.requests[0].content) == {
        "channel": CHANNEL,
        "text": f"hello\n\n{SIG}",
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": "hello"}},
            {"type": "context", "elements": [{"type": "mrkdwn", "text": SIG}]},
        ],
        "unfurl_links": False,
        "unfurl_media": False,
    }
    assert "- **signature**: appended (context block)" in result


# The measured fact replaced every "Sent using …" promise; this keeps the claim from coming
# back. In the docs the phrase may appear only as a SLACK_SIGNATURE value — the recommended
# bold label + bot-user mention `*Sent using* <@U…>` (or without the bold), or the URL
# fallback in its mrkdwn form `Sent using <https://…|…>` or its markdown form
# `Sent using [label](https://…)`, each optionally bold.
_REPO = Path(__file__).resolve().parents[1]
_DOCS = ("README.md", ".env.example", "SECURITY.md")
_SIGNATURE_VALUE_USE = re.compile(r"(?i)sent using(?!\*? <@U|\*? <https://|\*? \[[^\]\n]*\]\(https://)")


def test_the_docs_guard_still_catches_the_old_claim() -> None:
    old = 'Posts show your name plus Slack\'s "Sent using @<app>" line; the "Sent using …" line.'
    assert len(_SIGNATURE_VALUE_USE.findall(old)) == 2
    assert len(_SIGNATURE_VALUE_USE.findall("*Sent using* @Alejandro AI, *Sent using*: a label")) == 2
    allowed = [SIG, SIG_MD, MENTION_SIG, BOLD_SIG, "Sent using <@U0BOTID>", "*Sent using* <@U0BOTID>"]
    allowed += [f"*Sent using* <{APP_URL}|@Alejandro AI>", f"*Sent using* [@Alejandro AI]({APP_URL})"]
    assert _SIGNATURE_VALUE_USE.findall("\n".join(allowed)) == []


def test_no_source_file_claims_a_sent_using_line() -> None:
    offenders = [
        str(path.relative_to(_REPO))
        for path in sorted((_REPO / "src").rglob("*.py"))
        if "sent using" in path.read_text(encoding="utf-8").lower()
    ]
    assert offenders == []


@pytest.mark.parametrize("name", _DOCS)
def test_the_docs_mention_sent_using_only_as_the_signature_value(name: str) -> None:
    text = (_REPO / name).read_text(encoding="utf-8")
    claims = [match.start() for match in _SIGNATURE_VALUE_USE.finditer(text)]
    assert claims == [], f"{name}: 'Sent using' outside a SLACK_SIGNATURE value at offsets {claims}"
