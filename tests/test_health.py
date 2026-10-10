"""Unit tests for the health-check tool against a fake client."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from slack_mcp.config import REFUSED_TOKEN_PREFIXES, Settings, get_settings
from slack_mcp.errors import SlackApiError
from slack_mcp.tools.health import REQUIRED_USER_SCOPES, slack_health_check

TOKEN = "xoxp-FAKE-0123456789abcdef"


class _FakeClient:
    """Routes by method name; records every call as (method, params, kwargs)."""

    def __init__(self, routes: dict[str, Any] | None = None, exc: Exception | None = None) -> None:
        self._routes = routes or {}
        self._exc = exc
        self.calls: list[tuple[str, Any, dict[str, Any]]] = []
        self.last_retry_after: str | None = None
        self.last_scopes: str | None = None

    async def call(self, method: str, params: Any = None, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((method, params, kwargs))
        if self._exc is not None:
            raise self._exc
        payload = self._routes.get(method)
        if isinstance(payload, Exception):
            raise payload
        return dict(payload) if payload is not None else {"ok": True}


AUTH_TEST = {
    "ok": True,
    "url": "https://udocz.slack.com/",
    "team": "uDocz",
    "team_id": "T0123ABCDEF",
    "user": "alejandro",
    "user_id": "U0SELF00001",
}


def _patch(monkeypatch: pytest.MonkeyPatch, fake: _FakeClient, settings: Settings) -> None:
    monkeypatch.setattr("slack_mcp.tools.health.get_client", lambda: fake)
    monkeypatch.setattr("slack_mcp.tools.health.get_settings", lambda: settings)


async def test_health_without_token_reports_config_only(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient()
    _patch(monkeypatch, fake, Settings())

    result = await slack_health_check()

    assert "base URL**: https://slack.com/api" in result
    assert "writes disabled (read-only" in result
    assert "No Slack user token configured" in result
    assert fake.calls == []


@pytest.mark.parametrize("token", ["", "xoxb-FAKE-0123456789abcdef"])
async def test_the_early_return_is_redacted_too(monkeypatch: pytest.MonkeyPatch, token: str) -> None:
    # The missing / refused token path returns before calling Slack; it must still scrub.
    fake = _FakeClient()
    url = "https://proxy.example.test/xoxp-FAKE-0123456789abcdef/api"
    _patch(monkeypatch, fake, Settings(slack_api_url=url, slack_user_token=token))

    result = await slack_health_check()

    assert "base URL**: https://proxy.example.test/***/api" in result
    assert "xoxp-FAKE" not in result
    assert "xoxb-FAKE" not in result
    assert "- **token**: " in result
    assert fake.calls == []


@pytest.mark.parametrize(("prefix", "kind"), list(REFUSED_TOKEN_PREFIXES.items()))
async def test_health_with_a_refused_token_kind_reports_it_without_calling(
    monkeypatch: pytest.MonkeyPatch, prefix: str, kind: str
) -> None:
    fake = _FakeClient()
    token = f"{prefix}FAKE-0123456789abcdef"
    _patch(monkeypatch, fake, Settings(slack_user_token=token))

    result = await slack_health_check()

    assert f"token**: SLACK_USER_TOKEN is {kind}" in result
    assert token not in result
    assert fake.calls == []


async def test_health_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(routes={"auth.test": AUTH_TEST})
    fake.last_scopes = ",".join((*REQUIRED_USER_SCOPES, "identify"))
    _patch(
        monkeypatch,
        fake,
        Settings(
            slack_user_token=TOKEN,
            slack_allow_writes=True,
            slack_write_channels="C0123ABCDEF",
            slack_write_users="U0ANA000001,U0BOB000001",
        ),
    )

    result = await slack_health_check()

    assert "write guard**: ENABLED — 1 allowed channel id(s), 2 allowed user id(s)" in result
    assert "Slack Connect channels only by explicit id" in result
    assert "token**: user token (xoxp) — every call runs as you" in result
    assert "connectivity**: OK" in result
    assert "workspace**: uDocz (T0123ABCDEF) — https://udocz.slack.com/" in result
    assert "you**: alejandro (U0SELF00001)" in result
    assert f"scopes granted**: {len(REQUIRED_USER_SCOPES) + 1}" in result
    assert "every scope the v0.1 tools need is granted" in result
    assert "MISSING" not in result
    assert "warning" not in result
    assert TOKEN not in result
    assert fake.calls == [("auth.test", None, {})]


SIGNATURE = "Sent using <https://masavaco.slack.com/marketplace/A0C835DFNSH|@Alejandro AI>"


async def test_health_reports_the_signature_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(routes={"auth.test": AUTH_TEST})
    _patch(monkeypatch, fake, Settings(slack_user_token=TOKEN))

    result = await slack_health_check()

    assert "- **signature**: off (set SLACK_SIGNATURE so teammates can tell these messages apart)" in result
    assert "signature**: on" not in result


async def test_health_reports_the_signature_on_with_its_length(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(routes={"auth.test": AUTH_TEST})
    _patch(monkeypatch, fake, Settings(slack_user_token=TOKEN, slack_signature=f" {SIGNATURE}\n"))

    result = await slack_health_check()

    assert f"- **signature**: on ({len(SIGNATURE)} chars) — appended to every message" in result
    assert "signature**: off" not in result


async def test_health_reports_the_signature_even_without_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient()
    _patch(monkeypatch, fake, Settings(slack_signature="— enviado con Alejandro AI"))

    result = await slack_health_check()

    assert "- **signature**: on (26 chars) — appended to every message" in result
    assert "No Slack user token configured" in result
    assert fake.calls == []


def test_the_health_docstring_states_the_measured_fact() -> None:
    doc = " ".join((slack_health_check.__doc__ or "").split())
    assert "Slack does not label messages posted with a user token — they look exactly like ones you typed" in doc
    assert "`SLACK_SIGNATURE`" in doc
    assert "Sent using" not in doc


async def test_health_lists_missing_scopes(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(routes={"auth.test": AUTH_TEST})
    fake.last_scopes = "channels:read, chat:write"
    _patch(monkeypatch, fake, Settings(slack_user_token=TOKEN))

    result = await slack_health_check()

    assert "scopes granted**: 2 (channels:read, chat:write)" in result
    assert "scopes MISSING for the v0.1 tools**: channels:history, groups:read" in result
    assert "search:read" in result.split("MISSING", 1)[1]
    assert "chat:write" not in result.split("MISSING", 1)[1]


async def test_health_without_the_scopes_header(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(routes={"auth.test": AUTH_TEST})
    _patch(monkeypatch, fake, Settings(slack_user_token=TOKEN))

    result = await slack_health_check()

    assert "did not return the X-OAuth-Scopes header" in result


async def test_health_reports_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(routes={"auth.test": AUTH_TEST})
    fake.last_retry_after = "9"
    _patch(monkeypatch, fake, Settings(slack_user_token=TOKEN))

    result = await slack_health_check()

    assert "retry-after**: 9s" in result


@pytest.mark.parametrize("body", [{"bot_id": "B0123ABCDEF"}, {"user": "bot"}])
async def test_health_warns_when_slack_reports_a_bot(monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]) -> None:
    fake = _FakeClient(routes={"auth.test": {**AUTH_TEST, **body}})
    _patch(monkeypatch, fake, Settings(slack_user_token=TOKEN))

    result = await slack_health_check()

    assert "warning**: Slack reports this token as a BOT" in result


async def test_health_warns_about_a_rotating_token(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(routes={"auth.test": AUTH_TEST})
    _patch(monkeypatch, fake, Settings(slack_user_token="xoxe.xoxp-FAKE-0123456789abcdef"))

    result = await slack_health_check()

    assert "rotating user token (xoxe.xoxp)" in result
    assert "turn token rotation OFF" in result


async def test_health_invalid_auth_is_rendered_with_the_token_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(routes={"auth.test": SlackApiError("invalid_auth", method="auth.test")})
    _patch(monkeypatch, fake, Settings(slack_user_token=TOKEN))

    result = await slack_health_check()

    assert result.startswith("Error (invalid_auth): Slack refused auth.test: invalid_auth.")
    assert "Re-install your Slack app" in result


def test_fifteen_required_scopes_without_reactions_read_or_channels_write() -> None:
    assert len(REQUIRED_USER_SCOPES) == 15
    assert len(set(REQUIRED_USER_SCOPES)) == 15
    assert "reactions:read" not in REQUIRED_USER_SCOPES
    assert "channels:write" not in REQUIRED_USER_SCOPES
    assert "reactions:write" in REQUIRED_USER_SCOPES


async def test_health_with_an_invalid_setting_reports_it_without_calling(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient()
    monkeypatch.setattr("slack_mcp.tools.health.get_client", lambda: fake)
    monkeypatch.setenv("SLACK_API_URL", "http://slack.com/api")
    monkeypatch.setenv("SLACK_USER_TOKEN", TOKEN)

    result = await slack_health_check()

    assert result.startswith("Error: invalid configuration – SLACK_API_URL: ")
    assert TOKEN not in result
    assert fake.calls == []


async def test_health_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(exc=httpx.ConnectError("refused"))
    _patch(monkeypatch, fake, Settings(slack_user_token=TOKEN))

    result = await slack_health_check()

    assert result.startswith("Error")
    assert "could not connect" in result


@pytest.mark.live
async def test_health_live_smoke() -> None:
    """Real call with the developer's token: connectivity, identity and scopes."""
    get_settings.cache_clear()
    result = await slack_health_check()

    assert "connectivity**: OK" in result
    assert "you**:" in result
    assert "scopes granted**:" in result
