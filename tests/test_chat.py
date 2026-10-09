"""Unit tests for the chat tools (post / reply / update / schedule) against a fake client.

No live tests here on purpose: these tools write AS THE USER, and the live write smoke
belongs to the conductor's t6 (self-DM only). Three end-to-end tests at the bottom run the
real `SlackClient` over `httpx.MockTransport` to prove the tools' writes go through the
client's guard and on the wire as JSON.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import ValidationError

from slack_mcp.client import SlackClient
from slack_mcp.config import Settings
from slack_mcp.errors import SlackApiError
from slack_mcp.formatters import ts_to_human
from slack_mcp.guard import WriteNotAllowedError, WritesDisabledError
from slack_mcp.tools.chat import (
    MARKDOWN_TEXT_MAX,
    POST_TEXT_MAX,
    SCHEDULE_MAX_SECONDS,
    UPDATE_TEXT_MAX,
    PostMessageInput,
    ReplyInThreadInput,
    ScheduleMessageInput,
    UpdateMessageInput,
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
PERMALINK = "https://udocz.slack.com/archives/C0123ABCDEF/p1791000000123456"


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
    assert 'with Slack\'s "Sent using @Alejandro AI" line' in doc
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
