#!/usr/bin/env python3
"""Build report/index.html from runs/report.json. Static, no external assets."""
import html
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import labcore  # noqa: E402

CSS = """
:root{--bg:#fafaf7;--fg:#1d1d1b;--mute:#6a6a64;--card:#fff;--line:#deded6;--ok:#1d7a46;--bad:#b3261e;--warn:#9a6700}
@media (prefers-color-scheme:dark){:root{--bg:#161614;--fg:#eceae3;--mute:#9a9a92;--card:#1f1f1c;--line:#34342f;--ok:#5cc88a;--bad:#ff8a80;--warn:#e3b341}}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif}
main{max-width:920px;margin:0 auto;padding:32px 16px}
h1{font-size:1.5rem;margin:0 0 4px}p.sub{color:var(--mute);margin:0 0 24px}
.row{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:16px;margin:0 0 14px}
.row h2{font-size:1.05rem;margin:0 0 10px;display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap}
dl{display:grid;grid-template-columns:150px 1fr;gap:6px 12px;margin:0}dt{color:var(--mute)}dd{margin:0;overflow-wrap:anywhere}
.v{font-weight:600;padding:1px 10px;border-radius:99px;border:1px solid currentColor;font-size:.85rem}
.healthy{color:var(--ok)}.visible{color:var(--warn)}.misleading,.invisible,.missing{color:var(--bad)}
.no{color:var(--bad);font-weight:600}.yes{color:var(--ok);font-weight:600}
code{font-size:.85em}footer{color:var(--mute);font-size:.85rem;margin-top:24px}
@media(max-width:560px){dl{grid-template-columns:1fr}}
"""


def esc(x):
    return html.escape(str(x))


def build(report: dict) -> str:
    e = []
    for r in report["scenarios"]:
        s = r["sentry"]
        sentry = esc(labcore.sentry_cell(s))
        if s:
            sentry += f"<br><small>other error spans: {esc(', '.join(s['error_spans']) or 'none')}</small>"
        ok = r["answer_correct"]
        e.append(f"""<section class="row"><h2><span>{esc(r['sku'])} · {esc(r['title'])}</span><span class="v {esc(r['verdict'].replace(' ', '-'))}">{esc(r['verdict'])}</span></h2><dl>
<dt>What happened</dt><dd>{esc(r['what_happened'])} <small>(server truth log)</small></dd>
<dt>Agent said</dt><dd>{esc(r['agent_answer'])} <span class="{'yes' if ok else 'no'}">{'correct' if ok else 'wrong'}</span> (real: {esc(r['correct_answer'])})</dd>
<dt>Sentry showed</dt><dd>{sentry}</dd>
<dt>Trace</dt><dd><code>{esc(r['trace_id'])}</code>, header reached server: {'yes' if r['propagated_header_matches'] else 'no'}</dd></dl></section>""")
    note = "Sentry columns were read back from the API." if report["readback"] else "Sentry was not read back for this run."
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MCP Incident Lab</title><style>{CSS}</style></head><body><main>
<h1>MCP Incident Lab</h1><p class="sub">Three SKUs ({esc(', '.join(report['skus']))}), four scenarios each. The model is scripted, not a real LLM. {esc(note)}</p>
{''.join(e)}<footer>Ground truth comes from the server's own log, written outside the Sentry SDK.</footer></main></body></html>"""


if __name__ == "__main__":
    report = json.loads((ROOT / "runs/report.json").read_text())
    (ROOT / "report/index.html").write_text(build(report))
    print("wrote report/index.html")
