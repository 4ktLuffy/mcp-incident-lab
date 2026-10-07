import asyncio, json, sys, threading, time, urllib.request
import sentry_sdk, uvicorn
from sentry_sdk.transport import Transport
from sentry_sdk.integrations.mcp import MCPIntegration
from mcp.server.mcpserver import MCPServer

CLIENT_TIMEOUT = float(sys.argv[1])  # tool takes 3 s

class Print(Transport):
    def capture_envelope(self, env):
        for it in env.items:
            if it.type == "transaction":
                p = it.payload.json
                print("transaction", p["contexts"]["trace"]["status"], "spans:", [s["op"] for s in p["spans"]])

sentry_sdk.init(dsn="https://k@example.invalid/1", traces_sample_rate=1.0, transport=Print(),
                integrations=[MCPIntegration()])

mcp = MCPServer("inventory")

@mcp.tool()
async def check_inventory(sku: str) -> str:
    await asyncio.sleep(3)
    return "42"

server = uvicorn.Server(uvicorn.Config(mcp.streamable_http_app(), port=8799, log_level="error"))
threading.Thread(target=server.run, daemon=True).start()
while not server.started: time.sleep(0.05)

def post(body, headers, timeout=10):
    req = urllib.request.Request("http://127.0.0.1:8799/mcp", json.dumps(body).encode(), {
        "accept": "application/json, text/event-stream", "content-type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        r.read(); return r.headers

sid = post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18",
           "capabilities": {}, "clientInfo": {"name": "x", "version": "1"}}}, {}).get("mcp-session-id")
h = {"mcp-session-id": sid} if sid else {}
post({"jsonrpc": "2.0", "method": "notifications/initialized"}, h)
try:
    post({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
          "params": {"name": "check_inventory", "arguments": {"sku": "A"}}}, h, timeout=CLIENT_TIMEOUT)
    print("client got the result")
except TimeoutError:
    print(f"client gave up after {CLIENT_TIMEOUT}s")
time.sleep(5)          # the tool finishes on the server meanwhile
sentry_sdk.flush(2)
