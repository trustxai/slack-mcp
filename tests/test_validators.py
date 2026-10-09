"""Unit tests for the shared input normalisers."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from slack_mcp.validators import (
    channel_id,
    is_conversation_id,
    iso_to_epoch,
    message_ts,
    reaction_name,
    time_to_ts,
    user_id,
)

# 2026-10-09T15:00:00Z
EPOCH = int(datetime(2026, 10, 9, 15, 0, tzinfo=UTC).timestamp())


@pytest.mark.parametrize(
    ("value", "expected"),
    [("C0123ABCDEF", "C0123ABCDEF"), (" D0456GHIJKL ", "D0456GHIJKL"), ("G0789MNOPQR", "G0789MNOPQR")],
)
def test_channel_id_accepts_conversation_ids(value: str, expected: str) -> None:
    assert channel_id(value) == expected


@pytest.mark.parametrize("value", ["#general", "general", "c0123abcdef", "U0123ABCDEF", "C012", "", None, 123])
def test_channel_id_rejects_names_users_and_garbage(value: object) -> None:
    with pytest.raises(ValueError, match="Slack conversation ID like C0123ABCDEF") as info:
        channel_id(value, field="channel")
    assert "slack_list_conversations" in str(info.value)


def test_is_conversation_id() -> None:
    assert is_conversation_id("C0123ABCDEF") is True
    assert is_conversation_id("D0123ABCDEF") is True
    assert is_conversation_id("#general") is False
    assert is_conversation_id("U0123ABCDEF") is False
    assert is_conversation_id(" C0123ABCDEF") is False  # the guard sees the wire value, unstripped
    assert is_conversation_id(None) is False


@pytest.mark.parametrize(("value", "expected"), [("U0123ABCDEF", "U0123ABCDEF"), (" W0456GHIJKL ", "W0456GHIJKL")])
def test_user_id_accepts_user_ids(value: str, expected: str) -> None:
    assert user_id(value) == expected


@pytest.mark.parametrize("value", ["@ada", "ada", "u0123abcdef", "C0123ABCDEF", "B0123ABCDEF", "U01", ""])
def test_user_id_rejects_names_handles_and_other_ids(value: str) -> None:
    with pytest.raises(ValueError, match="Slack user ID like U0123ABCDEF") as info:
        user_id(value, field="users")
    assert "slack_resolve_user" in str(info.value)
    assert str(info.value).startswith("users must be")


@pytest.mark.parametrize("value", ["1712345678.123456", " 1712345678.123456 "])
def test_message_ts_accepts_slack_timestamps(value: str) -> None:
    assert message_ts(value) == "1712345678.123456"


@pytest.mark.parametrize(
    "value", ["1712345678", "1712345678.12345", "1712345678.1234567", "abc", "", "1712345678,123456"]
)
def test_message_ts_rejects_other_shapes(value: str) -> None:
    with pytest.raises(ValueError, match="timestamp like 1712345678.123456"):
        message_ts(value, field="thread_ts")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("thumbsup", "thumbsup"),
        (":thumbsup:", "thumbsup"),
        (" :White_Check_Mark: ", "white_check_mark"),
        ("+1", "+1"),
        ("thumbsup::skin-tone-6", "thumbsup::skin-tone-6"),
        (":thumbsup::skin-tone-2:", "thumbsup::skin-tone-2"),
        ("party-parrot", "party-parrot"),
    ],
)
def test_reaction_name_strips_colons_and_lowercases(value: str, expected: str) -> None:
    assert reaction_name(value) == expected


@pytest.mark.parametrize("value", ["", "::", "thumbs up", "👍", "thumbsup::skin-tone-9", "a:b"])
def test_reaction_name_rejects_garbage(value: str) -> None:
    with pytest.raises(ValueError, match="emoji short name"):
        reaction_name(value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-10-09T15:00:00Z", EPOCH),
        ("2026-10-09T10:00:00-05:00", EPOCH),
        ("2026-10-09T15:00:00", EPOCH),  # naive = UTC
        ("2026-10-09 15:00:00+00:00", EPOCH),
        ("2026-10-09", EPOCH - 15 * 3600),
        (str(EPOCH), EPOCH),
        (EPOCH, EPOCH),
        (f"{EPOCH}.000000", EPOCH),
    ],
)
def test_iso_to_epoch(value: object, expected: int) -> None:
    assert iso_to_epoch(value) == expected


@pytest.mark.parametrize("value", ["tomorrow", "09/10/2026", "2026-13-01", "", True])
def test_iso_to_epoch_rejects_garbage(value: object) -> None:
    with pytest.raises(ValueError, match="post_at"):
        iso_to_epoch(value)


def test_time_to_ts_passes_a_real_ts_through() -> None:
    assert time_to_ts("1712345678.123456") == "1712345678.123456"


@pytest.mark.parametrize(
    ("value", "expected"),
    [("2026-10-09T15:00:00Z", f"{EPOCH}.000000"), (str(EPOCH), f"{EPOCH}.000000"), (EPOCH, f"{EPOCH}.000000")],
)
def test_time_to_ts_converts_iso_and_epoch(value: object, expected: str) -> None:
    assert time_to_ts(value) == expected


def test_time_to_ts_rejects_garbage_naming_the_field() -> None:
    with pytest.raises(ValueError, match="latest must be ISO-8601"):
        time_to_ts("last week", field="latest")
