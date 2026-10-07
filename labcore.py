"""Pure logic for the lab: joining ground truth with agent logs, summarising a
Sentry trace, and deciding the verdict. No network, no Sentry SDK."""
from __future__ import annotations

import json
from pathlib import Path

OK_STATUSES = {None, "", "ok", "unknown", "cancelled"}
SCENARIOS = ["none", "tool_error", "stale_data", "slow"]
TITLES = {"none": "Healthy control", "tool_error": "Tool error", "stale_data": "Stale data", "slow": "Slow tool"}


def read_jsonl(path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


def trace_id_of(sentry_trace: str | None) -> str | None:
    return sentry_trace.split("-")[0] if sentry_trace else None


def join_runs(truth: list[dict], agent: list[dict]) -> list[dict]:
    t = {r["run_id"]: r for r in truth}
    out = []
    for a in agent:
        tr = t.get(a["run_id"])
        out.append({"run_id": a["run_id"], "fault": a["fault"], "agent": a, "truth": tr,
                    "propagated": bool(tr and trace_id_of(tr.get("sentry_trace")) == a.get("trace_id"))})
    return out


def what_happened(fault: str, truth: dict | None, agent: dict, real_units: int, timeout_ms: int) -> str:
    if truth is None:
        return "server never logged the call"
    if fault == "tool_error":
        return f"tool returned isError: {(truth.get('returned') or {}).get('text')}"
    if fault == "stale_data":
        got = (truth.get("returned") or {}).get("in_stock")
        return f"tool returned in_stock={got} from a stale cache; real stock is {real_units}; no error raised"
    if fault == "slow":
        return f"tool took {truth['duration_ms']:.0f} ms; client gave up after {timeout_ms} ms"
    return f"tool returned in_stock={(truth.get('returned') or {}).get('in_stock')}, correct"


def summarize_spans(rows: list[dict], error_events: int = 0) -> dict:
    """Reduce Sentry span rows for one trace to the few facts the report needs.

    The MCP integration's own span (mcp.server) is kept apart from the execute_tool span our
    agent code creates and marks by hand, so the verdict only credits what Sentry recorded.
    HTTP client spans marked error only because the client closed them (2xx or no response yet) are
    counted as noise.
    """
    # the tool call's span (the JS integration also records initialize as mcp.server)
    py = next((r for r in rows if r.get("span.op") == "mcp.server"
               and str(r.get("span.description") or "").startswith("tools/call")), None)
    js = next((r for r in rows if r.get("span.op") == "gen_ai.execute_tool"), None)
    bad = [r for r in rows if r.get("span.status") not in OK_STATUSES]
    # http.client spans marked error although nothing failed: a 2xx response whose stream the client
    # closed, or a request the client aborted on close() before any response (no status code at all).
    noise = [r for r in bad if r.get("span.op") == "http.client"
             and (not r.get("http.response.status_code") or 200 <= r["http.response.status_code"] < 300)]
    sdk_bad = [r for r in bad if r not in noise and r is not js]
    return {
        "spans": len(rows),
        "joined": py is not None and js is not None,
        "py_span_found": py is not None,
        "py_status": py.get("span.status") if py else None,
        "py_duration_ms": py.get("span.duration") if py else None,
        "js_tool_status": js.get("span.status") if js else None,
        "error_spans": [f"{r.get('span.op')}:{r.get('span.status')}" for r in sdk_bad],
        "false_http_errors": len(noise),
        "error_events": error_events,
    }


def verdict(answer_correct: bool, truth_failed: bool, sentry: dict | None, broken: bool = False) -> str:
    """healthy | lucky | visible | missing | misleading | invisible | not read back.

    lucky: the tool was broken but the answer is right anyway (e.g. the SKU really is out of stock).

    Judged on Sentry's own instrumentation only (not the span our agent code marks by hand,
    and not 2xx HTTP spans marked error):
    visible: wrong answer and Sentry marked the tool span (or another real span/event) as error.
    missing: the tool ran on the server but its span never reached Sentry.
    misleading: the tool really failed, yet Sentry recorded it as ok.
    invisible: wrong answer, the tool did not fail by its own account, everything is green.
    """
    if answer_correct:
        return "lucky" if broken else "healthy"
    if sentry is None:
        return "not read back"
    if sentry["error_spans"] or sentry["error_events"]:
        return "visible"
    if not sentry["py_span_found"]:
        return "missing"
    return "misleading" if truth_failed else "invisible"


def truth_failed(truth: dict | None, agent: dict) -> bool:
    return bool(truth and truth.get("is_error")) or bool((agent.get("tool_result_seen") or {}).get("threw"))


def build_report(joined: list[dict], sentry_by_run: dict | None, real_units: dict[str, int] | int,
                 timeout_ms: int) -> list[dict]:
    rows = []
    for j in joined:
        a, t = j["agent"], j["truth"]
        s = (sentry_by_run or {}).get(j["run_id"]) if sentry_by_run is not None else None
        sku = a.get("sku")
        units = real_units.get(sku, 0) if isinstance(real_units, dict) else real_units
        rows.append({
            "scenario": j["fault"], "title": TITLES.get(j["fault"], j["fault"]), "run_id": j["run_id"],
            "trace_id": a.get("trace_id"), "what_happened": what_happened(j["fault"], t, a, units, timeout_ms),
            "agent_answer": a["final_answer"], "correct_answer": a["correct_answer"],
            "answer_correct": a["answer_correct"], "propagated_header_matches": j["propagated"],
            "truth": t, "sentry": s, "sku": sku,
            "answer_kind": a.get("answer_kind"),
            # "hedged": the agent said it couldn't tell, instead of giving a confident answer
            "verdict": "hedged" if a.get("answer_kind") == "unclear"
                       else verdict(a["answer_correct"], truth_failed(t, a), s, broken=j["fault"] != "none"),
        })
    return rows


def sentry_cell(s: dict | None) -> str:
    if s is None:
        return "not read back"
    py = (f"MCP tool span {s['py_status'] or 'ok'} ({s['py_duration_ms']:.0f} ms)" if s["py_span_found"]
          else "MCP tool span missing")
    parts = [py, f"agent-marked span {s['js_tool_status'] or 'none'}", f"false HTTP errors {s['false_http_errors']}"]
    if s["error_spans"] or s["error_events"]:
        parts.append(f"other errors {len(s['error_spans']) + s['error_events']}")
    return ", ".join(parts)
