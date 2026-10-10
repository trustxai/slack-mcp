"""No fixture under tests/ looks like a real Slack token.

GitHub push protection flagged a test string with realistic digit groups
(`xoxp-` + digits-dash-digits) as a live Slack token, and the first push needed a bypass.
Fixtures therefore start with `FAKE` right after the family prefix (`xoxp-FAKE-…`,
`xoxe.xoxp-FAKE-…`, `xapp-FAKE-…`): the redaction regex still matches them, the secret
scanners do not. A test that needs the realistic shape builds it at runtime, so the
literal never appears in a file (see `REALISTIC_SHAPED_TOKEN` in test_errors.py).
"""

from __future__ import annotations

import re
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
