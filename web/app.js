/* Personal CA — local UI. Vanilla JS, no build step. */
'use strict';

const TOKEN = document.querySelector('meta[name="ca-token"]').content;
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
const fmt = (x, d = 2) => (x === null || x === undefined || Number.isNaN(x)) ? '—' : Number(x).toLocaleString('en-IN', { minimumFractionDigits: d, maximumFractionDigits: d });
const dt = (s) => s ? new Date(s).toLocaleString('en-IN', { dateStyle: 'medium', timeStyle: 'short' }) : '—';
const titleCase = (s) => String(s || '').replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* storage blocked */ } },
};

async function api(path, opts = {}) {
  const o = { ...opts, headers: { 'content-type': 'application/json', ...(opts.headers || {}) } };
  if (o.method && o.method !== 'GET') o.headers['x-ca-token'] = TOKEN;
  if (o.body && typeof o.body !== 'string') o.body = JSON.stringify(o.body);
  const r = await fetch(path, o);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || j.error || `HTTP ${r.status}`);
  return j;
}

function toast(msg) {
  const t = $('#toast'); t.textContent = msg; t.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), 2600);
}

/* ---------------------------------------------------------------- theme */
(function theme() {
  const saved = store.get('ca-theme', null);
  if (saved) document.documentElement.dataset.theme = saved;
  $('#theme-btn').onclick = () => {
    const cur = document.documentElement.dataset.theme || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    const next = cur === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    store.set('ca-theme', next);
  };
})();

let META = null, HEALTH = null;
const DECISION_TEXT = { buy: 'Buy', wait: 'Wait', avoid: 'Avoid', insufficient_data: 'Insufficient data', hold: 'Hold', reduce: 'Reduce', exit: 'Exit' };
const STAGE_ROLE = { quant: 'quant', news_brain: 'news', forensic: 'forensic', head_draft: 'head', critic: 'critic', resolution: 'head', filings: 'discovery', news: 'discovery' };
const FIX_HINTS = {
  demo_mode: 'DEMO uses a fictional company. Run in Live mode for a real decision.',
  policies_confirmed: 'Review config/horizons.yaml, config/rules.yaml and config/user.yaml, then set policies_confirmed: true.',
  surveillance: 'Surveillance status must be known (NSE ASM/GSM lists), or recorded with a source in data/manual/<SYMBOL>.yaml.',
  liquidity: 'Average traded value is below the liquidity floor.',
  stale_data: 'Price is stale. Connect Kite or run `ca ingest-bhav`.',
  data_quality: 'A probable corporate action or data conflict must be resolved first.',
  pledge: 'Promoter pledge must be established (filing or manual facts file).',
  governance: 'Forensic coverage too low for a governance score, or score below minimum. Another loop or a facts file can raise coverage.',
  audit: 'Auditor opinion not established — add the annual report evidence.',
  required_evidence: 'A required report, the critique, or a blocker test is missing/unresolved.',
  reward_risk: 'Reward/risk below the policy minimum for short-term buys.',
  benchmark: 'After-tax comparison with the Nifty 50 ETF assumption failed or could not be made (verify config/tax_rates.yaml).',
  plan_validity: 'Entry/stop/target levels are inconsistent.',
  bearish_pattern: 'A confirmed bearish pattern blocks short-term buys.',
};
const roleModel = (role) => (HEALTH && HEALTH.models && HEALTH.models[role]) || '';

/* --------------------------------------------------------------- router */
const ROUTES = { '': viewHome, runs: viewRuns, run: viewRun, inbox: viewInbox, source: viewSource, portfolio: viewPortfolio, system: viewSystem, et: viewET, batch: viewBatch, news: viewNews };
let CLEANUP = null;
async function route() {
  if (CLEANUP) { CLEANUP(); CLEANUP = null; }
  const [path] = location.hash.replace(/^#\/?/, '').split('?');
  const [name, ...rest] = path.split('/');
  $$('#nav a').forEach((a) => a.classList.toggle('active', (a.dataset.route === (name || 'home')) || (name === 'run' && a.dataset.route === 'runs') || (name === 'source' && a.dataset.route === 'inbox') || (name === 'batch' && a.dataset.route === 'et')));
  const fn = ROUTES[name] || viewHome;
  closeDrawer();
  try { await fn(...rest); } catch (e) { $('#app').innerHTML = `<div class="card notice bad">${esc(e.message)}</div>`; }
  window.scrollTo(0, 0);
}
window.addEventListener('hashchange', route);

async function refreshHealth() {
  try {
    HEALTH = await api('/api/health');
    const b = HEALTH.budget;
    const pill = $('#budget-pill');
    pill.textContent = `LLM $${b.spent_usd.toFixed(2)} / $${b.cap_usd.toFixed(0)}`;
    pill.className = 'pill ' + (b.remaining_usd < b.cap_usd * 0.15 ? 'bad' : 'info');
    pill.title = 'Monthly LLM budget (enforced in code)' + (b.calls_with_unknown_price ? ` · ${b.calls_with_unknown_price} call(s) with unknown price` : '');
  } catch (e) { /* ignore */ }
}

/* ----------------------------------------------------------------- home */
async function viewHome() {
  await refreshHealth();
  const h = HEALTH || { panel_missing_keys: ['ANTHROPIC_API_KEY'], analysis: { default_loops: 1, max_loops: 3 }, vendor_separation: {} };
  const st = { horizon: 'medium', mode: h.panel_missing_keys.length ? 'demo' : 'live', action: 'new_purchase', loops: h.analysis.default_loops };
  const runs = await api('/api/runs?limit=6');
  const vs = h.vendor_separation || {};
  $('#app').innerHTML = `
  <div class="hero">
    <div class="card">
      <h1>Analyse a stock</h1>
      <p class="muted">Three independent specialist brains study one frozen evidence bundle. A Head CA drafts, a Critic attacks, and code enforces the rules. Every claim links to its source.</p>
      ${h.panel_missing_keys.length ? `<div class="notice">Live mode needs: <b>${h.panel_missing_keys.map(esc).join(', ')}</b> (<code>ca secrets set NAME</code>). Demo mode runs the whole pipeline on a fictional company.</div>` : ''}
      <form class="analyse-form" id="f">
        <div><label for="sym">NSE symbol</label>
          <div class="sym-wrap"><input id="sym" type="text" autocomplete="off" placeholder="e.g. TCS, INFY, HDFCBANK" spellcheck="false"><div class="suggest" id="sug" hidden></div></div></div>
        <div class="row">
          <div><label>Horizon</label><div class="seg" id="hz">
            <button type="button" data-v="short">Short ≈3m</button><button type="button" data-v="medium">Medium ≈6m</button><button type="button" data-v="long">Long ≈12m+</button></div></div>
          <div><label>Mode</label><div class="seg" id="md"><button type="button" data-v="live">Live</button><button type="button" data-v="demo">Demo</button></div></div>
          <div><label title="Each extra loop re-fetches all evidence, searches for the previous loop's open questions, and re-runs the panel">Analysis loops ⓘ</label><div class="seg" id="lp">
            ${Array.from({ length: h.analysis.max_loops }, (_, i) => `<button type="button" data-v="${i + 1}">${i + 1}</button>`).join('')}</div></div>
        </div>
        <div><label for="act">Intended action</label>
          <select id="act"><option value="new_purchase">New purchase</option><option value="additional_purchase">Add to position</option>
          <option value="continue_holding">Review a holding</option><option value="reduce_exit">Reduce / exit</option></select></div>
        <div class="small muted">Panel: ${['quant', 'news', 'forensic', 'head', 'critic'].map((r) => `<b>${titleCase(r)}</b> ${esc(roleModel(r))}`).join(' · ')}
          <span class="pill ${vs.status === 'separated' ? 'good' : vs.status === 'partial' ? 'info' : 'warn'}">vendor separation: ${esc(vs.status || '—')}</span> <a href="#/system">change</a></div>
        <div class="row"><button class="btn primary" id="go" type="submit">Run the panel →</button><span class="muted small" id="est"></span></div>
      </form>
    </div>
    <div class="card">
      <h3>What happens when you click run</h3>
      <ol class="how">
        <li><div><b>Evidence</b> — prices from Kite/NSE, technicals computed in code, NSE surveillance lists and announcements, tiered news and your ET Prime captures. Then the bundle is <i>frozen</i> and hashed.</div></li>
        <li><div><b>Three brains, blind to each other</b> — Quant (valuation & setup), News & Narrative (what changed), Forensic CA (can the numbers be trusted).</div></li>
        <li><div><b>Head & Critic</b> — reports are anonymised; the Head drafts; the Critic raises 0–7 objections with settling tests; the Head must answer each.</div></li>
        <li><div><b>Rules in code</b> — vetoes, sizing, tax and a consistency check. No prompt can override them.</div></li>
        <li><div><b>Loops (optional)</b> — loop 2+ re-fetches everything, hunts for what loop 1 could not settle, and re-runs the panel. You see each loop and what changed.</div></li>
      </ol>
      <p class="small muted" style="margin-top:12px">Every citation chip is checked by code: <span class="cite verified">ev ✓</span> quote found verbatim · <span class="cite id_only">ev</span> id only · <span class="cite quote_mismatch">ev ✗</span> not supported.</p>
    </div>
  </div>
  <div class="card"><div class="row"><h3 style="margin:0">Recent runs</h3><span class="spacer"></span><a href="#/runs">All runs →</a></div>${runsTable(runs)}</div>`;
  const est = () => {
    $('#est').textContent = st.mode === 'demo' ? `Demo: fictional company, no model calls, $0 · ${st.loops} loop(s)`
      : `Live: about ${8 * st.loops} model calls and up to ${9 * st.loops} web searches over ${st.loops} loop(s); budget reserved first`;
    $('#sym').disabled = st.mode === 'demo';
    $('#sym').placeholder = st.mode === 'demo' ? 'DEMOCO (fictional)' : 'e.g. TCS, INFY, HDFCBANK';
  };
  const seg = (id, key, num) => {
    const el = $(id);
    const paint = () => $$('button', el).forEach((b) => b.classList.toggle('on', String(b.dataset.v) === String(st[key])));
    el.onclick = (e) => { if (e.target.dataset.v) { st[key] = num ? Number(e.target.dataset.v) : e.target.dataset.v; paint(); est(); } };
    paint();
  };
  const sym = $('#sym'), sug = $('#sug');
  try { const pre = sessionStorage.getItem('ca-sym'); if (pre) { sym.value = pre; sessionStorage.removeItem('ca-sym'); st.mode = 'live'; } } catch (e) { /* ignore */ }
  seg('#hz', 'horizon'); seg('#md', 'mode'); seg('#lp', 'loops', true); est();
  let tmr;
  sym.oninput = () => {
    clearTimeout(tmr);
    tmr = setTimeout(async () => {
      const q = sym.value.trim(); if (!q) { sug.hidden = true; return; }
      const rows = await api('/api/symbols?q=' + encodeURIComponent(q)).catch(() => []);
      sug.innerHTML = rows.map((r) => `<div data-s="${esc(r.symbol)}"><b>${esc(r.symbol)}</b> <span class="muted">${esc(r.name)}</span></div>`).join('');
      sug.hidden = !rows.length;
    }, 160);
  };
  sug.onclick = (e) => { const d = e.target.closest('[data-s]'); if (d) { sym.value = d.dataset.s; sug.hidden = true; } };
  $('#f').onsubmit = async (e) => {
    e.preventDefault();
    st.action = $('#act').value;
    if (st.mode === 'live' && !sym.value.trim()) { toast('Enter an NSE symbol'); return; }
    $('#go').disabled = true;
    try {
      const r = await api('/api/runs', { method: 'POST', body: { symbol: sym.value.trim().toUpperCase(), horizon: st.horizon, action: st.action, mode: st.mode, loops: st.loops } });
      location.hash = `#/run/${r.run_id}`;
    } catch (err) { toast(err.message); $('#go').disabled = false; }
  };
}

function decisionPill(d, actionable) {
  if (!d) return '<span class="pill">—</span>';
  const cls = d === 'buy' ? (actionable ? 'good' : 'warn') : d === 'wait' ? 'warn' : ['avoid', 'exit', 'reduce'].includes(d) ? 'bad' : '';
  return `<span class="pill ${cls}">${esc(DECISION_TEXT[d] || d)}${actionable ? ' · actionable' : ''}</span>`;
}
const statusPill = (s) => `<span class="pill ${s === 'completed' ? 'good' : s === 'failed' ? 'bad' : s === 'running' ? 'info' : 'warn'}">${esc(s)}</span>`;

function runsTable(runs) {
  if (!runs.length) return '<div class="empty">No runs yet. Try a Demo run to see the whole pipeline.</div>';
  return `<div class="tbl-wrap"><table><thead><tr><th>When</th><th>Stock</th><th>Horizon</th><th>Mode</th><th>Status</th><th>Decision</th><th>Cost</th><th>Summary</th></tr></thead><tbody>
  ${runs.map((r) => `<tr><td><a href="#/run/${esc(r.run_id)}">${dt(r.started_at)}</a></td><td><b>${esc(r.symbol)}</b></td><td>${esc(r.horizon)}</td>
  <td>${r.mode === 'demo' ? '<span class="pill bad">DEMO</span>' : '<span class="pill">live</span>'}</td><td>${statusPill(r.status)}</td>
  <td>${decisionPill(r.final_decision, r.actionable)}</td><td class="mono">$${(r.cost_usd || 0).toFixed(3)}</td><td class="small muted">${esc((r.summary || '').slice(0, 110))}</td></tr>`).join('')}
  </tbody></table></div>`;
}

async function viewRuns() {
  const runs = await api('/api/runs?limit=200');
  $('#app').innerHTML = `<div class="card"><h2>Runs ledger</h2><p class="muted">Every run, partial or complete, is kept. Failed and partial runs never appear as completed verdicts.</p>${runsTable(runs)}</div>`;
}

/* ------------------------------------------------------------------ run */
let RUN = null;

function newLoopState() {
  const s = {};
  META.stages.forEach((x) => (s[x.id] = { status: 'pending', msg: '', logs: [], dur: null, data: null }));
  return { stages: s, status: 'pending', msg: '', questions: [] };
}

async function viewRun(id) {
  META = META || await api('/api/meta');
  RUN = { id, loops: {}, total: 1, viewLoop: 1, pinned: false, open: new Set(), events: [], detail: null, bundle: null,
    evIndex: {}, done: false, tab: 'overview', resultLoop: null };
  $('#app').innerHTML = `<div id="run-head"></div><div class="card"><div id="loop-tabs"></div><div class="phases" id="phases"></div><div class="pipeline" id="pipeline"></div></div><div id="results"></div>`;
  RUN.detail = await api(`/api/runs/${id}`);
  setBundle(RUN.detail.bundle);
  renderRunHead();
  const es = new EventSource(`/api/runs/${id}/events`);
  es.onmessage = (m) => { const e = JSON.parse(m.data); if (RUN.events.find((x) => x.seq === e.seq)) return; applyEvent(e); };
  es.addEventListener('close', () => es.close());
  es.onerror = () => { if (RUN.done) es.close(); };
  CLEANUP = () => es.close();
  renderTracker();
}

function setBundle(b) {
  if (!b) return;
  RUN.bundle = b; RUN.evIndex = {};
  b.evidence.forEach((e) => (RUN.evIndex[e.id] = e));
}
function loopState(n) { if (!RUN.loops[n]) RUN.loops[n] = newLoopState(); return RUN.loops[n]; }

async function applyEvent(e) {
  RUN.events.push(e);
  const L = loopState(e.loop || 1);
  const s = e.stage && L.stages[e.stage];
  if (e.type === 'run_start') { RUN.start = e.data; RUN.total = (e.data && e.data.loops) || 1; }
  if (e.type === 'loop_start') { L.status = 'running'; L.msg = e.message; L.questions = (e.data && e.data.open_questions) || []; if (!RUN.pinned) RUN.viewLoop = e.loop; }
  if (e.type === 'loop_end') { L.status = 'completed'; L.msg = e.message; L.summary = e.data && e.data.summary; }
  if (e.type === 'stage_start' && s) { s.status = 'running'; s.msg = e.message; }
  if (e.type === 'log' && s) s.logs.push({ ts: e.ts, m: e.message });
  if (e.type === 'stage_end' && s) {
    s.status = e.status; s.msg = e.message; s.dur = e.duration_ms; s.data = e.data;
    if (e.stage === 'bundle') { const d = await api(`/api/runs/${RUN.id}`); setBundle(d.bundle); }
  }
  if (e.type === 'run_end') {
    RUN.done = true; RUN.endStatus = e.status; RUN.endMsg = e.message;
    RUN.detail = await api(`/api/runs/${RUN.id}`); setBundle(RUN.detail.bundle);
    renderRunHead(); renderResults(); refreshHealth();
  }
  scheduleTracker();
}
let _raf = 0;
function scheduleTracker() { if (_raf) return; _raf = requestAnimationFrame(() => { _raf = 0; renderTracker(); }); }

function renderRunHead() {
  const r = RUN.detail.run, b = RUN.bundle;
  const loops = (RUN.start && RUN.start.loops) || (RUN.detail.loops || []).length || 1;
  $('#run-head').innerHTML = `<div class="run-head card"><div style="flex:1;min-width:240px">
    <div class="row"><h1>${esc(r.symbol)}</h1>${r.mode === 'demo' ? '<span class="pill bad">DEMO · fictional</span>' : '<span class="pill">live</span>'} ${statusPill(r.status)}</div>
    <div class="muted">${esc(b ? b.company_name : '')}${b && b.sector ? ' · ' + esc(b.sector) : ''}</div>
    <div class="run-meta"><span class="pill">Horizon: ${esc(r.horizon)}</span><span class="pill">Action: ${esc(titleCase(r.action))}</span>
    <span class="pill">${loops} loop${loops > 1 ? 's' : ''}</span><span class="pill">Decision time ${dt(r.started_at)}</span>
    ${b ? `<span class="pill mono" title="Frozen evidence bundle hash (latest loop)">bundle ${esc(b.bundle_id.slice(0, 12))}</span>` : ''}
    <span class="pill mono">$${(r.cost_usd || 0).toFixed(4)}</span></div></div>
    <div class="row"><a class="btn sm" href="#/">New analysis</a></div></div>
    ${RUN.done && RUN.endStatus === 'failed' ? `<div class="card notice bad"><b>Run failed:</b> ${esc(RUN.endMsg)}</div>` : ''}`;
}

function renderTracker() {
  if (!RUN || !$('#phases')) return;
  const total = Math.max(RUN.total, Object.keys(RUN.loops).length, 1);
  $('#loop-tabs').innerHTML = total > 1 ? `<div class="loop-tabs">${Array.from({ length: total }, (_, i) => {
    const n = i + 1, L = RUN.loops[n] || { status: 'pending' };
    const ic = L.status === 'completed' ? '✓' : L.status === 'running' ? '●' : '○';
    return `<button data-loop="${n}" class="${RUN.viewLoop === n ? 'on' : ''} ${L.status}">${ic} Loop ${n}</button>`;
  }).join('')}<span class="small muted" style="margin-left:8px">${esc((RUN.loops[RUN.viewLoop] || {}).msg || '')}</span></div>
  ${(RUN.loops[RUN.viewLoop] || {}).questions && RUN.loops[RUN.viewLoop].questions.length ? `<details class="small" style="margin:6px 0 10px"><summary>Open questions carried into loop ${RUN.viewLoop} (${RUN.loops[RUN.viewLoop].questions.length})</summary><ul>${RUN.loops[RUN.viewLoop].questions.map((q) => `<li>${esc(q)}</li>`).join('')}</ul></details>` : ''}` : '';
  $$('#loop-tabs [data-loop]').forEach((b) => (b.onclick = () => { RUN.viewLoop = Number(b.dataset.loop); RUN.pinned = true; renderTracker(); }));
  const L = loopState(RUN.viewLoop);
  const byPhase = {};
  META.stages.forEach((s) => (byPhase[s.phase] = byPhase[s.phase] || []).push(s));
  $('#phases').innerHTML = META.phases.map((p) => {
    const ss = byPhase[p.id].map((s) => L.stages[s.id].status);
    const done = ss.filter((x) => !['pending', 'running'].includes(x)).length;
    const running = ss.includes('running'), fail = ss.includes('failed');
    const cls = done === ss.length ? (fail ? 'done fail' : 'done') : running ? 'active' : '';
    return `<div class="phase ${cls}"><div class="t"><span>${esc(p.title)}</span><span class="muted small">${done}/${ss.length}</span></div><div class="bar"><i style="width:${(done / ss.length) * 100}%"></i></div></div>`;
  }).join('');
  $('#pipeline').innerHTML = META.phases.map((p) => `
    <div><div class="lane-title"><h4 style="margin:0">${esc(p.title)}</h4><span class="line"></span>${p.id === 'brains' ? '<span class="small muted">run in parallel · blind to each other</span>' : ''}</div>
    <div class="stages ${p.id === 'brains' ? 'parallel' : ''}">${byPhase[p.id].map((s) => stageCard(s, L)).join('')}</div></div>`).join('');
  $$('.stage-h', $('#pipeline')).forEach((h) => (h.onclick = () => { const k = RUN.viewLoop + ':' + h.dataset.id; RUN.open.has(k) ? RUN.open.delete(k) : RUN.open.add(k); renderTracker(); }));
}

function stageCard(s, L) {
  const st = L.stages[s.id];
  const key = RUN.viewLoop + ':' + s.id;
  const icon = { completed: '✓', failed: '!', skipped: '↷', degraded: '!' }[st.status] || '';
  const open = RUN.open.has(key) || (st.status === 'running');
  const logs = RUN.open.has(key) ? st.logs : st.logs.slice(-3);
  const dur = st.dur != null ? (st.dur / 1000).toFixed(1) + 's' : st.status === 'running' ? 'running…' : '';
  const role = STAGE_ROLE[s.id];
  const who = s.who === 'code' ? '⚙ code' : (role && roleModel(role) ? '✦ ' + roleModel(role) : '✦ model') + (s.who === 'code+claude' ? ' + ⚙ code' : '');
  return `<div class="stage ${st.status}"><div class="stage-h" data-id="${s.id}"><span class="st-icon ${st.status}">${icon}</span>
    <span><span class="name">${esc(s.title)}</span> <span class="who">${esc(who)}</span></span><span class="dur">${dur} ${st.logs.length ? (RUN.open.has(key) ? '▾' : '▸') : ''}</span></div>
    ${st.msg && st.status !== 'pending' ? `<div class="msg">${esc(st.msg)}</div>` : ''}
    ${open && logs.length ? `<div class="logs">${logs.map((l) => `<div><span class="ts">${esc(l.ts.slice(11, 19))}</span>${esc(l.m)}</div>`).join('')}</div>` : ''}</div>`;
}

/* --------------------------------------------------------------- results */
function stageData(id, loop) { const L = RUN.loops[loop || currentLoopNo()]; return L && L.stages[id] && L.stages[id].data; }
function currentLoopNo() { return RUN.resultLoop || (RUN.detail.loops || []).length || 1; }
function currentRecord() {
  const loops = RUN.detail.loops || [];
  const n = currentLoopNo();
  const final = RUN.detail.decision_record;
  if (!loops.length || n === loops.length) return final;
  return loops[n - 1];
}
function currentBundle(dr) { return (RUN.detail.bundles && RUN.detail.bundles[dr.bundle_id]) || RUN.bundle; }

function citeChip(c) {
  const st = c.status || 'id_only';
  const mark = st === 'verified' ? ' ✓' : st === 'id_only' ? '' : ' ✗';
  const e = RUN.evIndex[c.evidence_id];
  const title = e ? `${e.publisher} · ${e.tier}` : 'Not in bundle';
  return `<span class="cite ${esc(st)}" title="${esc(title)} — ${esc(st.replace('_', ' '))}" data-ev="${esc(c.evidence_id)}" data-pid="${esc(c.passage_id || '')}" data-q="${esc(c.quote || '')}" data-st="${esc(st)}">${esc(c.evidence_id)}${mark}</span>`;
}
const chips = (cs) => (cs || []).map(citeChip).join('');

function statementsHtml(list) {
  if (!list || !list.length) return '<div class="muted small">None.</div>';
  return `<div class="stmts">${list.map((s) => `<div class="stmt"><span class="kind">${esc(s.kind)}</span><span>${esc(s.text)} ${chips(s.cites)}</span></div>`).join('')}</div>`;
}

function renderResults() {
  const final = RUN.detail.decision_record;
  if (!final) { $('#results').innerHTML = ''; return; }
  const loops = RUN.detail.loops || [];
  const dr = currentRecord();
  setBundle(currentBundle(dr));
  const isFinal = dr === final;
  const fa = dr.final_action, hp = dr.head_proposal;
  const blocking = dr.rule_results.filter((r) => ['blocked', 'unknown'].includes(r.result));
  const plan = fa.plan || ((hp.entry_low && hp.stop) ? hp : null);
  const sz = fa.sizing && fa.sizing.status === 'ok' ? fa.sizing : null;
  const rr = dr.calculations && dr.calculations.reward_risk;
  const draftStage = stageData('head_draft'), finalStage = stageData('resolution');
  const hpA = (finalStage && finalStage.final) || (draftStage && draftStage.draft) || hp;
  $('#results').innerHTML = `
  ${loops.length > 1 ? loopComparison(loops) : ''}
  <div class="card"><div class="verdict">
    <div><div class="decision-box ${esc(fa.decision)}"><div class="small">${isFinal ? 'Permitted action' : `Loop ${dr.loop} result (superseded)`}</div><div class="d">${esc(DECISION_TEXT[fa.decision] || fa.decision)}</div>
      <div class="small" style="margin-top:4px">${fa.actionable ? '✅ Actionable under your rules' : 'Research only — not actionable'}</div></div>
      <div class="kv" style="margin-top:12px"><div><span>Last close</span><b>${fmt(dr.price_at_decision)}</b></div><div><span>Head score</span><b>${hp.head_score ?? '—'}</b></div>
      <div><span>Governance</span><b>${dr.governance.score ?? 'withheld'}</b></div><div><span>Evidence</span><b>${esc(hp.evidence_sufficiency || '—')}</b></div></div></div>
    <div>
      <div class="flow"><span class="pill">Head draft: ${esc(DECISION_TEXT[(dr.head_draft || {}).decision] || '—')}</span><span class="arrow">→</span>
        <span class="pill">After critique: ${esc(DECISION_TEXT[hp.decision] || hp.decision)}</span><span class="arrow">→</span>
        <span class="pill ${fa.actionable ? 'good' : 'warn'}">Rules: ${esc(DECISION_TEXT[fa.decision] || fa.decision)}</span></div>
      <h2>${esc(hp.summary)}</h2>
      ${statementsHtml(hpA.reasons)}
      ${dr.adjustments.length ? `<div class="notice" style="margin-top:10px"><b>Adjusted by rules:</b> ${dr.adjustments.map(esc).join('<br>')}</div>` : ''}
      ${blocking.length ? `<h4 style="margin-top:14px">What stands between this and an actionable decision</h4><div class="checklist">${blocking.map((r) => `<div><span class="res ${r.result}">${r.result === 'blocked' ? '✖' : '?'}</span><span><b>${esc(titleCase(r.rule))}</b> — ${esc(r.detail)}<br><span class="muted small">${esc(FIX_HINTS[r.rule] || '')}</span></span></div>`).join('')}</div>` : ''}
      ${plan ? `<h4 style="margin-top:14px">${fa.actionable ? 'Plan' : 'Conditional levels (not actionable)'}</h4><div class="kv">
        <div><span>Entry zone</span><b>${fmt(plan.entry_low, 0)}–${fmt(plan.entry_high, 0)}</b></div><div><span>Stop</span><b>${fmt(plan.stop, 0)}</b></div>
        <div><span>Target</span><b>${fmt(plan.target_low, 0)}${plan.target_high ? '–' + fmt(plan.target_high, 0) : ''}</b></div><div><span>Reward / risk</span><b>${rr ?? '—'}</b></div>
        ${sz ? `<div><span>Quantity</span><b>${sz.qty} sh</b></div><div><span>Planned loss at stop</span><b>₹${fmt(sz.planned_loss_at_stop_inr, 0)}</b></div>` : ''}</div>
        ${sz ? `<p class="small muted">Tranches: ${sz.tranches.map((t) => `${t.tranche} ${t.qty} @ ${fmt(t.price, 0)}`).join(' · ')}. ${esc(sz.note)}</p>` : ''}` : ''}
      ${hp.entry_conditions && hp.entry_conditions.length ? `<p class="small"><b>Entry conditions:</b> ${hp.entry_conditions.map(esc).join('; ')}</p>` : ''}
      ${hp.invalidation_conditions && hp.invalidation_conditions.length ? `<p class="small"><b>Invalidated if:</b> ${hp.invalidation_conditions.map(esc).join('; ')}</p>` : ''}
      ${hp.material_dissent && hp.material_dissent.length ? `<p class="small"><b>Material dissent kept:</b> ${hp.material_dissent.map(esc).join('; ')}</p>` : ''}
    </div></div></div>
  ${topSourcesCard(dr)}
  <div class="card">${loops.length > 1 ? `<div class="row" style="margin-bottom:8px"><span class="small muted">Showing details for</span><div class="seg" id="rl">${loops.map((l) => `<button data-l="${l.loop}" class="${l.loop === currentLoopNo() ? 'on' : ''}">Loop ${l.loop}${l.loop === loops.length ? ' (final)' : ''}</button>`).join('')}</div></div>` : ''}
  <div class="tabs" id="tabs">${[['overview', 'Overview & chart'], ['analysts', 'Analyst reports'], ['panel', 'Head ↔ Critic'], ['rules', 'Rules'], ['sources', `Sources (${RUN.bundle ? RUN.bundle.evidence.length : 0})`], ['audit', 'Audit trail']].map(([k, t]) => `<button data-t="${k}" class="${RUN.tab === k ? 'on' : ''}">${t}</button>`).join('')}</div><div id="tab-body"></div></div>`;
  $('#tabs').onclick = (e) => { const t = e.target.dataset.t; if (t) { RUN.tab = t; $$('#tabs button').forEach((b) => b.classList.toggle('on', b.dataset.t === t)); renderTab(); } };
  const rl = $('#rl'); if (rl) rl.onclick = (e) => { const l = e.target.dataset.l; if (l) { RUN.resultLoop = Number(l); renderResults(); } };
  const ts = $('#topn'); if (ts) ts.onclick = (e) => { const n = e.target.dataset.n; if (n) { store.set('ca-topn', Number(n)); renderResults(); } };
  renderTab();
}

function loopComparison(loops) {
  const row = (label, f) => `<tr><td><b>${label}</b></td>${loops.map((l) => `<td>${f(l)}</td>`).join('')}</tr>`;
  const diffPill = (l, i, key) => (i > 0 && loops[i - 1][key] !== l[key]) ? ' <span class="pill info">changed</span>' : '';
  return `<div class="card"><h3>What changed across loops</h3><p class="small muted">Each loop re-fetched all evidence and searched for the previous loop's open questions. The final loop's result is the issued decision; earlier loops are kept for audit.</p>
  <div class="tbl-wrap"><table><thead><tr><th></th>${loops.map((l) => `<th>Loop ${l.loop}${l.loop === loops.length ? ' · final' : ''}</th>`).join('')}</tr></thead><tbody>
  ${row('Evidence items', (l) => `${l.evidence_count}${l.new_evidence ? ` <span class="pill info">+${l.new_evidence} new/changed</span>` : ''}`)}
  ${row('Questions searched for', (l) => l.asked.length ? `<details><summary class="small">${l.asked.length} question(s)</summary><ul class="small">${l.asked.map((q) => `<li>${esc(q)}</li>`).join('')}</ul></details>` : '<span class="muted small">— (first loop)</span>')}
  ${row('Head draft → final', (l) => `${decisionPill(l.draft_decision)} → ${decisionPill(l.head_decision)}`)}
  ${row('Permitted by rules', (l) => { const i = loops.indexOf(l); return decisionPill(l.permitted, l.actionable) + diffPill(l, i, 'permitted'); })}
  ${row('Critic objections', (l) => `${l.objections} · open blockers: ${esc(l.open_blockers.join(', ') || 'none')}`)}
  ${row('Governance (coverage)', (l) => `${l.governance ?? 'withheld'} <span class="muted small">(${Math.round((l.governance_coverage || 0) * 100)}%)</span>`)}
  ${row('Citation integrity', (l) => Object.entries(l.citation_scores || {}).map(([r, s]) => `${esc(titleCase(r))} ${(s * 100).toFixed(0)}%`).join(' · '))}
  ${row('Rule blockers', (l) => `<span class="small">${esc(l.rule_blockers.join(', ') || 'none')}</span>`)}
  ${row('Left open', (l) => `<span class="small">${l.open_questions.length} question(s)</span>`)}
  ${row('Head summary', (l) => `<span class="small">${esc(l.summary)}</span>`)}
  </tbody></table></div></div>`;
}

function topSourcesCard(dr) {
  const all = dr.top_sources || [];
  if (!all.length) return '';
  const def = (HEALTH && HEALTH.display && HEALTH.display.top_sources) || 8;
  const n = store.get('ca-topn', def);
  const tiers = (META && META.tiers) || {};
  return `<div class="card"><div class="row"><h3 style="margin:0">Best sources for this decision</h3><span class="spacer"></span>
    <span class="small muted">show</span><div class="seg" id="topn">${[5, 8, 10].map((k) => `<button data-n="${k}" class="${k === n ? 'on' : ''}">${k}</button>`).join('')}</div></div>
    <p class="small muted">Ranked by source tier × how often the panel relied on it (verified quotes count most) × recency. Computed prices/technicals are excluded. All sources are in the Sources tab.</p>
    <ol class="top-sources">${all.slice(0, n).map((s) => `<li><div class="row"><span class="tier ${s.tier}">${esc(tiers[s.tier] ? tiers[s.tier].label : s.tier)}</span>
      <a href="javascript:void 0" class="src-open" data-ev="${esc(s.evidence_id)}"><b>${esc(s.title)}</b></a><span class="small muted">${esc(s.publisher)}</span>
      ${s.url ? `<a class="small" href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">open ↗</a>` : ''}</div>
      <div class="small muted">${esc(s.why)}</div></li>`).join('')}</ol></div>`;
}

function renderTab() {
  const dr = currentRecord(), b = currentBundle(dr), el = $('#tab-body');
  const T = { overview: tabOverview, analysts: tabAnalysts, panel: tabPanel, rules: tabRules, sources: tabSources, audit: tabAudit }[RUN.tab];
  el.innerHTML = T(dr, b);
  if (RUN.tab === 'overview') drawChart(dr, b);
  if (RUN.tab === 'sources') bindSourceFilter();
  if (RUN.tab === 'audit') { const c = $('#copy-wa'); if (c) c.onclick = () => navigator.clipboard.writeText(dr.whatsapp).then(() => toast('Copied')).catch(() => toast('Copy blocked — select the text')); }
}

function tabOverview(dr, b) {
  const f = (b.calculations || []).reduce((a, c) => ((a[c.name] = c.result), a), {});
  return `<div class="grid g2"><div><h3>Price, averages and plan levels</h3><div class="chart-legend" id="legend"></div><div class="chart-wrap" id="chart"></div>
    <p class="small muted">Source: ${esc(b.price.source || '—')}. Technicals computed in code from stored candles. Hover for daily values.</p></div>
    <div><h3>Key computed facts</h3><div class="kv">
      <div><span>Close</span><b>${fmt(f.close)}</b></div><div><span>SMA 200</span><b>${fmt(f.sma200)}</b></div><div><span>RSI 14</span><b>${fmt(f.rsi14, 1)}</b></div>
      <div><span>ATR 14</span><b>${fmt(f.atr14)}</b></div><div><span>52w high</span><b>${fmt(f.high_52w)}</b></div><div><span>52w low</span><b>${fmt(f.low_52w)}</b></div>
      <div><span>ADV20 (₹ cr)</span><b>${fmt(f.adv20_crore)}</b></div><div><span>3m return</span><b>${fmt(f.ret_3m_pct, 1)}%</b></div></div>
      <h4 style="margin-top:16px">Chart patterns</h4>${(b.patterns || []).length ? b.patterns.map((p) => `<div class="small"><b>${esc(titleCase(p.name))}</b> <span class="pill ${p.status === 'confirmed' ? (p.bias === 'bullish' ? 'good' : 'bad') : ''}">${esc(p.status)}</span> neckline ${fmt(p.neckline)} · target ${fmt(p.measured_target)} · stop ${fmt(p.pattern_stop)}</div>`).join('') : '<div class="muted small">None detected.</div>'}
      <h4 style="margin-top:16px">Evidence coverage</h4><div class="row">${Object.entries(b.coverage || {}).map(([k, v]) => `<span class="pill ${['available', 'not_listed'].includes(v) || v > 0 ? 'good' : ['missing', 'unknown', 0].includes(v) ? 'warn' : ''}">${esc(titleCase(k))}: ${esc(v)}</span>`).join('')}</div>
      ${(b.quality_flags || []).length ? `<h4 style="margin-top:16px">Quality flags</h4><ul class="small">${b.quality_flags.map((q) => `<li>${esc(q)}</li>`).join('')}</ul>` : ''}
      ${(dr.learn || []).length ? `<h4 style="margin-top:16px">Learn</h4>${dr.learn.map((l) => `<span class="pill info">${esc(titleCase(l))}</span> `).join('')}` : ''}
    </div></div>`;
}

function tabAnalysts(dr) {
  const roles = [['quant', 'quant', 'Quant'], ['news', 'news_brain', 'News & Narrative'], ['forensic', 'forensic', 'Forensic CA']];
  return `<div class="legend-cites" style="margin-bottom:12px"><span><span class="cite verified">ev ✓</span> quote verified verbatim by code</span><span><span class="cite id_only">ev</span> id only (weaker)</span><span><span class="cite quote_mismatch">ev ✗</span> unsupported / not found</span><span>Click any chip to open the source passage.</span></div>
  <div class="grid g3">${roles.map(([role, stg, name]) => {
    const d = stageData(stg, dr.loop || currentLoopNo()); const lbl = (dr.labels || {})[role];
    if (!d || !d.report) return `<div class="card analyst"><h3>${name}</h3><div class="notice bad">${esc(((RUN.loops[dr.loop || 1] || {}).stages || {})[stg]?.msg || 'Not available')}</div></div>`;
    const r = d.report, a = d.audit;
    return `<div class="card analyst"><h3>${name} <span class="pill">Analyst ${esc(lbl)}</span> <span class="pill ${r.specialist_conclusion === 'supportive' ? 'good' : r.specialist_conclusion === 'concerning' ? 'bad' : ''}">${esc(r.specialist_conclusion.replace('_', ' '))}</span></h3>
      <p><b>${esc(r.headline)}</b></p>
      <div class="small muted">Citation integrity <span class="score-bar"><i style="width:${(a.score * 100).toFixed(0)}%"></i></span> ${(a.score * 100).toFixed(0)}% · ${a.counts.verified} verified / ${a.total} · ${esc(d.model || '')} · ${esc(d.prompt_version || '')}</div>
      <h4 style="margin-top:10px">Findings</h4>${statementsHtml(r.statements)}
      ${r.red_flags.length ? `<h4>Red flags</h4>${statementsHtml(r.red_flags)}` : ''}
      <h4>Checks</h4><div class="tbl-wrap"><table><tbody>${r.checks.map((c) => `<tr><td><b>${esc(titleCase(c.name))}</b><div class="small muted">${esc(c.note)} ${chips(c.cites)}</div></td><td><span class="res ${c.result}">${esc(c.result.replace('_', ' '))}</span><div class="small muted">${esc(c.materiality)}</div></td></tr>`).join('')}</tbody></table></div>
      ${r.scenarios.length ? `<h4>Scenarios (assumptions; returns computed in code)</h4>${r.scenarios.map((s) => `<div class="small"><b>${esc(s.name)}</b> ${fmt(s.price_assumption)} — ${esc(s.rationale)} ${chips(s.cites)}</div>`).join('')}` : ''}
      ${r.missing_evidence.length ? `<h4>Missing evidence</h4><ul class="small">${r.missing_evidence.map((m) => `<li>${esc(m)}</li>`).join('')}</ul>` : ''}
      ${r.would_change_mind.length ? `<h4>Would change my view</h4><ul class="small">${r.would_change_mind.map((m) => `<li>${esc(m)}</li>`).join('')}</ul>` : ''}
    </div>`;
  }).join('')}</div>`;
}

function tabPanel(dr) {
  const n = dr.loop || currentLoopNo();
  const ds = stageData('head_draft', n), cs = stageData('critic', n);
  const draft = (ds && ds.draft) || dr.head_draft, res = dr.resolution || { rows: [] };
  const crit = (cs && cs.critique) || dr.critique;
  const objCites = {}; (crit ? crit.objections : []).forEach((o) => (objCites[o.id] = o.cites));
  return `<div class="grid g2"><div><h3>1 · Head draft <span class="small muted">${esc((ds && ds.model) || '')}</span></h3>${draft ? `<p>${decisionPill(draft.decision)} ${esc(draft.summary)}</p>${statementsHtml(draft.reasons)}
      ${draft.material_dissent.length ? `<p class="small"><b>Dissent:</b> ${draft.material_dissent.map(esc).join('; ')}</p>` : ''}` : '<div class="muted">No draft.</div>'}</div>
    <div><h3>3 · Final proposal</h3><p>${decisionPill(dr.head_proposal.decision)} ${esc(dr.head_proposal.summary)}</p>
      <p class="small muted">Open blockers after code resolution: <b>${esc(res.open_blockers.join(', ') || 'none')}</b>${res.open_major && res.open_major.length ? ` · unresolved major: ${esc(res.open_major.join(', '))}` : ''}</p></div></div>
    <h3 style="margin-top:12px">2 · Critic objections and settling tests <span class="small muted">${esc((cs && cs.model) || '')}</span></h3>
    ${crit ? `<p class="small muted">Checklist reviewed: ${crit.checklist.map(esc).join(' · ')}</p>` : ''}
    ${!res.rows.length ? `<div class="muted">${crit ? 'The Critic raised no material objections (zero is a valid result).' : 'No critique recorded.'}</div>` : res.rows.map((row) => {
      const o = row.objection, r = row.response || {};
      const rc = (r.cites || []).map((c, i) => ({ ...c, status: (row.citation_status || [])[i] }));
      return `<div class="obj ${esc(o.severity)}"><div class="row"><b>${esc(o.id)}</b><span class="pill ${o.severity === 'blocker' ? 'bad' : o.severity === 'major' ? 'warn' : ''}">${esc(o.severity)}</span><span class="pill">${esc(titleCase(o.type))}</span><span class="spacer"></span>Effective: <span class="res ${esc(row.effective_result)}">${esc(row.effective_result)}</span></div>
        <div>${esc(o.text)} ${chips(objCites[o.id] || o.cites)}</div><div class="small muted"><b>Impact:</b> ${esc(o.decision_impact)} · <b>Settling test:</b> ${esc(o.settling_test.description)}</div>
        <div class="small"><b>Head:</b> ${esc(r.action || 'no response')} — ${esc(r.explanation || '')} ${chips(rc)} <span class="muted">(effect: ${esc(r.decision_effect || '—')})</span></div>
        ${row.code_note ? `<div class="small"><span class="pill warn">code</span> ${esc(row.code_note)}</div>` : ''}</div>`;
    }).join('')}`;
}

function tabRules(dr) {
  const g = dr.governance, c = dr.calculations || {};
  return `<p class="muted small">Deterministic rules from <code>config/rules.yaml</code> (${esc(dr.versions.rules)}). Unknown inputs never pass silently.</p>
  <div class="tbl-wrap"><table><thead><tr><th>Rule</th><th>Result</th><th>Detail</th><th>Phase</th></tr></thead><tbody>${dr.rule_results.map((r) => `<tr><td><b>${esc(titleCase(r.rule))}</b></td><td><span class="res ${esc(r.result)}">${esc(r.result)}</span></td><td>${esc(r.detail)}</td><td class="small muted">${esc(r.phase || 'final')}</td></tr>`).join('')}</tbody></table></div>
  <div class="grid g2" style="margin-top:14px"><div><h4>Governance (code rubric ${esc(g.rubric)})</h4><p>Score <b>${g.score ?? 'withheld'}</b> · coverage ${(g.coverage * 100).toFixed(0)}% · penalty ${g.penalty}<br><span class="small muted">${esc(g.note)}</span></p></div>
  <div><h4>Calculations</h4><pre class="wa">${esc(JSON.stringify(c, null, 1))}</pre></div></div>
  <h4>Consistency check</h4><p><span class="res ${dr.consistency_check.result}">${esc(dr.consistency_check.result)}</span> ${esc(dr.consistency_check.issues.join('; '))}</p>`;
}

function tabSources(dr, b) {
  const tiers = META.tiers;
  const counts = {};
  b.evidence.forEach((e) => (counts[e.tier] = (counts[e.tier] || 0) + 1));
  return `<div class="row" style="margin-bottom:12px" id="tier-filter"><button class="btn sm" data-tier="">All (${b.evidence.length})</button>
    ${Object.entries(counts).map(([t, n]) => `<button class="btn sm" data-tier="${t}"><span class="tier ${t}">${esc(tiers[t] ? tiers[t].label : t)}</span> ${n}</button>`).join('')}</div>
    <div id="ev-list">${b.evidence.map((e) => `<div class="ev" data-tier="${e.tier}" id="src-${e.id}"><div class="ev-h"><span class="id">${e.id}</span><span class="tier ${e.tier}">${esc(tiers[e.tier] ? tiers[e.tier].label : e.tier)}</span><b>${esc(e.title)}</b></div>
      <div class="small muted">${esc(e.publisher)} · ${esc(titleCase(e.kind))} · published ${esc(e.published_at ? dt(e.published_at) : (e.published_hint ? '≈ ' + e.published_hint : 'unknown'))} · first seen ${dt(e.first_seen_at)} · access ${esc(e.access_status)} · via ${esc(e.retrieved_via)}
      ${e.url ? ` · <a href="${esc(e.url)}" target="_blank" rel="noopener noreferrer">open source ↗</a>` : ''}</div>
      ${e.notes ? `<div class="small muted">${esc(e.notes)}</div>` : ''}
      <details><summary class="small">${e.passages.length} passage(s)</summary>${e.passages.map((p) => `<div class="passage"><span class="pid">${p.id}</span> ${esc(p.text)}</div>`).join('')}</details></div>`).join('')}</div>`;
}
function bindSourceFilter() {
  $('#tier-filter').onclick = (e) => { const b = e.target.closest('[data-tier]'); if (!b) return; const t = b.dataset.tier; $$('#ev-list .ev').forEach((el) => (el.hidden = t && el.dataset.tier !== t)); };
}

function tabAudit(dr) {
  const calls = RUN.detail.calls || [];
  const vs = dr.vendor_separation || {};
  return `<div class="grid g2"><div>${dr.whatsapp ? `<h4>WhatsApp text</h4><div class="wa">${esc(dr.whatsapp)}</div><button class="btn sm" id="copy-wa" style="margin-top:8px">Copy</button>` : `<div class="notice info">Loop ${dr.loop} record — superseded by the final loop. Kept for audit.</div>`}</div>
  <div><h4>Identity & versions</h4><table><tbody>
    <tr><td>Record</td><td class="mono">${esc(dr.verdict_id)} · loop ${dr.loop || 1}/${dr.loops_total || 1}</td></tr><tr><td>Bundle hash</td><td class="mono">${esc(dr.bundle_id)}</td></tr>
    ${dr.parent_bundle_id ? `<tr><td>Parent bundle</td><td class="mono">${esc(dr.parent_bundle_id)}</td></tr>` : ''}
    <tr><td>Evaluation contract</td><td class="mono">${esc(dr.evaluation_contract.id)}</td></tr>
    <tr><td>Analyst labels</td><td>${Object.entries(dr.labels || {}).map(([r, l]) => `${l} = ${esc(titleCase(r))}`).join(' · ')}</td></tr>
    <tr><td>Role models</td><td class="small">${Object.entries((dr.versions || {}).role_models || {}).map(([r, m]) => `${esc(r)}: ${esc(m)}`).join('<br>')}</td></tr>
    <tr><td>Vendor separation</td><td><span class="pill ${vs.status === 'separated' ? 'good' : 'warn'}">${esc(vs.status || '—')}</span> <span class="small muted">Head–Critic ${vs.head_critic_separated ? 'on different providers' : 'on the same provider'}</span></td></tr>
    <tr><td>Fallback substitutions</td><td>${(dr.fallback_events || []).length ? esc(JSON.stringify(dr.fallback_events)) : 'none'}</td></tr>
    <tr><td>Prompts</td><td class="small">${Object.values((dr.versions || {}).prompts || {}).map(esc).join(' · ')}</td></tr>
    <tr><td>Config</td><td class="small">${['rules', 'models', 'tax_rates', 'horizons', 'features'].map((k) => esc((dr.versions || {})[k])).join(' · ')}</td></tr>
    ${dr.report_path ? `<tr><td>Report file</td><td class="small mono">${esc(dr.report_path)}</td></tr>` : ''}</tbody></table></div></div>
  <h4 style="margin-top:14px">Model calls in this run (${calls.length}) · run total $${(RUN.detail.run.cost_usd || 0).toFixed(4)}${dr.calls_with_unknown_price ? ` · <span class="pill warn">${dr.calls_with_unknown_price} with unknown price</span>` : ''}</h4>
  <div class="tbl-wrap"><table><thead><tr><th>Role</th><th>Model</th><th>Prompt</th><th>Tokens in/out</th><th>Searches</th><th>Cost</th><th>Latency</th><th>Status</th><th>Provider id</th></tr></thead><tbody>
  ${calls.map((c) => `<tr><td>${esc(c.role)}</td><td class="mono">${esc(c.model)}</td><td class="small">${esc(c.prompt_version)}</td><td class="mono">${(c.input_tokens || 0).toLocaleString()} / ${(c.output_tokens || 0).toLocaleString()}</td><td>${c.web_searches || 0}</td><td class="mono">${c.cost_usd === null ? 'unknown' : '$' + (c.cost_usd || 0).toFixed(4)}</td><td>${((c.latency_ms || 0) / 1000).toFixed(1)}s</td><td>${esc(c.status)}</td><td class="mono small">${esc(c.provider_request_id || '—')}</td></tr>`).join('')}</tbody></table></div>`;
}

/* ---------------------------------------------------------------- chart */
function drawChart(dr, b) {
  const host = $('#chart'); if (!host || !b.candles || !b.candles.length) { if (host) host.innerHTML = '<div class="empty">No price data.</div>'; return; }
  const N = Math.min(140, b.candles.length);
  const cs = b.candles.slice(-N);
  const off = b.candles.length - N;
  const sma = (k) => (b.price[k] || []).slice(-b.candles.length).slice(off);
  const lines = [['sma20', 'SMA 20', 'var(--s1)'], ['sma50', 'SMA 50', 'var(--s2)'], ['sma200', 'SMA 200', 'var(--s3)']];
  const hp = dr.head_proposal; const plan = dr.final_action.plan || hp;
  const levels = [];
  if (plan && plan.stop) levels.push(['Stop', plan.stop, 'var(--down)']);
  if (plan && plan.target_low) levels.push(['Target', plan.target_low, 'var(--up)']);
  const vals = cs.flatMap((c) => [c.high, c.low]).concat(levels.map((l) => l[1])).concat(plan && plan.entry_low ? [plan.entry_low, plan.entry_high] : []);
  const lo = Math.min(...vals) * 0.985, hi = Math.max(...vals) * 1.015;
  const W = Math.max(host.clientWidth || 560, 420), H = 300, P = { l: 8, r: 64, t: 10, b: 22 };
  const cw = (W - P.l - P.r) / N;
  const x = (i) => P.l + i * cw + cw / 2, y = (v) => P.t + (hi - v) / (hi - lo) * (H - P.t - P.b);
  let grid = '';
  for (let i = 0; i <= 5; i++) { const v = lo + (hi - lo) * i / 5; grid += `<line x1="${P.l}" x2="${W - P.r}" y1="${y(v)}" y2="${y(v)}" stroke="var(--border)" stroke-width="1"/><text x="${W - P.r + 6}" y="${y(v) + 4}" font-size="11" fill="var(--muted)">${fmt(v, 0)}</text>`; }
  let band = '';
  if (plan && plan.entry_low && plan.entry_high) band = `<rect x="${P.l}" width="${W - P.l - P.r}" y="${y(plan.entry_high)}" height="${Math.max(2, y(plan.entry_low) - y(plan.entry_high))}" fill="var(--accent)" opacity=".12"/><text x="${P.l + 4}" y="${y(plan.entry_high) - 4}" font-size="11" fill="var(--text-2)">Entry zone</text>`;
  const candles = cs.map((c, i) => { const up = c.close >= c.open, col = up ? 'var(--up)' : 'var(--down)'; const bw = Math.max(1.5, cw * 0.6);
    return `<line x1="${x(i)}" x2="${x(i)}" y1="${y(c.high)}" y2="${y(c.low)}" stroke="${col}" stroke-width="1"/><rect x="${x(i) - bw / 2}" width="${bw}" y="${y(Math.max(c.open, c.close))}" height="${Math.max(1, Math.abs(y(c.open) - y(c.close)))}" fill="${col}" rx="1"/>`; }).join('');
  const paths = lines.map(([k, , col]) => { const s = sma(k); let d = ''; s.forEach((v, i) => { if (v != null) d += (d ? 'L' : 'M') + x(i).toFixed(1) + ' ' + y(v).toFixed(1); }); return d ? `<path d="${d}" fill="none" stroke="${col}" stroke-width="2"/>` : ''; }).join('');
  const lvl = levels.map(([n, v, col]) => `<line x1="${P.l}" x2="${W - P.r}" y1="${y(v)}" y2="${y(v)}" stroke="${col}" stroke-dasharray="5 4" stroke-width="1.5"/><text x="${W - P.r + 6}" y="${y(v) - 4}" font-size="11" font-weight="700" fill="var(--text)">${n}</text>`).join('');
  const lab = (i) => `<text x="${x(i)}" y="${H - 6}" font-size="11" fill="var(--muted)" text-anchor="middle">${esc(cs[i].date.slice(5))}</text>`;
  host.innerHTML = `<svg width="${W}" height="${H}" role="img" aria-label="Candlestick chart of the last ${N} sessions">${grid}${band}${candles}${paths}${lvl}${lab(0)}${lab(Math.floor(N / 2))}${lab(N - 1)}<line id="xh" y1="${P.t}" y2="${H - P.b}" stroke="var(--text-2)" stroke-width="1" opacity="0"/><rect id="hit" x="${P.l}" y="0" width="${W - P.l - P.r}" height="${H}" fill="transparent"/></svg><div class="tip" id="tip" hidden></div>`;
  $('#legend').innerHTML = lines.map(([, n, col]) => `<span><i style="background:${col}"></i>${n}</span>`).join('') + `<span><i style="background:var(--up)"></i>Up day</span><span><i style="background:var(--down)"></i>Down day</span>${levels.length ? '<span><i style="border-top:2px dashed var(--muted);height:0"></i>Plan levels</span>' : ''}`;
  const tip = $('#tip'), xh = $('#xh');
  $('#hit').onmousemove = (ev) => { const r = ev.currentTarget.getBoundingClientRect(); const i = Math.max(0, Math.min(N - 1, Math.floor((ev.clientX - r.left) / cw)));
    const c = cs[i]; xh.setAttribute('x1', x(i)); xh.setAttribute('x2', x(i)); xh.setAttribute('opacity', '.4');
    const s = lines.map(([k, n]) => { const v = sma(k)[i]; return v != null ? `${n} ${fmt(v)}` : ''; }).filter(Boolean).join(' · ');
    tip.innerHTML = `<b>${esc(c.date)}</b><br>O ${fmt(c.open)} H ${fmt(c.high)} L ${fmt(c.low)} C <b>${fmt(c.close)}</b><br><span class="muted">Vol ${Number(c.volume).toLocaleString('en-IN')}</span><br><span class="muted">${s}</span>`;
    tip.hidden = false; const left = x(i) + 14; tip.style.left = (left > W - 220 ? x(i) - 230 : left) + 'px'; tip.style.top = '14px'; };
  $('#hit').onmouseleave = () => { tip.hidden = true; xh.setAttribute('opacity', '0'); };
}

/* --------------------------------------------------------------- drawer */
document.addEventListener('click', (e) => {
  const c = e.target.closest('.cite[data-ev]');
  if (c) { openSource(c.dataset.ev, c.dataset.pid, c.dataset.q, c.dataset.st); return; }
  const s = e.target.closest('.src-open[data-ev]');
  if (s) { e.preventDefault(); openSource(s.dataset.ev, '', '', 'source'); }
});
$('#drawer-close').onclick = closeDrawer;
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeDrawer(); });
function closeDrawer() { $('#drawer').hidden = true; }

function highlight(text, q) {
  const t = esc(text); if (!q) return t;
  const norm = (s) => s.toLowerCase().replace(/\s+/g, ' ');
  const i = norm(text).indexOf(norm(q).trim());
  if (i < 0) return t;
  return esc(text.slice(0, i)) + '<mark>' + esc(text.slice(i, i + q.trim().length)) + '</mark>' + esc(text.slice(i + q.trim().length));
}

function openSource(evId, pid, quote, status) {
  const e = RUN && RUN.evIndex[evId];
  $('#drawer-title').textContent = evId;
  const statusText = { verified: '✓ Quote found verbatim in this passage (checked by code).', id_only: 'Cited by id only — no quote to verify.',
    quote_mismatch: '✗ The quoted text was NOT found in this source. Treat the claim as unsupported.', unknown_passage: '✗ Passage id does not exist in the bundle.',
    unknown_evidence: '✗ This evidence id does not exist in the bundle (fabricated or out of boundary).', source: '' }[status] || '';
  if (!e) { $('#drawer-body').innerHTML = `<div class="notice bad">${esc(statusText || 'Evidence not found in this bundle.')}</div>${quote ? `<p>Quoted: “${esc(quote)}”</p>` : ''}`; $('#drawer').hidden = false; return; }
  const tiers = (META && META.tiers) || {};
  $('#drawer-body').innerHTML = `${statusText ? `<div class="notice ${status === 'verified' ? 'info' : status === 'id_only' ? '' : 'bad'}">${esc(statusText)}</div>` : ''}
    <h3>${esc(e.title)}</h3><div class="row"><span class="tier ${e.tier}">${esc(tiers[e.tier] ? tiers[e.tier].label : e.tier)}</span><span class="small muted">${esc(tiers[e.tier] ? tiers[e.tier].description : '')}</span></div>
    <table style="margin:10px 0"><tbody><tr><td>Publisher</td><td>${esc(e.publisher)}</td></tr><tr><td>Kind</td><td>${esc(titleCase(e.kind))}</td></tr>
    <tr><td>Published</td><td>${e.published_at ? dt(e.published_at) : (e.published_hint ? '≈ ' + esc(e.published_hint) + ' (search hint, unverified)' : 'unknown')}</td></tr>
    <tr><td>First seen</td><td>${dt(e.first_seen_at)}</td></tr><tr><td>Access</td><td>${esc(e.access_status)}</td></tr><tr><td>Retrieved via</td><td>${esc(e.retrieved_via)}</td></tr>
    ${e.url ? `<tr><td>Link</td><td><a href="${esc(e.url)}" target="_blank" rel="noopener noreferrer">${esc(e.url.slice(0, 70))}${e.url.length > 70 ? '…' : ''} ↗</a></td></tr>` : ''}
    <tr><td>Content hash</td><td class="mono small">${esc((e.content_hash || '').slice(0, 24))}…</td></tr></tbody></table>
    ${e.notes ? `<p class="small muted">${esc(e.notes)}</p>` : ''}
    <h4>Passages</h4>${e.passages.map((p) => `<div class="passage" ${p.id === pid ? 'style="border-left-color:var(--accent)"' : ''}><span class="pid">${p.id}${p.id === pid ? ' · cited' : ''}</span><br>${p.id === pid || !pid ? highlight(p.text, quote) : esc(p.text)}</div>`).join('')}`;
  $('#drawer').hidden = false;
}

/* ---------------------------------------------------------------- inbox */
async function viewInbox() {
  const [rows, queue] = await Promise.all([api('/api/inbox'), api('/api/queue')]);
  const qs = { pending: 'info', in_progress: 'info', captured: 'good', failed: 'bad', cancelled: '' };
  $('#app').innerHTML = `<div class="grid g2">
  <div class="card"><h2>ET Prime article queue</h2>
    <p class="muted small">Paste ET article links you can read with your subscription. Then open the Personal CA extension in Chrome and click <b>Fetch queued articles</b>: it opens each link in your signed-in browser, captures the text, and sends it here. No server-side login or scraping. Tag a stock so the article joins that stock's next analysis.</p>
    <textarea id="q-urls" rows="4" style="width:100%" placeholder="https://economictimes.indiatimes.com/prime/...articleshow/....cms&#10;one link per line"></textarea>
    <div class="row" style="margin-top:8px"><input id="q-sym" type="text" placeholder="Stock (optional), e.g. TCS" style="width:200px;text-transform:uppercase"><input id="q-note" type="text" placeholder="Note (optional)" style="flex:1;min-width:140px"><button class="btn primary" id="q-add">Add to queue</button></div>
    <div id="q-msg" class="small"></div>
  </div>
  <div class="card"><div class="row"><h3 style="margin:0">Queue (${queue.filter((q) => ['pending', 'in_progress'].includes(q.status)).length} waiting)</h3><span class="spacer"></span><button class="btn sm" id="q-ref">Refresh</button></div>
    ${queue.length ? `<div class="tbl-wrap"><table><tbody>${queue.slice(0, 30).map((q) => `<tr><td><span class="pill ${qs[q.status] || ''}">${esc(q.status.replace('_', ' '))}</span></td>
      <td class="small"><a href="${esc(q.canonical_url)}" target="_blank" rel="noopener noreferrer">${esc(q.canonical_url.replace('https://economictimes.indiatimes.com', 'ET').slice(0, 70))}</a>${q.error ? `<div class="res fail">${esc(q.error)}</div>` : ''}${q.note ? `<div class="muted">${esc(q.note)}</div>` : ''}</td>
      <td>${q.symbol ? `<span class="pill info">${esc(q.symbol)}</span>` : ''}</td>
      <td>${q.source_id ? `<a class="btn sm" href="#/source/${esc(q.source_id)}">View</a>` : q.status === 'pending' ? `<button class="btn sm" data-cancel="${esc(q.queue_id)}">Cancel</button>` : ''}</td></tr>`).join('')}</tbody></table></div>` : '<div class="empty">Nothing queued.</div>'}</div></div>
  <div class="card"><h2>Research inbox</h2>
    <p class="muted">Captured articles (ET Prime etc.). Captures are <b>attributed, unverified</b> research inputs: importing never verifies a claim. Only captures where you attested personal-research use are sent to a model. A capture linked to a stock joins that stock's analysis as a news source (tier: user capture).</p>
    ${rows.length ? `<div class="tbl-wrap"><table><thead><tr><th>Captured</th><th>Title</th><th>Publisher</th><th>Stocks</th><th>Access</th><th>Usage</th><th>Claims</th></tr></thead><tbody>
    ${rows.map((r) => `<tr><td>${dt(r.ingested_at)}</td><td><a href="#/source/${esc(r.source_id)}">${esc(r.title || r.canonical_url)}</a><div class="small muted">v${r.version} · ${r.n_passages} passages</div></td><td>${esc(r.publisher)}</td>
      <td>${(r.company_links.resolved || []).map((c) => `<span class="pill info">${esc(c.symbol)}</span>`).join(' ')}${(r.company_links.ambiguous || []).length ? ' <span class="pill warn">ambiguous</span>' : ''}${!(r.company_links.resolved || []).length ? '<span class="muted small">none — link one</span>' : ''}</td>
      <td><span class="pill ${r.access_status === 'partially_read' ? 'warn' : ''}">${esc(r.access_status)}</span></td>
      <td><span class="pill ${r.usage_status.includes('unattested') ? 'warn' : 'good'}">${r.usage_status.includes('unattested') ? 'not attested' : 'attested'}</span></td><td>${r.claims}</td></tr>`).join('')}</tbody></table></div>`
    : `<div class="empty">Inbox is empty. Queue some ET links above, or open an article and use the extension's <b>Capture page</b>.</div>`}</div>`;
  $('#q-add').onclick = async () => {
    const urls = $('#q-urls').value.split(/\s+/).filter(Boolean);
    if (!urls.length) { toast('Paste at least one link'); return; }
    try {
      const r = await api('/api/queue', { method: 'POST', body: { urls, symbol: $('#q-sym').value.trim() || null, note: $('#q-note').value.trim() || null } });
      toast(`${r.added.length} queued`);
      if (r.rejected.length) $('#q-msg').innerHTML = r.rejected.map((x) => `<div class="res fail">${esc(x.url.slice(0, 60))}: ${esc(x.reason)}</div>`).join('');
      else viewInbox();
    } catch (e) { toast(e.message); }
  };
  $('#q-ref').onclick = viewInbox;
  $$('[data-cancel]').forEach((b) => (b.onclick = async () => { await api(`/api/queue/${b.dataset.cancel}/cancel`, { method: 'POST' }); viewInbox(); }));
}

async function viewSource(id) {
  const s = await api('/api/inbox/' + encodeURIComponent(id));
  const attested = s.usage_status === 'personal_research_user_attested';
  $('#app').innerHTML = `<div class="card"><a href="#/inbox">← Inbox</a><h2 style="margin-top:8px">${esc(s.title)}</h2>
    <div class="row"><span class="tier T4_capture_unverified">User capture (unverified)</span><span class="pill">${esc(s.publisher)}</span><span class="pill">v${s.version}</span><span class="pill">${esc(s.access_status)}</span>
    <a href="${esc(s.canonical_url)}" target="_blank" rel="noopener noreferrer">open original ↗</a></div>
    <p class="small muted">Published ${esc(s.published_at || 'unknown')} · captured ${dt(s.captured_at)} · fingerprint <span class="mono">${esc(s.fingerprint.slice(0, 16))}</span>${s.warnings.length ? ' · warnings: ' + s.warnings.map(esc).join(', ') : ''}</p>
    ${!attested ? '<div class="notice">Usage for personal research was not attested at capture — this source will not be sent to any model.</div>' : ''}
    <div class="row"><b>Stocks:</b> ${(s.company_links.resolved || []).map((c) => `<a class="pill info" href="#/" data-analyse="${esc(c.symbol)}" title="Analyse ${esc(c.symbol)}">${esc(c.symbol)} · ${esc(c.name)} ${String(c.matched || '').startsWith('user') ? '(linked by you)' : ''}</a>`).join(' ') || '<span class="muted">none resolved</span>'}
      ${(s.company_links.ambiguous || []).map((a) => `<span class="pill warn" title="Not attached automatically: ambiguous">${esc(a.matched)}: ${a.candidates.map(esc).join('/')}</span>`).join(' ')}
      <span class="spacer"></span><input id="lnk" type="text" placeholder="Link to stock, e.g. TCS" style="width:170px;text-transform:uppercase"><button class="btn sm" id="lnk-go">Link</button></div></div>
  <div class="grid g2"><div class="card"><div class="row"><h3 style="margin:0">Claims (${s.claims.length})</h3><span class="spacer"></span><button class="btn sm" id="xc" ${attested ? '' : 'disabled'}>${s.claims.length ? 'Re-extract' : 'Extract'} claims</button></div>
    <p class="small muted">Model-extracted, attributed and <b>unverified</b>. Each carries a verbatim quote (checked) and a verification task.</p>
    <div id="claims">${claimsHtml(s.claims)}</div></div>
    <div class="card"><h3>Ask about this capture <span class="pill warn">research only</span></h3><p class="small muted">Answers cite exact passages via the Citations API. This is not a panel verdict.</p>
    <textarea id="q" rows="3" style="width:100%" placeholder="e.g. What target and horizon does the article give, and who is the source?"></textarea>
    <div class="row" style="margin-top:8px"><button class="btn primary sm" id="ask" ${attested ? '' : 'disabled'}>Ask</button></div><div id="ans"></div></div></div>
  <div class="card"><h3>Captured passages</h3>${s.passages.map((p) => `<div class="passage"><span class="pid">${p.id}</span> ${esc(p.text)}</div>`).join('')}</div>`;
  $$('[data-analyse]').forEach((a) => (a.onclick = () => { try { sessionStorage.setItem('ca-sym', a.dataset.analyse); } catch (e) { /* ignore */ } }));
  $('#lnk-go').onclick = async () => { const v = $('#lnk').value.trim().toUpperCase(); if (!v) return; try { await api(`/api/inbox/${id}/link`, { method: 'POST', body: { symbol: v } }); toast(`Linked to ${v}`); viewSource(id); } catch (e) { toast(e.message); } };
  const xc = $('#xc'); if (xc) xc.onclick = async () => { xc.disabled = true; xc.textContent = 'Extracting…'; try { const c = await api(`/api/inbox/${id}/claims`, { method: 'POST' }); $('#claims').innerHTML = claimsHtml(c); } catch (e) { toast(e.message); } xc.disabled = false; xc.textContent = 'Re-extract claims'; };
  $('#ask').onclick = async () => { const q = $('#q').value.trim(); if (!q) return; $('#ans').innerHTML = '<p class="muted">Thinking…</p>';
    try { const a = await api('/v1/research/questions', { method: 'POST', body: { source_ids: [id], question: q } });
      $('#ans').innerHTML = `<div class="passage" style="margin-top:10px">${a.parts.map((p) => esc(p.text) + p.citations.map((c) => ` <span class="cite verified" title="${esc(c.cited_text)}">${esc(c.passage_ids.map((x) => x.split(':').pop()).join(','))} ✓</span>`).join('')).join('')}</div><p class="small muted">${esc(a.note)} · $${(a.cost_usd || 0).toFixed(4)}</p>`;
    } catch (e) { $('#ans').innerHTML = `<div class="notice bad">${esc(e.message)}</div>`; } };
}
function claimsHtml(cs) {
  if (!cs.length) return '<div class="muted small">No claims extracted yet.</div>';
  return cs.map((c) => `<div class="obj minor"><div class="row"><span class="pill">${esc(titleCase(c.claim_type))}</span>${c.symbol ? `<span class="pill info">${esc(c.symbol)}</span>` : `<span class="pill warn">${esc(c.company_mention)} (unresolved)</span>`}<span class="pill warn">${esc(c.status)}</span>${c.direction ? `<span class="pill">${esc(c.direction)}</span>` : ''}<span class="pill">${esc(titleCase(c.evidence_class || ''))}</span></div>
    <div>${esc(c.text)}</div><div class="small muted">“${esc(c.quote)}” — ${esc(c.passage_id)}${c.author_or_broker ? ' · ' + esc(c.author_or_broker) : ''}${c.target_price ? ' · target ' + fmt(c.target_price, 0) : ''}${c.horizon_text ? ' · ' + esc(c.horizon_text) : ''}</div>
    <div class="small"><b>Verify:</b> ${esc(c.verification_task)}</div></div>`).join('');
}

/* ------------------------------------------------------------- Newsdesk */
const ND_PILL = { filed: 'good', confirmed: 'good', filing_on_event: 'info', investigative: 'warn', unconfirmed: '', contradicted: 'bad', denied: 'bad' };

async function viewNews(sym) {
  const wl = await api('/api/newsdesk/watchlist');
  sym = (sym || (wl.stocks[0] || {}).symbol || '').toUpperCase();
  const w = wl.worker || {};
  $('#app').innerHTML = `<div class="grid" style="grid-template-columns:260px 1fr;align-items:start">
    <div class="card tight"><h3>Newsdesk watchlist</h3>
      <p class="small muted">Watched stocks are refreshed in the background while <code>ca serve</code> runs${w.last_sweep ? ` · last sweep ${esc(w.last_sweep.slice(11, 16))}` : ''}${w.next_in_minutes ? ` · every ${w.next_in_minutes} min now` : ''}.</p>
      ${wl.stocks.map((s) => `<div class="row" style="margin:4px 0"><a href="#/news/${esc(s.symbol)}" class="${s.symbol === sym ? 'pill info' : 'pill'}">${esc(s.symbol)}</a><span class="small muted">${s.last_refresh ? esc(s.last_refresh.slice(5, 16).replace('T', ' ')) : 'never'}</span><span class="spacer"></span><button class="icon-btn" style="width:24px;height:24px" data-rm-sym="${esc(s.symbol)}" title="Remove ${esc(s.symbol)} from the watchlist">×</button></div>`).join('') || '<div class="muted small">No stocks yet.</div>'}
      <div class="row" style="margin-top:10px"><input id="nd-add" type="text" placeholder="Add, e.g. TCS" style="width:120px;text-transform:uppercase"><button class="btn sm" id="nd-add-go">Add</button></div>
      <p class="small muted" style="margin-top:10px">Any symbol works without adding it — open <code>#/news/SYMBOL</code>. Adding it enables background refresh.</p></div>
    <div id="nd-main">${sym ? '<div class="card"><div class="loading">Building dossier… the first fetch of a stock can take a few minutes (NSE filings and their PDFs, at a polite one request per second); later refreshes are quick.</div></div>' : '<div class="card empty">Add a stock to start.</div>'}</div></div>`;
  $('#nd-add-go').onclick = async () => { const s = $('#nd-add').value.trim().toUpperCase(); if (!s) return; try { await api('/api/newsdesk/watchlist', { method: 'POST', body: { symbol: s, watch: true } }); location.hash = `#/news/${s}`; } catch (e) { toast(e.message); } };
  $$('[data-rm-sym]').forEach((b) => (b.onclick = () => removeWatch(b.dataset.rmSym, sym)));
  if (sym) loadDossier(sym, 'auto');
}

async function removeWatch(s, current) {
  try {
    await api(`/api/newsdesk/watchlist/${encodeURIComponent(s)}/remove`, { method: 'POST' });
    toast(`${s} removed — no more background refreshes. Its stored news is kept and still used by analyses.`);
    if (s === current) location.hash = '#/news'; else viewNews(current);
  } catch (e) { toast(e.message); }
}

let ND_POLL = null;
function progressCard(p, first) {
  const steps = (p.steps || []).slice(-14);
  return `<div class="card" id="nd-progress"><div class="row"><span class="st-icon ${p.running ? 'running' : p.error ? 'failed' : 'completed'}">${p.running ? '' : p.error ? '!' : '✓'}</span>
    <h3 style="margin:0">${p.running ? (first ? 'First fetch in progress' : 'Refreshing in the background') : p.error ? 'Refresh failed' : 'Refresh finished'}</h3>
    <span class="spacer"></span><span class="small muted">started ${esc((p.started || '').slice(11, 19))} by ${esc(p.who || '—')}</span></div>
    ${first ? '<p class="small muted">NSE serves filing PDFs slowly (about 20–40 s each, read one at a time). Each line below is a real step as it happens.</p>' : ''}
    <div class="logs" style="margin-left:0;max-height:320px">${steps.map((s) => `<div><span class="ts">${esc(s.t)}</span>${esc(s.m)}</div>`).join('') || '<div class="muted">Starting…</div>'}</div></div>`;
}

function pollProgress(sym, first) {
  clearTimeout(ND_POLL);
  ND_POLL = setTimeout(async () => {
    if (!location.hash.startsWith('#/news')) return;
    const p = await api(`/api/newsdesk/progress/${sym}`).catch(() => null);
    if (!p) return;
    const box = $('#nd-progress');
    if (box) box.outerHTML = progressCard(p, first);
    if (p.running) pollProgress(sym, first);
    else setTimeout(() => { if (location.hash.startsWith('#/news')) viewNews(sym); }, 1500);
  }, 2000);
}

async function loadDossier(sym, refresh) {
  const days = store.get('nd-days', 90);
  const main = $('#nd-main'); if (!main) return;
  let d;
  try { d = await api(`/api/newsdesk/dossier/${sym}?days=${days}&refresh=${refresh}`); } catch (e) { main.innerHTML = `<div class="card notice bad">${esc(e.message)}</div>`; return; }
  const entry = await api(`/api/newsdesk/entry/${sym}`);
  const q = d.quality, C = d.citations;
  const rep = d.refresh_report;
  const cite = (n) => { const c = C[String(n)]; return c ? `<span class="cite ${c.tier === 'T1_official' ? 'verified' : 'id_only'}" data-nd="${c.article_id}" title="${esc(c.publisher)} · ${esc(c.tier)}${c.headline_only ? ' · headline only' : ''}">${n}</span>` : ''; };
  const watched = (await api('/api/newsdesk/watchlist')).stocks.some((s) => s.symbol === d.symbol);
  if (d.refreshing) pollProgress(sym, d.never_fetched);
  if (d.never_fetched) { main.innerHTML = progressCard(d.progress, true); return; }
  main.innerHTML = `${d.refreshing ? progressCard(d.progress, false) : ''}<div class="card"><div class="row"><h2 style="margin:0">${esc(d.symbol)}</h2><span class="muted">${esc(d.name)}</span><span class="spacer"></span>
      <div class="seg" id="nd-days">${[30, 90, 180].map((k) => `<button data-d="${k}" class="${k === days ? 'on' : ''}">${k}d</button>`).join('')}</div>
      <button class="btn sm" id="nd-refresh" ${d.refreshing ? 'disabled' : ''}>Refresh now</button>
      ${watched ? `<button class="btn sm" id="nd-unwatch" title="Stop background refreshes for ${esc(d.symbol)}">Stop watching</button>` : ''}
      <a class="btn sm primary" href="#/" id="nd-analyse">Analyse ${esc(d.symbol)}</a></div>
    <p class="small muted">As of ${dt(d.as_of)} · last fetch ${d.last_refresh ? dt(d.last_refresh) : 'never'}${rep ? ' · ' + Object.entries(rep.sources).map(([k, v]) => `${k}: ${v.items}`).join(' · ') : ''}${rep && rep.failed.length ? ` · <span class="res fail">failed: ${esc(rep.failed.join('; '))}</span>` : ''}</p>
    <div class="kv"><div><span>Events</span><b>${q.events}</b></div><div><span>Filed / confirmed</span><b>${q.filed_or_confirmed}</b></div><div><span>Filing on event</span><b>${q.filing_on_event}</b></div>
      <div><span>Press only</span><b>${q.press_only}</b></div><div><span>Contradicted / denied</span><b>${q.against}</b></div><div><span>Headline only</span><b>${q.headline_only_events}</b></div></div>
    <p class="small muted">Set aside (kept on record, not used): ${Object.entries(q.dropped).map(([k, v]) => `${esc(k)} ${v}`).join(' · ') || 'none'}</p></div>
  ${d.check_first.length ? `<div class="card notice bad"><b>Check these first:</b><br>${d.check_first.map(esc).join('<br>')}</div>` : ''}
  <div class="card"><h3>Events, ranked</h3><p class="small muted">score = materiality × confidence × recency. Wording-alike articles from several outlets count as one story; a press figure is "confirmed" only by a filed figure of the same metric within 5%.</p>
    ${d.events.map((e, i) => `<div class="obj ${e.status === 'contradicted' || e.status === 'denied' ? 'blocker' : e.status === 'filed' || e.status === 'confirmed' ? '' : 'minor'}">
      <div class="row"><b>${i + 1}.</b><span class="pill ${ND_PILL[e.status] || ''}">${esc(e.status_label)}</span><span class="pill">${esc(titleCase(e.type))}</span><span class="pill">${esc(e.fact_type)}</span>${e.direction !== '0' ? `<span class="pill">${esc(e.direction)}</span>` : ''}<span class="spacer"></span>
        <span class="small muted">conf ${e.confidence} · weight ${e.score}</span></div>
      <div>${esc(e.title)} ${e.citations.map(cite).join('')}</div>
      <div class="small muted">${e.filings} filing(s) · ${e.independent_press_stories} independent press stor${e.independent_press_stories === 1 ? 'y' : 'ies'} · ${esc(e.publishers.join(', '))} · first seen ${esc(e.first_seen.slice(0, 10))}</div>
      ${e.claims.filter((c) => c.amount_cr).length ? `<div class="small">${e.claims.filter((c) => c.amount_cr).map((c) => `<span class="pill ${ND_PILL[c.status] || ''}">${esc(c.status)}</span> ${esc(c.metric)} ₹${fmt(c.amount_cr, 0)} cr — ${esc(c.text.slice(0, 140))}`).join('<br>')}</div>` : ''}</div>`).join('') || '<div class="empty">No events in this window.</div>'}</div>
  <div class="grid g2"><div class="card"><h3>Coming up</h3>${d.coming_up.map((c) => `<div class="small"><b>${esc(c.date)}</b> ${esc(c.what)} ${cite(c.citation)}</div>`).join('') || '<div class="muted small">Nothing scheduled in filings.</div>'}
      <h3 style="margin-top:12px">Not known yet</h3>${d.not_known.map((x) => `<div class="small">• ${esc(x)}</div>`).join('') || '<div class="muted small">—</div>'}</div>
    <div class="card"><h3>Names used to match news</h3><p class="small muted">aliases identify the company · link_only = shorthand matched case-sensitively · related = subsidiaries/brands (ranked lower) · exclude = look-alikes removed before matching.</p>
      ${['aliases', 'link_only', 'related', 'exclude'].map((k) => `<label class="small"><b>${k}</b><input id="nd-${k}" type="text" style="width:100%" value="${esc((entry[k] || []).join(', '))}"></label>`).join('')}
      <div class="row" style="margin-top:8px"><label class="small">market cap ₹ cr <input id="nd-mcap" type="text" style="width:120px" value="${esc(entry.market_cap_cr || '')}"></label><button class="btn sm primary" id="nd-save">Save &amp; watch</button></div></div></div>`;
  $('#nd-days').onclick = (e) => { const k = e.target.dataset.d; if (k) { store.set('nd-days', Number(k)); loadDossier(sym, 'no'); } };
  $('#nd-refresh').onclick = () => loadDossier(sym, 'force');
  const uw = $('#nd-unwatch'); if (uw) uw.onclick = () => removeWatch(sym, sym);
  $('#nd-analyse').onclick = () => { try { sessionStorage.setItem('ca-sym', sym); } catch (e) { /* ignore */ } };
  $('#nd-save').onclick = async () => {
    const list = (k) => $(`#nd-${k}`).value.split(',').map((x) => x.trim()).filter(Boolean);
    try { await api('/api/newsdesk/watchlist', { method: 'POST', body: { symbol: sym, aliases: list('aliases'), link_only: list('link_only'), related: list('related'), exclude: list('exclude'), market_cap_cr: parseFloat($('#nd-mcap').value) || null, watch: true } }); toast('Saved — refreshing with the new names'); viewNews(sym); }
    catch (e) { toast(e.message); }
  };
}

document.addEventListener('click', async (e) => {
  const c = e.target.closest('[data-nd]');
  if (!c) return;
  const a = await api(`/api/newsdesk/article/${c.dataset.nd}`).catch(() => null);
  if (!a) return;
  $('#drawer-title').textContent = `Saved copy #${a.id}`;
  $('#drawer-body').innerHTML = `<h3>${esc(a.title)}</h3><div class="row"><span class="tier ${esc(a.tier)}">${esc(a.tier)}</span><span class="small muted">${esc(a.publisher)} · ${esc(a.via || '')}</span></div>
    <table style="margin:10px 0"><tbody><tr><td>Published</td><td>${a.published_at ? dt(a.published_at) : 'unknown'}</td></tr><tr><td>First seen</td><td>${dt(a.first_seen_at)}</td></tr>
    <tr><td>Link</td><td><a href="${esc(a.url)}" target="_blank" rel="noopener noreferrer">${esc(a.url.slice(0, 70))} ↗</a></td></tr><tr><td>SHA-256</td><td class="mono small">${esc(a.content_hash)}</td></tr>
    ${a.dropped_reason ? `<tr><td>Set aside</td><td>${esc(a.dropped_reason)}</td></tr>` : ''}</tbody></table>
    ${a.is_stub ? '<div class="notice">Headline only — the article text was not retrieved.</div>' : `<div class="passage" style="white-space:pre-wrap">${esc((a.body || '').slice(0, 20000))}</div>`}`;
  $('#drawer').hidden = false;
});

/* ------------------------------------------------------------- ET Prime */
const STATE_PILL = { captured: 'good', pending: 'info', in_progress: 'info', done: 'good', failed: 'bad', cancelled: '', not_captured: 'warn' };

function listStatusTable(rows) {
  if (!rows.length) return '<div class="empty small">No articles in this list yet.</div>';
  return `<div class="tbl-wrap"><table><tbody>${rows.map((r) => `<tr>
    <td><span class="pill ${STATE_PILL[r.state] || ''}">${esc(r.state.replace('_', ' '))}</span>${r.requeued ? ' <span class="pill info">re-queued</span>' : ''}</td>
    <td class="small">${r.title ? `<b>${esc(r.title)}</b><br>` : ''}<a href="${esc(r.url)}" target="_blank" rel="noopener noreferrer">${esc(r.url.replace('https://economictimes.indiatimes.com', 'ET').slice(0, 80))}</a>
      ${r.error ? `<div class="res fail">${esc(r.error)}</div>` : ''}</td>
    <td class="small">${r.passages ? `${r.passages} passages` : ''}</td>
    <td>${(r.stocks || []).map((s) => `<span class="pill info">${esc(s)}</span>`).join(' ')}</td>
    <td>${r.source_id ? `<a class="btn sm" href="#/source/${esc(r.source_id)}">View</a>` : ''}</td></tr>`).join('')}</tbody></table></div>`;
}

let ET = { top: null, ranking: null, picked: new Set() };

function extBanner(ext, path, pending) {
  if (ext && ext.connected) {
    return `<div class="notice info"><b>✓ Personal CA extension v${esc(ext.version || '?')} connected</b> (checked in ${ext.seconds_ago}s ago).
      ${pending ? `${pending} article(s) waiting — with Auto-fetch on they are captured within about a minute.` : 'Nothing waiting.'}
      <button class="btn sm" id="ext-fetch" style="margin-left:8px">Fetch now</button></div>`;
  }
  return `<div class="notice bad"><b>The Personal CA extension is not connected${ext && ext.ever_seen ? ` (last seen ${Math.round(ext.seconds_ago / 60)} min ago — is Chrome open?)` : ''}.</b>
    Articles are read in your own Chrome by this project's extension (v0.5.0) — <i>not</i> the older "Personal CA Research Companion 0.1.1" prototype. Set it up once:
    <ol class="small" style="margin:6px 0 0">
      <li>Chrome → <code>chrome://extensions</code> → turn on <b>Developer mode</b> (top right).</li>
      <li>Disable or remove the old <b>Personal CA Research Companion 0.1.1</b> to avoid confusion.</li>
      <li>Click <b>Load unpacked</b> and choose <code>${esc(path)}</code>. (Already loaded? Click its ↻ reload icon.)</li>
      <li>Click the extension's toolbar icon → side panel → <b>Settings</b> → paste the pairing token from <a href="#/system">System</a> → <b>Save &amp; test</b>.</li>
      <li>Tick the personal-research attestation, click <b>Allow ET access</b>, and keep <b>Auto-fetch</b> on.</li>
    </ol>
    Within a minute this banner turns green and queued articles start arriving.${pending ? ` <b>${pending} article(s) are waiting.</b>` : ''}</div>`;
}

async function viewET() {
  const [lists, batches] = await Promise.all([api('/api/et/lists'), api('/api/batches').catch(() => [])]);
  ET.top = ET.top || store.get('ca-idea-top', (HEALTH && HEALTH.display && HEALTH.display.top_sources) || 8);
  const st = lists.status;
  const pending = [...st.news_articles, ...st.idea_articles].filter((r) => ['pending', 'in_progress', 'not_captured'].includes(r.state)).length;
  $('#app').innerHTML = `
  <div class="card"><h2>ET Prime &amp; stock ideas</h2>
    <p class="muted">Two lists you can edit any time (saved to <code>${esc(st.file)}</code>, also editable by hand). Saving queues every article not yet captured. Then open the Personal CA extension in Chrome and click <b>Fetch queued articles</b> — it reads each article in <i>your</i> signed-in browser and sends it here.</p>
    ${extBanner(lists.extension, lists.extension_path, pending)}
    <div class="grid g2">
      <div><h3>1 · News articles <span class="muted small">(${lists.news_articles.length})</span></h3>
        <p class="small muted">Any ET / ET Prime article. Each becomes a news source for whichever stock it mentions (or the stock you link it to) in future analyses.</p>
        <textarea id="l-news" rows="7" style="width:100%" placeholder="One ET link per line">${esc(lists.news_articles.join('\n'))}</textarea></div>
      <div><h3>2 · Stock-idea articles <span class="muted small">(${lists.idea_articles.length}, about 10)</span></h3>
        <p class="small muted">"Stocks to buy" style pieces. Every pick is extracted with a verbatim quote, ranked across articles, and you choose which to analyse.</p>
        <textarea id="l-ideas" rows="7" style="width:100%" placeholder="One ET Prime link per line">${esc(lists.idea_articles.join('\n'))}</textarea></div>
    </div>
    <div class="row" style="margin-top:10px"><button class="btn primary" id="l-save">Save lists &amp; queue new articles</button><button class="btn" id="l-recap" title="Queue every listed article again; changed content becomes a new version">Re-capture all</button><span class="small muted" id="l-msg"></span></div>
    <details style="margin-top:12px"><summary>Capture status — news (${st.news_articles.length}) · ideas (${st.idea_articles.length})</summary>
      <h4 style="margin-top:10px">News articles</h4>${listStatusTable(st.news_articles)}
      <h4 style="margin-top:10px">Stock-idea articles</h4>${listStatusTable(st.idea_articles)}</details>
    <div class="row" style="margin-top:12px"><b>Find more ET articles for a stock:</b><input id="scan-sym" type="text" placeholder="e.g. TCS" style="width:120px;text-transform:uppercase"><button class="btn sm" id="scan-go">Queue a topic scan</button>
      <span class="small muted">The extension opens ET's topic page for that company, collects article links (Prime first) and captures them.</span></div>
  </div>
  <div class="card"><div class="row"><h2 style="margin:0">Best stock ideas from your idea articles</h2><span class="spacer"></span>
      <span class="small muted">show</span><div class="seg" id="i-top">${[5, 8, 10].map((k) => `<button data-n="${k}" class="${k === ET.top ? 'on' : ''}">${k}</button>`).join('')}</div>
      <button class="btn primary" id="i-extract">Extract &amp; rank ideas</button></div>
    <p class="small muted">Score = 3 × articles positive (buy/accumulate) − 3 × articles negative + 1 × neutral mentions + 0.5 × named recommenders + target upside (capped). These are <b>attributed opinions from your articles</b>, not our valuation — the panel analysis decides.</p>
    <div id="ideas-body"><div class="muted small">Click <b>Extract &amp; rank ideas</b> after the idea articles are captured.</div></div>
  </div>
  <div class="card"><h3>Recent batches</h3>${batches.length ? `<div class="tbl-wrap"><table><tbody>${batches.map((b) => `<tr><td><a href="#/batch/${esc(b.batch_id)}">${dt(b.created_at)}</a></td><td>${statusPill(b.status)}</td><td>${b.symbols.map((s) => `<span class="pill">${esc(s)}</span>`).join(' ')}</td><td class="small muted">${esc(b.params.horizon)} · ${b.params.loops} loop(s) · ${esc(b.params.mode)}</td></tr>`).join('')}</tbody></table></div>` : '<div class="empty small">No batches yet.</div>'}</div>`;
  const save = async (recapture) => {
    const lines = (id) => $(id).value.split(/\s+/).filter(Boolean);
    try {
      const r = await api('/api/et/lists', { method: 'POST', body: { news_articles: lines('#l-news'), idea_articles: lines('#l-ideas'), recapture } });
      toast(`Saved · ${r.queued} queued for capture`);
      if (r.rejected.length || r.queue_rejected.length) { $('#l-msg').innerHTML = [...r.rejected, ...r.queue_rejected].map((x) => `<span class="res fail">${esc(x.url.slice(0, 50))}: ${esc(x.reason)}</span>`).join('<br>'); return; }
      viewET();
    } catch (e) { toast(e.message); }
  };
  const fn = $('#ext-fetch');
  if (fn) fn.onclick = () => {
    const id = lists.extension.id;
    if (!(window.chrome && chrome.runtime && chrome.runtime.sendMessage)) { toast('Open the extension side panel and click "Fetch queued articles now"'); return; }
    try {
      chrome.runtime.sendMessage(id, { type: 'fetchNow' }, (r) => {
        if (chrome.runtime.lastError || !r) { toast('Could not reach the extension — use its side panel'); return; }
        toast(r.error ? r.error : r.busy ? 'Already fetching' : `Fetched: ${r.done || 0} captured, ${r.failed || 0} failed`);
        viewET();
      });
      toast('Fetching in your browser…');
    } catch (e) { toast('Use the extension side panel: ' + e.message); }
  };
  if (pending && lists.extension.connected) setTimeout(() => { if (location.hash.startsWith('#/et')) viewET(); }, 20000);
  $('#l-save').onclick = () => save(false);
  $('#l-recap').onclick = () => save(true);
  $('#scan-go').onclick = async () => { const s = $('#scan-sym').value.trim(); if (!s) return; try { const r = await api('/api/et/scan', { method: 'POST', body: { symbol: s } }); toast(r.added.length ? 'Topic scan queued' : (r.rejected[0] || {}).reason || 'Not queued'); } catch (e) { toast(e.message); } };
  $('#i-top').onclick = (e) => { const n = e.target.dataset.n; if (n) { ET.top = Number(n); store.set('ca-idea-top', ET.top); $$('#i-top button').forEach((b) => b.classList.toggle('on', b.dataset.n === n)); loadIdeas(false); } };
  $('#i-extract').onclick = () => loadIdeas(true);
  loadIdeas(false);
}

async function loadIdeas(extract) {
  const body = $('#ideas-body'); if (!body) return;
  if (extract) { body.innerHTML = '<div class="muted">Extracting ideas from captured idea articles…</div>'; $('#i-extract').disabled = true; }
  try {
    const r = extract ? await api(`/api/ideas/extract?top=${ET.top}`, { method: 'POST' }) : await api(`/api/ideas?top=${ET.top}`);
    ET.ranking = r;
    body.innerHTML = ideasHtml(r);
    bindIdeas();
  } catch (e) { body.innerHTML = `<div class="notice bad">${esc(e.message)}</div>`; }
  const b = $('#i-extract'); if (b) b.disabled = false;
}

function ideasHtml(r) {
  const head = `<p class="small muted">${r.idea_articles_captured} of ${r.idea_articles_listed} idea article(s) captured · ${r.total_candidates} stock(s) found · prices: ${esc(r.price_source)}${r.method ? ` · extraction: ${esc(r.method)}` : ''}${r.processed !== undefined ? ` · ${r.processed} article(s) processed now` : ''}</p>
    ${(r.errors || []).length ? `<div class="notice bad">${r.errors.map(esc).join('<br>')}</div>` : ''}`;
  if (!r.top.length) return head + '<div class="empty">No ideas yet. Capture the idea articles, then click Extract & rank.</div>';
  return head + `<div class="tbl-wrap"><table><thead><tr><th></th><th>#</th><th>Stock</th><th>Score</th><th>Signals</th><th>Evidence</th></tr></thead><tbody>
    ${r.top.map((s, i) => `<tr><td><input type="checkbox" data-pick="${esc(s.symbol)}" ${ET.picked.has(s.symbol) ? 'checked' : ''}></td><td>${i + 1}</td>
      <td><b>${esc(s.symbol)}</b><div class="small muted">${esc(s.name)}</div></td><td><b>${s.score}</b></td>
      <td class="small">${esc(s.why)}</td>
      <td><details><summary class="small">${s.mentions.length} mention(s)</summary>${s.mentions.map((m) => `<div class="passage small"><span class="pill ${['buy', 'accumulate'].includes(m.stance) ? 'good' : ['reduce', 'sell', 'avoid'].includes(m.stance) ? 'bad' : ''}">${esc(m.stance.replace('_', ' '))}</span>
        ${m.target_price ? ` target ${fmt(m.target_price, 0)}` : ''}${m.horizon_text ? ` · ${esc(m.horizon_text)}` : ''}${m.recommended_by ? ` · by ${esc(m.recommended_by)}` : ''}<br>“${esc(m.quote)}” <span class="pid">${esc(m.passage_id)}</span><br>
        <span class="muted">${esc(m.rationale)} — <a href="#/source/${esc(m._src)}">${esc((m.source_title || '').slice(0, 60))}</a> · ${esc(m._method)}</span></div>`).join('')}</details></td></tr>`).join('')}
    </tbody></table></div>
    ${r.unresolved_mentions.length ? `<details class="small"><summary>${r.unresolved_mentions.length} mention(s) not matched to an NSE symbol</summary><ul>${r.unresolved_mentions.map((u) => `<li>${esc(u.mention)} — ${esc(u.source)}</li>`).join('')}</ul></details>` : ''}
    <div class="card tight" style="margin-top:12px"><h3>Analyse the stocks you pick</h3>
      <div class="row"><div><label class="small">Horizon</label><div class="seg" id="b-hz"><button data-v="short">Short</button><button data-v="medium" class="on">Medium</button><button data-v="long">Long</button></div></div>
        <div><label class="small">Loops</label><div class="seg" id="b-lp">${Array.from({ length: (HEALTH && HEALTH.analysis.max_loops) || 3 }, (_, i) => `<button data-v="${i + 1}" class="${i === 0 ? 'on' : ''}">${i + 1}</button>`).join('')}</div></div>
        <div><label class="small">Mode</label><div class="seg" id="b-md"><button data-v="live" class="${HEALTH && !HEALTH.panel_missing_keys.length ? 'on' : ''}">Live</button><button data-v="demo" class="${HEALTH && HEALTH.panel_missing_keys.length ? 'on' : ''}">Demo</button></div></div>
        <span class="spacer"></span><button class="btn primary" id="b-go">Analyse selected (<span id="b-n">${ET.picked.size}</span>)</button></div>
      <p class="small muted" id="b-est">Each stock gets the full panel (three brains, Head, Critic, rules). Live cost scales with stocks × loops; the budget is checked before starting. Demo mode uses the fictional company for every pick (UI preview only).</p></div>`;
}

function bindIdeas() {
  const segv = (id) => { const b = $(`${id} button.on`); return b ? b.dataset.v : null; };
  ['#b-hz', '#b-lp', '#b-md'].forEach((id) => { const el = $(id); if (el) el.onclick = (e) => { if (e.target.dataset.v) { $$(`${id} button`).forEach((b) => b.classList.toggle('on', b === e.target)); } }; });
  $$('[data-pick]').forEach((c) => (c.onchange = () => { c.checked ? ET.picked.add(c.dataset.pick) : ET.picked.delete(c.dataset.pick); $('#b-n').textContent = ET.picked.size; }));
  const go = $('#b-go'); if (!go) return;
  go.onclick = async () => {
    if (!ET.picked.size) { toast('Tick at least one stock'); return; }
    if (ET.picked.size > 10) { toast('Pick at most 10'); return; }
    go.disabled = true;
    try {
      const ideas = ET.ranking.top.filter((s) => ET.picked.has(s.symbol));
      const r = await api('/api/batches', { method: 'POST', body: { symbols: [...ET.picked], horizon: segv('#b-hz') || 'medium', loops: Number(segv('#b-lp') || 1), mode: segv('#b-md') || 'live', ideas } });
      location.hash = `#/batch/${r.batch_id}`;
    } catch (e) { toast(e.message); go.disabled = false; }
  };
}

async function viewBatch(id) {
  let timer = null;
  CLEANUP = () => clearTimeout(timer);
  const draw = async () => {
    const b = await api(`/api/batches/${id}`);
    const done = b.items.filter((i) => i.verdict).length;
    $('#app').innerHTML = `<div class="card"><a href="#/et">← ET Prime &amp; ideas</a><div class="row" style="margin-top:6px"><h2 style="margin:0">Batch analysis</h2>${statusPill(b.status)}
      <span class="pill">${esc(b.params.horizon)}</span><span class="pill">${b.params.loops} loop(s)</span>${b.params.mode === 'demo' ? '<span class="pill bad">DEMO</span>' : '<span class="pill">live</span>'}<span class="spacer"></span><span class="small muted">${done}/${b.items.length} complete</span></div>
      <div class="phase ${b.status === 'completed' ? 'done' : 'active'}" style="margin-top:10px"><div class="bar"><i style="width:${(done / b.items.length) * 100}%"></i></div></div>
      <p class="small muted">Sorted by outcome: actionable buys first, then wait, hold, insufficient data, avoid. Each row links to its full report (progress tracker, analyst reports, Head ↔ Critic, rules, sources).${b.report_path ? ` Combined report: <code>${esc(b.report_path)}</code>` : ''}</p>
      <div class="tbl-wrap"><table><thead><tr><th>Stock</th><th>Status</th><th>Permitted action</th><th>Head proposal</th><th>Governance</th><th>What blocks it</th><th>Summary</th><th></th></tr></thead><tbody>
      ${b.items.map((it) => { const v = it.verdict; const p = it.progress;
        return `<tr><td><b>${esc(it.symbol)}</b></td>
        <td>${v ? statusPill(it.run ? it.run.status : it.status) : it.status === 'running' ? `<span class="pill info">running${p ? ` · loop ${p.loop} · ${esc(p.current || '')}` : ''}</span>` : statusPill(it.status)}</td>
        <td>${v ? decisionPill(v.decision, v.actionable) : '—'}</td><td>${v ? decisionPill(v.head_decision) : '—'}</td><td>${v && v.governance != null ? v.governance : '—'}</td>
        <td class="small">${v ? esc(v.blockers.join(', ') || 'none') : ''}</td><td class="small">${v ? esc(v.summary) : esc((it.run && it.run.summary) || '')}</td>
        <td><a class="btn sm" href="#/run/${esc(it.run_id)}">Report</a></td></tr>`; }).join('')}</tbody></table></div></div>
      ${(b.params.ideas || []).length ? `<div class="card"><h3>Idea signals that led here</h3><ul class="small">${b.params.ideas.map((i) => `<li><b>${esc(i.symbol)}</b> (score ${esc(i.score)}) — ${esc(i.why)}</li>`).join('')}</ul><p class="small muted">Attributed opinions from your ET articles. The panel and rules above are what decide.</p></div>` : ''}`;
    if (b.status !== 'completed') timer = setTimeout(draw, 3000);
  };
  await draw();
}

/* ------------------------------------------------------------ portfolio */
async function viewPortfolio() {
  const r = await api('/api/portfolio/holdings');
  const s = r.status.connect, m = r.status.mcp;
  const pill = (st) => `<span class="pill ${st.state === 'connected' ? 'good' : ['login_required', 'error'].includes(st.state) ? 'warn' : ''}">${esc(st.state.replace('_', ' '))}</span>`;
  $('#app').innerHTML = `<div class="grid g2">
    <div class="card"><div class="row"><h2 style="margin:0">Kite MCP</h2><span class="pill info">recommended · free</span><span class="spacer"></span>${pill(m)}</div>
      <p class="small">Uses Zerodha's hosted Kite MCP server (the same one Claude Desktop/Code connect to). You log in on Zerodha's own page (OAuth); the token stays on this laptop. Gives holdings, quotes and daily candles — no paid Kite Connect app needed.</p>
      <p class="small muted">${esc(m.message)}</p>
      <div class="row">${m.state === 'connected' ? '<button class="btn sm" id="mcp-off">Disconnect</button>' : '<a class="btn primary sm" href="/kite-mcp/login">Connect Kite (MCP)</a>'}</div>
      <p class="small muted">Read-only by construction: only read tools (holdings, positions, margins, quotes, LTP, OHLC, historical data, instrument search) can be called; order/GTT tools are refused in code.</p></div>
    <div class="card"><div class="row"><h2 style="margin:0">Kite Connect</h2><span class="pill">optional · paid data</span><span class="spacer"></span>${pill(s)}</div>
      <p class="small muted">${esc(s.message)}</p>
      ${s.state !== 'not_configured' ? '<a class="btn sm" href="/kite/login">Log in to Kite Connect</a>' : `<p class="small">Only needed if you prefer your own Kite Connect app: create one at developers.kite.trade with redirect <code>http://127.0.0.1:${location.port || 8765}/kite/callback</code>, then <code>ca secrets set KITE_API_KEY</code> / <code>KITE_API_SECRET</code>.</p>`}
      <p class="small muted">Price source order in analyses: Kite Connect → Kite MCP → NSE bhavcopy.</p></div></div>
  <div class="card"><div class="row"><h2 style="margin:0">Holdings</h2><span class="spacer"></span>${r.via ? `<span class="pill">via ${esc(r.via)}</span>` : ''}</div>
    ${!r.holdings.length ? '<div class="empty">Connect Kite (MCP) to see your holdings here.</div>' : ''}
    ${r.holdings.length ? `<div class="tbl-wrap"><table><thead><tr><th>Symbol</th><th>Qty</th><th>Avg</th><th>LTP</th><th>P&L</th><th>Day</th><th></th></tr></thead><tbody>
      ${r.holdings.map((h) => `<tr><td><b>${esc(h.symbol)}</b></td><td>${h.quantity}</td><td>${fmt(h.average_price)}</td><td>${fmt(h.last_price)}</td><td class="res ${h.pnl >= 0 ? 'pass' : 'fail'}">${fmt(h.pnl, 0)}</td><td>${fmt(h.day_change_pct)}%</td>
      <td><button class="btn sm" data-sym="${esc(h.symbol)}">Review holding</button></td></tr>`).join('')}</tbody></table></div>
      <p class="small muted">Average price is not a tax-lot history; lot-level tax needs your broker's transaction export.</p>` : ''}</div>`;
  $$('[data-sym]').forEach((b) => (b.onclick = async () => { const r2 = await api('/api/runs', { method: 'POST', body: { symbol: b.dataset.sym, horizon: 'medium', action: 'continue_holding', mode: 'live' } }).catch((e) => toast(e.message)); if (r2) location.hash = `#/run/${r2.run_id}`; }));
  const off = $('#mcp-off'); if (off) off.onclick = async () => { await api('/api/kite-mcp/disconnect', { method: 'POST' }); toast('Kite MCP disconnected'); viewPortfolio(); };
  if (location.hash.includes('kite_mcp=failed')) toast('Kite MCP login did not complete — try Connect again');
  if (location.hash.includes('kite_mcp=connected')) toast(m.state === 'connected' ? 'Kite MCP connected' : 'Logged in, but the Kite MCP check failed: ' + m.message);
}

/* --------------------------------------------------------------- system */
async function viewSystem() {
  await refreshHealth();
  const h = HEALTH, p = await api('/api/pairing');
  const item = (ok, title, detail, fix) => `<div class="health-item"><span class="st-icon ${ok === true ? 'completed' : ok === false ? 'failed' : 'skipped'}">${ok === true ? '✓' : '!'}</span><div><b>${title}</b><div class="small muted">${detail}</div>${!ok && fix ? `<div class="small">${fix}</div>` : ''}</div></div>`;
  $('#app').innerHTML = `<div class="card" id="models-card"><div class="loading">Loading models…</div></div>
  <div class="grid g2"><div class="card"><h2>Readiness</h2>
    ${item(h.anthropic.configured, 'Anthropic API key', h.anthropic.configured ? 'Configured (OS keychain or .env; never sent to the browser)' : 'Missing', 'Run <code>ca secrets set ANTHROPIC_API_KEY</code>, then restart <code>ca serve</code>.')}
    ${item(h.openai.configured ? true : null, 'OpenAI API key', h.openai.configured ? 'Configured' : 'Not configured — only needed if a role uses OpenAI', 'Run <code>ca secrets set OPENAI_API_KEY</code>, then restart <code>ca serve</code>.')}
    ${item(!h.panel_missing_keys.length, 'Keys for the configured panel', h.panel_missing_keys.length ? 'Missing: ' + h.panel_missing_keys.map(esc).join(', ') : 'All panel roles can run', '')}
    ${item(h.kite_mcp.connected || h.kite.state === 'connected' ? true : null, 'Zerodha Kite (read-only)', h.kite_mcp.connected ? 'Kite MCP connected (holdings, quotes, candles)' : h.kite.state === 'connected' ? 'Kite Connect connected' : 'Not connected — prices fall back to NSE bhavcopy', '<a href="#/portfolio">Connect Kite (MCP) on the Portfolio page</a> — free, OAuth login on Zerodha\'s page.')}
    ${item(h.bhavcopy.trading_days >= 200 ? true : null, 'NSE bhavcopy store', `${h.bhavcopy.trading_days} trading days · last ${h.bhavcopy.last || '—'}`, 'Run <code>ca ingest-bhav --days 260</code> (fallback when Kite is not connected).')}
    ${item(h.policies_confirmed, 'Personal policies confirmed', 'Horizons, risk base and limits reviewed in config/user.yaml', 'Review the config files and set <code>policies_confirmed: true</code>. Until then everything is research-only.')}
    ${item(h.capital_configured ? true : null, 'Capital configured', 'Needed for position sizing and tranches', 'Set <code>total_investment_capital_inr</code> in config/user.yaml.')}
    ${item(h.tax_policy.review_status === 'verified', 'Tax policy verified', `${esc(h.tax_policy.version)} · ${esc(h.tax_policy.review_status)}`, 'Check config/tax_rates.yaml against the latest Finance Act and set <code>review_status: verified</code>. Until then after-tax figures are refused.')}
    <p class="small muted" style="margin-top:10px">Budget: $${h.budget.spent_usd.toFixed(2)} spent of $${h.budget.cap_usd} this month (hard cap in code)${h.budget.calls_with_unknown_price ? ` · ${h.budget.calls_with_unknown_price} call(s) had no price configured` : ''}.</p>
    <button class="btn sm" id="reload">Reload config files</button></div>
  <div class="card"><h2>Chrome extension</h2>
    <ol class="small"><li>Open <code>chrome://extensions</code>, enable <b>Developer mode</b>, click <b>Load unpacked</b>, choose the repo's <code>browser/chrome-extension</code> folder. After pulling updates, click the reload ↻ icon on the extension card.</li>
    <li>Click the extension icon → side panel → <b>Settings</b>, paste the pairing token below.</li>
    <li>Tick the personal-research attestation, click <b>Allow ET access</b> once.</li>
    <li><b>Queue mode:</b> add ET links on the Research inbox page (or "Add this tab" / "Scan page for articles" in the extension), then <b>Fetch queued articles</b>.</li>
    <li><b>Single article:</b> open it and click <b>Capture page</b> (or select text → <b>Capture selection</b>), then <b>Send</b>.</li></ol>
    <h4>Pairing token</h4><div class="row"><code id="tok" style="word-break:break-all">${esc(p.token)}</code></div>
    <div class="row" style="margin-top:8px"><button class="btn sm" id="copytok">Copy</button><button class="btn sm" id="rot">Rotate token</button></div>
    <p class="small muted">The service only listens on 127.0.0.1:${p.port}. The token authorises sending captures; rotate it if it leaks.</p></div></div>`;
  $('#copytok').onclick = () => navigator.clipboard.writeText(p.token).then(() => toast('Token copied')).catch(() => toast('Select and copy manually'));
  $('#rot').onclick = async () => { await api('/api/pairing/rotate', { method: 'POST' }); toast('Rotated — re-pair the extension and reload this page'); setTimeout(() => location.reload(), 900); };
  $('#reload').onclick = async () => { await api('/api/config/reload', { method: 'POST' }); toast('Config reloaded'); viewSystem(); };
  renderModels();
}

async function renderModels(refresh) {
  const m = await api('/api/models');
  const providers = Object.keys(m.providers);
  const cats = {};
  await Promise.all(providers.map(async (p) => { cats[p] = await api(`/api/models/catalog?provider=${p}${refresh ? '&refresh=true' : ''}`).catch(() => ({ models: [], live: false })); }));
  const roles = ['quant', 'news', 'forensic', 'head', 'critic', 'claims', 'discovery', 'research_qa'];
  const sep = m.separation;
  const opts = (p, cur) => {
    const ids = cats[p].models.map((x) => x.id);
    const list = ids.includes(cur) || !cur ? ids : [cur, ...ids];
    return list.map((id) => `<option value="${esc(id)}" ${id === cur ? 'selected' : ''}>${esc(id)}${(cats[p].models.find((x) => x.id === id) || {}).priced === false ? ' (no price set)' : ''}</option>`).join('') + '<option value="__custom">Custom model id…</option>';
  };
  $('#models-card').innerHTML = `<div class="row"><h2 style="margin:0">Models per role</h2><span class="spacer"></span>
      <span class="pill ${sep.status === 'separated' ? 'good' : sep.status === 'partial' ? 'info' : 'warn'}">Vendor separation: ${esc(sep.status)}</span>
      <button class="btn sm" id="m-refresh">Refresh model lists</button></div>
    <p class="small muted">Choose a provider and model for each role. Lists load live from each provider's Models API (newest first) once its key is configured. Tip: put the <b>Critic on a different provider than the Head</b> so they don't share blind spots. Saved to <code>data/settings/models_override.yaml</code> (version ${esc(m.version)}); every decision records the exact models used.</p>
    ${providers.map((p) => `<div class="small">${esc(m.providers[p].label)}: ${cats[p].live ? `<span class="res pass">live list (${cats[p].models.length})</span>` : `<span class="res unknown">${esc(cats[p].note || 'suggestions only')}</span>`}</div>`).join('')}
    <div class="tbl-wrap" style="margin-top:10px"><table><thead><tr><th>Role</th><th>Provider</th><th>Model</th><th>Effort</th></tr></thead><tbody>
    ${roles.map((r) => { const rc = m.roles[r]; const locked = m.locked[r]; const prov = rc.provider || 'anthropic';
      return `<tr data-role="${r}"><td><b>${esc(titleCase(r))}</b><div class="small muted">${esc(m.help[r] || '')}</div></td>
        <td><select class="m-prov" ${locked ? 'disabled title="Uses a provider-specific tool"' : ''}>${providers.map((p) => `<option value="${p}" ${p === prov ? 'selected' : ''}>${esc(m.providers[p].label)}</option>`).join('')}</select>${locked ? '<div class="small muted">locked</div>' : ''}</td>
        <td><select class="m-model">${opts(prov, rc.model)}</select><input class="m-custom" type="text" placeholder="model id" hidden style="margin-top:4px;width:100%"></td>
        <td><select class="m-effort">${['low', 'medium', 'high', 'xhigh', 'max'].map((e) => `<option ${e === (rc.effort || 'high') ? 'selected' : ''}>${e}</option>`).join('')}</select></td></tr>`; }).join('')}
    </tbody></table></div>
    <div id="m-price" class="small"></div>
    <div class="row" style="margin-top:10px"><button class="btn primary" id="m-save">Save model settings</button><span class="small muted">Takes effect for the next run.</span></div>`;
  $$('#models-card tr[data-role]').forEach((tr) => {
    const prov = $('.m-prov', tr), sel = $('.m-model', tr), cust = $('.m-custom', tr);
    prov.onchange = () => { sel.innerHTML = opts(prov.value, ''); cust.hidden = true; checkPrices(); };
    sel.onchange = () => { cust.hidden = sel.value !== '__custom'; checkPrices(); };
    cust.oninput = checkPrices;
  });
  function chosen() {
    return $$('#models-card tr[data-role]').map((tr) => {
      const sel = $('.m-model', tr); const model = sel.value === '__custom' ? $('.m-custom', tr).value.trim() : sel.value;
      return [tr.dataset.role, { provider: $('.m-prov', tr).value, model, effort: $('.m-effort', tr).value }];
    });
  }
  function checkPrices() {
    const unpriced = [...new Set(chosen().map(([, s]) => s.model).filter((id) => id && !m.pricing[id]))];
    $('#m-price').innerHTML = unpriced.length ? `<div class="notice" style="margin-top:10px">No price configured for: ${unpriced.map((id) => `<div class="row" style="margin-top:4px"><code>${esc(id)}</code> input $/M <input data-pin="${esc(id)}" type="text" style="width:80px"> output $/M <input data-pout="${esc(id)}" type="text" style="width:80px"></div>`).join('')}<div>Without a price, calls are recorded as "cost unknown" and the monthly cap cannot count them. Check the provider's pricing page.</div></div>` : '';
  }
  checkPrices();
  $('#m-refresh').onclick = () => renderModels(true);
  $('#m-save').onclick = async () => {
    const roles = Object.fromEntries(chosen());
    if (Object.values(roles).some((s) => !s.model)) { toast('Every role needs a model'); return; }
    const pricing = {};
    $$('[data-pin]').forEach((i) => { const id = i.dataset.pin; const inp = parseFloat(i.value), out = parseFloat($(`[data-pout="${CSS.escape(id)}"]`).value); if (!isNaN(inp) && !isNaN(out)) pricing[id] = { input: inp, output: out }; });
    try { await api('/api/models', { method: 'POST', body: { roles, pricing } }); toast('Model settings saved'); await refreshHealth(); renderModels(); }
    catch (e) { toast(e.message); }
  };
}

/* ----------------------------------------------------------------- boot */
(async () => {
  await refreshHealth();
  try { META = await api('/api/meta'); } catch (e) { /* retry later */ }
  route();
  setInterval(refreshHealth, 60000);
})();
