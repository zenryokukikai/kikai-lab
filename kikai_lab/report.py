"""Aggregate a kikai project into a single report (JSON) + a self-contained HTML
dashboard, for viewing the project concept, per-experiment descriptions, and the run
ledger offline (no server). Metrics/artifacts are layered on separately (fetched from
the training host on demand and merged into `runs[].metrics`).

Data sources (all local records):
  * current.json          -> project state (id, stage, summary, current experiment/run)
  * experiments/*.yaml     -> experiments (title, summary, external_refs)
  * containers/*.yaml      -> the run ledger (container_id, status, role, image, summary)
"""
from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from kikai_lab.decision import load_decisions
from kikai_lab.store import compute_current_state, load_current
from kikai_lab.validation import load_yaml


def build_project_report(project_root: str | Path, *, now: Any = None) -> dict[str, Any]:
    """Pure aggregation of the local project records into one report dict."""
    root = Path(project_root)
    current = load_current(root)
    staleness: str | None = None
    age_hours: float | None = None
    if current.get("last_verified_at"):
        try:
            state = compute_current_state(current, now=now)
            staleness = state.staleness
            age_hours = round(state.age_hours, 1)
        except (ValueError, KeyError):
            staleness = None

    project = {
        "project_id": current.get("project_id"),
        "current_stage": current.get("current_stage"),
        "current_experiment_id": current.get("current_experiment_id"),
        "current_run_name": current.get("current_run_name"),
        "summary": current.get("summary"),
        "last_verified_at": current.get("last_verified_at"),
        "verified_by": current.get("verified_by"),
        "next_decision_id": current.get("next_decision_id"),
        "next_decision_required": current.get("next_decision_required"),
        "staleness": staleness,
        "age_hours": age_hours,
    }

    experiments: list[dict[str, Any]] = []
    exp_dir = root / "experiments"
    if exp_dir.is_dir():
        for path in sorted(exp_dir.glob("*.yaml")):
            rec = load_yaml(path)
            if rec.get("kind") != "experiment":
                continue
            experiments.append({
                "experiment_id": rec.get("experiment_id"),
                "title": rec.get("title"),
                "summary": rec.get("summary"),
                "external_refs": rec.get("external_refs", []),
                "is_current": rec.get("experiment_id") == current.get("current_experiment_id"),
            })

    runs: list[dict[str, Any]] = []
    con_dir = root / "containers"
    if con_dir.is_dir():
        for path in sorted(con_dir.glob("*.yaml")):
            rec = load_yaml(path)
            if rec.get("kind") != "docker_container":
                continue
            docker = rec.get("docker") or {}
            runs.append({
                "container_id": rec.get("container_id"),
                "name": docker.get("name"),
                "image": docker.get("image"),
                "role": rec.get("role"),
                "status": rec.get("status"),
                "summary": rec.get("summary"),
                "related_runs": rec.get("related_runs", []),
                "metrics": None,  # filled by the metrics layer (remote, on demand)
            })

    decisions = [
        {
            "decision_id": d.get("decision_id"),
            "title": d.get("title"),
            "summary": d.get("summary"),
            "status": d.get("status"),
            "decided_at": d.get("decided_at"),
            "links": d.get("links", []),
        }
        for d in load_decisions(root)
    ]

    return {
        "schema_version": 1,
        "kind": "kikai_project_report",
        "project": project,
        "decisions": decisions,
        "experiments": experiments,
        "runs": runs,
        "decision_count": len(decisions),
        "experiment_count": len(experiments),
        "run_count": len(runs),
    }


_HTML_TEMPLATE = """<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>kikai — __PROJECT__</title>
<style>
:root{--bg:#0f1419;--card:#1a2230;--fg:#e6edf3;--muted:#8b97a7;--accent:#4ea1ff;
--ok:#3fb950;--warn:#d29922;--stale:#f85149;--border:#2b3645}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:24px}
h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:28px 0 12px;color:var(--muted);
text-transform:uppercase;letter-spacing:.06em}
.proj{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:18px 20px}
.proj .sum{color:var(--muted);margin-top:8px;white-space:pre-wrap}
.meta{display:flex;flex-wrap:wrap;gap:8px 18px;margin-top:12px;font-size:12.5px;color:var(--muted)}
.meta b{color:var(--fg);font-weight:600}
.badge{display:inline-block;padding:2px 9px;border-radius:999px;font-size:11.5px;font-weight:600}
.b-fresh{background:rgba(63,185,80,.15);color:var(--ok)}
.b-warn{background:rgba(210,153,34,.15);color:var(--warn)}
.b-stale{background:rgba(248,81,73,.15);color:var(--stale)}
.b-cur{background:rgba(78,161,255,.15);color:var(--accent)}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:14px}
.card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:14px 16px}
.card.cur{border-color:var(--accent)}
.card h3{margin:0 0 6px;font-size:14px}
.card .sum{color:var(--muted);font-size:12.5px;white-space:pre-wrap}
.refs{margin-top:8px;font-size:11.5px;color:var(--muted)}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--border);vertical-align:top}
th{color:var(--muted);font-weight:600;position:sticky;top:0;background:var(--bg)}
td.id{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;white-space:nowrap}
.st{font-size:11px;padding:1px 7px;border-radius:6px;background:#222c3a;color:var(--muted)}
.foot{margin-top:30px;color:var(--muted);font-size:11.5px}
.filter{margin:0 0 10px;padding:7px 11px;width:100%;max-width:340px;background:var(--card);
color:var(--fg);border:1px solid var(--border);border-radius:8px}
</style></head><body><div class="wrap" id="app"></div>
<script>const REPORT=__REPORT__;
function esc(s){return (s==null?'':String(s)).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
function badge(st){if(!st)return '';return `<span class="badge b-${st}">${st}</span>`}
function render(){const r=REPORT,p=r.project,a=document.getElementById('app');
let h=`<h1>kikai — ${esc(p.project_id)} ${badge(p.staleness)}</h1>`;
h+=`<div class="proj"><div><b>${esc(p.current_stage||'')}</b></div>`;
if(p.summary)h+=`<div class="sum">${esc(p.summary)}</div>`;
h+=`<div class="meta"><span>current experiment: <b>${esc(p.current_experiment_id||'-')}</b></span>`;
h+=`<span>current run: <b>${esc(p.current_run_name||'-')}</b></span>`;
if(p.last_verified_at)h+=`<span>verified: <b>${esc(p.last_verified_at)}</b> ${p.age_hours!=null?'('+p.age_hours+'h)':''} by ${esc(p.verified_by||'')}</span>`;
if(p.next_decision_id)h+=`<span>next decision: <b>${esc(p.next_decision_id)}</b>${p.next_decision_required?' (required)':''}</span>`;
h+=`</div></div>`;
if(r.decisions&&r.decisions.length){h+=`<h2>Decisions (${r.decision_count})</h2><div class="cards">`;
for(const d of r.decisions){h+=`<div class="card"><h3>${esc(d.decision_id)} — ${esc(d.title||'')} <span class="st">${esc(d.status||'')}</span></h3>`;
if(d.summary)h+=`<div class="sum">${esc(d.summary)}</div>`;
if(d.decided_at)h+=`<div class="refs">decided: ${esc(d.decided_at)}</div>`;
if(d.links&&d.links.length)h+=`<div class="refs">links: ${d.links.map(x=>esc((x.kind||'')+':'+(x.id||''))).join(', ')}</div>`;
h+=`</div>`}h+=`</div>`}
h+=`<h2>Experiments (${r.experiment_count})</h2><div class="cards">`;
for(const e of r.experiments){h+=`<div class="card ${e.is_current?'cur':''}"><h3>${esc(e.title||e.experiment_id)} ${e.is_current?badge('cur').replace('>cur<','>current<'):''}</h3>`;
if(e.summary)h+=`<div class="sum">${esc(e.summary)}</div>`;
if(e.external_refs&&e.external_refs.length)h+=`<div class="refs">refs: ${e.external_refs.map(x=>esc(x.id||x.title||'')).join(', ')}</div>`;
h+=`</div>`}
h+=`</div>`;
h+=`<h2>Runs (${r.run_count})</h2>`;
h+=`<input class="filter" id="f" placeholder="filter runs…" oninput="draw()">`;
h+=`<div id="runs"></div>`;
a.innerHTML=h;draw()}
function draw(){const q=(document.getElementById('f').value||'').toLowerCase();
const rows=REPORT.runs.filter(x=>!q||JSON.stringify(x).toLowerCase().includes(q));
let h=`<table><thead><tr><th>container</th><th>status</th><th>role</th><th>image</th><th>summary</th><th>metrics</th></tr></thead><tbody>`;
for(const x of rows){h+=`<tr><td class="id">${esc(x.container_id)}<br><span class="st">${esc(x.name||'')}</span></td>`;
h+=`<td><span class="st">${esc(x.status||'')}</span></td><td>${esc(x.role||'')}</td><td class="id">${esc(x.image||'')}</td>`;
h+=`<td>${esc(x.summary||'')}</td><td>${x.metrics?esc(JSON.stringify(x.metrics)):'<span class="st">—</span>'}</td></tr>`}
h+=`</tbody></table>`;document.getElementById('runs').innerHTML=h}
render();</script>
<div class="wrap foot">generated by <code>kikai report</code> · static offline dashboard</div>
</body></html>"""


def render_report_html(report: dict[str, Any]) -> str:
    """Self-contained HTML dashboard with the report JSON inlined (no server / no fetch)."""
    payload = json.dumps(report, ensure_ascii=False).replace("</", "<\\/")
    # HTML-escape project_id before it lands in <title> (stored-XSS sink otherwise), and
    # substitute __PROJECT__ BEFORE inlining __REPORT__ so a payload that happens to contain
    # the literal "__PROJECT__" can't be rewritten by the project substitution.
    project_id = html.escape(str((report.get("project") or {}).get("project_id") or "project"))
    return _HTML_TEMPLATE.replace("__PROJECT__", project_id).replace("__REPORT__", payload)
