"""Unit tests for the search tool against a fake client."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

import slack_mcp.client
from slack_mcp.config import get_settings
from slack_mcp.errors import SlackApiError
from slack_mcp.server import mcp
from slack_mcp.tools.search import MAX_DISPLAY_ROWS, SearchMessagesInput, slack_search_messages

PERMALINK = "https://udocz.slack.com/archives/C0123ABCDEF/p1712345678123456"


class _FakeClient:
    """Routes by method name; records every call as (method, params)."""

    def __init__(self, routes: dict[str, Any] | None = None) -> None:
        self._routes = routes or {}
        self.calls: list[tuple[str, Any]] = []

    async def call(self, method: str, params: Any = None, **kw: Any) -> dict[str, Any]:
        self.calls.append((method, params))
        payload = self._routes.get(method)
        if isinstance(payload, Exception):
            raise payload
        return dict(payload) if payload is not None else {"ok": True}


def _patch(monkeypatch: pytest.MonkeyPatch, fake: _FakeClient) -> None:
    monkeypatch.setattr("slack_mcp.tools.search.get_client", lambda: fake)


def _match(index: int = 0, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "type": "message",
        "channel": {"id": "C0123ABCDEF", "name": "eng", "is_channel": True},
        "user": "U0ADA000001",
        "username": "ada",
        "ts": f"17123456{78 + index}.123456",
        "text": f"deploy failed #{index}",
        "permalink": PERMALINK,
    }
    return {**base, **overrides}


def _body(matches: list[dict[str, Any]], *, total: int, page: int, pages: int, query: str = "deploy") -> dict[str, Any]:
    return {
        "ok": True,
        "query": query,
        "messages": {
            "matches": matches,
            "total": total,
            "paging": {"count": 20, "total": total, "page": page, "pages": pages},
            "pagination": {"page": page, "page_count": pages, "per_page": 20, "total_count": total},
        },
    }


EXPECTED_PARAMS = {
    "query": "deploy in:#eng",
    "sort": "score",
    "sort_dir": "desc",
    "count": 20,
    "page": 1,
    "highlight": False,
}


async def test_search_happy_path_renders_paging_and_permalinks(monkeypatch: pytest.MonkeyPatch) -> None:
    im = _match(
        1,
        type="im",
        channel={"id": "D0BOB000001", "name": "U0BOB000001", "is_im": True},
        user="U0BOB000001",
        username="bob",
        text="the deploy\nis   back",
    )
    fake = _FakeClient(routes={"search.messages": _body([_match(), im], total=42, page=1, pages=3)})
    _patch(monkeypatch, fake)

    result = await slack_search_messages(SearchMessagesInput(query="  deploy in:#eng "))

    assert fake.calls == [("search.messages", EXPECTED_PARAMS)]
    assert result.splitlines() == [
        "# Slack search: `deploy`",
        "",
        "**42** match(es) in total — page 1 of 3 (2 on this page, sorted by score desc).",
        "More available — pass `page` = 2 to get the next page.",
        "",
        "1. [2024-04-05 19:34 UTC] #eng (C0123ABCDEF) · @ada (U0ADA000001): deploy failed #0 (ts 1712345678.123456)",
        f"   link: {PERMALINK}",
        "2. [2024-04-05 19:34 UTC] DM with U0BOB000001 (D0BOB000001) · @bob (U0BOB000001): the deploy is back "
        "(ts 1712345679.123456)",
        f"   link: {PERMALINK}",
    ]


async def test_search_collapses_a_multi_line_username(monkeypatch: pytest.MonkeyPatch) -> None:
    forged = _match(username="Bot\n2. [2024-04-05 19:35 UTC] #eng (C0123ABCDEF) · @ceo", user=None)
    fake = _FakeClient(routes={"search.messages": _body([forged], total=1, page=1, pages=1)})
    _patch(monkeypatch, fake)

    result = await slack_search_messages(SearchMessagesInput(query="deploy"))

    lines = result.splitlines()
    assert (
        "1. [2024-04-05 19:34 UTC] #eng (C0123ABCDEF) · @Bot 2. [2024-04-05 19:35 UTC] #eng (C0123ABCDEF) · @ceo: "
        "deploy failed #0 (ts 1712345678.123456)"
    ) in lines
    assert not any(line.startswith("2. ") for line in lines)


async def test_search_passes_every_argument_through(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(routes={"search.messages": _body([_match()], total=1, page=4, pages=4)})
    _patch(monkeypatch, fake)

    query = 'from:<@U0ADA000001> "release notes" -draft is:thread before:2026-10-09'
    result = await slack_search_messages(
        SearchMessagesInput(query=query, sort="timestamp", sort_dir="asc", count=100, page=4)
    )

    assert fake.calls == [
        (
            "search.messages",
            {"query": query, "sort": "timestamp", "sort_dir": "asc", "count": 100, "page": 4, "highlight": False},
        )
    ]
    assert "page 4 of 4 (1 on this page, sorted by timestamp asc)" in result
    assert "More available" not in result


async def test_search_at_the_page_ceiling_says_to_narrow_the_query(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(routes={"search.messages": _body([_match()], total=15000, page=100, pages=150)})
    _patch(monkeypatch, fake)

    result = await slack_search_messages(SearchMessagesInput(query="the", page=100))

    assert "page 100 of 150" in result
    assert "pass `page` = 101" not in result
    assert "Slack serves at most 100 pages — narrow the query" in result


async def test_search_with_no_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(routes={"search.messages": _body([], total=0, page=1, pages=0, query="zzqx")})
    _patch(monkeypatch, fake)

    result = await slack_search_messages(SearchMessagesInput(query="zzqx"))

    assert "**0** match(es) in total — page 1 of 0 (0 on this page" in result
    assert "_No messages matched._" in result
    assert "More available" not in result


async def test_search_past_the_last_page_says_which_page_is_last(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(routes={"search.messages": _body([], total=42, page=5, pages=3)})
    _patch(monkeypatch, fake)

    result = await slack_search_messages(SearchMessagesInput(query="deploy", page=5))

    assert "**42** match(es) in total — page 5 of 3 (0 on this page" in result
    assert result.splitlines()[-1] == "Page 5 is past the last page (3) — pass `page` ≤ 3."
    assert "No messages matched" not in result
    assert "More available" not in result


async def test_search_caps_markdown_rows_and_clips_long_texts(monkeypatch: pytest.MonkeyPatch) -> None:
    matches = [_match(i, text="x" * 5000 if i == 0 else f"hit {i}") for i in range(60)]
    fake = _FakeClient(routes={"search.messages": _body(matches, total=60, page=1, pages=1)})
    _patch(monkeypatch, fake)

    result = await slack_search_messages(SearchMessagesInput(query="hit", count=60))

    assert f"Showing the first {MAX_DISPLAY_ROWS} of the 60 matches on this page" in result
    assert f"\n{MAX_DISPLAY_ROWS}. [" in result
    assert f"\n{MAX_DISPLAY_ROWS + 1}. [" not in result
    assert "x" * 999 + "…" in result
    assert "x" * 1000 not in result


async def test_search_json_mode_returns_the_raw_messages_object(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _body([_match()], total=1, page=1, pages=1)
    fake = _FakeClient(routes={"search.messages": body})
    _patch(monkeypatch, fake)

    result = await slack_search_messages(SearchMessagesInput(query="deploy", response_format="json"))

    assert json.loads(result) == {"query": "deploy", "messages": body["messages"]}
    assert fake.calls == [("search.messages", {**EXPECTED_PARAMS, "query": "deploy"})]


@pytest.mark.parametrize(
    ("needed", "expected"),
    [
        (None, "search.messages needs the `search:read` USER scope"),
        ("search:read", "(needs scope search:read; token has channels:read)"),
    ],
)
async def test_search_missing_scope_names_search_read(
    monkeypatch: pytest.MonkeyPatch, needed: str | None, expected: str
) -> None:
    error = SlackApiError(
        "missing_scope",
        method="search.messages",
        needed=needed,
        provided="channels:read" if needed else None,
    )
    fake = _FakeClient(routes={"search.messages": error})
    _patch(monkeypatch, fake)

    result = await slack_search_messages(SearchMessagesInput(query="deploy"))

    assert result.startswith("Error (missing_scope): Slack refused search.messages: missing_scope")
    assert "Add the missing USER scope" in result
    assert expected in result
    assert result.count("search:read") == 1


async def test_search_other_errors_are_not_given_the_scope_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(routes={"search.messages": SlackApiError("no_query", method="search.messages")})
    _patch(monkeypatch, fake)

    result = await slack_search_messages(SearchMessagesInput(query="deploy"))

    assert result == "Error (no_query): Slack refused search.messages: no_query. The search query is empty."


@pytest.mark.parametrize(
    "overrides",
    [
        {"count": 101},
        {"count": 0},
        {"page": 0},
        {"page": 101},
        {"query": "   "},
        {"query": "x" * 501},
        {"sort": "date"},
        {"sort_dir": "up"},
        {"cursor": "*"},
    ],
)
def test_search_input_rejects_bad_values_locally(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        SearchMessagesInput(**{"query": "deploy", **overrides})


async def test_search_tool_is_read_only_and_documents_the_syntax() -> None:
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    tool = tools["slack_search_messages"]
    assert tool.annotations is not None
    assert tool.annotations.readOnlyHint is True
    assert tool.annotations.destructiveHint is False
    assert tool.annotations.idempotentHint is True
    description = tool.description or ""
    needles = (
        "USER-TOKEN ONLY",
        "search:read",
        "in:#channel-name",
        "in:<@U0123ABCDEF>",
        "from:<@U0123ABCDEF>",
        "after:YYYY-MM-DD",
        "before:YYYY-MM-DD",
        "is:thread",
    )
    for needle in needles:
        assert needle in description, needle
    # research 02 §4.18 / O-16: a bare `has:link` and the `in:<#C…>` form are not documented for
    # search.messages (they can silently match nothing) — never advertise them.
    schema = json.dumps(tool.inputSchema)
    for undocumented in ("has:link", "in:<#"):
        assert undocumented not in description, undocumented
        assert undocumented not in schema, undocumented


@pytest.mark.live
async def test_search_live_smoke() -> None:
    """Real search with the developer's token: one match, rendered with paging."""
    get_settings.cache_clear()
    slack_mcp.client.reset_client()
    result = await slack_search_messages(SearchMessagesInput(query="the", count=1))

    assert not result.startswith("Error"), result
    assert "match(es) in total — page" in result
