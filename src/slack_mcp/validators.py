"""Input normalisers shared by every tool module's pydantic models.

Each function takes the raw value the LLM sent, returns the exact string Slack expects
on the wire, and raises `ValueError` with a readable message otherwise — so a
`field_validator` can call it directly and pydantic turns the message into the tool's
`Error:` string. Tool modules must use these instead of re-implementing the rules.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

# Slack IDs: channels C…, DMs D…, group DMs / private channels G…; users U… (W… on
# Enterprise Grid). Always upper-case alphanumerics, 9+ characters in practice.
_CHANNEL_RE = re.compile(r"^[CDG][A-Z0-9]{8,}$")
_USER_RE = re.compile(r"^[UW][A-Z0-9]{8,}$")
# A message timestamp is `<epoch seconds>.<6 digits>` and doubles as the message id.
_TS_RE = re.compile(r"^\d{9,13}\.\d{6}$")
_EPOCH_RE = re.compile(r"^\d{9,13}$")
# Emoji short names: lower-case, digits, `_`, `+`, `-`, optionally `::skin-tone-N`.
_REACTION_RE = re.compile(r"^[a-z0-9_+\-']+(?:::skin-tone-[2-6])?$")


def is_conversation_id(value: object) -> bool:
    """True when `value` is shaped like a conversation id (the write guard's ID-only rule)."""
    return isinstance(value, str) and bool(_CHANNEL_RE.match(value))


def channel_id(value: object, *, field: str = "channel") -> str:
    """Validate a conversation id (`C…` channel, `D…` DM, `G…` group DM / private channel)."""
    text = str(value).strip()
    if not _CHANNEL_RE.match(text):
        raise ValueError(
            f"{field} must be a Slack conversation ID like C0123ABCDEF (not a #name — resolve it with "
            f"slack_list_conversations first), got {value!r}"
        )
    return text


def user_id(value: object, *, field: str = "user") -> str:
    """Validate a user id (`U…`, or `W…` on Enterprise Grid)."""
    text = str(value).strip()
    if not _USER_RE.match(text):
        raise ValueError(
            f"{field} must be a Slack user ID like U0123ABCDEF (not a name or @handle — resolve it with "
            f"slack_resolve_user or slack_lookup_user_by_email first), got {value!r}"
        )
    return text


def message_ts(value: object, *, field: str = "ts") -> str:
    """Validate a message timestamp / id (`1712345678.123456`)."""
    text = str(value).strip()
    if not _TS_RE.match(text):
        raise ValueError(f"{field} must be a Slack message timestamp like 1712345678.123456, got {value!r}")
    return text


def reaction_name(value: object, *, field: str = "name") -> str:
    """Normalise an emoji short name: strips surrounding colons/whitespace, lower-cases."""
    text = str(value).strip().strip(":").strip().lower()
    if not text or not _REACTION_RE.match(text):
        raise ValueError(f"{field} must be an emoji short name like thumbsup or white_check_mark, got {value!r}")
    return text


def _parse_time(value: object, *, field: str) -> int:
    """Epoch seconds from an int, a digit string, or an ISO-8601 date/datetime (naive = UTC)."""
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an ISO-8601 datetime or epoch seconds, not a bool")
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if _EPOCH_RE.match(text):
        return int(text)
    if _TS_RE.match(text):
        return int(text.split(".", 1)[0])
    if not text:
        raise ValueError(f"{field} must be an ISO-8601 datetime (e.g. 2026-10-09T15:00:00Z) or epoch seconds")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(
            f"{field} must be ISO-8601 (e.g. 2026-10-09 or 2026-10-09T15:00:00Z) or epoch seconds, got {text!r}"
        ) from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp())


def iso_to_epoch(value: object, *, field: str = "post_at") -> int:
    """Epoch seconds for scheduling arguments (`post_at`)."""
    return _parse_time(value, field=field)


def time_to_ts(value: object, *, field: str = "oldest") -> str:
    """Normalise a history bound to the `ts` form Slack accepts (`1712345678.000000`).

    A real message ts passes through unchanged (so a bound can be "this message");
    ISO-8601 and epoch inputs become `<epoch>.000000`.
    """
    text = str(value).strip()
    if _TS_RE.match(text):
        return text
    return f"{_parse_time(text, field=field)}.000000"
