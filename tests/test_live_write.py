"""Opt-in LIVE write smoke — one ordered scenario in YOUR OWN self-DM, nowhere else.

Runs ONLY as (conftest gate: token set, the switch exported in the SHELL, bare `-m live_write`):

    SLACK_TEST_ALLOW_WRITES=1 uv run pytest -m live_write -rA

`-rA` prints the captured stdout of passed tests, which is where the measured facts and the
final "LIVE WRITE SMOKE RESULT" block land (paste that block into STATE.md).

What it does, through the REAL stack (Settings from the environment, the real client and
its write guard, the real tool functions — no fakes):

1. `slack_health_check` + `auth.test` → your own user id.
2. `slack_open_conversation([you])` → your self-DM `D…` id (research/02 O-4, O-23).
3. In that DM only: post (mrkdwn, with a mention and a link) → post (markdown) → reply in
   the thread → update the first post → react, react again (`already_reacted`), unreact →
   schedule a message 2 minutes ahead → read it back with get_thread / get_history.
4. Length probes at the 4,000-character boundary, confined to the markdown post's thread (O-2).
5. `slack_search_messages` for the run's nonce (index lag → XFAIL, not a failure).

Writes are forced ON with `SLACK_WRITE_CHANNELS` and `SLACK_WRITE_USERS` both EMPTY: the
self-DM must pass with no allowlist entry (guard row 6: "your self-DM therefore passes once
writes are on"; row 5's Slack Connect refusal must not fire for it). No channel id and no
other person's id is ever used. Nothing is deleted — there is no delete tool — so the
thread stays readable in the Slack client from the printed permalinks.

O-3 is answered (measured 2026-10-10, internal app, user token, a self-DM and a channel):
Slack shows NO native app label under these posts — they look exactly like ones you typed.
`SLACK_SIGNATURE` is what tells them apart. Step 6 edits the post from its text AS SLACK
STORED IT (read back with `slack_get_thread`, so a signed post comes back signed and
entity-encoded), which is the edit flow that must not double-sign. When the signature is
set for the run (shell or .env), step 9 asserts that the post (after that edit) and the
thread reply end with the signature line, and that it appears exactly twice in the thread
(once each); unset, that check is skipped and the result block says so.
"""

from __future__ import annotations

import asyncio
import html
import json
import os
import re
import secrets
import time
from collections.abc import Iterator
from typing import Any

import pytest

from slack_mcp.client import SlackClient, get_client, reset_client
from slack_mcp.config import get_settings
from slack_mcp.errors import SlackApiError
from slack_mcp.formatters import MESSAGE_CONTINUATION, ResponseFormat
from slack_mcp.tools.chat import (
    PostMessageInput,
    ReplyInThreadInput,
    ScheduleMessageInput,
    UpdateMessageInput,
    slack_post_message,
    slack_reply_in_thread,
    slack_schedule_message,
    slack_update_message,
)
from slack_mcp.tools.conversations import (
    GetConversationInput,
    GetHistoryInput,
    GetThreadInput,
    OpenConversationInput,
    slack_get_conversation,
    slack_get_history,
    slack_get_thread,
    slack_open_conversation,
)
from slack_mcp.tools.health import slack_health_check
from slack_mcp.tools.reactions import ReactionInput, slack_add_reaction, slack_remove_reaction
from slack_mcp.tools.search import SearchMessagesInput, slack_search_messages

pytestmark = pytest.mark.live_write

SCHEDULE_AHEAD_SECONDS = 120
SEARCH_POLL_SECONDS = 20.0
SEARCH_POLL_INTERVAL_SECONDS = 2.0
REACTION = "white_check_mark"
EDIT_MARKER = "[EDITED by slack_update_message]"
REPLY_MARKER = "[thread reply by slack_reply_in_thread]"
SECOND_LINE = "Second line: slack_get_thread must render this as a continuation."

# Shared across the ordered tests of this module (pytest runs them in file order).
_RUN: dict[str, str] = {}
_ANSWERS: dict[str, str] = {}


@pytest.fixture
def live_stack(monkeypatch: pytest.MonkeyPatch) -> Iterator[SlackClient]:
    """The real Settings + client, with writes ON and BOTH allowlists empty."""
    if not os.environ.get("SLACK_USER_TOKEN"):
        pytest.skip("live_write needs SLACK_USER_TOKEN (shell or .env)")
    monkeypatch.setenv("SLACK_ALLOW_WRITES", "1")
    monkeypatch.setenv("SLACK_WRITE_CHANNELS", "")
    monkeypatch.setenv("SLACK_WRITE_USERS", "")
    get_settings.cache_clear()
    reset_client()
    settings = get_settings()
    assert settings.slack_allow_writes, "setup: SLACK_ALLOW_WRITES=1 did not reach Settings"
    assert settings.write_channels == [], f"setup: write_channels must be empty, got {settings.write_channels}"
    assert settings.write_users == [], f"setup: write_users must be empty, got {settings.write_users}"
    yield get_client()
    get_settings.cache_clear()
    reset_client()


def _field(output: str, name: str) -> str:
    """The first token after `**name**:` in a tool's markdown confirmation ('' if absent)."""
    match = re.search(rf"\*\*{re.escape(name)}\*\*: (\S+)", output)
    return match.group(1) if match else ""


def _line(output: str, needle: str) -> str:
    """The first line of `output` containing `needle` (stripped), or '' if none does."""
    return next((line.strip() for line in output.splitlines() if needle in line), "")


def _need(*keys: str) -> None:
    missing = [key for key in keys if not _RUN.get(key)]
    if missing:
        pytest.skip(f"the self-DM scenario did not record {', '.join(missing)} (it failed or was skipped earlier)")


def _sized(label: str, size: int) -> str:
    head = f"O-2 probe ({label}, {size} chars): "
    return head + ("0123456789" * (size // 10 + 1))[: size - len(head)]


async def test_1_self_dm_write_scenario(live_stack: SlackClient) -> None:
    client = live_stack
    nonce = f"slackmcpt13{secrets.token_hex(5)}"
    _RUN["nonce"] = nonce

    # -- step 1: health + auth.test → my own user id -----------------------------------
    health = await slack_health_check()
    print("== slack_health_check ==\n" + health)
    assert "connectivity**: OK" in health, f"step 1 health: connectivity was not OK:\n{health}"
    assert "ENABLED — 0 allowed channel id(s), 0 allowed user id(s)" in health, (
        f"step 1 health: writes must read ENABLED with BOTH allowlists empty:\n{health}"
    )
    me: dict[str, Any] = await client.call("auth.test")
    my_id = str(me.get("user_id") or "")
    assert re.fullmatch(r"[UW][A-Z0-9]+", my_id), f"step 1 auth.test: no usable user_id, got {my_id!r}"
    assert f"({my_id})" in health, f"step 1 health: the 'you' line does not name auth.test's {my_id}:\n{health}"
    _RUN["me"] = my_id
    _RUN["scopes"] = client.last_scopes or "N/A"
    _ANSWERS["O-11"] = (
        f"token is a user token (health: {_line(health, '**token**') or 'N/A'}); auth.test bot_id="
        f"{me.get('bot_id') or 'absent'} — the install yielded a working User OAuth Token "
        "(whether the button needed a bot user is a human observation)"
    )

    # -- step 2: open my self-DM with ONE id (mine) --------------------------------------
    opened = await slack_open_conversation(OpenConversationInput(users=[my_id]))
    accepted = client.last_accepted_scopes or "N/A"
    print("== slack_open_conversation([me]) ==\n" + opened + f"\n(x-accepted-oauth-scopes: {accepted})")
    dm = _field(opened, "conversation id")
    assert dm.startswith("D"), (
        f"step 2 O-4: conversations.open with ONE id (your own) must return a D… self-DM id, got {dm!r}:\n{opened}"
    )
    _RUN["dm"] = dm
    _ANSWERS["O-4"] = f"conversations.open with ONE id (your own) → {dm} (ok; no not_enough_users)"
    _ANSWERS["O-23"] = (
        f"conversations.open succeeded with the granted scopes [{_RUN['scopes']}]; "
        f"Slack's x-accepted-oauth-scopes for conversations.open = [{accepted}]"
    )

    # O-22 baseline: what conversations.info / users.info say for an INTERNAL DM (a Slack
    # Connect peer is needed to answer the external half).
    info: dict[str, Any] = await client.call("conversations.info", {"channel": dm})
    channel = info.get("channel") or {}
    flag_names = ("is_im", "is_ext_shared", "is_shared", "is_org_shared", "is_pending_ext_shared")
    flags = {key: channel.get(key) for key in flag_names}
    profile: dict[str, Any] = await client.call("users.info", {"user": my_id})
    user = profile.get("user") or {}
    conv_md = await slack_get_conversation(GetConversationInput(channel=dm))
    assert "Slack Connect:" in conv_md, f"step 2 get_conversation: no 'Slack Connect:' line for {dm}:\n{conv_md}"
    _ANSWERS["O-22"] = (
        f"baseline only (self-DM, internal): conversations.info flags {flags}; users.info team_id="
        f"{user.get('team_id')} vs auth.test team_id={me.get('team_id')}, is_stranger={user.get('is_stranger')}; "
        f"tool line: {_line(conv_md, 'Slack Connect:')} — a DM with a Slack Connect user is still needed"
    )

    # -- step 3: post (mrkdwn) with a mention and a link ---------------------------------
    first_text = (
        f"slack-mcp t13 live write smoke {nonce}: *mrkdwn bold*, a mention of <@{my_id}> "
        f"and a link <https://example.com|example.com link>.\n{SECOND_LINE}"
    )
    posted = await slack_post_message(PostMessageInput(channel=dm, text=first_text))
    print("== slack_post_message (mrkdwn) ==\n" + posted)
    assert "SENT — Slack accepted the message" in posted, f"step 3 post mrkdwn: not SENT:\n{posted}"
    ts1 = _field(posted, "ts")
    assert re.fullmatch(r"\d+\.\d+", ts1), f"step 3 post mrkdwn: no ts in the confirmation:\n{posted}"
    assert _field(posted, "channel") == dm, f"step 3 post mrkdwn: Slack answered for another channel:\n{posted}"
    _RUN["ts1"] = ts1
    _RUN["permalink1"] = _field(posted, "permalink")
    signature = get_settings().signature
    _ANSWERS["O-3"] = (
        "answered 2026-10-10: Slack shows no native app label under user-token posts (they look exactly like "
        "typed ones); SLACK_SIGNATURE is what differentiates them — "
        + (
            f"set for this run ({len(signature)} chars): see the signature line below"
            if signature
            else "NOT set for this run, so these posts carry no marker"
        )
    )

    # -- step 4: post (markdown) ---------------------------------------------------------
    md_text = f"slack-mcp t13 markdown post {nonce}: **markdown bold** and a [markdown link](https://example.com)."
    posted_md = await slack_post_message(PostMessageInput(channel=dm, text=md_text, format="markdown"))
    print("== slack_post_message (markdown) ==\n" + posted_md)
    assert "SENT — Slack accepted the message" in posted_md, f"step 4 post markdown: not SENT:\n{posted_md}"
    _RUN["md_ts"] = _field(posted_md, "ts")
    _RUN["permalink_md"] = _field(posted_md, "permalink")
    assert re.fullmatch(r"\d+\.\d+", _RUN["md_ts"]), f"step 4 post markdown: no ts:\n{posted_md}"

    # -- step 5: reply in the first post's thread ----------------------------------------
    replied = await slack_reply_in_thread(ReplyInThreadInput(channel=dm, thread_ts=ts1, text=f"{REPLY_MARKER} {nonce}"))
    print("== slack_reply_in_thread ==\n" + replied)
    assert "SENT — Slack accepted the message" in replied, f"step 5 reply: not SENT:\n{replied}"
    assert _field(replied, "thread_ts") == ts1, f"step 5 reply: Slack did not place it under {ts1}:\n{replied}"
    reply_ts = _field(replied, "ts")
    assert re.fullmatch(r"\d+\.\d+", reply_ts), f"step 5 reply: no ts:\n{replied}"
    _RUN["reply_ts"] = reply_ts
    _RUN["permalink_reply"] = _field(replied, "permalink")

    # -- step 6: update the first post (keeps the mention, the link and the second line) --
    # Edit the text AS SLACK STORED IT, read back with slack_get_thread: with SLACK_SIGNATURE
    # set it already ends with the signature (entity-encoded where Slack encodes `&`), and the
    # edit must not sign it a second time.
    stored = await slack_get_thread(
        GetThreadInput(channel=dm, thread_ts=ts1, limit=5, response_format=ResponseFormat.JSON)
    )
    stored_items = json.loads(stored).get("items") or []
    read_back = next((str(item.get("text") or "") for item in stored_items if item.get("ts") == ts1), "")
    assert f"smoke {nonce}:" in read_back, f"step 6 read-back: the post's stored text was not found:\n{stored}"
    edited_text = read_back.replace(f"smoke {nonce}:", f"smoke {nonce} {EDIT_MARKER}:", 1)
    updated = await slack_update_message(UpdateMessageInput(channel=dm, ts=ts1, text=edited_text))
    print("== slack_update_message ==\n" + updated)
    assert updated.startswith("# Message updated"), f"step 6 update: not confirmed:\n{updated}"
    assert EDIT_MARKER in updated, f"step 6 update: Slack's stored text lacks the new text:\n{updated}"
    _RUN["mention_kept"] = str(f"<@{my_id}>" in updated)
    _RUN["link_kept"] = str("<https://example.com|example.com link>" in updated)

    # -- step 7: react, react again (already_reacted is an outcome), unreact -------------
    reaction = ReactionInput(channel=dm, timestamp=ts1, name=REACTION)
    added = await slack_add_reaction(reaction)
    assert added.startswith(f"Added reaction :{REACTION}:"), f"step 7 add reaction: not added:\n{added}"
    again = await slack_add_reaction(reaction)
    assert "already_reacted" in again and not again.startswith("Error"), (
        f"step 7 add reaction twice: expected the already_reacted outcome sentence, got:\n{again}"
    )
    removed = await slack_remove_reaction(reaction)
    assert removed.startswith(f"Removed your reaction :{REACTION}:"), f"step 7 remove reaction: not removed:\n{removed}"
    print("== reactions ==\n" + "\n".join((added, again, removed)))

    # -- step 8: schedule a message 2 minutes ahead --------------------------------------
    post_at = int(time.time()) + SCHEDULE_AHEAD_SECONDS
    scheduled = await slack_schedule_message(
        ScheduleMessageInput(
            channel=dm,
            post_at=post_at,
            text=f"slack-mcp t13 scheduled message {nonce} (chat.scheduleMessage, {SCHEDULE_AHEAD_SECONDS}s ahead)",
        )
    )
    print("== slack_schedule_message ==\n" + scheduled)
    assert scheduled.startswith("# Message scheduled"), f"step 8 schedule: not confirmed:\n{scheduled}"
    scheduled_id = _field(scheduled, "scheduled_message_id")
    assert scheduled_id and scheduled_id != "N/A", f"step 8 schedule: no scheduled_message_id:\n{scheduled}"
    _RUN["scheduled_id"] = scheduled_id
    _RUN["post_at"] = str(post_at)

    # -- step 9: read back the thread ----------------------------------------------------
    thread = await slack_get_thread(GetThreadInput(channel=dm, thread_ts=ts1, limit=20))
    print("== slack_get_thread ==\n" + thread)
    assert thread.startswith("# Thread"), f"step 9 get_thread: unexpected output:\n{thread}"
    assert EDIT_MARKER in thread, f"step 9 get_thread: the parent does not show the edited text:\n{thread}"
    assert f"(ts {reply_ts})" in thread and REPLY_MARKER in thread, f"step 9 get_thread: reply missing:\n{thread}"
    assert f"{MESSAGE_CONTINUATION}{SECOND_LINE}" in thread, (
        f"step 9 get_thread: the second line is not rendered as a {MESSAGE_CONTINUATION!r} continuation:\n{thread}"
    )
    # SLACK_SIGNATURE (O-3): when set, the post (as edited) and the reply must END with it, once.
    # render_message puts the `(ts …)` suffix on a message's last line, so "last line == the
    # signature" reads as `  │ <signature> (ts <ts>)`. Compared entity-decoded: Slack stores
    # an `&` of the signature as `&amp;`.
    if signature:
        plain_thread, plain_signature = html.unescape(thread), html.unescape(signature)
        for label, ts in (("post, after its edit", ts1), ("thread reply", reply_ts)):
            assert f"{MESSAGE_CONTINUATION}{plain_signature} (ts {ts})" in plain_thread, (
                f"step 9 signature: the {label} ({ts}) does not end with SLACK_SIGNATURE {signature!r}:\n{thread}"
            )
        assert plain_thread.count(plain_signature) == 2, (
            f"step 9 signature: expected it exactly twice (post + reply), found "
            f"{plain_thread.count(plain_signature)} — the edit from the read-back text double-signed?\n{thread}"
        )
        # The markdown post is not asserted — how Slack stores markdown_text is not measured
        # yet — only recorded, to compare by eye with its permalink.
        md_thread = await slack_get_thread(GetThreadInput(channel=dm, thread_ts=_RUN["md_ts"], limit=1))
        _RUN["signature"] = (
            f"on ({len(signature)} chars) — verified as the last line of the post (after its edit) and of the "
            f"reply; the markdown post's last line reads back as: {_line(md_thread, '(ts ' + _RUN['md_ts'] + ')')!r}"
        )
    else:
        _RUN["signature"] = "off (SLACK_SIGNATURE unset for this run) — the signature check was skipped"

    # -- step 10: read back the history --------------------------------------------------
    history = await slack_get_history(GetHistoryInput(channel=dm, limit=20))
    print("== slack_get_history ==\n" + history)
    assert history.startswith(f"# History of {dm}"), f"step 10 get_history: unexpected output:\n{history}"
    assert f"(ts {ts1})" in history, f"step 10 get_history: the first post {ts1} is not in the history:\n{history}"
    assert f"(ts {_RUN['md_ts']})" in history, f"step 10 get_history: the markdown post is missing:\n{history}"
    _RUN["retry_after"] = client.last_retry_after or ""


async def test_2_length_limits_o2(live_stack: SlackClient) -> None:
    """O-2: measure the 4,000-char boundary — every probe lives in the markdown post's thread."""
    _need("dm", "md_ts")
    client, dm, parent = live_stack, _RUN["dm"], _RUN["md_ts"]
    outcomes: list[str] = []

    long_post = await slack_reply_in_thread(
        ReplyInThreadInput(channel=dm, thread_ts=parent, text=_sized("chat.postMessage", 4_001))
    )
    long_ts = _field(long_post, "ts") if "SENT" in long_post else ""
    outcomes.append(f"chat.postMessage 4,001 chars: {'ok' if long_ts else long_post.splitlines()[0]}")
    assert long_ts or long_post.startswith("Error"), f"O-2 postMessage probe: neither SENT nor an error:\n{long_post}"

    if long_ts:
        for size in (4_000, 4_001):
            try:
                await client.call("chat.update", {"channel": dm, "ts": long_ts, "text": _sized("chat.update", size)})
                outcomes.append(f"chat.update {size:,} chars: ok")
            except SlackApiError as exc:
                outcomes.append(f"chat.update {size:,} chars: {exc.code}")

    scheduled = await slack_schedule_message(
        ScheduleMessageInput(
            channel=dm,
            thread_ts=parent,
            post_at=int(time.time()) + SCHEDULE_AHEAD_SECONDS,
            text=_sized("chat.scheduleMessage", 4_001),
        )
    )
    outcomes.append(
        "chat.scheduleMessage 4,001 chars: "
        + (f"ok ({_field(scheduled, 'scheduled_message_id')})" if "# Message scheduled" in scheduled else scheduled)
    )
    assert "# Message scheduled" in scheduled or scheduled.startswith("Error"), (
        f"O-2 scheduleMessage probe: neither scheduled nor an error:\n{scheduled}"
    )
    _ANSWERS["O-2"] = "; ".join(outcomes)
    print("== O-2 length probes ==\n" + "\n".join(outcomes))


async def test_3_search_finds_the_nonce(live_stack: SlackClient) -> None:
    _need("nonce", "ts1")
    nonce, ts1 = _RUN["nonce"], _RUN["ts1"]
    deadline = time.monotonic() + SEARCH_POLL_SECONDS
    result = ""
    while True:
        result = await slack_search_messages(SearchMessagesInput(query=nonce, count=20))
        assert not result.startswith("Error"), f"step 11 search: the search call itself failed:\n{result}"
        if f"(ts {ts1})" in result or time.monotonic() >= deadline:
            break
        await asyncio.sleep(SEARCH_POLL_INTERVAL_SECONDS)
    print("== slack_search_messages ==\n" + result)
    if f"(ts {ts1})" not in result:
        _RUN["search"] = f"NOT indexed within {SEARCH_POLL_SECONDS:.0f}s (search index lag)"
        pytest.xfail(f"step 11 search: {nonce} not found within {SEARCH_POLL_SECONDS:.0f}s — search indexing lag")
    _RUN["search"] = f"found ts {ts1} for query {nonce}"


def test_4_live_write_smoke_result() -> None:
    """Print the block t6 pastes into STATE.md (with `-rA` or `-s`)."""
    _need("dm")
    rows = [
        "LIVE WRITE SMOKE RESULT",
        f"- self-DM: {_RUN['dm']} (user {_RUN.get('me')}); writes ON with SLACK_WRITE_CHANNELS='' and "
        "SLACK_WRITE_USERS='' — the self-DM passed the guard with NO allowlist entry",
        f"- nonce: {_RUN.get('nonce')}",
        f"- permalink, first post (mrkdwn, edited, reacted+unreacted): {_RUN.get('permalink1') or 'N/A'}",
        f"- permalink, markdown post (O-2 probes in its thread): {_RUN.get('permalink_md') or 'N/A'}",
        f"- permalink, thread reply: {_RUN.get('permalink_reply') or 'N/A'}",
        f"- scheduled_message_id: {_RUN.get('scheduled_id') or 'N/A'} (post_at {_RUN.get('post_at') or 'N/A'})",
        f"- update kept the mention verbatim: {_RUN.get('mention_kept', 'N/A')}; the link: {_RUN.get('link_kept', 'N/A')}",
        f"- search: {_RUN.get('search', 'not run')}",
        f"- signature: {_RUN.get('signature', 'not checked (step 9 did not run)')}",
        f"- scopes granted: {_RUN.get('scopes', 'N/A')}",
    ]
    for item in ("O-2", "O-3", "O-4", "O-11", "O-22", "O-23"):
        rows.append(f"- {item}: {_ANSWERS.get(item, 'not measured (its step did not run)')}")
    rows.append(
        "- O-14: not provoked on purpose (no step is built to lack a scope); if a step above failed with "
        "missing_scope, its printed error is the measurement of the needed/provided fields"
    )
    rows.append(
        f"- O-15: not provoked on purpose; last Retry-After seen by the client: {_RUN.get('retry_after') or 'none'}"
    )
    print("\n".join(rows))
