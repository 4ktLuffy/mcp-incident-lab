"""Real lab MCP server (server/server.py build_app) in-process, real uvicorn, raw-HTTP client that
disconnects after CLIENT_TIMEOUT. Envelopes captured by a custom transport; nothing leaves the process.
usage: python mcp_disconnect_inproc.py <client_timeout_s> [static|stream]
"""
import json, os, sys, threading, time
from pathlib import Path
os.environ["LAB_SLOW_SECONDS"] = "3"; os.environ["LAB_RUNS_DIR"] = "/tmp/lab_exp_runs"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import httpx2 as httpx, uvicorn, sentry_sdk
from sentry_sdk.transport import Transport
from sentry_sdk.integrations.mcp import MCPIntegration
from server import server as srv

timeout, mode = float(sys.argv[1]), (sys.argv[2] if len(sys.argv) > 2 else "static")
cap = []
class Capture(Transport):
    def capture_envelope(self, env):
        for it in env.items:
            p = it.payload.json
            if it.type == "transaction":
                cap.append(("transaction", p["transaction"], p["contexts"]["trace"].get("status"),
                            [(s["op"], s["description"]) for s in p["spans"]]))
            elif it.type == "span":
                for s in p["items"]:
                    cap.append(("span_v2", s["name"], s.get("status"), "segment" if s.get("is_segment") else "child",
                                round((s["end_timestamp"] - s["start_timestamp"]) * 1000)))
kw = {"trace_lifecycle": "stream"} if mode == "stream" else {}
sentry_sdk.init(dsn="https://k@example.invalid/1", traces_sample_rate=1.0, transport=Capture(),
                integrations=[MCPIntegration()], **kw)
app = srv.build_app()
cfg = uvicorn.Config(app, host="127.0.0.1", port=8799, log_level="error")
server = uvicorn.Server(cfg)
threading.Thread(target=server.run, daemon=True).start()
while not server.started: time.sleep(0.05)

H = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
url = "http://127.0.0.1:8799/mcp"
with httpx.Client(timeout=10) as c:
    r = c.post(url, headers=H, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "x", "version": "1"}}})
    sid = r.headers.get("mcp-session-id"); H2 = dict(H, **({"mcp-session-id": sid} if sid else {}))
    c.post(url, headers=H2, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
cap.clear()
call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "check_inventory", "arguments": {"sku": "SKU-1001"}}}
t0 = time.time()
try:
    with httpx.Client(timeout=timeout) as c:
        c.post(url, headers=dict(H2, **{"x-lab-fault": "slow"}), json=call)
    print("client got a response")
except httpx.TimeoutException:
    print(f"client timed out/disconnected at {time.time()-t0:.1f}s")
time.sleep(5); sentry_sdk.flush(2); time.sleep(0.3)
print(f"[timeout={timeout}s mode={mode}] tool sleeps 3s; envelopes:")
for x in cap: print("   ", json.dumps(x))
