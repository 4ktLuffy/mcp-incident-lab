"""Build docs/index.html: a clickable replay of one real lab run.

Inputs: runs/report.json (lab.py --readback) and runs/spans_raw.json (every span of each trace,
read back from Sentry by lab.py --readback). Ports, ids and trace ids are dropped; durations and statuses are kept as
Sentry returned them. The scripted model's own HTTP server spans are left out (they exist only
because the fake LLM runs inside the agent process).

    .venv/bin/python report/demo.py
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "runs"
JS_SERVER = "mcp-incident-lab-js-server"  # release set by agent/mcp_server.mjs

STDIO_NOTES = {
    "none": "Over stdio the tool's span lands in its own trace. Sentry's Python MCP integration doesn't read trace context from the MCP request (a known gap, getsentry/sentry-python#5205), so nothing links it to the agent.",
    "tool_error": "The tool's span is in a separate trace, and its status is unknown, not error (getsentry/sentry-python#7890).",
    "stale_data": "The tool's span is in a separate trace, and nothing in it shows the data was stale.",
    "slow": "Over stdio the client's close cancels the tool on the server, and that span is recorded as internal_error. So the slow call is visible here, but in a trace nothing links to the agent.",
}

CODEX_NOTES = {
    "healthy": "A real model (gpt-5.6-sol through the Codex CLI) answers correctly.",
    "tool_error": "The real model retried, then told the user it couldn't check. An honest answer, but in Sentry every failed tool call is still recorded as ok (getsentry/sentry-python#7890).",
    "slow": "The real model retried, then said it couldn't check: two timeouts, about 4 seconds gone. Neither tool span reached Sentry (getsentry/sentry-python#7916).",
    "stale_data": "Even a real model answers confidently wrong here: the tool said 0 with no error, so there was nothing to doubt. Nothing in Sentry shows it either.",
}

JS_NOTES = {
    "none": "Healthy. One false alarm left: the agent's GET event stream, the @sentry/node bug fixed in getsentry/sentry-javascript#25129.",
    "tool_error": "Sentry's JS MCP integration marks the isError result as an error. The Python integration records the same result as ok (getsentry/sentry-python#7890).",
    "stale_data": "Same as with the Python server: nothing in the trace shows the data was stale unless tool results are recorded.",
    "slow": "The tool's span arrives with its real duration, because @sentry/node sends spans as they finish. The Python SDK drops it unless span streaming is on (getsentry/sentry-python#7916). It is still marked ok although the client had given up.",
}

FIXED_LABELS = {"none": "healthy", "tool_error": "misleading", "stale_data": "catchable", "slow": "visible"}

FIXED_NOTES = {
    "none": "No false alarms: with the @sentry/node fix (getsentry/sentry-javascript#25129) the closed streams are no longer errors.",
    "tool_error": "Still recorded as ok. This one needs a fix in sentry-python's MCP integration (getsentry/sentry-python#7890), which isn't written yet.",
    "stale_data": "Now catchable: with tool results recorded, the span shows in_stock: 0 for a SKU that has {units}. A check on the tool output, or a person, can see it.",
    "slow": "The tool's span arrives (span streaming), with its real duration and the result the agent never got.",
}

NOTES = {
    "none": "Everything worked. Two HTTP spans are still marked error: the MCP client closed two streams after a 200, which @sentry/node counts as a failure.",
    "tool_error": "The tool returned isError. Sentry's MCP span says ok. The only red span is the one our agent code marks by hand.",
    "stale_data": "The tool returned old data without any error. The agent's answer is wrong and every span Sentry recorded is ok.",
    "slow": "The tool ran for 6 seconds and its span never reached Sentry. The server request is marked ok, and the only red HTTP spans are requests the client closed itself.",
}


def clean(desc: str | None) -> str:
    d = re.sub(r"http://127\.0\.0\.1:\d+", "", desc or "")
    return d.replace("/v1/chat/completions", "/v1/chat/completions (scripted model)")


LUCKY = ("The tool broke, but {sku} really has 0 in stock, so the answer happens to be right. A check on the "
         "agent's answers alone would never notice, yet the trace looks exactly like the in-stock SKUs.")
CONTROL_ZERO = ("Control: {sku} really has 0 in stock and the agent says so. The scripted model follows the tool "
                "result; it isn't hardcoded to say out of stock.")


def note_for(s: dict, units: int, fixed: bool, js: bool = False, codex: bool = False, stdio: bool = False) -> str:
    if stdio and s["verdict"] != "lucky" and not (s["scenario"] == "none" and units == 0):
        return STDIO_NOTES[s["scenario"]]
    if codex and s["verdict"] != "lucky" and not (s["scenario"] == "none" and units == 0):
        return CODEX_NOTES["healthy" if s["scenario"] == "none" else s["scenario"]]
    if s["verdict"] == "lucky":
        return LUCKY.format(sku=s["sku"])
    if s["scenario"] == "none" and units == 0:
        return CONTROL_ZERO.format(sku=s["sku"])
    return (JS_NOTES if js else FIXED_NOTES if fixed else NOTES)[s["scenario"]].format(units=units)


def spans_for(rows: list[dict], scenario: dict, fixed: bool = False, js: bool = False) -> list[dict]:
    t0 = min(r["precise.start_ts"] for r in rows)
    out = []
    for r in sorted(rows, key=lambda r: r["precise.start_ts"]):
        if r["project.name"] == "node" and r["span.op"] == "http.server" and r.get("release") != JS_SERVER:
            continue  # the scripted model's server, not part of the system under test
        code = r.get("http.response.status_code")
        flag = None
        if r.get("separate_trace") and r["span.op"] == "mcp.server":
            flag = f"in a separate trace: nothing links it to the agent (status {r['span.status']})"
        elif r["span.op"] == "http.client" and r["span.status"] == "error" and code and 200 <= code < 300:
            flag = "false alarm: HTTP 200, the client closed the stream"
        elif r["span.op"] == "http.client" and r["span.status"] == "error":
            flag = "false alarm: no response yet, the client closed the request (the MCP cancel notice)"
        elif r["span.op"] == "mcp.server" and not str(r["span.description"]).startswith("tools/call"):
            flag = None
        elif r["span.op"] == "mcp.server" and js and scenario["scenario"] == "tool_error":
            flag = "error: the JS integration reads isError"
        elif r["span.op"] == "mcp.server" and js and scenario["scenario"] == "slow":
            flag = "arrived (sent as it finished), but ok although the client had given up"
        elif r["span.op"] == "mcp.server" and scenario["scenario"] == "tool_error":
            flag = "still ok, although the tool returned isError (#7890, not fixed yet)" if fixed else "ok, although the tool returned isError"
        elif r["span.op"] == "mcp.server" and fixed and r.get("mcp.tool.result.content"):
            flag = f"tool result recorded: {r['mcp.tool.result.content']}"
        elif r["span.op"] == "gen_ai.execute_tool" and r["span.status"] == "error":
            flag = "marked error by our agent code, not by Sentry"
        elif r["span.op"] == "http.server" and scenario["scenario"] == "slow" and r["span.duration"] > 1000:
            flag = "ok, although the client had already given up"
        out.append({"start": round((r["precise.start_ts"] - t0) * 1000, 1), "dur": round(r["span.duration"], 1),
                    "op": r["span.op"], "desc": clean(r["span.description"]), "status": r["span.status"],
                    "side": ("JS MCP server" if r.get("release") == JS_SERVER
                             else "JS agent" if r["project.name"] == "node" else "Python MCP server"), "flag": flag})
    if scenario["scenario"] == "slow" and not scenario["sentry"]["py_span_found"]:
        t = scenario["truth"]
        start = max((s["start"] for s in out if s["op"] == "http.server" and s["dur"] > 1000), default=0)
        out.append({"start": start, "dur": t["duration_ms"], "op": "mcp.server", "desc": "tools/call check_inventory",
                    "status": "missing", "side": "Python MCP server",
                    "flag": f"never reached Sentry (the tool really ran {t['duration_ms']:,.0f} ms)", "ghost": True})
        out.sort(key=lambda x: (x["start"], x.get("ghost", False)))
    return out


def load(runs: Path, fixed: bool, js: bool = False, codex: bool = False, stdio: bool = False) -> list[dict]:
    calls: dict[str, int] = {}
    for t in map(json.loads, (runs / "truth.jsonl").read_text().splitlines()):
        calls[t["run_id"]] = calls.get(t["run_id"], 0) + 1
    report = json.loads((runs / "report.json").read_text())
    raw = json.loads((runs / "spans_raw.json").read_text())
    seen = {a["run_id"]: a.get("tool_result_seen") or {} for a in map(json.loads, (runs / "agent.jsonl").read_text().splitlines())}
    stock = {k: v["in_stock"] for k, v in json.loads((ROOT / "server/inventory.json").read_text()).items()}
    data = []
    for s in report["scenarios"]:
        t = s["truth"] or {}
        units = stock.get(s["sku"], 0)
        verdict = s["verdict"] if (not fixed or js or s["verdict"] == "lucky") else FIXED_LABELS[s["scenario"]]
        data.append({"id": s["scenario"], "sku": s["sku"], "units": units, "title": s["title"], "verdict": verdict, "correct": s["answer_correct"],
                     "answer": s["agent_answer"], "truth": s["what_happened"], "returned": t.get("returned"), "seen": seen.get(s["run_id"], {}),
                     "note": note_for(s, units, fixed, js, codex, stdio), "kind": s.get("answer_kind") or ("right" if s["answer_correct"] else "wrong"),
                     "model": "gpt-5.6-sol via Codex CLI" if codex else "scripted model", "calls": calls.get(s["run_id"], 0),
                     "spans": spans_for(raw[f'{s["sku"]}/{s["scenario"]}'], s, fixed, js)})
    return data


def main() -> None:
    data = {"today": load(RUNS, False)}
    if (ROOT / "runs-fixed/spans_raw.json").exists():
        data["fixed"] = load(ROOT / "runs-fixed", True)
    if (ROOT / "runs-js/spans_raw.json").exists():
        data["js"] = load(ROOT / "runs-js", False, js=True)
    if (ROOT / "runs-stdio/spans_raw.json").exists():
        data["stdio"] = load(ROOT / "runs-stdio", False, stdio=True)
    if (ROOT / "runs-codex/spans_raw.json").exists():
        data["codex"] = load(ROOT / "runs-codex", False, codex=True)
    html = (ROOT / "report/demo_template.html").read_text().replace("/*DATA*/null", json.dumps(data))
    out = ROOT / "docs/index.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text(html)
    print(f"wrote {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
