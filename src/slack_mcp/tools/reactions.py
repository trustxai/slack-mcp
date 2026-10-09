"""Slack `reactions` tools: add / remove an emoji reaction on a message, as you.

Both methods are writes (`reactions.add`, `reactions.remove` are in the client's
`WRITE_METHODS`), so the client runs the write guard before anything is sent and never
retries them. Slack's `already_reacted` / `no_reaction` answers are not failures here:
the message already is in the state the caller asked for, so the tool says so in a
plain outcome sentence instead of an `Error` string.
"""

from __future__ import annotations

from typing import Any

from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, field_validator

from slack_mcp.client import get_client
from slack_mcp.errors import SlackApiError, handle_api_error
from slack_mcp.formatters import clip_response, fmt_warnings
from slack_mcp.server import mcp
from slack_mcp.validators import channel_id, message_ts, reaction_name


class ReactionInput(BaseModel):
    """The message to react to and the emoji (shared by add and remove)."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    channel: str = Field(
        description=(
            "Conversation ID that holds the message (C… channel, D… DM, G… group DM / private channel). "
            "Not a #name — resolve names with slack_list_conversations."
        ),
    )
    timestamp: str = Field(
        description=(
            "The message's `ts` (e.g. 1712345678.123456), as shown in `(ts …)` by slack_get_history / "
            "slack_get_thread / slack_search_messages."
        ),
    )
    name: str = Field(
        description=(
            "Emoji short name without colons, e.g. thumbsup, white_check_mark, eyes, or thumbsup::skin-tone-3. "
            "Surrounding colons (:thumbsup:) are stripped for you."
        ),
    )

    @field_validator("channel", mode="before")
    @classmethod
    def _check_channel(cls, value: object) -> str:
        return channel_id(value, field="channel")

    @field_validator("timestamp", mode="before")
    @classmethod
    def _check_timestamp(cls, value: object) -> str:
        return message_ts(value, field="timestamp")

    @field_validator("name", mode="before")
    @classmethod
    def _check_name(cls, value: object) -> str:
        return reaction_name(value, field="name")


def _wire_params(params: ReactionInput) -> dict[str, Any]:
    """The exact `reactions.add` / `reactions.remove` arguments (Slack calls the ts `timestamp`)."""
    return {"channel": params.channel, "timestamp": params.timestamp, "name": params.name}


def _target(params: ReactionInput) -> str:
    return f"message {params.timestamp} in {params.channel}"


def _with_warnings(text: str, body: dict[str, Any]) -> str:
    warnings = fmt_warnings(body)
    return clip_response(f"{text}\n{warnings}" if warnings else text)


@mcp.tool(
    name="slack_add_reaction",
    annotations=ToolAnnotations(
        title="Add Slack Reaction",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def slack_add_reaction(params: ReactionInput) -> str:
    """Add an emoji reaction to a message, as you.

    Calls `reactions.add` with the conversation id, the message `timestamp` and the
    emoji short name (colons stripped). The reaction is added AS YOU — it shows under
    your name, exactly as if you had clicked it in Slack. If you had already added that
    reaction, Slack answers `already_reacted`; the tool reports that as an outcome
    ("already there, nothing changed"), not as an error, because the message is in the
    state you asked for. The call is never retried.

    Refused unless `SLACK_ALLOW_WRITES=1` AND the target is allowed by
    `SLACK_WRITE_CHANNELS` / `SLACK_WRITE_USERS` (Slack Connect channels only by explicit
    ID).

    When to Use:
    - To acknowledge a message without replying (:eyes:, :white_check_mark:, :thumbsup:).
    - To mark a message as handled in a workflow that uses reactions as status.

    When NOT to Use:
    - To answer in words (use `slack_reply_in_thread` / `slack_post_message`).
    - To undo a reaction (use `slack_remove_reaction`).
    - When you only have a #channel name or a person's name: get the conversation id
      with `slack_list_conversations` (or `slack_resolve_user` + `slack_open_conversation`
      for a DM) and the message `ts` with `slack_get_history` / `slack_search_messages`.

    Returns:
    One sentence naming the emoji, the message ts and the conversation id — "added", or
    "was already there" for `already_reacted` — plus any Slack warning; or an `Error ...`
    string.

    Examples:
    - params = {"channel": "C0123ABCDEF", "timestamp": "1712345678.123456", "name": "eyes"}
    - params = {"channel": "D0123ABCDEF", "timestamp": "1712345678.123456", "name": ":white_check_mark:"}

    Error Handling:
    `invalid_name` = unknown emoji; `message_not_found` / `bad_timestamp` = wrong ts for
    that conversation; `channel_not_found` = wrong id or no access; `is_archived`,
    `thread_locked`, `too_many_reactions` / `too_many_emoji` = Slack refuses more
    reactions there; `missing_scope` = the app lacks the `reactions:write` user scope.
    A guard refusal names the env var (`SLACK_ALLOW_WRITES`, `SLACK_WRITE_CHANNELS`,
    `SLACK_WRITE_USERS`) that would allow it.
    """
    try:
        client = get_client()
        try:
            body = await client.call("reactions.add", _wire_params(params))
        except SlackApiError as exc:
            if exc.code == "already_reacted":
                return clip_response(
                    f"Reaction :{params.name}: was already on {_target(params)} (Slack: already_reacted) — nothing "
                    "changed; the message already shows your reaction."
                )
            raise
        return _with_warnings(f"Added reaction :{params.name}: to {_target(params)}.", body)
    except Exception as exc:
        return handle_api_error(exc)


@mcp.tool(
    name="slack_remove_reaction",
    annotations=ToolAnnotations(
        title="Remove Slack Reaction",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def slack_remove_reaction(params: ReactionInput) -> str:
    """Remove one of YOUR emoji reactions from a message.

    Calls `reactions.remove` with the conversation id, the message `timestamp` and the
    emoji short name (colons stripped). It acts AS YOU, so it can only remove a reaction
    you added — other people's reactions are untouched. If your reaction is not on the
    message, Slack answers `no_reaction`; the tool reports that as an outcome ("not
    there, nothing to remove"), not as an error, because the message is in the state you
    asked for. The call is never retried.

    Refused unless `SLACK_ALLOW_WRITES=1` AND the target is allowed by
    `SLACK_WRITE_CHANNELS` / `SLACK_WRITE_USERS` (Slack Connect channels only by explicit
    ID).

    When to Use:
    - To undo a reaction you (or this tool) added by mistake.
    - To clear a status reaction (e.g. remove :eyes: once you add :white_check_mark:).

    When NOT to Use:
    - To add a reaction (use `slack_add_reaction`).
    - To remove someone else's reaction — Slack does not allow it.
    - When you only have a #channel name: get the id with `slack_list_conversations` and
      the message `ts` with `slack_get_history` / `slack_search_messages`.

    Returns:
    One sentence naming the emoji, the message ts and the conversation id — "removed",
    or "was not there" for `no_reaction` — plus any Slack warning; or an `Error ...`
    string.

    Examples:
    - params = {"channel": "C0123ABCDEF", "timestamp": "1712345678.123456", "name": "eyes"}

    Error Handling:
    `invalid_name` = unknown emoji; `message_not_found` / `bad_timestamp` = wrong ts for
    that conversation; `no_access` / `channel_not_found` = wrong id or no access;
    `thread_locked`; `missing_scope` = the app lacks the `reactions:write` user scope. A
    guard refusal names the env var (`SLACK_ALLOW_WRITES`, `SLACK_WRITE_CHANNELS`,
    `SLACK_WRITE_USERS`) that would allow it.
    """
    try:
        client = get_client()
        try:
            body = await client.call("reactions.remove", _wire_params(params))
        except SlackApiError as exc:
            if exc.code == "no_reaction":
                return clip_response(
                    f"Reaction :{params.name}: was not on {_target(params)} as yours (Slack: no_reaction) — nothing "
                    "to remove; your reaction is absent, as asked."
                )
            raise
        return _with_warnings(f"Removed your reaction :{params.name}: from {_target(params)}.", body)
    except Exception as exc:
        return handle_api_error(exc)
