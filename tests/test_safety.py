"""SDK-raised errors and log lines are redacted (safety.py, wired in server.py).

FastMCP validates a tool's arguments BEFORE the tool body runs, so neither
`handle_api_error` nor `clip_response` ever sees that failure. These tests measure what
leaves the server: `mcp.call_tool` (the protocol handler's funnel), a real in-memory
client session (`isError`), and a real stdio subprocess (responses + stderr).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.shared.exceptions import McpError
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import ErrorData, TextContent

from slack_mcp.config import get_settings
from slack_mcp.errors import SlackApiError
from slack_mcp.safety import (
    MAX_ERROR_MESSAGE_CHARS,
    RedactingFastMCP,
    RedactingLogFilter,
    install_log_redaction,
)
from slack_mcp.server import mcp
from slack_mcp.tools.conversations import GetHistoryInput, slack_get_history

FAKE_BOT = "xoxb-1234567890-abcdefghij"
FAKE_USER = "xoxp-1234567890-abcdefghij"
CHANNEL = "C0123ABCDEF"


class _FakeClient:
    def __init__(self, routes: dict[str, Any]) -> None:
        self._routes = routes
        self.calls: list[tuple[str, Any]] = []

    async def call(self, method: str, params: Any = None, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((method, params))
        payload = self._routes[method]
        if isinstance(payload, Exception):
            raise payload
        return dict(payload)


async def _sdk_error(name: str, arguments: dict[str, Any]) -> str:
    with pytest.raises(ToolError) as info:
        await mcp.call_tool(name, arguments)
    return str(info.value)


def _assert_scrubbed(text: str, *secrets: str, masked: bool = True) -> None:
    assert ("***" in text) is masked, text
    for secret in secrets:
        assert secret not in text, text
    assert "xox" not in text, text
    assert "input_value" not in text, text


# --- argument validation (raised by the SDK, before the tool body) --------------------------


@pytest.mark.parametrize(
    ("name", "arguments", "location", "rule", "masked"),
    [
        pytest.param(
            "slack_get_history",
            {"params": {"channel": FAKE_BOT}},
            "params.channel",
            "[type=value_error]",
            True,
            id="token-shaped-channel",
        ),
        pytest.param(
            "slack_open_conversation",
            {"params": {"users": ["U0123ABCDEF", FAKE_USER]}},
            "params.users",
            "[type=value_error]",
            True,
            id="token-inside-a-list-field",
        ),
        pytest.param(
            "slack_get_history",
            {"params": {"channel": CHANNEL, "note": FAKE_BOT}},
            "params.note",
            "Extra inputs are not permitted [type=extra_forbidden]",
            False,
            id="token-as-the-value-of-an-unknown-field",
        ),
        pytest.param(
            "slack_get_history",
            {"params": {"channel": CHANNEL, FAKE_BOT: 1}},
            "params.***",
            "Extra inputs are not permitted [type=extra_forbidden]",
            True,
            id="token-as-the-name-of-an-unknown-field",
        ),
        pytest.param(
            "slack_get_history",
            {"params": json.dumps({"channel": FAKE_BOT})},
            "params.channel",
            "[type=value_error]",
            True,
            id="params-sent-as-a-json-string",
        ),
    ],
)
async def test_sdk_validation_error_is_redacted(
    name: str, arguments: dict[str, Any], location: str, rule: str, masked: bool
) -> None:
    """`masked=False`: the value of an unknown field is not echoed at all any more."""
    text = await _sdk_error(name, arguments)

    _assert_scrubbed(text, FAKE_BOT, FAKE_USER, masked=masked)
    assert text.startswith(f"Error executing tool {name}: 1 validation error for {name}Arguments\n"), text
    assert f"\n{location}\n" in text, text
    assert rule in text, text


async def test_the_configured_token_is_redacted_even_when_not_slack_shaped(monkeypatch: pytest.MonkeyPatch) -> None:
    configured = "configured-secret-value-0123456789"
    monkeypatch.setenv("SLACK_USER_TOKEN", configured)
    get_settings.cache_clear()

    text = await _sdk_error("slack_get_history", {"params": {"channel": configured}})

    assert configured not in text, text
    assert "got '***'" in text, text


async def test_a_normal_validation_error_stays_readable() -> None:
    text = await _sdk_error("slack_get_history", {"params": {"channel": "general"}})

    assert text == (
        "Error executing tool slack_get_history: 1 validation error for slack_get_historyArguments\n"
        "params.channel\n"
        "  Value error, channel must be a Slack conversation ID like C0123ABCDEF (not a #name — resolve it with "
        "slack_list_conversations first), got 'general' [type=value_error]"
    )


async def test_several_errors_are_counted_and_each_is_located() -> None:
    text = await _sdk_error("slack_get_history", {"params": {"limit": "lots", "extra": 1}})

    assert text.startswith(
        "Error executing tool slack_get_history: 3 validation errors for slack_get_historyArguments\n"
    ), text
    assert "params.channel\n  Field required [type=missing]" in text, text
    assert "params.limit\n" in text, text
    assert "params.extra\n  Extra inputs are not permitted [type=extra_forbidden]" in text, text


async def test_a_huge_echoed_value_is_clipped_after_redaction() -> None:
    huge = "x" * 5_000 + FAKE_BOT

    text = await _sdk_error("slack_get_history", {"params": {"channel": huge}})

    assert "[truncated]" in text, text
    assert len(text) < MAX_ERROR_MESSAGE_CHARS + 300, len(text)
    assert "xox" not in text


async def test_an_unknown_token_shaped_tool_name_is_redacted() -> None:
    text = await _sdk_error(FAKE_BOT, {})

    assert text == "Unknown tool: ***"


async def test_over_the_protocol_the_client_gets_is_error_with_redacted_text() -> None:
    async with create_connected_server_and_client_session(mcp) as client:
        result = await client.call_tool("slack_get_history", {"params": {"channel": FAKE_BOT}})

    assert result.isError is True
    [block] = result.content
    assert isinstance(block, TextContent)
    _assert_scrubbed(block.text, FAKE_BOT)
    assert "got '***'" in block.text


# --- what the tools return is unchanged ------------------------------------------------------

HISTORY = {
    "ok": True,
    "messages": [{"type": "message", "ts": "1791547200.000100", "user": "U0ANA000001", "text": "hi"}],
    "has_more": False,
    "response_metadata": {"next_cursor": ""},
}


@pytest.mark.parametrize(
    "route",
    [
        pytest.param(HISTORY, id="happy-path"),
        pytest.param(SlackApiError("not_in_channel", method="conversations.history"), id="error-return"),
    ],
)
async def test_tool_returns_pass_through_unchanged(monkeypatch: pytest.MonkeyPatch, route: Any) -> None:
    fake = _FakeClient({"conversations.history": route})
    monkeypatch.setattr("slack_mcp.tools.conversations.get_client", lambda: fake)
    direct = await slack_get_history(GetHistoryInput(channel=CHANNEL))

    async with create_connected_server_and_client_session(mcp) as client:
        result = await client.call_tool("slack_get_history", {"params": {"channel": CHANNEL}})

    assert result.isError is False
    [block] = result.content
    assert isinstance(block, TextContent)
    assert block.text == direct
    assert fake.calls == [("conversations.history", {"channel": CHANNEL, "limit": 50})] * 2
    if isinstance(route, SlackApiError):
        assert direct.startswith("Error (not_in_channel): ")
    else:
        assert direct.startswith("# History of C0123ABCDEF")


# --- the subclass on its own (a throwaway server, not the real registry) --------------------


def _scratch_server() -> RedactingFastMCP:
    server = RedactingFastMCP("scratch")

    @server.tool(name="boom")
    async def boom() -> str:
        raise RuntimeError(f"tool body blew up holding {FAKE_USER}")

    @server.tool(name="elicit")
    async def elicit() -> str:
        raise McpError(ErrorData(code=-32042, message="elicitation required"))

    return server


async def test_an_exception_from_a_tool_body_is_redacted_too() -> None:
    with pytest.raises(ToolError) as info:
        await _scratch_server().call_tool("boom", {})

    assert str(info.value) == "Error executing tool boom: tool body blew up holding ***"
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__ is True


async def test_an_mcp_error_is_not_rewrapped() -> None:
    server = _scratch_server()
    with pytest.raises(ToolError) as plain:
        await FastMCP.call_tool(server, "elicit", {})
    # Today the SDK wraps a tool body's McpError in a ToolError itself; whatever it does,
    # the subclass must not change the exception type it lets through.
    with pytest.raises(type(plain.value)):
        await server.call_tool("elicit", {})


def test_the_registry_server_is_the_redacting_subclass() -> None:
    assert isinstance(mcp, RedactingFastMCP)


# --- log lines -------------------------------------------------------------------------------


def test_the_log_filter_redacts_args_and_tracebacks() -> None:
    try:
        raise ValueError(f"boom {FAKE_USER}")
    except ValueError:
        record = logging.LogRecord("mcp", logging.WARNING, __file__, 1, "Tool '%s' not listed", (FAKE_BOT,), None)
        record.exc_info = sys.exc_info()

    assert RedactingLogFilter().filter(record) is True

    message = record.getMessage()
    assert message.startswith("Tool '***' not listed\nTraceback"), message
    assert "ValueError: boom ***" in message
    assert "xox" not in message
    assert record.args is None
    assert record.exc_info is None


def test_install_log_redaction_is_idempotent() -> None:
    logger = logging.getLogger("slack_mcp.tests.scratch")
    handler = logging.NullHandler()
    logger.addHandler(handler)
    try:
        install_log_redaction(logger)
        install_log_redaction(logger)
        assert sum(isinstance(f, RedactingLogFilter) for f in handler.filters) == 1
    finally:
        logger.removeHandler(handler)


async def test_the_stdio_server_scrubs_responses_and_stderr() -> None:
    """A real `python -m slack_mcp` subprocess: what goes back on stdout and into stderr.

    A fresh process gets FastMCP's own stderr handler (pytest's handlers would mask it
    in-process — see test_logging.py), which is the one the filter must sit on.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("SLACK_")}
    env["COLUMNS"] = "400"  # rich wraps at the console width; keep each log line whole
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "slack_mcp",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        limit=16 * 1024 * 1024,  # the tools/list reply is one line well over asyncio's 64 KiB default
    )
    assert proc.stdin is not None and proc.stdout is not None

    async def send(message: dict[str, Any]) -> dict[str, Any] | None:
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write((json.dumps(message) + "\n").encode())
        await proc.stdin.drain()
        if "id" not in message:
            return None
        line = await asyncio.wait_for(proc.stdout.readline(), timeout=30)
        reply: dict[str, Any] = json.loads(line)
        assert reply["id"] == message["id"], reply
        return reply

    replies: list[dict[str, Any]] = []
    try:
        init = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}
        await send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": init})
        await send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        listed = await send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        for request_id, params in [
            (3, {"name": "slack_get_history", "arguments": {"params": {"channel": FAKE_BOT}}}),
            (4, {"name": FAKE_BOT, "arguments": {}}),
            (5, {"name": "slack_get_history", "arguments": FAKE_BOT}),  # malformed: not an object
        ]:
            reply = await send({"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": params})
            assert reply is not None
            replies.append(reply)
    finally:
        proc.stdin.close()
        _, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=30)
    stderr = stderr_bytes.decode()

    assert listed is not None
    assert len(listed["result"]["tools"]) == len(await mcp.list_tools())
    history, unknown, malformed = replies
    assert history["result"]["isError"] is True
    assert "got '***'" in history["result"]["content"][0]["text"]
    assert unknown["result"]["content"][0]["text"] == "Unknown tool: ***"
    assert malformed["error"]["message"] == "Invalid request parameters"
    assert FAKE_BOT not in json.dumps(replies)
    # The SDK logged both the unknown name and the malformed request — scrubbed.
    assert "Tool '***' not listed" in stderr, stderr
    assert "Failed to validate request" in stderr, stderr
    assert "xox" not in stderr, stderr
