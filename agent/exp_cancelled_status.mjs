import * as Sentry from "@sentry/node";
Sentry.init({ dsn: "http://lab@127.0.0.1:9/1", tracesSampleRate: 1 });
Sentry.startSpan({name:"root"}, () => {
  for (const [label, st] of [["error+aborted msg",{code:2,message:"This operation was aborted"}],["error+'cancelled'",{code:2,message:"cancelled"}],["unset",{code:0}]]) {
    const s = Sentry.startInactiveSpan({name: label}); s.setStatus(st); s.end(); console.log(label.padEnd(22), "->", Sentry.spanToJSON(s).status);
  }
});
