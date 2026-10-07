# MCP Incident Lab

A small lab that shows how a broken MCP tool turns into a confident wrong answer from an agent, and what Sentry does and doesn't show about it.

An inventory agent asks an MCP server whether a SKU is in stock. The SKU really is in stock (42 units). We break the tool in three ways and keep our own log of what the server actually did, written by the server code and not by the Sentry SDK. Then we compare that log with the answer the agent gave and with what Sentry recorded.

## The four scenarios

1. **Healthy control.** Tool returns 42. Agent says in stock.
2. **Tool error.** Tool returns an MCP error result (`isError`, "warehouse DB timeout"). Agent still says "out of stock".
3. **Stale data.** Tool quietly returns 0 from a stale cache. No error anywhere. Agent says "out of stock".
4. **Slow tool.** Tool sleeps 6 s, the client gives up after 2 s. Agent says "out of stock".

Each run gets a verdict, judged only on what Sentry's own instrumentation recorded. The span our agent code marks by hand doesn't count, and neither do HTTP spans marked error on a 2xx response (see below).

- **healthy:** right answer.
- **visible:** Sentry marked a real span or event as an error.
- **missing:** the tool ran on the server, but its span never reached Sentry.
- **misleading:** the tool really failed, but Sentry recorded it as ok.
- **invisible:** wrong answer, the tool didn't fail by its own account, everything is green.

## What we saw (Sentry Python SDK 2.71.0, @sentry/node 11.4.0, mcp 2.3.0)

| Scenario | Agent answer | Sentry's MCP tool span | Verdict |
|---|---|---|---|
| Healthy | right | ok | healthy |
| Tool error | wrong | **ok**, although the tool returned `isError` | misleading |
| Stale data | wrong | ok | invisible |
| Slow tool | wrong | **missing** | visible only as the client's timed-out request |

Three things we checked with controls:

1. **The tool error shows as ok.** `MCPIntegration` only marks exceptions that escape the handler, and an `isError` result is returned, not raised. The trace shows a failure only because our agent code marks its own span by hand.
2. **The slow tool's span is dropped.** The client gives up at 2 s and the HTTP request on the server closes as ok at 2 s. The tool keeps running for 6 s and its `mcp.server` span never reaches Sentry, even when the server is left running 20 s longer and stopped gracefully. Control: with a 10 s client timeout the same 6 s call shows up (`mcp.server`, 6,002 ms). So the slowest tool calls are the ones you can't see.
3. **Healthy runs have two error spans.** @sentry/node marks a fetch span as error when the server answered 200 but the client closed the streamed body early. The MCP client does that on every normal session (the GET event stream and a streamed POST). `agent/exp_abort_status.mjs` reproduces it without MCP: a 200 response read fully is ok; the same 200 response cancelled mid-stream is error, both with `reader.cancel()` and with `AbortController`. Nothing leaves the machine.

## How to run

```
.venv/bin/python lab.py                  # offline, Sentry column says "not read back"
.venv/bin/python lab.py --offline-dsn    # same, with a dummy local DSN so trace headers propagate
.venv/bin/python lab.py --readback       # also pull the traces back from Sentry
.venv/bin/python report/build.py         # writes report/index.html
.venv/bin/python -m pytest tests
```

Env vars, read at runtime only: `SENTRY_DSN_PY`, `SENTRY_DSN_JS` (to send), and for `--readback` also `SENTRY_AUTH_TOKEN`, `SENTRY_ORG`, `SENTRY_REGION_URL`. `--readback` waits 45 s by default (`--wait`) and then polls each trace. Output goes to `runs/`: `truth.jsonl` (server), `agent.jsonl` (agent), `report.json`.

## How it is wired

- `server/server.py`: Python MCP server (`MCPServer`, which is what FastMCP became in mcp 2.x) over streamable HTTP, with `MCPIntegration`. The fault comes from the `x-lab-fault` header per request, read from the request context, so no restart is needed.
- `agent/agent.mjs`: JS agent. It forwards `sentry-trace` and `baggage` so the Python spans land in the same trace.
- The JS side marks its `gen_ai.execute_tool` span as error when the MCP result has `isError` or the call throws. That is what a normal developer would write, and it matters: without it the tool-error run would be all green.
- `labcore.py` holds the join and verdict logic; `lab.py` runs everything.

## What the integration does (read from the SDK source)

`MCPIntegration` wraps tool calls in an `mcp.server` span and captures exceptions that escape the handler. A tool that returns an error result does not raise, so that span is not marked as failed. The live runs above confirm this.

## Limits

- The model is scripted. It is not an LLM, and its "confident wrong answer" is written in, though it copies a common real behaviour.
- One transport (streamable HTTP), one tool, one SKU, one run per scenario.
- With no DSN set, the JS SDK does not emit trace headers, so propagation can only be checked with a DSN set (`--offline-dsn` uses a local dummy).
- In the slow scenario the server keeps working after the client leaves, so the truth log shows a result the agent never saw.
