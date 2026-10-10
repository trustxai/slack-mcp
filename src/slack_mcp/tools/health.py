"""Health-check tool — the in-repo exemplar of the house tool style.

Wave workers: copy this module's shape (decorator, annotations, docstring sections,
try/except -> handle_api_error, `-> str` return, `fmt_user` for people). It calls the
one method every token can answer, `auth.test`, reads the granted scopes from the
`X-OAuth-Scopes` response header, and reports the write guard so an LLM knows up front
whether — and where — mutations will be allowed.
"""

from __future__ import annotations

from mcp.types import ToolAnnotations

from slack_mcp.client import get_client
from slack_mcp.config import get_settings
from slack_mcp.errors import handle_api_error
from slack_mcp.formatters import clip_response
from slack_mcp.server import mcp

# The user scopes the v0.1 tool set needs, in the order the app config lists them.
# Deliberately absent — scopes cannot be removed from an issued token, so the list holds
# only what a v0.1 tool calls: `channels:write` (only conversations.join needs it — the
# server never auto-joins — and it also grants archive/create/kick/leave/rename) and
# `reactions:read` (no v0.1 tool calls reactions.get). The
# granted set is read from the `X-OAuth-Scopes` response header — Slack has no
# `auth.scopes` method (research 02 §4.1).
REQUIRED_USER_SCOPES: tuple[str, ...] = (
    "channels:read",
    "channels:history",
    "groups:read",
    "groups:history",
    "im:read",
    "im:history",
    "im:write",
    "mpim:read",
    "mpim:history",
    "mpim:write",
    "chat:write",
    "reactions:write",
    "users:read",
    "users:read.email",
    "search:read",
)


def _scope_report(granted_header: str | None) -> list[str]:
    """Lines comparing the granted scopes (from `X-OAuth-Scopes`) with the required list."""
    if granted_header is None:
        return ["- **scopes**: Slack did not return the X-OAuth-Scopes header on this call."]
    granted = {part.strip() for part in granted_header.split(",") if part.strip()}
    missing = [scope for scope in REQUIRED_USER_SCOPES if scope not in granted]
    lines = [f"- **scopes granted**: {len(granted)} ({', '.join(sorted(granted))})"]
    if missing:
        lines.append(
            f"- **scopes MISSING for the v0.1 tools**: {', '.join(missing)} — add them under User Token Scopes "
            "and re-install the app."
        )
    else:
        lines.append("- **scopes**: every scope the v0.1 tools need is granted")
    return lines


@mcp.tool(
    name="slack_health_check",
    annotations=ToolAnnotations(
        title="Slack Health Check",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def slack_health_check() -> str:
    """Verify the token against Slack, show who you are, the granted scopes, and the write guard.

    Calls `auth.test` with the configured user token and renders the workspace, your
    user name and id, the token kind, and which of the scopes the tools need are
    granted (read from the `X-OAuth-Scopes` response header). Also states whether
    writes are enabled (SLACK_ALLOW_WRITES), how many channel / user ids the
    allowlists contain, and whether a signature is configured: Slack does not label
    messages posted with a user token — they look exactly like ones you typed — so
    `SLACK_SIGNATURE`, when set, is appended as the last line of every message the
    chat tools post, reply, schedule or edit.

    When to Use:
    - As the first call after configuring the server, to confirm the token works.
    - To learn your own user id (needed to recognise yourself in histories).
    - To debug `invalid_auth` / `missing_scope` before trying other tools.

    When NOT to Use:
    - To look up other people (use `slack_get_user` / `slack_resolve_user`).
    - To list channels (use `slack_list_conversations`).

    Returns:
    A markdown block with the base URL, the write-guard state, the signature state
    (`on (N chars)` / `off`), connectivity, the account, and the scope comparison — or
    an `Error ...` string describing the failure.

    Examples:
    - Call with no arguments: `slack_health_check()` (the tool takes no `params`).

    Error Handling:
    `invalid_auth` / `token_revoked` mean the token is missing, mistyped, or the app
    was re-installed (new token); a bot/app/browser token is refused with a message
    that names the kind. Without a configured token the tool returns the config state
    and says what to set, without calling Slack.
    """
    try:
        settings = get_settings()
        lines = [
            "# Slack health",
            "",
            f"- **base URL**: {settings.base_url}",
            "- **write guard**: "
            + (
                f"ENABLED — {len(settings.write_channels)} allowed channel id(s), "
                f"{len(settings.write_users)} allowed user id(s); Slack Connect channels only by explicit id"
                if settings.slack_allow_writes
                else "writes disabled (read-only; set SLACK_ALLOW_WRITES=1 plus SLACK_WRITE_CHANNELS / "
                "SLACK_WRITE_USERS to allow posting)"
            ),
            "- **signature**: "
            + (
                f"on ({len(settings.signature)} chars) — appended to every message"
                if settings.signature is not None
                else "off (set SLACK_SIGNATURE so teammates can tell these messages apart)"
            ),
        ]
        problem = settings.token_problem()
        if problem is not None:
            lines.append(f"- **token**: {problem}")
            # Every return path goes through clip_response (or to_json): it redacts.
            return clip_response("\n".join(lines))
        if settings.token_is_rotating:
            lines.append(
                "- **token**: rotating user token (xoxe.xoxp) — every call runs as you, but it expires (~12 h) and "
                "this server cannot refresh it: turn token rotation OFF in the app and re-install"
            )
        else:
            lines.append("- **token**: user token (xoxp) — every call runs as you")
        client = get_client()
        body = await client.call("auth.test")
        workspace = f"{body.get('team') or 'N/A'} ({body.get('team_id') or 'N/A'})"
        if body.get("url"):
            workspace += f" — {body['url']}"
        lines.extend(
            [
                "- **connectivity**: OK",
                f"- **workspace**: {workspace}",
                f"- **you**: {body.get('user') or 'N/A'} ({body.get('user_id') or 'N/A'})",
            ]
        )
        # auth.test answers a bot token with `bot_id` AND `"user": "bot"` (research 02 §4.1).
        if body.get("bot_id") or body.get("user") == "bot":
            lines.append(
                "- **warning**: Slack reports this token as a BOT (bot_id / user=bot) — it is not acting as you; "
                "use the app's User OAuth Token (xoxp-…)"
            )
        lines.extend(_scope_report(client.last_scopes))
        if client.last_retry_after:
            lines.append(f"- **retry-after**: {client.last_retry_after}s (Slack asked to slow down)")
        return clip_response("\n".join(lines))
    except Exception as exc:
        return handle_api_error(exc)
