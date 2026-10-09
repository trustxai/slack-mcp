"""Credential redaction for everything the MCP SDK says on the server's behalf.

The tools already scrub their own output (`handle_api_error`, `clip_response`), but the
SDK speaks before and around them:

- **Argument validation.** FastMCP validates a tool's arguments against its pydantic
  model BEFORE the tool body runs, and the resulting `ToolError` carries pydantic's
  rendering of the failure: `input_value='<whatever the client sent>'`, plus whatever the
  field validator echoed (`got '<value>'`). A token-shaped argument came back verbatim.
- **Unknown names.** `Unknown tool: <name>`, `Unknown prompt: <name>` and
  `Unknown resource: <uri>` echo what the client asked for.
- **Log lines.** The low-level server logs `Tool '<name>' not listed …`, and the session
  logs `Failed to validate request: <pydantic error>` for a malformed request — both to
  stderr, which MCP clients persist to log files.

`RedactingFastMCP.call_tool` is the one funnel every `tools/call` goes through
(FastMCP registers the bound `self.call_tool` as the protocol handler), so overriding it
covers every tool without touching a signature; `get_prompt` and `read_resource` are the
same funnels for `prompts/get` and `resources/read`. A pydantic `ValidationError` is
re-rendered WITHOUT `input_value` / `input_type` / the docs URL — location, message and
error type only, so a malformed request is still readable — and every message is passed
through `errors.redact_credentials`. Raising `ToolError` again keeps the SDK's
`isError: true` result. `install_log_redaction` adds a `logging.Filter` to the root
handlers that FastMCP configures, so the log lines above are scrubbed too.
"""

from __future__ import annotations

import logging
import re
import traceback
from typing import TYPE_CHECKING, Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.shared.exceptions import McpError
from pydantic import ValidationError

from slack_mcp.errors import redact_credentials

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from mcp.server.lowlevel.helper_types import ReadResourceContents
    from mcp.types import ContentBlock, GetPromptResult
    from pydantic import AnyUrl

# One validation message is capped at this many characters (after redaction): the field
# validators echo the offending value (`got '…'`), and a client can send a huge one.
MAX_ERROR_MESSAGE_CHARS = 500

# pydantic prints a rejected value as `input_value=<repr>` and cuts a long repr down to
# its first 25 and last 24 characters (`'xoxp-1234…' … '...' … 'WXYZ'`). The token
# pattern masks the head — it starts like a token — but not the tail, so the whole value
# goes. First pydantic's own `…, input_type=<type>]` line ending (greedy, so a value that
# contains ", input_type=" itself is still swallowed whole), then any other spelling up to
# the next `, input_type=`, a closing `]` at the end of the line, or the end of the line.
_INPUT_VALUE_LINE_RE = re.compile(r"input_value=.*, input_type=(\w+)\]$", re.MULTILINE)
_INPUT_VALUE_ANY_RE = re.compile(r"input_value=(?!<redacted>).*?(, input_type=|\]$|$)", re.MULTILINE)
REDACTED_INPUT = "input_value=<redacted>"


def scrub(text: str) -> str:
    """`redact_credentials`, then drop every pydantic `input_value=` echo."""
    text = redact_credentials(text)
    text = _INPUT_VALUE_LINE_RE.sub(rf"{REDACTED_INPUT}, input_type=\1]", text)
    return _INPUT_VALUE_ANY_RE.sub(rf"{REDACTED_INPUT}\1", text)


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
            lines.append(_clip(scrub(location)))
        lines.append(f"  {_clip(scrub(err['msg']))} [type={err['type']}]")
    return scrub("\n".join(lines))


def _same_type_scrubbed(exc: Exception) -> Exception:
    """`exc`'s type with a scrubbed message, so the SDK maps it exactly as before."""
    message = scrub(str(exc))
    try:
        return type(exc)(message)
    except Exception:  # a constructor that wants more than a message
        return RuntimeError(message)


class RedactingFastMCP(FastMCP):
    """FastMCP whose tool, prompt and resource errors never carry a credential or a raw echo."""

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Sequence[ContentBlock] | dict[str, Any]:
        try:
            return await super().call_tool(name, arguments)
        except McpError:
            # A JSON-RPC error the SDK must deliver as-is (URL elicitation, which the SDK's
            # Tool.run re-raises unwrapped). Never built from the arguments.
            raise
        except Exception as exc:  # ToolError in practice; anything else would be rendered via str() too
            cause = exc.__cause__
            if isinstance(cause, ValidationError):
                message = describe_validation_error(name, cause)
            else:
                message = scrub(str(exc))
            # `from None`: the unredacted original must not ride along in a traceback.
            raise ToolError(message) from None

    async def get_prompt(self, name: str, arguments: dict[str, Any] | None = None) -> GetPromptResult:
        try:
            return await super().get_prompt(name, arguments)
        except McpError:
            raise
        except Exception as exc:  # `Unknown prompt: <name>` — a ValueError
            raise _same_type_scrubbed(exc) from None

    async def read_resource(self, uri: AnyUrl | str) -> Iterable[ReadResourceContents]:
        try:
            return await super().read_resource(uri)
        except McpError:
            raise
        except Exception as exc:  # `Unknown resource: <uri>` — a ValueError / ResourceError
            raise _same_type_scrubbed(exc) from None


class RedactingLogFilter(logging.Filter):
    """Scrub credentials from a log record's rendered message (and traceback, if any).

    A filter that raises takes the logging call down with it, so nothing here may raise.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # e.g. mismatched `%` args: logging would only report it later
            message = f"{record.msg} {record.args!r}"
        if record.exc_info or record.stack_info:
            # Fold the traceback into the message so it is scrubbed like the rest; the
            # handler then has nothing left to render from the live exception.
            try:
                extra = "".join(traceback.format_exception(*record.exc_info)) if record.exc_info else ""
            except Exception:
                extra = "<traceback unavailable>"
            if record.stack_info:
                extra += record.stack_info
            message = f"{message}\n{extra.rstrip()}"
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        # The rendered message replaces msg + args, so `%s` arguments are covered too.
        record.msg = scrub(message)
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
