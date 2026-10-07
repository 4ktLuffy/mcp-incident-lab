// SCRIPTED model: a tiny OpenAI-compatible server. No real LLM is involved.
// Turn 1 calls check_inventory. Turn 2 answers confidently from whatever the
// tool message holds; an error or missing result still becomes "out of stock".
import http from "node:http";

export function scriptedAnswer(sku, toolContent) {
  let units = 0;
  try {
    const o = JSON.parse(toolContent);
    if (o && typeof o.in_stock === "number") units = o.in_stock;
  } catch {}
  return units > 0 ? `SKU ${sku} is in stock (${units} units).` : `SKU ${sku} is out of stock.`;
}

export function startFakeLlm() {
  const server = http.createServer((req, res) => {
    let body = "";
    req.on("data", (c) => (body += c));
    req.on("end", () => {
      const msgs = JSON.parse(body || "{}").messages || [];
      const lastUser = [...msgs].reverse().find((m) => m.role === "user");
      const sku = (lastUser?.content || "").match(/SKU-\d+/)?.[0] || "UNKNOWN";
      const tool = [...msgs].reverse().find((m) => m.role === "tool");
      const base = { id: "scripted-" + Date.now(), object: "chat.completion",
        created: Math.floor(Date.now() / 1000), model: "scripted-model-v1" };
      let choice, usage;
      if (!tool) {
        choice = { index: 0, finish_reason: "tool_calls", message: { role: "assistant", content: null,
          tool_calls: [{ id: "call_1", type: "function",
            function: { name: "check_inventory", arguments: JSON.stringify({ sku }) } }] } };
        usage = { prompt_tokens: 312, completion_tokens: 24, total_tokens: 336 };
      } else {
        choice = { index: 0, finish_reason: "stop", message: { role: "assistant",
          content: scriptedAnswer(sku, typeof tool.content === "string" ? tool.content : "") } };
        usage = { prompt_tokens: 398, completion_tokens: 17, total_tokens: 415 };
      }
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify({ ...base, choices: [choice], usage }));
    });
  });
  return new Promise((resolve) =>
    server.listen(0, "127.0.0.1", () => resolve({ server, port: server.address().port })));
}
