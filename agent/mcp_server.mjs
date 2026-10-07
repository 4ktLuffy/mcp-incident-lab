// JS twin of server/server.py: same tool, same faults, same truth log. Lives in agent/ so the bare imports
// resolve against agent/node_modules. Started by `lab.py --server js`.
// LAB_SENTRY_NODE points at a locally built @sentry/node (lab.py --with-fixes); default is the installed release.
// LAB_PRINT_SPANS=1 prints the mcp.server spans this process would send (local capture, no network).
const Sentry = await import(process.env.LAB_SENTRY_NODE || "@sentry/node");
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { isInitializeRequest } from "@modelcontextprotocol/sdk/types.js";
import { appendFileSync, mkdirSync, readFileSync } from "node:fs";
import { createServer } from "node:http";
import { randomUUID } from "node:crypto";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { z } from "zod";

const here = dirname(fileURLToPath(import.meta.url));
const INVENTORY = JSON.parse(readFileSync(resolve(here, "../server/inventory.json"), "utf8"));
const FAULTS = new Set(["tool_error", "stale_data", "slow"]);
const SLOW_SECONDS = Number(process.env.LAB_SLOW_SECONDS || "6");
const runsDir = resolve(process.env.LAB_RUNS_DIR || resolve(here, "../runs"));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const printSpans = !!process.env.LAB_PRINT_SPANS;
Sentry.init({
  dsn: process.env.SENTRY_DSN_JS || undefined,
  tracesSampleRate: 1,
  sendDefaultPii: !!process.env.LAB_PY_RECORD_OUTPUTS,
  release: "mcp-incident-lab-js-server",
  beforeSendSpan: printSpans ? (s) => {
    const a = s.attributes || s.data || {};
    const op = a["sentry.op"]?.value ?? a["sentry.op"] ?? s.op;
    if (String(op).startsWith("mcp.server")) console.log(`SPAN op=${op} name=${s.name ?? s.description} status=${JSON.stringify(s.status)}`);
    return s;
  } : undefined,
});

function appendTruth(row) {
  mkdirSync(runsDir, { recursive: true });
  appendFileSync(resolve(runsDir, "truth.jsonl"), JSON.stringify(row) + "\n");
}

function lookup(sku) {
  const row = INVENTORY[sku] ?? { in_stock: 0, warehouse: "unknown" };
  return { sku, in_stock: row.in_stock, warehouse: row.warehouse };
}

/** Do the work for one call and log truth. Returns {payload, isError, err}. */
async function runCheck(sku, fault, runId, sentryTrace) {
  const t0 = performance.now();
  let payload = null, isError = false, err = null, returned = null;
  try {
    if (fault === "tool_error") {
      isError = true; err = "warehouse DB timeout";
      returned = { isError: true, text: err };
    } else if (fault === "stale_data") {
      // Stale cache: claims zero stock, no error anywhere.
      payload = { ...lookup(sku), in_stock: 0 };
      returned = payload;
    } else if (fault === "slow") {
      await sleep(SLOW_SECONDS * 1000);
      payload = returned = lookup(sku);
    } else {
      payload = returned = lookup(sku);
    }
    return { payload, isError, err };
  } finally {
    appendTruth({
      run_id: runId, fault: fault || "none", args: { sku }, returned, is_error: isError,
      duration_ms: Math.round((performance.now() - t0) * 10) / 10, sentry_trace: sentryTrace,
    });
  }
}

function buildServer() {
  const mcp = Sentry.wrapMcpServerWithSentry(new McpServer({ name: "inventory", version: "1.0.0" }),
    { recordInputs: !!process.env.LAB_PY_RECORD_OUTPUTS, recordOutputs: !!process.env.LAB_PY_RECORD_OUTPUTS });
  mcp.registerTool("check_inventory", { description: "Look up units in stock for a SKU.", inputSchema: { sku: z.string() } },
    async ({ sku }, extra) => {
      const h = extra.requestInfo?.headers ?? {};
      const fault = FAULTS.has(h["x-lab-fault"]) ? h["x-lab-fault"] : null;
      const { payload, isError, err } = await runCheck(sku, fault, h["x-lab-run-id"] ?? null, h["sentry-trace"] ?? null);
      if (isError) return { content: [{ type: "text", text: err }], isError: true };
      return { content: [{ type: "text", text: JSON.stringify(payload) }], structuredContent: payload };
    });
  return mcp;
}

const sessions = new Map(); // session id -> transport (stateful, like the Python server)

async function readBody(req) {
  const chunks = [];
  for await (const c of req) chunks.push(c);
  return chunks.length ? JSON.parse(Buffer.concat(chunks).toString()) : undefined;
}

const httpServer = createServer(async (req, res) => {
  try {
    if (!req.url.startsWith("/mcp")) { res.writeHead(404).end(); return; }
    const body = req.method === "POST" ? await readBody(req) : undefined;
    const sid = req.headers["mcp-session-id"];
    let transport = sid ? sessions.get(sid) : undefined;
    if (!transport && !sid && body && isInitializeRequest(body)) {
      transport = new StreamableHTTPServerTransport({
        sessionIdGenerator: () => randomUUID(),
        onsessioninitialized: (id) => sessions.set(id, transport),
      });
      transport.onclose = () => { if (transport.sessionId) sessions.delete(transport.sessionId); };
      await buildServer().connect(transport);
    }
    if (!transport) { res.writeHead(400, { "content-type": "application/json" })
      .end(JSON.stringify({ jsonrpc: "2.0", error: { code: -32000, message: "no valid session" }, id: null })); return; }
    await transport.handleRequest(req, res, body);
  } catch (e) {
    if (!res.headersSent) res.writeHead(500);
    res.end();
  }
});

httpServer.listen(Number(process.env.LAB_PORT || "8765"), "127.0.0.1");

let stopping = false;
async function shutdown() {
  if (stopping) return;
  stopping = true;
  httpServer.closeAllConnections();
  httpServer.close();
  await Sentry.close(3000);
  process.exit(0);
}
process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);
