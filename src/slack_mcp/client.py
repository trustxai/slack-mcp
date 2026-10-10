"""Async HTTP client for the Slack Web API.

One auth scheme: every request carries ``Authorization: Bearer <SLACK_USER_TOKEN>`` — a
USER token, so every call runs as the person who installed the app. Every method is a
``POST <base_url>/<method>``. The body is JSON (``application/json; charset=utf-8``) only
for ``JSON_METHODS`` (``chat.postMessage`` / ``chat.update`` / ``chat.scheduleMessage``,
as python-slack-sdk does) and ``application/x-www-form-urlencoded`` for everything else —
form encoding is the one Slack accepts on every method, and read methods ignore JSON.

An ``ok: true`` body is returned intact, ``warning`` / ``response_metadata.warnings``
included (``chat.postMessage`` truncates text over 40,000 characters and only says so
with a ``message_truncated`` warning) — a warning is never treated as a failure.

The safety rails live HERE, not in the tools, so no wave module can forget them:

- **Closed method list.** Only methods in ``KNOWN_METHODS`` (``READ_METHODS`` ∪
  ``WRITE_METHODS``) can be called; anything else raises ``UnknownMethodError`` before
  the token is even read. A write method that is not in ``WRITE_METHODS`` therefore
  cannot slip past the guard — adding a method is a deliberate spine change.
- **Token kind.** The token must be a user token (``xoxp-``); anything else is refused
  before the first byte is sent (``config.Settings.validate_token``).
- **Write guard.** "Is this a write" is a method set, never the HTTP verb — Slack uses
  POST for reads too. Every write runs through ``guard.WriteGuard`` on the exact
  parameters that go on the wire: kill-switch, ID-only channel allowlist, DM
  membership, Slack Connect refusal. The guard's own lookups go through a read-only
  path that refuses write methods.
- **Envelope check.** Slack answers HTTP 200 with ``{"ok": false, "error": "<code>"}``.
  The client raises ``SlackApiError`` so tools never check ``ok`` for failure.
- **No automatic retry of a write, ever** — a retried ``chat.postMessage`` is a
  duplicate message. A read is retried ONCE on HTTP 429 when ``Retry-After`` is at most
  ``MAX_RETRY_AFTER_SECONDS``.

Tools call ``await client.call(method, params)`` with ONE plain mapping and get the
parsed ``ok: true`` body back. The client drops ``None`` values, comma-joins lists of
scalars (``users=U1,U2``), and picks the encoding by method.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any

import httpx

from slack_mcp.config import Settings, get_settings
from slack_mcp.errors import SlackApiError
from slack_mcp.guard import WriteGuard

# Methods that change workspace state. Membership here, not the verb, triggers the guard.
WRITE_METHODS: frozenset[str] = frozenset(
    {
        "chat.postMessage",
        "chat.update",
        "chat.scheduleMessage",
        "chat.deleteScheduledMessage",
        "reactions.add",
        "reactions.remove",
        "conversations.open",
    }
)

# Read-only methods the tools (and the guard) may call. Nothing outside these two sets
# reaches Slack.
READ_METHODS: frozenset[str] = frozenset(
    {
        "auth.test",
        "bots.info",
        "chat.getPermalink",
        "chat.scheduledMessages.list",
        "conversations.history",
        "conversations.info",
        "conversations.list",
        "conversations.members",
        "conversations.replies",
        "search.messages",
        "users.conversations",
        "users.info",
        "users.list",
        "users.lookupByEmail",
    }
)

KNOWN_METHODS: frozenset[str] = READ_METHODS | WRITE_METHODS

# Methods sent as a JSON body — exactly what python-slack-sdk sends as JSON (research 02
# §2). Read methods ignore JSON bodies ("those methods do not understand attributes
# presented as JSON"), and form encoding is accepted by every method, so everything
# else — reads, reactions.*, conversations.open, chat.deleteScheduledMessage,
# chat.getPermalink — goes form-encoded. NOT the guard set: that is WRITE_METHODS.
JSON_METHODS: frozenset[str] = frozenset({"chat.postMessage", "chat.update", "chat.scheduleMessage"})

# A read is retried once on 429 only when Slack asks for at most this many seconds.
MAX_RETRY_AFTER_SECONDS = 10.0

_JSON_CONTENT_TYPE = "application/json; charset=utf-8"


class UnknownMethodError(RuntimeError):
    """Raised for a Web API method that is not on this server's method list."""


def encoding_for(method: str) -> str:
    """`"json"` or `"form"` — how `method` is sent on the wire."""
    return "json" if method in JSON_METHODS else "form"


def _scalar(value: Any, *, form: bool) -> Any:
    if isinstance(value, bool):
        return ("true" if value else "false") if form else value
    if form and not isinstance(value, str):
        return str(value)
    return value


def compact_params(params: Mapping[str, Any] | None, *, form: bool) -> dict[str, Any]:
    """The wire body for `params`: `None` and empty lists dropped, lists of scalars comma-joined.

    Slack takes `users=U1,U2` / `types=public_channel,private_channel` as comma-joined
    strings in both encodings. In a form, bools become `true`/`false` and numbers become
    strings; in JSON they stay native. Mappings and lists of mappings stay structured in
    JSON and are JSON-encoded strings in a form. The chat tools send Block Kit `blocks`
    this way — as a list of mappings, on the JSON methods (`JSON_METHODS`).
    """
    out: dict[str, Any] = {}
    if not params:
        return out
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, list | tuple):
            items = [item for item in value if item is not None]
            if not items:
                continue
            if all(isinstance(item, Mapping) for item in items):
                out[key] = json.dumps(items) if form else items
            else:
                out[key] = ",".join(str(_scalar(item, form=True)) for item in items)
            continue
        if isinstance(value, Mapping):
            out[key] = json.dumps(dict(value)) if form else dict(value)
            continue
        out[key] = _scalar(value, form=form)
    return out


async def _sleep(seconds: float) -> None:
    """Indirection so tests can stub the backoff."""
    await asyncio.sleep(seconds)


def _retry_after_seconds(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


class SlackClient:
    """Thin async wrapper around httpx for the Slack Web API."""

    def __init__(
        self,
        settings: Settings | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        # Injectable transport so tests can use httpx.MockTransport.
        self._transport = transport
        self.guard = WriteGuard(self._settings, self._guard_read)
        # Diagnostics captured from the last response (the health tool reports them).
        self.last_retry_after: str | None = None
        self.last_scopes: str | None = None
        self.last_accepted_scopes: str | None = None

    @property
    def settings(self) -> Settings:
        return self._settings

    # -- requests ------------------------------------------------------------

    async def call(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Call a Web API method and return the parsed `ok: true` body.

        Raises `UnknownMethodError` for a method off the list, `RuntimeError` /
        `TokenTypeError` when the token is missing or not a user token,
        `WritesDisabledError` / `WriteNotAllowedError` from the guard, `SlackApiError`
        for an `ok: false` (or non-JSON) body, and `httpx.HTTPStatusError` / transport
        errors otherwise. Tools route all of them through
        `slack_mcp.errors.handle_api_error`.
        """
        if method not in KNOWN_METHODS:
            raise UnknownMethodError(
                f"{method!r} is not on this server's method list (slack_mcp.client.KNOWN_METHODS); adding a Slack "
                "method is a deliberate change to the client."
            )
        self._settings.validate_token()
        form = encoding_for(method) == "form"
        body = compact_params(params, form=form)
        is_write = method in WRITE_METHODS
        if is_write:
            # The guard decides on exactly what will be sent.
            await self.guard.check(method, body)
        return await self._request(method, body, form=form, timeout=timeout, allow_retry=not is_write)

    async def _guard_read(self, method: str, params: Mapping[str, Any] | None) -> dict[str, Any]:
        """The guard's lookup path: read methods only, so it can never write or recurse."""
        if method not in READ_METHODS:
            raise UnknownMethodError(f"the write guard may only call read methods, not {method!r}")
        form = encoding_for(method) == "form"
        return await self._request(method, compact_params(params, form=form), form=form, timeout=None, allow_retry=True)

    async def _request(
        self,
        method: str,
        body: dict[str, Any],
        *,
        form: bool,
        timeout: float | None,
        allow_retry: bool,
    ) -> dict[str, Any]:
        settings = self._settings
        url = f"{settings.base_url}/{method}"
        headers = {"accept": "application/json", "authorization": f"Bearer {settings.slack_user_token}"}
        effective_timeout = timeout if timeout is not None else settings.slack_request_timeout_seconds

        async with httpx.AsyncClient(timeout=effective_timeout, transport=self._transport) as client:
            resp = await self._send(client, url, headers, body, form=form)
            if resp.status_code == 429 and allow_retry:
                wait = _retry_after_seconds(resp.headers.get("retry-after"))
                if wait is not None and wait <= MAX_RETRY_AFTER_SECONDS:
                    await _sleep(wait)
                    resp = await self._send(client, url, headers, body, form=form)

        self.last_retry_after = resp.headers.get("retry-after")
        self.last_scopes = resp.headers.get("x-oauth-scopes")
        self.last_accepted_scopes = resp.headers.get("x-accepted-oauth-scopes")
        resp.raise_for_status()
        return self._check_envelope(resp, method)

    @staticmethod
    async def _send(
        client: httpx.AsyncClient, url: str, headers: dict[str, str], body: dict[str, Any], *, form: bool
    ) -> httpx.Response:
        if form:
            return await client.post(url, headers=headers, data=body)
        content = json.dumps(body, ensure_ascii=False).encode("utf-8")
        return await client.post(url, headers={**headers, "content-type": _JSON_CONTENT_TYPE}, content=content)

    @staticmethod
    def _check_envelope(resp: httpx.Response, method: str) -> dict[str, Any]:
        """Parse the body and raise `SlackApiError` unless it says `ok: true`."""
        try:
            body = resp.json()
        except ValueError:
            raise SlackApiError("non_json_response", method=method) from None
        if not isinstance(body, dict):
            raise SlackApiError("non_json_response", method=method)
        if body.get("ok") is True:
            return body
        metadata = body.get("response_metadata")
        messages: list[str] = []
        if isinstance(metadata, dict):
            messages = [str(m) for m in metadata.get("messages") or []]
        needed = body.get("needed")
        provided = body.get("provided")
        raise SlackApiError(
            str(body.get("error") or "unknown_error"),
            method=method,
            needed=str(needed) if needed else None,
            provided=str(provided) if provided else None,
            messages=messages,
            body=body,
        )


_client: SlackClient | None = None


def get_client() -> SlackClient:
    """Lazy module-level singleton (tools monkeypatch this in unit tests)."""
    global _client
    if _client is None:
        _client = SlackClient()
    return _client


def reset_client() -> None:
    """Drop the singleton and its guard cache (tests call this between settings changes)."""
    global _client
    _client = None
