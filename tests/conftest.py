"""Shared pytest configuration: marker gating + settings isolation.

Two live tiers, gated here so the standard gate can never reach Slack for real:

- `live` (read-only) runs when SLACK_USER_TOKEN is set (shell or .env).
- `live_write` (posts in YOUR self-DM) runs only when ALL of these hold: the token is set,
  `SLACK_TEST_ALLOW_WRITES=1` was exported in the SHELL that launched pytest (a value in
  .env is ignored on purpose), and the `-m` expression is exactly `live_write`.
  `-m "not live"` alone does not deselect a test marked only `live_write`, and a compound
  expression can select one by accident (`not live or not (live_write)` does), so only
  the bare `-m live_write` opens the gate — this last condition is what keeps every other
  run from ever writing.
"""

from __future__ import annotations

import os

import pytest
from dotenv import load_dotenv

import slack_mcp.client
from slack_mcp.config import get_settings

# Read BEFORE load_dotenv(): the write switch must come from the shell, never from a file.
_WRITES_ALLOWED_BY_SHELL = os.environ.get("SLACK_TEST_ALLOW_WRITES") == "1"

# Load .env so live-marked tests can pick up the real token; non-live tests are isolated
# from it by the autouse fixture below.
load_dotenv()
if not _WRITES_ALLOWED_BY_SHELL:
    # A value that came from .env must not be visible to anything downstream either.
    os.environ.pop("SLACK_TEST_ALLOW_WRITES", None)


def selects_live_write(markexpr: str | None) -> bool:
    """True only for the bare expression `-m live_write` (no compound expression opens the gate)."""
    return (markexpr or "").strip() == "live_write"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    has_token = bool(os.environ.get("SLACK_USER_TOKEN"))
    writes_requested = selects_live_write(config.getoption("markexpr"))
    skip_live = pytest.mark.skip(reason="live tests require SLACK_USER_TOKEN in the environment / .env")
    skip_write = pytest.mark.skip(
        reason=(
            "live_write tests run ONLY with SLACK_USER_TOKEN set, SLACK_TEST_ALLOW_WRITES=1 exported in the shell "
            "(never .env), and `-m live_write`"
        )
    )
    for item in items:
        if "live" in item.keywords and not has_token:
            item.add_marker(skip_live)
        if "live_write" in item.keywords and not (has_token and _WRITES_ALLOWED_BY_SHELL and writes_requested):
            item.add_marker(skip_write)


def _reset_caches() -> None:
    get_settings.cache_clear()
    slack_mcp.client.reset_client()


@pytest.fixture(autouse=True)
def _isolate_settings_env(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Keep the developer's real SLACK_* env / .env out of non-live tests.

    Strips ambient SLACK_* vars, chdirs away from the repo (so `Settings(env_file=".env")`
    finds nothing), and clears the settings cache and the client singleton (with its
    guard cache) before and after each non-live test. Live tests are left untouched.
    """
    if "live" in request.keywords or "live_write" in request.keywords:
        return
    for key in list(os.environ):
        if key.startswith("SLACK_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path_factory.mktemp("isolated-cwd"))
    _reset_caches()
    request.addfinalizer(_reset_caches)
