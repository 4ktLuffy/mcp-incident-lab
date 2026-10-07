// Does @sentry/node mark a 200 fetch span as error when the client aborts a streamed body?
// Spans are captured locally via beforeSendTransaction; nothing leaves the machine.
import * as Sentry from "@sentry/node";
import { createServer } from "node:http";
const seen = [];
Sentry.init({ dsn: "http://lab@127.0.0.1:9/1", tracesSampleRate: 1,
  beforeSendSpan(s) { const a = s.attributes || s.data || {}; const op = a["sentry.op"]?.value ?? a["sentry.op"] ?? s.op;
    if (op === "http.client") seen.push(`${s.name ?? s.description} -> ${s.status} (http ${JSON.stringify(a["http.response.status_code"])})`); return s; } });
const srv = createServer((req, res) => {
  if (req.url === "/stream") { res.writeHead(200, { "content-type": "text/event-stream" }); res.write("data: 1\n\n"); return; } // never ends
  res.writeHead(200, { "content-type": "text/plain" }); res.end("ok");
});
await new Promise((r) => srv.listen(0, "127.0.0.1", r));
const base = `http://127.0.0.1:${srv.address().port}`;
await Sentry.startSpan({ name: "root", op: "test" }, async () => {
  const a = await fetch(base + "/plain"); await a.text();                       // control: full body read
  const ac = new AbortController();
  const b = await fetch(base + "/stream", { signal: ac.signal });               // stream, read one chunk, abort
  const rd = b.body.getReader(); await rd.read(); ac.abort(); await rd.read().catch(() => {});
  const c = await fetch(base + "/stream"); const rc = c.body.getReader(); await rc.read(); await rc.cancel(); // stream, reader.cancel()
});
await Sentry.flush(3000); srv.closeAllConnections(); srv.close();
console.log(seen.join("\n"));
