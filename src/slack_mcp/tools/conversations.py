"""Slack `conversations` tools: find, inspect, read and open conversations.

Six tools over the `conversations.*` family (research 02 §4.2–§4.7):

- `slack_list_conversations` — `conversations.list`: turn a channel NAME into the ID every
  other tool needs.
- `slack_get_conversation` — `conversations.info`: one conversation, with its Slack Connect
  flags spelled out (the write guard refuses shared conversations).
- `slack_list_members` — `conversations.members`: member ids only (names come from
  `slack_get_user`; this module never calls `users.info` per id).
- `slack_get_history` — `conversations.history`: messages, rendered oldest first.
- `slack_get_thread` — `conversations.replies`: one thread, parent first.
- `slack_open_conversation` 🔒 — `conversations.open`: start a DM or a group DM. The only
  write here: the CLIENT runs the write guard on it (every id must be in
  `SLACK_WRITE_USERS`), never this module.

Every call goes through `client.call(method, params)` with one plain mapping; the client
form-encodes all six (02 §2), comma-joins the `types` / `users` lists, raises
`SlackApiError` for `ok: false`, and never retries the write. Markdown output is capped at
`MAX_DISPLAY_ROWS` rows per page on top of the API `limit`; JSON mode returns the raw
Slack objects.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Literal, Self

from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from slack_mcp.client import get_client
from slack_mcp.errors import handle_api_error
from slack_mcp.formatters import (
    ResponseFormat,
    clip_response,
    fmt_warnings,
    md_cell,
    paginated_response,
    render_message,
    shared_flags,
    to_json,
    ts_to_human,
)
from slack_mcp.server import mcp
from slack_mcp.validators import channel_id, message_ts, time_to_ts, user_id

# Markdown rows rendered per page, on top of the API `limit` (JSON mode returns them all).
MAX_DISPLAY_ROWS = 50
# Page size bounds for every list in this module (Slack recommends 100–200; 02 §3, O-7, O-8).
DEFAULT_LIMIT = 50
MAX_PAGE_LIMIT = 200
# conversations.open takes 1–8 OTHER people; Slack adds you (a group DM holds ≤ 9; 02 §0.9).
MAX_OPEN_USERS = 8
# Names shown per collapsed run of join/leave rows before "+N more".
_MAX_MEMBERSHIP_NAMES = 10

ConversationType = Literal["public_channel", "private_channel", "mpim", "im"]
ALL_CONVERSATION_TYPES: tuple[ConversationType, ...] = ("public_channel", "private_channel", "mpim", "im")

# Membership-event subtypes collapsed into one line per consecutive run.
_MEMBERSHIP_SUBTYPES: dict[str, str] = {
    "channel_join": "joined",
    "channel_leave": "left",
    "group_join": "joined",
    "group_leave": "left",
}

# `mpdm-ana--bob--alejandro-1` → the handles of everyone in the group DM (you included).
_MPIM_NAME_RE = re.compile(r"^mpdm-(.+)-\d+$")


def _read_annotations(title: str) -> ToolAnnotations:
    """The hints every read tool in this module carries."""
    return ToolAnnotations(
        title=title,
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )


# -- shared input helpers ---------------------------------------------------------------


def _blank_to_none(value: object) -> object:
    """An empty / whitespace-only optional string means "not given"."""
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _limit_field(what: str) -> Any:
    return Field(
        default=DEFAULT_LIMIT,
        ge=1,
        le=MAX_PAGE_LIMIT,
        description=(
            f"How many {what} per page (1–{MAX_PAGE_LIMIT}, default {DEFAULT_LIMIT}). Markdown pages hold at "
            f"most {MAX_DISPLAY_ROWS}, so markdown asks Slack for at most {MAX_DISPLAY_ROWS} and the cursor never "
            "skips a row; JSON sends the full limit."
        ),
    )


def _cursor_field() -> Any:
    return Field(
        default=None,
        description="The `next_cursor` printed by the previous page, passed back verbatim. Omit for the first page.",
    )


def _format_field() -> Any:
    return Field(
        default=ResponseFormat.MARKDOWN,
        description="`markdown` (default, readable) or `json` (the raw Slack objects).",
    )


def _channel_field(description: str) -> Any:
    return Field(description=description)


# -- input models -----------------------------------------------------------------------


class ListConversationsInput(BaseModel):
    """Input for `slack_list_conversations`."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    types: list[ConversationType] = Field(
        default_factory=lambda: list(ALL_CONVERSATION_TYPES),
        min_length=1,
        description=(
            "Which kinds to list: any of `public_channel`, `private_channel`, `mpim` (group DMs), `im` (DMs). "
            "Default: all four."
        ),
    )
    exclude_archived: bool = Field(default=True, description="Leave archived channels out (default true).")
    limit: int = _limit_field("conversations")
    cursor: str | None = _cursor_field()
    response_format: ResponseFormat = _format_field()

    @field_validator("types", mode="before")
    @classmethod
    def _split_types(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @field_validator("types", mode="after")
    @classmethod
    def _dedupe_types(cls, value: list[ConversationType]) -> list[ConversationType]:
        return list(dict.fromkeys(value))

    @field_validator("cursor", mode="before")
    @classmethod
    def _cursor(cls, value: object) -> object:
        return _blank_to_none(value)


class GetConversationInput(BaseModel):
    """Input for `slack_get_conversation`."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    channel: str = _channel_field(
        "Conversation ID (C… channel, D… DM, G… group DM / private channel). Not a #name — find the id with "
        "slack_list_conversations."
    )
    response_format: ResponseFormat = _format_field()

    @field_validator("channel", mode="before")
    @classmethod
    def _channel(cls, value: object) -> str:
        return channel_id(value)


class ListMembersInput(BaseModel):
    """Input for `slack_list_members`."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    channel: str = _channel_field("Conversation ID (C…, D… or G…) whose members to list.")
    limit: int = _limit_field("member ids")
    cursor: str | None = _cursor_field()
    response_format: ResponseFormat = _format_field()

    @field_validator("channel", mode="before")
    @classmethod
    def _channel(cls, value: object) -> str:
        return channel_id(value)

    @field_validator("cursor", mode="before")
    @classmethod
    def _cursor(cls, value: object) -> object:
        return _blank_to_none(value)


class GetHistoryInput(BaseModel):
    """Input for `slack_get_history`."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    channel: str = _channel_field("Conversation ID (C…, D… or G…) to read.")
    oldest: str | None = Field(
        default=None,
        description=(
            "Only messages after this moment: ISO-8601 (`2026-10-09`, `2026-10-09T15:00:00Z`; no offset = UTC), "
            "epoch seconds, or a message ts (`1712345678.123456`). Default: the beginning."
        ),
    )
    latest: str | None = Field(
        default=None,
        description="Only messages before this moment (same formats as `oldest`). Default: now.",
    )
    inclusive: bool | None = Field(
        default=None,
        description="Also include a message whose ts equals `oldest`/`latest` exactly. Slack ignores it without a bound.",
    )
    limit: int = _limit_field("messages")
    cursor: str | None = _cursor_field()
    response_format: ResponseFormat = _format_field()

    @field_validator("channel", mode="before")
    @classmethod
    def _channel(cls, value: object) -> str:
        return channel_id(value)

    @field_validator("oldest", mode="before")
    @classmethod
    def _oldest(cls, value: object) -> str | None:
        return None if _blank_to_none(value) is None else time_to_ts(value, field="oldest")

    @field_validator("latest", mode="before")
    @classmethod
    def _latest(cls, value: object) -> str | None:
        return None if _blank_to_none(value) is None else time_to_ts(value, field="latest")

    @field_validator("cursor", mode="before")
    @classmethod
    def _cursor(cls, value: object) -> object:
        return _blank_to_none(value)

    @model_validator(mode="after")
    def _bounds_in_order(self) -> Self:
        if self.oldest is not None and self.latest is not None and float(self.oldest) > float(self.latest):
            raise ValueError(f"oldest ({self.oldest}) is after latest ({self.latest}) — swap them or drop one")
        return self


class GetThreadInput(BaseModel):
    """Input for `slack_get_thread`."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    channel: str = _channel_field("Conversation ID (C…, D… or G…) the thread lives in.")
    thread_ts: str = Field(
        description=(
            "The thread's parent message ts (`1712345678.123456`) — or the ts of any reply in it. Shown as "
            "`(ts …)` in slack_get_history."
        ),
    )
    limit: int = _limit_field("messages")
    cursor: str | None = _cursor_field()
    response_format: ResponseFormat = _format_field()

    @field_validator("channel", mode="before")
    @classmethod
    def _channel(cls, value: object) -> str:
        return channel_id(value)

    @field_validator("thread_ts", mode="before")
    @classmethod
    def _thread_ts(cls, value: object) -> str:
        return message_ts(value, field="thread_ts")

    @field_validator("cursor", mode="before")
    @classmethod
    def _cursor(cls, value: object) -> object:
        return _blank_to_none(value)


class OpenConversationInput(BaseModel):
    """Input for `slack_open_conversation`."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    users: list[str] = Field(
        min_length=1,
        max_length=MAX_OPEN_USERS,
        description=(
            f"1–{MAX_OPEN_USERS} Slack user ids (U…/W…) of the OTHER people — Slack adds you automatically, so do "
            "not include your own id, except alone (that opens your self-DM). One id opens a DM with that person; "
            "2 or more open a group DM of you + them (at most 9 people). Resolve names with slack_resolve_user first."
        ),
    )

    @field_validator("users", mode="before")
    @classmethod
    def _users(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.split(",")
        if not isinstance(value, list | tuple):
            return value  # pydantic rejects the type with its own message
        ids = list(dict.fromkeys(user_id(item, field="users") for item in value if str(item).strip()))
        if len(ids) > MAX_OPEN_USERS:
            raise ValueError(
                f"users takes at most {MAX_OPEN_USERS} OTHER people (Slack adds you; a group DM holds at most 9), "
                f"got {len(ids)}"
            )
        return ids


# -- rendering helpers ------------------------------------------------------------------


def _objects(value: Any) -> list[dict[str, Any]]:
    """The dict items of a Slack list field (anything else is dropped)."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _next_cursor(body: Mapping[str, Any]) -> str:
    metadata = body.get("response_metadata")
    if isinstance(metadata, Mapping):
        return str(metadata.get("next_cursor") or "")
    return ""


def _text_of(field: Any) -> str:
    """The text under `value` of a `topic` / `purpose` object, on ONE line.

    Anyone in the channel can set these, so newlines are collapsed: a purpose such as
    "x\\n- **Slack Connect: no**" must not print a second, fake bullet.
    """
    if isinstance(field, Mapping):
        return " ".join(str(field.get("value") or "").split())
    return ""


def _page_limit(limit: int, fmt: ResponseFormat) -> int:
    """The `limit` sent to Slack: markdown never asks for more rows than it can show."""
    return limit if fmt is ResponseFormat.JSON else min(limit, MAX_DISPLAY_ROWS)


def _reduced_limit_note(requested: int, sent: int) -> str | None:
    if sent >= requested:
        return None
    return (
        f"Markdown pages hold at most {MAX_DISPLAY_ROWS} rows, so Slack was asked for {sent} (not {requested}) and the "
        f"cursor continues right after them; use response_format=json for pages up to {MAX_PAGE_LIMIT}."
    )


def _mpim_handles(name: Any) -> list[str]:
    match = _MPIM_NAME_RE.match(str(name or ""))
    if not match:
        return []
    return [handle for handle in match.group(1).split("--") if handle]


def _conversation_type(conv: Mapping[str, Any]) -> ConversationType:
    # The id prefix is unreliable for privacy (02 §6) — the flags decide.
    if conv.get("is_im"):
        return "im"
    if conv.get("is_mpim"):
        return "mpim"
    if conv.get("is_private") or conv.get("is_group"):
        return "private_channel"
    return "public_channel"


def _conversation_label(conv: Mapping[str, Any]) -> str:
    """`#general` / `DM with U0123…` / `group DM: @ana, @bob, @you`."""
    if conv.get("is_im"):
        other = conv.get("user")
        label = f"DM with {other}" if other else "DM"
        if conv.get("is_user_deleted"):
            label += " (deactivated user)"
        return label
    if conv.get("is_mpim"):
        handles = _mpim_handles(conv.get("name"))
        if handles:
            return "group DM: " + ", ".join(f"@{handle}" for handle in handles)
        return f"group DM {conv.get('name')}" if conv.get("name") else "group DM"
    return f"#{conv.get('name') or conv.get('name_normalized') or conv.get('id') or '?'}"


def _shared_cell(conv: Mapping[str, Any]) -> str:
    flags = shared_flags(conv)
    if flags:
        return "yes (" + ", ".join(flags) + ")"
    # conversations.list IM entries carry only is_org_shared (02 §6, O-22): no proof either way.
    return "unknown (DM)" if conv.get("is_im") else "no"


def _member_cell(conv: Mapping[str, Any]) -> str:
    if "is_member" in conv:
        return "yes" if conv.get("is_member") else "no"
    return "yes" if conv.get("is_im") or conv.get("is_mpim") else ""


def _conversation_row(conv: Mapping[str, Any]) -> str:
    label = _conversation_label(conv) + (" (archived)" if conv.get("is_archived") else "")
    members = conv.get("num_members")
    cells = [
        md_cell(label),
        md_cell(conv.get("id")),
        _conversation_type(conv),
        md_cell(members if isinstance(members, int) else ""),
        _member_cell(conv),
        md_cell(_shared_cell(conv)),
        md_cell(_text_of(conv.get("topic")), max_len=80),
    ]
    return "| " + " | ".join(cells) + " |"


def _cap_note(hidden: int, what: str) -> str:
    # Only reachable when Slack returns more rows than the (already capped) limit asked for.
    return (
        f"_{hidden} more {what} on this page not shown (markdown cap {MAX_DISPLAY_ROWS}; Slack returned more than "
        "the limit) — use response_format=json; the cursor resumes AFTER this whole page._"
    )


def _membership_line(run: Sequence[Mapping[str, Any]]) -> str:
    """One line for a run of consecutive join/leave rows."""
    start = ts_to_human(run[0].get("ts"))
    end = ts_to_human(run[-1].get("ts"))
    span = start if start == end else f"{start} → {end}"
    events = [
        f"{entry.get('user') or 'someone'} {_MEMBERSHIP_SUBTYPES.get(str(entry.get('subtype')), 'changed')}"
        for entry in run[:_MAX_MEMBERSHIP_NAMES]
    ]
    if len(run) > _MAX_MEMBERSHIP_NAMES:
        events.append(f"+{len(run) - _MAX_MEMBERSHIP_NAMES} more")
    count = len(run)
    return f"[{span}] {count} join/leave event{'' if count == 1 else 's'}: {', '.join(events)}"


def _is_thread_reply(message: Mapping[str, Any]) -> bool:
    thread_ts = message.get("thread_ts")
    return bool(thread_ts) and thread_ts != message.get("ts")


def _message_lines(messages: Sequence[Mapping[str, Any]], *, mark_replies: bool = False) -> list[str]:
    """`render_message` per message, consecutive join/leave rows collapsed to one line."""
    lines: list[str] = []
    run: list[Mapping[str, Any]] = []
    for message in messages:
        if message.get("subtype") in _MEMBERSHIP_SUBTYPES:
            run.append(message)
            continue
        if run:
            lines.append(_membership_line(run))
            run = []
        line = render_message(message)
        if mark_replies and _is_thread_reply(message):
            line = "↳ " + line
        lines.append(line)
    if run:
        lines.append(_membership_line(run))
    return lines


def _message_page(
    *,
    title: str,
    messages: Sequence[Mapping[str, Any]],
    total: int,
    order: str,
    notes: Sequence[str],
    next_cursor: str,
    limit: int,
    next_page: str,
    mark_replies: bool = False,
) -> str:
    """Markdown for a page of messages: header, cursor line (verbatim), one line per message."""
    lines = [f"# {title}", "", f"Showing **{len(messages):,}** message(s), {order} (limit {limit})."]
    lines.extend(notes)
    cursor = next_cursor.strip()
    if cursor:
        lines.append(f"More available — pass `cursor` = `{cursor}` to get the next page ({next_page}).")
    lines.append("")
    if not messages:
        lines.append("_No messages._")
        return clip_response("\n".join(lines))
    lines.extend(_message_lines(messages, mark_replies=mark_replies))
    if total > len(messages):
        lines.extend(["", _cap_note(total - len(messages), "message(s)")])
    return clip_response("\n".join(lines))


# -- tools ------------------------------------------------------------------------------


@mcp.tool(
    name="slack_list_conversations",
    annotations=_read_annotations("List Slack Conversations"),
)
async def slack_list_conversations(params: ListConversationsInput) -> str:
    """List channels, private channels, group DMs and DMs, one page at a time.

    This is how to turn a channel NAME (`#general`) into the conversation ID (`C0123…`)
    every other tool needs. Calls `conversations.list` with your user token, so it shows
    every public channel in the workspace — including ones you are not in (the
    "You're in" column says which) — plus the private channels, group DMs and DMs you
    belong to. Each row shows the name (or who a DM is with), the id, the type, the
    member count, whether you are a member, a `Shared` flag when any of `is_shared` /
    `is_ext_shared` / `is_org_shared` / `is_pending_ext_shared` is set (the write guard
    refuses those), and the topic.

    When to Use:
    - To find the id of a channel you know by name, before reading or posting there.
    - To see which group DMs / DMs you have, and with whom.
    - To check whether a channel is shared outside the workspace (Slack Connect).

    When NOT to Use:
    - To find a PERSON's id (use `slack_resolve_user` / `slack_lookup_user_by_email`).
    - For one conversation's full details (use `slack_get_conversation`).
    - To read messages (use `slack_get_history` / `slack_get_thread`) or to search them
      (use `slack_search_messages`).
    - To start a DM that does not exist yet (use `slack_open_conversation`).

    Returns:
    A markdown table (Conversation | ID | Type | Members | You're in | Shared | Topic),
    at most 50 rows (markdown asks Slack for at most 50), or with `response_format="json"`
    the raw Slack objects (up to `limit`) plus `next_cursor`. A DM row shows `Shared: unknown (DM)`: Slack's list omits the external
    flags on DMs.

    Pagination:
    Cursor-based. When more exist the output prints `next_cursor` verbatim — pass it back
    as `cursor`. Slack applies `exclude_archived` AFTER cutting the page, so a page can
    hold fewer rows than `limit` while more remain: follow the cursor, never compare the
    row count with `limit`.

    Examples:
    - All kinds, first page: `params = {}`
    - Public and private channels only: `params = {"types": ["public_channel", "private_channel"]}`
    - Next page: `params = {"cursor": "dGVhbTpDMDYxRkE1UEI="}`

    Error Handling:
    `missing_scope` names the absent user scope: each type needs its own —
    `channels:read` (public), `groups:read` (private), `mpim:read`, `im:read`.
    `invalid_cursor` means the cursor expired: start again without it. Invalid `types`
    or `limit` are rejected locally before anything is sent.
    """
    try:
        limit = _page_limit(params.limit, params.response_format)
        request: dict[str, Any] = {
            "types": list(params.types),
            "exclude_archived": params.exclude_archived,
            "limit": limit,
        }
        if params.cursor is not None:
            request["cursor"] = params.cursor
        body = await get_client().call("conversations.list", request)
        conversations = _objects(body.get("channels"))
        next_cursor = _next_cursor(body)
        notes = [
            "Types: "
            + ", ".join(params.types)
            + ("; archived excluded." if params.exclude_archived else "; archived included.")
        ]
        if params.response_format is ResponseFormat.JSON:
            return paginated_response(
                items=conversations,
                fmt=params.response_format,
                item_formatter=_conversation_row,
                title="Slack conversations",
                next_cursor=next_cursor,
                limit=limit,
                note=notes[0],
            )
        reduced = _reduced_limit_note(params.limit, limit)
        if reduced:
            notes.append(reduced)
        shown = conversations[:MAX_DISPLAY_ROWS]
        if len(conversations) > len(shown):
            notes.append(_cap_note(len(conversations) - len(shown), "conversation(s)"))
        return paginated_response(
            items=shown,
            fmt=params.response_format,
            item_formatter=_conversation_row,
            title="Slack conversations",
            next_cursor=next_cursor,
            limit=limit,
            note="\n".join(notes),
            table_header=["Conversation", "ID", "Type", "Members", "You're in", "Shared", "Topic"],
        )
    except Exception as exc:
        return handle_api_error(exc)


@mcp.tool(
    name="slack_get_conversation",
    annotations=_read_annotations("Get Slack Conversation"),
)
async def slack_get_conversation(params: GetConversationInput) -> str:
    """Show one conversation's details, including whether it is shared outside the workspace.

    Calls `conversations.info` (with `include_num_members`) and renders the name or who a
    DM is with, the type, archived / member state, the member count, topic, purpose,
    creator and creation time — and the sharing flags explicitly as `Slack Connect:
    yes/no`, because the write guard refuses to post into a shared conversation unless
    its id is listed in `SLACK_WRITE_CHANNELS`.

    When to Use:
    - Before posting somewhere, to check it is not a Slack Connect channel.
    - To read a channel's topic / purpose, or who a DM is with.
    - To confirm an id from `slack_list_conversations` is the conversation you expect.

    When NOT to Use:
    - To find a conversation by name (use `slack_list_conversations`).
    - To list the members (use `slack_list_members`).
    - To read messages (use `slack_get_history`).

    Returns:
    A markdown block with the conversation's fields and the `Slack Connect: yes/no` line
    (topic and purpose on one line each), or with `response_format="json"` the raw Slack
    `channel` object. If Slack's answer has no `channel` object, says so and nothing else.

    Examples:
    - `params = {"channel": "C0123ABCDEF"}`
    - Raw object: `params = {"channel": "D0123ABCDEF", "response_format": "json"}`

    Error Handling:
    `channel_not_found` means a wrong id or no access to it; `channel_is_limited_access`
    means Slack restricts apps there. `missing_scope` names the absent user scope
    (`channels:read`, `groups:read`, `mpim:read` or `im:read`, by conversation type). A
    `#name` instead of an id is rejected locally.
    """
    try:
        body = await get_client().call("conversations.info", {"channel": params.channel, "include_num_members": True})
        raw = body.get("channel")
        if not isinstance(raw, dict) or not raw:
            # Never render type / "Slack Connect: no" from an empty object.
            return clip_response(f"Slack returned no channel object for {params.channel}.")
        conv: dict[str, Any] = raw
        if params.response_format is ResponseFormat.JSON:
            return clip_response(to_json(conv))
        cid = conv.get("id") or params.channel
        kind = _conversation_type(conv)
        flags = shared_flags(conv)
        lines = [f"# {_conversation_label(conv)} ({cid})", "", f"- **type**: {kind}"]
        if flags:
            lines.append(
                f"- **Slack Connect: yes** ({', '.join(flags)}) — shared outside this workspace; the write guard "
                f"refuses writes here unless {cid} is listed in SLACK_WRITE_CHANNELS"
            )
        elif kind == "im":
            lines.append(
                "- **Slack Connect: no** (no sharing flag set — Slack may omit them on DMs, so the write guard checks "
                "a DM's members against SLACK_WRITE_USERS instead)"
            )
        elif kind == "mpim":
            lines.append(
                "- **Slack Connect: no** (no sharing flag set) — the write guard checks a group DM's members against "
                "SLACK_WRITE_USERS"
            )
        else:
            lines.append(
                f"- **Slack Connect: no** (no sharing flag set) — writes need its id ({cid}) in SLACK_WRITE_CHANNELS"
            )
        if conv.get("is_frozen"):
            lines.append("- **frozen**: yes — a disconnected Slack Connect copy, read-only")
        team_ids = conv.get("shared_team_ids")
        if isinstance(team_ids, list) and team_ids:
            lines.append(f"- **shared team ids**: {', '.join(str(team) for team in team_ids)}")
        if kind == "im" and conv.get("user"):
            lines.append(f"- **with**: {conv.get('user')}")
        lines.append(f"- **archived**: {'yes' if conv.get('is_archived') else 'no'}")
        if "is_member" in conv:
            lines.append(f"- **you are a member**: {'yes' if conv.get('is_member') else 'no'}")
        if isinstance(conv.get("num_members"), int):
            lines.append(f"- **members**: {conv['num_members']}")
        topic = _text_of(conv.get("topic"))
        if topic:
            lines.append(f"- **topic**: {topic}")
        purpose = _text_of(conv.get("purpose"))
        if purpose:
            lines.append(f"- **purpose**: {purpose}")
        if conv.get("creator"):
            lines.append(f"- **creator**: {conv.get('creator')}")
        if conv.get("created"):
            lines.append(f"- **created**: {ts_to_human(conv.get('created'))}")
        return clip_response("\n".join(lines))
    except Exception as exc:
        return handle_api_error(exc)


@mcp.tool(
    name="slack_list_members",
    annotations=_read_annotations("List Slack Conversation Members"),
)
async def slack_list_members(params: ListMembersInput) -> str:
    """List the user ids of a conversation's members, one page at a time.

    Calls `conversations.members`, which returns user ids ONLY — no names. This tool
    does not look each id up (one `users.info` per member would burn the rate limit);
    call `slack_get_user` for the people you need. People from other organisations in a
    Slack Connect channel are included.

    When to Use:
    - To check who is in a channel or group DM before posting or mentioning (`<@U…>`).
    - To get the ids to pass to `slack_get_user`.

    When NOT to Use:
    - To find a person by name (use `slack_resolve_user`).
    - For the member COUNT only (use `slack_get_conversation`, which shows it).

    Returns:
    A markdown list of user ids (markdown asks Slack for at most 50 per page), or with
    `response_format="json"` the raw id list (up to `limit`) plus `next_cursor`.

    Pagination:
    Cursor-based: when more exist the output prints `next_cursor` verbatim — pass it back
    as `cursor`.

    Examples:
    - `params = {"channel": "C0123ABCDEF"}`
    - Next page: `params = {"channel": "C0123ABCDEF", "cursor": "dXNlcjpVMEc5V0ZYTlo="}`

    Error Handling:
    `channel_not_found` means a wrong id or no access; `fetch_members_failed` is
    Slack-side (try again later); `invalid_cursor` means start without the cursor.
    `missing_scope` names the absent user scope (`channels:read`, `groups:read`,
    `mpim:read` or `im:read`). A non-id `channel` is rejected locally.
    """
    try:
        limit = _page_limit(params.limit, params.response_format)
        request: dict[str, Any] = {"channel": params.channel, "limit": limit}
        if params.cursor is not None:
            request["cursor"] = params.cursor
        body = await get_client().call("conversations.members", request)
        raw = body.get("members")
        members = [str(member) for member in raw if isinstance(member, str)] if isinstance(raw, list) else []
        next_cursor = _next_cursor(body)
        note = "User ids only — Slack returns no names here. Call slack_get_user with an id for the person's name."
        if params.response_format is ResponseFormat.JSON:
            items = members
        else:
            reduced = _reduced_limit_note(params.limit, limit)
            if reduced:
                note += "\n" + reduced
            items = members[:MAX_DISPLAY_ROWS]
            if len(members) > len(items):
                note += "\n" + _cap_note(len(members) - len(items), "member id(s)")
        return paginated_response(
            items=items,
            fmt=params.response_format,
            item_formatter=lambda uid: f"- {md_cell(uid)}",
            title=f"Members of {params.channel}",
            next_cursor=next_cursor,
            limit=limit,
            note=note,
        )
    except Exception as exc:
        return handle_api_error(exc)


@mcp.tool(
    name="slack_get_history",
    annotations=_read_annotations("Get Slack Conversation History"),
)
async def slack_get_history(params: GetHistoryInput) -> str:
    """Read the messages of a channel, group DM or DM, rendered oldest first.

    Calls `conversations.history`. Each message renders as `[YYYY-MM-DD HH:MM UTC]
    U0123…: text (ts 1712345678.123456)`, the ts being the message id to reply to or
    react to; a message with a thread ends in `· N replies` — read that thread with
    `slack_get_thread` and the same ts. Consecutive join/leave rows collapse into one
    line. Authors are user ids (look one up with `slack_get_user`); text stays as Slack
    stores it, so `<@U…>` is a mention and `<url|label>` a link. With your user token you
    can read any public channel without joining it, plus the private conversations you
    are in. Thread replies are not inlined — only their parent appears here.

    When to Use:
    - To catch up on a channel or DM, or read a time window (`oldest` / `latest`).
    - To find the ts of a message before replying in its thread or reacting to it.

    When NOT to Use:
    - To read a thread's replies (use `slack_get_thread`).
    - To find messages by words, author or date across channels (use `slack_search_messages`).
    - When you only have a channel NAME (resolve it with `slack_list_conversations` first).

    Returns:
    A markdown transcript shown oldest first (markdown asks Slack for at most 50 messages
    per page), or with `response_format="json"` the raw Slack messages (up to `limit`) in
    Slack's own order (newest first) plus `next_cursor`.

    Pagination:
    Slack returns the newest messages first; `next_cursor` (printed verbatim when more
    exist) fetches the next OLDER page. Pass it back as `cursor` with the same bounds.

    Examples:
    - Latest messages: `params = {"channel": "C0123ABCDEF"}`
    - One day: `params = {"channel": "C0123ABCDEF", "oldest": "2026-10-08", "latest": "2026-10-09"}`
    - Since a message, including it: `params = {"channel": "C0123ABCDEF", "oldest": "1712345678.123456", "inclusive": true}`

    Error Handling:
    `not_in_channel` means a private conversation you are not in; `channel_not_found` a
    wrong id or no access; `invalid_ts_oldest` / `invalid_ts_latest` a bad bound;
    `invalid_cursor` an expired cursor. `missing_scope` names the absent user scope
    (`channels:history`, `groups:history`, `mpim:history` or `im:history`). Unparseable
    bounds, or `oldest` after `latest`, are rejected locally.
    """
    try:
        limit = _page_limit(params.limit, params.response_format)
        request: dict[str, Any] = {"channel": params.channel, "limit": limit}
        if params.oldest is not None:
            request["oldest"] = params.oldest
        if params.latest is not None:
            request["latest"] = params.latest
        if params.inclusive is not None:
            request["inclusive"] = params.inclusive
        if params.cursor is not None:
            request["cursor"] = params.cursor
        body = await get_client().call("conversations.history", request)
        messages = _objects(body.get("messages"))
        next_cursor = _next_cursor(body)
        title = f"History of {params.channel}"
        if params.response_format is ResponseFormat.JSON:
            return paginated_response(
                items=messages,
                fmt=params.response_format,
                item_formatter=render_message,
                title=title,
                next_cursor=next_cursor,
                limit=limit,
                note="Messages in Slack's order: newest first.",
            )
        # Slack sends newest first: keep the newest page rows, then read them chronologically.
        shown = list(reversed(messages[:MAX_DISPLAY_ROWS]))
        notes = ["Messages ending in `· N replies` have a thread: read it with slack_get_thread and that ts."]
        reduced = _reduced_limit_note(params.limit, limit)
        if reduced:
            notes.append(reduced)
        return _message_page(
            title=title,
            messages=shown,
            total=len(messages),
            order="oldest first",
            notes=notes,
            next_cursor=next_cursor,
            limit=limit,
            next_page="older messages",
        )
    except Exception as exc:
        return handle_api_error(exc)


@mcp.tool(
    name="slack_get_thread",
    annotations=_read_annotations("Get Slack Thread"),
)
async def slack_get_thread(params: GetThreadInput) -> str:
    """Read one thread: the parent message first, then its replies oldest first.

    Calls `conversations.replies` with the thread's ts (the parent's — or any reply's;
    Slack resolves the thread either way). Messages render like `slack_get_history`, with
    replies marked `↳`, consecutive join/leave rows collapsed, and the parent's ts called
    out so a reply can target it. `limit` is always sent: Slack's own default is 1,000.

    When to Use:
    - After `slack_get_history` shows a message with `· N replies`.
    - Before answering in a thread, to read what was already said.

    When NOT to Use:
    - To read a whole channel (use `slack_get_history`).
    - To find a thread by words (use `slack_search_messages`).
    - To reply (use `slack_reply_in_thread` with the parent ts).
    - When you only have a channel NAME (resolve it with `slack_list_conversations` first).

    Returns:
    A markdown transcript (markdown asks Slack for at most 50 messages per page), or with
    `response_format="json"` the raw Slack messages (up to `limit`) plus `next_cursor`. A
    message with no replies comes back alone.

    Pagination:
    Long threads page with `next_cursor` (printed verbatim when more exist): pass it back
    as `cursor` with the same `channel` and `thread_ts` for the later replies.

    Examples:
    - `params = {"channel": "C0123ABCDEF", "thread_ts": "1712345678.123456"}`
    - Next page: `params = {"channel": "C0123ABCDEF", "thread_ts": "1712345678.123456", "cursor": "bWVzc2FnZToxNzEy"}`

    Error Handling:
    `thread_not_found` means no message with that ts in that conversation (Slack also
    answers it for join/leave rows); `channel_not_found` a wrong id or no access.
    `missing_scope` names the absent user scope (`channels:history`, `groups:history`,
    `mpim:history` or `im:history`). A malformed `thread_ts` is rejected locally.
    """
    try:
        limit = _page_limit(params.limit, params.response_format)
        request: dict[str, Any] = {"channel": params.channel, "ts": params.thread_ts, "limit": limit}
        if params.cursor is not None:
            request["cursor"] = params.cursor
        body = await get_client().call("conversations.replies", request)
        messages = _objects(body.get("messages"))
        next_cursor = _next_cursor(body)
        title = f"Thread {params.thread_ts} in {params.channel}"
        if params.response_format is ResponseFormat.JSON:
            return paginated_response(
                items=messages,
                fmt=params.response_format,
                item_formatter=render_message,
                title=title,
                next_cursor=next_cursor,
                limit=limit,
                note="Messages in Slack's order: parent first, then replies oldest first.",
            )
        # Parent first (stable): Slack already sends it first; this only guarantees it.
        ordered = sorted(messages, key=_is_thread_reply)
        shown = ordered[:MAX_DISPLAY_ROWS]
        parent = next((message for message in shown if not _is_thread_reply(message)), None)
        notes = []
        if parent is not None and parent.get("ts"):
            notes.append(f"Parent first, replies marked ↳. To reply in this thread use thread_ts = {parent['ts']}.")
        else:
            notes.append("Replies marked ↳ (the parent message is not on this page).")
        reduced = _reduced_limit_note(params.limit, limit)
        if reduced:
            notes.append(reduced)
        return _message_page(
            title=title,
            messages=shown,
            total=len(messages),
            order="parent first, then replies oldest first",
            notes=notes,
            next_cursor=next_cursor,
            limit=limit,
            next_page="later replies",
            mark_replies=True,
        )
    except Exception as exc:
        return handle_api_error(exc)


@mcp.tool(
    name="slack_open_conversation",
    annotations=ToolAnnotations(
        title="Open Slack DM or Group DM",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
async def slack_open_conversation(params: OpenConversationInput) -> str:
    """Open (or find) a DM or group DM with 1–8 people and return its conversation id. 🔒

    This is how to start a DM or a group DM. Calls `conversations.open` with the user ids
    (and `return_im`); Slack adds YOU automatically, so one id gives a DM with that
    person and 2–8 ids give a group DM of you + them (at most 9 people). Do not include
    your own id, except alone: `users = [<your id>]` opens your self-DM (your id is in
    `slack_health_check`). If that conversation already exists Slack returns it
    (`already_open`) instead of making a new one. Opening sends no message: the next step
    is `slack_post_message` to the returned `D…` / `G…` id — never post to a `U…` user id
    directly.

    Refused unless `SLACK_ALLOW_WRITES=1` AND the target is allowed by
    `SLACK_WRITE_CHANNELS` / `SLACK_WRITE_USERS` (Slack Connect channels only by explicit
    ID): here every id in `users` must be listed in `SLACK_WRITE_USERS` (your own id
    always passes). The client's write guard checks that before anything is sent. It runs
    AS YOU — the conversation is opened under your account, and a message you then post
    there goes out as you, with Slack's "Sent using @Alejandro AI" line. Never retried
    automatically.

    When to Use:
    - To message someone (or a small group) privately: open first, then post to the id.
    - To get the `D…` id of your DM with someone, e.g. to read it with `slack_get_history`.

    When NOT to Use:
    - To post in a channel (use `slack_post_message` with the channel id from
      `slack_list_conversations`).
    - With names or @handles (resolve each with `slack_resolve_user` /
      `slack_lookup_user_by_email` first — only `U…`/`W…` ids are accepted).
    - With 9 or more other people (create a channel in Slack instead).

    Returns:
    A confirmation echoing only what Slack returned: the conversation id, whether it was
    `already_open` (and `no_op`) when Slack says so, and who a DM is with when Slack
    names them — or an `Error ...` string.

    Examples:
    - DM: `params = {"users": ["U0123ABCDEF"]}`
    - Group DM of you + two people: `params = {"users": ["U0123ABCDEF", "U0456GHIJKL"]}`

    Error Handling:
    `Error: … SLACK_ALLOW_WRITES …` / `Error: … SLACK_WRITE_USERS …` come from the guard
    (nothing was sent). `user_not_found` means an unknown id; `user_disabled` /
    `user_not_visible` a deactivated or hidden account; `too_many_users` more than 8 ids;
    `not_enough_users` an id count Slack would not accept; `invalid_user_combination` an
    external person who shares no channel with you; `cannot_dm_bot` a bot id.
    `missing_scope` names the absent user scope (`im:write` for a DM, `mpim:write` for a
    group DM). More than 8 ids, or anything that is not a `U…`/`W…` id, is rejected
    locally.
    """
    try:
        users = list(params.users)
        body = await get_client().call("conversations.open", {"users": users, "return_im": True})
        raw = body.get("channel")
        conv: dict[str, Any] = raw if isinstance(raw, dict) else {}
        cid = conv.get("id")
        # Neutral: the ids as sent. Whether that makes a DM, a group DM or your self-DM is Slack's answer.
        requested = f"users {', '.join(users)}"
        if not cid:
            return clip_response(
                f"Slack answered ok to conversations.open for {requested} but returned no conversation id — "
                "do not post; check with slack_list_conversations."
            )
        lines = ["# Conversation opened", "", f"- **conversation id**: {cid}", f"- **requested**: {requested}"]
        if conv.get("user"):
            lines.append(f"- **with** (per Slack): {conv.get('user')}")
        already_open = body.get("already_open", conv.get("already_open"))
        if already_open is True:
            lines.append("- **already_open**: true — it was already open; Slack returned the existing conversation")
        elif already_open is False:
            lines.append("- **already_open**: false — Slack reports it was not already open for you")
        else:
            lines.append("- **already_open**: not reported by Slack")
        if "no_op" in body:
            lines.append(f"- **no_op**: {'true' if body.get('no_op') else 'false'}")
        warnings = fmt_warnings(body)
        if warnings:
            lines.append(f"- {warnings}")
        lines.append(
            f"- **next step**: post with slack_post_message(channel={cid!r}, …) — never to a U… id. That message goes "
            'out as you, with Slack\'s "Sent using @Alejandro AI" line.'
        )
        return clip_response("\n".join(lines))
    except Exception as exc:
        return handle_api_error(exc)
