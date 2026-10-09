"""FastMCP server definition and entry point for slack_mcp (stdio only)."""

from __future__ import annotations

import logging

from slack_mcp.safety import RedactingFastMCP, install_log_redaction

# RedactingFastMCP: every tool-call error the SDK raises — including argument-validation
# failures that happen BEFORE a tool body runs — is redacted and stripped of the raw
# input echo (see safety.py). Tools still register with the plain `@mcp.tool(...)`.
mcp = RedactingFastMCP("slack_mcp")

# FastMCP configures root logging at INFO to stderr, which MCP clients persist to log
# files. At INFO, httpx logs one "HTTP Request: ..." line per call — the URL names the
# method the account just called, so quiet it down to WARNING. httpcore only logs at
# DEBUG today; it is pinned too.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
# The SDK's own log lines can quote what a client sent (an unknown tool name, a malformed
# request); scrub them on the stderr handler FastMCP just installed.
install_log_redaction()

# Register all tools (side-effect imports via decorators)
from slack_mcp.tools import register_all  # noqa: E402

register_all(mcp)


def main_stdio() -> None:
    """Entry point for local / Docker stdio transport (`amazing-slack-mcp`).

    There is deliberately no HTTP entry point: the token acts as the user, so the server
    never listens on a socket and the token never leaves the machine that runs it.
    """
    mcp.run()


if __name__ == "__main__":
    main_stdio()
