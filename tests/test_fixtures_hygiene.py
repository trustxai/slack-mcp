"""What this public repo must not hold: realistic Slack tokens, or real workspace and client identifiers.

GitHub push protection flagged a test string with realistic digit groups
(`xoxp-` + digits-dash-digits) as a live Slack token, and the first push needed a bypass.
Fixtures therefore start with `FAKE` right after the family prefix (`xoxp-FAKE-…`,
`xoxe.xoxp-FAKE-…`, `xapp-FAKE-…`): the redaction regex still matches them, the secret
scanners do not. A test that needs the realistic shape builds it at runtime, so the
literal never appears in a file (see `REALISTIC_SHAPED_TOKEN` in test_errors.py).

Examples and fixtures also use placeholder workspaces and ids (`acme.slack.com`,
`slack-acme` / `slack-globex`, `A0123456789`, `U0BOTUSER01`, `T0123ABCDEF`) instead of the
real ones that once sat in the README, `.env.example` and the tests; the second half of
this file keeps them from coming back.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
_SKIPPED_DIRS = {"__pycache__", ".pytest_cache"}

# The two realistic shapes named in the task: a classic token (two 10+ digit groups) and
# a rotating one (`xoxe.xoxb-<digit>-<digits>` / `xoxe.xoxp-<digit>-<digits>`).
_CLASSIC = re.compile(r"xox[abps]-[0-9]{10,}-[0-9]{10,}", re.IGNORECASE)
_ROTATING = re.compile(r"xoxe\.xox[bp]-[0-9]+-[0-9]{10,}", re.IGNORECASE)
# Stricter house rule, covering what GitHub actually flagged (`xoxp-<4 digits>-<4 digits>-…`, short
# groups) and the `xapp-<digit>-A…` / `xoxe-<digit>-…` shapes: every real Slack token family has a
# digit right after its prefix, so no fixture may.
_DIGIT_AFTER_PREFIX = re.compile(r"(?:xox[a-z]|xapp|xwfp)(?:\.(?:xox[a-z]|xapp))?-[0-9]", re.IGNORECASE)
PATTERNS = {"classic": _CLASSIC, "rotating": _ROTATING, "digit-after-prefix": _DIGIT_AFTER_PREFIX}


def _test_files() -> list[Path]:
    return sorted(
        path
        for path in TESTS_DIR.rglob("*")
        if path.is_file() and not _SKIPPED_DIRS.intersection(path.relative_to(TESTS_DIR).parts)
    )


def _hits(pattern: re.Pattern[str]) -> list[str]:
    hits = []
    for path in _test_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), start=1):
            hits.extend(f"{path.relative_to(TESTS_DIR)}:{number}: {m.group(0)}" for m in pattern.finditer(line))
    return hits


def test_the_scan_covers_the_test_files() -> None:
    names = {path.name for path in _test_files()}
    assert {"test_errors.py", "test_safety.py", "test_fixtures_hygiene.py"} <= names, names


@pytest.mark.parametrize("name", sorted(PATTERNS))
def test_no_test_file_holds_a_realistic_slack_token_literal(name: str) -> None:
    hits = _hits(PATTERNS[name])
    assert not hits, "realistic Slack token shapes in tests/ (use xoxp-FAKE-…):\n" + "\n".join(hits)


# Both branches of the scanner: realistic shapes (built at runtime, never literal) are
# caught, and the FAKE fixture shapes are not.
_REALISTIC = {
    "classic": ["-".join(["xoxp", "1" * 12, "2" * 12, "3" * 12, "f" * 32]), "-".join(["xoxb", "1" * 10, "2" * 13])],
    "rotating": ["-".join(["xoxe.xoxp", "1", "2" * 12, "abc"]), "-".join(["xoxe.xoxb", "1", "2" * 10])],
    "digit-after-prefix": [
        "-".join(["xoxp", "1111", "2222", "3333", "abc"]),
        "-".join(["xapp", "1", "A0123", "456"]),
        "-".join(["xoxe", "1", "abc"]),
        "-".join(["XOXE.XOXP", "1", "abc"]),
    ],
}
_FAKE = [
    "xoxp-FAKE-0123456789abcdef",
    "xoxp-FAKE-0000000000-0000000000-abcdefghij",
    "xoxe.xoxp-FAKE-0123456789abcdef",
    "xoxe.xapp-FAKE-0123456789",
    "xapp-FAKE-A0123-0123456789",
    "xoxe-FAKE-REFRESH0123456789",
    "XOXB-FAKE-0123456789ABCDEF",
]


@pytest.mark.parametrize("name", sorted(PATTERNS))
def test_the_patterns_catch_realistic_shapes_and_spare_fake_ones(name: str) -> None:
    pattern = PATTERNS[name]
    assert all(pattern.search(sample) for sample in _REALISTIC[name]), _REALISTIC[name]
    assert not [sample for sample in _FAKE if pattern.search(sample)]
    # The stricter rule is a superset of the two named shapes.
    assert all(_DIGIT_AFTER_PREFIX.search(sample) for samples in _REALISTIC.values() for sample in samples)


# --- No real workspace or client identifier in a tracked file ---------------------------
#
# The forbidden identifiers (client names, workspace subdomains, real app / user / team /
# channel / DM ids) are listed only as SHA-256 digests of their lowercase form, grouped by
# length, so this file does not publish them again. The scan matches like `git grep -i`:
# any substring of a run of letters and digits, ignoring case. To ban one more, add
#   python -c "import hashlib; print(hashlib.sha256(b'<lowercase identifier>').hexdigest())"
# under its length. A failure names file:line only, never the identifier (CI logs are public).

REPO_ROOT = TESTS_DIR.parent
_NOT_SCANNED = {"CHANGELOG.md", "uv.lock"}  # release-please writes the first; the second is generated
_FORBIDDEN: dict[int, frozenset[str]] = {
    5: frozenset(
        {
            "98dd5ef39c8fc5d07f4e9c6f16552e0a9ec2f6827bf7cc6df809c200a8b20393",
            "ce626ab0e843e51263ca29353e1cb273eeb3d1ea5ff8135852b628f107503087",
        }
    ),
    6: frozenset({"f0f904c7968a161b403cde4747e0ace0894cb13955206f260d2cc081f5aa9a9c"}),
    9: frozenset(
        {
            "8432ef34fe95ef520604983ea741b96c9a3bf351c05a5b4a33a2f38d14aaaae6",
            "26c94ef039b584d7e966e715263028fce5c4dd5c7bc8553283830a799e4f3b6d",
            "d8ce6b9cae707886cb0fc61b11e23be1e42cdb9cf67cfed45f242d4e8033d77a",
        }
    ),
    11: frozenset(
        {
            "0c80f2a71e18a4efe45f9d8768b3d705b03bdadb1b8c25bcdebc39974b210b4a",
            "8b494767e0e73f74d036bab9a3ce24f684853f620aeaf42efe022bf4d1940bc4",
            "ddba743348972e2761541ae542d4ce0429b2d9dc28bbb6703e40548e1838c711",
            "bc1817bdcb074520333acf8ce7b1262fd33c0cfb994434d619fa08bc7e5f4425",
            "5e2657509202885ec64c83649a7da4d34f8ff1d6ac1db0845c0a0e8671eee7d3",
            "90b41e20878a28d31dc72f56f64e8cb5effd88df2812318de5d38f75e49858ce",
            "cdc6ebd058ed21a8040ae57ac7b07bfe662ac93f942e69c7be65c87ef06c2388",
            "bac4551c1c220d0163d7551292a4af78a026e7a99c88fb751f546eddfb6a9a62",
        }
    ),
}
_RUN = re.compile(r"[a-z0-9]+")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _forbidden_runs(text: str, forbidden: Mapping[int, frozenset[str]]) -> set[str]:
    """The letters-and-digits runs of `text`, lowercased, that hold a forbidden substring."""
    return {
        run
        for run in set(_RUN.findall(text.lower()))
        if any(
            _sha256(run[start : start + length]) in digests
            for length, digests in forbidden.items()
            for start in range(len(run) - length + 1)
        )
    }


def _tracked_files() -> list[Path]:
    # Tracked files only: an untracked .env or scratch note may hold real values legitimately.
    try:
        listed = subprocess.run(["git", "-C", str(REPO_ROOT), "ls-files", "-z"], capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        pytest.skip(f"the identifier scan needs `git ls-files` in a checkout: {exc}")
    names = [name for name in listed.stdout.decode().split("\0") if name and name not in _NOT_SCANNED]
    return [REPO_ROOT / name for name in names if (REPO_ROOT / name).is_file()]


def test_the_identifier_scan_covers_the_tracked_files() -> None:
    names = {path.relative_to(REPO_ROOT).as_posix() for path in _tracked_files()}
    expected = {"README.md", ".env.example", "loop.config.json", "src/slack_mcp/server.py", "tests/test_health.py"}
    assert expected <= names, names
    assert not _NOT_SCANNED & names


def test_no_tracked_file_holds_a_real_workspace_or_client_identifier() -> None:
    texts = {path: path.read_text(encoding="utf-8", errors="replace") for path in _tracked_files()}
    forbidden = _forbidden_runs("\n".join(texts.values()), _FORBIDDEN)
    hits = [
        f"{path.relative_to(REPO_ROOT)}:{number}"
        for path, text in texts.items()
        for number, line in enumerate(text.splitlines(), start=1)
        if forbidden.intersection(_RUN.findall(line.lower()))
    ]
    assert not hits, "real workspace or client identifiers (use acme / globex / A0123456789-style placeholders):\n" + (
        "\n".join(hits)
    )


# Both branches of the scan, with a planted identifier instead of a real one: it is caught in
# every shape a real one took (subdomain, entry name, env var, a longer word), case aside, and a
# near miss is not. The placeholders the docs and fixtures use now must never be forbidden.
_PLANTED = {10: frozenset({_sha256("zzclientco")})}


@pytest.mark.parametrize(
    "text",
    [
        "https://ZZClientCo.slack.com/archives/C0123ABCDEF",
        "claude mcp add-json slack-zzclientco",
        '"${env:SLACK_ZZCLIENTCO_TOKEN}"',
        "zzclientcoinc",
    ],
)
def test_the_identifier_scan_catches_a_planted_identifier(text: str) -> None:
    assert _forbidden_runs(text, _PLANTED)


@pytest.mark.parametrize("text", ["zz-clientco", "zzclientc0", "acme.slack.com"])
def test_the_identifier_scan_spares_a_near_miss(text: str) -> None:
    assert not _forbidden_runs(text, _PLANTED)


def test_the_placeholders_are_not_forbidden() -> None:
    placeholders = (
        "https://acme.slack.com/ https://acme-labs.slack.com/ slack-acme slack-globex SLACK_ACME_TOKEN "
        "SLACK_GLOBEX_TOKEN op://Private/slack-acme/credential op://Private/slack-globex/credential "
        "A0123456789 U0BOTUSER01 U0123ABCDEF U0456GHIJKL C0123ABCDEF D0123ABCDEF T0123ABCDEF"
    )
    assert not _forbidden_runs(placeholders, _FORBIDDEN)
