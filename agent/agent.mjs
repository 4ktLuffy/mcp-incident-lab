// Inventory agent. The "model" is SCRIPTED (see fake_llm.mjs), not a real LLM.
// LAB_SENTRY_NODE points at a locally built @sentry/node (lab.py --with-fixes); default is the installed release.
const Sentry = await import(process.env.LAB_SENTRY_NODE || "@sentry/node");
import OpenAI from "openai";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { appendFileSync, mkdirSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { startFakeLlm } from "./fake_llm.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const arg = (n, d) => { const i = process.argv.indexOf("--" + n); return i > 0 ? process.argv[i + 1] : d; };
const runId = arg("run-id", "run-" + Date.now());
const fault = arg("fault", "none");
const sku = arg("sku", "SKU-1001");
const mcpUrl = arg("mcp-url", "http://127.0.0.1:8765/mcp");
const timeoutMs = Number(arg("timeout-ms", "2000"));
const runsDir = resolve(arg("runs-dir", resolve(here, "../runs")));

Sentry.init({
  dsn: process.env.SENTRY_DSN_JS || undefined,
  tracesSampleRate: 1,
  sendDefaultPii: false,
  integrations: [Sentry.openAIIntegration({ recordInputs: false, recordOutputs: false })],
});

const table = JSON.parse(readFileSync(resolve(here, "../server/inventory.json"), "utf8"));
const correctAnswer = (table[sku]?.in_stock ?? 0) > 0 ? "in stock" : "out of stock";

const { server: llmServer, port } = await startFakeLlm();
const openai = Sentry.instrumentOpenAiClient(
  new OpenAI({ apiKey: "scripted", baseURL: `http://127.0.0.1:${port}/v1` }),
  { recordInputs: false, recordOutputs: false });

const tools = [{ type: "function", function: { name: "check_inventory", description: "Units in stock for a SKU",
  parameters: { type: "object", properties: { sku: { type: "string" } }, required: ["sku"] } } }];

async function callTool(args) {
  // What a normal developer writes: error status if the call throws or the result says isError.
  return Sentry.startSpan({ op: "gen_ai.execute_tool", name: "execute_tool check_inventory",
    attributes: { "gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "check_inventory" } },
  async (span) => {
    const td = Sentry.getTraceData();
    const headers = { "x-lab-fault": fault, "x-lab-run-id": runId };
    if (td["sentry-trace"]) headers["sentry-trace"] = td["sentry-trace"];
    if (td.baggage) headers["baggage"] = td.baggage;
    const client = new Client({ name: "inventory-bot", version: "1.0.0" });
    try {
      await client.connect(new StreamableHTTPClientTransport(new URL(mcpUrl), { requestInit: { headers } }));
      const r = await client.callTool({ name: "check_inventory", arguments: args }, undefined, { timeout: timeoutMs });
      const text = (r.content || []).map((c) => c.text ?? "").join(" ");
      if (r.isError) span.setStatus({ code: 2, message: "tool returned isError" });
      return { text, isError: !!r.isError, threw: null };
    } catch (e) {
      span.setStatus({ code: 2, message: "tool call threw" });
      return { text: "", isError: true, threw: String(e.message || e) };
    } finally {
      client.close().catch(() => {});
    }
  });
}

let traceId = null, answer = "", seen = null;
await Sentry.startSpan({ op: "gen_ai.invoke_agent", name: "invoke_agent inventory-bot",
  attributes: { "gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": "inventory-bot" } },
async (span) => {
  traceId = span.spanContext().traceId;
  const messages = [{ role: "user", content: `Is ${sku} in stock?` }];
  const first = await openai.chat.completions.create({ model: "scripted-model-v1", messages, tools });
  const call = first.choices[0].message.tool_calls[0];
  seen = await callTool(JSON.parse(call.function.arguments));
  messages.push(first.choices[0].message,
    { role: "tool", tool_call_id: call.id, content: seen.text || (seen.threw ? "" : "") });
  const second = await openai.chat.completions.create({ model: "scripted-model-v1", messages, tools });
  answer = second.choices[0].message.content;
});

const correct = answer.includes(correctAnswer) && !(correctAnswer === "in stock" && answer.includes("out of stock"));
const row = { run_id: runId, fault, sku, trace_id: traceId, model: "scripted", final_answer: answer,
  correct_answer: `SKU ${sku} is ${correctAnswer}`, answer_correct: correct, tool_result_seen: seen };
mkdirSync(runsDir, { recursive: true });
appendFileSync(resolve(runsDir, "agent.jsonl"), JSON.stringify(row) + "\n");
console.log(JSON.stringify(row));

llmServer.close();
await Sentry.flush(5000);
await Sentry.close(2000);
process.exit(0);
