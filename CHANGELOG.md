# Changelog

## [0.1.1](https://github.com/trustxai/slack-mcp/compare/v0.1.0...v0.1.1) (2026-10-10)


### Bug Fixes

* generic placeholders instead of real workspace and client identifiers ([59ca0d8](https://github.com/trustxai/slack-mcp/commit/59ca0d89906ccef1eb89049dff7b5432b79cfc7e))

## 0.1.0 (2026-10-10)


### Features

* **chat:** optional SLACK_SIGNATURE appended as the last line of every message (t14) ([864959c](https://github.com/trustxai/slack-mcp/commit/864959cfe4b34531849cdf580a3eb9066e1aed4a))
* **chat:** post, reply in thread, update and schedule messages as the user ([e160bfd](https://github.com/trustxai/slack-mcp/commit/e160bfd986d3c8bd7402801c97e89952e2961a48))
* **chat:** signature as a Block Kit context block under every signed message ([27adcfa](https://github.com/trustxai/slack-mcp/commit/27adcfac79790bd1c5d86e23f9c70a3fcdee410f))
* **conversations:** list, info, members, history, thread and open tools ([1a1a439](https://github.com/trustxai/slack-mcp/commit/1a1a439f8445d1573cc9256fbdb1c2708c9bc460))
* **reactions:** slack_add_reaction and slack_remove_reaction, as you, behind the write guard ([81d7b42](https://github.com/trustxai/slack-mcp/commit/81d7b42cfedbb6c142c8ef63553e1d27411cad5e))
* **search:** slack_search_messages, user-token only, page-based paging ([16a3955](https://github.com/trustxai/slack-mcp/commit/16a39556f182803f0722cbb00fb5c1c892e0be26))
* Stage 0 foundation (user-token spine, write guard, stub registry, oracle, CI, release tooling) ([9b05f85](https://github.com/trustxai/slack-mcp/commit/9b05f85d084a4f8281752bd302e16d2cbb32ce96))
* **users:** list, profile, lookup by email and a cached name resolver ([29bf57c](https://github.com/trustxai/slack-mcp/commit/29bf57cc464b41f8118759f5318327e5cfb9a86c))


### Bug Fixes

* **chat,formatters:** end-anchored signature detection; read block messages back from their blocks ([9bd49e1](https://github.com/trustxai/slack-mcp/commit/9bd49e1252cd1aa21b7757c7cb025fe2ac247fb4))
* **chat:** blank line before the signature; recommend a bot-user mention ([6ecb84e](https://github.com/trustxai/slack-mcp/commit/6ecb84e6fb34f5c2a795a792ef6b694dfe4f8616))
* **chat:** markdown signature after a blank line; live smoke checks it (t14) ([d7ab3ba](https://github.com/trustxai/slack-mcp/commit/d7ab3baa592610e490e93ccaa781e1bf278b4e0a))
* **chat:** recognise a stored or other-format signature; markdown keeps its blank line (t14) ([ca2757f](https://github.com/trustxai/slack-mcp/commit/ca2757ff538660b65ae4f17c0f7f9d3d54d3d9c0))
* **chat:** report a failed permalink lookup by cause only, never with write-outcome advice ([0fa80ed](https://github.com/trustxai/slack-mcp/commit/0fa80edf23de27159618ec8cd69603cbdd8953fa))
* **chat:** split long lines at a space, never inside an entity; review follow-ups ([1f143e6](https://github.com/trustxai/slack-mcp/commit/1f143e62ae632627342d4684661fbeb2a198cbd3))
* **conversations:** one-line topic/purpose, honest empty info, capped markdown pages ([e068db4](https://github.com/trustxai/slack-mcp/commit/e068db43c33eb72b3d99fc68fd0e2f7f321bb0f7))
* **formatters,chat:** review follow-ups — block order, whitespace-bounded lift, both signature forms, markdown read-back limit ([f6b220a](https://github.com/trustxai/slack-mcp/commit/f6b220a2d24480a8ba94806c622e32d7b21f22ec))
* **formatters,validators:** one-line authors, no negative epochs, the ms hint only for 13 digits ([125f68e](https://github.com/trustxai/slack-mcp/commit/125f68e6cc3b8d7f9e5196eae7692b93276aabe7))
* **formatters:** render_message indents continuation lines so authorship stays unambiguous ([031ae9f](https://github.com/trustxai/slack-mcp/commit/031ae9f468eddbb4ef44d4cd32be3866e0f099d1))
* **reactions,search:** review round — undocumented in:&lt;#C…&gt;, doubled lock, past-the-end page, cross-code tests ([705b66f](https://github.com/trustxai/slack-mcp/commit/705b66f254e6ae5812fb504f43d7e00413749905))
* **safety:** drop pydantic's truncated input_value tail, redact prompts/resources, mcp floor 1.21.1 ([d9bc7d8](https://github.com/trustxai/slack-mcp/commit/d9bc7d82592d49c604a0968c38f074bea9040457))
* **server:** redact SDK-raised tool-call errors and the SDK's own log lines ([daf943d](https://github.com/trustxai/slack-mcp/commit/daf943d3ed6b19e9bae993b9d81765015a9cca8d))
* **users:** a rate-limited re-walk never replaces a better cached directory ([efa6dca](https://github.com/trustxai/slack-mcp/commit/efa6dca3ac907014feb4e132c38585b5e745d761))
* **users:** resolver walk capped at 20 pages, partial cache on 429, lock and token-scoped key ([d849759](https://github.com/trustxai/slack-mcp/commit/d8497592064073993614b4f06965fa198588d39e))
* **validators:** ASCII digits only, fullmatch, and a millisecond epoch is never read as seconds ([bfbc65b](https://github.com/trustxai/slack-mcp/commit/bfbc65b92b33d5cbd5402549255bc1e41fa25f6c))


### Documentation

* **readme:** fold in the audit (no false redaction claim, resolver bounds, test tiers) ([42d6c60](https://github.com/trustxai/slack-mcp/commit/42d6c60e2c135f1e80e3b373a48648ca8a246055))
* **readme:** full README for v0.1 (safety model, app recipe, client configs, cheatsheet) ([5f30e6f](https://github.com/trustxai/slack-mcp/commit/5f30e6f4408c6b2a2733c6e0cd66af394d116347))
* **readme:** the token redaction covers the MCP SDK's own output (t12) ([26f494c](https://github.com/trustxai/slack-mcp/commit/26f494ca9b94417c15db131a93c309321b374ab8))

## Changelog
