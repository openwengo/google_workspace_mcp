"""
Process-wide limits on concurrent tool calls, by cost class, plus per-call metrics.

Tools that may hold a whole document, file or message in memory are "heavy" and
share a narrow lane; every other tool shares a wider "light" lane. A call beyond
its lane's limit waits for a slot instead of failing, up to a queue timeout.
When calls drain, glibc is asked to hand freed memory back to the OS: without
``malloc_trim`` the process keeps a burst's peak RSS and the next burst starts
from there.

Every tool call logs one ``tool_call`` line with its wait, duration, result size
and memory readings, so memory spikes can be attributed to tools.

Environment:
    WORKSPACE_MCP_MAX_CONCURRENT_HEAVY_TOOLS  default 2, 0 = unlimited
    WORKSPACE_MCP_MAX_CONCURRENT_LIGHT_TOOLS  default 8, 0 = unlimited
    WORKSPACE_MCP_TOOL_QUEUE_TIMEOUT          seconds a call may wait, default 120
    WORKSPACE_MCP_HEAVY_TOOLS                 comma-separated names; replaces the default list
    WORKSPACE_MCP_MALLOC_TRIM                 default true
"""

import asyncio
import contextlib
import ctypes
import logging
import os
import sys
import time
from dataclasses import dataclass
from typing import AsyncIterator, Callable, FrozenSet, Optional

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

from core.process_memory import cgroup_memory_bytes, peak_rss_bytes, rss_bytes

logger = logging.getLogger(__name__)

HEAVY = "heavy"
LIGHT = "light"

# Tools that fetch a whole Doc (including the Docs write tools whose managers
# read the document first), download or export files, or read full messages.
DEFAULT_HEAVY_TOOLS: FrozenSet[str] = frozenset(
    {
        # Docs
        "get_doc_content",
        "get_doc_as_markdown",
        "inspect_doc_structure",
        "debug_table_structure",
        "export_doc_to_pdf",
        "manage_doc_tab",
        "batch_update_doc",
        "create_table_with_data",
        "update_doc_headers_footers",
        # Drive
        "get_drive_file_content",
        "get_drive_file_download_url",
        "create_drive_file",
        "update_drive_file",
        "import_to_google_doc",
        "import_to_google_sheets",
        # Gmail
        "get_gmail_message_content",
        "get_gmail_messages_content_batch",
        "get_gmail_thread_content",
        "get_gmail_threads_content_batch",
        "get_gmail_attachment_content",
        "send_gmail_message",
        "draft_gmail_message",
        # Slides
        "get_presentation",
        "get_page_thumbnail",
        # Chat
        "download_chat_attachment",
    }
)

_TRIM_MIN_INTERVAL_SECONDS = 1.0
_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}


@dataclass(frozen=True)
class ToolGateConfig:
    heavy_limit: int
    light_limit: int
    queue_timeout: float
    heavy_tools: FrozenSet[str]
    malloc_trim: bool


def _non_negative_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        value = -1
    if value < 0:
        raise ValueError(f"Invalid {name}={raw!r}; expected a non-negative integer.")
    return value


def load_tool_gate_config() -> ToolGateConfig:
    """Read the gate configuration from the environment; raise ValueError if invalid."""
    timeout_raw = os.getenv("WORKSPACE_MCP_TOOL_QUEUE_TIMEOUT", "").strip() or "120"
    try:
        queue_timeout = float(timeout_raw)
    except ValueError:
        queue_timeout = -1.0
    if queue_timeout <= 0:
        raise ValueError(
            f"Invalid WORKSPACE_MCP_TOOL_QUEUE_TIMEOUT={timeout_raw!r}; "
            "expected a positive number of seconds."
        )

    heavy_raw = os.getenv("WORKSPACE_MCP_HEAVY_TOOLS", "").strip()
    heavy_tools = (
        frozenset(name.strip() for name in heavy_raw.split(",") if name.strip())
        if heavy_raw
        else DEFAULT_HEAVY_TOOLS
    )

    trim_raw = os.getenv("WORKSPACE_MCP_MALLOC_TRIM", "").strip().lower() or "true"
    if trim_raw not in _TRUTHY | _FALSY:
        raise ValueError(
            f"Invalid WORKSPACE_MCP_MALLOC_TRIM={trim_raw!r}; expected true or false."
        )

    return ToolGateConfig(
        heavy_limit=_non_negative_int("WORKSPACE_MCP_MAX_CONCURRENT_HEAVY_TOOLS", 2),
        light_limit=_non_negative_int("WORKSPACE_MCP_MAX_CONCURRENT_LIGHT_TOOLS", 8),
        queue_timeout=queue_timeout,
        heavy_tools=heavy_tools,
        malloc_trim=trim_raw in _TRUTHY,
    )


def _load_malloc_trim() -> Optional[Callable[[int], int]]:
    """glibc's malloc_trim, or None (non-Linux, musl)."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        return ctypes.CDLL("libc.so.6").malloc_trim
    except (OSError, AttributeError):
        return None


@dataclass
class _Lane:
    limit: int
    semaphore: Optional[asyncio.Semaphore]
    inflight: int = 0


@dataclass(frozen=True)
class Slot:
    cost_class: str
    waited: float
    inflight_heavy: int
    inflight_light: int


class ToolGate:
    """Two lanes of bounded concurrency shared by every FastMCP app in the process."""

    def __init__(self, config: ToolGateConfig):
        self.config = config
        self._malloc_trim = _load_malloc_trim() if config.malloc_trim else None
        self._last_trim = 0.0
        self.trim_future: Optional[asyncio.Future] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._lanes: dict = {}

    def cost_class(self, tool_name: str) -> str:
        return HEAVY if tool_name in self.config.heavy_tools else LIGHT

    def _current_lanes(self) -> dict:
        # asyncio primitives bind to one loop; production has a single loop,
        # tests create one per test.
        loop = asyncio.get_running_loop()
        if loop is not self._loop:
            self._loop = loop
            self._lanes = {
                cls: _Lane(limit, asyncio.Semaphore(limit) if limit else None)
                for cls, limit in (
                    (HEAVY, self.config.heavy_limit),
                    (LIGHT, self.config.light_limit),
                )
            }
        return self._lanes

    @contextlib.asynccontextmanager
    async def slot(self, tool_name: str) -> AsyncIterator[Slot]:
        lanes = self._current_lanes()
        cost_class = self.cost_class(tool_name)
        lane = lanes[cost_class]
        started = time.monotonic()
        if lane.semaphore is not None:
            try:
                await asyncio.wait_for(
                    lane.semaphore.acquire(), self.config.queue_timeout
                )
            except asyncio.TimeoutError:
                raise ToolError(
                    f"Server busy: {lane.inflight} {cost_class} tool calls are running "
                    f"(limit {lane.limit}) and {tool_name} waited "
                    f"{self.config.queue_timeout:g}s for a slot. Retry in a moment."
                ) from None
        lane.inflight += 1
        try:
            yield Slot(
                cost_class,
                time.monotonic() - started,
                lanes[HEAVY].inflight,
                lanes[LIGHT].inflight,
            )
        finally:
            lane.inflight -= 1
            if lane.semaphore is not None:
                lane.semaphore.release()
            if lanes[HEAVY].inflight + lanes[LIGHT].inflight == 0 or (
                cost_class == HEAVY and lanes[HEAVY].inflight == 0
            ):
                self._trim()

    def _trim(self) -> None:
        if self._malloc_trim is None:
            return
        now = time.monotonic()
        if now - self._last_trim < _TRIM_MIN_INTERVAL_SECONDS:
            return
        self._last_trim = now
        # Off the event loop: on a fragmented heap of ~1 GiB a trim takes
        # ~250 ms, which would stall every in-flight request.
        self.trim_future = asyncio.get_running_loop().run_in_executor(
            None, self._timed_trim
        )

    def _timed_trim(self) -> None:
        started = time.monotonic()
        self._malloc_trim(0)
        logger.debug("malloc_trim took %.1f ms", (time.monotonic() - started) * 1000)


_gate: Optional[ToolGate] = None


def get_tool_gate() -> ToolGate:
    global _gate
    if _gate is None:
        _gate = ToolGate(load_tool_gate_config())
    return _gate


def reset_tool_gate() -> None:
    """Drop the process gate so the next call rereads the environment (tests)."""
    global _gate
    _gate = None


def _mib(value: Optional[int]) -> str:
    return "-" if value is None else str(round(value / 2**20))


def _result_size(result) -> int:
    """Characters of text plus base64 payload carried by a tool result."""
    size = 0
    for block in getattr(result, "content", None) or ():
        text = getattr(block, "text", None) or getattr(block, "data", None)
        if isinstance(text, str):
            size += len(text)
    return size


class ToolConcurrencyMiddleware(Middleware):
    """Admit tool calls through the process gate and log one metrics line per call."""

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext):
        gate = get_tool_gate()
        tool_name = context.message.name
        rss_before = rss_bytes()
        status, slot, result = "error", None, None
        arrived = started = time.monotonic()
        try:
            async with gate.slot(tool_name) as slot:
                started = time.monotonic()
                result = await call_next(context)
            status = "tool_error" if getattr(result, "is_error", False) else "ok"
            return result
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        except ToolError:
            if slot is None:
                status = "busy"
            raise
        finally:
            finished = time.monotonic()
            usage, limit = cgroup_memory_bytes()
            logger.info(
                "tool_call tool=%s class=%s status=%s wait_ms=%d duration_ms=%d "
                "result_chars=%d rss_mb=%s->%s peak_rss_mb=%s cgroup_mb=%s/%s "
                "inflight_heavy=%s inflight_light=%s",
                tool_name,
                gate.cost_class(tool_name),
                status,
                (started - arrived) * 1000 if slot else (finished - arrived) * 1000,
                (finished - started) * 1000 if slot else 0,
                _result_size(result),
                _mib(rss_before),
                _mib(rss_bytes()),
                _mib(peak_rss_bytes()),
                _mib(usage),
                _mib(limit),
                slot.inflight_heavy if slot else "-",
                slot.inflight_light if slot else "-",
            )
