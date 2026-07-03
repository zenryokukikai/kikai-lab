/* kikai dashboard — hash-routed vanilla-JS SPA over the /v1 envelope API.
 *
 * No build step, no framework, no runtime network fetch beyond /v1 (Chart.js is
 * vendored). Every server-sourced string goes through esc() before touching
 * innerHTML: registry data is semi-trusted (agents write it), so the dashboard
 * treats it as text, never markup.
 */
'use strict';

// ------------------------------------------------------------------ utilities

/** Escape a value for safe interpolation into HTML (element or attribute text). */
function esc(value) {
  return String(value == null ? '' : value).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function $(sel) { return document.querySelector(sel); }

function badge(text, klass) {
  if (!text) return '';
  return `<span class="badge b-${esc(klass || text)}">${esc(text)}</span>`;
}

function fmtNum(v) {
  if (v == null) return '—';
  if (typeof v === 'number' && !Number.isInteger(v)) return v.toPrecision(5);
  return String(v);
}

/** Dismissible toast; kind 'err' (default) or 'ok'. Auto-hides after 8s.
 * message is treated as TEXT and escaped; pass {html: true} only with pre-escaped
 * fragments (server strings must never reach innerHTML raw). */
function toast(message, kind, opts) {
  const box = document.createElement('div');
  box.className = 'toast' + (kind === 'ok' ? ' ok' : '');
  const body = (opts && opts.html) ? message : esc(message);
  box.innerHTML = `<div>${body}</div><button aria-label="dismiss">✕</button>`;
  box.querySelector('button').onclick = () => box.remove();
  $('#toasts').appendChild(box);
  setTimeout(() => box.remove(), 8000);
}

/**
 * Fetch an /v1 endpoint and unwrap its envelope ({ok, data, errors, ...}).
 * On ok=false (or network failure) shows each error as a toast and throws;
 * pass {quiet: true} to skip the toast (callers that render errors inline).
 */
async function api(path, opts) {
  const { quiet, ...init } = opts || {};
  // every JSON mutation must carry the content-type or FastAPI 422s on text/plain;
  // default it here so no caller can forget (a shipped button once did).
  if (typeof init.body === 'string' && !(init.headers || {})['Content-Type']) {
    init.headers = { ...(init.headers || {}), 'Content-Type': 'application/json' };
  }
  let body;
  try {
    const res = await fetch(path, init);
    body = await res.json();
  } catch (err) {
    if (!quiet) toast(`request failed: ${esc(err.message)} — <code>${esc(path)}</code>`, 'err', { html: true });
    throw err;
  }
  if (!body || body.ok !== true) {
    const errors = (body && body.errors) || [];
    const messages = errors.map((e) => `<code>${esc(e.code)}</code> ${esc(e.message)}`);
    if (!quiet) (messages.length ? messages : [`request failed: <code>${esc(path)}</code>`])
      .forEach((m) => toast(m, 'err', { html: true }));
    const err = new Error(errors.map((e) => e.code).join(',') || 'request_failed');
    err.status = errors.length ? errors[0].code : 'unknown';
    err.errors = errors;
    throw err;
  }
  return body.data;
}

// --------------------------------------------------------------- view state

/** Per-view resources torn down on every route change (timers + charts). */
const view = { timers: [], charts: [] };

function addTimer(fn, ms) { view.timers.push(setInterval(fn, ms)); }

function resetView() {
  view.timers.forEach(clearInterval);
  view.timers = [];
  view.charts.forEach((c) => c.destroy());
  view.charts = [];
  closeModal();
}

function openModal(html) {
  $('#modal').innerHTML = html;
  $('#modal-backdrop').classList.remove('hidden');
}

function closeModal() {
  $('#modal-backdrop').classList.add('hidden');
  $('#modal').innerHTML = '';
}

const PALETTE = ['#4ea1ff', '#3fb950', '#d29922', '#f85149',
  '#bc8cff', '#39c5cf', '#ff9bce', '#e3b341'];

/** Chart.js line chart with the dashboard dark theme; registered for teardown. */
function makeLineChart(canvas, datasets, labels) {
  const grid = 'rgba(43, 54, 69, .6)';
  const chart = new Chart(canvas, {
    type: 'line',
    data: { labels, datasets },
    options: {
      animation: false,
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { labels: { color: '#8b97a7', boxWidth: 12 } } },
      scales: {
        x: { ticks: { color: '#8b97a7', maxTicksLimit: 12 }, grid: { color: grid } },
        y: { ticks: { color: '#8b97a7' }, grid: { color: grid } },
      },
    },
  });
  view.charts.push(chart);
  return chart;
}

function dataset(label, data, i) {
  const color = PALETTE[i % PALETTE.length];
  return {
    label, data, borderColor: color, backgroundColor: color,
    borderWidth: 1.5, pointRadius: 0, tension: 0.1, spanGaps: true,
  };
}

// ------------------------------------------------------------------ routing

function href(...parts) { return '#/' + parts.map(encodeURIComponent).join('/'); }

function setCrumbs(html) { $('#crumbs').innerHTML = html; }

function render() {
  resetView();
  const root = $('#view');
  root.innerHTML = '<div class="muted">loading…</div>';
  const parts = location.hash.replace(/^#\/?/, '').split('/')
    .filter(Boolean).map(decodeURIComponent);
  if (parts.length === 0) return viewProjects(root);
  if (parts[0] === 'p' && parts.length === 2) return viewProject(root, parts[1]);
  if (parts[0] === 'p' && parts[2] === 'e' && parts.length === 4) {
    return viewExperiment(root, parts[1], parts[3]);
  }
  if (parts[0] === 'p' && parts[2] === 'r' && parts.length === 4) {
    return viewRun(root, parts[1], parts[3]);
  }
  root.innerHTML = `<h1>not found</h1>
    <p class="muted">unknown route <code>${esc(location.hash)}</code> — <a href="#/">projects</a></p>`;
}

// --------------------------------------------------------- view: projects (#/)

async function viewProjects(root) {
  setCrumbs('projects');
  const showArchived = sessionStorage.getItem('kikai.showArchived') === '1';
  let data;
  try {
    data = await api(`/v1/projects?include_archived=${showArchived}`);
  } catch (err) {
    root.innerHTML = '<p class="note">could not load projects — see error toast.</p>';
    return;
  }
  const cards = data.projects.map((p) => {
    const invalid = p.status === 'invalid';
    const archived = p.status === 'archived';
    const archiveBtn = invalid ? '' : `
      <button class="small ${archived ? '' : 'danger'}" data-arch="${esc(p.project_id)}"
              data-mode="${archived ? 'unarchive' : 'archive'}">
        ${archived ? 'unarchive' : 'archive'}</button>`;
    return `<div class="card">
      <h3><a href="${href('p', p.project_id)}">${esc(p.project_id)}</a>
        ${badge(p.status)}</h3>
      <div class="sum">${esc(p.summary || '')}</div>
      <div class="refs">
        experiments: ${esc(p.experiment_count ?? '—')} ·
        runs: ${esc(p.run_count ?? '—')} ·
        managed: ${esc(p.managed_run_count ?? '—')}
        ${p.updated_at ? ` · updated ${esc(p.updated_at)}` : ''}
      </div>
      <div class="row" style="margin-top:10px">${archiveBtn}</div>
    </div>`;
  }).join('');
  root.innerHTML = `
    <div class="row spread">
      <h1>Projects <span class="muted">(${esc(data.total)})</span></h1>
      <div class="row">
        <label class="check"><input type="checkbox" id="show-archived"
          ${showArchived ? 'checked' : ''}> show archived</label>
        <button class="primary" id="new-project">New project</button>
      </div>
    </div>
    <div class="cards" style="margin-top:14px">
      ${cards || '<p class="note">no projects yet — create one.</p>'}
    </div>`;
  $('#show-archived').onchange = (e) => {
    sessionStorage.setItem('kikai.showArchived', e.target.checked ? '1' : '0');
    render();
  };
  $('#new-project').onclick = showNewProjectModal;
  root.querySelectorAll('button[data-arch]').forEach((btn) => {
    btn.onclick = async () => {
      const id = btn.dataset.arch;
      const mode = btn.dataset.mode;
      if (!confirm(`${mode} project '${id}'?`)) return;
      await api(`/v1/projects/${encodeURIComponent(id)}/${mode}`, { method: 'POST' });
      toast(`project <code>${esc(id)}</code> ${esc(mode)}d`, 'ok', { html: true });
      render();
    };
  });
}

function showNewProjectModal() {
  openModal(`
    <h3>New project</h3>
    <input class="input" id="np-id" placeholder="project id (e.g. example_project)">
    <textarea class="input" id="np-summary" placeholder="one-paragraph summary"></textarea>
    <div class="row" style="justify-content:flex-end">
      <button id="np-cancel">Cancel</button>
      <button class="primary" id="np-create">Create</button>
    </div>`);
  $('#np-cancel').onclick = closeModal;
  $('#np-create').onclick = async () => {
    const id = $('#np-id').value.trim();
    const summary = $('#np-summary').value.trim();
    if (!id) { toast('project id is required'); return; }
    const data = await api(`/v1/projects/${encodeURIComponent(id)}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ summary }),
    });
    closeModal();
    toast(`project <code>${esc(id)}</code> ${data.created ? 'created' : 'saved'}`, 'ok', { html: true });
    render();
  };
}

// -------------------------------------------------- view: project (#/p/<id>)

function stalenessBadge(staleness) {
  if (!staleness) return '';
  const klass = { fresh: 'fresh', warn: 'warn', stale: 'stale' }[staleness] || 'unknown';
  return badge(staleness, klass);
}

async function viewProject(root, pid) {
  setCrumbs(`<a href="#/">projects</a> / ${esc(pid)}`);
  let report, runsData;
  try {
    [{ report }, runsData] = await Promise.all([
      api(`/v1/projects/${encodeURIComponent(pid)}/report`),
      api(`/v1/projects/${encodeURIComponent(pid)}/runs`),
    ]);
  } catch (err) {
    root.innerHTML = '<p class="note">could not load project — see error toast.</p>';
    return;
  }
  const p = report.project;
  const decisions = (report.decisions || []).map((d) => `
    <div class="card">
      <h3>${esc(d.decision_id)} — ${esc(d.title || '')} <span class="st">${esc(d.status || '')}</span></h3>
      ${d.summary ? `<div class="sum">${esc(d.summary)}</div>` : ''}
      ${d.decided_at ? `<div class="refs">decided: ${esc(d.decided_at)}</div>` : ''}
      ${(d.links || []).length ? `<div class="refs">links: ${d.links.map((x) => esc(`${x.kind || ''}:${x.id || ''}`)).join(', ')}</div>` : ''}
    </div>`).join('');
  const experiments = (report.experiments || []).map((e) => `
    <div class="card clickable ${e.is_current ? 'cur' : ''}"
         data-goto="${esc(href('p', pid, 'e', e.experiment_id || ''))}">
      <h3>${esc(e.title || e.experiment_id)} ${e.is_current ? badge('current', 'cur') : ''}</h3>
      ${e.summary ? `<div class="sum">${esc(e.summary)}</div>` : ''}
      <div class="refs mono">${esc(e.experiment_id)}</div>
    </div>`).join('');
  root.innerHTML = `
    <h1>${esc(pid)} ${stalenessBadge(p.staleness)}</h1>
    <div class="proj">
      <div><b>${esc(p.current_stage || '')}</b></div>
      ${p.summary ? `<div class="sum">${esc(p.summary)}</div>` : ''}
      <div class="meta">
        <span>current experiment: <b>${esc(p.current_experiment_id || '—')}</b></span>
        <span>current run: <b>${esc(p.current_run_name || '—')}</b></span>
        ${p.last_verified_at ? `<span>verified: <b>${esc(p.last_verified_at)}</b>
          ${p.age_hours != null ? `(${esc(p.age_hours)}h)` : ''} by ${esc(p.verified_by || '')}</span>` : ''}
        ${p.next_decision_id ? `<span>next decision: <b>${esc(p.next_decision_id)}</b>
          ${p.next_decision_required ? '(required)' : ''}</span>` : ''}
      </div>
    </div>
    ${decisions ? `<h2>Decisions (${esc(report.decision_count)})</h2><div class="cards">${decisions}</div>` : ''}
    <h2>Experiments (${esc(report.experiment_count)})</h2>
    <div class="cards">${experiments || '<p class="note">no experiments registered.</p>'}</div>
    <h2>Runs (${esc(runsData.total)})</h2>
    <input class="filter" id="run-filter" placeholder="filter runs…">
    <div id="run-table"></div>`;
  root.querySelectorAll('[data-goto]').forEach((el) => {
    el.onclick = () => { location.hash = el.dataset.goto; };
  });

  const drawRuns = () => {
    const q = ($('#run-filter').value || '').toLowerCase();
    const rows = runsData.runs
      .filter((r) => !q || JSON.stringify(r).toLowerCase().includes(q))
      .map((r) => {
        if (r._invalid) {
          return `<tr><td class="id">${esc(r._id)}</td>
            <td colspan="4"><span class="st">invalid: ${esc(r._error_code)}</span></td></tr>`;
        }
        return `<tr class="clickable" data-goto="${esc(href('p', pid, 'r', r.run_name || ''))}">
          <td class="id"><a href="${esc(href('p', pid, 'r', r.run_name || ''))}">${esc(r.run_name)}</a></td>
          <td class="id">${esc(r.experiment_id || '')}</td>
          <td>${badge(r.status || 'unknown')}</td>
          <td>${esc(r.lifecycle_state || '—')}</td>
          <td>${r.managed ? badge('managed', 'cur') : '<span class="st">—</span>'}</td>
        </tr>`;
      }).join('');
    $('#run-table').innerHTML = `<table><thead><tr>
        <th>run</th><th>experiment</th><th>status</th><th>lifecycle</th><th>managed</th>
      </tr></thead><tbody>${rows || '<tr><td colspan="5" class="note">no runs</td></tr>'}</tbody></table>`;
    $('#run-table').querySelectorAll('tr[data-goto]').forEach((tr) => {
      tr.onclick = (e) => {
        if (e.target.tagName !== 'A') location.hash = tr.dataset.goto;
      };
    });
  };
  $('#run-filter').oninput = drawRuns;
  drawRuns();
}

// ------------------------------------- view: experiment (#/p/<id>/e/<eid>)

async function viewExperiment(root, pid, eid) {
  setCrumbs(`<a href="#/">projects</a> /
    <a href="${esc(href('p', pid))}">${esc(pid)}</a> / ${esc(eid)}`);
  let data;
  try {
    data = await api(
      `/v1/projects/${encodeURIComponent(pid)}/experiments/${encodeURIComponent(eid)}`);
  } catch (err) {
    root.innerHTML = '<p class="note">could not load experiment — see error toast.</p>';
    return;
  }
  const e = data.experiment;
  const fields = ['status', 'stage', 'created_at', 'updated_at']
    .filter((k) => e[k] != null)
    .map((k) => `<span>${esc(k)}: <b>${esc(e[k])}</b></span>`).join('');
  const runs = (data.runs || []).map((r) => `
    <tr><td class="id"><a href="${esc(href('p', pid, 'r', r.run_name || ''))}">${esc(r.run_name)}</a></td>
    <td>${badge(r.status || 'unknown')}</td></tr>`).join('');
  root.innerHTML = `
    <h1>${esc(e.title || eid)}</h1>
    <div class="proj">
      <div class="refs mono">${esc(eid)}</div>
      ${e.summary ? `<div class="sum">${esc(e.summary)}</div>` : ''}
      <div class="meta">${fields}</div>
      ${(e.external_refs || []).length ? `<div class="refs">refs:
        ${e.external_refs.map((x) => esc(x.id || x.title || '')).join(', ')}</div>` : ''}
    </div>
    <h2>Loss across runs (${esc(data.run_count)})</h2>
    <div class="chart-box" style="height:340px"><canvas id="exp-chart"></canvas></div>
    <p class="note" id="exp-chart-note"></p>
    <h2>Runs</h2>
    <table><thead><tr><th>run</th><th>status</th></tr></thead>
    <tbody>${runs || '<tr><td colspan="2" class="note">no runs</td></tr>'}</tbody></table>`;

  // One comparison chart; runs without metrics come back as null and are skipped.
  let metrics;
  try {
    metrics = await api(`/v1/projects/${encodeURIComponent(pid)}/experiments/`
      + `${encodeURIComponent(eid)}/metrics?keys=loss&max_points=200`, { quiet: true });
  } catch (err) {
    $('#exp-chart-note').textContent = 'metrics unavailable for this experiment';
    return;
  }
  const named = Object.entries(metrics.runs || {}).filter(([, v]) => v && v.step.length);
  if (!named.length) {
    $('#exp-chart-note').textContent = 'no run has metrics yet';
    return;
  }
  // Union x-axis over every run's steps so runs of different lengths align.
  const allSteps = [...new Set(named.flatMap(([, v]) => v.step))].sort((a, b) => a - b);
  const datasets = named.map(([name, v], i) => {
    const byStep = new Map(v.step.map((s, j) => [s, (v.series.loss || [])[j]]));
    return dataset(name, allSteps.map((s) => byStep.has(s) ? byStep.get(s) : null), i);
  });
  makeLineChart($('#exp-chart'), datasets, allSteps);
}

// -------------------------------------------- view: run (#/p/<id>/r/<run>)

async function viewRun(root, pid, run) {
  setCrumbs(`<a href="#/">projects</a> /
    <a href="${esc(href('p', pid))}">${esc(pid)}</a> / ${esc(run)}`);
  const base = `/v1/projects/${encodeURIComponent(pid)}/runs/${encodeURIComponent(run)}`;
  let detail;
  try {
    detail = await api(base);
  } catch (err) {
    root.innerHTML = '<p class="note">could not load run — see error toast.</p>';
    return;
  }
  const checkpoints = (detail.checkpoints || []).map((c) =>
    `<tr><td class="id">${esc(c.step)}</td><td class="id">${esc(c.name)}</td></tr>`).join('');
  root.innerHTML = `
    <div class="row spread">
      <h1>${esc(run)} <span id="run-status-badge">${badge(detail.derived_status || 'unknown')}</span></h1>
      <button class="danger" id="stop-btn">Stop run</button>
    </div>
    <div class="proj">
      <div class="meta">
        <span>experiment: <b><a href="${esc(href('p', pid, 'e', detail.run.experiment_id || ''))}">
          ${esc(detail.run.experiment_id || '—')}</a></b></span>
        <span>declared status: <b>${esc(detail.run.status || '—')}</b></span>
        <span>managed: <b>${detail.managed_run ? 'yes' : 'no'}</b></span>
        ${detail.terminal_event ? `<span>terminal: <b>${esc(detail.terminal_event)}</b></span>` : ''}
      </div>
      <div class="kpis">
        <div class="kpi">latest step<b id="kpi-step">—</b></div>
        <div class="kpi">latest loss<b id="kpi-loss">—</b></div>
        <div class="kpi">QC delivered<b id="kpi-qc">—</b></div>
        <div class="kpi">container<b id="kpi-container">—</b></div>
      </div>
    </div>
    <h2>Metrics</h2>
    <div class="chart-box">
      <div class="row">
        <label class="check">series
          <select class="input" id="metric-key" style="margin:0"></select></label>
        <span class="note" id="metric-note"></span>
      </div>
      <div style="height:320px"><canvas id="run-chart"></canvas></div>
    </div>
    <div class="two-pane" style="margin-top:14px">
      <div>
        <h2>Artifacts</h2>
        <p class="note hidden" id="content-disabled-note">
          artifact content serving is disabled on this server (no content root configured);
          showing metadata only.</p>
        <div class="gallery" id="gallery"><p class="note">loading…</p></div>
      </div>
      <div>
        <h2>Conclusions</h2>
        <div id="conclusions">${
          ((detail.run || {}).conclusions || []).map((c) => `
            <div class="card"><h3>${badge(c.verdict)} <span class="st">${esc(c.at || '')}</span></h3>
            <div class="sum">${esc(c.summary || '')}</div>
            ${(c.evidence || []).length ? `<div class="refs">evidence: ${(c.evidence || []).map(esc).join(' / ')}</div>` : ''}
            ${c.next_run ? `<div class="refs">next: ${esc(c.next_run)}</div>` : ''}</div>`).join('')
          || '<div class="note">no conclusion recorded yet</div>'}</div>
        <div class="card">
          <select id="concl-verdict">
            <option value="adopted">adopted</option><option value="rejected">rejected</option>
            <option value="superseded">superseded</option><option value="inconclusive" selected>inconclusive</option>
          </select>
          <textarea id="concl-summary" rows="2" placeholder="考察を記録(このrunで何が分かったか)"></textarea>
          <button id="concl-save">record conclusion</button>
        </div>
        <h2>Checkpoints (${esc((detail.checkpoints || []).length)})</h2>
        <table><thead><tr><th>step</th><th>file</th></tr></thead>
        <tbody>${checkpoints || '<tr><td colspan="2" class="note">none yet</td></tr>'}</tbody></table>
      </div>
    </div>
    <div class="row spread" style="margin-top:24px">
      <h2 style="margin:0">Logs</h2>
      <button class="small" id="logs-refresh">refresh</button>
    </div>
    <pre class="logs" id="logs">loading…</pre>`;

  // -- status header: poll every 10s (timer is torn down on route change)
  const refreshStatus = async () => {
    let s;
    try { s = await api(`${base}/status`, { quiet: true }); } catch (err) { return; }
    if (!$('#run-status-badge')) return; // route changed while the fetch was in flight
    $('#run-status-badge').innerHTML = badge(s.derived_status || 'unknown');
    $('#kpi-step').textContent = fmtNum(s.latest_step);
    $('#kpi-loss').textContent = fmtNum(s.latest_loss);
    $('#kpi-qc').textContent = String((s.qc_done_steps || []).length);
    const c = s.container || {};
    $('#kpi-container').textContent =
      c.running ? 'running' : (c.exists ? `${c.state || 'exited'} (${fmtNum(c.exit_code)})` : 'absent');
  };
  refreshStatus();
  addTimer(refreshStatus, 10000);

  // -- stop button
  $('#stop-btn').onclick = async () => {
    if (!confirm(`stop run '${run}'?`)) return;
    const data = await api(`${base}/stop`, { method: 'POST' });
    toast(data.already_stopped
      ? `run <code>${esc(run)}</code> was already stopped`
      : `run <code>${esc(run)}</code> stop requested`, 'ok', { html: true });
    refreshStatus();
  };

  // -- record conclusion
  $('#concl-save').onclick = async () => {
    const summary = $('#concl-summary').value.trim();
    if (!summary) { toast('summary is required'); return; }
    $('#concl-save').disabled = true;
    try {
      await api(`${base}/conclusion`, {
        method: 'POST',
        body: JSON.stringify({ verdict: $('#concl-verdict').value, summary }),
      });
    } finally {
      const btn = $('#concl-save');
      if (btn) btn.disabled = false;
    }
    toast('conclusion recorded', 'ok');
    render();
  };

  // -- metrics chart with a series picker fed by available_keys
  let chart = null;
  const drawMetrics = async (key) => {
    let m;
    try {
      m = await api(`${base}/metrics?keys=${encodeURIComponent(key)}&max_points=500`,
        { quiet: true });
    } catch (err) {
      const note = $('#metric-note');
      if (note) note.textContent = 'no metrics available for this run';
      return null;
    }
    if (!$('#run-chart')) return null; // route changed while the fetch was in flight
    if (chart) { chart.destroy(); view.charts = view.charts.filter((c) => c !== chart); }
    chart = makeLineChart($('#run-chart'),
      [dataset(key, m.series[key] || [], 0)], m.step);
    $('#metric-note').textContent =
      `${m.points} points${m.downsampled ? ` (downsampled, stride ${m.stride})` : ''}`;
    return m;
  };
  const first = await drawMetrics('loss');
  const keys = (first && first.available_keys.length) ? first.available_keys : ['loss'];
  $('#metric-key').innerHTML = keys.map((k) =>
    `<option value="${esc(k)}" ${k === 'loss' ? 'selected' : ''}>${esc(k)}</option>`).join('');
  $('#metric-key').onchange = (e) => drawMetrics(e.target.value);

  // -- logs pane (inline errors: an unmanaged run has no container to read)
  const refreshLogs = async () => {
    try {
      const data = await api(`${base}/logs?tail=200`, { quiet: true });
      $('#logs').textContent = data.lines.join('\n') || '(no output)';
    } catch (err) {
      const detailMsg = (err.errors || []).map((e) => `${e.code}: ${e.message}`).join('; ');
      $('#logs').textContent = detailMsg || 'logs unavailable';
    }
  };
  $('#logs-refresh').onclick = refreshLogs;
  refreshLogs();

  renderArtifacts(pid, run);
}

/** True when an artifact id/kind suggests a media type the browser can preview. */
function looksLike(artifact, exts, kindHint) {
  const id = String(artifact.artifact_id || '').toLowerCase();
  const kind = String(artifact.kind || '').toLowerCase();
  return exts.some((ext) => id.includes(ext)) || (kindHint && kind.includes(kindHint));
}

async function renderArtifacts(pid, run) {
  const gallery = $('#gallery');
  let data;
  try {
    data = await api(`/v1/projects/${encodeURIComponent(pid)}/artifacts`
      + `?run_name=${encodeURIComponent(run)}`, { quiet: true });
  } catch (err) {
    gallery.innerHTML = '<p class="note">artifacts unavailable</p>';
    return;
  }
  if (!data.artifacts.length) {
    gallery.innerHTML = '<p class="note">no artifacts recorded for this run</p>';
    return;
  }
  // Probe the first artifact's content once: a 403 means the server has no
  // --content-root (fail-closed content serving) — note it and skip media tags.
  // A 1-byte Range GET, not HEAD: FastAPI routes answer 405 to HEAD, and the
  // content endpoint is Range-capable so this transfers almost nothing.
  const contentUrl = (a) => `/v1/projects/${encodeURIComponent(pid)}/artifacts/`
    + `${encodeURIComponent(a.artifact_id)}/content`;
  let contentDisabled = false;
  try {
    const probe = await fetch(contentUrl(data.artifacts[0]),
      { headers: { Range: 'bytes=0-0' } });
    contentDisabled = probe.status === 403;
  } catch (err) { contentDisabled = true; }
  if (contentDisabled) $('#content-disabled-note').classList.remove('hidden');

  gallery.innerHTML = data.artifacts.map((a) => {
    const url = esc(contentUrl(a));
    let media = '';
    if (!contentDisabled) {
      if (looksLike(a, ['.mp4', '.webm'], 'video')) {
        media = `<video controls preload="metadata" src="${url}"></video>`;
      } else if (looksLike(a, ['.png', '.jpg', '.jpeg', '.gif', '.webp'], 'image')) {
        media = `<img loading="lazy" src="${url}" alt="${esc(a.artifact_id)}">`;
      } else {
        media = `<div class="refs"><a href="${url}" download>download</a></div>`;
      }
    }
    return `<div class="card">
      <span class="chip">${esc(a.kind || 'artifact')}</span>
      ${a.artifact_class ? `<span class="st">${esc(a.artifact_class)}</span>` : ''}
      <div class="refs mono">${esc(a.artifact_id)}</div>
      ${media}
    </div>`;
  }).join('');
}

// -------------------------------------------------------------------- boot

window.addEventListener('hashchange', render);
render();
