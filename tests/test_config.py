"""Unit tests for Settings, the token-kind check and the cached singleton."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from slack_mcp.config import (
    DEFAULT_API_URL,
    REFUSED_TOKEN_PREFIXES,
    Settings,
    TokenTypeError,
    get_settings,
)

USER_TOKEN = "xoxp-FAKE-0123456789abcdef"
ROTATING_USER_TOKEN = "xoxe.xoxp-FAKE-0123456789abcdef"


def test_defaults() -> None:
    settings = Settings()
    assert settings.slack_user_token == ""
    assert settings.slack_api_url == DEFAULT_API_URL == "https://slack.com/api"
    assert settings.slack_request_timeout_seconds == 30.0
    assert settings.slack_allow_writes is False
    assert settings.slack_write_channels == ""
    assert settings.slack_write_users == ""
    assert settings.has_token is False
    assert settings.base_url == DEFAULT_API_URL
    assert settings.write_channels == []
    assert settings.write_users == []
    assert settings.token_is_rotating is False


def test_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_USER_TOKEN", USER_TOKEN)
    monkeypatch.setenv("SLACK_API_URL", "https://proxy.example.test/api/")
    monkeypatch.setenv("SLACK_REQUEST_TIMEOUT_SECONDS", "5.5")
    monkeypatch.setenv("SLACK_ALLOW_WRITES", "1")
    monkeypatch.setenv("SLACK_WRITE_CHANNELS", "C0123ABCDEF")
    monkeypatch.setenv("SLACK_WRITE_USERS", "U0123ABCDEF,U0456GHIJKL")
    settings = Settings()
    assert settings.slack_user_token == USER_TOKEN
    assert settings.has_token is True
    assert settings.base_url == "https://proxy.example.test/api"
    assert settings.slack_request_timeout_seconds == 5.5
    assert settings.slack_allow_writes is True
    assert settings.write_channels == ["C0123ABCDEF"]
    assert settings.write_users == ["U0123ABCDEF", "U0456GHIJKL"]


@pytest.mark.parametrize("value", ["0", "false", "no"])
def test_allow_writes_off_values(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("SLACK_ALLOW_WRITES", value)
    assert Settings().slack_allow_writes is False


@pytest.mark.parametrize("pad", [" ", "\t", "\n", " \t\n"])
def test_string_settings_are_stripped(monkeypatch: pytest.MonkeyPatch, pad: str) -> None:
    # A pasted token with a trailing space/newline is an illegal header value, and the
    # resulting error echoes the token back to the model.
    monkeypatch.setenv("SLACK_USER_TOKEN", f"{pad}{USER_TOKEN}{pad}")
    monkeypatch.setenv("SLACK_API_URL", f"{pad}https://example.test/api/{pad}")
    monkeypatch.setenv("SLACK_WRITE_CHANNELS", f"{pad}C0123ABCDEF{pad}")
    monkeypatch.setenv("SLACK_WRITE_USERS", f"{pad}U0123ABCDEF{pad}")
    settings = Settings()
    assert settings.slack_user_token == USER_TOKEN
    assert settings.slack_api_url == "https://example.test/api/"
    assert settings.base_url == "https://example.test/api"
    assert settings.slack_write_channels == "C0123ABCDEF"
    assert settings.slack_write_users == "U0123ABCDEF"
    assert settings.token_problem() is None


@pytest.mark.parametrize("value", ["", "   ", "\n"])
def test_blank_token_is_empty(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("SLACK_USER_TOKEN", value)
    settings = Settings()
    assert settings.slack_user_token == ""
    assert settings.has_token is False


def test_csv_parsing_of_the_allowlists(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_WRITE_CHANNELS", " C0123ABCDEF, D0456GHIJKL ,,G0789MNOPQR, C0123ABCDEF ,")
    monkeypatch.setenv("SLACK_WRITE_USERS", "U0123ABCDEF,,U0123ABCDEF, W0456GHIJKL")
    settings = Settings()
    assert settings.write_channels == ["C0123ABCDEF", "D0456GHIJKL", "G0789MNOPQR"]
    assert settings.write_users == ["U0123ABCDEF", "W0456GHIJKL"]
    assert Settings(slack_write_users=",, ,").write_users == []


# --- the API URL must be https (plain http only for loopback) -------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://slack.com/api",
        "https://proxy.example.test/slack/api/",
        "http://127.0.0.1:8080/api",
        "http://localhost/api",
        "http://localhost:9999",
    ],
)
def test_https_or_loopback_http_api_urls_are_accepted(url: str) -> None:
    assert Settings(slack_api_url=url).slack_api_url == url


@pytest.mark.parametrize(
    "url",
    [
        "http://slack.com/api",
        "http://proxy.example.test/api",
        "http://127.0.0.1.evil.example/api",
        "http://localhost.evil.example/api",
        "ftp://slack.com/api",
        "slack.com/api",
        "https://",
        "",
    ],
)
def test_cleartext_or_malformed_api_urls_are_refused(url: str) -> None:
    with pytest.raises(ValidationError, match="must be an https:// URL"):
        Settings(slack_api_url=url)


def test_an_http_api_url_in_the_env_fails_at_settings_load(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_API_URL", "http://slack.com/api")
    with pytest.raises(ValidationError, match="plain http only for 127.0.0.1 / localhost"):
        get_settings()


# --- the token-kind check -------------------------------------------------------------


def test_missing_token_problem_names_the_variable() -> None:
    problem = Settings().token_problem()
    assert problem is not None
    assert "No Slack user token configured" in problem
    assert "SLACK_USER_TOKEN" in problem


@pytest.mark.parametrize("token", [USER_TOKEN, ROTATING_USER_TOKEN])
def test_user_tokens_are_accepted(token: str) -> None:
    settings = Settings(slack_user_token=token)
    assert settings.token_problem() is None
    settings.validate_token()  # no raise


def test_rotating_user_token_is_flagged() -> None:
    assert Settings(slack_user_token=ROTATING_USER_TOKEN).token_is_rotating is True
    assert Settings(slack_user_token=USER_TOKEN).token_is_rotating is False


@pytest.mark.parametrize(("prefix", "kind"), list(REFUSED_TOKEN_PREFIXES.items()))
def test_every_refused_prefix_names_its_kind_and_never_the_value(prefix: str, kind: str) -> None:
    token = f"{prefix}FAKE-0123456789abcdef"
    settings = Settings(slack_user_token=token)
    problem = settings.token_problem()
    assert problem is not None
    assert kind in problem
    assert f"({prefix}…)" in problem
    assert "refuses" in problem
    assert token not in problem
    assert "FAKE-0123456789abcdef" not in problem
    with pytest.raises(TokenTypeError) as info:
        settings.validate_token()
    assert token not in str(info.value)


def test_the_refused_list_covers_bot_app_browser_and_rotating_kinds() -> None:
    for prefix in ("xoxb-", "xapp-", "xoxc-", "xoxd-", "xoxe.xoxb-", "xoxe.xapp-", "xoxe-"):
        assert prefix in REFUSED_TOKEN_PREFIXES


def test_rotating_bot_token_is_named_precisely() -> None:
    problem = Settings(slack_user_token="xoxe.xoxb-FAKE-0123456789abcdef").token_problem()
    assert problem is not None
    assert "a rotating bot token (xoxe.xoxb-…)" in problem


def test_unknown_shape_is_refused_without_echo() -> None:
    token = "not-a-slack-token-0123456789"
    settings = Settings(slack_user_token=token)
    problem = settings.token_problem()
    assert problem is not None
    assert "does not look like a Slack user token" in problem
    assert token not in problem
    with pytest.raises(TokenTypeError):
        settings.validate_token()


def test_inner_whitespace_is_refused_without_echo() -> None:
    token = "xoxp-FAKE-0123\n456789abcdef"
    settings = Settings(slack_user_token=token)
    problem = settings.token_problem()
    assert problem is not None
    assert "whitespace" in problem
    assert "0123" not in problem
    with pytest.raises(TokenTypeError):
        settings.validate_token()


def test_validate_token_raises_runtime_error_when_missing() -> None:
    with pytest.raises(RuntimeError, match="No Slack user token configured") as info:
        Settings().validate_token()
    # Missing is a plain RuntimeError, not the wrong-kind error.
    assert not isinstance(info.value, TokenTypeError)


def test_token_type_error_is_a_runtime_error() -> None:
    assert issubclass(TokenTypeError, RuntimeError)


def test_get_settings_is_cached() -> None:
    assert get_settings() is get_settings()


# --- SLACK_SIGNATURE -------------------------------------------------------------------

SIGNATURE = "Sent using <https://masavaco.slack.com/marketplace/A0C835DFNSH|@Alejandro AI>"


def test_signature_is_off_by_default() -> None:
    settings = Settings()
    assert settings.slack_signature == ""
    assert settings.signature is None


def test_signature_from_the_env_is_whitespace_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_SIGNATURE", f"  {SIGNATURE} \n")
    get_settings.cache_clear()
    assert get_settings().signature == SIGNATURE
    assert get_settings().slack_signature == SIGNATURE


@pytest.mark.parametrize("value", ["", "   ", "\n", " \r\n\t "])
def test_a_blank_signature_is_off(value: str) -> None:
    assert Settings(slack_signature=value).signature is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("— enviado con\nAlejandro AI", "— enviado con Alejandro AI"),
        (
            "  Sent using\r\n   <https://x.slack.com/marketplace/A1|@App>  \n\n",
            "Sent using <https://x.slack.com/marketplace/A1|@App>",
        ),
        ("one\n\n\ntwo\nthree", "one two three"),
        ("keeps  inner   spaces", "keeps  inner   spaces"),
    ],
)
def test_a_multi_line_signature_is_collapsed_to_one_line(value: str, expected: str) -> None:
    settings = Settings(slack_signature=value)
    assert settings.signature == expected
    assert "\n" not in settings.slack_signature
    assert "\r" not in settings.slack_signature
