"""Unit tests for the users tools against a fake client (plus read-only live smokes)."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from pydantic import ValidationError

from slack_mcp.client import get_client
from slack_mcp.config import Settings
from slack_mcp.errors import SlackApiError
from slack_mcp.formatters import ResponseFormat
from slack_mcp.tools import users
from slack_mcp.tools.users import (
    MAX_DISPLAY_ROWS,
    GetUserInput,
    ListUsersInput,
    LookupUserByEmailInput,
    ResolveUserInput,
    slack_get_user,
    slack_list_users,
    slack_lookup_user_by_email,
    slack_resolve_user,
)

Route = dict[str, Any] | Exception | Callable[[Any], Any]


class _FakeClient:
    """Routes by method name; records every call as (method, params)."""

    def __init__(self, routes: dict[str, Route] | None = None) -> None:
        self._routes = routes or {}
        self.calls: list[tuple[str, Any]] = []

    async def call(self, method: str, params: Any = None, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((method, copy.deepcopy(params)))
        route = self._routes.get(method)
        if route is None:
            raise AssertionError(f"unexpected Slack method {method!r}")
        payload = route(params) if callable(route) else route
        if isinstance(payload, Exception):
            raise payload
        assert isinstance(payload, dict)
        return copy.deepcopy(payload)


def _by_cursor(pages: dict[str | None, Any]) -> Callable[[Any], Any]:
    """A users.list route that answers per `cursor` (None = the first page)."""

    def route(params: Any) -> Any:
        return pages[(params or {}).get("cursor")]

    return route


def _patch(monkeypatch: pytest.MonkeyPatch, fake: _FakeClient) -> None:
    monkeypatch.setattr("slack_mcp.tools.users.get_client", lambda: fake)


@pytest.fixture(autouse=True)
def _fresh_directory_cache() -> Iterator[None]:
    users.clear_user_cache()
    yield
    users.clear_user_cache()


def _user(
    uid: str,
    name: str,
    real_name: str,
    *,
    display_name: str = "",
    email: str | None = None,
    title: str = "",
    **flags: Any,
) -> dict[str, Any]:
    profile: dict[str, Any] = {"real_name": real_name, "display_name": display_name, "title": title}
    if email is not None:
        profile["email"] = email
    return {
        "id": uid,
        "name": name,
        "real_name": real_name,
        "tz": "America/Lima",
        "tz_label": "Peru Standard Time",
        "deleted": False,
        "is_bot": False,
        "profile": profile,
        **flags,
    }


ADA = _user("U0ADA000001", "ada", "Ada Lovelace", display_name="ada", email="ada@example.com", title="Analyst")
RICARDO = _user("U0RIC000001", "ricardo.perez", "Ricardo Pérez", display_name="Ricardo", email="ricardo@example.com")
RICARDO_G = _user("U0RIC000002", "rgomez", "Ricardo Gómez", display_name="rgomez")
GRACE = _user("U0GRA000001", "grace", "Grace Ramírez", display_name="grace")
OLD_RICARDO = _user("U0OLD000001", "ricardo.viejo", "Ricardo Viejo", deleted=True)
BOT = _user("U0BOT000001", "ricardobot", "Ricardo Bot", is_bot=True)
SLACKBOT = _user("USLACKBOT", "slackbot", "Slackbot")


def _page(members: list[dict[str, Any]], next_cursor: str = "") -> dict[str, Any]:
    return {"ok": True, "members": members, "response_metadata": {"next_cursor": next_cursor}}


# --------------------------------------------------------------------------------------
# slack_list_users
# --------------------------------------------------------------------------------------


async def test_list_users_hides_deleted_and_bots_and_says_how_many(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient({"users.list": _page([ADA, OLD_RICARDO, BOT, SLACKBOT, RICARDO])})
    _patch(monkeypatch, fake)

    result = await slack_list_users(ListUsersInput())

    assert fake.calls == [("users.list", {"limit": 50})]
    assert result.startswith("# Slack users")
    assert "Showing **2** item(s) (limit 50)." in result
    assert "Hidden on this page: 1 deactivated, 2 bots — pass `include_deleted=true` / `include_bots=true`" in result
    assert "| User | Title | Time zone | Email | Flags |" in result
    assert "| Ada Lovelace (@ada, U0ADA000001) | Analyst | America/Lima | ada@example.com |  |" in result
    assert "Ricardo Pérez (@Ricardo, U0RIC000001)" in result
    assert "U0OLD000001" not in result
    assert "U0BOT000001" not in result
    assert "USLACKBOT" not in result
    # An empty next_cursor is the last page.
    assert "More available" not in result


async def test_list_users_passes_the_cursor_and_prints_the_next_one(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient({"users.list": _page([ADA], next_cursor="dXNlcjpVMDYxTkZUVDI=")})
    _patch(monkeypatch, fake)

    result = await slack_list_users(ListUsersInput(limit=1, cursor="dXNlcjpVMEc5V0ZYTlo="))

    assert fake.calls == [("users.list", {"limit": 1, "cursor": "dXNlcjpVMEc5V0ZYTlo="})]
    assert "More available — pass `cursor` = `dXNlcjpVMDYxTkZUVDI=` to get the next page." in result
    assert "Hidden" not in result


async def test_list_users_without_response_metadata_is_the_last_page(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient({"users.list": {"ok": True, "members": [ADA]}})
    _patch(monkeypatch, fake)

    result = await slack_list_users(ListUsersInput(cursor="   "))

    # A blank cursor is dropped by the tool, not sent.
    assert fake.calls == [("users.list", {"limit": 50})]
    assert "More available" not in result


async def test_list_users_can_include_deleted_and_bots_with_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    admin = _user("U0ADM000001", "boss", "The Boss", is_admin=True, is_restricted=True)
    fake = _FakeClient({"users.list": _page([OLD_RICARDO, BOT, SLACKBOT, admin])})
    _patch(monkeypatch, fake)

    result = await slack_list_users(ListUsersInput(include_deleted=True, include_bots=True))

    assert "Showing **4** item(s)" in result
    assert "Hidden" not in result
    assert "| deactivated |" in result
    assert "Ricardo Bot (@ricardobot, U0BOT000001) |  | America/Lima |  | bot |" in result
    assert "Slackbot (@slackbot, USLACKBOT) |  | America/Lima |  | bot |" in result
    assert "| admin, guest |" in result


async def test_list_users_json_returns_the_raw_objects(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient({"users.list": _page([ADA, BOT], next_cursor="next==")})
    _patch(monkeypatch, fake)

    result = await slack_list_users(ListUsersInput(limit=2, response_format=ResponseFormat.JSON))

    data = json.loads(result)
    assert data["count"] == 1
    assert data["has_more"] is True
    assert data["next_cursor"] == "next=="
    assert data["items"] == [ADA]
    assert data["note"].startswith("Hidden on this page: 1 bot —")


async def test_list_users_caps_the_rendered_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    people = [_user(f"U0P{index:08d}", f"p{index}", f"Person {index}") for index in range(60)]
    fake = _FakeClient({"users.list": _page(people)})
    _patch(monkeypatch, fake)

    result = await slack_list_users(ListUsersInput(limit=200))

    assert fake.calls == [("users.list", {"limit": 200})]
    assert f"Showing **{MAX_DISPLAY_ROWS}** item(s) (limit 200)." in result
    assert "10 more on this page were not displayed" in result
    assert "U0P00000049" in result
    assert "U0P00000050" not in result


def test_list_users_rejects_an_out_of_range_limit_locally() -> None:
    with pytest.raises(ValidationError, match="less than or equal to 200"):
        ListUsersInput(limit=201)
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ListUsersInput.model_validate({"limit": 5, "team_id": "T0123"})


async def test_list_users_missing_scope_names_users_read(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient({"users.list": SlackApiError("missing_scope", method="users.list")})
    _patch(monkeypatch, fake)

    result = await slack_list_users(ListUsersInput())

    assert result.startswith("Error (missing_scope): Slack refused users.list: missing_scope.")
    assert "User Token Scopes" in result
    assert result.endswith("users.list needs the USER scope `users:read`.")


# --------------------------------------------------------------------------------------
# slack_get_user
# --------------------------------------------------------------------------------------


async def test_get_user_renders_the_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    user = {**ADA, "is_admin": True, "profile": {**ADA["profile"], "status_emoji": ":coffee:", "status_text": "AFK"}}
    fake = _FakeClient({"users.info": {"ok": True, "user": user}})
    _patch(monkeypatch, fake)

    result = await slack_get_user(GetUserInput(user=" U0ADA000001 "))

    assert fake.calls == [("users.info", {"user": "U0ADA000001"})]
    assert result.startswith("# Ada Lovelace (@ada, U0ADA000001)")
    assert "- **real name**: Ada Lovelace" in result
    assert "- **display name**: ada" in result
    assert "- **handle**: @ada" in result
    assert "- **title**: Analyst" in result
    assert "- **email**: ada@example.com" in result
    assert "- **time zone**: America/Lima (Peru Standard Time)" in result
    assert "- **status**: :coffee: AFK" in result
    assert "- **is_admin**: yes" in result
    assert "- **is_bot**: no" in result
    assert "- **deleted**: no" in result
    assert "write `<@U0ADA000001>` in a message to mention them" in result


async def test_get_user_without_email_names_the_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    stranger = _user("W0EXT000001", "ext", "External Person", is_stranger=True, deleted=True)
    fake = _FakeClient({"users.info": {"ok": True, "user": stranger}})
    _patch(monkeypatch, fake)

    result = await slack_get_user(GetUserInput(user="W0EXT000001"))

    assert fake.calls == [("users.info", {"user": "W0EXT000001"})]
    assert "- **email**: not visible (needs the `users:read.email` scope" in result
    assert "- **display name**: N/A (not set)" in result
    assert "- **deleted**: yes" in result
    assert "- **external**: yes — a Slack Connect user" in result
    assert "- **mention**: none — the account is deactivated" in result


async def test_get_user_json_returns_the_raw_user(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient({"users.info": {"ok": True, "user": ADA}})
    _patch(monkeypatch, fake)

    result = await slack_get_user(GetUserInput(user="U0ADA000001", response_format=ResponseFormat.JSON))

    assert json.loads(result) == ADA


@pytest.mark.parametrize("bad", ["ada", "@ada", "C0123ABCDEF", "u0ada000001"])
def test_get_user_rejects_a_non_user_id_locally(bad: str) -> None:
    with pytest.raises(ValidationError, match="must be a Slack user ID like U0123ABCDEF"):
        GetUserInput(user=bad)


async def test_get_user_not_found_renders_the_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient({"users.info": SlackApiError("user_not_found", method="users.info")})
    _patch(monkeypatch, fake)

    result = await slack_get_user(GetUserInput(user="U0NOPE00001"))

    assert result == (
        "Error (user_not_found): Slack refused users.info: user_not_found. Unknown user id. Resolve the person with "
        "slack_resolve_user or slack_lookup_user_by_email."
    )


# --------------------------------------------------------------------------------------
# slack_lookup_user_by_email
# --------------------------------------------------------------------------------------


async def test_lookup_by_email_renders_the_card(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient({"users.lookupByEmail": {"ok": True, "user": ADA}})
    _patch(monkeypatch, fake)

    result = await slack_lookup_user_by_email(LookupUserByEmailInput(email=" ada@example.com "))

    assert fake.calls == [("users.lookupByEmail", {"email": "ada@example.com"})]
    assert result.startswith("# Slack user for ada@example.com\n\n**Ada Lovelace (@ada, U0ADA000001)**")
    assert "- **email**: ada@example.com" in result
    assert "`<@U0ADA000001>`" in result


async def test_lookup_by_email_missing_scope_names_users_read_email(monkeypatch: pytest.MonkeyPatch) -> None:
    # O-14: Slack may omit `needed`, so the tool names the scope itself.
    fake = _FakeClient({"users.lookupByEmail": SlackApiError("missing_scope", method="users.lookupByEmail")})
    _patch(monkeypatch, fake)

    result = await slack_lookup_user_by_email(LookupUserByEmailInput(email="ada@example.com"))

    assert result.startswith("Error (missing_scope): Slack refused users.lookupByEmail: missing_scope.")
    assert result.endswith("users.lookupByEmail needs the USER scope `users:read.email`.")


async def test_lookup_by_email_missing_scope_with_needed_is_not_repeated(monkeypatch: pytest.MonkeyPatch) -> None:
    exc = SlackApiError("missing_scope", method="users.lookupByEmail", needed="users:read.email", provided="users:read")
    fake = _FakeClient({"users.lookupByEmail": exc})
    _patch(monkeypatch, fake)

    result = await slack_lookup_user_by_email(LookupUserByEmailInput(email="ada@example.com"))

    assert "(needs scope users:read.email; token has users:read)" in result
    assert result.count("users:read.email") == 1


async def test_lookup_by_email_not_found_renders_the_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient({"users.lookupByEmail": SlackApiError("users_not_found", method="users.lookupByEmail")})
    _patch(monkeypatch, fake)

    result = await slack_lookup_user_by_email(LookupUserByEmailInput(email="nobody@example.com"))

    assert result.startswith("Error (users_not_found): Slack refused users.lookupByEmail: users_not_found.")
    assert "deactivated" in result
    assert fake.calls == [("users.lookupByEmail", {"email": "nobody@example.com"})]


@pytest.mark.parametrize("bad", ["ada", "ada@", "@example.com", "ada @example.com", "<mailto:ada@example.com|ada>"])
def test_lookup_by_email_rejects_a_non_address_locally(bad: str) -> None:
    with pytest.raises(ValidationError):
        LookupUserByEmailInput(email=bad)


# --------------------------------------------------------------------------------------
# slack_resolve_user
# --------------------------------------------------------------------------------------

DIRECTORY_PAGES: dict[str | None, Any] = {
    None: _page([ADA, RICARDO, OLD_RICARDO], next_cursor="cGFnZTI="),
    "cGFnZTI=": _page([GRACE, BOT, SLACKBOT], next_cursor=""),
}


async def test_resolve_walks_every_page_once_and_caches_across_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient({"users.list": _by_cursor(DIRECTORY_PAGES)})
    _patch(monkeypatch, fake)

    first = await slack_resolve_user(ResolveUserInput(query="Ricardo"))
    second = await slack_resolve_user(ResolveUserInput(query="Grace"))

    assert fake.calls == [
        ("users.list", {"limit": 200}),
        ("users.list", {"limit": 200, "cursor": "cGFnZTI="}),
    ]
    assert "**Unique match**: Ricardo Pérez (@Ricardo, U0RIC000001) — mention as `<@U0RIC000001>`." in first
    assert "**Unique match**: Grace Ramírez (@grace, U0GRA000001) — mention as `<@U0GRA000001>`." in second


async def test_resolve_excludes_deleted_and_bots_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient({"users.list": _by_cursor(DIRECTORY_PAGES)})
    _patch(monkeypatch, fake)

    result = await slack_resolve_user(ResolveUserInput(query="ricardo"))

    assert result.startswith('# Slack users matching "ricardo"')
    assert "Searched 3 people. Hidden from the search: 1 deactivated, 2 bots" in result
    assert "| 1.00 | Ricardo Pérez (@Ricardo, U0RIC000001) |  | ricardo@example.com |" in result
    assert "U0OLD000001" not in result
    assert "U0BOT000001" not in result


async def test_resolve_with_include_deleted_and_bots_turns_a_unique_match_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeClient({"users.list": _by_cursor(DIRECTORY_PAGES)})
    _patch(monkeypatch, fake)

    result = await slack_resolve_user(ResolveUserInput(query="ricardo", include_deleted=True, include_bots=True))

    assert "Searched 6 people." in result
    assert "Hidden" not in result
    assert "**Ambiguous**: 3 people score ≥ 0.90" in result
    assert "Unique match" not in result
    for uid in ("U0RIC000001", "U0OLD000001", "U0BOT000001"):
        assert uid in result


async def test_resolve_a_tie_renders_both_and_calls_it_ambiguous(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient({"users.list": _page([RICARDO, RICARDO_G, ADA])})
    _patch(monkeypatch, fake)

    result = await slack_resolve_user(ResolveUserInput(query="Ricardo"))

    assert fake.calls == [("users.list", {"limit": 200})]
    assert "**Ambiguous**: 2 people score ≥ 0.90 — confirm which one before mentioning or messaging anyone." in result
    assert "Unique match" not in result
    assert "<@" not in result
    # Same score: ordered by name, so the table is stable.
    gomez = result.index("| 1.00 | Ricardo Gómez (@rgomez, U0RIC000002)")
    perez = result.index("| 1.00 | Ricardo Pérez (@Ricardo, U0RIC000001)")
    assert gomez < perez
    assert "Ada Lovelace" not in result


async def test_resolve_ignores_case_accents_and_the_at_sign(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient({"users.list": _page([GRACE, ADA])})
    _patch(monkeypatch, fake)

    by_accentless = await slack_resolve_user(ResolveUserInput(query="GRACE RAMIREZ"))
    by_handle = await slack_resolve_user(ResolveUserInput(query="@ada"))

    assert "**Unique match**: Grace Ramírez (@grace, U0GRA000001)" in by_accentless
    assert "**Unique match**: Ada Lovelace (@ada, U0ADA000001)" in by_handle
    assert len(fake.calls) == 1


async def test_resolve_by_exact_id_or_email_scores_one(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient({"users.list": _page([ADA, RICARDO])})
    _patch(monkeypatch, fake)

    by_id = await slack_resolve_user(ResolveUserInput(query="U0RIC000001"))
    by_email = await slack_resolve_user(ResolveUserInput(query="ada@example.com"))

    assert "| 1.00 | Ricardo Pérez (@Ricardo, U0RIC000001)" in by_id
    assert "**Unique match**: Ada Lovelace (@ada, U0ADA000001)" in by_email


async def test_resolve_a_one_letter_typo_still_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient({"users.list": _page([ADA, RICARDO])})
    _patch(monkeypatch, fake)

    result = await slack_resolve_user(ResolveUserInput(query="lovlace"))

    # SequenceMatcher("lovlace", "lovelace").ratio() == 14/15
    assert "| 0.93 | Ada Lovelace (@ada, U0ADA000001) | Analyst | ada@example.com |" in result
    assert "**Unique match**: Ada Lovelace (@ada, U0ADA000001)" in result
    assert "Ricardo" not in result


async def test_resolve_a_partial_name_is_listed_without_a_unique_call_out(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient({"users.list": _page([ADA, RICARDO])})
    _patch(monkeypatch, fake)

    result = await slack_resolve_user(ResolveUserInput(query="lovel"))

    # SequenceMatcher("lovel", "lovelace").ratio() == 10/13
    assert "| 0.77 | Ada Lovelace (@ada, U0ADA000001) |" in result
    assert "**No confident match** (none scores ≥ 0.90) — confirm the person before using any of these ids." in result
    assert "Unique match" not in result
    assert "Ambiguous" not in result
    assert "Ricardo" not in result


async def test_resolve_with_no_close_match(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient({"users.list": _page([ADA, RICARDO])})
    _patch(monkeypatch, fake)

    result = await slack_resolve_user(ResolveUserInput(query="zzzzqqq"))

    assert "_No one scores ≥ 0.50._" in result
    assert "slack_lookup_user_by_email" in result
    assert "| Score |" not in result


async def test_resolve_respects_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    people = [_user(f"U0ANA{index:06d}", f"ana{index}", f"Ana {index}") for index in range(8)]
    fake = _FakeClient({"users.list": _page(people)})
    _patch(monkeypatch, fake)

    result = await slack_resolve_user(ResolveUserInput(query="Ana", limit=3))

    assert result.count("| 1.00 |") == 3
    assert "**Ambiguous**: 8 people" in result


async def test_resolve_json_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient({"users.list": _by_cursor(DIRECTORY_PAGES)})
    _patch(monkeypatch, fake)

    result = await slack_resolve_user(ResolveUserInput(query="Ricardo", response_format=ResponseFormat.JSON))

    data = json.loads(result)
    assert data["unique_match"] == "U0RIC000001"
    assert data["confident_matches"] == 1
    assert data["searched"] == 3
    assert data["hidden_deleted"] == 1
    assert data["hidden_bots"] == 2
    assert data["directory_complete"] is True
    assert data["matches"][0] == {
        "score": 1.0,
        "id": "U0RIC000001",
        "name": "ricardo.perez",
        "real_name": "Ricardo Pérez",
        "display_name": "Ricardo",
        "title": "",
        "email": "ricardo@example.com",
        "deleted": False,
        "is_bot": False,
    }


async def test_resolve_refresh_rewalks_the_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient({"users.list": _page([ADA])})
    _patch(monkeypatch, fake)

    await slack_resolve_user(ResolveUserInput(query="ada"))
    await slack_resolve_user(ResolveUserInput(query="ada", refresh=True))

    assert fake.calls == [("users.list", {"limit": 200}), ("users.list", {"limit": 200})]


async def test_resolve_cache_is_keyed_by_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient({"users.list": _page([ADA])})
    _patch(monkeypatch, fake)

    await slack_resolve_user(ResolveUserInput(query="ada"))
    monkeypatch.setattr(
        "slack_mcp.tools.users.get_settings", lambda: Settings(slack_api_url="https://other.example.test/api")
    )
    await slack_resolve_user(ResolveUserInput(query="ada"))

    assert len(fake.calls) == 2


async def test_resolve_an_error_mid_walk_caches_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    failing = _FakeClient(
        {
            "users.list": _by_cursor(
                {
                    None: _page([ADA], next_cursor="cGFnZTI="),
                    "cGFnZTI=": SlackApiError("ratelimited", method="users.list"),
                }
            )
        }
    )
    _patch(monkeypatch, failing)

    error = await slack_resolve_user(ResolveUserInput(query="ada"))

    assert error.startswith("Error (ratelimited): Slack refused users.list: ratelimited.")
    assert "wait for Retry-After" in error

    healthy = _FakeClient({"users.list": _page([ADA])})
    _patch(monkeypatch, healthy)
    result = await slack_resolve_user(ResolveUserInput(query="ada"))

    assert healthy.calls == [("users.list", {"limit": 200})]
    assert "**Unique match**: Ada Lovelace" in result


async def test_resolve_stops_at_the_page_cap_and_says_the_index_is_partial(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("slack_mcp.tools.users.MAX_RESOLVE_PAGES", 2)
    fake = _FakeClient({"users.list": lambda params: _page([ADA], next_cursor="more")})
    _patch(monkeypatch, fake)

    result = await slack_resolve_user(ResolveUserInput(query="ada"))

    assert fake.calls == [("users.list", {"limit": 200}), ("users.list", {"limit": 200, "cursor": "more"})]
    assert "The directory index is PARTIAL: it stops after 2 pages (400 people)" in result


@pytest.mark.parametrize("query", ["", "   ", "@", " @ "])
def test_resolve_rejects_an_empty_query_locally(query: str) -> None:
    with pytest.raises(ValidationError):
        ResolveUserInput(query=query)


def test_resolve_rejects_a_limit_over_ten_locally() -> None:
    with pytest.raises(ValidationError, match="less than or equal to 10"):
        ResolveUserInput(query="ada", limit=11)


# --------------------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------------------


async def test_the_four_tools_are_registered_read_only_with_every_docstring_section() -> None:
    from slack_mcp.server import mcp

    tools = {tool.name: tool for tool in await mcp.list_tools()}
    sections = ["When to Use:", "When NOT to Use:", "Returns:", "Examples:", "Error Handling:"]
    for name in ("slack_list_users", "slack_get_user", "slack_lookup_user_by_email", "slack_resolve_user"):
        tool = tools[name]
        assert tool.annotations is not None
        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.destructiveHint is False
        assert tool.annotations.idempotentHint is True
        assert tool.annotations.openWorldHint is True
        description = tool.description or ""
        positions = [description.find(section) for section in sections]
        assert all(position > 0 for position in positions), (name, dict(zip(sections, positions, strict=True)))
        assert positions == sorted(positions), name
        assert "slack_lookup_user_by_email" in description or name == "slack_lookup_user_by_email"
        assert "slack_open_conversation" in description, name
    assert "users:read.email" in (tools["slack_lookup_user_by_email"].description or "")
    assert "Pagination:" in (tools["slack_list_users"].description or "")


# --------------------------------------------------------------------------------------
# Live smokes (read-only; skipped without SLACK_USER_TOKEN)
# --------------------------------------------------------------------------------------


@pytest.mark.live
async def test_list_users_live_smoke() -> None:
    result = await slack_list_users(ListUsersInput(limit=5))

    assert result.startswith("# Slack users"), result


@pytest.mark.live
async def test_get_user_and_resolve_live_smoke() -> None:
    me = await get_client().call("auth.test")
    uid = str(me["user_id"])

    profile = await slack_get_user(GetUserInput(user=uid))
    resolved = await slack_resolve_user(ResolveUserInput(query=str(me["user"])))

    assert uid in profile, profile
    assert "- **mention**:" in profile
    assert uid in resolved, resolved


@pytest.mark.live
async def test_lookup_user_by_email_live_smoke() -> None:
    me = await get_client().call("auth.test")
    info = await get_client().call("users.info", {"user": me["user_id"]})
    email = (info.get("user") or {}).get("profile", {}).get("email")
    if not email:
        pytest.skip("the token cannot see your own email (users:read.email missing or hidden by the workspace)")

    result = await slack_lookup_user_by_email(LookupUserByEmailInput(email=email))

    assert str(me["user_id"]) in result, result
