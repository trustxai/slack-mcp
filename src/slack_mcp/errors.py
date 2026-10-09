"""Uniform error-to-string mapping for tool responses.

Slack answers HTTP 200 for most failures and signals them in the body as
`{"ok": false, "error": "<code>", ...}` — the client turns that into `SlackApiError`.
Transport-level statuses still happen (429 with `Retry-After`, 5xx), and every returned
string is scrubbed of the configured token and of anything that looks like a Slack token
(Slack echoes tokens in some error bodies).
"""

from __future__ import annotations

import re
from typing import Any

import httpx
from pydantic import ValidationError

from slack_mcp.config import get_settings

# Configured values shorter than this are not redacted: a tiny test value such as
# "key" would otherwise mangle ordinary words in the message.
_MIN_REDACT_LEN = 8

# Any Slack-shaped token, whatever its family, configured or not: `xoxp-…`, `xoxb-…`,
# `xapp-…`, `xwfp-…`, and the rotating `xoxe.xoxp-…` / `xoxe.xapp-…` forms. Token bodies
# are alphanumerics and dashes only, so a sentence's trailing period is left alone.
# Case-insensitive: `_describe` upper-cases a validation error's location, so a
# token-shaped field name reaches this pattern as `XOXB-…`.
_TOKEN_RE = re.compile(r"(?:xox[a-z]|xapp|xwfp)(?:\.(?:xox[a-z]|xapp))?-[A-Za-z0-9-]{10,}", re.IGNORECASE)

_TOKEN_HINT = (
    "The user token is missing, invalid, revoked or expired. Re-install your Slack app in this workspace and put "
    "the new 'User OAuth Token' (xoxp-…) in SLACK_USER_TOKEN."
)

_UNKNOWN_WRITE = (
    "Execution status is UNKNOWN for a write — read the conversation back (slack_get_history) before retrying, "
    "to avoid a duplicate message."
)


class SlackApiError(RuntimeError):
    """An `ok: false` response. `code` is Slack's error string (e.g. `not_in_channel`)."""

    def __init__(
        self,
        code: str,
        *,
        method: str = "",
        needed: str | None = None,
        provided: str | None = None,
        messages: list[str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.method = method
        self.needed = needed
        self.provided = provided
        self.messages = messages or []
        self.body: dict[str, Any] = body or {}
        detail = f"Slack refused {method or 'the request'}: {code}"
        if needed:
            detail += f" (needs scope {needed}; token has {provided or 'none of it'})"
        if self.messages:
            detail += " — " + "; ".join(self.messages)
        super().__init__(detail)


# Hints by Slack error code (research 02 §3/§4). Keep each one actionable for an LLM
# that cannot see the Slack UI.
_HINTS: dict[str, str] = {
    # -- token / account ---------------------------------------------------------
    "invalid_auth": _TOKEN_HINT,
    "not_authed": _TOKEN_HINT,
    "token_revoked": _TOKEN_HINT,
    "token_expired": _TOKEN_HINT,
    "account_inactive": _TOKEN_HINT,
    "missing_scope": (
        "Add the missing USER scope to your app (api.slack.com/apps → OAuth & Permissions → User Token Scopes), "
        "re-install the app, and update SLACK_USER_TOKEN."
    ),
    "not_allowed_token_type": "That method does not accept a user token, so this server cannot call it.",
    "no_permission": "Your account is not allowed to do that in this workspace.",
    "access_denied": "Access denied by Slack for this account.",
    "team_access_not_granted": "The token is not granted access to that workspace (the app is installed elsewhere).",
    "ekm_access_denied": "Workspace encryption policy denies this action.",
    "enterprise_is_restricted": "Your Enterprise organisation restricts this method.",
    "org_login_required": "The workspace was migrated into an Enterprise organisation — re-install the app there.",
    "two_factor_setup_required": "Your workspace requires two-factor authentication on your account first.",
    # -- conversations -------------------------------------------------------------
    "not_in_channel": "You are not a member of that channel. Join it in Slack first — this server never auto-joins.",
    "channel_not_found": (
        "Wrong conversation id, or you have no access to it. Find the id with slack_list_conversations."
    ),
    "channel_is_limited_access": "That conversation has limited access for apps; Slack refuses this call there.",
    "is_archived": "That channel is archived; nothing can be posted or reacted there.",
    "thread_not_found": "No parent message with that thread_ts exists in that conversation.",
    "message_not_found": "No message with that ts exists in that conversation (check channel AND ts).",
    "bad_timestamp": "That timestamp is not a valid message ts (format 1712345678.123456).",
    # -- people / DMs --------------------------------------------------------------
    "user_not_found": "Unknown user id. Resolve the person with slack_resolve_user or slack_lookup_user_by_email.",
    "users_not_found": (
        "One of the user ids (or the email) is unknown, deactivated, or not visible to you. Resolve each person "
        "with slack_resolve_user."
    ),
    "user_not_visible": "That user is not visible from your account (another workspace or a deactivated account).",
    "user_disabled": "That user's account is deactivated.",
    "cannot_dm_bot": "That id belongs to a bot; this server does not open DMs with bots.",
    "too_many_users": "A group DM holds at most 9 people including you: pass 1–8 other user ids.",
    "not_enough_users": "Pass at least one other user id to open a DM.",
    "invalid_user_combination": "External people must already share at least one channel with you.",
    # -- messages ------------------------------------------------------------------
    # msg_too_long: the threshold depends on the method — see `hint_for`.
    "msg_too_long": "The text exceeds Slack's limit for this method — shorten it or split it into several messages.",
    "message_truncated": (
        "Slack truncated the text (chat.postMessage keeps at most 40,000 characters) — the message WAS posted; "
        "send the remainder as a reply."
    ),
    "no_text": "The message text is empty.",
    "cant_update_message": "Only your own messages can be edited (it must have been posted by this account).",
    "edit_window_closed": "The workspace's edit window for that message has closed.",
    "message_limit_exceeded": "The workspace hit its plan's message limit.",
    "time_in_past": "post_at must be in the future.",
    "time_too_far": "post_at is more than 120 days ahead — Slack schedules at most 120 days out.",
    "invalid_time": "post_at is not a valid Unix time.",
    "restricted_too_many": "At most 30 messages can be scheduled within any 5-minute window in one channel.",
    "invalid_scheduled_message_id": (
        "No such scheduled message, or it already posted / posts within 60 seconds (it can no longer be deleted)."
    ),
    "restricted_action": "Workspace policy prevents this action in that channel.",
    "restricted_action_read_only_channel": "That channel is read-only for you.",
    "restricted_action_thread_only_channel": "That channel only allows replies in threads.",
    # -- reactions -----------------------------------------------------------------
    "already_reacted": "You had already added that reaction — the end state is what you asked for.",
    "no_reaction": "That reaction was not on the message (or is not yours) — the end state is what you asked for.",
    "invalid_name": "Unknown emoji short name.",
    "too_many_reactions": "That message has reached Slack's reaction limit.",
    # -- search --------------------------------------------------------------------
    "no_query": "The search query is empty.",
    # -- rate limits ---------------------------------------------------------------
    "ratelimited": "Rate limited by Slack — wait for Retry-After; do not retry in a tight loop.",
    "rate_limited": "You posted too many messages too fast — wait before posting again (about 1 per second per channel).",
    # -- our bugs (the server sent something Slack could not use) -------------------
    "invalid_arguments": "Slack rejected the arguments — check the ids and values you sent.",
    "invalid_arg_name": "Slack rejected an argument name (a server bug — please report it).",
    "invalid_array_arg": "Slack rejected a list argument — check the ids and values you sent.",
    "invalid_form_data": "Slack rejected the request body — check the ids and values you sent.",
    "invalid_charset": "Slack rejected the body's charset (a server bug — please report it).",
    "invalid_json": "Slack could not parse the JSON body (a server bug — please report it).",
    "json_not_object": "Slack expected a JSON object body (a server bug — please report it).",
    "invalid_cursor": "The pagination cursor is invalid or expired — start again without it.",
    "method_deprecated": "Slack deprecated this method (a server bug — please report it).",
    "deprecated_endpoint": "Slack retired this endpoint (a server bug — please report it).",
    # -- transient: the outcome of a write is unknown ---------------------------------
    "request_timeout": _UNKNOWN_WRITE,
    "internal_error": _UNKNOWN_WRITE,
    "fatal_error": _UNKNOWN_WRITE,
    "service_unavailable": _UNKNOWN_WRITE,
    "non_json_response": "Slack answered with something that is not JSON; the call may or may not have run.",
}


# Per-method overrides, where Slack documents a method-specific threshold.
_METHOD_HINTS: dict[tuple[str, str], str] = {
    ("msg_too_long", "chat.update"): (
        "The text is too long: chat.update refuses more than 4,000 characters. Shorten it, or post the rest as a "
        "new message."
    ),
}


def hint_for(code: str, method: str = "") -> str:
    """The actionable hint for a Slack error code on `method` (empty when unknown)."""
    return _METHOD_HINTS.get((code, method)) or _HINTS.get(code, "")


def redact_credentials(text: str) -> str:
    """Replace the configured token and any Slack-shaped token in `text` with ``***``.

    Covers the configured value in its raw and whitespace-stripped forms plus the
    repr-escaped form (OSError and h11 quote values via repr, so a newline inside one
    shows up as a literal backslash-n). Values under `_MIN_REDACT_LEN` characters (after
    stripping) are skipped. If settings cannot be loaded (e.g. an invalid env var), the
    pattern-based pass still runs.
    """
    values: set[str] = set()
    try:
        raw = get_settings().slack_user_token
    except (ValueError, OSError):  # pydantic ValidationError / SettingsError are ValueErrors
        raw = ""
    stripped = raw.strip()
    if len(stripped) >= _MIN_REDACT_LEN:
        values.update((raw, stripped, repr(raw)[1:-1], repr(stripped)[1:-1]))
    # Longest first, so a value that contains another is replaced whole.
    for value in sorted(values, key=len, reverse=True):
        text = text.replace(value, "***")
    return _TOKEN_RE.sub("***", text)


def handle_api_error(exc: Exception) -> str:
    """Map an exception to a human-readable `Error ...` string for the LLM.

    Tools never raise: every tool body is wrapped in try/except and returns this string
    on failure. The string never carries the token.
    """
    return redact_credentials(_describe(exc))


def _body_text(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:300].strip() or "empty response body"
    if isinstance(body, dict) and body.get("error"):
        return str(body["error"])
    return resp.text[:300].strip() or "empty response body"


def _describe(exc: Exception) -> str:
    """Unredacted message for `handle_api_error`."""
    if isinstance(exc, SlackApiError):
        hint = hint_for(exc.code, exc.method)
        return f"Error ({exc.code}): {str(exc).rstrip('.')}." + (f" {hint}" if hint else "")
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        detail = _body_text(exc.response)
        retry_after = exc.response.headers.get("retry-after")
        if status in (401, 403):
            return f"Error ({status}): {detail}. {_TOKEN_HINT}"
        if status == 429:
            wait = f" Retry-After: {retry_after}s." if retry_after else ""
            return f"Error (429): Rate limited by Slack – {detail}.{wait} Back off; do not retry in a tight loop."
        if status >= 500:
            return f"Error ({status}): Slack-side failure – {detail}. {_UNKNOWN_WRITE}"
        return f"Error ({status}): {detail}"
    if isinstance(exc, httpx.TimeoutException):
        return (
            f"Error: the Slack API request timed out. {_UNKNOWN_WRITE} Raise SLACK_REQUEST_TIMEOUT_SECONDS if "
            "this recurs."
        )
    if isinstance(exc, httpx.ConnectError):
        return "Error: could not connect to the Slack API. Check network access and SLACK_API_URL."
    # After the timeout/connect branches (all TransportError subclasses). Never include
    # str(exc): h11's "Illegal header value b'...'" quotes the header verbatim, i.e. the token.
    if isinstance(exc, httpx.LocalProtocolError):
        return (
            "Error: the HTTP client rejected the Slack API request (LocalProtocolError). This usually means stray "
            f"whitespace or newlines in SLACK_USER_TOKEN — re-check the value. {_UNKNOWN_WRITE}"
        )
    if isinstance(exc, httpx.UnsupportedProtocol):
        return "Error: SLACK_API_URL is not an http(s) URL — it must start with https:// (e.g. https://slack.com/api)."
    if isinstance(exc, httpx.TransportError):
        return (
            f"Error: the Slack API request failed at the network layer ({type(exc).__name__}). Check network "
            f"access, any HTTPS_PROXY setting, and SLACK_API_URL. {_UNKNOWN_WRITE}"
        )
    if isinstance(exc, ValidationError):
        # Never the input values (a bad setting may sit next to the token); location + message only.
        details = "; ".join(
            f"{'.'.join(str(part) for part in err['loc']).upper() or 'value'}: {err['msg']}"
            for err in exc.errors(include_url=False, include_input=False, include_context=False)
        )
        label = "invalid configuration" if exc.title == "Settings" else "invalid input"
        return f"Error: {label} – {details}"
    if isinstance(exc, RuntimeError):
        return f"Error: {exc}"
    return f"Error: unexpected failure – {type(exc).__name__}: {exc}"
