# Slack MCP Server (amazing-slack-mcp)

A [Model Context Protocol](https://modelcontextprotocol.io) server for the
[Slack Web API](https://docs.slack.dev/apis/web-api/), built on the official
[MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) (FastMCP). It
**speaks as you**: the only credential it accepts is the *User OAuth Token* (`xoxp-…`) of
your own internal Slack app, so everything it posts shows your name and avatar — plus
Slack's own "Sent using @Alejandro AI" line (your app's name) under the message — and
everything it reads is what you can see in Slack.

It runs over **stdio only**. There is no HTTP transport: a token that acts as you never
leaves the machine that runs the server. Bot tokens (`xoxb-…`), app-level tokens
(`xapp-…`) and browser session tokens (`xoxc-…` / `xoxd-…`) are refused before anything
is sent.

> The PyPI distribution and console script are both **`amazing-slack-mcp`** (the bare
> `slack-mcp` and `slack-mcp-server` names on PyPI belong to unrelated packages).
> Always run `uvx amazing-slack-mcp`.

## Safety model

All rails are enforced in the HTTP client — not in the tools — so no tool can forget them:

- **Writes are off by default.** Posting, editing, scheduling, reacting and opening DMs
  return `Error: … writes are disabled` until `SLACK_ALLOW_WRITES=1`. Reads need nothing.
- **Allowlists, by ID.** With writes on, a write still needs its target in
  `SLACK_WRITE_CHANNELS` (conversation IDs) — or, for a DM / group DM, every other member
  in `SLACK_WRITE_USERS` (user IDs). Channel names are never matched.
- **People outside your organisation only by explicit listing.** External people can only
  be written to via their own user id in `SLACK_WRITE_USERS` (a 1:1 DM); channels and
  group DMs shared with another organisation only by their explicit conversation id in
  `SLACK_WRITE_CHANNELS`. (Any conversation id listed there is writable as-is; a DM that
  Slack itself flags as shared is writable only that way.)
- **Nothing is retried after a write.** A timeout or 5xx on a post is reported as
  outcome UNKNOWN; read the conversation back before trying again.
- **No delete tool**, and the token never appears in any output, log or error.

Found a way around one of these rails? Report it privately — see [SECURITY.md](SECURITY.md).

## Available Tools

<!-- TOOL TABLE START -->
All **1 tool**, grouped by module. 🔒 = refused unless `SLACK_ALLOW_WRITES=1` and the target passes the allowlists (`SLACK_WRITE_CHANNELS` / `SLACK_WRITE_USERS`).

| Tool | Description |
|---|---|
| **Health** | |
| `slack_health_check` | Verify the token against Slack, show who you are, the granted scopes, and the write guard. |
<!-- TOOL TABLE END -->

## Features

TODO(t5)

## Prerequisites

TODO(t5) — the Slack app recipe ("Alejandro AI", the 15 User Token Scopes listed in
[.env.example](.env.example) and nothing more, rotation OFF, distribution OFF, one app per
workspace).

## Quickstart

TODO(t5)

## Run with uvx (zero install)

TODO(t5)

## Client Configuration

TODO(t5) — Claude Code, Cursor, Codex, Claude Desktop, MCP Inspector; two workspaces =
two entries.

## Environment Variables

TODO(t5) — see [.env.example](.env.example) meanwhile.

## Formatting cheatsheet

TODO(t5)

## Troubleshooting

TODO(t5)

## Contributing

TODO(t5). The test tiers, meanwhile:

```sh
uv run pytest -m "not live and not live_write"          # the gate — never touches Slack
uv run pytest -m live                                   # read-only smoke; SLACK_USER_TOKEN in .env
SLACK_TEST_ALLOW_WRITES=1 uv run pytest -m live_write   # posts in YOUR self-DM only
```

`SLACK_TEST_ALLOW_WRITES=1` is honoured only when exported inline on the command line,
never from `.env`, and only together with `-m live_write`.

## License

Apache-2.0 — see [LICENSE](LICENSE).
