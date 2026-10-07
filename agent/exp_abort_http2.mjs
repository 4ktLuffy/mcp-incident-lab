// Unconfirmed lead, not reported: with node:http, a client-side res.destroy() mid-body seems to leave the
// http.client span unended (never sent). Prints spanEnd events for a full read vs a destroyed response.
import * as Sentry from "@sentry/node";
import { createServer, request } from "node:http";
const spanToJSON = Sentry.spanToJSON;
Sentry.init({ dsn: "http://lab@127.0.0.1:9/1", tracesSampleRate: 1 });
const c = Sentry.getClient();
c.on("spanEnd", s => { const j = Sentry.spanToJSON(s); console.log("spanEnd", s.constructor.name, JSON.stringify(j).slice(0,300)); });
const srv = createServer((req,res)=>{ res.writeHead(200); res.write("x"); if(req.url==="/plain") res.end(); });
await new Promise(r=>srv.listen(0,"127.0.0.1",r)); const port=srv.address().port;
const get=(path,mode)=>new Promise(res=>{ const q=request({host:"127.0.0.1",port,path},r=>{
  if(mode==="full"){ r.on("data",()=>{}); r.on("end",res); } else r.once("data",()=>{ r.destroy(); setTimeout(res,300); }); }); q.on("error",()=>{}); q.end(); });
await Sentry.startSpan({name:"root"}, async()=>{ console.log("-- full"); await get("/plain","full"); console.log("-- res.destroy()"); await get("/s","destroy"); console.log("-- done"); });
srv.closeAllConnections(); srv.close(); await Sentry.flush(1000);
