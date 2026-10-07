"""Inventory MCP server for the incident lab.

Fault mode is chosen per request from the `x-lab-fault` header (read through the
MCP request context, `Context.headers`). Ground truth for every tool call is
appended to runs/truth.jsonl by this file, outside the Sentry SDK.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

from mcp.server.mcpserver import Context, MCPServer
from mcp_types import CallToolResult, TextContent

HERE = Path(__file__).resolve().parent
INVENTORY = json.loads((HERE / "inventory.json").read_text())
FAULTS = {"tool_error", "stale_data", "slow"}
SLOW_SECONDS = float(os.environ.get("LAB_SLOW_SECONDS", "6"))


def runs_dir() -> Path:
    return Path(os.environ.get("LAB_RUNS_DIR", HERE.parent / "runs"))


def truth_path() -> Path:
    return runs_dir() / "truth.jsonl"


def append_truth(row: dict) -> None:
    p = truth_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write(json.dumps(row) + "\n")


def lookup(sku: str) -> dict:
    row = INVENTORY.get(sku, {"in_stock": 0, "warehouse": "unknown"})
    return {"sku": sku, "in_stock": row["in_stock"], "warehouse": row["warehouse"]}


async def run_check(sku: str, fault: str | None, run_id: str | None,
                    sentry_trace: str | None) -> tuple[dict | None, bool, str | None]:
    """Do the work for one call and log truth. Returns (payload, is_error, error_text)."""
    t0 = time.perf_counter()
    payload: dict | None = None
    is_error = False
    err: str | None = None
    returned: object = None
    try:
        if fault == "tool_error":
            is_error, err = True, "warehouse DB timeout"
            returned = {"isError": True, "text": err}
        elif fault == "stale_data":
            # Stale cache: claims zero stock, no error anywhere.
            payload = {**lookup(sku), "in_stock": 0}
            returned = payload
        elif fault == "slow":
            await asyncio.sleep(SLOW_SECONDS)
            payload = lookup(sku)
            returned = payload
        else:
            payload = lookup(sku)
            returned = payload
        return payload, is_error, err
    finally:
        append_truth({
            "run_id": run_id, "fault": fault or "none", "args": {"sku": sku},
            "returned": returned, "is_error": is_error,
            "duration_ms": round((time.perf_counter() - t0) * 1000, 1),
            "sentry_trace": sentry_trace,
        })


def build_app():
    """Create the Sentry-instrumented app. Sentry must be initialised before this runs."""
    mcp = MCPServer("inventory")

    @mcp.tool()
    async def check_inventory(sku: str, ctx: Context) -> CallToolResult:
        """Look up units in stock for a SKU."""
        h = ctx.headers or {}
        fault = h.get("x-lab-fault") or None
        if fault not in FAULTS:
            fault = None
        payload, is_error, err = await run_check(
            sku, fault, h.get("x-lab-run-id"), h.get("sentry-trace"))
        if is_error:
            return CallToolResult(content=[TextContent(type="text", text=err)], is_error=True)
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(payload))],
                              structured_content=payload)

    return mcp.streamable_http_app()


def main() -> None:
    import sentry_sdk
    import uvicorn
    from sentry_sdk.integrations.mcp import MCPIntegration

    sentry_sdk.init(dsn=os.environ.get("SENTRY_DSN_PY") or None, traces_sample_rate=1.0,
                    send_default_pii=False, integrations=[MCPIntegration()])
    uvicorn.run(build_app(), host="127.0.0.1", port=int(os.environ.get("LAB_PORT", "8765")),
                log_level="warning")


if __name__ == "__main__":
    main()
