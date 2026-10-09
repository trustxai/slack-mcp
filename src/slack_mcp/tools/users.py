"""Slack `users` tools: list, profile, lookup by email, and local name → id resolution.

Four read-only tools over `users.list`, `users.info` and `users.lookupByEmail`
(research 02 §4.9–4.11). `slack_resolve_user` is the bridge every write needs: it turns
a person's name, handle or display name into the `U…` id that `<@U…>` mentions and
`slack_open_conversation` take. It walks `users.list` once into a per-process cache
(keyed by the API base URL and a hash of the token) and scores the query with stdlib
`difflib` — no extra dependency, and no request per lookup after the first.
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import re
import time
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, field_validator

from slack_mcp.client import get_client
from slack_mcp.config import get_settings
from slack_mcp.errors import SlackApiError, handle_api_error
from slack_mcp.formatters import (
    ResponseFormat,
    clip_response,
    fmt_user,
    md_cell,
    paginated_response,
    render_mention,
    to_json,
)
from slack_mcp.server import mcp
from slack_mcp.validators import user_id

# Context-window guard: at most this many rows rendered per call, on top of the API limit.
MAX_DISPLAY_ROWS = 50

# The resolver reads the directory in pages of this size (the documented ≤ 200 advice).
RESOLVE_PAGE_LIMIT = 200
# …and stops after this many pages (≈ 4,000 people), saying the index is partial. users.list
# is Tier 2 (20+ calls per minute), so 20 pages is about one minute of its budget.
MAX_RESOLVE_PAGES = 20

# A query shorter than this (after normalising) is never called a unique match.
MIN_UNIQUE_QUERY_LEN = 2
# Words shorter than this are not scored on their own (initials such as the "J" in "J. R. Smith").
MIN_WORD_LEN = 2

# Scores at or above this are a confident match; exactly one of them is a "unique match".
UNIQUE_MATCH_SCORE = 0.9
# Below this a candidate is noise and is not listed at all.
MIN_SCORE = 0.5

# Slackbot is special: users.list reports it with `is_bot: false` (research 02 §4.9).
_SLACKBOT_ID = "USLACKBOT"

_EMAIL_RE = re.compile(r"^[^@\s<>|]+@[^@\s<>|]+\.[^@\s<>|]+$")
_TOKEN_SPLIT_RE = re.compile(r"[\s._\-]+")


# --------------------------------------------------------------------------------------
# Per-process directory cache for slack_resolve_user
# --------------------------------------------------------------------------------------


@dataclass
class _Directory:
    """Every member `users.list` returned (deleted users and bots included; filtered at query time)."""

    members: list[dict[str, Any]]
    pages: int
    complete: bool
    # Why the walk stopped early: "rate_limited" or "page_cap" (None when complete).
    stopped: str | None = None
    # `_clock()` time of the last walk that produced or kept this entry.
    walked_at: float = 0.0


# Keyed by (base URL, sha256(token)[:12]): a test server and the real API never share an
# entry, and neither do two tokens (two workspaces) used by the same process.
_DIRECTORY_CACHE: dict[tuple[str, str], _Directory] = {}
# Serialises the walk, so concurrent resolver calls on an empty cache read users.list once.
_DIRECTORY_LOCK = asyncio.Lock()
# Injectable monotonic clock (tests replace it).
_clock: Callable[[], float] = time.monotonic
# A rate-limited PARTIAL entry is re-walked automatically once it is this old: users.list
# is Tier 2, whose budget is per minute.
RATE_LIMITED_REWALK_SECONDS = 60.0


def clear_user_cache() -> None:
    """Forget the cached directory and reset the walk lock (tests call this between event loops).

    `refresh=true` on `slack_resolve_user` re-reads the directory for one key instead.
    """
    global _DIRECTORY_LOCK
    _DIRECTORY_CACHE.clear()
    _DIRECTORY_LOCK = asyncio.Lock()


def _cache_key() -> tuple[str, str]:
    settings = get_settings()
    token_hash = hashlib.sha256(settings.slack_user_token.encode()).hexdigest()[:12]
    return settings.base_url, token_hash


def _is_rate_limit(exc: Exception) -> bool:
    """An HTTP 429 (after the client's one bounded retry) or a `ratelimited` / `rate_limited` body.

    The status is read by duck typing (`exc.response.status_code`), so the tool module's
    only coupling to the transport stays `get_client().call(...)`.
    """
    if getattr(getattr(exc, "response", None), "status_code", None) == 429:
        return True
    return isinstance(exc, SlackApiError) and exc.code in ("ratelimited", "rate_limited")


def _needs_walk(cached: _Directory | None, refresh: bool) -> bool:
    """Walk on a miss, on `refresh`, or when a rate-limited PARTIAL entry is old enough to retry."""
    if cached is None or refresh:
        return True
    return cached.stopped == "rate_limited" and _clock() - cached.walked_at >= RATE_LIMITED_REWALK_SECONDS


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def _compact(params: Mapping[str, Any]) -> dict[str, Any]:
    """The params dict without `None` values — what the tools hand to `client.call`."""
    return {key: value for key, value in params.items() if value is not None}


def _profile(user: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = user.get("profile")
    return raw if isinstance(raw, Mapping) else {}


def _next_cursor(body: Mapping[str, Any]) -> str:
    metadata = body.get("response_metadata")
    if not isinstance(metadata, Mapping):
        return ""
    return str(metadata.get("next_cursor") or "").strip()


def _members(body: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = body.get("members")
    if not isinstance(raw, list):
        return []
    return [member for member in raw if isinstance(member, dict)]


def _is_bot(user: Mapping[str, Any]) -> bool:
    return bool(user.get("is_bot")) or user.get("id") == _SLACKBOT_ID


def _split_hidden(
    members: list[dict[str, Any]], *, include_deleted: bool, include_bots: bool
) -> tuple[list[dict[str, Any]], int, int]:
    """(visible, hidden_deleted, hidden_bots). A deleted bot counts once, as deleted."""
    visible: list[dict[str, Any]] = []
    hidden_deleted = hidden_bots = 0
    for member in members:
        if member.get("deleted") and not include_deleted:
            hidden_deleted += 1
        elif _is_bot(member) and not include_bots:
            hidden_bots += 1
        else:
            visible.append(member)
    return visible, hidden_deleted, hidden_bots


def _hidden_note(hidden_deleted: int, hidden_bots: int, *, scope: str) -> str | None:
    parts: list[str] = []
    if hidden_deleted:
        parts.append(f"{hidden_deleted} deactivated")
    if hidden_bots:
        parts.append(f"{hidden_bots} bot{'s' if hidden_bots != 1 else ''}")
    if not parts:
        return None
    return f"Hidden {scope}: {', '.join(parts)} — pass `include_deleted=true` / `include_bots=true` to show them."


def _flags(user: Mapping[str, Any]) -> list[str]:
    flags: list[str] = []
    if user.get("deleted"):
        flags.append("deactivated")
    if _is_bot(user):
        flags.append("bot")
    if user.get("is_primary_owner"):
        flags.append("primary owner")
    elif user.get("is_owner"):
        flags.append("owner")
    elif user.get("is_admin"):
        flags.append("admin")
    if user.get("is_ultra_restricted"):
        flags.append("single-channel guest")
    elif user.get("is_restricted"):
        flags.append("guest")
    if user.get("is_stranger"):
        flags.append("external")
    return flags


def _yes_no(value: Any) -> str:
    return "yes" if value else "no"


def _with_scope_note(exc: Exception, scope: str, method: str) -> str:
    """`handle_api_error`, plus the exact scope when Slack says `missing_scope` without naming it."""
    message = handle_api_error(exc)
    if isinstance(exc, SlackApiError) and exc.code == "missing_scope" and scope not in message:
        message += f" {method} needs the USER scope `{scope}`."
    return message


def _render_profile(user: Mapping[str, Any], *, title: str | None = None) -> str:
    """The full markdown card for one user object (users.info / users.lookupByEmail)."""
    profile = _profile(user)
    uid = user.get("id")
    lines = [f"# {title or fmt_user(user)}", ""]
    if title:
        lines.extend([f"**{fmt_user(user)}**", ""])
    real_name = user.get("real_name") or profile.get("real_name")
    lines.append(f"- **real name**: {real_name or 'N/A'}")
    lines.append(f"- **display name**: {profile.get('display_name') or 'N/A (not set)'}")
    lines.append(f"- **handle**: @{user.get('name')}" if user.get("name") else "- **handle**: N/A")
    lines.append(f"- **title**: {profile.get('title') or 'N/A'}")
    email = profile.get("email")
    lines.append(
        f"- **email**: {email}"
        if email
        else "- **email**: not visible (needs the `users:read.email` scope, or the workspace hides emails)"
    )
    tz = user.get("tz")
    tz_label = user.get("tz_label")
    if tz and tz_label:
        lines.append(f"- **time zone**: {tz} ({tz_label})")
    else:
        lines.append(f"- **time zone**: {tz or tz_label or 'N/A'}")
    status = " ".join(str(part) for part in (profile.get("status_emoji"), profile.get("status_text")) if part)
    if status:
        lines.append(f"- **status**: {status}")
    lines.append(f"- **is_admin**: {_yes_no(user.get('is_admin'))}" + (" (owner)" if user.get("is_owner") else ""))
    lines.append(f"- **is_bot**: {_yes_no(_is_bot(user))}")
    lines.append(f"- **deleted**: {_yes_no(user.get('deleted'))}")
    if user.get("is_restricted") or user.get("is_ultra_restricted"):
        lines.append(
            "- **guest**: " + ("single-channel guest" if user.get("is_ultra_restricted") else "multi-channel guest")
        )
    if user.get("is_stranger"):
        lines.append("- **external**: yes — a Slack Connect user from another organisation")
    if uid:
        if user.get("deleted"):
            lines.append("- **mention**: none — the account is deactivated")
        else:
            lines.append(f"- **mention**: write `{render_mention(str(uid))}` in a message to mention them")
    return clip_response("\n".join(lines))


# --------------------------------------------------------------------------------------
# Scoring for slack_resolve_user
# --------------------------------------------------------------------------------------


def _norm(text: Any) -> str:
    """Case-, accent- and whitespace-insensitive form (`Ramírez` == `ramirez`), leading `@` dropped."""
    decomposed = unicodedata.normalize("NFKD", str(text))
    stripped = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(stripped.casefold().strip().lstrip("@").split())


def _candidates(user: Mapping[str, Any]) -> list[str]:
    """real_name / display_name / name, each whole and split into words (`Ricardo Pérez` → `ricardo`).

    Single-character words (initials such as the `J` in `J. R. Smith`) are not candidates
    on their own, so a one-letter query cannot score 1.0 against everyone with that initial.
    """
    profile = _profile(user)
    names: list[str] = []
    for raw in (
        user.get("real_name"),
        profile.get("real_name"),
        profile.get("display_name"),
        user.get("name"),
    ):
        if not raw:
            continue
        whole = _norm(raw)
        if not whole:
            continue
        names.append(whole)
        names.extend(part for part in _TOKEN_SPLIT_RE.split(whole) if len(part) >= MIN_WORD_LEN and part != whole)
    return list(dict.fromkeys(names))


def _score(query: str, user: Mapping[str, Any]) -> float:
    """Best `difflib.SequenceMatcher` ratio of the query against the user's names (1.0 for an exact id/email)."""
    profile = _profile(user)
    if query == _norm(user.get("id") or "") or (profile.get("email") and query == _norm(profile["email"])):
        return 1.0
    best = 0.0
    for candidate in _candidates(user):
        ratio = difflib.SequenceMatcher(None, query, candidate).ratio()
        if ratio > best:
            best = ratio
            if best == 1.0:
                break
    return best


async def _load_directory(refresh: bool) -> tuple[_Directory, str | None]:
    """The directory for this base URL + token, plus a notice when a re-walk was cut short.

    Walks the `users.list` pages on a miss, on `refresh`, and when a rate-limited PARTIAL
    entry is at least `RATE_LIMITED_REWALK_SECONDS` old. The walk runs under a lock, and
    the cache is checked again once the lock is held, so concurrent calls on an empty
    cache read the directory once.

    A rate limit mid-walk never makes the cache worse:
    - with an earlier entry that is complete, or at least as large as what this walk
      read, that entry is KEPT (its clock restarts) and the notice says so — a
      rate-limited `refresh` never replaces a complete directory with a partial one;
    - otherwise, if at least one page was read, it is cached as PARTIAL
      (`stopped="rate_limited"`), so later calls do not restart from page 1;
    - with nothing read and nothing cached, the error is raised.
    Any other error raises and leaves the cache as it was.
    """
    key = _cache_key()
    cached = _DIRECTORY_CACHE.get(key)
    if cached is not None and not _needs_walk(cached, refresh):
        return cached, None
    async with _DIRECTORY_LOCK:
        cached = _DIRECTORY_CACHE.get(key)
        if cached is not None and not _needs_walk(cached, refresh):
            return cached, None
        client = get_client()
        members: list[dict[str, Any]] = []
        cursor: str | None = None
        pages = 0
        stopped: str | None = None
        while True:
            try:
                body = await client.call("users.list", _compact({"limit": RESOLVE_PAGE_LIMIT, "cursor": cursor}))
            except Exception as exc:
                if not _is_rate_limit(exc):
                    raise
                if cached is not None and (cached.complete or len(cached.members) >= len(members)):
                    cached.walked_at = _clock()
                    return cached, (
                        f"Re-reading the directory was rate-limited by Slack after {pages} page(s); kept the "
                        f"directory cached earlier ({len(cached.members):,} people)."
                    )
                if pages == 0:
                    raise
                stopped = "rate_limited"
                break
            pages += 1
            members.extend(_members(body))
            cursor = _next_cursor(body) or None
            if cursor is None:
                break
            if pages >= MAX_RESOLVE_PAGES:
                stopped = "page_cap"
                break
        directory = _Directory(
            members=members, pages=pages, complete=stopped is None, stopped=stopped, walked_at=_clock()
        )
        _DIRECTORY_CACHE[key] = directory
        return directory, None


# --------------------------------------------------------------------------------------
# Input models
# --------------------------------------------------------------------------------------


class ListUsersInput(BaseModel):
    """Input for slack_list_users."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    limit: int = Field(
        default=50, ge=1, le=200, description="Members to request from Slack for this page (1–200, default 50)."
    )
    cursor: str | None = Field(
        default=None,
        description="The `next_cursor` printed by the previous page; omit for the first page.",
    )
    include_deleted: bool = Field(
        default=False, description="Also show deactivated accounts (hidden by default; the count is reported)."
    )
    include_bots: bool = Field(
        default=False, description="Also show bots and Slackbot (hidden by default; the count is reported)."
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN, description="'markdown' (a table) or 'json' (the raw user objects)."
    )

    @field_validator("cursor", mode="before")
    @classmethod
    def _empty_cursor_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value


class GetUserInput(BaseModel):
    """Input for slack_get_user."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    user: str = Field(
        ...,
        description="The Slack user ID (U… or W…), e.g. 'U0123ABCDEF'. Not a name — use slack_resolve_user first.",
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN, description="'markdown' (a profile card) or 'json' (the raw user object)."
    )

    @field_validator("user", mode="before")
    @classmethod
    def _check_user(cls, value: object) -> str:
        return user_id(value)


class LookupUserByEmailInput(BaseModel):
    """Input for slack_lookup_user_by_email."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    email: str = Field(
        ...,
        min_length=3,
        max_length=254,
        description="The person's email address in this workspace, e.g. 'ada@example.com'.",
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN, description="'markdown' (a profile card) or 'json' (the raw user object)."
    )

    @field_validator("email", mode="after")
    @classmethod
    def _check_email(cls, value: str) -> str:
        if not _EMAIL_RE.match(value):
            raise ValueError(f"email must be a plain address like ada@example.com, got {value!r}")
        return value


class ResolveUserInput(BaseModel):
    """Input for slack_resolve_user."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    query: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description=(
            "A name, display name or @handle to look for, e.g. 'Ricardo', 'ricardo.perez' or 'Ada Lovelace'. "
            "Case and accents are ignored."
        ),
    )
    limit: int = Field(default=5, ge=1, le=10, description="How many top-scoring people to return (1–10, default 5).")
    include_deleted: bool = Field(
        default=False, description="Also consider deactivated accounts (excluded by default)."
    )
    include_bots: bool = Field(default=False, description="Also consider bots and Slackbot (excluded by default).")
    refresh: bool = Field(
        default=False,
        description="Re-read the directory from Slack instead of the cached copy (e.g. someone joined since).",
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN, description="'markdown' (a ranked table) or 'json' (scores + ids)."
    )

    @field_validator("query", mode="after")
    @classmethod
    def _query_has_letters(cls, value: str) -> str:
        if not _norm(value):
            raise ValueError("query must contain a name, display name or handle (it is empty after normalising)")
        return value


# --------------------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------------------


def _user_row(user: Mapping[str, Any]) -> str:
    profile = _profile(user)
    tz = user.get("tz") or user.get("tz_label")
    return (
        f"| {md_cell(fmt_user(user))} | {md_cell(profile.get('title'))} | {md_cell(tz)} | "
        f"{md_cell(profile.get('email'))} | {md_cell(', '.join(_flags(user)))} |"
    )


@mcp.tool(
    name="slack_list_users",
    annotations=ToolAnnotations(
        title="List Slack Users",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def slack_list_users(params: ListUsersInput) -> str:
    """List the people in the workspace, one cursor page at a time.

    Calls `users.list` with an explicit `limit` and renders each member as
    `Name (@handle, U0123…)` with title, time zone, email (when the token may see it)
    and flags (admin, guest, external). Deactivated accounts and bots — which Slack
    includes in the list — are hidden by default, and the response says how many were
    hidden on this page.

    When to Use:
    - To browse the directory, or to page through everyone (e.g. for a roster).
    - To see who is a guest, an admin or an external (Slack Connect) member.

    When NOT to Use:
    - To find ONE person by name or handle — use `slack_resolve_user` (ranked, cached,
      no paging by hand).
    - When you have their email — use `slack_lookup_user_by_email`.
    - When you have their id — use `slack_get_user` for the full profile.
    - To message someone — resolve the id, then `slack_open_conversation` followed by
      `slack_post_message` (writes are gated separately).

    Returns:
    A markdown table (`User | Title | Time zone | Email | Flags`) with the page count,
    the hidden deactivated/bot counts, and the `next_cursor` when there is a next page —
    or, in JSON mode, the raw user objects with `has_more` / `next_cursor`. At most
    50 rows are rendered per call.

    Pagination:
    Slack paginates with a cursor. When the response prints `cursor` = `…`, pass that
    value as `cursor` to get the next page; no cursor means the last page. Slack cuts
    the page BEFORE the deleted/bot filter, so a page can show fewer than `limit`
    people and still have more. The tool never walks more than one page.

    Examples:
    - First page: `params = {"limit": 50}`
    - Next page: `params = {"limit": 50, "cursor": "dXNlcjpVMEc5V0ZYTlo="}`
    - Everyone incl. bots: `params = {"include_bots": true, "include_deleted": true}`

    Error Handling:
    `missing_scope` means the app lacks the USER scope `users:read` (and emails need
    `users:read.email`); `invalid_cursor` means the cursor expired — start again
    without it; `ratelimited` means wait for Retry-After (users.list is Tier 2).
    """
    try:
        body = await get_client().call("users.list", _compact({"limit": params.limit, "cursor": params.cursor}))
        visible, hidden_deleted, hidden_bots = _split_hidden(
            _members(body), include_deleted=params.include_deleted, include_bots=params.include_bots
        )
        notes: list[str] = []
        hidden = _hidden_note(hidden_deleted, hidden_bots, scope="on this page")
        if hidden:
            notes.append(hidden)
        shown = visible[:MAX_DISPLAY_ROWS]
        if len(visible) > len(shown):
            notes.append(
                f"{len(visible) - len(shown)} more on this page were not displayed (at most {MAX_DISPLAY_ROWS} rows "
                f"per call) — re-run with `limit` ≤ {MAX_DISPLAY_ROWS} to see every row, since the cursor skips them."
            )
        return paginated_response(
            items=shown,
            fmt=params.response_format,
            item_formatter=_user_row,
            title="Slack users",
            next_cursor=_next_cursor(body),
            limit=params.limit,
            note="\n".join(notes) or None,
            table_header=("User", "Title", "Time zone", "Email", "Flags"),
        )
    except Exception as exc:
        return _with_scope_note(exc, "users:read", "users.list")


@mcp.tool(
    name="slack_get_user",
    annotations=ToolAnnotations(
        title="Get Slack User",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def slack_get_user(params: GetUserInput) -> str:
    """Show one person's profile by user id.

    Calls `users.info` and renders the profile: real name, display name, handle, title,
    email (when visible), time zone, status, admin / bot / deactivated flags, guest and
    external (Slack Connect) markers, and the `<@U…>` string that mentions them.

    When to Use:
    - To turn an id seen in a history (`<@U0123ABCDEF>` or a message author) into a name.
    - To check someone's time zone, title or whether the account is deactivated.

    When NOT to Use:
    - When you only have a name or handle — use `slack_resolve_user` first.
    - When you have an email — use `slack_lookup_user_by_email` (one call, same card).
    - To message the person — `slack_open_conversation` with their id, then
      `slack_post_message` to the returned DM id.

    Returns:
    A markdown profile card headed `Name (@handle, U0123…)`, or the raw `user` object in
    JSON mode.

    Examples:
    - `params = {"user": "U0123ABCDEF"}`
    - `params = {"user": "U0123ABCDEF", "response_format": "json"}`

    Error Handling:
    A name or a channel id is rejected locally (only `U…`/`W…` ids). `user_not_found`
    means the id does not exist in this workspace; `user_not_visible` means your
    account cannot see it; `missing_scope` means the app lacks the USER scope
    `users:read` (emails also need `users:read.email`).
    """
    try:
        body = await get_client().call("users.info", {"user": params.user})
        raw_user = body.get("user")
        user: dict[str, Any] = raw_user if isinstance(raw_user, dict) else {}
        if params.response_format is ResponseFormat.JSON:
            return clip_response(to_json(user))
        return _render_profile(user)
    except Exception as exc:
        return _with_scope_note(exc, "users:read", "users.info")


@mcp.tool(
    name="slack_lookup_user_by_email",
    annotations=ToolAnnotations(
        title="Look Up Slack User by Email",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def slack_lookup_user_by_email(params: LookupUserByEmailInput) -> str:
    """Find the person behind an email address and show their profile.

    Calls `users.lookupByEmail` — an exact match, no fuzziness — and renders the same
    profile card as `slack_get_user`, including the user id and the `<@U…>` mention.

    When to Use:
    - You have someone's work email (from a calendar invite, a ticket, a signature) and
      need their Slack id to mention or DM them.

    When NOT to Use:
    - With a name or handle — use `slack_resolve_user` (fuzzy, ranked).
    - With an id — use `slack_get_user`.
    - To message the person — after this, `slack_open_conversation` with the id, then
      `slack_post_message`.

    Returns:
    A markdown profile card headed `Name (@handle, U0123…)`, or the raw `user` object in
    JSON mode.

    Examples:
    - `params = {"email": "ada@example.com"}`

    Error Handling:
    A string that is not an email address is rejected locally. `users_not_found` means
    no ACTIVE account has that email (Slack returns the same code for a deactivated
    one — `slack_list_users` with `include_deleted=true` shows those). `missing_scope`
    means the app lacks the USER scope `users:read.email`, which this method requires.
    """
    try:
        body = await get_client().call("users.lookupByEmail", {"email": params.email})
        raw_user = body.get("user")
        user: dict[str, Any] = raw_user if isinstance(raw_user, dict) else {}
        if params.response_format is ResponseFormat.JSON:
            return clip_response(to_json(user))
        return _render_profile(user, title=f"Slack user for {params.email}")
    except Exception as exc:
        return _with_scope_note(exc, "users:read.email", "users.lookupByEmail")


@mcp.tool(
    name="slack_resolve_user",
    annotations=ToolAnnotations(
        title="Resolve Slack User by Name",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def slack_resolve_user(params: ResolveUserInput) -> str:
    """Turn a name, display name or @handle into Slack user ids, ranked by similarity.

    The first call walks the `users.list` pages (200 at a time, at most 20 pages) into a
    per-process cache; later calls are answered locally until the server restarts, and
    `refresh=true` re-reads the directory. Each person is scored with
    `difflib.SequenceMatcher` against their real name, display name and handle — whole
    and word by word (words of 2+ characters, so initials do not count), so `Ricardo`
    matches `Ricardo Pérez` — ignoring case and accents. An exact id or email scores
    1.0. Deactivated accounts and bots are excluded by default.

    When to Use:
    - Before mentioning someone (`<@U…>`), opening a DM (`slack_open_conversation`) or
      reading their profile (`slack_get_user`) when all you have is a name.
    - To disambiguate: two people with similar names are listed side by side.

    When NOT to Use:
    - When you have the email — `slack_lookup_user_by_email` is exact.
    - When you already have the id — `slack_get_user`.
    - It never sends anything: to message the person, pass the id to
      `slack_open_conversation` and then `slack_post_message` (both gated writes).

    Returns:
    A ranked markdown table (`Score | User | Title | Email`) of the top `limit` people
    scoring at least 0.50. When exactly ONE person scores ≥ 0.90 it is called out as the
    unique match with the `<@U…>` string to mention them (never for a query shorter than
    2 characters); several ≥ 0.90 are reported as ambiguous — confirm which one before
    writing. A PARTIAL directory (rate limited mid-walk, or past 20 pages) is said so,
    with the number of people it holds, and a unique match in it is labelled "Unique
    match in the PARTIAL index — confirm before writing". JSON mode returns the scores
    and ids plus `unique_match`.

    Examples:
    - `params = {"query": "Ricardo"}`
    - `params = {"query": "@ada", "limit": 3}`
    - `params = {"query": "Ada Lovelace", "refresh": true}`

    Error Handling:
    An empty query is rejected locally. `missing_scope` means the app lacks the USER
    scope `users:read`. A rate limit on the FIRST page is returned as the error (wait for
    Retry-After and call again; nothing is cached); a rate limit on a later page keeps
    the pages already read as a PARTIAL directory, which the next call re-reads from
    page 1 once it is a minute old (or pass `refresh=true`). A rate-limited re-read
    never replaces a complete (or larger) cached directory: the old one is kept and the
    response says so.
    """
    try:
        directory, notice = await _load_directory(params.refresh)
        pool, hidden_deleted, hidden_bots = _split_hidden(
            directory.members, include_deleted=params.include_deleted, include_bots=params.include_bots
        )
        query = _norm(params.query)
        scored = [(score, member) for member in pool if (score := _score(query, member)) >= MIN_SCORE]
        scored.sort(key=lambda pair: (-pair[0], _norm(fmt_user(pair[1])), str(pair[1].get("id") or "")))
        top = scored[: params.limit]
        confident = [member for score, member in scored if score >= UNIQUE_MATCH_SCORE]
        too_short = len(query) < MIN_UNIQUE_QUERY_LEN
        unique = confident[0] if len(confident) == 1 and not too_short else None

        if params.response_format is ResponseFormat.JSON:
            return clip_response(
                to_json(
                    {
                        "query": params.query,
                        "searched": len(pool),
                        "hidden_deleted": hidden_deleted,
                        "hidden_bots": hidden_bots,
                        "directory_complete": directory.complete,
                        "directory_stopped": directory.stopped,
                        "people_indexed": len(directory.members),
                        "directory_notice": notice,
                        "unique_match": unique.get("id") if unique else None,
                        "confident_matches": len(confident),
                        "matches": [
                            {
                                "score": round(score, 3),
                                "id": member.get("id"),
                                "name": member.get("name"),
                                "real_name": member.get("real_name") or _profile(member).get("real_name"),
                                "display_name": _profile(member).get("display_name"),
                                "title": _profile(member).get("title"),
                                "email": _profile(member).get("email"),
                                "deleted": bool(member.get("deleted")),
                                "is_bot": _is_bot(member),
                            }
                            for score, member in top
                        ],
                    }
                )
            )

        lines = [f'# Slack users matching "{params.query}"', ""]
        searched = f"Searched {len(pool):,} people"
        hidden = _hidden_note(hidden_deleted, hidden_bots, scope="from the search")
        lines.append(searched + "." if not hidden else f"{searched}. {hidden}")
        if directory.stopped == "rate_limited":
            lines.append(
                f"The directory index is PARTIAL: Slack rate-limited users.list after {directory.pages} page(s), so "
                f"it holds {len(directory.members):,} people — pass `refresh=true` in a minute to re-read it, or use "
                "`slack_lookup_user_by_email` if the person is missing."
            )
        elif not directory.complete:
            lines.append(
                f"The directory index is PARTIAL: it stops after {directory.pages} pages and holds "
                f"{len(directory.members):,} people — use `slack_lookup_user_by_email` if the person is missing."
            )
        if notice:
            lines.append(notice)
        lines.append("")
        if unique is not None:
            uid = str(unique.get("id") or "")
            label = (
                "**Unique match**"
                if directory.complete
                else "**Unique match in the PARTIAL index — confirm before writing**"
            )
            lines.append(f"{label}: {fmt_user(unique)} — mention as `{render_mention(uid)}`.")
            lines.append("")
        elif too_short and top:
            lines.append(
                f"**Too short for a unique match**: give at least {MIN_UNIQUE_QUERY_LEN} characters (a name or a "
                "handle) before using any of these ids."
            )
            lines.append("")
        elif len(confident) > 1:
            lines.append(
                f"**Ambiguous**: {len(confident)} people score ≥ {UNIQUE_MATCH_SCORE:.2f} — confirm which one "
                "before mentioning or messaging anyone."
            )
            lines.append("")
        elif top:
            lines.append(
                f"**No confident match** (none scores ≥ {UNIQUE_MATCH_SCORE:.2f}) — confirm the person before using "
                "any of these ids."
            )
            lines.append("")
        if not top:
            lines.append(
                f"_No one scores ≥ {MIN_SCORE:.2f}._ Try another spelling, `refresh=true` (if they joined after the "
                "directory was cached), the email (`slack_lookup_user_by_email`), or browse with `slack_list_users`."
            )
            return clip_response("\n".join(lines))
        lines.append("| Score | User | Title | Email |")
        lines.append("|---|---|---|---|")
        for score, member in top:
            profile = _profile(member)
            lines.append(
                f"| {score:.2f} | {md_cell(fmt_user(member))} | {md_cell(profile.get('title'))} | "
                f"{md_cell(profile.get('email'))} |"
            )
        return clip_response("\n".join(lines))
    except Exception as exc:
        return _with_scope_note(exc, "users:read", "users.list")
