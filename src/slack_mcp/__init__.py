"""MCP server for the Slack Web API that speaks as YOU (user token; stdio only; writes gated by allowlists)."""

from importlib.metadata import PackageNotFoundError, version

try:
    # Read from the installed distribution, so it never drifts from release-please's bump.
    __version__ = version("amazing-slack-mcp")
except PackageNotFoundError:  # running from a source tree that is not installed
    __version__ = "0.0.0"
