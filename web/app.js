/* Personal CA — local UI. Vanilla JS, no build step. */
'use strict';

const TOKEN = document.querySelector('meta[name="ca-token"]').content;
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
const fmt = (x, d = 2) => (x === null || x === undefined || Number.isNaN(x)) ? '—' : Number(x).toLocaleString('en-IN', { minimumFractionDigits: d, maximumFractionDigits: d });
const dt = (s) => s ? new Date(s).toLocaleString('en-IN', { dateStyle: 'medium', timeStyle: 'short' }) : '—';
const titleCase = (s) => String(s || '').replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());

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
  let saved = null;
  try { saved = localStorage.getItem('ca-theme'); } catch (e) { /* storage blocked */ }
  if (saved) document.documentElement.dataset.theme = saved;
  $('#theme-btn').onclick = () => {
    const cur = document.documentElement.dataset.theme || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    const next = cur === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem('ca-theme', next); } catch (e) { /* ignore */ }
  };
})();

let META = null, HEALTH = null;
const DECISION_TEXT = {
  buy: 'Buy', wait: 'Wait', avoid: 'Avoid', insufficient_data: 'Insufficient data', hold: 'Hold', reduce: 'Reduce', exit: 'Exit',
};
const WHO = { code: '⚙ code', claude: '✦ Claude', 'code+claude': '⚙ code + ✦ Claude' };
const FIX_HINTS = {
  demo_mode: 'DEMO uses a fictional company. Run in Live mode for a real decision.',
  policies_confirmed: 'Review config/horizons.yaml, config/rules.yaml and config/user.yaml, then set policies_confirmed: true.',
  surveillance: 'Surveillance status must be known. NSE quote must load, or record it in data/manual/<SYMBOL>.yaml.',
  liquidity: 'Average traded value is below the liquidity floor.',
  stale_data: 'Price is stale. Connect Kite or run `ca ingest-bhav`.',
  data_quality: 'A probable corporate action or data conflict must be resolved first.',
  pledge: 'Promoter pledge must be established (filing or manual facts file).',
  governance: 'Forensic coverage too low for a governance score, or score below minimum.',
  audit: 'Auditor opinion not established — add the annual report evidence.',
  required_evidence: 'A required report, the critique, or a blocker test is missing/unresolved.',
  reward_risk: 'Reward/risk below the policy minimum for short-term buys.',
  benchmark: 'After-tax comparison with the Nifty 50 ETF assumption failed or could not be made (verify config/tax_rates.yaml).',
  plan_validity: 'Entry/stop/target levels are inconsistent.',
  bearish_pattern: 'A confirmed bearish pattern blocks short-term buys.',
};

/* --------------------------------------------------------------- router */
const ROUTES = { '': viewHome, runs: viewRuns, run: viewRun, inbox: viewInbox, source: viewSource, portfolio: viewPortfolio, system: viewSystem };
let CLEANUP = null;
async function route() {
  if (CLEANUP) { CLEANUP(); CLEANUP = null; }
  const [path] = location.hash.replace(/^#\/?/, '').split('?');
  const [name, ...rest] = path.split('/');
  $$('#nav a').forEach((a) => a.classList.toggle('active', (a.dataset.route === (name || 'home')) || (name === 'run' && a.dataset.route === 'runs') || (name === 'source' && a.dataset.route === 'inbox')));
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
  } catch (e) { /* ignore */ }
}

/* ----------------------------------------------------------------- home */
async function viewHome() {
  const st = { horizon: 'medium', mode: HEALTH && HEALTH.anthropic.configured ? 'live' : 'demo', action: 'new_purchase' };
  const runs = await api('/api/runs?limit=6');
  $('#app').innerHTML = `
  <div class="hero">
    <div class="card">
      <h1>Analyse a stock</h1>
      <p class="muted">Three independent specialist brains study one frozen evidence bundle. A Head CA drafts, a Critic attacks, and code enforces the rules. Every claim links to its source.</p>
      ${HEALTH && !HEALTH.anthropic.configured ? `<div class="notice">No Anthropic API key yet — Live mode needs one (<code>ca secrets set ANTHROPIC_API_KEY</code>). Demo mode runs the whole pipeline on a fictional company.</div>` : ''}
      <form class="analyse-form" id="f">
        <div><label for="sym">NSE symbol</label>
          <div class="sym-wrap"><input id="sym" type="text" autocomplete="off" placeholder="e.g. TCS, INFY, HDFCBANK" spellcheck="false"><div class="suggest" id="sug" hidden></div></div></div>
        <div class="row">
          <div><label>Horizon</label><div class="seg" id="hz">
            <button type="button" data-v="short">Short ≈3m</button><button type="button" data-v="medium">Medium ≈6m</button><button type="button" data-v="long">Long ≈12m+</button></div></div>
          <div><label>Mode</label><div class="seg" id="md">
            <button type="button" data-v="live">Live</button><button type="button" data-v="demo">Demo</button></div></div>
        </div>
        <div><label for="act">Intended action</label>
          <select id="act"><option value="new_purchase">New purchase</option><option value="additional_purchase">Add to position</option>
          <option value="continue_holding">Review a holding</option><option value="reduce_exit">Reduce / exit</option></select></div>
        <div class="row"><button class="btn primary" id="go" type="submit">Run the panel →</button>
          <span class="muted small" id="est"></span></div>
      </form>
    </div>
    <div class="card">
      <h3>What happens when you click run</h3>
      <ol class="how">
        <li><div><b>Evidence</b> — prices from Kite/NSE, technicals computed in code, exchange data, tiered news and your ET Prime captures. Then the bundle is <i>frozen</i> and hashed.</div></li>
        <li><div><b>Three brains, blind to each other</b> — Quant (valuation & setup), News & Narrative (what changed), Forensic CA (can the numbers be trusted).</div></li>
        <li><div><b>Head & Critic</b> — reports are anonymised; the Head drafts; the Critic raises 0–7 objections with settling tests; the Head must answer each.</div></li>
        <li><div><b>Rules in code</b> — vetoes, sizing, tax and a consistency check. No prompt can override them.</div></li>
        <li><div><b>Ledger</b> — the decision, every model call and every source are stored so you can audit it later.</div></li>
      </ol>
      <p class="small muted" style="margin-top:12px">Every citation chip is checked by code: <span class="cite verified">ev ✓</span> quote found verbatim · <span class="cite id_only">ev</span> id only · <span class="cite quote_mismatch">ev ✗</span> not supported.</p>
    </div>
  </div>
  <div class="card"><div class="row"><h3 style="margin:0">Recent runs</h3><span class="spacer"></span><a href="#/runs">All runs →</a></div>${runsTable(runs)}</div>`;
  const seg = (id, key) => {
    const el = $(id);
    const paint = () => $$('button', el).forEach((b) => b.classList.toggle('on', b.dataset.v === st[key]));
    el.onclick = (e) => { if (e.target.dataset.v) { st[key] = e.target.dataset.v; paint(); est(); } };
    paint();
  };
  const est = () => {
    $('#est').textContent = st.mode === 'demo' ? 'Demo: fictional company, no model calls, $0'
      : 'Live: about 8 Claude calls and up to 9 web searches; a budget reservation is taken first';
    $('#sym').disabled = st.mode === 'demo';
    $('#sym').placeholder = st.mode === 'demo' ? 'DEMOCO (fictional)' : 'e.g. TCS, INFY, HDFCBANK';
  };
  seg('#hz', 'horizon'); seg('#md', 'mode'); est();
  const sym = $('#sym'), sug = $('#sug');
  try { const pre = sessionStorage.getItem('ca-sym'); if (pre) { sym.value = pre; sessionStorage.removeItem('ca-sym'); } } catch (e) { /* ignore */ }
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
  document.addEventListener('click', (e) => { if (!e.target.closest('.sym-wrap')) sug.hidden = true; }, { once: true });
  $('#f').onsubmit = async (e) => {
    e.preventDefault();
    st.action = $('#act').value;
    if (st.mode === 'live' && !sym.value.trim()) { toast('Enter an NSE symbol'); return; }
    $('#go').disabled = true;
    try {
      const r = await api('/api/runs', { method: 'POST', body: { symbol: sym.value.trim().toUpperCase(), horizon: st.horizon, action: st.action, mode: st.mode } });
      location.hash = `#/run/${r.run_id}`;
    } catch (err) { toast(err.message); $('#go').disabled = false; }
  };
}

function decisionPill(d, actionable) {
  if (!d) return '<span class="pill">—</span>';
  const cls = d === 'buy' ? (actionable ? 'good' : 'warn') : d === 'wait' ? 'warn' : ['avoid', 'exit', 'reduce'].includes(d) ? 'bad' : '';
  return `<span class="pill ${cls}">${esc(DECISION_TEXT[d] || d)}${actionable ? ' · actionable' : ''}</span>`;
}

function runsTable(runs) {
  if (!runs.length) return '<div class="empty">No runs yet. Try a Demo run to see the whole pipeline.</div>';
  return `<div class="tbl-wrap"><table><thead><tr><th>When</th><th>Stock</th><th>Horizon</th><th>Mode</th><th>Status</th><th>Decision</th><th>Cost</th><th>Summary</th></tr></thead><tbody>
  ${runs.map((r) => `<tr><td><a href="#/run/${esc(r.run_id)}">${dt(r.started_at)}</a></td><td><b>${esc(r.symbol)}</b></td><td>${esc(r.horizon)}</td>
  <td>${r.mode === 'demo' ? '<span class="pill bad">DEMO</span>' : '<span class="pill">live</span>'}</td><td>${statusPill(r.status)}</td>
  <td>${decisionPill(r.final_decision, r.actionable)}</td><td class="mono">$${(r.cost_usd || 0).toFixed(3)}</td><td class="small muted">${esc((r.summary || '').slice(0, 110))}</td></tr>`).join('')}
  </tbody></table></div>`;
}
const statusPill = (s) => `<span class="pill ${s === 'completed' ? 'good' : s === 'failed' ? 'bad' : s === 'running' ? 'info' : 'warn'}">${esc(s)}</span>`;

async function viewRuns() {
  const runs = await api('/api/runs?limit=200');
  $('#app').innerHTML = `<div class="card"><h2>Runs ledger</h2><p class="muted">Every run, partial or complete, is kept. Failed and partial runs never appear as completed verdicts.</p>${runsTable(runs)}</div>`;
}

/* ------------------------------------------------------------------ run */
let RUN = null;

async function viewRun(id) {
  RUN = { id, stages: {}, open: new Set(), events: [], detail: null, bundle: null, evIndex: {}, done: false, tab: 'overview' };
  META = META || await api('/api/meta');
  META.stages.forEach((s) => (RUN.stages[s.id] = { status: 'pending', msg: '', logs: [], dur: null, data: null }));
  $('#app').innerHTML = `<div id="run-head"></div><div class="card"><div class="phases" id="phases"></div><div class="pipeline" id="pipeline"></div></div><div id="results"></div>`;
  const detail = await api(`/api/runs/${id}`);
  RUN.detail = detail; setBundle(detail.bundle);
  renderRunHead();
  const es = new EventSource(`/api/runs/${id}/events`);
  es.onmessage = (m) => { const e = JSON.parse(m.data); if (RUN.events.find((x) => x.seq === e.seq)) return; applyEvent(e); };
  es.addEventListener('close', () => es.close());
  es.onerror = () => { if (RUN.done) es.close(); };
  CLEANUP = () => es.close();
  // if already complete & not streaming from memory, events arrive via replay
  renderTracker();
}

function setBundle(b) {
  if (!b) return;
  RUN.bundle = b; RUN.evIndex = {};
  b.evidence.forEach((e) => (RUN.evIndex[e.id] = e));
}

async function applyEvent(e) {
  RUN.events.push(e);
  const s = e.stage && RUN.stages[e.stage];
  if (e.type === 'stage_start' && s) { s.status = 'running'; s.msg = e.message; s.started = e.ts; }
  if (e.type === 'log' && s) s.logs.push({ ts: e.ts, m: e.message });
  if (e.type === 'stage_end' && s) {
    s.status = e.status; s.msg = e.message; s.dur = e.duration_ms; s.data = e.data;
    if (e.stage === 'bundle') { const d = await api(`/api/runs/${RUN.id}`); setBundle(d.bundle); }
  }
  if (e.type === 'run_start') RUN.start = e.data;
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
  $('#run-head').innerHTML = `<div class="run-head card"><div style="flex:1;min-width:240px">
    <div class="row"><h1>${esc(r.symbol)}</h1>${r.mode === 'demo' ? '<span class="pill bad">DEMO · fictional</span>' : '<span class="pill">live</span>'} ${statusPill(r.status)}</div>
    <div class="muted">${esc(b ? b.company_name : '')}${b && b.sector ? ' · ' + esc(b.sector) : ''}</div>
    <div class="run-meta"><span class="pill">Horizon: ${esc(r.horizon)}</span><span class="pill">Action: ${esc(titleCase(r.action))}</span>
    <span class="pill">Decision time ${dt(r.started_at)}</span>${b ? `<span class="pill mono" title="Frozen evidence bundle hash">bundle ${esc(b.bundle_id.slice(0, 12))}</span>` : ''}
    <span class="pill mono">$${(r.cost_usd || 0).toFixed(4)}</span></div></div>
    <div class="row"><a class="btn sm" href="#/">New analysis</a></div></div>
    ${RUN.done && RUN.endStatus === 'failed' ? `<div class="card notice bad"><b>Run failed:</b> ${esc(RUN.endMsg)}</div>` : ''}`;
}

function renderTracker() {
  if (!RUN || !$('#phases')) return;
  const byPhase = {};
  META.stages.forEach((s) => (byPhase[s.phase] = byPhase[s.phase] || []).push(s));
  $('#phases').innerHTML = META.phases.map((p) => {
    const ss = byPhase[p.id].map((s) => RUN.stages[s.id].status);
    const done = ss.filter((x) => !['pending', 'running'].includes(x)).length;
    const running = ss.includes('running'), fail = ss.includes('failed');
    const cls = done === ss.length ? (fail ? 'done fail' : 'done') : running ? 'active' : '';
    return `<div class="phase ${cls}"><div class="t"><span>${esc(p.title)}</span><span class="muted small">${done}/${ss.length}</span></div><div class="bar"><i style="width:${(done / ss.length) * 100}%"></i></div></div>`;
  }).join('');
  $('#pipeline').innerHTML = META.phases.map((p) => `
    <div><div class="lane-title"><h4 style="margin:0">${esc(p.title)}</h4><span class="line"></span>${p.id === 'brains' ? '<span class="small muted">run in parallel · blind to each other</span>' : ''}</div>
    <div class="stages ${p.id === 'brains' ? 'parallel' : ''}">${byPhase[p.id].map(stageCard).join('')}</div></div>`).join('');
  $$('.stage-h', $('#pipeline')).forEach((h) => (h.onclick = () => { const id = h.dataset.id; RUN.open.has(id) ? RUN.open.delete(id) : RUN.open.add(id); renderTracker(); }));
}

function stageCard(s) {
  const st = RUN.stages[s.id];
  const icon = { completed: '✓', failed: '!', skipped: '↷', degraded: '!' }[st.status] || '';
  const open = RUN.open.has(s.id) || (st.status === 'running');
  const logs = RUN.open.has(s.id) ? st.logs : st.logs.slice(-3);
  const dur = st.dur != null ? (st.dur / 1000).toFixed(1) + 's' : st.status === 'running' ? 'running…' : '';
  return `<div class="stage ${st.status}"><div class="stage-h" data-id="${s.id}"><span class="st-icon ${st.status}">${icon}</span>
    <span><span class="name">${esc(s.title)}</span> <span class="who">${esc(WHO[s.who] || s.who)}</span></span><span class="dur">${dur} ${st.logs.length ? (RUN.open.has(s.id) ? '▾' : '▸') : ''}</span></div>
    ${st.msg && st.status !== 'pending' ? `<div class="msg">${esc(st.msg)}</div>` : ''}
    ${open && logs.length ? `<div class="logs">${logs.map((l) => `<div><span class="ts">${esc(l.ts.slice(11, 19))}</span>${esc(l.m)}</div>`).join('')}</div>` : ''}</div>`;
}

/* --------------------------------------------------------------- results */
function stageData(id) { return RUN.stages[id] && RUN.stages[id].data; }

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
  const dr = RUN.detail.decision_record;
  if (!dr) { $('#results').innerHTML = ''; return; }
  const fa = dr.final_action, hp = dr.head_proposal;
  const blocking = dr.rule_results.filter((r) => ['blocked', 'unknown'].includes(r.result));
  const plan = fa.plan || ((hp.entry_low && hp.stop) ? hp : null);
  const sz = fa.sizing && fa.sizing.status === 'ok' ? fa.sizing : null;
  const rr = dr.calculations && dr.calculations.reward_risk;
  $('#results').innerHTML = `
  <div class="card"><div class="verdict">
    <div><div class="decision-box ${esc(fa.decision)}"><div class="small">Permitted action</div><div class="d">${esc(DECISION_TEXT[fa.decision] || fa.decision)}</div>
      <div class="small" style="margin-top:4px">${fa.actionable ? '✅ Actionable under your rules' : 'Research only — not actionable'}</div></div>
      <div class="kv" style="margin-top:12px"><div><span>Last close</span><b>${fmt(dr.price_at_decision)}</b></div><div><span>Head score</span><b>${hp.head_score ?? '—'}</b></div>
      <div><span>Governance</span><b>${dr.governance.score ?? 'withheld'}</b></div><div><span>Evidence</span><b>${esc(hp.evidence_sufficiency || '—')}</b></div></div></div>
    <div>
      <div class="flow"><span class="pill">Head draft: ${esc(DECISION_TEXT[(dr.head_draft || {}).decision] || '—')}</span><span class="arrow">→</span>
        <span class="pill">After critique: ${esc(DECISION_TEXT[hp.decision] || hp.decision)}</span><span class="arrow">→</span>
        <span class="pill ${fa.actionable ? 'good' : 'warn'}">Rules: ${esc(DECISION_TEXT[fa.decision] || fa.decision)}</span></div>
      <h2>${esc(hp.summary)}</h2>
      ${statementsHtml(hp.reasons)}
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
  <div class="card"><div class="tabs" id="tabs">${[['overview', 'Overview & chart'], ['analysts', 'Analyst reports'], ['panel', 'Head ↔ Critic'], ['rules', 'Rules'], ['sources', `Sources (${RUN.bundle ? RUN.bundle.evidence.length : 0})`], ['audit', 'Audit trail']].map(([k, t]) => `<button data-t="${k}" class="${RUN.tab === k ? 'on' : ''}">${t}</button>`).join('')}</div><div id="tab-body"></div></div>`;
  $('#tabs').onclick = (e) => { const t = e.target.dataset.t; if (t) { RUN.tab = t; $$('#tabs button').forEach((b) => b.classList.toggle('on', b.dataset.t === t)); renderTab(); } };
  renderTab();
}

function renderTab() {
  const dr = RUN.detail.decision_record, b = RUN.bundle, el = $('#tab-body');
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
      <h4 style="margin-top:16px">Learn</h4>${(dr.learn || []).map((l) => `<span class="pill info">${esc(titleCase(l))}</span> `).join('')}
    </div></div>`;
}

function tabAnalysts(dr) {
  const roles = [['quant', 'quant', 'Quant'], ['news', 'news_brain', 'News & Narrative'], ['forensic', 'forensic', 'Forensic CA']];
  return `<div class="legend-cites" style="margin-bottom:12px"><span><span class="cite verified">ev ✓</span> quote verified verbatim by code</span><span><span class="cite id_only">ev</span> id only (weaker)</span><span><span class="cite quote_mismatch">ev ✗</span> unsupported / not found</span><span>Click any chip to open the source passage.</span></div>
  <div class="grid g3">${roles.map(([role, stg, name]) => {
    const d = stageData(stg); const lbl = dr.labels[role];
    if (!d || !d.report) return `<div class="card analyst"><h3>${name}</h3><div class="notice bad">${esc(RUN.stages[stg].msg || 'Not available')}</div></div>`;
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
  const draft = dr.head_draft, res = dr.resolution || { rows: [] };
  const crit = dr.critique;
  return `<div class="grid g2"><div><h3>1 · Head draft</h3>${draft ? `<p>${decisionPill(draft.decision)} ${esc(draft.summary)}</p>${statementsHtml(draft.reasons)}
      ${draft.material_dissent.length ? `<p class="small"><b>Dissent:</b> ${draft.material_dissent.map(esc).join('; ')}</p>` : ''}` : '<div class="muted">No draft.</div>'}</div>
    <div><h3>3 · Final proposal</h3><p>${decisionPill(dr.head_proposal.decision)} ${esc(dr.head_proposal.summary)}</p>
      <p class="small muted">Open blockers after code resolution: <b>${esc(res.open_blockers.join(', ') || 'none')}</b>${res.open_major && res.open_major.length ? ` · unresolved major: ${esc(res.open_major.join(', '))}` : ''}</p></div></div>
    <h3 style="margin-top:12px">2 · Critic objections and settling tests</h3>
    ${crit ? `<p class="small muted">Checklist reviewed: ${crit.checklist.map(esc).join(' · ')}</p>` : ''}
    ${!res.rows.length ? `<div class="muted">${crit ? 'The Critic raised no material objections (zero is a valid result).' : 'No critique recorded.'}</div>` : res.rows.map((row) => {
      const o = row.objection, r = row.response || {};
      return `<div class="obj ${esc(o.severity)}"><div class="row"><b>${esc(o.id)}</b><span class="pill ${o.severity === 'blocker' ? 'bad' : o.severity === 'major' ? 'warn' : ''}">${esc(o.severity)}</span><span class="pill">${esc(titleCase(o.type))}</span><span class="spacer"></span>Effective: <span class="res ${esc(row.effective_result)}">${esc(row.effective_result)}</span></div>
        <div>${esc(o.text)} ${chips(o.cites)}</div><div class="small muted"><b>Impact:</b> ${esc(o.decision_impact)} · <b>Settling test:</b> ${esc(o.settling_test.description)}</div>
        <div class="small"><b>Head:</b> ${esc(r.action || 'no response')} — ${esc(r.explanation || '')} ${chips(r.cites)} <span class="muted">(effect: ${esc(r.decision_effect || '—')})</span></div>
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
      <div class="small muted">${esc(e.publisher)} · ${esc(titleCase(e.kind))} · published ${esc(e.published_at ? dt(e.published_at) : (e.published_hint ? '≈ ' + e.published_hint : 'unknown'))} · first seen ${dt(e.first_seen_at)} · access ${esc(e.access_status)}
      ${e.url ? ` · <a href="${esc(e.url)}" target="_blank" rel="noopener noreferrer">open source ↗</a>` : ''}</div>
      ${e.notes ? `<div class="small muted">${esc(e.notes)}</div>` : ''}
      <details><summary class="small">${e.passages.length} passage(s)</summary>${e.passages.map((p) => `<div class="passage"><span class="pid">${p.id}</span> ${esc(p.text)}</div>`).join('')}</details></div>`).join('')}</div>`;
}
function bindSourceFilter() {
  $('#tier-filter').onclick = (e) => { const b = e.target.closest('[data-tier]'); if (!b) return; const t = b.dataset.tier; $$('#ev-list .ev').forEach((el) => (el.hidden = t && el.dataset.tier !== t)); };
}

function tabAudit(dr, b) {
  const calls = RUN.detail.calls || [];
  return `<div class="grid g2"><div><h4>WhatsApp text</h4><div class="wa">${esc(dr.whatsapp)}</div><button class="btn sm" id="copy-wa" style="margin-top:8px">Copy</button></div>
  <div><h4>Identity & versions</h4><table><tbody>
    <tr><td>Verdict</td><td class="mono">${esc(dr.verdict_id)}</td></tr><tr><td>Bundle hash</td><td class="mono">${esc(dr.bundle_id)}</td></tr>
    <tr><td>Evaluation contract</td><td class="mono">${esc(dr.evaluation_contract.id)}</td></tr>
    <tr><td>Analyst labels</td><td>${Object.entries(dr.labels).map(([r, l]) => `${l} = ${esc(titleCase(r))}`).join(' · ')}</td></tr>
    <tr><td>Vendor separation</td><td><span class="pill warn">${esc(dr.vendor_separation)}</span> <span class="small muted">all roles on Anthropic models</span></td></tr>
    <tr><td>Fallback substitutions</td><td>${dr.fallback_events.length ? esc(JSON.stringify(dr.fallback_events)) : 'none'}</td></tr>
    <tr><td>Prompts</td><td class="small">${Object.entries(dr.versions.prompts).map(([k, v]) => esc(v)).join(' · ')}</td></tr>
    <tr><td>Config</td><td class="small">${['rules', 'models', 'tax_rates', 'horizons', 'features'].map((k) => esc(dr.versions[k])).join(' · ')}</td></tr>
    <tr><td>Report file</td><td class="small mono">${esc(dr.report_path)}</td></tr></tbody></table></div></div>
  <h4 style="margin-top:14px">Model calls (${calls.length}) · total $${(dr.cost_usd || 0).toFixed(4)}</h4>
  <div class="tbl-wrap"><table><thead><tr><th>Role</th><th>Model</th><th>Prompt</th><th>Tokens in/out</th><th>Searches</th><th>Cost</th><th>Latency</th><th>Status</th><th>Provider id</th></tr></thead><tbody>
  ${calls.map((c) => `<tr><td>${esc(c.role)}</td><td class="mono">${esc(c.model)}</td><td class="small">${esc(c.prompt_version)}</td><td class="mono">${(c.input_tokens || 0).toLocaleString()} / ${(c.output_tokens || 0).toLocaleString()}</td><td>${c.web_searches || 0}</td><td class="mono">$${(c.cost_usd || 0).toFixed(4)}</td><td>${((c.latency_ms || 0) / 1000).toFixed(1)}s</td><td>${esc(c.status)}</td><td class="mono small">${esc(c.provider_request_id || '—')}</td></tr>`).join('')}</tbody></table></div>`;
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
  const ticks = 5; let grid = '';
  for (let i = 0; i <= ticks; i++) { const v = lo + (hi - lo) * i / ticks; grid += `<line x1="${P.l}" x2="${W - P.r}" y1="${y(v)}" y2="${y(v)}" stroke="var(--border)" stroke-width="1"/><text x="${W - P.r + 6}" y="${y(v) + 4}" font-size="11" fill="var(--muted)">${fmt(v, 0)}</text>`; }
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
  if (c) openSource(c.dataset.ev, c.dataset.pid, c.dataset.q, c.dataset.st);
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
    unknown_evidence: '✗ This evidence id does not exist in the bundle (fabricated or out of boundary).' }[status] || '';
  if (!e) { $('#drawer-body').innerHTML = `<div class="notice bad">${esc(statusText || 'Evidence not found in this bundle.')}</div>${quote ? `<p>Quoted: “${esc(quote)}”</p>` : ''}`; $('#drawer').hidden = false; return; }
  const tiers = (META && META.tiers) || {};
  $('#drawer-body').innerHTML = `<div class="notice ${status === 'verified' ? 'info' : status === 'id_only' ? '' : 'bad'}">${esc(statusText)}</div>
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
  const rows = await api('/api/inbox');
  $('#app').innerHTML = `<div class="card"><h2>Research inbox</h2>
    <p class="muted">Articles you captured in Chrome (e.g. ET Prime) with the Personal CA extension. Captures are <b>attributed, unverified</b> research inputs: importing never verifies a claim. Only captures where you attested personal-research use are sent to a model.</p>
    ${rows.length ? `<div class="tbl-wrap"><table><thead><tr><th>Captured</th><th>Title</th><th>Publisher</th><th>Companies</th><th>Access</th><th>Usage</th><th>Claims</th></tr></thead><tbody>
    ${rows.map((r) => `<tr><td>${dt(r.ingested_at)}</td><td><a href="#/source/${esc(r.source_id)}">${esc(r.title || r.canonical_url)}</a><div class="small muted">v${r.version} · ${r.n_passages} passages</div></td><td>${esc(r.publisher)}</td>
      <td>${(r.company_links.resolved || []).map((c) => `<span class="pill info">${esc(c.symbol)}</span>`).join(' ')}${(r.company_links.ambiguous || []).length ? ' <span class="pill warn">ambiguous</span>' : ''}</td>
      <td><span class="pill ${r.access_status === 'partially_read' ? 'warn' : ''}">${esc(r.access_status)}</span></td>
      <td><span class="pill ${r.usage_status.includes('attested') && !r.usage_status.includes('unattested') ? 'good' : 'warn'}">${r.usage_status.includes('unattested') ? 'not attested' : 'attested'}</span></td><td>${r.claims}</td></tr>`).join('')}</tbody></table></div>`
    : `<div class="empty">Inbox is empty. Install the extension (System → Chrome extension), open an article, and click <b>Send to Personal CA</b>.</div>`}</div>`;
}

async function viewSource(id) {
  const s = await api('/api/inbox/' + encodeURIComponent(id));
  const attested = s.usage_status === 'personal_research_user_attested';
  $('#app').innerHTML = `<div class="card"><a href="#/inbox">← Inbox</a><h2 style="margin-top:8px">${esc(s.title)}</h2>
    <div class="row"><span class="tier T4_capture_unverified">User capture (unverified)</span><span class="pill">${esc(s.publisher)}</span><span class="pill">v${s.version}</span><span class="pill">${esc(s.access_status)}</span>
    <a href="${esc(s.canonical_url)}" target="_blank" rel="noopener noreferrer">open original ↗</a></div>
    <p class="small muted">Published ${esc(s.published_at || 'unknown')} · captured ${dt(s.captured_at)} · fingerprint <span class="mono">${esc(s.fingerprint.slice(0, 16))}</span>${s.warnings.length ? ' · warnings: ' + s.warnings.map(esc).join(', ') : ''}</p>
    ${!attested ? '<div class="notice">Usage for personal research was not attested at capture — this source will not be sent to any model.</div>' : ''}
    <div class="row"><b>Companies:</b> ${(s.company_links.resolved || []).map((c) => `<a class="pill info" href="#/" onclick="sessionStorage.setItem('ca-sym','${esc(c.symbol)}')">${esc(c.symbol)} · ${esc(c.name)}</a>`).join(' ') || '<span class="muted">none resolved</span>'}
      ${(s.company_links.ambiguous || []).map((a) => `<span class="pill warn" title="Not attached: ambiguous">${esc(a.matched)}: ${a.candidates.map(esc).join('/')}</span>`).join(' ')}</div></div>
  <div class="grid g2"><div class="card"><div class="row"><h3 style="margin:0">Claims (${s.claims.length})</h3><span class="spacer"></span><button class="btn sm" id="xc" ${attested ? '' : 'disabled'}>Extract claims with Claude</button></div>
    <div id="claims">${claimsHtml(s.claims)}</div></div>
    <div class="card"><h3>Ask about this capture <span class="pill warn">research only</span></h3><p class="small muted">Answers cite exact passages via the Citations API. This is not a panel verdict.</p>
    <textarea id="q" rows="3" style="width:100%" placeholder="e.g. What target and horizon does the article give, and who is the source?"></textarea>
    <div class="row" style="margin-top:8px"><button class="btn primary sm" id="ask" ${attested ? '' : 'disabled'}>Ask</button></div><div id="ans"></div></div></div>
  <div class="card"><h3>Captured passages</h3>${s.passages.map((p) => `<div class="passage"><span class="pid">${p.id}</span> ${esc(p.text)}</div>`).join('')}</div>`;
  const xc = $('#xc'); if (xc) xc.onclick = async () => { xc.disabled = true; xc.textContent = 'Extracting…'; try { const c = await api(`/api/inbox/${id}/claims`, { method: 'POST' }); $('#claims').innerHTML = claimsHtml(c); } catch (e) { toast(e.message); } xc.disabled = false; xc.textContent = 'Extract claims with Claude'; };
  $('#ask').onclick = async () => { const q = $('#q').value.trim(); if (!q) return; $('#ans').innerHTML = '<p class="muted">Thinking…</p>';
    try { const a = await api('/v1/research/questions', { method: 'POST', body: { source_ids: [id], question: q } });
      $('#ans').innerHTML = `<div class="passage" style="margin-top:10px">${a.parts.map((p) => esc(p.text) + p.citations.map((c) => ` <span class="cite verified" title="${esc(c.cited_text)}">${esc(c.passage_ids.map((x) => x.split(':').pop()).join(','))} ✓</span>`).join('')).join('')}</div><p class="small muted">${esc(a.note)} · $${(a.cost_usd || 0).toFixed(4)}</p>`;
    } catch (e) { $('#ans').innerHTML = `<div class="notice bad">${esc(e.message)}</div>`; } };
}
function claimsHtml(cs) {
  if (!cs.length) return '<div class="muted small">No claims extracted yet.</div>';
  return cs.map((c) => `<div class="obj minor"><div class="row"><span class="pill">${esc(titleCase(c.claim_type))}</span>${c.symbol ? `<span class="pill info">${esc(c.symbol)}</span>` : `<span class="pill warn">${esc(c.company_mention)} (unresolved)</span>`}<span class="pill warn">${esc(c.status)}</span>${c.direction ? `<span class="pill">${esc(c.direction)}</span>` : ''}</div>
    <div>${esc(c.text)}</div><div class="small muted">“${esc(c.quote)}” — ${esc(c.passage_id)}${c.author_or_broker ? ' · ' + esc(c.author_or_broker) : ''}${c.target_price ? ' · target ' + fmt(c.target_price, 0) : ''}</div>
    <div class="small"><b>Verify:</b> ${esc(c.verification_task)}</div></div>`).join('');
}

/* ------------------------------------------------------------ portfolio */
async function viewPortfolio() {
  const r = await api('/api/portfolio/holdings');
  const s = r.status;
  $('#app').innerHTML = `<div class="card"><div class="row"><h2 style="margin:0">Portfolio (Kite, read-only)</h2><span class="spacer"></span>
    <span class="pill ${s.state === 'connected' ? 'good' : s.state === 'login_required' ? 'warn' : ''}">${esc(s.state.replace('_', ' '))}</span>
    ${s.state !== 'not_configured' ? '<a class="btn sm primary" href="/kite/login">Log in to Kite</a>' : ''}</div>
    <p class="muted">${esc(s.message)} The adapter only exposes read operations (holdings, positions, quotes, candles). No order tools exist in this platform.</p>
    ${s.state === 'not_configured' ? `<div class="notice info">To connect: create an app at developers.kite.trade with redirect URL <code>http://127.0.0.1:${location.port || 8765}/kite/callback</code>, then run <code>ca secrets set KITE_API_KEY</code> and <code>ca secrets set KITE_API_SECRET</code>. Without Kite, prices come from NSE bhavcopy.</div>` : ''}
    ${r.holdings.length ? `<div class="tbl-wrap"><table><thead><tr><th>Symbol</th><th>Qty</th><th>Avg</th><th>LTP</th><th>P&L</th><th>Day</th><th></th></tr></thead><tbody>
      ${r.holdings.map((h) => `<tr><td><b>${esc(h.symbol)}</b></td><td>${h.quantity}</td><td>${fmt(h.average_price)}</td><td>${fmt(h.last_price)}</td><td class="res ${h.pnl >= 0 ? 'pass' : 'fail'}">${fmt(h.pnl, 0)}</td><td>${fmt(h.day_change_pct)}%</td>
      <td><button class="btn sm" data-sym="${esc(h.symbol)}">Review holding</button></td></tr>`).join('')}</tbody></table></div>
      <p class="small muted">Average price is not a tax-lot history; lot-level tax needs your broker's transaction export.</p>` : ''}</div>`;
  $$('[data-sym]').forEach((b) => (b.onclick = async () => { const r2 = await api('/api/runs', { method: 'POST', body: { symbol: b.dataset.sym, horizon: 'medium', action: 'continue_holding', mode: 'live' } }).catch((e) => toast(e.message)); if (r2) location.hash = `#/run/${r2.run_id}`; }));
}

/* --------------------------------------------------------------- system */
async function viewSystem() {
  await refreshHealth();
  const h = HEALTH, p = await api('/api/pairing');
  const item = (ok, title, detail, fix) => `<div class="health-item"><span class="st-icon ${ok === true ? 'completed' : ok === false ? 'failed' : 'skipped'}">${ok === true ? '✓' : '!'}</span><div><b>${title}</b><div class="small muted">${detail}</div>${!ok && fix ? `<div class="small">${fix}</div>` : ''}</div></div>`;
  $('#app').innerHTML = `<div class="grid g2"><div class="card"><h2>Readiness</h2>
    ${item(h.anthropic.configured, 'Anthropic API key', h.anthropic.configured ? 'Configured (kept in OS keychain or .env; never sent to the browser)' : 'Missing', 'Run <code>ca secrets set ANTHROPIC_API_KEY</code>, then restart <code>ca serve</code>.')}
    ${item(h.kite.state === 'connected' ? true : h.kite.state === 'not_configured' ? null : false, 'Zerodha Kite (read-only)', esc(h.kite.message), '<a href="#/portfolio">Connect on the Portfolio page</a>')}
    ${item(h.bhavcopy.trading_days >= 200 ? true : null, 'NSE bhavcopy store', `${h.bhavcopy.trading_days} trading days · last ${h.bhavcopy.last || '—'}`, 'Run <code>ca ingest-bhav --days 260</code> (fallback when Kite is not connected).')}
    ${item(h.policies_confirmed, 'Personal policies confirmed', 'Horizons, risk base and limits reviewed in config/user.yaml', 'Review the config files and set <code>policies_confirmed: true</code>. Until then everything is research-only.')}
    ${item(h.capital_configured ? true : null, 'Capital configured', 'Needed for position sizing', 'Set <code>total_investment_capital_inr</code> in config/user.yaml.')}
    ${item(h.tax_policy.review_status === 'verified', 'Tax policy verified', `${esc(h.tax_policy.version)} · ${esc(h.tax_policy.review_status)}`, 'Check config/tax_rates.yaml against the latest Finance Act and set <code>review_status: verified</code>. Until then after-tax figures are refused.')}
    ${item(null, 'Vendor separation', `${esc(h.vendor_separation)} — all roles use Anthropic models: ${Object.entries(h.models).map(([r, m]) => esc(r) + '=' + esc(m)).join(', ')}`, '')}
    <p class="small muted" style="margin-top:10px">Budget: $${h.budget.spent_usd.toFixed(2)} spent of $${h.budget.cap_usd} this month (hard cap in code).</p>
    <button class="btn sm" id="reload">Reload config files</button></div>
  <div class="card"><h2>Chrome extension</h2>
    <ol class="small"><li>Open <code>chrome://extensions</code>, enable <b>Developer mode</b>, click <b>Load unpacked</b>, choose the repo's <code>browser/chrome-extension</code> folder.</li>
    <li>Click the extension icon → side panel → <b>Settings</b>, paste the pairing token below.</li>
    <li>On an ET Prime article you can read, click <b>Allow ET & capture</b> (or select text → <b>Capture selection</b>).</li>
    <li>Check the passages, tick <b>personal research use</b>, then <b>Send to Personal CA</b>. You get a receipt; the capture appears in the Research inbox.</li></ol>
    <h4>Pairing token</h4><div class="row"><code id="tok" style="word-break:break-all">${esc(p.token)}</code></div>
    <div class="row" style="margin-top:8px"><button class="btn sm" id="copytok">Copy</button><button class="btn sm" id="rot">Rotate token</button></div>
    <p class="small muted">The service only listens on 127.0.0.1:${p.port}. The token authorises sending captures; rotate it if it leaks.</p></div></div>`;
  $('#copytok').onclick = () => navigator.clipboard.writeText(p.token).then(() => toast('Token copied')).catch(() => toast('Select and copy manually'));
  $('#rot').onclick = async () => { await api('/api/pairing/rotate', { method: 'POST' }); toast('Rotated — re-pair the extension and reload this page'); setTimeout(() => location.reload(), 900); };
  $('#reload').onclick = async () => { await api('/api/config/reload', { method: 'POST' }); toast('Config reloaded'); viewSystem(); };
}

/* ----------------------------------------------------------------- boot */
(async () => {
  await refreshHealth();
  try { META = await api('/api/meta'); } catch (e) { /* retry later */ }
  route();
  setInterval(refreshHealth, 60000);
})();
