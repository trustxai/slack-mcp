"""Credential redaction for everything the MCP SDK says on the server's behalf.

The tools already scrub their own output (`handle_api_error`, `clip_response`), but the
SDK speaks before and around them:

- **Argument validation.** FastMCP validates a tool's arguments against its pydantic
  model BEFORE the tool body runs, and the resulting `ToolError` carries pydantic's
  rendering of the failure: `input_value='<whatever the client sent>'`, plus whatever the
  field validator echoed (`got '<value>'`). A token-shaped argument came back verbatim.
- **Unknown tools.** `Unknown tool: <name>` echoes the requested name.
- **Log lines.** The low-level server logs `Tool '<name>' not listed …` and the session
  logs `Failed to validate request: <pydantic error>` for a malformed request, both to
  stderr, which MCP clients persist to log files.

`RedactingFastMCP.call_tool` is the one funnel every `tools/call` goes through
(FastMCP registers the bound `self.call_tool` as the protocol handler), so overriding it
covers every tool without touching a signature. A pydantic `ValidationError` is
re-rendered WITHOUT `input_value` / `input_type` / the docs URL — location, message and
error type only, so a malformed request is still readable — and every message is passed
through `errors.redact_credentials`. Raising `ToolError` again keeps the SDK's
`isError: true` result. `install_log_redaction` adds a `logging.Filter` to the root
handlers that FastMCP configures, so the log lines above are scrubbed too.
"""

from __future__ import annotations

import logging
import traceback
from typing import TYPE_CHECKING, Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.shared.exceptions import McpError
from pydantic import ValidationError

from slack_mcp.errors import redact_credentials

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mcp.types import ContentBlock

# One validation message is capped at this many characters (after redaction): the field
# validators echo the offending value (`got '…'`), and a client can send a huge one.
MAX_ERROR_MESSAGE_CHARS = 500


def _clip(text: str, limit: int = MAX_ERROR_MESSAGE_CHARS) -> str:
    return text if len(text) <= limit else f"{text[:limit]}… [truncated]"


def describe_validation_error(tool: str, exc: ValidationError) -> str:
    """Pydantic's layout for an argument-validation failure, minus the raw input.

    `Error executing tool <tool>: N validation error(s) for <Model>` then, per error, the
    location line and the message with its error type. Each piece is redacted BEFORE it
    is clipped, so a cut can never leave half a token behind.
    """
    errors = exc.errors(include_url=False, include_input=False, include_context=False)
    count = len(errors)
    lines = [f"Error executing tool {tool}: {count} validation error{'' if count == 1 else 's'} for {exc.title}"]
    for err in errors:
        location = ".".join(str(part) for part in err["loc"])
        if location:
            lines.append(_clip(redact_credentials(location)))
        lines.append(f"  {_clip(redact_credentials(err['msg']))} [type={err['type']}]")
    return redact_credentials("\n".join(lines))


class RedactingFastMCP(FastMCP):
    """FastMCP whose tool-call errors never carry a credential or a raw argument echo."""

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Sequence[ContentBlock] | dict[str, Any]:
        try:
            return await super().call_tool(name, arguments)
        except McpError:
            # A JSON-RPC error the SDK must deliver as-is (e.g. URL elicitation). Never
            # built from the arguments, and the tools here never raise one.
            raise
        except Exception as exc:  # ToolError in practice; anything else would be rendered via str() too
            cause = exc.__cause__
            if isinstance(cause, ValidationError):
                message = describe_validation_error(name, cause)
            else:
                message = redact_credentials(str(exc))
            # `from None`: the unredacted original must not ride along in a traceback.
            raise ToolError(message) from None


class RedactingLogFilter(logging.Filter):
    """Scrub credentials from a log record's rendered message (and traceback, if any)."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if record.exc_info or record.stack_info:
            # Fold the traceback into the message so it is redacted like the rest; the
            # handler then has nothing left to render from the live exception.
            extra = "".join(traceback.format_exception(*record.exc_info)) if record.exc_info else ""
            if record.stack_info:
                extra += record.stack_info
            message = f"{message}\n{extra.rstrip()}"
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        # The rendered message replaces msg + args, so `%s` arguments are covered too.
        record.msg = redact_credentials(message)
        record.args = None
        return True


def install_log_redaction(logger: logging.Logger | None = None) -> None:
    """Attach one `RedactingLogFilter` to every handler of `logger` (default: root).

    Call it after FastMCP's constructor, which is what installs the stderr handler.
    Idempotent: a handler that already has the filter is left alone.
    """
    target = logger if logger is not None else logging.getLogger()
    for handler in target.handlers:
        if not any(isinstance(existing, RedactingLogFilter) for existing in handler.filters):
            handler.addFilter(RedactingLogFilter())
