"""Settings for the Slack MCP server, loaded from env vars / .env."""

from __future__ import annotations

from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_API_URL = "https://slack.com/api"

# Plain http is allowed only towards these hosts (local test doubles): anywhere else the
# Bearer token would cross the network in cleartext.
LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost"})

# The ONLY credential this server accepts: a user token from your own Slack app's
# "User OAuth Token". With token rotation on, Slack issues it as `xoxe.xoxp-…` — that
# form is accepted, but it expires (~12 h) and this server has no refresh flow, so the
# app recipe keeps rotation OFF.
USER_TOKEN_PREFIXES: tuple[str, ...] = ("xoxp-", "xoxe.xoxp-")
ROTATING_USER_TOKEN_PREFIX = "xoxe.xoxp-"

# Everything else is refused before the first request, with a message that names the
# kind and never echoes the value. Bots belong to a bot gateway (Hermes), not to this
# server; browser session tokens (xoxc/xoxd) are not an API credential — using them is
# circumventing Slack's access controls, which its Developer Policy forbids. Longer
# prefixes first, so the rotating forms are named precisely.
REFUSED_TOKEN_PREFIXES: dict[str, str] = {
    "xoxe.xoxb-": "a rotating bot token",
    "xoxe.xapp-": "a rotating app-level token",
    "xoxb-": "a bot token",
    "xapp-": "an app-level token",
    "xoxc-": "a browser session token",
    "xoxd-": "a browser session cookie",
    "xoxe-": "a refresh token",
    "xoxr-": "a refresh token",
    "xoxa-": "a legacy workspace token",
    "xoxs-": "a legacy session token",
    "xwfp-": "a workflow token",
}


class TokenTypeError(RuntimeError):
    """Raised when SLACK_USER_TOKEN is set but is not a usable user token."""


class Settings(BaseSettings):
    """All configuration, derived from `SLACK_*` environment variables.

    Every field has a default so importing the package never fails; a missing or
    wrong-kind token surfaces as a descriptive error on the first request (the client
    calls `validate_token()` before sending anything).
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # User OAuth Token (`xoxp-…`) of your own internal Slack app, installed in ONE
    # workspace. Sent as `Authorization: Bearer <token>` on every request. It acts as
    # YOU — treat it like your password; re-install the app to rotate it.
    slack_user_token: str = ""

    # Web API base URL. Override only for proxies/testing. Must be https:// (plain http
    # only for 127.0.0.1 / localhost) — the token travels in a request header.
    slack_api_url: str = DEFAULT_API_URL

    slack_request_timeout_seconds: float = 30.0

    # Kill-switch for anything that changes workspace state (posting, editing,
    # scheduling, reacting, opening DMs). Off by default: those tools return an error
    # until SLACK_ALLOW_WRITES=1 — and even then a target must pass the allowlists below.
    slack_allow_writes: bool = False

    # Comma-separated conversation IDs (C…/G…/D…) a write may target. An ID listed here
    # is allowed even if the channel is shared with another organisation (Slack Connect).
    # Names (#general) are never matched: the guard compares IDs only.
    slack_write_channels: str = ""

    # Comma-separated user IDs (U…/W…). A DM or group DM is writable when every OTHER
    # member is listed here; `conversations.open` may only open conversations with them.
    slack_write_users: str = ""

    # MCP clients pass these via a JSON `env` block, where a pasted value often keeps a
    # trailing space/newline: the token then fails as a header value (and that error
    # echoes it back). Strip every string setting on the way in.
    @field_validator(
        "slack_user_token",
        "slack_api_url",
        "slack_write_channels",
        "slack_write_users",
        mode="before",
    )
    @classmethod
    def _strip_whitespace(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("slack_api_url", mode="after")
    @classmethod
    def _require_https(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme == "https" and parts.hostname:
            return value
        if parts.scheme == "http" and parts.hostname in LOOPBACK_HOSTS:
            return value
        raise ValueError(
            "SLACK_API_URL must be an https:// URL (plain http only for 127.0.0.1 / localhost): every request "
            "carries the token in a header, which plain http would send in cleartext."
        )

    @property
    def has_token(self) -> bool:
        return bool(self.slack_user_token)

    @property
    def base_url(self) -> str:
        """Effective API base without a trailing slash."""
        return self.slack_api_url.rstrip("/")

    @property
    def token_is_rotating(self) -> bool:
        """True for an `xoxe.xoxp-` token (expires; this server cannot refresh it)."""
        return self.slack_user_token.startswith(ROTATING_USER_TOKEN_PREFIX)

    @staticmethod
    def _split_csv(value: str) -> list[str]:
        seen: list[str] = []
        for raw in value.split(","):
            item = raw.strip()
            if item and item not in seen:
                seen.append(item)
        return seen

    @property
    def write_channels(self) -> list[str]:
        """`SLACK_WRITE_CHANNELS` as a de-duplicated list (order kept, blanks dropped)."""
        return self._split_csv(self.slack_write_channels)

    @property
    def write_users(self) -> list[str]:
        """`SLACK_WRITE_USERS` as a de-duplicated list (order kept, blanks dropped)."""
        return self._split_csv(self.slack_write_users)

    def token_problem(self) -> str | None:
        """`None` when the configured token is a usable user token; otherwise a message.

        The message never includes the token value — at most its prefix family.
        """
        token = self.slack_user_token
        if not token:
            return (
                "No Slack user token configured. Set SLACK_USER_TOKEN to the 'User OAuth Token' (xoxp-…) of your "
                "own Slack app (api.slack.com/apps → your app → OAuth & Permissions)."
            )
        if any(char.isspace() for char in token):
            # An inner space/newline makes an illegal header value, and the HTTP stack's
            # error for that quotes the header — i.e. the token. Refuse it up front.
            return (
                "SLACK_USER_TOKEN contains whitespace inside the value (a broken paste?). Copy the 'User OAuth "
                "Token' again from your app's OAuth & Permissions page."
            )
        if token.startswith(USER_TOKEN_PREFIXES):
            return None
        for prefix, kind in REFUSED_TOKEN_PREFIXES.items():
            if token.startswith(prefix):
                return (
                    f"SLACK_USER_TOKEN is {kind} ({prefix}…), which this server refuses: it speaks AS YOU and only "
                    "accepts a user token (xoxp-…) from your own app's 'User OAuth Token'. Bot tokens belong to a "
                    "bot gateway (e.g. Hermes), not here."
                )
        return (
            "SLACK_USER_TOKEN does not look like a Slack user token (expected it to start with xoxp- or "
            "xoxe.xoxp-). Copy the 'User OAuth Token' from your app's OAuth & Permissions page."
        )

    def validate_token(self) -> None:
        """Raise `RuntimeError` (missing) or `TokenTypeError` (unusable) unless the token is a user token."""
        problem = self.token_problem()
        if problem is None:
            return
        if not self.has_token:
            raise RuntimeError(problem)
        raise TokenTypeError(problem)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings singleton (tests call `get_settings.cache_clear()`)."""
    return Settings()
