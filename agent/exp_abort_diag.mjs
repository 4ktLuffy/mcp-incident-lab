// Print undici diagnostics_channel events for control vs cancelled body. No network beyond 127.0.0.1.
import dc from "node:diagnostics_channel";
import { createServer } from "node:http";
let tag = "";
for (const n of ["create","headers","trailers","error","bodyChunkReceived","bodySent"]) {
  dc.subscribe(`undici:request:${n}`, (m) => {
    if (n === "bodyChunkReceived") return;
    console.log(`  [${tag}] undici:request:${n}`, n==="error" ? `${m.error?.name}: ${m.error?.message}` : n==="headers" ? `status=${m.response?.statusCode}` : "");
  });
}
const srv = createServer((req, res) => {
  res.writeHead(200, {"content-type":"text/event-stream"}); res.write("data: 1\n\n");
  if (req.url === "/plain") res.end();
});
await new Promise(r => srv.listen(0,"127.0.0.1",r));
const base = `http://127.0.0.1:${srv.address().port}`;
tag="control"; { const r = await fetch(base+"/plain"); await r.text(); }
tag="reader.cancel"; { const r = await fetch(base+"/s"); const rd=r.body.getReader(); await rd.read(); await rd.cancel(); await new Promise(r=>setTimeout(r,100)); }
tag="abort"; { const ac=new AbortController(); const r = await fetch(base+"/s",{signal:ac.signal}); const rd=r.body.getReader(); await rd.read(); ac.abort(); await rd.read().catch(()=>{}); await new Promise(r=>setTimeout(r,100)); }
srv.closeAllConnections(); srv.close();
