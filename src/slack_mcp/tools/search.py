"""Slack `search` tool: full-text message search with Slack's own query syntax, as you.

`search.messages` only answers user tokens (scope `search:read`) — one of the reasons
this server speaks as the user instead of as a bot. Results are what YOU can see in
Slack, filtered by the search preferences set in your Slack UI. Paging is by page
number (`page` / `count`), not by cursor.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from slack_mcp.client import get_client
from slack_mcp.errors import SlackApiError, handle_api_error
from slack_mcp.formatters import ResponseFormat, clip_response, to_json, ts_to_human
from slack_mcp.server import mcp

# Context-window guards (house rule 8): matches rendered per call in markdown, and the
# characters of each match's text shown there (JSON mode keeps the full text).
MAX_DISPLAY_ROWS = 50
MAX_TEXT_CHARS = 1_000
# search.messages: "the max `page` value is `100`" (research 02 §4.18).
MAX_PAGE = 100

_SCOPE_HINT = "search.messages needs the `search:read` USER scope (a bot token can never search)."


class SearchMessagesInput(BaseModel):
    """Arguments for `search.messages`."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    query: str = Field(
        min_length=1,
        max_length=500,
        description=(
            "Slack search query, sent verbatim. Words plus modifiers, e.g. "
            '`deploy in:#eng from:@ada after:2026-10-01`, `"exact phrase" -draft`, `in:<#C0123ABCDEF>`, '
            "`from:<@U0123ABCDEF>`, `is:thread`, `has:pin`, `before:2026-10-09`, `on:2026-10-08`, `during:2026`."
        ),
    )
    sort: Literal["score", "timestamp"] = Field(
        default="score",
        description="`score` = most relevant first (Slack's default); `timestamp` = by date.",
    )
    sort_dir: Literal["asc", "desc"] = Field(
        default="desc",
        description="`desc` (default) = best / newest first; `asc` = the reverse.",
    )
    count: int = Field(
        default=20,
        ge=1,
        le=100,
        description=f"Matches per page, 1–100 (default 20). Markdown output shows at most {MAX_DISPLAY_ROWS} per call.",
    )
    page: int = Field(
        default=1,
        ge=1,
        le=MAX_PAGE,
        description=f"Page number, starting at 1 (Slack serves at most {MAX_PAGE} pages).",
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="`markdown` (default, readable) or `json` (the raw `messages` object, full texts).",
    )


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _channel_label(match: Mapping[str, Any]) -> str:
    """`#general (C0123…)`, `DM with U0123… (D0123…)` or `group DM mpdm-… (G0123…)`.

    For an IM result Slack sets `type: "im"` and puts the other person's user id in
    `channel.name` (research 02 §4.18).
    """
    channel = _mapping(match.get("channel"))
    cid = channel.get("id")
    name = channel.get("name")
    kind = match.get("type")
    if channel.get("is_im") or kind == "im":
        label = f"DM with {name}" if name else "DM"
    elif channel.get("is_mpim") or kind == "mpim":
        label = f"group DM {name}" if name else "group DM"
    else:
        label = f"#{name}" if name else "conversation"
    return f"{label} ({cid})" if cid else label


def _author(match: Mapping[str, Any]) -> str:
    """`@ada (U0123…)` from the match's `username` + `user` id."""
    username = match.get("username")
    uid = match.get("user")
    if username and uid:
        return f"@{username} ({uid})"
    if username:
        return f"@{username}"
    return str(uid) if uid else "unknown"


def _text(match: Mapping[str, Any]) -> str:
    text = " ".join(str(match.get("text") or "").split())
    if len(text) > MAX_TEXT_CHARS:
        text = text[: MAX_TEXT_CHARS - 1] + "…"
    return text


def _render_match(index: int, match: Mapping[str, Any]) -> str:
    ts = match.get("ts", "")
    line = f"{index}. [{ts_to_human(ts)}] {_channel_label(match)} · {_author(match)}: {_text(match)}"
    if ts:
        line += f" (ts {ts})"
    permalink = match.get("permalink")
    if permalink:
        line += f"\n   link: {permalink}"
    return line


def _render_markdown(params: SearchMessagesInput, body: Mapping[str, Any]) -> str:
    messages = _mapping(body.get("messages"))
    raw_matches = messages.get("matches")
    matches: list[Mapping[str, Any]] = (
        [m for m in raw_matches if isinstance(m, Mapping)] if isinstance(raw_matches, list) else []
    )
    paging = _mapping(messages.get("paging"))
    pagination = _mapping(messages.get("pagination"))
    total = paging.get("total", messages.get("total", pagination.get("total_count", len(matches))))
    page = paging.get("page", pagination.get("page", params.page))
    pages = paging.get("pages", pagination.get("page_count"))

    lines = [
        f"# Slack search: `{body.get('query') or params.query}`",
        "",
        f"**{total}** match(es) in total — page {page} of {pages if pages is not None else '?'} "
        f"({len(matches)} on this page, sorted by {params.sort} {params.sort_dir}).",
    ]
    if isinstance(page, int) and isinstance(pages, int) and page < pages:
        if page < MAX_PAGE:
            lines.append(f"More available — pass `page` = {page + 1} to get the next page.")
        else:
            lines.append(
                f"Slack serves at most {MAX_PAGE} pages — narrow the query (in:, from:, after:, before:) to reach "
                "the rest."
            )
    if len(matches) > MAX_DISPLAY_ROWS:
        lines.append(
            f"Showing the first {MAX_DISPLAY_ROWS} of the {len(matches)} matches on this page — ask for `count` ≤ "
            f'{MAX_DISPLAY_ROWS} to see whole pages, or use `response_format` = "json".'
        )
    lines.append("")
    if not matches:
        lines.append(
            "_No messages matched._ Results follow the search filters set in your Slack UI; check the modifiers "
            "(in:#channel, from:@user, after:YYYY-MM-DD)."
        )
        return clip_response("\n".join(lines))
    for index, match in enumerate(matches[:MAX_DISPLAY_ROWS], start=1):
        lines.append(_render_match(index, match))
    return clip_response("\n".join(lines))


@mcp.tool(
    name="slack_search_messages",
    annotations=ToolAnnotations(
        title="Search Slack Messages",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def slack_search_messages(params: SearchMessagesInput) -> str:
    """Search messages across every conversation you can see, with Slack's query syntax.

    Calls `search.messages`. The `query` is passed to Slack verbatim, so Slack's search
    modifiers work exactly as in the Slack search box. Search is USER-TOKEN ONLY — a bot
    token cannot search at all, which is one reason this server speaks as you. Results
    are what you can see (public channels, your private channels, DMs and group DMs) and
    follow the search filters set in your Slack UI; Slack collapses near-duplicate
    matches into one. Highlight markup is turned off, so texts come back as written.

    Query syntax (combine freely):
    - `"exact phrase"`, `-word` to exclude a word
    - `in:#channel` or `in:<#C0123ABCDEF>`; `in:<@U0123ABCDEF>` for a DM; `-in:#channel`
    - `from:@user` or `from:<@U0123ABCDEF>`; `-from:@user`; `with:@user`
    - `after:YYYY-MM-DD`, `before:YYYY-MM-DD`, `on:YYYY-MM-DD`, `during:2026` / a month
    - `is:thread`, `is:saved`, `has:pin`, `has::eyes:` (has that reaction)

    When to Use:
    - To find a message when you don't know which conversation it is in.
    - To get the channel id + `ts` of a message so you can reply (`slack_reply_in_thread`)
      or react (`slack_add_reaction`) to it.
    - To find what someone said about a topic (`from:@ada topic`).

    When NOT to Use:
    - To read a conversation in order (use `slack_get_history`) or a whole thread (use
      `slack_get_thread`).
    - To find a channel or person by name (use `slack_list_conversations` /
      `slack_resolve_user`).

    Returns:
    Markdown: the total, `page X of Y`, and per match the conversation (name + id), the
    author (`@username` + id), the time, the text (clipped to 1,000 characters), the
    `ts` and the permalink. JSON: the raw `messages` object (`matches`, `paging`,
    `pagination`, `total`) with full texts.

    Pagination:
    By page number, not cursor: pass `page` = N+1 as the output says (max 100 pages of
    up to 100 matches). Markdown shows at most 50 matches per call.

    Examples:
    - params = {"query": "deploy failed in:#eng after:2026-10-01"}
    - params = {"query": "from:<@U0123ABCDEF> invoice", "sort": "timestamp", "count": 50}
    - params = {"query": '"release notes" is:thread', "page": 2}

    Error Handling:
    `missing_scope` = the app lacks the `search:read` USER scope (add it under User Token
    Scopes and re-install); `no_query` = empty query; `ratelimited` = slow down (Tier 2,
    about 20 searches per minute); `invalid_auth` = token problem.
    """
    try:
        client = get_client()
        body = await client.call(
            "search.messages",
            {
                "query": params.query,
                "sort": params.sort,
                "sort_dir": params.sort_dir,
                "count": params.count,
                "page": params.page,
                "highlight": False,
            },
        )
        if params.response_format is ResponseFormat.JSON:
            return clip_response(to_json({"query": body.get("query", params.query), "messages": body.get("messages")}))
        return _render_markdown(params, body)
    except Exception as exc:
        message = handle_api_error(exc)
        if isinstance(exc, SlackApiError) and exc.code == "missing_scope" and "search:read" not in message:
            message = f"{message} {_SCOPE_HINT}"
        return message
