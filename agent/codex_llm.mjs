// REAL model, no API key: a tiny OpenAI-compatible server that asks the Codex CLI (`codex exec`)
// for each step. Codex runs read-only in an empty folder and is told to reply with one JSON object:
// a tool call or an answer. Token usage is what Codex reports for the call, which includes the
// Codex CLI's own instructions, so it is much larger than the prompt below.
import http from "node:http";
import { spawn } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

export const CODEX_MODEL = process.env.LAB_CODEX_MODEL || "gpt-5.6-sol";
const EMPTY_DIR = mkdtempSync(join(tmpdir(), "lab-codex-"));

function render(messages) {
  return messages.map((m) => {
    if (m.role === "tool") return `TOOL RESULT (check_inventory): ${m.content || "(empty)"}`;
    if (m.tool_calls) return `ASSISTANT called ${m.tool_calls.map((c) => `${c.function.name}(${c.function.arguments})`).join(", ")}`;
    return `${m.role.toUpperCase()}: ${m.content}`;
  }).join("\n");
}

export function promptFor(messages) {
  return `You are the language model inside a warehouse inventory assistant. You cannot run commands or read files; do not use any of your own tools.
The assistant has one tool: check_inventory(sku) -> units in stock for that SKU.

Conversation so far:
${render(messages)}

Decide the assistant's next step. Reply with ONLY one JSON object and nothing else, either
{"tool_call": {"name": "check_inventory", "arguments": {"sku": "<sku>"}}}
or
{"answer": "<your reply to the user>"}`;
}

export function parseReply(text) {
  const m = text.match(/\{[\s\S]*\}/);
  if (!m) return { answer: text.trim() };
  try { return JSON.parse(m[0]); } catch { return { answer: text.trim() }; }
}

function runCodex(prompt) {
  return new Promise((resolve, reject) => {
    const p = spawn("codex", ["exec", "--json", "--ephemeral", "--skip-git-repo-check", "-s", "read-only",
      "-m", CODEX_MODEL, "-c", "model_reasoning_effort=low", prompt], { cwd: EMPTY_DIR, stdio: ["ignore", "pipe", "ignore"] });
    let out = "";
    p.stdout.on("data", (c) => (out += c));
    p.on("error", reject);
    p.on("close", () => {
      let text = "", usage = null;
      for (const line of out.split("\n")) {
        let e; try { e = JSON.parse(line); } catch { continue; }
        if (e.type === "item.completed" && e.item?.type === "agent_message") text = e.item.text;
        if (e.type === "turn.completed") usage = e.usage;
      }
      text ? resolve({ text, usage }) : reject(new Error("codex returned no message"));
    });
  });
}

export function startCodexLlm() {
  const server = http.createServer((req, res) => {
    let body = "";
    req.on("data", (c) => (body += c));
    req.on("end", async () => {
      const messages = JSON.parse(body || "{}").messages || [];
      try {
        const { text, usage } = await runCodex(promptFor(messages));
        const reply = parseReply(text);
        const message = reply.tool_call
          ? { role: "assistant", content: null, tool_calls: [{ id: "call_" + Date.now(), type: "function",
              function: { name: reply.tool_call.name, arguments: JSON.stringify(reply.tool_call.arguments || {}) } }] }
          : { role: "assistant", content: String(reply.answer ?? text) };
        const u = usage ? { prompt_tokens: usage.input_tokens, completion_tokens: usage.output_tokens,
          total_tokens: usage.input_tokens + usage.output_tokens } : undefined;
        res.writeHead(200, { "content-type": "application/json" });
        res.end(JSON.stringify({ id: "codex-" + Date.now(), object: "chat.completion", created: Math.floor(Date.now() / 1000),
          model: CODEX_MODEL, choices: [{ index: 0, finish_reason: reply.tool_call ? "tool_calls" : "stop", message }],
          ...(u ? { usage: u } : {}) }));
      } catch (e) {
        res.writeHead(500, { "content-type": "application/json" });
        res.end(JSON.stringify({ error: { message: String(e.message || e) } }));
      }
    });
  });
  return new Promise((resolve) =>
    server.listen(0, "127.0.0.1", () => resolve({ server, port: server.address().port })));
}
