"""Run one memlab scenario: start server.py, fire N concurrent tools/call, report memory.

Usage: python client.py TOOL ARGS_JSON CONCURRENCY [NAME=VALUE ...]

NAME=VALUE pairs are set in the server's environment. Prints one JSON line:
RSS after a warm-up call, peak RSS (VmHWM) during the burst, RSS one second
after it, response size and the slowest call's duration, all in MiB / seconds.
Set MEMLAB_SERVER_LOG=path to keep the server's log.
"""

import asyncio
import json
import os
import subprocess
import sys
import time

import httpx

HERE = os.path.dirname(os.path.abspath(__file__))
_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "MCP-Protocol-Version": "2025-06-18",
}


def _memory_mib(pid):
    values = {}
    with open(f"/proc/{pid}/status") as f:
        for line in f:
            if line.startswith(("VmRSS", "VmHWM")):
                key, value = line.split(":")
                values[key] = int(value.split()[0]) // 1024
    return values


async def _call(client, request_id, tool, arguments):
    body = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }
    size, started, head = 0, time.monotonic(), b""
    async with client.stream("POST", "/mcp", json=body, headers=_HEADERS) as response:
        async for chunk in response.aiter_bytes():
            head += chunk[: max(0, 400 - len(head))]
            size += len(chunk)
    error = b'"isError":true' in head or response.status_code != 200
    return {"bytes": size, "secs": time.monotonic() - started, "error": error}


async def main():
    tool, arguments, concurrency = (
        sys.argv[1],
        json.loads(sys.argv[2]),
        int(sys.argv[3]),
    )
    env = dict(os.environ)
    env.update(pair.split("=", 1) for pair in sys.argv[4:])
    port = 18000 + os.getpid() % 1000
    log_path = os.environ.get("MEMLAB_SERVER_LOG")
    log = open(log_path, "w") if log_path else subprocess.DEVNULL
    server = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "server.py"), str(port)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=log,
        text=True,
    )
    try:
        pid = int(server.stdout.readline().split("pid=")[1])
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", timeout=600
        ) as client:
            for _ in range(200):
                try:
                    await client.get("/mcp")
                    break
                except httpx.ConnectError:
                    await asyncio.sleep(0.1)
            # Pay lazy imports and payload synthesis before measuring.
            await _call(client, 0, tool, arguments)
            await asyncio.sleep(0.5)
            before = _memory_mib(pid)["VmRSS"]
            with open(f"/proc/{pid}/clear_refs", "w") as f:
                f.write("5")  # reset VmHWM: measure this burst only
            results = await asyncio.gather(
                *(_call(client, i + 1, tool, arguments) for i in range(concurrency))
            )
            peak = _memory_mib(pid)["VmHWM"]
            await asyncio.sleep(1.0)
            after = _memory_mib(pid)["VmRSS"]
    finally:
        server.terminate()
        server.wait()
    print(
        json.dumps(
            {
                "rss_before": before,
                "peak": peak,
                "added": peak - before,
                "rss_after": after,
                "response_mb": round(results[0]["bytes"] / 1e6, 2),
                "slowest_s": round(max(r["secs"] for r in results), 2),
                "errors": sum(r["error"] for r in results),
            }
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
