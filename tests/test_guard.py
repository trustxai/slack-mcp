"""Unit tests for the write guard's decision table (see guard.py's module docstring)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from slack_mcp.config import Settings
from slack_mcp.guard import (
    CACHE_TTL_SECONDS,
    MAX_MEMBER_PAGES,
    WriteGuard,
    WriteNotAllowedError,
    WritesDisabledError,
    normalise_users,
)

ME = "U0SELF00001"
ANA = "U0ANA000001"
BOB = "U0BOB000001"
EVE = "U0EVE000001"  # never allowlisted


class _FakeFetch:
    """Answers the guard's read calls from a route table and records them."""

    def __init__(self, routes: dict[str, Any] | None = None) -> None:
        self.routes: dict[str, Any] = {"auth.test": {"ok": True, "user_id": ME}, **(routes or {})}
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    @property
    def methods(self) -> list[str]:
        return [method for method, _ in self.calls]

    async def __call__(self, method: str, params: Mapping[str, Any] | None) -> dict[str, Any]:
        self.calls.append((method, dict(params) if params is not None else None))
        route = self.routes[method]
        if isinstance(route, Exception):
            raise route
        if isinstance(route, list):  # successive pages
            return dict(route.pop(0))
        return dict(route)


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _guard(fetch: _FakeFetch, *, clock: _Clock | None = None, **settings_kwargs: Any) -> WriteGuard:
    settings_kwargs.setdefault("slack_allow_writes", True)
    settings = Settings(slack_user_token="xoxp-FAKE-0123456789abcdef", **settings_kwargs)
    if clock is None:
        return WriteGuard(settings, fetch)
    return WriteGuard(settings, fetch, clock=clock)


def _info(channel: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "channel": channel}


def _members(*ids: str) -> dict[str, Any]:
    return {"ok": True, "members": list(ids), "response_metadata": {"next_cursor": ""}}


# --- row 1: the kill-switch ---------------------------------------------------------------


@pytest.mark.parametrize("method", ["chat.postMessage", "conversations.open", "reactions.add", "chat.update"])
async def test_writes_off_refuses_everything_without_a_lookup(method: str) -> None:
    fetch = _FakeFetch()
    guard = _guard(fetch, slack_allow_writes=False, slack_write_channels="C0123ABCDEF", slack_write_users=ANA)
    with pytest.raises(WritesDisabledError, match="SLACK_ALLOW_WRITES=1"):
        await guard.check(method, {"channel": "C0123ABCDEF", "users": ANA})
    assert fetch.calls == []


# --- row 2: conversations.open --------------------------------------------------------------


async def test_open_with_every_user_allowed_passes_without_a_lookup() -> None:
    fetch = _FakeFetch()
    await _guard(fetch, slack_write_users=f"{ANA},{BOB}").check("conversations.open", {"users": [ANA, BOB]})
    assert fetch.calls == []


async def test_open_with_one_user_not_allowed_is_refused_naming_them() -> None:
    fetch = _FakeFetch()
    with pytest.raises(WriteNotAllowedError) as info:
        await _guard(fetch, slack_write_users=ANA).check("conversations.open", {"users": [ANA, EVE]})
    message = str(info.value)
    assert f"{EVE} is not in SLACK_WRITE_USERS" in message
    assert f"{ANA} is" not in message
    assert fetch.methods == ["auth.test"]  # it checked whether EVE was you first


async def test_open_excludes_your_own_id_via_auth_test() -> None:
    fetch = _FakeFetch()
    await _guard(fetch, slack_write_users=ANA).check("conversations.open", {"users": [ME, ANA]})
    assert fetch.methods == ["auth.test"]


async def test_open_your_self_dm_passes_with_an_empty_allowlist() -> None:
    fetch = _FakeFetch()
    await _guard(fetch).check("conversations.open", {"users": ME})
    assert fetch.methods == ["auth.test"]


async def test_open_with_a_comma_joined_users_string() -> None:
    fetch = _FakeFetch()
    guard = _guard(fetch, slack_write_users=f"{ANA},{BOB}")
    await guard.check("conversations.open", {"users": f"{ANA},{BOB}"})
    with pytest.raises(WriteNotAllowedError, match="are not in SLACK_WRITE_USERS") as info:
        await guard.check("conversations.open", {"users": f"{ANA}, {EVE},U0ZED000001"})
    assert f"{EVE}, U0ZED000001 are not" in str(info.value)


async def test_open_without_users_or_by_channel_is_refused() -> None:
    fetch = _FakeFetch()
    guard = _guard(fetch, slack_write_users=ANA)
    with pytest.raises(WriteNotAllowedError, match="at least one user id"):
        await guard.check("conversations.open", {})
    with pytest.raises(WriteNotAllowedError, match="at least one user id"):
        await guard.check("conversations.open", {"users": " , "})
    with pytest.raises(WriteNotAllowedError, match="never by `channel`"):
        await guard.check("conversations.open", {"channel": "D0123ABCDEF", "users": ANA})
    assert fetch.calls == []


# --- row 3: IDs only ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "channel", ["#general", "general", "U0ANA000001", "c0123abcdef", " C0123ABCDEF", "C0123ABCDEF\n"]
)
async def test_a_channel_that_is_not_a_conversation_id_is_refused(channel: str) -> None:
    fetch = _FakeFetch()
    guard = _guard(fetch, slack_write_channels="#general,general,C0123ABCDEF", slack_write_users=ANA)
    with pytest.raises(WriteNotAllowedError, match="conversation ID"):
        await guard.check("chat.postMessage", {"channel": channel})
    assert fetch.calls == []


@pytest.mark.parametrize("params", [{}, {"channel": ""}, {"channel": None}, {"channel": ["C0123ABCDEF"]}, None])
async def test_a_write_without_a_channel_is_refused(params: dict[str, Any] | None) -> None:
    fetch = _FakeFetch()
    with pytest.raises(WriteNotAllowedError, match="needs a `channel` id"):
        await _guard(fetch).check("reactions.add", params)
    assert fetch.calls == []


# --- row 4: SLACK_WRITE_CHANNELS short-circuits -----------------------------------------------


async def test_listed_channel_short_circuits_with_no_fetch() -> None:
    fetch = _FakeFetch()
    await _guard(fetch, slack_write_channels="C0999ZZZZZZ,C0123ABCDEF").check(
        "chat.postMessage", {"channel": "C0123ABCDEF"}
    )
    assert fetch.calls == []


async def test_a_listed_slack_connect_channel_passes_by_explicit_id() -> None:
    fetch = _FakeFetch({"conversations.info": _info({"id": "C0EXT000001", "is_ext_shared": True})})
    await _guard(fetch, slack_write_channels="C0EXT000001").check("chat.postMessage", {"channel": "C0EXT000001"})
    assert fetch.calls == []  # never even looked up


# --- row 5: Slack Connect refusal -------------------------------------------------------------


@pytest.mark.parametrize("flag", ["is_ext_shared", "is_shared", "is_org_shared", "is_pending_ext_shared"])
async def test_shared_channel_is_refused_naming_the_flag(flag: str) -> None:
    fetch = _FakeFetch({"conversations.info": _info({"id": "C0EXT000001", "name": "partners", flag: True})})
    with pytest.raises(WriteNotAllowedError) as info:
        await _guard(fetch).check("chat.postMessage", {"channel": "C0EXT000001"})
    message = str(info.value)
    assert f"Slack Connect: {flag}" in message
    assert "#partners (C0EXT000001)" in message
    assert "Only an explicit id in SLACK_WRITE_CHANNELS" in message
    assert fetch.methods == ["conversations.info"]


@pytest.mark.parametrize("value", [1, "true", "yes"])
async def test_a_truthy_non_boolean_sharing_flag_is_refused(value: object) -> None:
    fetch = _FakeFetch({"conversations.info": _info({"id": "C0EXT000001", "name": "partners", "is_ext_shared": value})})
    with pytest.raises(WriteNotAllowedError, match="Slack Connect: is_ext_shared"):
        await _guard(fetch).check("chat.postMessage", {"channel": "C0EXT000001"})


async def test_shared_flags_refuse_even_an_im_looking_conversation() -> None:
    fetch = _FakeFetch(
        {
            "conversations.info": _info({"id": "D0EXT000001", "is_im": True, "user": ANA, "is_ext_shared": True}),
            "conversations.members": _members(ME, ANA),
        }
    )
    with pytest.raises(WriteNotAllowedError, match="Slack Connect: is_ext_shared"):
        await _guard(fetch, slack_write_users=ANA).check("chat.postMessage", {"channel": "D0EXT000001"})
    assert fetch.methods == ["conversations.info"]  # refused before the membership path


# --- row 6: DMs and group DMs -------------------------------------------------------------------


async def test_im_with_the_other_member_allowed_passes() -> None:
    fetch = _FakeFetch(
        {
            "conversations.info": _info({"id": "D0123ABCDEF", "is_im": True, "user": ANA}),
            "conversations.members": _members(ME, ANA),
        }
    )
    await _guard(fetch, slack_write_users=ANA).check("chat.postMessage", {"channel": "D0123ABCDEF"})
    assert fetch.methods == ["conversations.info", "conversations.members", "auth.test"]
    assert fetch.calls[1] == ("conversations.members", {"channel": "D0123ABCDEF", "limit": 200})


async def test_im_with_the_other_member_not_allowed_is_refused() -> None:
    fetch = _FakeFetch(
        {
            "conversations.info": _info({"id": "D0123ABCDEF", "is_im": True, "user": EVE}),
            "conversations.members": _members(ME, EVE),
        }
    )
    with pytest.raises(WriteNotAllowedError) as info:
        await _guard(fetch, slack_write_users=ANA).check("chat.postMessage", {"channel": "D0123ABCDEF"})
    assert str(info.value).startswith(f"chat.postMessage to DM D0123ABCDEF refused: {EVE} is not in SLACK_WRITE_USERS")


async def test_im_counts_the_dm_user_even_if_members_omits_them() -> None:
    fetch = _FakeFetch(
        {
            "conversations.info": _info({"id": "D0123ABCDEF", "is_im": True, "user": EVE}),
            "conversations.members": _members(ME),
        }
    )
    with pytest.raises(WriteNotAllowedError, match=f"{EVE} is not in SLACK_WRITE_USERS"):
        await _guard(fetch).check("chat.postMessage", {"channel": "D0123ABCDEF"})


async def test_group_dm_with_no_members_is_refused() -> None:
    fetch = _FakeFetch(
        {"conversations.info": _info({"id": "G0MPIM00001", "is_mpim": True}), "conversations.members": _members()}
    )
    with pytest.raises(WriteNotAllowedError, match="returned no members"):
        await _guard(fetch, slack_write_users=ANA).check("chat.postMessage", {"channel": "G0MPIM00001"})


@pytest.mark.parametrize("user", [None, ""])
async def test_im_whose_info_does_not_name_its_user_is_refused(user: str | None) -> None:
    # Members [me] must not be mistaken for the self-DM when Slack does not say who the DM is with.
    channel: dict[str, Any] = {"id": "D0123ABCDEF", "is_im": True}
    if user is not None:
        channel["user"] = user
    fetch = _FakeFetch({"conversations.info": _info(channel), "conversations.members": _members(ME)})
    with pytest.raises(WriteNotAllowedError, match="did not say who the DM is with"):
        await _guard(fetch, slack_write_users=ANA).check("chat.postMessage", {"channel": "D0123ABCDEF"})
    assert fetch.methods == ["conversations.info"]


async def test_members_still_paginating_after_the_cap_is_refused_and_not_cached() -> None:
    page = {"ok": True, "members": [ME], "response_metadata": {"next_cursor": "more=="}}
    fetch = _FakeFetch(
        {
            "conversations.info": _info({"id": "G0MPIM00001", "is_mpim": True}),
            "conversations.members": [page] * (MAX_MEMBER_PAGES * 2),
        }
    )
    guard = _guard(fetch, slack_write_users=ANA)
    with pytest.raises(WriteNotAllowedError, match="partial list"):
        await guard.check("chat.postMessage", {"channel": "G0MPIM00001"})
    assert fetch.methods.count("conversations.members") == MAX_MEMBER_PAGES
    assert "auth.test" not in fetch.methods  # nothing was decided on the partial list
    with pytest.raises(WriteNotAllowedError, match="partial list"):
        await guard.check("chat.postMessage", {"channel": "G0MPIM00001"})
    assert fetch.methods.count("conversations.members") == MAX_MEMBER_PAGES * 2  # the partial list was not cached


async def test_self_dm_passes() -> None:
    fetch = _FakeFetch(
        {
            "conversations.info": _info({"id": "D0SELF00001", "is_im": True, "user": ME}),
            "conversations.members": _members(ME),
        }
    )
    await _guard(fetch).check("chat.postMessage", {"channel": "D0SELF00001"})
    assert fetch.methods == ["conversations.info", "conversations.members", "auth.test"]


async def test_mpim_with_both_others_allowed_passes() -> None:
    fetch = _FakeFetch(
        {
            "conversations.info": _info({"id": "G0MPIM00001", "is_mpim": True}),
            "conversations.members": _members(ME, ANA, BOB),
        }
    )
    await _guard(fetch, slack_write_users=f"{ANA},{BOB}").check("chat.postMessage", {"channel": "G0MPIM00001"})


async def test_mpim_with_one_other_missing_is_refused() -> None:
    fetch = _FakeFetch(
        {
            "conversations.info": _info({"id": "G0MPIM00001", "is_mpim": True}),
            "conversations.members": _members(ME, ANA, EVE),
        }
    )
    with pytest.raises(WriteNotAllowedError) as info:
        await _guard(fetch, slack_write_users=ANA).check("chat.postMessage", {"channel": "G0MPIM00001"})
    assert f"group DM G0MPIM00001 refused: {EVE} is not in SLACK_WRITE_USERS" in str(info.value)


async def test_members_are_paged() -> None:
    fetch = _FakeFetch(
        {
            "conversations.info": _info({"id": "G0MPIM00001", "is_mpim": True}),
            "conversations.members": [
                {"ok": True, "members": [ME, ANA], "response_metadata": {"next_cursor": "next=="}},
                {"ok": True, "members": [EVE]},
            ],
        }
    )
    with pytest.raises(WriteNotAllowedError, match=EVE):
        await _guard(fetch, slack_write_users=ANA).check("reactions.add", {"channel": "G0MPIM00001"})
    assert fetch.calls[2] == ("conversations.members", {"channel": "G0MPIM00001", "limit": 200, "cursor": "next=="})


# --- row 7: everything else -------------------------------------------------------------------


async def test_plain_channel_not_listed_is_refused_naming_the_env_var() -> None:
    fetch = _FakeFetch({"conversations.info": _info({"id": "C0123ABCDEF", "name": "general", "is_channel": True})})
    with pytest.raises(WriteNotAllowedError) as info:
        await _guard(fetch, slack_write_users=ANA).check("chat.update", {"channel": "C0123ABCDEF"})
    assert str(info.value) == (
        "chat.update to #general (C0123ABCDEF) refused: the channel is not in SLACK_WRITE_CHANNELS. Add its id "
        "(C0123ABCDEF) to that list to allow writes there."
    )


async def test_private_channel_not_listed_is_refused() -> None:
    fetch = _FakeFetch({"conversations.info": _info({"id": "G0PRIV00001", "name": "secret", "is_private": True})})
    with pytest.raises(WriteNotAllowedError, match="not in SLACK_WRITE_CHANNELS"):
        await _guard(fetch).check("chat.postMessage", {"channel": "G0PRIV00001"})


async def test_an_info_body_without_a_channel_fails_closed() -> None:
    fetch = _FakeFetch({"conversations.info": {"ok": True}})
    with pytest.raises(WriteNotAllowedError, match="conversation C0123ABCDEF refused"):
        await _guard(fetch).check("chat.postMessage", {"channel": "C0123ABCDEF"})


async def test_a_lookup_failure_propagates_and_nothing_is_allowed() -> None:
    fetch = _FakeFetch({"conversations.info": RuntimeError("channel_not_found")})
    with pytest.raises(RuntimeError, match="channel_not_found"):
        await _guard(fetch).check("chat.postMessage", {"channel": "C0123ABCDEF"})


# --- the cache ----------------------------------------------------------------------------------


async def test_info_cache_hits_within_ttl_and_misses_after() -> None:
    clock = _Clock()
    fetch = _FakeFetch({"conversations.info": _info({"id": "C0123ABCDEF", "name": "general"})})
    guard = _guard(fetch, clock=clock)

    for _ in range(2):
        with pytest.raises(WriteNotAllowedError):
            await guard.check("chat.postMessage", {"channel": "C0123ABCDEF"})
    assert fetch.methods == ["conversations.info"]

    clock.now += CACHE_TTL_SECONDS - 1
    with pytest.raises(WriteNotAllowedError):
        await guard.check("chat.postMessage", {"channel": "C0123ABCDEF"})
    assert fetch.methods == ["conversations.info"]

    clock.now += 2
    with pytest.raises(WriteNotAllowedError):
        await guard.check("chat.postMessage", {"channel": "C0123ABCDEF"})
    assert fetch.methods == ["conversations.info", "conversations.info"]


async def test_members_and_self_id_are_cached() -> None:
    clock = _Clock()
    fetch = _FakeFetch(
        {
            "conversations.info": _info({"id": "D0123ABCDEF", "is_im": True, "user": ANA}),
            "conversations.members": _members(ME, ANA),
        }
    )
    guard = _guard(fetch, clock=clock, slack_write_users=ANA)
    await guard.check("chat.postMessage", {"channel": "D0123ABCDEF"})
    await guard.check("chat.postMessage", {"channel": "D0123ABCDEF"})
    assert fetch.methods == ["conversations.info", "conversations.members", "auth.test"]
    assert await guard.self_user_id() == ME
    assert fetch.methods.count("auth.test") == 1


async def test_clear_cache_forgets_everything() -> None:
    fetch = _FakeFetch(
        {
            "conversations.info": _info({"id": "D0123ABCDEF", "is_im": True, "user": ANA}),
            "conversations.members": _members(ME, ANA),
        }
    )
    guard = _guard(fetch, slack_write_users=ANA)
    await guard.check("chat.postMessage", {"channel": "D0123ABCDEF"})
    guard.clear_cache()
    await guard.check("chat.postMessage", {"channel": "D0123ABCDEF"})
    assert fetch.methods == ["conversations.info", "conversations.members", "auth.test"] * 2


# --- helpers ------------------------------------------------------------------------------------


def test_normalise_users() -> None:
    assert normalise_users(None) == []
    assert normalise_users("") == []
    assert normalise_users(f" {ANA}, {BOB} ,,{ANA}") == [ANA, BOB]
    assert normalise_users([ANA, f" {BOB} ", "", ANA]) == [ANA, BOB]
    assert normalise_users((ANA,)) == [ANA]
