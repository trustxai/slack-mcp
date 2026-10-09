"""The `live_write` gate in conftest.py, exercised through a real inner pytest run.

Each case copies tests/conftest.py next to a harmless `live_write`-marked test in a temp
directory and runs pytest there in a subprocess with a controlled environment, so what is
checked is the real collection hook, not a re-implementation of it.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import selects_live_write

CONFTEST = Path(__file__).resolve().parent / "conftest.py"

INNER_TEST = """
import pytest


@pytest.mark.live_write
def test_would_write():
    pass
"""

INNER_INI = """[pytest]
markers =
    live: read-only live tests
    live_write: mutating live tests
"""

FAKE_TOKEN = "xoxp-FAKE-0123456789abcdef"


def _run(tmp_path: Path, markexpr: str, env_extra: dict[str, str], dotenv: str | None = None) -> str:
    shutil.copy(CONFTEST, tmp_path / "conftest.py")
    (tmp_path / "test_inner.py").write_text(INNER_TEST)
    (tmp_path / "pytest.ini").write_text(INNER_INI)
    if dotenv is not None:
        (tmp_path / ".env").write_text(dotenv)
    env = {key: value for key, value in os.environ.items() if not key.startswith("SLACK_")}
    env.pop("PYTEST_ADDOPTS", None)
    env.update(env_extra)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-rs",
            "-p",
            "no:cacheprovider",
            "-c",
            str(tmp_path / "pytest.ini"),
            "--rootdir",
            str(tmp_path),
            "-m",
            markexpr,
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
    )
    return result.stdout + result.stderr


ARMED = {"SLACK_USER_TOKEN": FAKE_TOKEN, "SLACK_TEST_ALLOW_WRITES": "1"}


def test_live_write_is_skipped_under_not_live_even_when_armed(tmp_path: Path) -> None:
    out = _run(tmp_path, "not live", ARMED)
    assert "1 skipped" in out, out
    assert "passed" not in out, out


def test_live_write_is_deselected_by_the_standard_gate(tmp_path: Path) -> None:
    out = _run(tmp_path, "not live and not live_write", ARMED)
    assert "1 deselected" in out, out
    assert "passed" not in out, out


@pytest.mark.parametrize(
    "markexpr",
    [
        "not live or not (live_write)",  # selects a live_write-only test: found in review
        "live or live_write",
        "live_write or not live_write",
        "live_write and not live",
    ],
)
def test_no_compound_expression_opens_the_gate_even_when_armed(tmp_path: Path, markexpr: str) -> None:
    out = _run(tmp_path, markexpr, ARMED)
    assert "passed" not in out, out
    assert "1 skipped" in out or "1 deselected" in out, out


def test_live_write_runs_only_when_selected_and_armed_from_the_shell(tmp_path: Path) -> None:
    out = _run(tmp_path, "live_write", ARMED)
    assert "1 passed" in out, out


def test_the_write_switch_in_dotenv_is_ignored(tmp_path: Path) -> None:
    out = _run(
        tmp_path,
        "live_write",
        {"SLACK_USER_TOKEN": FAKE_TOKEN},
        dotenv="SLACK_TEST_ALLOW_WRITES=1\n",
    )
    assert "1 skipped" in out, out
    assert "passed" not in out, out


def test_live_write_needs_the_token_too(tmp_path: Path) -> None:
    out = _run(tmp_path, "live_write", {"SLACK_TEST_ALLOW_WRITES": "1"})
    assert "1 skipped" in out, out


@pytest.mark.parametrize(
    ("markexpr", "expected"),
    [
        ("live_write", True),
        ("  live_write ", True),
        ("live or live_write", False),
        ("live_write and not live", False),
        ("not live or not (live_write)", False),
        ("(live_write)", False),
        ("not live", False),
        ("not live and not live_write", False),
        ("", False),
        (None, False),
    ],
)
def test_selects_live_write(markexpr: str | None, expected: bool) -> None:
    assert selects_live_write(markexpr) is expected


# -- the REAL tests/test_live_write.py under the standard gates ------------------------

REPO = Path(__file__).resolve().parent.parent
LIVE_WRITE_FILE = REPO / "tests" / "test_live_write.py"


def _run_real_live_write_file(tmp_path: Path, markexpr: str) -> str:
    """Run pytest on the real live-write module, ARMED, with the repo's own config.

    SLACK_API_URL points at a closed loopback port, so even a broken gate could not reach
    Slack with the fake token; cwd is a temp dir so no `.env` is read by Settings.
    """
    env = {key: value for key, value in os.environ.items() if not key.startswith("SLACK_")}
    env.pop("PYTEST_ADDOPTS", None)
    env.update(ARMED)
    env["SLACK_API_URL"] = "http://127.0.0.1:9/api/"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-rs",
            "-p",
            "no:cacheprovider",
            "-c",
            str(REPO / "pyproject.toml"),
            "--rootdir",
            str(REPO),
            "-m",
            markexpr,
            str(LIVE_WRITE_FILE),
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
    )
    return result.stdout + result.stderr


def _live_write_test_count() -> int:
    return len(re.findall(r"^(?:async )?def test_", LIVE_WRITE_FILE.read_text(), flags=re.MULTILINE))


def test_the_real_live_write_file_is_deselected_by_the_standard_gate(tmp_path: Path) -> None:
    count = _live_write_test_count()
    assert count >= 1
    out = _run_real_live_write_file(tmp_path, "not live and not live_write")
    assert f"{count} deselected" in out, out
    assert "passed" not in out and "failed" not in out and "error" not in out.lower(), out


def test_the_real_live_write_file_is_skipped_under_not_live_even_when_armed(tmp_path: Path) -> None:
    count = _live_write_test_count()
    out = _run_real_live_write_file(tmp_path, "not live")
    assert f"{count} skipped" in out, out
    assert "passed" not in out and "failed" not in out and "xfail" not in out, out
