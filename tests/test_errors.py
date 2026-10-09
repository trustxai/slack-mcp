"""Unit tests for error mapping and credential redaction."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from pydantic import BaseModel, ValidationError

from slack_mcp.config import Settings, TokenTypeError
from slack_mcp.errors import SlackApiError, handle_api_error, hint_for, redact_credentials
from slack_mcp.guard import WriteNotAllowedError, WritesDisabledError

TOKEN = "xoxp-FAKE-0123456789abcdef"


def _status_error(
    status: int, body: Any = None, *, text: str | None = None, headers: dict[str, str] | None = None
) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://slack.com/api/chat.postMessage")
    if text is not None:
        response = httpx.Response(status, text=text, request=request, headers=headers)
    else:
        response = httpx.Response(status, json=body, request=request, headers=headers)
    return httpx.HTTPStatusError("boom", request=request, response=response)


def _use_settings(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    monkeypatch.setattr("slack_mcp.errors.get_settings", lambda: settings)


# --- SlackApiError rendering ------------------------------------------------------------


def test_slack_api_error_carries_the_envelope() -> None:
    body = {"ok": False, "error": "missing_scope", "needed": "search:read", "provided": "chat:write"}
    exc = SlackApiError(
        "missing_scope", method="search.messages", needed="search:read", provided="chat:write", body=body
    )
    assert exc.code == "missing_scope"
    assert exc.method == "search.messages"
    assert exc.body == body
    assert str(exc) == "Slack refused search.messages: missing_scope (needs scope search:read; token has chat:write)"
    assert SlackApiError("x").body == {}
    assert SlackApiError("x").messages == []


def test_missing_scope_renders_needed_provided_and_the_hint() -> None:
    message = handle_api_error(
        SlackApiError("missing_scope", method="search.messages", needed="search:read", provided="chat:write,users:read")
    )
    assert message.startswith(
        "Error (missing_scope): Slack refused search.messages: missing_scope "
        "(needs scope search:read; token has chat:write,users:read)."
    )
    assert "User Token Scopes" in message
    assert "re-install the app" in message


def test_response_metadata_messages_are_rendered() -> None:
    message = handle_api_error(
        SlackApiError(
            "invalid_arguments", method="chat.postMessage", messages=["[ERROR] missing required field: channel"]
        )
    )
    assert "— [ERROR] missing required field: channel." in message
    assert "check the ids and values you sent" in message


@pytest.mark.parametrize(
    ("code", "fragment"),
    [
        ("not_in_channel", "Join it in Slack first"),
        ("channel_not_found", "slack_list_conversations"),
        ("msg_too_long", "exceeds Slack's limit for this method"),
        ("message_truncated", "WAS posted"),
        ("cant_update_message", "Only your own messages"),
        ("users_not_found", "slack_resolve_user"),
        ("too_many_users", "at most 9 people including you"),
        ("time_too_far", "120 days"),
        ("time_in_past", "future"),
        ("already_reacted", "end state is what you asked for"),
        ("rate_limited", "too many messages"),
        ("invalid_auth", "Re-install your Slack app"),
        ("token_revoked", "Re-install your Slack app"),
        ("internal_error", "UNKNOWN"),
    ],
)
def test_hint_table(code: str, fragment: str) -> None:
    assert fragment in hint_for(code)
    assert fragment in handle_api_error(SlackApiError(code, method="chat.postMessage"))


def test_msg_too_long_hint_depends_on_the_method() -> None:
    update = handle_api_error(SlackApiError("msg_too_long", method="chat.update"))
    assert "chat.update refuses more than 4,000 characters" in update
    for method in ("chat.postMessage", "chat.scheduleMessage", ""):
        message = handle_api_error(SlackApiError("msg_too_long", method=method))
        assert "4,000" not in message
        assert "exceeds Slack's limit for this method" in message
    assert hint_for("msg_too_long", "chat.update") != hint_for("msg_too_long")


def test_unknown_code_has_no_hint() -> None:
    assert hint_for("some_new_code") == ""
    assert handle_api_error(SlackApiError("some_new_code", method="x.y")) == (
        "Error (some_new_code): Slack refused x.y: some_new_code."
    )


def test_non_json_response_hint() -> None:
    message = handle_api_error(SlackApiError("non_json_response", method="auth.test"))
    assert "may or may not have run" in message


# --- HTTP status errors -----------------------------------------------------------------


def test_429_mentions_retry_after() -> None:
    message = handle_api_error(_status_error(429, {"ok": False, "error": "ratelimited"}, headers={"retry-after": "30"}))
    assert message.startswith("Error (429): Rate limited by Slack – ratelimited. Retry-After: 30s.")
    assert "tight loop" in message


def test_429_without_body_or_header() -> None:
    message = handle_api_error(_status_error(429, text=""))
    assert message.startswith("Error (429): Rate limited by Slack")
    assert "Retry-After" not in message


@pytest.mark.parametrize("status", [401, 403])
def test_401_and_403_carry_the_token_hint(status: int) -> None:
    message = handle_api_error(_status_error(status, {"ok": False, "error": "invalid_auth"}))
    assert message.startswith(f"Error ({status}): invalid_auth.")
    assert "Re-install your Slack app" in message


@pytest.mark.parametrize("status", [500, 502, 503])
def test_5xx_is_execution_unknown(status: int) -> None:
    message = handle_api_error(_status_error(status, text="<html>bad gateway</html>"))
    assert message.startswith(f"Error ({status}): Slack-side failure – <html>bad gateway</html>.")
    assert "UNKNOWN" in message
    assert "duplicate message" in message


def test_other_status_falls_through() -> None:
    assert handle_api_error(_status_error(418, {"ok": False, "error": "teapot"})) == "Error (418): teapot"


# --- transport errors never echo a header value ----------------------------------------


def test_transport_errors_are_described_by_type() -> None:
    assert "timed out" in handle_api_error(httpx.ReadTimeout("t"))
    assert "UNKNOWN" in handle_api_error(httpx.ReadTimeout("t"))
    assert "could not connect" in handle_api_error(httpx.ConnectError("c"))
    local = handle_api_error(httpx.LocalProtocolError("Illegal header value b'Bearer SOME-HEADER-VALUE'"))
    assert "SOME-HEADER-VALUE" not in local
    assert "stray whitespace" in local
    assert "must start with https://" in handle_api_error(httpx.UnsupportedProtocol("u"))
    generic = handle_api_error(httpx.ReadError("leaky detail SOME-HEADER-VALUE"))
    assert "network layer (ReadError)" in generic
    assert "SOME-HEADER-VALUE" not in generic


def test_a_settings_validation_error_names_the_variable_but_never_the_value() -> None:
    with pytest.raises(ValidationError) as info:
        Settings(slack_api_url="http://evil.example.test/api", slack_user_token="xoxp-FAKE-0123456789abcdef")
    message = handle_api_error(info.value)
    assert message.startswith("Error: invalid configuration – SLACK_API_URL: ")
    assert "must be an https:// URL" in message
    assert "evil.example.test" not in message  # the input value is never echoed
    assert "xoxp-FAKE" not in message


def test_a_non_settings_validation_error_is_invalid_input() -> None:
    class _Model(BaseModel):
        limit: int

    with pytest.raises(ValidationError) as info:
        _Model(limit="many")  # type: ignore[arg-type]
    message = handle_api_error(info.value)
    assert message.startswith("Error: invalid input – LIMIT: ")
    assert "many" not in message


def test_guard_and_token_errors_render_as_plain_errors() -> None:
    assert handle_api_error(WritesDisabledError("writes are disabled")) == "Error: writes are disabled"
    assert handle_api_error(WriteNotAllowedError("not in SLACK_WRITE_CHANNELS")) == (
        "Error: not in SLACK_WRITE_CHANNELS"
    )
    assert handle_api_error(TokenTypeError("SLACK_USER_TOKEN is a bot token")) == (
        "Error: SLACK_USER_TOKEN is a bot token"
    )
    assert handle_api_error(ValueError("nope")) == "Error: unexpected failure – ValueError: nope"


# --- redaction ----------------------------------------------------------------------------


def test_redaction_covers_the_configured_token_in_raw_stripped_and_repr_forms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Settings strip outer whitespace, so the stored value is the stripped one; a value
    # with an INNER newline keeps it, and its repr-escaped form must be covered too. A
    # value that does not look like a Slack token (e.g. behind a proxy) relies on this pass.
    secret = "CUSTOM-SECRET-abc\ndef123"
    _use_settings(monkeypatch, Settings(slack_user_token=secret))
    text = "raw=CUSTOM-SECRET-abc\ndef123 repr='CUSTOM-SECRET-abc\\ndef123' end"
    assert redact_credentials(text) == "raw=*** repr='***' end"


def test_redaction_covers_the_configured_user_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_settings(monkeypatch, Settings(slack_user_token=TOKEN))
    assert redact_credentials(f"header Bearer {TOKEN} sent") == "header Bearer *** sent"


@pytest.mark.parametrize(
    "token",
    [
        "xoxb-FAKE-0123456789abcdef",
        "xoxp-1111-2222-3333-FAKEFAKEFAKE0123456789",
        "xoxe.xoxp-1-FAKE0123456789",
        "xoxe-1-FAKEREFRESH0123456789",
        "xapp-1-A0123-FAKE0123456789",
        "xoxe.xapp-1-FAKE0123456789",
        "xwfp-FAKE-0123456789abcdef",
        "xoxc-FAKE-0123456789abcdef",
    ],
)
def test_redaction_of_unconfigured_slack_tokens_by_pattern(monkeypatch: pytest.MonkeyPatch, token: str) -> None:
    _use_settings(monkeypatch, Settings())
    assert redact_credentials(f"Slack echoed {token}.") == "Slack echoed ***."


@pytest.mark.parametrize("token", ["XOXB-FAKE-0123456789ABCDEF", "XOXE.XOXP-1-FAKE0123456789", "XAPP-1-A0123-FAKE0123"])
def test_redaction_pattern_ignores_case(monkeypatch: pytest.MonkeyPatch, token: str) -> None:
    _use_settings(monkeypatch, Settings())
    assert redact_credentials(f"Slack echoed {token}.") == "Slack echoed ***."


def test_an_upper_cased_token_shaped_field_name_is_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_describe` upper-cases the location BEFORE redaction, so the field name arrives as XOXB-…."""
    _use_settings(monkeypatch, Settings())

    class _Model(BaseModel):
        model_config = {"extra": "forbid"}

    field = "xoxb-1234567890-abcdefghij"
    with pytest.raises(ValidationError) as info:
        _Model.model_validate({field: 1})
    message = handle_api_error(info.value)

    assert message == "Error: invalid input – ***: Extra inputs are not permitted"
    assert field.upper() not in message


def test_redaction_leaves_short_or_prefix_only_mentions_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_settings(monkeypatch, Settings())
    text = "expected xoxp-… or xoxe.xoxp-…; xoxb-short is fine"
    assert redact_credentials(text) == text


def test_redaction_skips_short_configured_values(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_settings(monkeypatch, Settings(slack_user_token="short"))
    assert redact_credentials("a short word") == "a short word"


def test_redaction_survives_broken_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> Settings:
        raise ValueError("bad env")

    monkeypatch.setattr("slack_mcp.errors.get_settings", boom)
    assert redact_credentials("unchanged") == "unchanged"
    assert redact_credentials("leak xoxb-FAKE-0123456789abcdef") == "leak ***"


def test_handle_api_error_redacts_a_token_in_an_error_body(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_settings(monkeypatch, Settings(slack_user_token=TOKEN))
    exc = SlackApiError("invalid_auth", method="auth.test", messages=[f"token {TOKEN} is invalid"])
    message = handle_api_error(exc)
    assert TOKEN not in message
    assert "token *** is invalid" in message
    status_message = handle_api_error(_status_error(400, text=f"bad request for {TOKEN}"))
    assert TOKEN not in status_message
    assert handle_api_error(RuntimeError(f"leaked {TOKEN} here")) == "Error: leaked *** here"
