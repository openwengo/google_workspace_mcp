"""Tests for the per-class tool concurrency gate and its metrics middleware."""

import asyncio
import logging

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from core import tool_concurrency
from core.tool_concurrency import (
    DEFAULT_HEAVY_TOOLS,
    HEAVY,
    LIGHT,
    ToolConcurrencyMiddleware,
    ToolGate,
    ToolGateConfig,
    load_tool_gate_config,
    reset_tool_gate,
)

_ENV = (
    "WORKSPACE_MCP_MAX_CONCURRENT_HEAVY_TOOLS",
    "WORKSPACE_MCP_MAX_CONCURRENT_LIGHT_TOOLS",
    "WORKSPACE_MCP_TOOL_QUEUE_TIMEOUT",
    "WORKSPACE_MCP_HEAVY_TOOLS",
    "WORKSPACE_MCP_MALLOC_TRIM",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    reset_tool_gate()
    yield
    reset_tool_gate()


def _gate(heavy=1, light=2, timeout=5.0, heavy_tools=("big",)):
    return ToolGate(
        ToolGateConfig(heavy, light, timeout, frozenset(heavy_tools), malloc_trim=False)
    )


def test_default_config():
    config = load_tool_gate_config()
    assert (config.heavy_limit, config.light_limit) == (2, 8)
    assert config.queue_timeout == 120
    assert config.heavy_tools == DEFAULT_HEAVY_TOOLS
    assert config.malloc_trim is True


def test_config_from_environment(monkeypatch):
    monkeypatch.setenv("WORKSPACE_MCP_MAX_CONCURRENT_HEAVY_TOOLS", "1")
    monkeypatch.setenv("WORKSPACE_MCP_MAX_CONCURRENT_LIGHT_TOOLS", "0")
    monkeypatch.setenv("WORKSPACE_MCP_TOOL_QUEUE_TIMEOUT", "30")
    monkeypatch.setenv("WORKSPACE_MCP_HEAVY_TOOLS", "a, b,,")
    monkeypatch.setenv("WORKSPACE_MCP_MALLOC_TRIM", "off")

    config = load_tool_gate_config()

    assert (config.heavy_limit, config.light_limit) == (1, 0)
    assert config.queue_timeout == 30
    assert config.heavy_tools == {"a", "b"}
    assert config.malloc_trim is False


@pytest.mark.parametrize(
    "name, value",
    [
        ("WORKSPACE_MCP_MAX_CONCURRENT_HEAVY_TOOLS", "-1"),
        ("WORKSPACE_MCP_MAX_CONCURRENT_LIGHT_TOOLS", "two"),
        ("WORKSPACE_MCP_TOOL_QUEUE_TIMEOUT", "0"),
        ("WORKSPACE_MCP_MALLOC_TRIM", "maybe"),
    ],
)
def test_invalid_config_is_rejected(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        load_tool_gate_config()


def test_default_heavy_tools_are_registered_tools():
    """Renamed tools must not silently fall back to the light lane."""
    from core.server import server
    from core.tool_registry import get_tool_components

    for module in (
        "gdocs.docs_tools",
        "gdrive.drive_tools",
        "gmail.gmail_tools",
        "gslides.slides_tools",
        "gchat.chat_tools",
    ):
        __import__(module)

    assert DEFAULT_HEAVY_TOOLS <= set(get_tool_components(server))


@pytest.mark.asyncio
async def test_heavy_calls_queue_while_light_calls_proceed():
    gate = _gate(heavy=1)
    release = asyncio.Event()
    running = []

    async def heavy():
        async with gate.slot("big") as slot:
            running.append(slot.cost_class)
            await release.wait()
            return slot

    first = asyncio.create_task(heavy())
    second = asyncio.create_task(heavy())
    await asyncio.sleep(0.05)
    assert running == [HEAVY]

    async with gate.slot("small") as light_slot:
        assert light_slot.cost_class == LIGHT
        assert light_slot.inflight_heavy == 1

    release.set()
    first_slot, second_slot = await asyncio.gather(first, second)
    assert first_slot.waited < 0.05 <= second_slot.waited


@pytest.mark.asyncio
async def test_queue_timeout_reports_busy():
    gate = _gate(heavy=1, timeout=0.05)
    async with gate.slot("big"):
        with pytest.raises(ToolError, match="Server busy"):
            async with gate.slot("big"):
                pass


@pytest.mark.asyncio
async def test_zero_limit_means_unlimited():
    gate = _gate(heavy=0, timeout=0.05)
    async with gate.slot("big"), gate.slot("big"), gate.slot("big") as slot:
        assert slot.inflight_heavy == 3  # counts include the admitted call


@pytest.mark.asyncio
async def test_memory_is_trimmed_once_calls_drain():
    gate = _gate(heavy=2, light=2)
    trims = []
    gate._malloc_trim = trims.append

    async with gate.slot("big"):
        async with gate.slot("small"):
            pass
        assert gate.trim_future is None  # a heavy call is still running
    await gate.trim_future
    assert trims == [0]

    first_trim = gate.trim_future
    async with gate.slot("small"):
        pass
    assert gate.trim_future is first_trim  # rate-limited


def _server_with_gate() -> FastMCP:
    server = FastMCP("gate-test")
    server.add_middleware(ToolConcurrencyMiddleware())

    @server.tool
    async def get_doc_as_markdown(delay: float = 0.0) -> str:
        await asyncio.sleep(delay)
        return "x" * 10

    return server


@pytest.mark.asyncio
async def test_middleware_logs_one_metrics_line_per_call(caplog):
    caplog.set_level(logging.INFO, logger="core.tool_concurrency")
    async with Client(_server_with_gate()) as client:
        await client.call_tool("get_doc_as_markdown", {})

    lines = [r.getMessage() for r in caplog.records if "tool_call" in r.getMessage()]
    assert len(lines) == 1
    assert "tool=get_doc_as_markdown class=heavy status=ok" in lines[0]
    assert "result_chars=10" in lines[0]


@pytest.mark.asyncio
async def test_middleware_returns_busy_error_when_queue_times_out(monkeypatch, caplog):
    monkeypatch.setenv("WORKSPACE_MCP_MAX_CONCURRENT_HEAVY_TOOLS", "1")
    monkeypatch.setenv("WORKSPACE_MCP_TOOL_QUEUE_TIMEOUT", "0.1")
    caplog.set_level(logging.INFO, logger="core.tool_concurrency")

    async with Client(_server_with_gate()) as client:
        slow = asyncio.create_task(
            client.call_tool("get_doc_as_markdown", {"delay": 0.5})
        )
        await asyncio.sleep(0.05)
        with pytest.raises(ToolError, match="Server busy"):
            await client.call_tool("get_doc_as_markdown", {})
        await slow

    statuses = [
        r.getMessage().split("status=")[1].split()[0]
        for r in caplog.records
        if "tool_call" in r.getMessage()
    ]
    assert sorted(statuses) == ["busy", "ok"]


def test_gate_is_shared_by_the_process():
    assert tool_concurrency.get_tool_gate() is tool_concurrency.get_tool_gate()
