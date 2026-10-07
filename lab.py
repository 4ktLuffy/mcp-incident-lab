#!/usr/bin/env python3
"""Run healthy + 3 fault scenarios against the local MCP server, optionally read traces back."""
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

import labcore

ROOT = Path(__file__).resolve().parent
RUNS = ROOT / "runs"
FIX_ENV: dict[str, str] = {}  # set by --with-fixes
MODEL = "scripted"  # set by --model
SERVER = "py"  # set by --server
TRANSPORT = "http"  # set by --transport
SKUS = ["SKU-1001", "SKU-2002", "SKU-3003"]  # 42 in stock, really out of stock, 7 in stock
TIMEOUT_MS = 2000
DUMMY_DSN = "http://lab@127.0.0.1:9/1"  # local, goes nowhere


def wait_port(port: int, secs: float = 20) -> None:
    end = time.time() + secs
    while time.time() < end:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.2)
    raise RuntimeError("server did not start")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --- Sentry readback (copied in spirit from spanproof's sentry_api; credentials from env only) ---
def api_get(path: str, params: list[tuple[str, str]]) -> dict:
    import urllib.error
    import urllib.request
    url = f"{os.environ['SENTRY_REGION_URL']}/api/0/{path}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {os.environ['SENTRY_AUTH_TOKEN']}"})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code < 500 and e.code != 429:
                raise
        except (urllib.error.URLError, TimeoutError):
            pass
        time.sleep(2 ** attempt)
    raise RuntimeError(f"Sentry API unreachable: {path}")


FIELDS = ["id", "span.op", "span.description", "span.status", "span.duration", "is_transaction",
          "http.response.status_code", "project.name", "release", "precise.start_ts",
          "mcp.tool.result.content"]


def fetch_spans(query: str) -> list[dict]:
    params = [("dataset", "spans"), ("query", query), ("statsPeriod", "1h"), ("per_page", "100")]
    params += [("field", f) for f in FIELDS + ["trace"]]
    return api_get(f"organizations/{os.environ['SENTRY_ORG']}/events/", params).get("data", [])


def fetch_by_run_id(run_id: str, have: list[dict]) -> list[dict]:
    """Spans tagged lab.run_id (stdio server) that are not already in the agent's trace."""
    seen = {r.get("id") for r in have}
    return [{**r, "separate_trace": True} for r in fetch_spans(f"lab.run_id:{run_id}") if r.get("id") not in seen]


def has_tool_span(rows: list[dict]) -> bool:
    return any(r.get("span.op") == "mcp.server" and str(r.get("span.description") or "").startswith("tools/call")
               for r in rows)


def fetch_trace(trace_id: str) -> tuple[list[dict], int]:
    org = os.environ["SENTRY_ORG"]
    params = [("dataset", "spans"), ("query", f"trace:{trace_id}"), ("statsPeriod", "1h"), ("per_page", "100")]
    params += [("field", f) for f in FIELDS]
    rows = api_get(f"organizations/{org}/events/", params).get("data", [])
    try:
        ep = [("dataset", "errors"), ("query", f"trace:{trace_id}"), ("statsPeriod", "1h"),
              ("field", "id"), ("per_page", "20")]
        errs = len(api_get(f"organizations/{org}/events/", ep).get("data", []))
    except Exception:  # noqa: BLE001 - errors dataset is a best-effort second signal
        errs = 0
    return rows, errs


def readback(runs: list[dict], wait: int, tries: int = 8) -> dict:
    need = ["SENTRY_ORG", "SENTRY_REGION_URL", "SENTRY_AUTH_TOKEN"]
    missing = [k for k in need if not os.environ.get(k)]
    if missing:
        sys.exit(f"--readback needs env vars: {', '.join(missing)}")
    print(f"waiting {wait}s for Sentry ingestion...", flush=True)
    time.sleep(wait)
    out, raw = {}, {}
    for r in runs:
        tid = r["agent"]["trace_id"]
        summary = None
        for _ in range(tries):
            rows, errs = fetch_trace(tid)
            if TRANSPORT == "stdio" and not has_tool_span(rows):
                rows += fetch_by_run_id(r["run_id"], rows)
            summary = labcore.summarize_spans(rows, errs)
            if summary["joined"] or summary["separate_trace"]:
                break
            time.sleep(15)
        out[r["run_id"]] = summary
        raw[f'{r["agent"]["sku"]}/{r["fault"]}'] = rows
    (RUNS / "spans_raw.json").write_text(json.dumps(raw, indent=1))  # every span, for report/demo.py
    return out


def run_lab(offline_dsn: bool) -> None:
    RUNS.mkdir(exist_ok=True)
    for f in ("truth.jsonl", "agent.jsonl"):
        (RUNS / f).write_text("")
    stdio = TRANSPORT == "stdio"  # the agent spawns the server itself, one per run
    port = free_port()
    env = dict(os.environ, LAB_PORT=str(port), LAB_RUNS_DIR=str(RUNS), **FIX_ENV)
    if offline_dsn:
        env["SENTRY_DSN_PY"] = env["SENTRY_DSN_JS"] = DUMMY_DSN
    py = ROOT / ".venv/bin/python"
    cmd, cwd = ([str(py), "-m", "server.server"], ROOT) if SERVER == "py" else (["node", "mcp_server.mjs"], ROOT / "agent")
    server = None if stdio else subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.DEVNULL,
                                                 stderr=subprocess.DEVNULL)
    try:
        if server:
            wait_port(port)
        for sku in SKUS:
          for fault in labcore.SCENARIOS:
            print(f"running {sku} {fault}...", flush=True)
            subprocess.run(["node", "agent.mjs", "--run-id", f"lab-{sku}-{fault}-{int(time.time())}", "--fault", fault,
                            "--sku", sku, "--mcp-url", f"http://127.0.0.1:{port}/mcp",
                            "--timeout-ms", str(TIMEOUT_MS), "--runs-dir", str(RUNS), "--model", MODEL,
                            "--transport", TRANSPORT],
                           cwd=ROOT / "agent", env=env, check=True, stdout=subprocess.DEVNULL)
        if server:
            end = time.time() + 20  # the slow call finishes on the server after the client left
            while time.time() < end and len(labcore.read_jsonl(RUNS / "truth.jsonl")) < len(SKUS) * len(labcore.SCENARIOS):
                time.sleep(0.5)
            time.sleep(5)  # let the server finish and send its spans before a graceful stop
        # stdio: each server process ended when its agent closed the client; a missing truth row is a finding
    finally:
        if server:
            server.send_signal(signal.SIGINT)  # graceful: uvicorn shuts down and the SDK flushes at exit
            server.wait(10)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--readback", action="store_true", help="pull traces back from Sentry (needs env vars)")
    ap.add_argument("--wait", type=int, default=45, help="seconds to wait before reading back")
    ap.add_argument("--offline-dsn", action="store_true",
                    help="use a dummy local DSN so trace headers propagate without sending anything")
    ap.add_argument("--model", choices=["scripted", "codex"], default="scripted",
                    help="codex: a real model through the Codex CLI (no API key); writes to runs-codex*/")
    ap.add_argument("--with-fixes", metavar="SENTRY_NODE_BUILD",
                    help="rerun with the fixes on: span streaming and recorded tool results in Python, and the "
                         "@sentry/node build at this path (e.g. the getsentry/sentry-javascript#25129 branch, "
                         "packages/node/build/esm/index.js). Writes to runs-fixed/")
    ap.add_argument("--server", choices=["py", "js"], default="py",
                    help="which MCP server to run: server/server.py (py) or agent/mcp_server.mjs (js, writes to runs-js/)")
    ap.add_argument("--transport", choices=["http", "stdio"], default="http",
                    help="stdio: the agent spawns the Python server per run (writes to runs-stdio*/)")
    args = ap.parse_args()
    if args.transport == "stdio" and args.server == "js":
        ap.error("--transport stdio only works with the Python server")

    global RUNS, SERVER, MODEL, TRANSPORT
    SERVER, MODEL, TRANSPORT = args.server, args.model, args.transport
    if SERVER == "js":
        RUNS = ROOT / "runs-js"
    if args.with_fixes:
        RUNS = ROOT / ("runs-js-fixed" if SERVER == "js" else "runs-fixed")
        FIX_ENV.update(LAB_SENTRY_NODE=str(Path(args.with_fixes).resolve()), LAB_PY_STREAM="1", LAB_PY_RECORD_OUTPUTS="1")
    if TRANSPORT == "stdio":
        RUNS = RUNS.with_name(RUNS.name + "-stdio")
    if MODEL == "codex":
        RUNS = RUNS.with_name(RUNS.name + "-codex")
    run_lab(args.offline_dsn)
    joined = labcore.join_runs(labcore.read_jsonl(RUNS / "truth.jsonl"), labcore.read_jsonl(RUNS / "agent.jsonl"))
    sentry = readback(joined, args.wait) if args.readback else None
    units = {k: v["in_stock"] for k, v in json.loads((ROOT / "server/inventory.json").read_text()).items()}
    report = labcore.build_report(joined, sentry, units, TIMEOUT_MS, TRANSPORT)
    (RUNS / "report.json").write_text(json.dumps(
        {"skus": SKUS, "client_timeout_ms": TIMEOUT_MS, "model": MODEL, "transport": TRANSPORT, "readback": bool(args.readback),
         "scenarios": report}, indent=2))

    hdr = ("sku", "scenario", "what really happened", "answer correct?", "what Sentry showed", "verdict")
    rows = [(r["sku"], r["title"], r["what_happened"], "yes" if r["answer_correct"] else "NO",
             labcore.sentry_cell(r["sentry"]), r["verdict"]) for r in report]
    w = [max(len(str(x[i])) for x in rows + [hdr]) for i in range(6)]
    for row in [hdr] + rows:
        print(" | ".join(str(c).ljust(w[i]) for i, c in enumerate(row)))
    print("trace context reached server:", all(r["propagated_header_matches"] for r in report))


if __name__ == "__main__":
    main()
