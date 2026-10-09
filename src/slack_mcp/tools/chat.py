"""Slack `chat` tools: post, reply in a thread, edit, and schedule messages — AS YOU.

Every tool here writes with the user token, so Slack shows the message under your own
name and avatar, with Slack's "Sent using @Alejandro AI" line. The tools never gate,
never retry and never pick an encoding: `client.call()` runs the write guard on the
exact parameters that go on the wire (kill-switch, ID-only channel allowlist, DM
membership, Slack Connect refusal), sends `chat.postMessage` / `chat.update` /
`chat.scheduleMessage` as JSON, and refuses to retry a write.

Two facts from research 02 shape the inputs: `chat.postMessage` truncates `text` over
40,000 characters and only says so with a `message_truncated` warning (so every
confirmation renders `fmt_warnings`), and `chat.update` errors with `msg_too_long` past
4,000 characters (so the update tool refuses that locally). `format="markdown"` sends
the same string as `markdown_text` — real Markdown, at most 12,000 characters, never
combined with `text`.
"""

from __future__ import annotations

import time
from typing import Any, Literal, Self

from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from slack_mcp.client import SlackClient, get_client
from slack_mcp.errors import handle_api_error
from slack_mcp.formatters import clip_response, fmt_warnings, ts_to_human
from slack_mcp.server import mcp
from slack_mcp.validators import channel_id, iso_to_epoch, message_ts

# `mrkdwn` sends `text` (Slack's own syntax); `markdown` sends `markdown_text` (real Markdown).
MessageFormat = Literal["mrkdwn", "markdown"]

# Research 02 §4.13–§4.15.
POST_TEXT_MAX = 40_000  # chat.postMessage / scheduleMessage: Slack truncates beyond this
MARKDOWN_TEXT_MAX = 12_000  # markdown_text: "Limit this field to 12,000 characters"
UPDATE_TEXT_MAX = 4_000  # chat.update: msg_too_long past 4,000 characters
SCHEDULE_MAX_DAYS = 120  # chat.scheduleMessage: "up to 120 days into the future"
SCHEDULE_MAX_SECONDS = SCHEDULE_MAX_DAYS * 86_400

_CHANNEL_DESCRIPTION = (
    "Conversation ID to post in: C… (channel), D… (DM) or G… (group DM / private channel). Never a #name or a "
    "U… user id — find a channel id with slack_list_conversations, and open a DM with slack_open_conversation."
)
_FORMAT_DESCRIPTION = (
    "How Slack reads `text`. 'mrkdwn' (default) = Slack's own syntax: *bold* (single asterisks), _italic_, "
    "~strike~, `code`, <@U0123ABCDEF> mentions, <https://example.com|label> links, no headers. 'markdown' = real "
    "Markdown (**bold**, # headings, [label](url)), sent as markdown_text, at most 12,000 characters."
)
_UNFURL_LINKS_DESCRIPTION = "Let Slack expand link previews in the message (default false)."
_UNFURL_MEDIA_DESCRIPTION = "Let Slack expand image/video previews in the message (default false)."


def _now() -> float:
    """Current epoch seconds (indirection so tests can pin the clock)."""
    return time.time()


def _thread_ts_or_none(value: object) -> str | None:
    return None if value is None else message_ts(value, field="thread_ts")


def _check_markdown_length(fmt: MessageFormat, text: str) -> None:
    if fmt == "markdown" and len(text) > MARKDOWN_TEXT_MAX:
        raise ValueError(
            f"text is {len(text):,} characters, but format='markdown' sends it as markdown_text, which Slack limits "
            f"to {MARKDOWN_TEXT_MAX:,} characters — shorten it, split it into several messages, or use "
            f"format='mrkdwn' (up to {POST_TEXT_MAX:,})"
        )


def _text_field(fmt: MessageFormat, text: str) -> dict[str, str]:
    """`{"text": …}` or `{"markdown_text": …}` — never both (Slack: `markdown_text_conflict`)."""
    return {"markdown_text": text} if fmt == "markdown" else {"text": text}


# -- input models ----------------------------------------------------------------


class PostMessageInput(BaseModel):
    """Input for slack_post_message."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    channel: str = Field(..., description=_CHANNEL_DESCRIPTION)
    text: str = Field(
        ...,
        min_length=1,
        max_length=POST_TEXT_MAX,
        description=(
            "The message, sent verbatim. Keep it under ~4,000 characters for readability; Slack truncates past "
            "40,000 (and at most 12,000 with format='markdown')."
        ),
    )
    format: MessageFormat = Field(default="mrkdwn", description=_FORMAT_DESCRIPTION)
    thread_ts: str | None = Field(
        default=None,
        description=(
            "Optional: the PARENT message's ts (1712345678.123456) to post as a thread reply. Prefer "
            "slack_reply_in_thread for replies."
        ),
    )
    unfurl_links: bool = Field(default=False, description=_UNFURL_LINKS_DESCRIPTION)
    unfurl_media: bool = Field(default=False, description=_UNFURL_MEDIA_DESCRIPTION)

    @field_validator("channel", mode="before")
    @classmethod
    def _channel(cls, value: object) -> str:
        return channel_id(value)

    @field_validator("thread_ts", mode="before")
    @classmethod
    def _thread_ts(cls, value: object) -> str | None:
        return _thread_ts_or_none(value)

    @model_validator(mode="after")
    def _markdown_limit(self) -> Self:
        _check_markdown_length(self.format, self.text)
        return self


class ReplyInThreadInput(BaseModel):
    """Input for slack_reply_in_thread."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    channel: str = Field(..., description="Conversation ID (C…/D…/G…) the thread lives in.")
    thread_ts: str = Field(
        ...,
        description=(
            "The thread's PARENT message ts (1712345678.123456) — as printed by slack_get_history; use the parent, "
            "not the ts of a reply inside the thread."
        ),
    )
    text: str = Field(
        ...,
        min_length=1,
        max_length=POST_TEXT_MAX,
        description="The reply, sent verbatim (Slack truncates past 40,000; at most 12,000 with format='markdown').",
    )
    format: MessageFormat = Field(default="mrkdwn", description=_FORMAT_DESCRIPTION)
    reply_broadcast: bool = Field(
        default=False,
        description="Also show the reply in the channel itself ('Also send to #channel'). Default false.",
    )
    unfurl_links: bool = Field(default=False, description=_UNFURL_LINKS_DESCRIPTION)
    unfurl_media: bool = Field(default=False, description=_UNFURL_MEDIA_DESCRIPTION)

    @field_validator("channel", mode="before")
    @classmethod
    def _channel(cls, value: object) -> str:
        return channel_id(value)

    @field_validator("thread_ts", mode="before")
    @classmethod
    def _thread_ts(cls, value: object) -> str:
        return message_ts(value, field="thread_ts")

    @model_validator(mode="after")
    def _markdown_limit(self) -> Self:
        _check_markdown_length(self.format, self.text)
        return self


class UpdateMessageInput(BaseModel):
    """Input for slack_update_message."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    channel: str = Field(
        ..., description="Conversation ID (C…/D…/G…) of the message. For a DM this is the D… id, never a U… id."
    )
    ts: str = Field(..., description="The ts (1712345678.123456) of YOUR message to edit.")
    text: str = Field(
        ...,
        min_length=1,
        max_length=UPDATE_TEXT_MAX,
        description="The new text, replacing the old one entirely. At most 4,000 characters (chat.update's limit).",
    )
    format: MessageFormat = Field(default="mrkdwn", description=_FORMAT_DESCRIPTION)

    @field_validator("channel", mode="before")
    @classmethod
    def _channel(cls, value: object) -> str:
        return channel_id(value)

    @field_validator("ts", mode="before")
    @classmethod
    def _ts(cls, value: object) -> str:
        return message_ts(value, field="ts")


class ScheduleMessageInput(BaseModel):
    """Input for slack_schedule_message."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    channel: str = Field(..., description=_CHANNEL_DESCRIPTION)
    post_at: str | int = Field(
        ...,
        description=(
            "When Slack should post it: ISO-8601 (e.g. 2026-10-12T14:00:00Z or 2026-10-12T09:00:00-05:00; no "
            "offset = UTC) or epoch seconds. Must be in the future and at most 120 days ahead."
        ),
    )
    text: str = Field(
        ...,
        min_length=1,
        max_length=POST_TEXT_MAX,
        description="The message, sent verbatim (at most 12,000 characters with format='markdown').",
    )
    format: MessageFormat = Field(default="mrkdwn", description=_FORMAT_DESCRIPTION)
    thread_ts: str | None = Field(
        default=None,
        description="Optional: the PARENT message's ts (1712345678.123456) to schedule a thread reply.",
    )
    unfurl_links: bool = Field(default=False, description=_UNFURL_LINKS_DESCRIPTION)
    unfurl_media: bool = Field(default=False, description=_UNFURL_MEDIA_DESCRIPTION)

    @field_validator("channel", mode="before")
    @classmethod
    def _channel(cls, value: object) -> str:
        return channel_id(value)

    @field_validator("post_at", mode="before")
    @classmethod
    def _post_at(cls, value: object) -> int:
        epoch = iso_to_epoch(value, field="post_at")
        now = _now()
        if epoch <= now:
            raise ValueError(
                f"post_at must be in the future: {ts_to_human(epoch)} is not after now ({ts_to_human(now)})"
            )
        if epoch - now > SCHEDULE_MAX_SECONDS:
            raise ValueError(
                f"post_at must be at most {SCHEDULE_MAX_DAYS} days ahead (Slack's limit): {ts_to_human(epoch)} is "
                f"{(epoch - now) / 86_400:.1f} days out"
            )
        return epoch

    @field_validator("thread_ts", mode="before")
    @classmethod
    def _thread_ts(cls, value: object) -> str | None:
        return _thread_ts_or_none(value)

    @model_validator(mode="after")
    def _markdown_limit(self) -> Self:
        _check_markdown_length(self.format, self.text)
        return self


# -- shared post path ------------------------------------------------------------


async def _permalink_line(client: SlackClient, channel: str, ts: str) -> str:
    """The permalink bullet. A lookup failure is reported here, never as a failed post."""
    if not channel or not ts:
        return "- **permalink**: not looked up — Slack's answer did not include the channel and ts"
    try:
        body = await client.call("chat.getPermalink", {"channel": channel, "message_ts": ts})
    except Exception as exc:
        return (
            f"- **permalink**: lookup failed — {handle_api_error(exc)} The message itself WAS sent; do not post it "
            "again."
        )
    link = body.get("permalink")
    return f"- **permalink**: {link}" if link else "- **permalink**: Slack returned none"


async def _post_and_confirm(wire: dict[str, Any], *, heading: str) -> str:
    """Send ONE chat.postMessage (no retry) and render what Slack returned, plus the permalink.

    If the post raises, the exception goes to the caller's handler (not sent / status
    unknown). Once Slack answered `ok`, the message exists: everything after that point
    reports it as SENT, whatever the permalink lookup does.
    """
    client = get_client()
    body = await client.call("chat.postMessage", wire)
    channel = str(body.get("channel") or "")
    ts = str(body.get("ts") or "")
    raw_message = body.get("message")
    message: dict[str, Any] = raw_message if isinstance(raw_message, dict) else {}
    lines = [
        f"# {heading}",
        "",
        "SENT — Slack accepted the message (ok), posted as you. Do not send it again.",
        "",
        f"- **channel**: {channel or 'N/A'}",
        f"- **ts**: {ts or 'N/A'} ({ts_to_human(ts)}) — pass it as `ts` to slack_update_message to edit it",
    ]
    thread_ts = message.get("thread_ts")
    if thread_ts and str(thread_ts) != ts:
        lines.append(f"- **thread_ts**: {thread_ts} (the thread's parent — use it for further replies)")
    lines.append(await _permalink_line(client, channel or str(wire.get("channel") or ""), ts))
    warnings = fmt_warnings(body)
    if warnings:
        lines.append(warnings)
    return clip_response("\n".join(lines))


# -- tools -----------------------------------------------------------------------


@mcp.tool(
    name="slack_post_message",
    annotations=ToolAnnotations(
        title="Post Slack Message (as you)",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
async def slack_post_message(params: PostMessageInput) -> str:
    """Post a message to a Slack channel, DM or group DM — posted as you, with Slack's "Sent using @Alejandro AI" line.

    Calls `chat.postMessage` once with your user token, so the message shows your name
    and avatar plus Slack's "Sent using @Alejandro AI" line — there is no bot identity.
    The text goes out verbatim. With `format="mrkdwn"` (default) it is sent as `text` in
    Slack's own syntax: `*bold*` (single asterisks), `_italic_`, `` `code` ``,
    `<@U0123ABCDEF>` to mention someone (an id, never a name), `<https://x.y|label>` for a
    link, and no headers. With `format="markdown"` the same string is sent as
    `markdown_text` (real Markdown, at most 12,000 characters) instead of `text`. After a
    successful post the tool looks up the message permalink with `chat.getPermalink`.

    Refused unless `SLACK_ALLOW_WRITES=1` AND the target is allowed by
    `SLACK_WRITE_CHANNELS` / `SLACK_WRITE_USERS` (Slack Connect channels only by explicit
    ID) — the client's write guard checks this before anything is sent. The post is
    never retried, by the tool or the client: a retry could duplicate the message.

    When to Use:
    - To send a new message to a channel or an existing DM / group DM, by conversation ID.
    - To send an update, a checklist or an answer the user asked you to post AS THEM.

    When NOT to Use:
    - To reply inside a thread: use `slack_reply_in_thread` (explicit thread intent).
    - To change a message you already posted: use `slack_update_message`.
    - To post later: use `slack_schedule_message`.
    - To message a person you have no DM id for: `slack_open_conversation` first, then post
      to the returned D…/G… id (never a U… id). Channel names → ids: `slack_list_conversations`;
      people → `<@U…>` ids: `slack_resolve_user`.

    Returns:
    A confirmation echoing only what Slack returned: `channel`, `ts` (with its UTC time),
    the permalink, and any Slack warning (e.g. `message_truncated` when Slack cut text past
    40,000 characters — the message was still posted). If the permalink lookup fails the
    message is still reported as SENT, with the lookup error. On failure, an `Error ...`
    string; nothing was posted unless it says the status is UNKNOWN.

    Examples:
    - `params = {"channel": "C0123ABCDEF", "text": "Deploy done :white_check_mark: <@U0ANA000001>"}`
    - `params = {"channel": "D0123ABCDEF", "text": "## Plan\\n- **one**\\n- two", "format": "markdown"}`

    Error Handling:
    - `Error: ... SLACK_ALLOW_WRITES ...` / `... SLACK_WRITE_CHANNELS ...` / `... SLACK_WRITE_USERS ...`:
      the write guard refused; the error names the env var that would allow it.
    - `not_in_channel`: you are not a member — join in Slack first (this server never auto-joins).
    - `channel_not_found` (wrong id / no access), `is_archived`, `restricted_action*` (channel policy),
      `rate_limited` (about 1 message per second per channel), `missing_scope` (needs the `chat:write`
      user scope).
    - A timeout or 5xx means the execution status is UNKNOWN: read the conversation back with
      `slack_get_history` before posting again.
    """
    try:
        wire: dict[str, Any] = {
            "channel": params.channel,
            **_text_field(params.format, params.text),
            "unfurl_links": params.unfurl_links,
            "unfurl_media": params.unfurl_media,
        }
        if params.thread_ts is not None:
            wire["thread_ts"] = params.thread_ts
        return await _post_and_confirm(wire, heading="Message sent")
    except Exception as exc:
        return handle_api_error(exc)


@mcp.tool(
    name="slack_reply_in_thread",
    annotations=ToolAnnotations(
        title="Reply in Slack Thread (as you)",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
async def slack_reply_in_thread(params: ReplyInThreadInput) -> str:
    """Reply inside a Slack thread — posted as you, with Slack's "Sent using @Alejandro AI" line.

    Calls `chat.postMessage` once with `thread_ts` set to the thread's PARENT message, so
    the reply lands in that thread under your name and avatar plus Slack's "Sent using
    @Alejandro AI" line. `reply_broadcast=true` also shows it in the channel. Text and
    `format` work exactly as in `slack_post_message` (`mrkdwn` → `text` with `*bold*`,
    `<@U…>` mentions and `<url|label>` links; `markdown` → `markdown_text`, ≤ 12,000
    characters). After a successful post the permalink is looked up with `chat.getPermalink`.

    Refused unless `SLACK_ALLOW_WRITES=1` AND the target is allowed by
    `SLACK_WRITE_CHANNELS` / `SLACK_WRITE_USERS` (Slack Connect channels only by explicit
    ID) — the client's write guard checks this before anything is sent. The reply is
    never retried, by the tool or the client: a retry could duplicate it.

    When to Use:
    - To answer a specific message, keeping the conversation in its thread.
    - To follow up on a message you posted (use the `ts` its confirmation printed).

    When NOT to Use:
    - For a new top-level message: use `slack_post_message`.
    - To edit a reply you already sent: use `slack_update_message`.
    - To find the parent ts: read the channel with `slack_get_history` / `slack_get_thread`
      (conversation ids come from `slack_list_conversations`; mention ids from `slack_resolve_user`).

    Returns:
    A confirmation echoing only what Slack returned: `channel`, `ts` of the reply, the
    thread's parent `thread_ts`, the permalink, and any Slack warning. If the permalink
    lookup fails the reply is still reported as SENT, with the lookup error. On failure,
    an `Error ...` string; nothing was posted unless it says the status is UNKNOWN.

    Examples:
    - `params = {"channel": "C0123ABCDEF", "thread_ts": "1712345678.123456", "text": "On it — ETA 3 pm"}`
    - `params = {"channel": "C0123ABCDEF", "thread_ts": "1712345678.123456", "text": "Shipped",
      "reply_broadcast": true}`

    Error Handling:
    - `Error: ... SLACK_ALLOW_WRITES ...` / `... SLACK_WRITE_CHANNELS ...` / `... SLACK_WRITE_USERS ...`:
      the write guard refused; the error names the env var that would allow it.
    - `not_in_channel` (join in Slack first), `channel_not_found`, `thread_not_found` / `cannot_reply_to_message`
      (wrong `thread_ts` — use the parent), `restricted_action_thread_locked`, `rate_limited`,
      `missing_scope` (needs the `chat:write` user scope).
    - A timeout or 5xx means the execution status is UNKNOWN: read the thread back with
      `slack_get_thread` before replying again.
    """
    try:
        wire: dict[str, Any] = {
            "channel": params.channel,
            "thread_ts": params.thread_ts,
            **_text_field(params.format, params.text),
            "reply_broadcast": params.reply_broadcast,
            "unfurl_links": params.unfurl_links,
            "unfurl_media": params.unfurl_media,
        }
        return await _post_and_confirm(wire, heading="Reply sent")
    except Exception as exc:
        return handle_api_error(exc)


@mcp.tool(
    name="slack_update_message",
    annotations=ToolAnnotations(
        title="Edit Your Slack Message",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def slack_update_message(params: UpdateMessageInput) -> str:
    """Edit one of your own Slack messages — the edit is made as you, with Slack's "Sent using @Alejandro AI" line.

    Calls `chat.update` once with your user token: the new `text` replaces the old text
    entirely (and removes any blocks the message had). Only messages you posted can be
    edited — Slack refuses anyone else's with `cant_update_message`. The message stays
    posted as you, with Slack's "Sent using @Alejandro AI" line. `format` works as in
    `slack_post_message` (`mrkdwn` → `text`, `markdown` → `markdown_text`). The text is
    capped at 4,000 characters locally because `chat.update` refuses longer text with
    `msg_too_long` (unlike `chat.postMessage`, which accepts up to 40,000).

    Refused unless `SLACK_ALLOW_WRITES=1` AND the target is allowed by
    `SLACK_WRITE_CHANNELS` / `SLACK_WRITE_USERS` (Slack Connect channels only by explicit
    ID) — the client's write guard checks this before anything is sent. The edit is
    never retried, by the tool or the client.

    When to Use:
    - To fix a typo or update the status in a message you posted (e.g. with `slack_post_message`).

    When NOT to Use:
    - For someone else's message (Slack refuses it), or to add to a conversation: post a
      reply with `slack_reply_in_thread` instead.
    - To move a message into or out of a thread: impossible with `chat.update`.
    - To find the message: `slack_get_history` prints each message's ts (conversation ids
      come from `slack_list_conversations`; mention ids from `slack_resolve_user`).

    Returns:
    A confirmation echoing only what Slack returned: `channel`, `ts`, the stored `text`,
    and any Slack warning — or an `Error ...` string.

    Examples:
    - `params = {"channel": "C0123ABCDEF", "ts": "1712345678.123456", "text": "Deploy done (v2) :rocket:"}`

    Error Handling:
    - `Error: ... SLACK_ALLOW_WRITES ...` / `... SLACK_WRITE_CHANNELS ...` / `... SLACK_WRITE_USERS ...`:
      the write guard refused; the error names the env var that would allow it.
    - `cant_update_message`: not your message (or not editable); `edit_window_closed`: the workspace's
      edit window passed; `message_not_found`: check channel AND ts; `msg_too_long`: over 4,000
      characters; `missing_scope`: needs the `chat:write` user scope.
    """
    try:
        wire: dict[str, Any] = {"channel": params.channel, "ts": params.ts, **_text_field(params.format, params.text)}
        body = await get_client().call("chat.update", wire)
        text = body.get("text")
        lines = [
            "# Message updated",
            "",
            "Slack accepted the edit (ok).",
            "",
            f"- **channel**: {body.get('channel') or 'N/A'}",
            f"- **ts**: {body.get('ts') or 'N/A'} ({ts_to_human(body.get('ts'))})",
            f"- **text** (as Slack stored it):\n{text}" if text else "- **text**: Slack returned none",
        ]
        warnings = fmt_warnings(body)
        if warnings:
            lines.append(warnings)
        return clip_response("\n".join(lines))
    except Exception as exc:
        return handle_api_error(exc)


@mcp.tool(
    name="slack_schedule_message",
    annotations=ToolAnnotations(
        title="Schedule Slack Message (as you)",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
async def slack_schedule_message(params: ScheduleMessageInput) -> str:
    """Schedule a Slack message for later — it will be posted as you, with Slack's "Sent using @Alejandro AI" line.

    Calls `chat.scheduleMessage` once with your user token. At `post_at` Slack posts the
    message under your name and avatar, with Slack's "Sent using @Alejandro AI" line.
    `post_at` is ISO-8601 (no offset = UTC) or epoch seconds, converted to epoch seconds;
    it must be in the future and at most 120 days ahead (checked locally — Slack's limit).
    Slack also refuses more than 30 scheduled messages within any 5-minute window in the
    same channel (`restricted_too_many`). Text and `format` work as in `slack_post_message`.
    There is NO un-schedule tool in v0.1: once scheduled, this server cannot cancel or
    edit it, so confirm the text, channel and time before calling.

    Refused unless `SLACK_ALLOW_WRITES=1` AND the target is allowed by
    `SLACK_WRITE_CHANNELS` / `SLACK_WRITE_USERS` (Slack Connect channels only by explicit
    ID) — the client's write guard checks this before anything is scheduled. The call is
    never retried, by the tool or the client: a retry could schedule it twice.

    When to Use:
    - To send a message at a specific later time (a reminder, an announcement at 9 am).

    When NOT to Use:
    - To send now: use `slack_post_message` / `slack_reply_in_thread`.
    - Channel names → ids: `slack_list_conversations`; DMs → `slack_open_conversation`;
      people → `<@U…>` ids: `slack_resolve_user`.

    Returns:
    A confirmation echoing only what Slack returned: `scheduled_message_id`, `channel`, and
    `post_at` as UTC time (plus epoch), and any Slack warning — or an `Error ...` string.

    Examples:
    - `params = {"channel": "C0123ABCDEF", "post_at": "2026-10-12T14:00:00Z", "text": "Standup in 5 :clock9:"}`
    - `params = {"channel": "C0123ABCDEF", "post_at": "2026-10-12T09:00:00-05:00", "text": "Reminder",
      "thread_ts": "1712345678.123456"}`

    Error Handling:
    - `Error: ... SLACK_ALLOW_WRITES ...` / `... SLACK_WRITE_CHANNELS ...` / `... SLACK_WRITE_USERS ...`:
      the write guard refused; the error names the env var that would allow it.
    - `time_in_past` / `time_too_far` (more than 120 days) / `invalid_time`; `restricted_too_many` (30 per
      channel per 5 minutes); `not_in_channel` (join in Slack first); `channel_not_found`; `is_archived`;
      `msg_too_long`; `missing_scope` (needs the `chat:write` user scope).
    - A timeout or 5xx means the execution status is UNKNOWN — do not schedule it again blindly.
    """
    try:
        wire: dict[str, Any] = {
            "channel": params.channel,
            "post_at": int(params.post_at),
            **_text_field(params.format, params.text),
            "unfurl_links": params.unfurl_links,
            "unfurl_media": params.unfurl_media,
        }
        if params.thread_ts is not None:
            wire["thread_ts"] = params.thread_ts
        body = await get_client().call("chat.scheduleMessage", wire)
        post_at = body.get("post_at")
        lines = [
            "# Message scheduled",
            "",
            "Slack accepted the schedule (ok). It will be posted as you at `post_at`; there is no un-schedule tool "
            "in v0.1, so this server cannot cancel it.",
            "",
            f"- **scheduled_message_id**: {body.get('scheduled_message_id') or 'N/A'}",
            f"- **channel**: {body.get('channel') or 'N/A'}",
            f"- **post_at**: {ts_to_human(post_at)} (epoch {post_at})" if post_at else "- **post_at**: N/A",
        ]
        warnings = fmt_warnings(body)
        if warnings:
            lines.append(warnings)
        return clip_response("\n".join(lines))
    except Exception as exc:
        return handle_api_error(exc)
