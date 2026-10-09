"""The write guard: decides whether a mutating Slack call may target a conversation.

It lives in the spine and runs INSIDE the client for every method in
`client.WRITE_METHODS`, on the exact parameters that will go on the wire, so no tool
module can forget it. The decision table (first matching row wins):

1. `SLACK_ALLOW_WRITES` is off → `WritesDisabledError`.
2. `conversations.open` → it must name at least one id in `users` and no `channel`;
   every id other than your own (from `auth.test`) must be in `SLACK_WRITE_USERS`.
3. Any other write must name a `channel` that is a conversation ID (`C…`/`D…`/`G…`).
   A name such as `#general`, or a user id, is refused: Slack would resolve it
   server-side, after the allowlists were checked.
4. The ID is in `SLACK_WRITE_CHANNELS` → allowed, with no lookup. An explicit ID is the
   only way a Slack Connect channel can be written to.
5. Otherwise the conversation is looked up (`conversations.info`, cached). A channel
   shared outside the workspace (`is_ext_shared` / `is_shared` / `is_org_shared` /
   `is_pending_ext_shared`) is refused, even when it looks like a DM, and the message
   names the flag.
6. A DM or group DM passes when every OTHER member (`conversations.members`, plus the
   DM's `user`, minus your own id from `auth.test`) is in `SLACK_WRITE_USERS`. Your
   self-DM therefore passes once writes are on. Fail-closed corners: a DM whose `info`
   does not name its `user`, a conversation with no members, or a member list that is
   still paginating after `MAX_MEMBER_PAGES` pages is refused — never decided on a guess.
7. Anything else → `WriteNotAllowedError`, naming the conversation, what it is, and the
   env var that would allow it.

Two deliberate choices about Slack Connect. Row 5 refuses on all four flags, stricter
than research 02's "external = is_ext_shared || is_pending_ext_shared": `is_shared` and
`is_org_shared` (another workspace of the same Enterprise org) also mean people outside
this workspace read the conversation. And row 5 is NOT the real gate for DMs: Slack's
DM objects may omit `is_ext_shared` (02 O-22 — `conversations.list` IM entries carry
only `is_org_shared`), so a DM with an external person can look internal. For DMs and
group DMs the gate that holds is row 6 — the member allowlist: an external person is
only writable to if you put their user id in `SLACK_WRITE_USERS` yourself.

The lookups go through a READ-ONLY callable the client injects (it refuses write
methods), so the guard can neither recurse into itself nor write. Results are cached
per process with a short TTL: membership rarely changes between two tool calls, and
every write still hits Slack's own ACLs.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from slack_mcp.config import Settings
from slack_mcp.formatters import fmt_channel, shared_flags
from slack_mcp.validators import is_conversation_id

# How long a conversations.info / conversations.members answer is reused (seconds).
CACHE_TTL_SECONDS = 300.0

# conversations.members pages read before giving up (a DM/group DM has ≤ 9 members, so
# one page always suffices; past the cap the guard refuses rather than use a partial list).
MAX_MEMBER_PAGES = 10

# A read-only Web API call: `(method, params) -> parsed ok:true body`.
Fetch = Callable[[str, Mapping[str, Any] | None], Awaitable[dict[str, Any]]]


class WritesDisabledError(RuntimeError):
    """Raised for a write method while SLACK_ALLOW_WRITES is off."""


class WriteNotAllowedError(RuntimeError):
    """Raised for a write whose target is not covered by the allowlists."""


def normalise_users(value: object) -> list[str]:
    """`users` as a de-duplicated list of ids, from a list or a comma-joined string."""
    if value is None:
        return []
    if isinstance(value, str):
        raw: list[str] = value.split(",")
    elif isinstance(value, Sequence):
        raw = [str(item) for item in value]
    else:
        raw = [str(value)]
    seen: list[str] = []
    for part in raw:
        item = part.strip()
        if item and item not in seen:
            seen.append(item)
    return seen


class WriteGuard:
    """Allowlist + membership checks for mutating calls (see the module docstring)."""

    def __init__(self, settings: Settings, fetch: Fetch, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._settings = settings
        self._fetch = fetch
        self._clock = clock
        self._self_id: str | None = None
        self._info: dict[str, tuple[float, dict[str, Any]]] = {}
        self._members: dict[str, tuple[float, list[str]]] = {}

    # -- caches ----------------------------------------------------------------

    def clear_cache(self) -> None:
        """Forget your own id and every cached conversation lookup."""
        self._self_id = None
        self._info.clear()
        self._members.clear()

    def _fresh(self, stamp: float) -> bool:
        return self._clock() - stamp < CACHE_TTL_SECONDS

    async def self_user_id(self) -> str:
        """Your own user id, from `auth.test` (cached for the process: the token is fixed)."""
        if self._self_id is None:
            body = await self._fetch("auth.test", None)
            self._self_id = str(body.get("user_id") or "")
        return self._self_id

    async def conversation_info(self, channel: str) -> dict[str, Any]:
        """`conversations.info` → the `channel` object (cached for `CACHE_TTL_SECONDS`)."""
        cached = self._info.get(channel)
        if cached and self._fresh(cached[0]):
            return cached[1]
        body = await self._fetch("conversations.info", {"channel": channel})
        raw = body.get("channel")
        info: dict[str, Any] = raw if isinstance(raw, dict) else {}
        self._info[channel] = (self._clock(), info)
        return info

    async def conversation_members(self, channel: str) -> list[str]:
        """`conversations.members` → member ids (cached for `CACHE_TTL_SECONDS`).

        Raises `WriteNotAllowedError` when the list is still paginating after
        `MAX_MEMBER_PAGES` pages (a partial list is never used, nor cached).
        """
        cached = self._members.get(channel)
        if cached and self._fresh(cached[0]):
            return cached[1]
        members: list[str] = []
        cursor = ""
        for _ in range(MAX_MEMBER_PAGES):
            params: dict[str, Any] = {"channel": channel, "limit": 200}
            if cursor:
                params["cursor"] = cursor
            body = await self._fetch("conversations.members", params)
            members.extend(str(m) for m in body.get("members") or [])
            metadata = body.get("response_metadata")
            cursor = str(metadata.get("next_cursor") or "") if isinstance(metadata, dict) else ""
            if not cursor:
                break
        if cursor:
            raise WriteNotAllowedError(
                f"the write guard read {MAX_MEMBER_PAGES} pages of members of {channel} and Slack still had more; it "
                "refuses rather than decide on a partial list. List the id in SLACK_WRITE_CHANNELS to allow it."
            )
        self._members[channel] = (self._clock(), members)
        return members

    # -- the decision ------------------------------------------------------------

    async def check(self, method: str, params: Mapping[str, Any] | None) -> None:
        """Raise unless `method` with `params` may proceed. Returns silently when allowed."""
        settings = self._settings
        if not settings.slack_allow_writes:
            raise WritesDisabledError(
                f"{method} would change your Slack workspace AS YOU, but writes are disabled. Set SLACK_ALLOW_WRITES=1 "
                "(and list the allowed targets in SLACK_WRITE_CHANNELS / SLACK_WRITE_USERS) to allow posting, editing, "
                "scheduling, reacting and opening DMs. Reads work without it."
            )
        params = params or {}
        if method == "conversations.open":
            await self._check_open(method, params)
            return

        channel = params.get("channel")
        if not isinstance(channel, str) or not channel.strip():
            raise WriteNotAllowedError(f"{method} needs a `channel` id to be checked against the allowlists.")
        if not is_conversation_id(channel):
            raise WriteNotAllowedError(
                f"{method} to {channel!r} refused: writes must name a conversation ID (C…, D… or G…), never a "
                "#name or a user id. Find the id with slack_list_conversations (or open a DM with "
                "slack_open_conversation)."
            )
        if channel in settings.write_channels:
            return

        info = await self.conversation_info(channel)
        label = fmt_channel(info, fallback_id=channel)
        shared = shared_flags(info)
        if shared:
            raise WriteNotAllowedError(
                f"{method} to {label} refused: the conversation is shared outside this workspace (Slack Connect: "
                f"{', '.join(shared)}). Only an explicit id in SLACK_WRITE_CHANNELS allows writing to a shared "
                "conversation."
            )
        if info.get("is_im") or info.get("is_mpim"):
            kind = "DM" if info.get("is_im") else "group DM"
            other = info.get("user")
            if info.get("is_im") and not (isinstance(other, str) and other):
                # Without `user` a DM listing only you could be mistaken for your self-DM.
                raise WriteNotAllowedError(
                    f"{method} to DM {channel} refused: Slack did not say who the DM is with, so the guard cannot "
                    "check them against SLACK_WRITE_USERS. List its id in SLACK_WRITE_CHANNELS to allow it."
                )
            members = list(await self.conversation_members(channel))
            if isinstance(other, str) and other and other not in members:
                members.append(other)
            if not members:
                raise WriteNotAllowedError(
                    f"{method} to {kind} {channel} refused: Slack returned no members for it, so the guard cannot "
                    "check them against SLACK_WRITE_USERS. List its id in SLACK_WRITE_CHANNELS to allow it."
                )
            await self._check_users(method, members, label=f"{kind} {channel}")
            return
        raise WriteNotAllowedError(
            f"{method} to {label} refused: the channel is not in SLACK_WRITE_CHANNELS. Add its id ({channel}) to "
            "that list to allow writes there."
        )

    async def _check_open(self, method: str, params: Mapping[str, Any]) -> None:
        if params.get("channel") is not None:
            raise WriteNotAllowedError(
                f"{method} refused: this server opens conversations by user ids (`users`) only, never by `channel`."
            )
        users = normalise_users(params.get("users"))
        if not users:
            raise WriteNotAllowedError(f"{method} needs at least one user id in `users`.")
        await self._check_users(method, users)

    async def _check_users(self, method: str, users: Sequence[str], *, label: str | None = None) -> None:
        allowed = set(self._settings.write_users)
        others = [uid for uid in users if uid not in allowed]
        if others:
            me = await self.self_user_id()
            others = [uid for uid in others if uid != me]
        if not others:
            return
        target = label or ("the users " + ", ".join(users))
        raise WriteNotAllowedError(
            f"{method} to {target} refused: {', '.join(others)} "
            f"{'is' if len(others) == 1 else 'are'} not in SLACK_WRITE_USERS. Add the id(s) to that list to allow "
            "writing to conversations with them."
        )
