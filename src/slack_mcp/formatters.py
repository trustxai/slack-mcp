"""Response formatting shared by every tool (markdown / JSON dual output).

Everything a tool returns passes through `to_json` or `clip_response`, and both scrub
credentials (`errors.redact_credentials`): message text written by other people can carry
a pasted `xoxb-…` / `xoxp-…` string, and a successful read must not leak it any more than
an error may.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from slack_mcp.errors import hint_for, redact_credentials

# Context-window guard: MCP responses cap at 1 MB; stay well under it.
MAX_RESPONSE_BYTES = 900_000

# Prefix for the 2nd+ lines of a multi-line message in `render_message`.
MESSAGE_CONTINUATION = "  │ "

# conversations.info flags that mean "people outside this workspace can read it".
SHARED_FLAGS: tuple[str, ...] = ("is_ext_shared", "is_shared", "is_org_shared", "is_pending_ext_shared")


class ResponseFormat(StrEnum):
    """Output format selector present on read tools."""

    MARKDOWN = "markdown"
    JSON = "json"


def to_json(data: Any) -> str:
    """Serialize any payload for the LLM (stable, human-readable), credentials redacted."""
    return redact_credentials(json.dumps(data, indent=2, default=str))


def ts_to_human(value: Any) -> str:
    """Render a Slack `ts` (`1712345678.123456`) or epoch seconds as `YYYY-MM-DD HH:MM UTC`."""
    if value in (None, ""):
        return "N/A"
    text = str(value).strip()
    try:
        seconds = float(text)
    except ValueError:
        return text
    try:
        return datetime.fromtimestamp(seconds, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")
    except (OverflowError, OSError, ValueError):
        return text


def fmt_user(user: Mapping[str, Any] | None, *, fallback_id: Any = None) -> str:
    """Render a user object as `Ada Lovelace (@ada, U0123ABCDEF)` so an LLM can chain the id."""
    if not user:
        return f"user {fallback_id}" if fallback_id is not None else "unknown user"
    raw_profile = user.get("profile")
    profile: Mapping[str, Any] = raw_profile if isinstance(raw_profile, Mapping) else {}
    uid = user.get("id", fallback_id)
    real = user.get("real_name") or profile.get("real_name") or profile.get("display_name") or user.get("name")
    handle = profile.get("display_name") or user.get("name")
    parts: list[str] = []
    if handle:
        parts.append(f"@{handle}")
    if uid is not None:
        parts.append(str(uid))
    tail = f" ({', '.join(parts)})" if parts else ""
    return f"{real or 'user'}{tail}"


def fmt_channel(channel: Mapping[str, Any] | None, *, fallback_id: Any = None) -> str:
    """Render a conversation as `#general (C0123)` / `DM with U0123 (D0123)` / `group DM (G0123)`."""
    if not channel:
        return f"conversation {fallback_id}" if fallback_id is not None else "unknown conversation"
    cid = channel.get("id", fallback_id)
    if channel.get("is_im"):
        label = f"DM with {channel.get('user')}" if channel.get("user") else "DM"
    elif channel.get("is_mpim"):
        label = "group DM"
    else:
        label = f"#{channel.get('name') or channel.get('name_normalized') or cid}"
    return f"{label} ({cid})" if cid is not None else label


def shared_flags(channel: Mapping[str, Any] | None) -> list[str]:
    """The names of the sharing flags set on a conversation (empty = internal to the workspace).

    Any truthy value counts, not only a real `true`: the write guard uses this same
    predicate, and when in doubt a conversation is treated as shared.
    """
    if not channel:
        return []
    return [flag for flag in SHARED_FLAGS if channel.get(flag)]


def render_mention(user_id: str) -> str:
    """The mrkdwn that mentions a user in outgoing text: `<@U0123ABCDEF>`."""
    return f"<@{user_id}>"


def mrkdwn_escape(text: str) -> str:
    """Escape the three characters Slack's mrkdwn treats as control characters (`&`, `<`, `>`).

    Only for text that must appear LITERALLY in a message. Tools send the LLM's text
    verbatim (so `<@U…>` mentions and `<url|label>` links keep working); this is the
    helper for the rare value that must not be parsed.
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def md_cell(value: Any, *, max_len: int = 200) -> str:
    """Make a value safe inside a markdown table cell: no newlines, escaped pipes, clipped."""
    if value in (None, ""):
        return ""
    text = " ".join(str(value).split())
    if len(text) > max_len:
        text = text[: max_len - 1] + "…"
    return text.replace("|", "\\|")


# Block Kit blocks an app builds (a person's typed message carries only `rich_text`): one
# of these makes `message_text` read the message from its blocks.
APP_BLOCK_TYPES = frozenset({"section", "markdown", "context"})
# The block types `message_text` can read in full; `divider` holds no text. A message with
# any other block (`rich_text`, `table`, `image`, `actions`, …) keeps Slack's `text`.
_READABLE_BLOCK_TYPES = APP_BLOCK_TYPES | {"header", "divider"}


def _text_object(value: Any) -> str:
    """The `text` string of a Block Kit text object (`{"type": "mrkdwn", "text": …}`), or ''."""
    text = value.get("text") if isinstance(value, Mapping) else None
    return text if isinstance(text, str) else ""


def _context_line(block: Mapping[str, Any]) -> str:
    """A context block's text elements joined with a space (image elements hold no text)."""
    elements = block.get("elements")
    if not isinstance(elements, list | tuple):
        return ""
    return " ".join(text for text in (_text_object(element).strip() for element in elements) if text)


def _body_parts(block: Mapping[str, Any]) -> list[str]:
    """The text a section (its text, then its fields), markdown or header block shows."""
    kind = block.get("type")
    if kind == "markdown":
        text = block.get("text")
        return [text] if isinstance(text, str) else []
    parts = [_text_object(block.get("text"))]
    fields = block.get("fields") if kind == "section" else None
    if isinstance(fields, list | tuple):
        parts.extend(_text_object(field) for field in fields)
    return parts


def message_text(message: Mapping[str, Any]) -> str:
    """The text a message shows: rebuilt from its blocks when an app built them, else Slack's `text`.

    Slack stores the `text` fallback of a message posted WITH `blocks` with every newline
    turned into a space (measured 2026-10-10: `body\\n\\nsignature` read back as
    `body  signature`), while the blocks come back verbatim. So when `blocks` holds a
    `section`, `markdown` or `context` block and nothing this cannot read, the text is
    rebuilt from them, in order: each section's text (then its fields), markdown block and
    header joined with `\\n`, then every context block's text elements (joined with a
    space) as the LAST line(s) — the shape the message was posted in, a signature once, on
    its own line. A message with no blocks, or only `rich_text` ones (typed by a person),
    keeps `text`. So does one that mixes app blocks with blocks this cannot read (rich
    text, tables, images, buttons — e.g. what Slack may translate a `markdown` block into),
    except that a context text ending its flattened `text` is moved onto a last line of
    its own. Nothing to rebuild → `text`.
    """
    text = str(message.get("text") or "")
    raw_blocks = message.get("blocks")
    blocks = [block for block in raw_blocks if isinstance(block, Mapping)] if isinstance(raw_blocks, list) else []
    kinds = {block.get("type") for block in blocks}
    if not kinds & APP_BLOCK_TYPES:
        return text
    footer = [line for line in (_context_line(block) for block in blocks if block.get("type") == "context") if line]
    if kinds <= _READABLE_BLOCK_TYPES:
        body = [
            part
            for block in blocks
            if block.get("type") in {"section", "markdown", "header"}
            for part in _body_parts(block)
            if part.strip()
        ]
        return "\n".join(body + footer) or text
    flattened = text.rstrip()
    if footer and flattened.endswith(footer[-1]) and len(flattened) > len(footer[-1]):
        return f"{flattened[: -len(footer[-1])].rstrip()}\n{footer[-1]}"
    return text


def render_message(message: Mapping[str, Any], *, names: Mapping[str, str] | None = None) -> str:
    """`[2026-10-09 14:05 UTC] Ada: text (ts 1712345678.123456) · 3 replies`.

    `names` maps user ids to display strings; unknown authors keep their id (or
    `username`/`bot_id` for app posts). The text is `message_text`: Slack's `text`, or —
    for a message an app built from Block Kit blocks, whose stored `text` Slack flattens
    to one line — the text rebuilt from those blocks, so what reads back is what was
    posted. Either way it is passed through as Slack stores it, so `<@U…>` mentions and
    `<url|label>` links stay recognisable — except line breaks: every
    line after the first is prefixed with `MESSAGE_CONTINUATION` (`  │ `), so a multi-line
    message can never print a line that reads like another `[time] Name:` message. The
    `(ts …)` and reply-count suffixes go on the LAST line. The author is collapsed to one
    line too: an app's `username` is free text and could otherwise forge a header.
    """
    author = str(message.get("user") or message.get("username") or message.get("bot_id") or "unknown")
    if names and author in names:
        author = names[author]
    author = " ".join(author.split()) or "unknown"
    ts = message.get("ts", "")
    first, *rest = message_text(message).strip().splitlines() or [""]
    continuation = "\n".join(f"{MESSAGE_CONTINUATION}{part}".rstrip() for part in rest)
    text = f"{first}\n{continuation}" if rest else first
    subtype = message.get("subtype")
    line = f"[{ts_to_human(ts)}] {author}: {text}" if text else f"[{ts_to_human(ts)}] {author}"
    if subtype:
        line += f" ({subtype})"
    if ts:
        line += f" (ts {ts})"
    reply_count = message.get("reply_count")
    if isinstance(reply_count, int) and reply_count > 0:
        line += f" · {reply_count} repl{'y' if reply_count == 1 else 'ies'}"
    if message.get("edited"):
        line += " · edited"
    return line


def fmt_warnings(body: Mapping[str, Any] | None) -> str:
    """One line naming the soft warnings on an `ok: true` body (empty string when none).

    Slack reports them as `warning` (a code, or a comma-separated list) and/or
    `response_metadata.warnings[]` — e.g. `message_truncated` when chat.postMessage cut a
    text over 40,000 characters but still posted it. Write tools append this to their
    confirmation so the LLM learns what Slack changed.
    """
    if not body:
        return ""
    codes: list[str] = []
    raw = body.get("warning")
    if isinstance(raw, str):
        codes.extend(part.strip() for part in raw.split(","))
    metadata = body.get("response_metadata")
    if isinstance(metadata, Mapping):
        warnings = metadata.get("warnings")
        if isinstance(warnings, list | tuple):
            codes.extend(str(item).strip() for item in warnings)
    unique = list(dict.fromkeys(code for code in codes if code))
    if not unique:
        return ""
    parts = [f"`{code}` — {hint_for(code)}" if hint_for(code) else f"`{code}`" for code in unique]
    return "Slack warning(s): " + "; ".join(parts)


def clip_response(text: str, max_bytes: int = MAX_RESPONSE_BYTES) -> str:
    """Redact credentials, then truncate an oversized response with an explicit note."""
    text = redact_credentials(text)
    encoded = text.encode()
    if len(encoded) <= max_bytes:
        return text
    cut = encoded[:max_bytes].decode(errors="ignore")
    return f"{cut}\n\n_[truncated: response exceeded {max_bytes:,} bytes — narrow the query]_"


def paginated_response(
    *,
    items: Sequence[Any],
    fmt: ResponseFormat,
    item_formatter: Callable[[Any], str],
    title: str,
    next_cursor: str | None,
    limit: int | None = None,
    note: str | None = None,
    table_header: Sequence[str] | None = None,
) -> str:
    """Uniform cursor-paginated output for list tools.

    Slack paginates with `response_metadata.next_cursor`; an empty string means the
    last page. The cursor is printed verbatim so the LLM can pass it back as `cursor`.
    With `table_header` (column titles), the markdown body is a table and
    `item_formatter` must return one `| a | b |` row per item (escape cells with `md_cell`).
    Items may be API objects or plain values (e.g. the member ids of conversations.members);
    JSON mode returns them as the API sent them (credentials redacted).
    """
    cursor = (next_cursor or "").strip()
    has_more = bool(cursor)
    count = len(items)

    if fmt is ResponseFormat.JSON:
        return clip_response(
            to_json(
                {
                    "title": title,
                    "count": count,
                    "limit": limit,
                    "has_more": has_more,
                    "next_cursor": cursor or None,
                    "note": note,
                    "items": list(items),
                }
            )
        )

    lines = [f"# {title}", "", f"Showing **{count:,}** item(s)" + (f" (limit {limit})." if limit else ".")]
    if note:
        lines.append(note)
    if has_more:
        lines.append(f"More available — pass `cursor` = `{cursor}` to get the next page.")
    lines.append("")
    if not items:
        lines.append("_No items._")
        return clip_response("\n".join(lines))
    if table_header:
        lines.append("| " + " | ".join(table_header) + " |")
        lines.append("|" + "---|" * len(table_header))
    for item in items:
        lines.append(item_formatter(item))
    return clip_response("\n".join(lines))
