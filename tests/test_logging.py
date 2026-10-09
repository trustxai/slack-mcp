"""Regression tests: httpx/httpcore request logs must not reach stderr.

FastMCP's constructor configures root logging at INFO to stderr, so without a fix the
httpx logger emits one "HTTP Request: ..." line per call. MCP clients persist stderr to
log files, so that line quietly writes every method the account calls to disk.
"""

from __future__ import annotations

import logging
import subprocess
import sys

import httpx
import pytest

from slack_mcp.client import SlackClient
from slack_mcp.config import Settings


def test_httpx_logger_effective_level_is_at_least_warning() -> None:
    """Checked in a fresh subprocess, not in-process.

    pytest's own logging plugin pre-attaches a handler to the root logger before any
    test runs, which makes the SDK's `logging.basicConfig` a silent no-op (it does
    nothing once the root logger already has handlers) — masking the bug if checked
    in-process. A bare subprocess has no such handler, so it reproduces the real,
    fresh-process behavior a running MCP server actually has.
    """
    script = (
        "import logging, slack_mcp.server\n"
        "level = logging.getLogger('httpx').getEffectiveLevel()\n"
        "core = logging.getLogger('httpcore').getEffectiveLevel()\n"
        "raise SystemExit(0 if level >= logging.WARNING and core >= logging.WARNING else 1)\n"
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, f"httpx effective level too low (stderr: {result.stderr})"


async def test_request_emits_no_httpx_log_record(caplog: pytest.LogCaptureFixture) -> None:
    import slack_mcp.server  # noqa: F401  (import triggers the fix under test)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    client = SlackClient(
        settings=Settings(slack_user_token="xoxp-FAKE-0123456789abcdef"),
        transport=httpx.MockTransport(handler),
    )

    with caplog.at_level(logging.INFO):
        await client.call("auth.test")

    assert not any(record.name.startswith("httpx") for record in caplog.records)
    assert not any("auth.test" in record.getMessage() for record in caplog.records)
    assert not any("xoxp-" in record.getMessage() for record in caplog.records)
