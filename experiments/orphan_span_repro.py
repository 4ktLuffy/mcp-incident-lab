"""Plain ASGI repro: handler outlives a disconnected client. No MCP, no network, no real Sentry.
usage: python orphan_span_repro.py [static|stream] [disconnect|wait]
"""
import asyncio, json, sys, time
import sentry_sdk
from sentry_sdk.transport import Transport
from sentry_sdk.integrations.asgi import SentryAsgiMiddleware

mode, client = sys.argv[1], sys.argv[2]
captured = []

class Capture(Transport):
    def capture_envelope(self, envelope):
        for item in envelope.items:
            p = item.payload.json
            if item.type == "transaction":
                captured.append(("transaction", p["transaction"], p["contexts"]["trace"].get("status"),
                                 [s["description"] for s in p["spans"]], p.get("_dropped_spans")))
            elif item.type == "span":
                for s in p["items"]:
                    captured.append(("span_v2", s["name"], s.get("status"), s.get("is_segment")))
            else:
                captured.append((item.type,))

kw = {"trace_lifecycle": "stream"} if mode == "stream" else {}
sentry_sdk.init(dsn="https://k@example.invalid/1", traces_sample_rate=1.0, transport=Capture(), **kw)

TOOL_SECONDS = 1.0
async def tool():
    # what MCPIntegration does: start a span (child of the current span/transaction) and run the work inside it
    start = sentry_sdk.traces.start_span if mode == "stream" else sentry_sdk.start_span
    kwargs = {"name": "tools/call check_inventory"} if mode == "stream" else {"op": "mcp.server", "name": "tools/call check_inventory"}
    with start(**kwargs):
        await asyncio.sleep(TOOL_SECONDS)

async def app(scope, receive, send):
    # response headers go out immediately (like SSE), as with MCP streamable HTTP
    await send({"type": "http.response.start", "status": 200, "headers": []})
    t = asyncio.create_task(tool())            # copies contextvars -> parent = the http.server transaction
    if client == "wait":
        await t
        await send({"type": "http.response.body", "body": b"done"})
    else:
        while (await receive())["type"] != "http.disconnect":   # Starlette's listen_for_disconnect
            pass
        # handler returns; tool task keeps running (as the MCP session task group does)
    scope["_task"] = t

async def main():
    q = asyncio.Queue()
    scope = {"type": "http", "method": "POST", "path": "/mcp", "headers": [], "query_string": b"",
             "server": ("x", 80), "scheme": "http", "http_version": "1.1"}
    async def receive(): return await q.get()
    async def send(e): pass
    mw = SentryAsgiMiddleware(app)
    run = asyncio.create_task(mw(scope, receive, send))
    if client == "disconnect":
        await asyncio.sleep(0.2); await q.put({"type": "http.disconnect"})
    await run
    print(f"[{mode}/{client}] t={time.perf_counter()-T0:.2f}s transaction handler returned; tool still running: {not scope['_task'].done()}")
    await scope["_task"]
    sentry_sdk.flush(2)
    await asyncio.sleep(0.2)
    print(f"[{mode}/{client}] after tool finished, envelopes captured:")
    for c in captured: print("   ", json.dumps(c, default=str))

T0 = time.perf_counter()
asyncio.run(main())
