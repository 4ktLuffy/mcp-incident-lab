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

NOTES = {
    "none": "Everything worked. Two HTTP spans are still marked error: the MCP client closed two streams after a 200, which @sentry/node counts as a failure.",
    "tool_error": "The tool returned isError. Sentry's MCP span says ok. The only red span is the one our agent code marks by hand.",
    "stale_data": "The tool returned old data without any error. The agent's answer is wrong and every span Sentry recorded is ok.",
    "slow": "Visible only as the agent's timeout. The tool ran for 6 seconds and its span never reached Sentry; the server request is marked ok.",
}


def clean(desc: str | None) -> str:
    d = re.sub(r"http://127\.0\.0\.1:\d+", "", desc or "")
    return d.replace("/v1/chat/completions", "/v1/chat/completions (scripted model)")


def spans_for(rows: list[dict], scenario: dict) -> list[dict]:
    t0 = min(r["precise.start_ts"] for r in rows)
    out = []
    for r in sorted(rows, key=lambda r: r["precise.start_ts"]):
        if r["project.name"] == "node" and r["span.op"] == "http.server":
            continue  # the scripted model's server, not part of the system under test
        code = r.get("http.response.status_code")
        flag = None
        if r["span.op"] == "http.client" and r["span.status"] == "error" and code and 200 <= code < 300:
            flag = "false alarm: HTTP 200, the client closed the stream"
        elif r["span.op"] == "http.client" and r["span.status"] == "error":
            flag = "real: the request timed out"
        elif r["span.op"] == "mcp.server" and scenario["scenario"] == "tool_error":
            flag = "ok, although the tool returned isError"
        elif r["span.op"] == "gen_ai.execute_tool" and r["span.status"] == "error":
            flag = "marked error by our agent code, not by Sentry"
        elif r["span.op"] == "http.server" and scenario["scenario"] == "slow" and r["span.duration"] > 1000:
            flag = "ok, although the client had already given up"
        out.append({"start": round((r["precise.start_ts"] - t0) * 1000, 1), "dur": round(r["span.duration"], 1),
                    "op": r["span.op"], "desc": clean(r["span.description"]), "status": r["span.status"],
                    "side": "JS agent" if r["project.name"] == "node" else "Python MCP server", "flag": flag})
    if scenario["scenario"] == "slow" and not scenario["sentry"]["py_span_found"]:
        t = scenario["truth"]
        start = max((s["start"] for s in out if s["op"] == "http.server" and s["dur"] > 1000), default=0)
        out.append({"start": start, "dur": t["duration_ms"], "op": "mcp.server", "desc": "tools/call check_inventory",
                    "status": "missing", "side": "Python MCP server",
                    "flag": f"never reached Sentry (the tool really ran {t['duration_ms']:,.0f} ms)", "ghost": True})
        out.sort(key=lambda x: (x["start"], x.get("ghost", False)))
    return out


def main() -> None:
    report = json.loads((RUNS / "report.json").read_text())
    raw = json.loads((RUNS / "spans_raw.json").read_text())
    seen = {a["run_id"]: a.get("tool_result_seen") or {} for a in map(json.loads, (RUNS / "agent.jsonl").read_text().splitlines())}
    data = []
    for s in report["scenarios"]:
        t = s["truth"] or {}
        data.append({"id": s["scenario"], "title": s["title"], "verdict": s["verdict"], "correct": s["answer_correct"],
                     "answer": s["agent_answer"], "truth": s["what_happened"], "returned": t.get("returned"), "seen": seen.get(s["run_id"], {}),
                     "note": NOTES[s["scenario"]], "spans": spans_for(raw[s["scenario"]], s)})
    html = (ROOT / "report/demo_template.html").read_text().replace("/*DATA*/null", json.dumps(data))
    out = ROOT / "docs/index.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text(html)
    print(f"wrote {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
