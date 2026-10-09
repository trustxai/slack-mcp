# Security Policy

This server holds a Slack **user** token: everything it does in Slack, it does **as you**
— reading what you can read, and (when enabled) posting, editing and reacting under your
name. It runs over stdio only, so the token never leaves the machine that runs it. This
page covers how to report a vulnerability, what counts as one, and how to run the server
safely.

## The identity model

- The only accepted credential is the *User OAuth Token* (`xoxp-…`) of your own internal,
  non-distributed Slack app, installed in one workspace. Posts show your name and avatar
  plus Slack's "Sent using @<app>" line.
- Bot tokens (`xoxb-…`), app-level tokens (`xapp-…`), browser session tokens
  (`xoxc-…` / `xoxd-…`) and refresh tokens are refused before any request is sent.
- Writes are off unless `SLACK_ALLOW_WRITES=1`, and even then a write must target a
  conversation ID listed in `SLACK_WRITE_CHANNELS`, or a DM / group DM whose other
  members are all listed in `SLACK_WRITE_USERS`. These rails live in the HTTP client, not
  in the tools.
- People outside your organisation (Slack Connect): external people can only be written
  to via their own user id in `SLACK_WRITE_USERS` (a 1:1 DM); channels and group DMs
  shared with another organisation only by their explicit conversation id in
  `SLACK_WRITE_CHANNELS`. Any conversation id listed there is writable as-is, and a DM
  that Slack itself flags as shared is writable only that way. Slack does not reliably
  flag DMs with external people, which is why the member allowlist — not the flags — is
  what gates a DM.

## Reporting a vulnerability

**Do not open a public issue, pull request or discussion.** Report privately through
GitHub:

1. Open the repository's **Security** tab → **Report a vulnerability**
   (direct link: <https://github.com/trustxai/slack-mcp/security/advisories/new>).
2. Include:
   - the version you ran (the PyPI release, e.g. `0.1.0`, or the commit SHA);
   - the settings involved — variable **names** only, e.g. whether `SLACK_ALLOW_WRITES`
     was set and how many IDs the allowlists held;
   - the tool calls and steps to reproduce, and what an attacker gains.

**Never include real credentials** — no tokens, and no workspace, channel or user IDs you
would not publish. Build proofs of concept with mocked responses (the test suite uses
`httpx.MockTransport`). If a live token ends up in a report anyway, treat it as leaked
(see below).

### What to expect

The project has a single maintainer, so these are targets, not guarantees:

| Step | Target |
|---|---|
| Acknowledge the report | 3 business days |
| Confirm or rule out the issue, with a severity assessment | 10 business days |
| Release a fix or mitigation | 90 days from the report — sooner for anything that can write to Slack |

Once the fix is on PyPI, a GitHub Security Advisory is published (with a CVE when
warranted) and the CHANGELOG entry links to it. Reporters are credited in the advisory
unless they ask not to be. Please keep the details private until the advisory is out.

## Supported versions

Only the latest release on PyPI receives fixes.

## What counts as a vulnerability

- Any write (post, edit, schedule, react, open a DM) while `SLACK_ALLOW_WRITES` is off.
- A write to a target outside the allowlists — including by channel **name**, by a user
  id in place of a conversation id, or through a DM / group DM with someone not in
  `SLACK_WRITE_USERS`.
- A write reaching an external person (another organisation) whose own user id is not in
  `SLACK_WRITE_USERS`, unless the conversation's id is listed in `SLACK_WRITE_CHANNELS`.
- A write to a channel or group DM shared with another organisation whose conversation id
  is not listed in `SLACK_WRITE_CHANNELS`.
- A bot, app-level or browser session token being accepted.
- The token (or any `xox…` / `xapp-…` string) appearing in any tool output — successful
  or not — log line or error message. That includes what the MCP SDK says on the
  server's behalf: an argument rejected before the tool runs (its value is never echoed
  back as `input_value`, and a value a validator quotes is redacted), an unknown tool
  name, and the SDK's own lines on stderr.
- A write being retried automatically, or reported as done when Slack did not confirm it.

## Running it safely

- Keep `SLACK_ALLOW_WRITES` unset unless the client genuinely needs to write, and keep
  the allowlists to the conversations and people you actually mean.
- Keep the token in an environment file or a secret manager; never paste it into a chat
  or a command line.
- Leave token rotation and public distribution OFF in the app settings.

## If the token leaks

A user token acts as you until it is revoked, and re-installing the app on its own may
hand back the same token. So, in this order:

1. **Revoke it**: revoke the app's tokens on its **OAuth & Permissions** page at
   <https://api.slack.com/apps>, or remove the app from the workspace.
2. **Re-install the app** to get a new `xoxp-…`, and update `SLACK_USER_TOKEN` wherever
   it is configured (environment file, secret manager, MCP client configs).
3. Review your recent activity in Slack for anything you did not send.
