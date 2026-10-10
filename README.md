# Slack MCP Server (amazing-slack-mcp)

A [Model Context Protocol](https://modelcontextprotocol.io) server for the
[Slack Web API](https://docs.slack.dev/apis/web-api/), built on the official
[MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) (FastMCP). It
**speaks as you**: the only credential it accepts is the *User OAuth Token* (`xoxp-…`) of
your own internal Slack app, so everything it posts shows your name and avatar, and
everything it reads is what you can see in Slack. Slack does not label these messages:
they look exactly like ones you typed (measured with an internal app, in a self-DM and in
a channel). To let teammates tell them apart, set `SLACK_SIGNATURE` — the server shows
it under every message it posts, replies with, schedules or edits, in a Block Kit context
block (the small grey font of Slack's own labels). The recommended value is a bold label
and a mention of your app's bot user (`*Sent using* <@U0BOTID>`). See
[Signing your messages](#signing-your-messages).

You create **one personal, internal app per workspace** (see [Prerequisites](#prerequisites--create-the-slack-app)).
The server runs over **stdio only**. There is no HTTP transport, so a token that acts as
you never leaves the machine that runs the server. Bot tokens (`xoxb-…`), app-level
tokens (`xapp-…`), browser session tokens (`xoxc-…` / `xoxd-…`) and refresh tokens are
refused before anything is sent. Bots belong to a bot gateway (such as Hermes), not here.

> The PyPI distribution and the console script are both **`amazing-slack-mcp`**. The bare
> `slack-mcp` and `slack-mcp-server` names on PyPI belong to unrelated packages, so always
> run `uvx amazing-slack-mcp`, never `uvx slack-mcp`.

## Safety model

The write rails (kill-switch, allowlists, the closed method list, no write retry) live in
the HTTP client, not in the tools, so no tool can forget them:

- **Writes are off by default.** Posting, replying, editing, scheduling, reacting and
  opening DMs return `Error: … writes are disabled` until `SLACK_ALLOW_WRITES=1`. Reads
  need nothing.
- **Allowlists, by ID only.** With writes on, a write still needs its target in
  `SLACK_WRITE_CHANNELS` (conversation IDs), or, for a DM or group DM, every *other*
  member in `SLACK_WRITE_USERS` (user IDs). A write that names a `#channel-name` or a
  `U…` user id instead of a conversation ID is refused, because Slack would resolve it
  after the allowlists were checked. To message a person, open the DM with
  `slack_open_conversation` (every id must be in `SLACK_WRITE_USERS`) and post to the
  `D…` / `G…` id it returns.
- **Your self-DM needs no allowlist entry.** Once `SLACK_ALLOW_WRITES=1`, the DM with
  yourself passes without any allowlist entry, which makes it a good place to try writes
  out.
- **Slack Connect only by explicit listing.** A channel or group DM shared outside the
  workspace (any of `is_shared`, `is_ext_shared`, `is_org_shared`,
  `is_pending_ext_shared`) is writable only if you list its conversation id in
  `SLACK_WRITE_CHANNELS`. The same goes for a DM that Slack flags as shared. Any other
  DM or group DM with someone from another organisation needs that person's own user id
  in `SLACK_WRITE_USERS`. Slack does not reliably flag DMs with external people, so for
  DMs the member allowlist is the gate that holds: nobody gets a message unless you
  listed them, or the conversation, yourself.
- **Nothing marks a post as automated unless you sign it.** Slack shows these messages
  under your name with no app label, exactly like ones you typed. Set `SLACK_SIGNATURE`
  so every message the server posts carries a line, under the text in Slack's grey label
  font, that says it was sent by your app ([Signing your messages](#signing-your-messages)). `slack_health_check` says whether it
  is on.
- **A write is never retried.** A retried post is a duplicate message. A timeout or 5xx
  on a write is reported as outcome UNKNOWN: read the conversation back before trying
  again. (A read is retried once on HTTP 429, and only when Slack asks to wait 10 s or
  less.)
- **No delete tool.** There is no tool to delete a message or to cancel a scheduled one.
  The server never joins a channel on its own, and it can only call the Slack methods on
  its built-in list.
- **No token in any output.** Every result and error a tool returns is scrubbed of the
  configured token and of anything shaped like a Slack token (Slack echoes tokens in some
  error bodies). This includes what the MCP SDK itself produces on the server's behalf —
  an argument rejected before a tool runs (the rejected value is not echoed), an unknown
  tool name, and the SDK's own log lines on stderr. The HTTP library's
  one-line-per-request logs are silenced.
- **Text from other people is data, not instructions.** History, threads and search
  results contain messages anyone in the workspace could have written. Don't let your
  agent follow instructions found in them, and keep your MCP client's approval prompt on
  for the 🔒 tools: the guard is a backstop, not a replacement for that prompt.

The server stores nothing on disk. The user directory used by `slack_resolve_user` and
the guard's conversation lookups (reused for five minutes) are kept in memory only. Slack's
Developer Policy forbids using Slack data to train an LLM. This server only returns what
you ask for to your MCP client; what the client keeps is set by the client.

Found a way around one of these rails? Report it privately: see [SECURITY.md](SECURITY.md).

## Features

**18 tools** (17 Slack tools plus a health check). They are listed below by group, and
every one is in the [table](#available-tools).

- **Conversations.** `slack_list_conversations` turns a channel *name* into the
  conversation ID every other tool needs. It lists public and private channels, group
  DMs and DMs, and flags shared ones. `slack_get_conversation` shows one conversation,
  with an explicit `Slack Connect: yes/no` line. `slack_list_members` returns member ids.
  `slack_get_history` and `slack_get_thread` render messages oldest first as
  `[time] author: text (ts …)`, so a reply or a reaction can target the `ts`.
  A user token can read any public channel without joining it.
  `slack_open_conversation` 🔒 opens (or finds) a DM or a group DM with 1–8 people.
- **Users.** `slack_list_users`, `slack_get_user` and `slack_lookup_user_by_email`
  (exact match). `slack_resolve_user` is the name → id resolver. It reads up to 20 pages of the
  directory (about 4,000 people) on first use and answers from that per-process cache
  afterwards (`refresh=true` re-reads it), then ranks people by similarity to a name,
  display name or @handle, ignoring case and accents. It calls out a unique match together with the `<@U…>`
  string that mentions that person.
- **Chat.** `slack_post_message` 🔒, `slack_reply_in_thread` 🔒, `slack_update_message` 🔒
  (your own messages only) and `slack_schedule_message` 🔒 (in the future, at most 120
  days ahead). Text goes out verbatim (only leading and trailing whitespace is trimmed),
  and each tool has a `format` switch:
  `"mrkdwn"` (default, Slack's own syntax) or `"markdown"` (real Markdown, sent as
  `markdown_text`). After a post or a thread reply, the confirmation includes the
  message's permalink. If the permalink lookup fails, the message is still reported as
  sent. Every confirmation repeats only what Slack returned, including any warning Slack
  attached, plus a `signature` line: `SLACK_SIGNATURE`, when set, goes in a context block
  under every post, reply, scheduled message and edit, and ends the plain-text fallback
  exactly once.
- **Reactions.** `slack_add_reaction` 🔒 and `slack_remove_reaction` 🔒 (yours only).
  Colons around the emoji name are stripped. "Already there" and "not there" are
  reported as outcomes, not errors.
- **Search.** `slack_search_messages` passes Slack's own query syntax through verbatim
  (`in:#channel`, `from:@user`, `after:2026-10-01`, `is:thread`, …). Search works only
  with a **user token**, because a bot token cannot search at all. That is one reason
  this server speaks as you.
- **Health.** `slack_health_check` calls `auth.test` and shows who you are, the token
  kind, the granted scopes compared with the 15 the tools need, the write-guard state,
  and whether a signature is on.

Read tools other than `slack_health_check` take `response_format: "markdown"`
(default) or `"json"` (the raw Slack objects). Markdown output is capped at 50 rows per call. Cursor-paginated tools print
`next_cursor` verbatim and never walk more than one page per call. Search pages by
number. The one exception is `slack_resolve_user`, which reads up to 20 pages of the directory
into its cache on first use and says so when the index is partial.

## Available Tools

<!-- TOOL TABLE START -->
All **18 tools**, grouped by module. 🔒 = refused unless `SLACK_ALLOW_WRITES=1` and the target passes the allowlists (`SLACK_WRITE_CHANNELS` / `SLACK_WRITE_USERS`).

| Tool | Description |
|---|---|
| **Health** | |
| `slack_health_check` | Verify the token against Slack, show who you are, the granted scopes, and the write guard. |
| **Conversations — channels, DMs, history, threads** | |
| `slack_get_conversation` | Show one conversation's details, including whether it is shared outside the workspace. |
| `slack_get_history` | Read the messages of a channel, group DM or DM, rendered oldest first. |
| `slack_get_thread` | Read one thread: the parent message first, then its replies oldest first. |
| `slack_list_conversations` | List channels, private channels, group DMs and DMs, one page at a time. |
| `slack_list_members` | List the user ids of a conversation's members, one page at a time. |
| `slack_open_conversation` 🔒 | Open (or find) a DM or group DM with 1–8 people and return its conversation id. |
| **Users — lookup and name → ID resolution** | |
| `slack_get_user` | Show one person's profile by user id. |
| `slack_list_users` | List the people in the workspace, one cursor page at a time. |
| `slack_lookup_user_by_email` | Find the person behind an email address and show their profile. |
| `slack_resolve_user` | Turn a name, display name or @handle into Slack user ids, ranked by similarity. |
| **Chat — post, reply, edit, schedule** | |
| `slack_post_message` 🔒 | Post a message to a Slack channel, DM or group DM as you — Slack adds no label; SLACK_SIGNATURE signs it if set. |
| `slack_reply_in_thread` 🔒 | Reply inside a Slack thread as you — Slack adds no label; SLACK_SIGNATURE signs it if set. |
| `slack_schedule_message` 🔒 | Schedule a Slack message for later, posted as you — Slack adds no label; SLACK_SIGNATURE signs it if set. |
| `slack_update_message` 🔒 | Edit one of your own Slack messages as you — a SLACK_SIGNATURE line, if set, stays exactly once. |
| **Reactions** | |
| `slack_add_reaction` 🔒 | Add an emoji reaction to a message, as you. |
| `slack_remove_reaction` 🔒 | Remove one of YOUR emoji reactions from a message. |
| **Search** | |
| `slack_search_messages` | Search messages across every conversation you can see, with Slack's query syntax. |
<!-- TOOL TABLE END -->

The table is generated from the code (`scripts/gen_tool_table.py`). Each row is the first
line of that tool's docstring.

## Prerequisites — create the Slack app

You need Python 3.13+ (or just [uv](https://docs.astral.sh/uv/), which brings its own),
and one Slack app **per workspace**, created by you, for you:

1. Go to <https://api.slack.com/apps> → **Create New App** → **From scratch**. Give it any
   name (the author's is "Alejandro AI"). Slack does not show it on your messages; to name
   it there, put it in `SLACK_SIGNATURE` ([Signing your messages](#signing-your-messages)).
   Pick the workspace.
2. **OAuth & Permissions** → **Scopes** → **User Token Scopes** (*not* Bot Token Scopes)
   → add exactly these 15:

   ```text
   channels:read      channels:history   groups:read        groups:history
   im:read            im:history         im:write           mpim:read
   mpim:history       mpim:write         chat:write         reactions:write
   users:read         users:read.email   search:read
   ```

   | Scopes | Used by |
   |---|---|
   | `channels:read` `groups:read` `im:read` `mpim:read` | listing and inspecting public channels, private channels, DMs and group DMs (also the write guard's lookups) |
   | `channels:history` `groups:history` `im:history` `mpim:history` | `slack_get_history`, `slack_get_thread` |
   | `im:write` `mpim:write` | `slack_open_conversation` (DM / group DM) |
   | `chat:write` | post, reply, edit, schedule |
   | `reactions:write` | add / remove a reaction |
   | `users:read` `users:read.email` | the user tools (emails, and `slack_lookup_user_by_email`, need `users:read.email`) |
   | `search:read` | `slack_search_messages` |

   This is the list `slack_health_check` compares against. Add nothing else.
   `channels:write` and `reactions:read` are deliberately absent: no tool joins a channel
   or reads a message's reactions, and `channels:write` would also allow archiving,
   creating, renaming and kicking. Leave Bot Token Scopes empty, because this server
   never uses a bot token — the one exception is the single bot scope (`users:read`) of
   the bot user the recommended signature mentions, if you add it
   ([Signing your messages](#signing-your-messages)).
3. **Token rotation: leave it OFF.** Slack says rotation "may not be turned off once it's
   turned on", and a rotated token (`xoxe.xoxp-…`) expires every 12 hours. This server
   has no refresh flow: it accepts such a token, and the health check warns that it will
   expire.
4. **Manage Distribution: leave public distribution OFF.** Turning it on makes the app an
   *unlisted distributed* app. Under Slack's 2025 rate-limit change, new installs of
   those apps drop to 1 request a minute on history and threads, while internal apps keep
   their limits. An undistributed app lives in a single workspace, which is why a second workspace needs a second app.
5. **Install to Workspace** → allow → copy the **User OAuth Token** (`xoxp-…`). That
   value is your `SLACK_USER_TOKEN`. If the app has a bot user, the page also shows a
   *Bot User OAuth Token* (`xoxb-…`): never use that one — the server refuses it.

Scopes only ever get added to an issued token: Slack says "it is not possible to
downgrade an access token's scopes". To add a scope later, add it and **reinstall** the
app, then copy the token again. To drop a scope, re-create the app. Slack's docs also
say an app can be uninstalled automatically when the person who installed it leaves
the workspace or becomes a guest.

## Quickstart

1. Put the token where the server can read it: either a `.env` file in the directory you
   launch from, or real environment variables (they win over `.env`):

   ```sh
   # .env (never commit it)
   SLACK_USER_TOKEN=xoxp-...
   ```

2. Run it with no install:

   ```sh
   uvx amazing-slack-mcp
   ```

   It is a stdio server, so in a bare terminal it just waits for an MCP client on stdin.
   Stop it with Ctrl-C and point a client at it instead ([below](#client-configuration)).

3. Call **`slack_health_check`** first. It needs no arguments and shows the workspace,
   your user name and id, whether the token is a user token, which of the 15 scopes are
   missing, and whether writes are enabled. Without a client, use the
   [MCP Inspector](#mcp-inspector).

From a clone of the repo (`uv sync --group dev`), the same check runs in one line:

```sh
uv run python -c "import asyncio; from slack_mcp.tools.health import slack_health_check; print(asyncio.run(slack_health_check()))"
```

Writes stay off until you add `SLACK_ALLOW_WRITES=1` plus the allowlists
([Environment variables](#environment-variables)).

## Client configuration

Every client starts the server as a subprocess and passes settings through `env`.
**Two workspaces means two entries**: same package, a different `SLACK_USER_TOKEN` (one
app per workspace), and usually a different `SLACK_WRITE_USERS`. The examples below use
`slack-udocz` and `slack-masava`. Leave out `SLACK_ALLOW_WRITES` for an entry that
should only read.

Keep the token out of config files where you can. Two ways to do that:

- **Variable expansion.** Cursor (`${env:NAME}`) and a Claude Code project `.mcp.json`
  (`${NAME}`) expand variables from the environment the client was started in.
- **1Password `op run`.** Launch the server as `op run -- uvx amazing-slack-mcp` and set
  `SLACK_USER_TOKEN` to an `op://vault/item/field` reference. `op run` swaps in the
  secret only in the subprocess's environment. This works in any client that can launch a
  command, provided the `op` CLI is installed and signed in where the client runs (use
  absolute paths if the client has a minimal PATH), and it is the simplest way to give two
  entries two different tokens.

### Claude Code

```sh
claude mcp add-json slack-udocz '{
  "type": "stdio",
  "command": "op",
  "args": ["run", "--", "uvx", "amazing-slack-mcp"],
  "env": {
    "SLACK_USER_TOKEN": "op://Private/slack-udocz/credential",
    "SLACK_ALLOW_WRITES": "1",
    "SLACK_WRITE_USERS": "U0123ABCDEF"
  }
}' --scope user

claude mcp add-json slack-masava '{
  "type": "stdio",
  "command": "op",
  "args": ["run", "--", "uvx", "amazing-slack-mcp"],
  "env": {
    "SLACK_USER_TOKEN": "op://Private/slack-masava/credential",
    "SLACK_ALLOW_WRITES": "1",
    "SLACK_WRITE_USERS": "U0456GHIJKL"
  }
}' --scope user
```

Without 1Password, use `"command": "uvx", "args": ["amazing-slack-mcp"]`. In a project
`.mcp.json`, write the token as `"SLACK_USER_TOKEN": "${SLACK_UDOCZ_TOKEN}"` and export
`SLACK_UDOCZ_TOKEN` in the shell that starts Claude Code. A literal `xoxp-…` value in
`env` also works, but it then sits in plain text in the config file.

### Cursor

`~/.cursor/mcp.json` (global) or `.cursor/mcp.json` (per project):

```json
{
  "mcpServers": {
    "slack-udocz": {
      "command": "uvx",
      "args": ["amazing-slack-mcp"],
      "env": {
        "SLACK_USER_TOKEN": "${env:SLACK_UDOCZ_TOKEN}",
        "SLACK_ALLOW_WRITES": "1",
        "SLACK_WRITE_USERS": "U0123ABCDEF"
      }
    },
    "slack-masava": {
      "command": "uvx",
      "args": ["amazing-slack-mcp"],
      "env": {
        "SLACK_USER_TOKEN": "${env:SLACK_MASAVA_TOKEN}",
        "SLACK_WRITE_USERS": "U0456GHIJKL"
      }
    }
  }
}
```

(`slack-masava` here is read-only: no `SLACK_ALLOW_WRITES`.)

### Codex

`~/.codex/config.toml`. With one workspace, `env_vars` forwards `SLACK_USER_TOKEN` from
the environment Codex runs in, so the token never goes into the file:

```toml
[mcp_servers.slack-udocz]
command = "uvx"
args = ["amazing-slack-mcp"]
env_vars = ["SLACK_USER_TOKEN"]
env = { SLACK_ALLOW_WRITES = "1", SLACK_WRITE_USERS = "U0123ABCDEF" }
```

`env_vars` forwards a variable under its own name, so two entries would both receive the
same `SLACK_USER_TOKEN`. For a second workspace, give each entry its own reference through
`op run`:

```toml
[mcp_servers.slack-masava]
command = "op"
args = ["run", "--", "uvx", "amazing-slack-mcp"]
env = { SLACK_USER_TOKEN = "op://Private/slack-masava/credential", SLACK_WRITE_USERS = "U0456GHIJKL" }
```

### Claude Desktop

Settings → Developer → Edit Config opens `claude_desktop_config.json` (on macOS:
`~/Library/Application Support/Claude/claude_desktop_config.json`). Add the same
`mcpServers` block as in the Cursor example. Desktop's config file is not documented to
expand environment variables, so use a literal token or the `op run` form. Quit Desktop
*before* you edit the file: a running Desktop has been seen to write its in-memory copy
back over edits. Restart Desktop afterwards. If Desktop cannot find `uvx` or `op`, use
their absolute paths (`which uvx`, `which op`).

Mind the tool budget. Claude Desktop's cloud (Cowork) sessions publish your local MCP
tools when they start, and a session has been seen to fail to start once the total tool
count across all servers grew large. Each entry of this server adds 18 tools, so with
many servers configured, consider leaving it out of Desktop and using it from Claude
Code, Cursor or Codex.

### MCP Inspector

From a directory with your `.env`:

```sh
npx @modelcontextprotocol/inspector uvx amazing-slack-mcp
```

Open the Tools tab and run `slack_health_check`. From a clone,
`npx @modelcontextprotocol/inspector uv run amazing-slack-mcp` does the same against your
working tree.

## Environment variables

Settings are read once per server process, on first use, so restart the client after
changing one. String values are whitespace-stripped. Real environment variables win over
`.env`, which is read from the server's working directory.

| Variable | Default | Meaning |
|---|---|---|
| `SLACK_USER_TOKEN` | *(empty)* | **Required.** Your app's User OAuth Token (`xoxp-…`), sent as `Authorization: Bearer …`. Any other token is refused (known kinds such as `xoxb-`, `xapp-`, `xoxc-` by name), and the value is never echoed. |
| `SLACK_ALLOW_WRITES` | off | Kill-switch. `1` allows posting, replying, editing, scheduling, reacting and opening DMs, and even then every write must pass the allowlists. |
| `SLACK_WRITE_CHANNELS` | *(empty)* | Comma-separated conversation IDs (`C…`, `G…`, `D…`) a write may target. IDs only; a `#name` never matches. The only way to write to a Slack Connect channel or a shared group DM. |
| `SLACK_WRITE_USERS` | *(empty)* | Comma-separated user IDs (`U…`, `W…`). A DM or group DM is writable when every *other* member is listed, and `slack_open_conversation` only opens conversations with these people. Your own self-DM needs no entry. |
| `SLACK_SIGNATURE` | *(empty = off)* | A line the server shows under every message it posts, replies with, schedules or edits, in a Block Kit context block (Slack's small grey label font), and appends to the plain-text fallback after a blank line (`"\n\n"` + signature) — never twice. Slack does not label these messages, so this is how teammates can tell them apart. Recommended: `*Sent using* <@U0BOTID>`, a bold label and a mention of your app's bot user. Slack mrkdwn, one line (a multi-line value is joined into one), at most 3,000 characters; it counts toward the length limits. See [Signing your messages](#signing-your-messages). |
| `SLACK_API_URL` | `https://slack.com/api` | Web API base URL, for proxies and tests. Must be `https://`; plain `http://` is accepted only for `127.0.0.1` / `localhost`. |
| `SLACK_REQUEST_TIMEOUT_SECONDS` | `30` | Per-request timeout. A timed-out write is reported as outcome UNKNOWN, never retried. |
| `SLACK_TEST_ALLOW_WRITES` | *(unset)* | **Test suite only**, never read by the server. `1` opens the `live_write` tests, but only when it is in the environment pytest starts with (a value in `.env` is ignored for this variable) and only with exactly `-m live_write`. See [Running the tests](#running-the-tests). |

## Signing your messages

Slack does not label these messages: they look exactly like ones you typed. Measured on
2026-10-10 with an internal app and a user token, in a self-DM and in a channel:
no app label under the message. To let teammates tell them apart, set `SLACK_SIGNATURE`.
Every message the server posts, replies with, schedules or edits then carries it in a
Block Kit **context block** under the text, which Slack renders in the small grey font of
its own labels.

The recommended value is a bold label, like Slack's native one, and a real Slack mention
of your app's **bot user**:

```sh
# .env — the bot user's id (U…), found as in step 3 below
SLACK_SIGNATURE=*Sent using* <@U0BOTID>
```

Slack renders the mention as the blue `@Alejandro AI` tag, and clicking it opens the
app's profile inside Slack — a link would open the web browser instead. The app gets a
bot user only so it can be mentioned. Its bot token is never used, and the server refuses
it anyway (`xoxb-…`). To add one:

1. <https://api.slack.com/apps> → your app → **App Manifest** → add these keys, keeping
   the user scopes as they are:

   ```yaml
   features:
     bot_user:
       display_name: Alejandro AI
       always_online: false
   oauth_config:
     scopes:
       bot:
         - users:read
   ```

   One bot scope, `users:read`, because Slack does not install a bot user without one.
   It is a bot scope: it does not go on your user token, whose 15 scopes stay as they are.
2. **Save**, then **Install App** → **Reinstall to Workspace**. `SLACK_USER_TOKEN` stays
   the **User OAuth Token** (`xoxp-…`; copy it again if it changed), never the new *Bot
   User OAuth Token* (`xoxb-…`).
3. Find the bot user's id with `slack_list_users` and `include_bots: true` (bots are
   hidden by default), or with `slack_resolve_user`, `query: "Alejandro AI"` and
   `include_bots: true`. It is a `U…` id: that is what goes in `<@…>`.

Without a bot user, the fallback is a link to your app's page. It renders the
`@Alejandro AI` part in link colour, like a tag, but opens the web:

```sh
# .env — your workspace's subdomain, and the App ID from your app's Basic Information page
SLACK_SIGNATURE=*Sent using* <https://<workspace>.slack.com/marketplace/<APP_ID>|@Alejandro AI>
```

For example, the author's app in the Masava workspace is
`*Sent using* <https://masavaco.slack.com/marketplace/A0C835DFNSH|@Alejandro AI>`.

In a shell, quote the value (`*`, `<`, `>` and `|` are shell metacharacters); in a
client's JSON `env` block it is an ordinary string. Either line only looks like Slack's
label: it is message content, a marker and not provenance — anyone can type the same
line, and any app can post the same block. How it behaves:

- With a signature set, `slack_post_message`, `slack_reply_in_thread`,
  `slack_schedule_message` and `slack_update_message` send Block Kit `blocks` plus a
  `text` fallback, in both formats. Without one, nothing changes: plain `text` (mrkdwn)
  or `markdown_text` (Markdown), no blocks. The blocks are:
  - `format: "mrkdwn"`: the text in one or more `section` blocks, cut at line boundaries
    into pieces of at most 3,000 characters (Slack's limit for a section's text). A single
    longer line is cut at a space, never inside a `<@U…>` mention or `<url|label>` link,
    and hard at 3,000 only when there is no such point;
  - `format: "markdown"`: the text in one `markdown` block (at most 12,000 characters),
    and no `markdown_text`, which Slack does not accept next to `blocks`;
  - then a `context` block with the signature as mrkdwn — so write it in mrkdwn
    (`*bold*`, `<@U…>`, `<url|label>`).

  With the recommended value, what goes to Slack for `Deploy done :rocket:` is:

  ```json
  {
    "text": "Deploy done :rocket:\n\n*Sent using* <@U0BOTID>",
    "blocks": [
      {"type": "section", "text": {"type": "mrkdwn", "text": "Deploy done :rocket:"}},
      {"type": "context", "elements": [{"type": "mrkdwn", "text": "*Sent using* <@U0BOTID>"}]}
    ]
  }
  ```

- The `text` fallback is what Slack shows in notifications and search: the text, a
  **blank line** (`"\n\n"`), then the signature. For `format: "markdown"` its
  `<url|label>` links are written as `[label](url)` there; a `<@U…>` mention stays as it
  is. The signature never goes inside a body block.
- Slack flattens the `text` fallback of a message posted with blocks when it stores it:
  every newline becomes a space (measured 2026-10-10), so it reads back as one line. The
  read tools (`slack_get_thread`, `slack_get_history`) rebuild the text from the blocks,
  so for `format: "mrkdwn"` what you read back is what was posted: the text with its
  line breaks, then the signature as its last line. This applies to every app-built
  message, not only the server's: text another app put only in its `text` fallback is
  not shown in markdown mode (JSON mode returns the raw messages, `text` and blocks
  included). A message typed by a person (only `rich_text` blocks) still reads back
  from `text`.

  One v0.1 limit: Slack stores a `markdown` block as `rich_text` (measured 2026-10-10),
  so a message posted with `format: "markdown"` reads back from its flattened `text`, as
  one line with only the signature on its own last line. Editing it from that read-back
  loses its line structure (headings, lists and paragraphs run together). Pass the
  original text when you edit such a message.
- `slack_update_message` resends the blocks with every edit, replacing the old ones (an
  edit that sends only `text` would drop them). The signature is appended unless the new
  text already ends with it, so an edit of a signed message stays signed exactly once:
  it counts whatever separates it from the text — blank lines, one newline, the spaces
  Slack's flattened fallback leaves of them, or nothing — in either form (`<url|label>`
  or `[label](url)`, so switching `format` on an edit is safe) and as Slack stores it
  (`&` read back as `&amp;`), so editing the text read back from Slack does not add a
  second one — the signature is cut from the body and goes back in the context block. A
  signature quoted in the middle of a text does not count; the text is signed again at
  the end. Slack does not show its "edited" mark on an edit made with blocks.
- It counts toward Slack's length limits, blank line included: the caps apply to the
  final `text`. A text that only goes over because of the signature is refused before
  anything is sent, and the error says so. So is a signature longer than 3,000
  characters (the limit for a context block's text) and a text that would need more than
  49 sections (Slack allows 50 blocks per message, the context block included).
- Every confirmation has a `signature` line (`appended (context block)`,
  `already at the end … (context block)`, or `not configured`), and
  `slack_health_check` shows `on (N chars)` or `off`.

## Formatting cheatsheet

With the default `format: "mrkdwn"`, the text is Slack's own syntax, which is **not**
Markdown:

| You want | Write |
|---|---|
| Mention a person | `<@U0123ABCDEF>`, which takes the user **id**, never a name (get it from `slack_resolve_user`). Slack notifies them only if they are a member of that conversation. |
| Link a channel | `<#C0123ABCDEF>`. Slack renders the channel's current name. Text read back may carry it as `<#C0123ABCDEF\|general>`. |
| A link | `<https://example.com\|label>`, or a bare URL |
| Bold / italic / strike | `*bold*` (single asterisks; `**x**` is not bold), `_italic_`, `~strike~` |
| Code | `` `inline` `` and ```` ```block``` ```` |
| Quote | `> quoted line` |
| Notify everyone | `<!channel>` notifies every member and `<!here>` only the active ones; both go out as you. |
| Headings, lists | mrkdwn has no headings and no list syntax. Write `•` or `-` lines as plain text, or use `format: "markdown"`. |
| A literal `&`, `<` or `>` | escape only those three: `&amp;`, `&lt;`, `&gt;` |

Set `format: "markdown"` to write ordinary Markdown instead (`**bold**`, `# heading`,
`[label](url)`, lists). The same string is sent as Slack's `markdown_text`, which holds
at most **12,000 characters**. Mentions still need the `<@U…>` form. The other limits:
`slack_post_message`, `slack_reply_in_thread` and `slack_schedule_message` accept up to
40,000 characters of mrkdwn, and `slack_update_message` up to 4,000. A `SLACK_SIGNATURE`
counts toward each of these limits, and with one set the text goes out in Block Kit
blocks (mrkdwn in sections of 3,000 characters, Markdown in one `markdown` block — see
[Signing your messages](#signing-your-messages)).

## Running the tests

```sh
uv sync --group dev
uv run pytest -m "not live and not live_write"          # the gate — never touches Slack
uv run pytest -m live                                   # read-only smoke; needs SLACK_USER_TOKEN (shell or .env)
SLACK_TEST_ALLOW_WRITES=1 uv run pytest -m live_write   # the self-DM smoke (tests/test_live_write.py)
```

- The default tier mocks every HTTP call and strips your real `SLACK_*` settings, so it
  runs anywhere.
- `live` runs only the read-only smoke tests against your workspace, and only when
  `SLACK_USER_TOKEN` is set. Otherwise those tests are skipped.
- `live_write` is the self-DM smoke (`tests/test_live_write.py`). It writes to Slack as
  you, in your own self-DM only, never a channel or another person. It runs **only** when
  all three hold: the token is set, `SLACK_TEST_ALLOW_WRITES=1` is in the environment
  pytest starts with (set it inline as shown; a value in `.env` is ignored on purpose),
  and the marker expression is exactly `-m live_write`. Nothing can delete what it posts,
  so the messages stay in your self-DM.

## Troubleshooting

- **`missing_scope`.** The error names the scope Slack wanted (and the ones the token
  has, when Slack says). Add it under **User Token Scopes**, reinstall the app, put the
  token in `SLACK_USER_TOKEN` again and restart the client. `slack_health_check` lists
  every missing scope at once.
- **`not_in_channel`.** You can read a public channel without joining it, but you cannot
  post there. Join it in Slack yourself: the server never auto-joins, and it does not
  request `channels:write`.
- **`invalid_auth` / `token_revoked` after a reinstall.** The token was revoked or
  replaced. Copy the **User OAuth Token** from OAuth & Permissions again, update it
  wherever it is configured, and restart the client.
- **"SLACK_USER_TOKEN is a bot token (xoxb-…), which this server refuses".** You pasted
  the *Bot* User OAuth Token. This server speaks as you and accepts only a user token, so
  copy the **User OAuth Token** (`xoxp-…`) instead. If you need a bot, that belongs to a
  bot gateway, not here. App-level (`xapp-…`) and browser (`xoxc-…` / `xoxd-…`) tokens
  get the same treatment.
- **A `#name` is rejected, or `channel_not_found`.** Tools take conversation IDs only, and
  a `#name` is refused before anything is sent. Find the ID with
  `slack_list_conversations`. `channel_not_found` means the ID is wrong or it is a
  private conversation you are not in.
- **Writes refused.** The error names the variable that would allow it:
  `SLACK_ALLOW_WRITES=1`, the conversation id in `SLACK_WRITE_CHANNELS`, or the people in
  `SLACK_WRITE_USERS`. Restart the client after changing them. A shared (Slack Connect)
  conversation always needs its id in `SLACK_WRITE_CHANNELS`.
- **`msg_too_long` on an edit.** `chat.update` refuses more than 4,000 characters, so
  `slack_update_message` caps the text there. Shorten it, or post the rest as a reply.
  With `SLACK_SIGNATURE` set, the signature counts too, and the error says how many
  characters it adds.
- **Teammates can't tell which messages the agent sent.** Slack adds no app label to a
  message posted with a user token. Set `SLACK_SIGNATURE`
  ([Signing your messages](#signing-your-messages)) and restart the client;
  `slack_health_check` then shows `signature: on`.
- **The health check says the token is rotating (`xoxe.xoxp-…`).** Token rotation was
  turned on, and Slack does not let you turn it off. The token expires every 12 hours, so
  create a new app with rotation off.
- **`uvx slack-mcp` installs something else.** That name (and `slack-mcp-server`)
  belongs to unrelated PyPI packages. This one is `uvx amazing-slack-mcp`.

## Contributing

```sh
uv sync --group dev
uv run pytest -m "not live and not live_write"
uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/
uv run mypy src/
uv run python scripts/gen_tool_table.py --check   # --write after adding or renaming a tool
```

Commits follow [Conventional Commits](https://www.conventionalcommits.org) (`feat(chat): …`,
`fix(guard): …`). Releases and the changelog are cut by release-please. A new Slack method
is a deliberate change to the client's method list (`src/slack_mcp/client.py`), and a
new write method has to be added to `WRITE_METHODS` so the guard sees it. Security
issues go through [SECURITY.md](SECURITY.md), never a public issue.

## License

Apache-2.0 — see [LICENSE](LICENSE).
