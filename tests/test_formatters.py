"""Unit tests for the shared formatters."""

from __future__ import annotations

import json
from typing import Any

import pytest

from slack_mcp.config import Settings
from slack_mcp.formatters import (
    MAX_RESPONSE_BYTES,
    SHARED_FLAGS,
    ResponseFormat,
    clip_response,
    fmt_channel,
    fmt_user,
    fmt_warnings,
    md_cell,
    mrkdwn_escape,
    paginated_response,
    render_mention,
    render_message,
    shared_flags,
    to_json,
    ts_to_human,
)

# 2024-04-05 19:34:38 UTC
TS = "1712345678.123456"


def test_ts_to_human() -> None:
    assert ts_to_human(TS) == "2024-04-05 19:34 UTC"
    assert ts_to_human("1712345678") == "2024-04-05 19:34 UTC"
    assert ts_to_human(1712345678) == "2024-04-05 19:34 UTC"
    assert ts_to_human(None) == "N/A"
    assert ts_to_human("") == "N/A"
    assert ts_to_human("not a ts") == "not a ts"
    assert ts_to_human("1e30") == "1e30"  # overflow falls back to the raw text


def test_fmt_user_with_profile() -> None:
    user = {
        "id": "U0123ABCDEF",
        "name": "ada",
        "real_name": "Ada Lovelace",
        "profile": {"display_name": "ada.l", "real_name": "Ada L."},
    }
    assert fmt_user(user) == "Ada Lovelace (@ada.l, U0123ABCDEF)"


def test_fmt_user_without_profile_or_display_name() -> None:
    assert fmt_user({"id": "U0123ABCDEF", "name": "ada"}) == "ada (@ada, U0123ABCDEF)"
    assert fmt_user({"id": "U0123ABCDEF", "name": "ada", "profile": {"display_name": ""}}) == (
        "ada (@ada, U0123ABCDEF)"
    )
    assert fmt_user({"id": "U0123ABCDEF", "profile": {"real_name": "Ada Lovelace"}}) == ("Ada Lovelace (U0123ABCDEF)")
    assert fmt_user({"id": "U0123ABCDEF", "profile": "not-a-dict"}) == "user (U0123ABCDEF)"
    assert fmt_user(None, fallback_id="U0123ABCDEF") == "user U0123ABCDEF"
    assert fmt_user({}) == "unknown user"


def test_fmt_channel_for_channel_im_and_mpim() -> None:
    assert fmt_channel({"id": "C0123ABCDEF", "name": "general", "is_channel": True}) == "#general (C0123ABCDEF)"
    assert fmt_channel({"id": "C0123ABCDEF", "name_normalized": "general"}) == "#general (C0123ABCDEF)"
    assert fmt_channel({"id": "D0123ABCDEF", "is_im": True, "user": "U0123ABCDEF"}) == (
        "DM with U0123ABCDEF (D0123ABCDEF)"
    )
    assert fmt_channel({"id": "D0123ABCDEF", "is_im": True}) == "DM (D0123ABCDEF)"
    assert fmt_channel({"id": "G0123ABCDEF", "is_mpim": True, "name": "mpdm-a--b-1"}) == "group DM (G0123ABCDEF)"
    assert fmt_channel({}, fallback_id="C0123ABCDEF") == "conversation C0123ABCDEF"
    assert fmt_channel(None) == "unknown conversation"


def test_shared_flags() -> None:
    assert SHARED_FLAGS == ("is_ext_shared", "is_shared", "is_org_shared", "is_pending_ext_shared")
    assert shared_flags({"is_shared": True, "is_ext_shared": True, "is_org_shared": False}) == [
        "is_ext_shared",
        "is_shared",
    ]
    assert shared_flags({"is_pending_ext_shared": True}) == ["is_pending_ext_shared"]
    # Any truthy value counts (fail-closed): the guard uses this same predicate.
    assert shared_flags({"is_shared": "true", "is_ext_shared": 1}) == ["is_ext_shared", "is_shared"]
    assert shared_flags({"is_shared": False, "is_ext_shared": None, "is_org_shared": 0}) == []
    assert shared_flags({}) == []
    assert shared_flags(None) == []


def test_render_mention_and_mrkdwn_escape() -> None:
    assert render_mention("U0123ABCDEF") == "<@U0123ABCDEF>"
    assert mrkdwn_escape("a < b && c > d") == "a &lt; b &amp;&amp; c &gt; d"
    assert mrkdwn_escape("plain") == "plain"


def test_md_cell() -> None:
    assert md_cell("a | b") == "a \\| b"
    assert md_cell("line one\nline two\t end") == "line one line two end"
    assert md_cell(None) == ""
    assert md_cell("") == ""
    assert md_cell(42) == "42"
    clipped = md_cell("x" * 300, max_len=10)
    assert clipped == "x" * 9 + "…"


def test_render_message_basic() -> None:
    line = render_message({"ts": TS, "user": "U0123ABCDEF", "text": "hello <@U0456GHIJKL>"})
    assert line == f"[2024-04-05 19:34 UTC] U0123ABCDEF: hello <@U0456GHIJKL> (ts {TS})"


def test_render_message_with_names_reply_count_subtype_and_edited() -> None:
    names = {"U0123ABCDEF": "Ada Lovelace (@ada, U0123ABCDEF)"}
    parent = {"ts": TS, "user": "U0123ABCDEF", "text": "parent", "reply_count": 3, "edited": {"ts": TS}}
    assert render_message(parent, names=names) == (
        f"[2024-04-05 19:34 UTC] Ada Lovelace (@ada, U0123ABCDEF): parent (ts {TS}) · 3 replies · edited"
    )
    one = render_message({"ts": TS, "user": "U0123ABCDEF", "text": "x", "reply_count": 1})
    assert one.endswith("· 1 reply")
    join = render_message({"ts": TS, "user": "U0123ABCDEF", "text": "", "subtype": "channel_join"})
    assert join == f"[2024-04-05 19:34 UTC] U0123ABCDEF (channel_join) (ts {TS})"
    bot = render_message({"ts": TS, "bot_id": "B0123ABCDEF", "username": "deploybot", "text": "ok"})
    assert "] deploybot: ok" in bot
    assert render_message({"text": "no ts"}) == "[N/A] unknown: no ts"


def test_clip_response_passthrough_and_truncate() -> None:
    assert clip_response("short") == "short"
    big = "x" * (MAX_RESPONSE_BYTES + 10)
    clipped = clip_response(big)
    assert clipped.endswith("narrow the query]_")
    body = clipped.split("\n\n_[truncated")[0]
    assert len(body.encode()) == MAX_RESPONSE_BYTES
    assert clip_response(big, max_bytes=10).startswith("x" * 10 + "\n\n_[truncated: response exceeded 10 bytes")


def test_clip_response_never_splits_a_multibyte_character() -> None:
    clipped = clip_response("é" * 10, max_bytes=5)
    assert clipped.startswith("éé\n\n_[truncated")


def test_to_json_handles_non_serialisable() -> None:
    assert json.loads(to_json({"a": {1, 2}}))["a"] in ("{1, 2}", "{2, 1}")


# --- successful output is scrubbed too ---------------------------------------------------

LEAKY_MESSAGE = {
    "ok": True,
    "messages": [
        {"ts": TS, "user": "U0123ABCDEF", "text": "oops, here is our bot token xoxb-FAKE-0123456789abcdef"},
        {"ts": TS, "user": "U0456GHIJKL", "text": "and mine: xoxp-FAKE-9876543210fedcba, keep it safe"},
    ],
}


def test_to_json_redacts_tokens_inside_a_successful_body() -> None:
    text = to_json(LEAKY_MESSAGE)
    assert "xoxb-FAKE" not in text
    assert "xoxp-FAKE" not in text
    payload = json.loads(text)  # still valid JSON
    assert payload["messages"][0]["text"] == "oops, here is our bot token ***"
    assert payload["messages"][1]["text"] == "and mine: ***, keep it safe"


def test_clip_response_redacts_tokens_in_markdown() -> None:
    markdown = "\n".join(render_message(message) for message in LEAKY_MESSAGE["messages"])
    text = clip_response(markdown)
    assert "xoxb-FAKE" not in text
    assert "xoxp-FAKE" not in text
    assert "bot token *** (ts" in text
    assert "and mine: ***, keep it safe" in text


def test_successful_output_redacts_the_configured_token(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "xoxp-FAKE-CONFIGURED-0123456789"
    monkeypatch.setattr("slack_mcp.errors.get_settings", lambda: Settings(slack_user_token=secret))
    assert secret not in clip_response(f"echo {secret}")
    assert secret not in to_json({"echo": secret})
    paginated = paginated_response(
        items=[{"name": secret}], fmt=ResponseFormat.JSON, item_formatter=_fmt, title="T", next_cursor=None
    )
    assert secret not in paginated


# --- warnings on ok bodies -----------------------------------------------------------------


def test_fmt_warnings() -> None:
    assert fmt_warnings({"ok": True}) == ""
    assert fmt_warnings(None) == ""
    assert fmt_warnings({"ok": True, "warning": "", "response_metadata": {"warnings": []}}) == ""
    text = fmt_warnings(
        {
            "ok": True,
            "warning": "message_truncated,superfluous_charset",
            "response_metadata": {"warnings": ["message_truncated"]},
        }
    )
    assert text.startswith("Slack warning(s): `message_truncated` — ")
    assert "WAS posted" in text
    assert text.endswith("; `superfluous_charset`")
    assert text.count("message_truncated") == 1
    assert fmt_warnings({"response_metadata": {"warnings": ["missing_charset"]}}) == (
        "Slack warning(s): `missing_charset`"
    )


def _fmt(item: dict[str, Any]) -> str:
    return f"- {item['name']}"


def test_paginated_response_markdown_with_next_cursor() -> None:
    text = paginated_response(
        items=[{"name": "a"}, {"name": "b"}],
        fmt=ResponseFormat.MARKDOWN,
        item_formatter=_fmt,
        title="Channels",
        next_cursor="dGVhbTpDMDYxRkE1UEI=",
        limit=2,
        note="1 archived channel hidden.",
    )
    assert text.startswith("# Channels\n")
    assert "Showing **2** item(s) (limit 2)." in text
    assert "1 archived channel hidden." in text
    assert "pass `cursor` = `dGVhbTpDMDYxRkE1UEI=` to get the next page" in text
    assert text.endswith("- a\n- b")


def test_paginated_response_markdown_last_page_and_empty() -> None:
    text = paginated_response(
        items=[{"name": "a"}], fmt=ResponseFormat.MARKDOWN, item_formatter=_fmt, title="T", next_cursor=""
    )
    assert "More available" not in text
    assert "Showing **1** item(s)." in text
    empty = paginated_response(items=[], fmt=ResponseFormat.MARKDOWN, item_formatter=_fmt, title="T", next_cursor=None)
    assert "_No items._" in empty


def test_paginated_response_markdown_table() -> None:
    text = paginated_response(
        items=[{"name": "a|b"}],
        fmt=ResponseFormat.MARKDOWN,
        item_formatter=lambda item: f"| {md_cell(item['name'])} |",
        title="T",
        next_cursor=None,
        table_header=["Name"],
    )
    assert text.endswith("| Name |\n|---|\n| a\\|b |")
    empty = paginated_response(
        items=[], fmt=ResponseFormat.MARKDOWN, item_formatter=_fmt, title="T", next_cursor=None, table_header=["Name"]
    )
    assert "| Name |" not in empty


def test_paginated_response_with_plain_string_items() -> None:
    members = ["U0123ABCDEF", "U0456GHIJKL"]
    text = paginated_response(
        items=members,
        fmt=ResponseFormat.MARKDOWN,
        item_formatter=lambda uid: f"- {uid}",
        title="Members",
        next_cursor="next==",
    )
    assert text.endswith("- U0123ABCDEF\n- U0456GHIJKL")
    payload = json.loads(
        paginated_response(
            items=tuple(members), fmt=ResponseFormat.JSON, item_formatter=str, title="Members", next_cursor=None
        )
    )
    assert payload["items"] == members


def test_paginated_response_json_with_and_without_next_cursor() -> None:
    items = [{"name": "a", "id": "C0123ABCDEF"}]
    payload = json.loads(
        paginated_response(
            items=items, fmt=ResponseFormat.JSON, item_formatter=_fmt, title="T", next_cursor=" abc= ", limit=50
        )
    )
    assert payload == {
        "title": "T",
        "count": 1,
        "limit": 50,
        "has_more": True,
        "next_cursor": "abc=",
        "note": None,
        "items": items,
    }
    last = json.loads(
        paginated_response(items=items, fmt=ResponseFormat.JSON, item_formatter=_fmt, title="T", next_cursor="")
    )
    assert last["has_more"] is False
    assert last["next_cursor"] is None
