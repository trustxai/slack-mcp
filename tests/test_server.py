"""Registry + entry-point tests for the Stage-0 spine."""

from __future__ import annotations

import importlib
import importlib.metadata
import sys
from pathlib import Path

import pytest

import slack_mcp
import slack_mcp.server as server
from slack_mcp.server import mcp

EXPECTED_MODULES = ["chat", "conversations", "health", "reactions", "search", "users"]

PYPROJECT = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()


def test_registry_imports_every_wave_module() -> None:
    for name in EXPECTED_MODULES:
        assert f"slack_mcp.tools.{name}" in sys.modules, f"tools/{name}.py is not imported by register_all"


async def test_health_tool_is_registered_with_annotations() -> None:
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    assert "slack_health_check" in tools
    health = tools["slack_health_check"]
    assert health.annotations is not None
    assert health.annotations.readOnlyHint is True
    assert health.annotations.destructiveHint is False
    assert health.annotations.idempotentHint is True
    assert health.annotations.openWorldHint is True
    assert (health.description or "").splitlines()[0].startswith("Verify the token against Slack")


async def test_the_exemplar_docstring_has_every_section_in_order() -> None:
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    description = tools["slack_health_check"].description or ""
    sections = ["When to Use:", "When NOT to Use:", "Returns:", "Examples:", "Error Handling:"]
    positions = [description.find(section) for section in sections]
    assert all(position > 0 for position in positions), dict(zip(sections, positions, strict=True))
    assert positions == sorted(positions)


async def test_every_registered_tool_has_name_prefix_annotations_and_description() -> None:
    for tool in await mcp.list_tools():
        assert tool.name.startswith("slack_"), tool.name
        assert tool.annotations is not None, tool.name
        assert tool.annotations.openWorldHint is True, tool.name
        assert tool.description, tool.name


def test_one_stdio_console_script_and_no_http_entry_point() -> None:
    assert 'amazing-slack-mcp = "slack_mcp.server:main_stdio"' in PYPROJECT
    assert "amazing-slack-mcp-http" not in PYPROJECT
    assert "main_http" not in PYPROJECT
    assert not hasattr(server, "main_http")
    assert callable(server.main_stdio)


def test_version_comes_from_the_installed_distribution() -> None:
    assert slack_mcp.__version__ == importlib.metadata.version("amazing-slack-mcp")


def test_version_falls_back_when_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", missing)
    try:
        assert importlib.reload(slack_mcp).__version__ == "0.0.0"
    finally:
        monkeypatch.undo()
        importlib.reload(slack_mcp)
    assert slack_mcp.__version__ == importlib.metadata.version("amazing-slack-mcp")


def test_python_m_entry_point_runs_stdio() -> None:
    main_src = (Path(__file__).resolve().parents[1] / "src" / "slack_mcp" / "__main__.py").read_text()
    assert "main_stdio()" in main_src
