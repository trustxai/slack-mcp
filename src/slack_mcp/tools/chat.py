"""Slack `chat` tools: post, reply in a thread, edit, and schedule messages — AS YOU.

Every tool here writes with the user token, so Slack shows the message under your own
name and avatar. Slack does not label these messages: they look exactly like ones you
typed (measured 2026-10-10 with an internal app, in a self-DM and in a channel).
To let teammates tell them apart, set `SLACK_SIGNATURE`. Every message these tools post,
reply, schedule or edit then goes out as Block Kit `blocks` — the text, then the
signature in a `context` block, the small grey font of Slack's own labels — plus a `text`
fallback (for notifications, search and read-back) that ends with the signature after a
blank line, never twice (`_sign`, `_build_message`). Without a signature the tools send
plain `text` / `markdown_text`. The tools
never gate, never retry and never pick an encoding: `client.call()` runs the write guard
on the exact parameters that go on the wire (kill-switch, ID-only channel allowlist, DM
membership, Slack Connect refusal), sends `chat.postMessage` / `chat.update` /
`chat.scheduleMessage` as JSON, and refuses to retry a write.

Two facts from research 02 shape the inputs: `chat.postMessage` truncates `text` over
40,000 characters and only says so with a `message_truncated` warning (so every
confirmation renders `fmt_warnings`), and `chat.update` errors with `msg_too_long` past
4,000 characters (so the update tool refuses that locally). `format="markdown"` sends
the same string as `markdown_text` — real Markdown, at most 12,000 characters, never
combined with `text` or `blocks` — or, when signed, as one `markdown` block. Every cap
applies to the FINAL `text`, signature included.
"""

from __future__ import annotations

import html
import re
import time
from typing import Any, Literal, Self

import httpx
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from slack_mcp.client import SlackClient, get_client
from slack_mcp.config import Settings, get_settings
from slack_mcp.errors import SlackApiError, handle_api_error, hint_for
from slack_mcp.formatters import clip_response, fmt_warnings, ts_to_human
from slack_mcp.server import mcp
from slack_mcp.validators import channel_id, iso_to_epoch, message_ts

# `mrkdwn` sends `text` (Slack's own syntax); `markdown` sends `markdown_text` (real Markdown).
MessageFormat = Literal["mrkdwn", "markdown"]

# Research 02 §4.13–§4.15.
# postMessage truncates past this; scheduleMessage reuses the cap — its msg_too_long
# threshold is undocumented (02 O-2, measured in t6).
POST_TEXT_MAX = 40_000
MARKDOWN_TEXT_MAX = 12_000  # markdown_text: "Limit this field to 12,000 characters"
UPDATE_TEXT_MAX = 4_000  # chat.update: msg_too_long past 4,000 characters
SCHEDULE_MAX_DAYS = 120  # chat.scheduleMessage: "up to 120 days into the future"
SCHEDULE_MAX_SECONDS = SCHEDULE_MAX_DAYS * 86_400

# Block Kit, for a signed message (docs.slack.dev/reference/block-kit, read 2026-10-10).
SECTION_TEXT_MAX = 3_000  # section block: "maximum length is 3000 characters" (its text)
CONTEXT_TEXT_MAX = 3_000  # a context element is a text object: "maximum length is 3000 characters"
MARKDOWN_BLOCK_MAX = 12_000  # "cumulative limit for all `markdown` blocks in a single payload is 12,000"
MESSAGE_BLOCKS_MAX = 50  # "You can include up to 50 blocks in each message"

# Slack codes whose errors.py hint is the "outcome UNKNOWN — read back before retrying"
# wording for writes; a failed permalink lookup under "SENT" renders them by code only.
_UNKNOWN_OUTCOME_CODES = frozenset(
    {"request_timeout", "internal_error", "fatal_error", "service_unavailable", "non_json_response"}
)

_CHANNEL_DESCRIPTION = (
    "Conversation ID to post in: C… (channel), D… (DM) or G… (group DM / private channel). Never a #name or a "
    "U… user id — find a channel id with slack_list_conversations, and open a DM with slack_open_conversation."
)
_FORMAT_DESCRIPTION = (
    "How Slack reads `text`. 'mrkdwn' (default) = Slack's own syntax: *bold* (single asterisks), _italic_, "
    "~strike~, `code`, <@U0123ABCDEF> mentions, <https://example.com|label> links, no headers. 'markdown' = real "
    "Markdown (**bold**, # headings, [label](url)), sent as markdown_text (as one markdown block when "
    "SLACK_SIGNATURE is set), at most 12,000 characters."
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


# -- the signature (SLACK_SIGNATURE) ---------------------------------------------

# A mrkdwn link `<https://…|label>` (or mailto:). Mentions (`<@U…>`), channel links
# (`<#C…|name>`) and specials (`<!here>`) do not match: they are not URLs.
_MRKDWN_LINK = re.compile(r"<((?:https?|mailto):[^|<>\s]+)\|([^<>]+)>")


def _markdown_label(label: str) -> str:
    return label.replace("[", "\\[").replace("]", "\\]")


def _signature_for(fmt: MessageFormat, signature: str) -> str:
    """The signature as it is written for `fmt`.

    `SLACK_SIGNATURE` is mrkdwn; `markdown_text` is standard Markdown, where `<url|label>`
    would render literally, so for `format="markdown"` each such link becomes
    `[label](url)`. A bare `<url>` is already a Markdown autolink and stays as it is.
    """
    if fmt != "markdown":
        return signature
    return _MRKDWN_LINK.sub(lambda m: f"[{_markdown_label(m.group(2))}]({m.group(1)})", signature)


# What goes between the text and the signature: a BLANK line, in both formats. In mrkdwn
# that keeps the signature visibly apart from the message (a single newline glued it to
# the last line). In Markdown it is also what keeps it out of the block above: a single
# newline after a list item, a `>` quote or a table row continues that block.
_SIGNATURE_SEPARATOR = "\n\n"


def _comparable(line: str) -> str:
    """A line as compared with the signature: entities decoded, edges trimmed.

    Slack stores `&`, `<` and `>` of plain text as `&amp;` / `&lt;` / `&gt;`, so a signature
    with an `&` (in its text or in a link's query string) reads back in its entity form.
    """
    return html.unescape(line).strip()


def _is_signature_line(line: str, signature: str) -> bool:
    """True when `line` is the signature in EITHER form — mrkdwn `<url|label>` or markdown `[label](url)`.

    Either form counts whatever the current `format`, so switching format on an edit never
    leaves two signature lines.
    """
    candidate = _comparable(line)
    return candidate in {_comparable(signature), _comparable(_signature_for("markdown", signature))}


def _unsigned(text: str, signature: str) -> tuple[str, bool]:
    """`(text without its trailing signature line, had one?)`.

    "Trailing signature line" means the LAST line, trailing whitespace ignored, is the
    signature in either form (`_is_signature_line`, entities decoded); it is cut together
    with the blank lines before it. Otherwise the text comes back right-trimmed. A
    signature quoted in the middle of the text is not a trailing line and stays.
    """
    body = text.rstrip()
    head, _, last = body.rpartition("\n")
    if _is_signature_line(last, signature):
        return head.rstrip(), True
    return body, False


def _sign(text: str, settings: Settings, fmt: MessageFormat = "mrkdwn") -> tuple[str, bool]:
    """`(text to send, appended?)` — the text plus the signature line, unless it already ends with it.

    Appends `"\\n\\n" + signature` — after a blank line — in both formats. "Already ends
    with it" means the LAST line, trailing whitespace ignored, is the signature in either
    form (`_unsigned`), whatever number of blank lines precedes it: that line is then
    rewritten as the signature for `fmt`, behind exactly one blank line, so an edit of a
    signed message — even from Slack's read-back text, or in the other format — keeps
    exactly one signature, always standing apart from the text (and in markdown never
    inside the list item, quote or table row above it). A signature quoted in the middle
    of the text does not count: the text is signed again at the end. No signature
    configured → the text unchanged. This is the `text` that goes on the wire (the
    fallback, when `_build_message` adds the blocks).
    """
    if settings.signature is None:
        return text, False
    signature = _signature_for(fmt, settings.signature)
    body, had_signature = _unsigned(text, settings.signature)
    if had_signature:
        return (f"{body}{_SIGNATURE_SEPARATOR}{signature}" if body else signature), False
    return f"{body}{_SIGNATURE_SEPARATOR}{signature}", True


class SignedTextTooLongError(RuntimeError):
    """The text plus its signature exceeds the cap for the method / format; nothing is sent."""


class BlockLimitError(RuntimeError):
    """The signed message does not fit Slack's Block Kit limits; nothing is sent."""


def _section_chunks(body: str, size: int = SECTION_TEXT_MAX) -> list[str]:
    """`body` cut at line boundaries into pieces of at most `size` characters, in order.

    Lines are packed greedily; a single line longer than `size` is hard-split into
    `size`-character pieces. The newline at a cut is dropped (each piece is its own
    section block), as are blank lines at a piece's edges and pieces with nothing but
    whitespace — a section's text must hold at least one character.
    """
    pieces: list[str] = []
    current: str | None = None
    for raw_line in body.split("\n"):
        line = raw_line
        while len(line) > size:
            if current is not None:
                pieces.append(current)
                current = None
            pieces.append(line[:size])
            line = line[size:]
        if current is None:
            current = line
        elif len(current) + 1 + len(line) <= size:
            current = f"{current}\n{line}"
        else:
            pieces.append(current)
            current = line
    if current is not None:
        pieces.append(current)
    return [piece.strip("\n") for piece in pieces if piece.strip()]


def _build_message(text: str, fmt: MessageFormat, settings: Settings) -> dict[str, Any]:
    """The message fields of the wire params for the FINAL `text` (as `_sign` returned it).

    No signature configured → `{"text": …}` or `{"markdown_text": …}`, exactly as before.
    With one → `{"text": text, "blocks": [...]}`: the text minus its trailing signature line
    (`_unsigned`, so the signature is never inside a body block) as one or more `section`
    blocks of mrkdwn (≤ 3,000 characters each, cut at line boundaries) — or, for
    `format="markdown"`, as ONE `markdown` block (≤ 12,000) with no `markdown_text`, which
    Slack refuses next to `blocks` — then a `context` block holding the signature as
    mrkdwn (the form `SLACK_SIGNATURE` is written in). `text` stays the whole signed text:
    Slack shows it in notifications and search and returns it on read-back.

    Raises `BlockLimitError` before any call when the signature is longer than a context
    text may be (3,000) or the body needs more than 49 blocks (50 per message, the
    context block included).
    """
    signature = settings.signature
    if signature is None:
        return _text_field(fmt, text)
    if len(signature) > CONTEXT_TEXT_MAX:
        raise BlockLimitError(
            f"SLACK_SIGNATURE is {len(signature):,} characters, but it is sent as the text of a Block Kit context "
            f"block, which Slack limits to {CONTEXT_TEXT_MAX:,} characters — shorten SLACK_SIGNATURE. Nothing was sent."
        )
    body, _ = _unsigned(text, signature)
    body_blocks: list[dict[str, Any]]
    if fmt == "markdown":
        if len(body) > MARKDOWN_BLOCK_MAX:
            raise BlockLimitError(
                f"text is {len(body):,} characters, but format='markdown' sends it as a markdown block, which Slack "
                f"limits to {MARKDOWN_BLOCK_MAX:,} characters — shorten it or split it into several messages. Nothing "
                "was sent."
            )
        body_blocks = [{"type": "markdown", "text": body}] if body else []
    else:
        body_blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": chunk}} for chunk in _section_chunks(body)
        ]
    if len(body_blocks) + 1 > MESSAGE_BLOCKS_MAX:
        raise BlockLimitError(
            f"the text needs {len(body_blocks)} section blocks of at most {SECTION_TEXT_MAX:,} characters, plus the "
            f"signature's context block; Slack allows at most {MESSAGE_BLOCKS_MAX} blocks per message — shorten the "
            "text or split it into several messages. Nothing was sent."
        )
    context = {"type": "context", "elements": [{"type": "mrkdwn", "text": signature}]}
    return {"text": text, "blocks": [*body_blocks, context]}


def _prepare_message(text: str, fmt: MessageFormat, limit: int, why: str) -> tuple[dict[str, Any], str]:
    """`(message fields for the wire params, confirmation line)`.

    Signs the text, checks `limit` on the FINAL text, then builds the fields with
    `_build_message`. The input models already cap the text as typed; this catches the
    case where the signature pushes it over. Raised before any Slack call, so nothing
    goes out.
    """
    settings = get_settings()
    final, appended = _sign(text, settings, fmt)
    if len(final) > limit:
        signature = _signature_for(fmt, settings.signature or "")
        # `final != text` also covers an existing signature line that was rewritten (its
        # blank line, or the other format's form), which can add characters.
        included = f" including the {len(signature):,}-character signature (SLACK_SIGNATURE)" if final != text else ""
        over = len(final) - limit
        raise SignedTextTooLongError(
            f"text is {len(final):,} characters{included}; the limit is {limit:,}{why} — shorten the text by at "
            f"least {over:,} character{'' if over == 1 else 's'}. Nothing was sent."
        )
    fields = _build_message(final, fmt, settings)
    if appended:
        line = "- **signature**: appended (context block)"
    elif settings.signature is not None:
        line = "- **signature**: already the last line of the text — not added again (context block)"
    else:
        line = "- **signature**: not configured"
    return fields, line


def _post_limit(fmt: MessageFormat) -> tuple[int, str]:
    """`(cap, why)` for chat.postMessage / chat.scheduleMessage text in `fmt`."""
    if fmt == "markdown":
        return MARKDOWN_TEXT_MAX, " for format='markdown' (Slack's markdown_text limit)"
    return POST_TEXT_MAX, " (Slack truncates longer text)"


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
            "40,000 (and at most 12,000 with format='markdown'). If SLACK_SIGNATURE is set it is shown under the text "
            "(a context block) and appended as the last line of the text fallback, and counts toward these limits."
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
        description=(
            "The reply, sent verbatim (Slack truncates past 40,000; at most 12,000 with format='markdown'). If "
            "SLACK_SIGNATURE is set it is shown under the text (a context block) and appended as the last line of the "
            "text fallback, and counts toward these limits."
        ),
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
        description=(
            "The new text, replacing the old one entirely. At most 4,000 characters (chat.update's limit). If "
            "SLACK_SIGNATURE is set it stays under the text (a context block) and the last line of the text fallback "
            "(appended unless this text already ends with it), and counts toward the limit."
        ),
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
        description=(
            "The message, sent verbatim (at most 12,000 characters with format='markdown'). If SLACK_SIGNATURE is "
            "set it is shown under the text (a context block) and appended as the last line of the text fallback, "
            "and counts toward the limits."
        ),
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


def _lookup_failure(exc: Exception) -> str:
    """Why chat.getPermalink failed, by cause only — `(code) — hint` or `(ExceptionType)`.

    Deliberately NOT `handle_api_error`: its timeout / 5xx wording ("execution status is
    UNKNOWN … before retrying") is about a write, and printed under "SENT" it could push
    an LLM to post the message again.
    """
    if isinstance(exc, SlackApiError):
        hint = "" if exc.code in _UNKNOWN_OUTCOME_CODES else hint_for(exc.code, exc.method)
        return f"(`{exc.code}`)" + (f" — {hint.rstrip('.')}" if hint else "")
    if isinstance(exc, httpx.HTTPStatusError):
        return f"({type(exc).__name__} {exc.response.status_code})"
    return f"({type(exc).__name__})"


async def _permalink_line(client: SlackClient, channel: str, ts: str) -> str:
    """The permalink bullet. A lookup failure is reported here, never as a failed post."""
    if not channel or not ts:
        return "- **permalink**: not looked up — Slack's answer did not include the channel and ts"
    try:
        body = await client.call("chat.getPermalink", {"channel": channel, "message_ts": ts})
    except Exception as exc:
        return (
            f"- **permalink**: lookup failed {_lookup_failure(exc)}. The message itself WAS sent; do not post it again."
        )
    link = body.get("permalink")
    return f"- **permalink**: {link}" if link else "- **permalink**: Slack returned none"


async def _post_and_confirm(wire: dict[str, Any], *, heading: str, signature_line: str) -> str:
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
    lines.append(signature_line)
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
    """Post a message to a Slack channel, DM or group DM as you — Slack adds no label; SLACK_SIGNATURE signs it if set.

    Calls `chat.postMessage` once with your user token, so the message shows your name
    and avatar — there is no bot identity. Slack does not label these messages: they look
    exactly like ones you typed. To let teammates tell them apart, set `SLACK_SIGNATURE` —
    the message then goes out as Block Kit `blocks`: the text, then the signature in a
    `context` block (the small grey font of Slack's own labels), plus a `text` fallback for
    notifications and search that ends with the signature after a blank line (not again if
    the text already ends with it; it counts toward the length limits below). Without a
    signature the tool sends plain `text` / `markdown_text`. The text goes out verbatim. With `format="mrkdwn"` (default) it is sent as `text` in
    Slack's own syntax: `*bold*` (single asterisks), `_italic_`, `` `code` ``,
    `<@U0123ABCDEF>` to mention someone (an id, never a name), `<https://x.y|label>` for a
    link, and no headers (signed: in `section` blocks of at most 3,000 characters, cut at
    line boundaries). With `format="markdown"` the same string is sent as `markdown_text`
    (real Markdown, at most 12,000 characters) instead of `text` — signed, as one
    `markdown` block and no `markdown_text`. After a successful post the tool looks up the
    message permalink with `chat.getPermalink`.

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
    40,000 characters — the message was still posted) — plus a `signature` line saying
    whether SLACK_SIGNATURE was appended. If the permalink lookup fails the message is
    still reported as SENT, with the lookup error. On failure, an `Error ...` string;
    nothing was posted unless it says the status is UNKNOWN.

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
    - `Error: text is N characters including the …-character signature …`: the signature pushed the
      text over the limit; nothing was sent — shorten the text.
    - `Error: … Slack allows at most 50 blocks per message …` / `Error: SLACK_SIGNATURE is N characters …
      context block …`: the signed message does not fit Block Kit's limits; nothing was sent.
      `invalid_blocks` / `msg_blocks_too_long` are Slack refusing the blocks.
    - A timeout or 5xx means the execution status is UNKNOWN: read the conversation back with
      `slack_get_history` before posting again.
    """
    try:
        message, signature_line = _prepare_message(params.text, params.format, *_post_limit(params.format))
        wire: dict[str, Any] = {
            "channel": params.channel,
            **message,
            "unfurl_links": params.unfurl_links,
            "unfurl_media": params.unfurl_media,
        }
        if params.thread_ts is not None:
            wire["thread_ts"] = params.thread_ts
        return await _post_and_confirm(wire, heading="Message sent", signature_line=signature_line)
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
    """Reply inside a Slack thread as you — Slack adds no label; SLACK_SIGNATURE signs it if set.

    Calls `chat.postMessage` once with `thread_ts` set to the thread's PARENT message, so
    the reply lands in that thread under your name and avatar. Slack does not label these
    messages: they look exactly like ones you typed. To let teammates tell them apart, set
    `SLACK_SIGNATURE` — the reply then shows it under the text in a `context` block, and
    the `text` fallback ends with it (not again if the text already ends with it; it counts
    toward the length limits).
    `reply_broadcast=true` also shows it in the channel. Text and
    `format` work exactly as in `slack_post_message` (`mrkdwn` → `text` with `*bold*`,
    `<@U…>` mentions and `<url|label>` links; `markdown` → `markdown_text`, ≤ 12,000
    characters; signed, both go out as `blocks` + `text`). After a successful post the
    permalink is looked up with `chat.getPermalink`.

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
    thread's parent `thread_ts`, the permalink, and any Slack warning — plus a `signature`
    line saying whether SLACK_SIGNATURE was appended. If the permalink lookup fails the
    reply is still reported as SENT, with the lookup error. On failure, an `Error ...`
    string; nothing was posted unless it says the status is UNKNOWN.

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
    - `Error: text is N characters including the …-character signature …`: the signature pushed the
      text over the limit; nothing was sent — shorten the text.
    - A timeout or 5xx means the execution status is UNKNOWN: read the thread back with
      `slack_get_thread` before replying again.
    """
    try:
        message, signature_line = _prepare_message(params.text, params.format, *_post_limit(params.format))
        wire: dict[str, Any] = {
            "channel": params.channel,
            "thread_ts": params.thread_ts,
            **message,
            "reply_broadcast": params.reply_broadcast,
            "unfurl_links": params.unfurl_links,
            "unfurl_media": params.unfurl_media,
        }
        return await _post_and_confirm(wire, heading="Reply sent", signature_line=signature_line)
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
    """Edit one of your own Slack messages as you — a SLACK_SIGNATURE line, if set, stays exactly once.

    Calls `chat.update` once with your user token: the new `text` replaces the old text
    entirely (and removes any blocks the message had). Only messages you posted can be
    edited — Slack refuses anyone else's with `cant_update_message`. The message stays
    posted as you. Slack does not label these messages: they look exactly like ones you
    typed. To let teammates tell them apart, set `SLACK_SIGNATURE` — the server keeps it
    as the last line of the edited text: appended unless the new text already ends with
    it (in either format's form, or as Slack stored it, e.g. `&amp;`), so an edit of a
    signed message — the text read back with `slack_get_thread` / `slack_get_history`,
    changed — stays signed exactly once. With a signature the edit always resends the
    `blocks` too (the text without its signature line, then the signature's `context`
    block), replacing the old ones; Slack does not show its "edited" mark on an edit made
    with blocks. `format` works as in
    `slack_post_message` (`mrkdwn` → `text`, `markdown` → `markdown_text`). The text
    (signature included) is capped at 4,000 characters locally because `chat.update`
    refuses longer text with `msg_too_long` (unlike `chat.postMessage`, which accepts up
    to 40,000).

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
    and any Slack warning — plus a `signature` line (appended / already the last line /
    not configured) — or an `Error ...` string.

    Examples:
    - `params = {"channel": "C0123ABCDEF", "ts": "1712345678.123456", "text": "Deploy done (v2) :rocket:"}`

    Error Handling:
    - `Error: ... SLACK_ALLOW_WRITES ...` / `... SLACK_WRITE_CHANNELS ...` / `... SLACK_WRITE_USERS ...`:
      the write guard refused; the error names the env var that would allow it.
    - `cant_update_message`: not your message (or not editable); `edit_window_closed`: the workspace's
      edit window passed; `message_not_found`: check channel AND ts; `msg_too_long`: over 4,000
      characters; `missing_scope`: needs the `chat:write` user scope.
    - `Error: text is N characters including the …-character signature …`: the signature pushed the
      text over 4,000; nothing was sent — shorten the text. `Error: SLACK_SIGNATURE is N characters
      … context block …`: the signature itself is too long for a context block; nothing was sent.
    """
    try:
        message, signature_line = _prepare_message(
            params.text, params.format, UPDATE_TEXT_MAX, " (chat.update refuses longer text with msg_too_long)"
        )
        wire: dict[str, Any] = {"channel": params.channel, "ts": params.ts, **message}
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
            signature_line,
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
    """Schedule a Slack message for later, posted as you — Slack adds no label; SLACK_SIGNATURE signs it if set.

    Calls `chat.scheduleMessage` once with your user token. At `post_at` Slack posts the
    message under your name and avatar. Slack does not label these messages: they look
    exactly like ones you typed. To let teammates tell them apart, set `SLACK_SIGNATURE` —
    the scheduled message then carries `blocks` with the signature in a `context` block
    under the text, and the `text` fallback ends with it (not again if the text already
    ends with it; it counts toward the length limits). `post_at` is ISO-8601
    (no offset = UTC) or epoch seconds, converted to epoch seconds; it must be in the
    future and at most 120 days ahead (checked locally — Slack's limit).
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
    `post_at` as UTC time (plus epoch), and any Slack warning — plus a `signature` line
    saying whether SLACK_SIGNATURE was appended — or an `Error ...` string.

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
    - `Error: text is N characters including the …-character signature …`: the signature pushed the
      text over the limit; nothing was scheduled — shorten the text. A Block Kit limit (`Error: …
      50 blocks …`, `… context block …`) also stops it before anything is scheduled.
    - A timeout or 5xx means the execution status is UNKNOWN — do not schedule it again blindly.
    """
    try:
        message, signature_line = _prepare_message(params.text, params.format, *_post_limit(params.format))
        wire: dict[str, Any] = {
            "channel": params.channel,
            "post_at": int(params.post_at),
            **message,
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
            signature_line,
        ]
        warnings = fmt_warnings(body)
        if warnings:
            lines.append(warnings)
        return clip_response("\n".join(lines))
    except Exception as exc:
        return handle_api_error(exc)
