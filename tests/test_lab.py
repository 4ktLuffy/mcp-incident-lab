import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import labcore  # noqa: E402
from server import server as srv  # noqa: E402


def call(fault, sku="SKU-1001", trace="aaaa-bbbb-1", monkeypatch=None):
    return asyncio.run(srv.run_check(sku, fault, "r1", trace))


def rows(tmp_path):
    return [json.loads(x) for x in (tmp_path / "truth.jsonl").read_text().splitlines()]


def test_healthy_logs_truth(tmp_path, monkeypatch):
    monkeypatch.setenv("LAB_RUNS_DIR", str(tmp_path))
    payload, err, _ = call(None)
    r = rows(tmp_path)[0]
    assert payload["in_stock"] == 42 and not err
    assert r["run_id"] == "r1" and r["fault"] == "none" and r["sentry_trace"] == "aaaa-bbbb-1"
    assert r["args"] == {"sku": "SKU-1001"} and r["is_error"] is False


def test_tool_error_logged(tmp_path, monkeypatch):
    monkeypatch.setenv("LAB_RUNS_DIR", str(tmp_path))
    payload, err, text = call("tool_error")
    r = rows(tmp_path)[0]
    assert payload is None and err and text == "warehouse DB timeout"
    assert r["is_error"] and r["returned"]["text"] == "warehouse DB timeout"


def test_stale_data_is_silent_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("LAB_RUNS_DIR", str(tmp_path))
    payload, err, _ = call("stale_data")
    r = rows(tmp_path)[0]
    assert payload["in_stock"] == 0 and not err and r["is_error"] is False
    assert srv.INVENTORY["SKU-1001"]["in_stock"] == 42  # the real table is untouched


def test_slow_duration_logged(tmp_path, monkeypatch):
    monkeypatch.setenv("LAB_RUNS_DIR", str(tmp_path))
    monkeypatch.setattr(srv, "SLOW_SECONDS", 0.05)
    call("slow")
    assert rows(tmp_path)[0]["duration_ms"] >= 50


def span(op, status="ok", dur=5, code=None):
    desc = "tools/call check_inventory" if op == "mcp.server" else op
    return {"span.op": op, "span.status": status, "span.duration": dur, "http.response.status_code": code,
            "span.description": desc}


def test_verdicts():
    green = labcore.summarize_spans([span("mcp.server"), span("gen_ai.execute_tool")])
    red = labcore.summarize_spans([span("mcp.server", "internal_error"), span("gen_ai.execute_tool")])
    hand_marked = labcore.summarize_spans([span("mcp.server"), span("gen_ai.execute_tool", "error")])
    noisy = labcore.summarize_spans([span("mcp.server"), span("gen_ai.execute_tool"), span("http.client", "error", code=200)])
    real_http = labcore.summarize_spans([span("mcp.server"), span("gen_ai.execute_tool"), span("http.client", "error", code=500)])
    dropped = labcore.summarize_spans([span("gen_ai.execute_tool", "error")])
    assert green["joined"] and green["error_spans"] == []
    init_first = labcore.summarize_spans([{"span.op": "mcp.server", "span.status": "ok", "span.description": "initialize"},
                                          span("mcp.server", "internal_error"), span("gen_ai.execute_tool")])
    assert init_first["py_status"] == "internal_error"   # the tool call's span, not initialize
    assert labcore.verdict(True, False, green) == "healthy"
    assert labcore.verdict(True, True, green, broken=True) == "lucky"   # broken tool, SKU really out of stock
    assert labcore.verdict(False, False, green) == "invisible"   # stale data
    assert labcore.verdict(False, True, green) == "misleading"   # failed but all green
    assert labcore.verdict(False, True, red) == "visible"
    assert labcore.verdict(False, False, labcore.summarize_spans([span("x")], error_events=1)) == "visible"
    assert labcore.verdict(False, True, hand_marked) == "misleading"  # only our own code marked it
    assert noisy["false_http_errors"] == 1 and labcore.verdict(False, False, noisy) == "invisible"
    assert labcore.verdict(False, False, real_http) == "visible"     # a real 5xx still counts
    assert labcore.verdict(False, True, dropped) == "missing"
    aborted = labcore.summarize_spans([span("gen_ai.execute_tool", "error"), span("http.client", "error", code=None)])
    assert aborted["false_http_errors"] == 1 and labcore.verdict(False, True, aborted) == "missing"
    assert labcore.verdict(False, True, None) == "not read back"


def test_join_and_propagation():
    truth = [{"run_id": "a", "sentry_trace": "T1-s-1"}, {"run_id": "b", "sentry_trace": None}]
    agent = [{"run_id": "a", "fault": "none", "trace_id": "T1"}, {"run_id": "b", "fault": "slow", "trace_id": "T2"}]
    j = labcore.join_runs(truth, agent)
    assert [x["propagated"] for x in j] == [True, False]


def test_truth_failed_counts_client_timeout():
    assert labcore.truth_failed({"is_error": False}, {"tool_result_seen": {"threw": "timeout"}})
    assert not labcore.truth_failed({"is_error": False}, {"tool_result_seen": {"threw": None}})


def test_separate_trace_is_visible():
    sep = {**span("mcp.server"), "separate_trace": True}
    s = labcore.summarize_spans([sep, span("gen_ai.execute_tool")])
    assert s["py_span_found"] and s["separate_trace"] and not s["joined"]
    assert "in a separate trace" in labcore.sentry_cell(s)
    same = labcore.summarize_spans([span("mcp.server"), span("gen_ai.execute_tool")])
    assert same["joined"] and not same["separate_trace"] and "separate" not in labcore.sentry_cell(same)
    assert labcore.verdict(False, False, s) == "invisible"   # found, so not "missing"


def test_what_happened_stdio_slow():
    a = {"tool_result_seen": {"threw": "timeout"}}
    assert labcore.what_happened("slow", None, a, 42, 2000, "stdio") == "server process ended before the tool finished"
    cut = {"returned": None, "is_error": False, "duration_ms": 2002.1}
    assert "before it finished" in labcore.what_happened("slow", cut, a, 42, 2000, "stdio")
    done = {"returned": {"in_stock": 42}, "is_error": False, "duration_ms": 6002.0}
    assert "took 6002 ms" in labcore.what_happened("slow", done, a, 42, 2000)
