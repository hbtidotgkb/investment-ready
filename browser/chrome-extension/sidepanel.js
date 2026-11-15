/* global personalCaExtract */
'use strict';

const VERSION = chrome.runtime.getManifest().version;
const ET_ORIGIN = 'https://economictimes.indiatimes.com/*';
const ET_PREFIX = 'https://economictimes.indiatimes.com/';
const MAX_SOURCES = 20;          // spec §7 future discovery budget: up to 20 article pages per session
const MAX_CHARS = 80000;
const PAGE_TIMEOUT_MS = 45000;
const RENDER_WAIT_MS = 2500;     // let client-side rendering finish before reading visible text
const BETWEEN_PAGES_MS = 3000;   // polite pacing between article loads
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let state = { sources: [], sessionId: null };
let settings = { svc: 'http://127.0.0.1:8765', tok: '' };
let queueRun = { active: false, cancel: false, items: [] };

function msg(text, kind = '') { const m = $('#msg'); m.textContent = text; m.className = 'msg ' + kind; m.hidden = !text; }
const svc = (p) => settings.svc.replace(/\/$/, '') + p;

async function call(path, opts = {}) {
  const r = await fetch(svc(path), { ...opts, headers: { 'content-type': 'application/json', 'x-ca-token': settings.tok, ...(opts.headers || {}) } });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || j.detail || `HTTP ${r.status}`);
  return j;
}

async function load() {
  const s = await chrome.storage.session.get(['sources', 'sessionId', 'attest']);   // cleared on browser restart
  state.sources = s.sources || [];
  state.sessionId = s.sessionId || ('rs-' + crypto.randomUUID());
  $('#attest').checked = !!s.attest;
  const l = await chrome.storage.local.get(['svc', 'tok']);
  settings.svc = l.svc || settings.svc; settings.tok = l.tok || '';
  $('#svc').value = settings.svc; $('#tok').value = settings.tok;
  await save(); render(); checkConn(); refreshQueue();
}
const save = () => chrome.storage.session.set({ sources: state.sources, sessionId: state.sessionId, attest: $('#attest').checked });

async function checkConn() {
  const c = $('#conn');
  try {
    const j = await (await fetch(svc('/v1/health'))).json();
    c.textContent = settings.tok ? `connected · v${j.version}` : 'connected · not paired';
    c.className = 'pill ' + (settings.tok ? 'good' : 'warn');
  } catch (e) { c.textContent = 'service offline'; c.className = 'pill bad'; }
}

async function activeTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab;
}

/* ------------------------------------------------------------ capturing */
async function extractFromTab(tabId, mode) {
  const [res] = await chrome.scripting.executeScript({ target: { tabId }, func: personalCaExtract, args: [mode, MAX_CHARS] });
  return res && res.result;
}

function addSource(r, extra = {}) {
  const item = { ...r, capture_id: crypto.randomUUID(), captured_at: new Date().toISOString(), selected: true, ...extra };
  if (state.sources.length >= MAX_SOURCES) state.sources.shift();
  state.sources.push(item);
  return item;
}

async function capture(mode) {
  msg('');
  const tab = await activeTab();
  if (!tab) { msg('No active tab.', 'bad'); return; }
  let r;
  try { r = await extractFromTab(tab.id, mode); } catch (e) {
    msg('No access to this page. On ET use "Allow ET access". On other sites, click the toolbar icon on that tab first (temporary access), then capture. ' + (e.message || ''), 'bad');
    return;
  }
  if (!r || r.error) { msg((r && r.error) || 'Capture failed.', 'bad'); return; }
  if (!r.passages.length) { msg('No readable text found. Try selecting the article text and "Capture selection".', 'bad'); return; }
  addSource(r); await save(); render();
  const chars = r.passages.reduce((a, p) => a + p.length, 0);
  msg(`Captured ${r.passages.length} passages (${chars.toLocaleString()} chars). Check the last paragraphs and any tables against the page, then Send.`, 'good');
}

function render() {
  $('#count').textContent = `${state.sources.length}/${MAX_SOURCES}`;
  const q = $('#search').value.trim().toLowerCase();
  $('#list').innerHTML = state.sources.map((s, i) => {
    const ps = s.passages.map((t, j) => ({ t, id: 'P' + (j + 1) })).filter((p) => !q || p.t.toLowerCase().includes(q));
    const hl = (t) => { if (!q) return esc(t); const k = t.toLowerCase().indexOf(q); return k < 0 ? esc(t) : esc(t.slice(0, k)) + '<mark>' + esc(t.slice(k, k + q.length)) + '</mark>' + esc(t.slice(k + q.length)); };
    return `<div class="src"><div class="row"><input type="checkbox" data-sel="${i}" ${s.selected ? 'checked' : ''} title="Include when sending"><span class="t">${esc(s.title || '(untitled)')}</span><span class="spacer"></span>${s.sent ? '<span class="pill good">sent</span>' : ''}<button class="btn sm" data-rm="${i}">Remove</button></div>
      <div class="u">${esc(s.url)}</div>
      <div class="u">${s.passages.length} passages · ${esc(s.extraction_method)} · published ${esc(s.published_at || 'unknown')}${s.author ? ' · ' + esc(s.author) : ''}${s.queue_id ? ' · from queue' : ''}</div>
      ${s.access_warnings.filter((w) => w !== 'capture_unverified').map((w) => `<div class="w">⚠ ${esc(w)}</div>`).join('')}
      <details ${q ? 'open' : ''}><summary>${q ? `${ps.length} matching passage(s)` : 'Inspect passages'}</summary>${ps.slice(0, 200).map((p) => `<div class="p"><b>${p.id}</b> ${hl(p.t)}</div>`).join('')}</details></div>`;
  }).join('') || '<p class="hint">No captures yet in this browser session.</p>';
}

function itemPayload(s) {
  return {
    capture_id: s.capture_id, url: s.url, title: (s.title || '').slice(0, 500), author: s.author ? s.author.slice(0, 300) : null,
    published_at: s.published_at ? s.published_at.slice(0, 60) : null, captured_at: s.captured_at, extraction_method: s.extraction_method,
    passages: s.passages.map((t, j) => ({ id: 'P' + (j + 1), text: t.slice(0, 20000) })), access_warnings: s.access_warnings.slice(0, 20),
    truncated: !!s.truncated, related_links: (s.related_links || []).slice(0, 50), usage_attested: $('#attest').checked,
    queue_id: s.queue_id || null,
  };
}
const batchOf = (items) => ({ schema_version: 'capture@1', research_session_id: state.sessionId, extension_version: VERSION, items: items.map(itemPayload) });

function receiptHtml(j) {
  return `<div class="rc"><b>Receipt ${esc(j.receipt_id)}</b>${j.replayed ? ' (already received — idempotent)' : ''}
    ${j.items.map((it) => `<div>${it.status === 'accepted' ? '✅' : it.status === 'duplicate' ? '↺' : '✖'} ${esc(it.status)} ${it.source_id ? esc(it.source_id) + ' v' + it.version : ''} ${it.reason ? '— ' + esc(it.reason) : ''}
    ${it.companies && it.companies.resolved.length ? '· ' + it.companies.resolved.map((c) => esc(c.symbol)).join(', ') : ''}${it.companies && it.companies.ambiguous.length ? ' · ambiguous names left unresolved' : ''}</div>`).join('')}
    ${j.claims_extraction ? `<div class="hint">Claim extraction ${esc(j.claims_extraction)}.</div>` : ''}<div class="hint">${esc(j.note)}</div></div>`;
}

async function send() {
  const sel = state.sources.filter((s) => s.selected);
  if (!sel.length) { msg('Select at least one source.', 'bad'); return; }
  if (!settings.tok) { msg('Paste the pairing token in Settings first.', 'bad'); return; }
  if (!$('#attest').checked) { msg('Tick the personal-research attestation first (unattested captures are never sent to a model).', 'bad'); return; }
  $('#send').disabled = true;
  try {
    const j = await call('/v1/research/captures', { method: 'POST', body: JSON.stringify(batchOf(sel)) });
    sel.forEach((s) => (s.sent = true)); await save(); render();
    $('#receipt').innerHTML = receiptHtml(j);
    msg('Sent. Open Personal CA → Research inbox.', 'good');
  } catch (e) { msg('Send failed: ' + e.message, 'bad'); }
  $('#send').disabled = false;
}

function exportJson() {
  const blob = new Blob([JSON.stringify(batchOf(state.sources.filter((s) => s.selected)), null, 1)], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob); a.download = `personal-ca-captures-${new Date().toISOString().slice(0, 19).replace(/:/g, '')}.json`;
  a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  msg('Exported. Import with: ca import-captures <file>', 'good');
}

/* --------------------------------------------------------- article queue */
async function refreshQueue() {
  if (!settings.tok) { $('#qcount').textContent = 'not paired'; return; }
  try {
    const q = await call('/v1/research/queue');
    $('#qcount').textContent = `${q.length} queued`;
    $('#qcount').className = 'pill ' + (q.length ? 'warn' : '');
    if (!queueRun.active) queueRun.items = q.map((x) => ({ ...x, st: 'waiting' }));
    renderQueue();
  } catch (e) { $('#qcount').textContent = 'offline'; }
}

function renderQueue() {
  const icon = { waiting: '○', running: '●', ok: '✓', fail: '✖', skipped: '–' };
  $('#qlist').innerHTML = queueRun.items.map((it) => `<div class="qi ${it.st === 'ok' ? 'ok' : it.st === 'fail' ? 'fail' : it.st === 'running' ? 'run' : ''}"><span class="st">${icon[it.st] || '○'}</span>
    <span>${esc(it.canonical_url.replace(ET_PREFIX, 'ET/').slice(0, 90))}${it.symbol ? ` <span class="pill">${esc(it.symbol)}</span>` : ''}${it.info ? `<br><span class="muted">${esc(it.info)}</span>` : ''}</span></div>`).join('');
}

function waitForLoad(tabId) {
  return new Promise((resolve, reject) => {
    const t = setTimeout(() => { chrome.tabs.onUpdated.removeListener(fn); reject(new Error('page load timed out')); }, PAGE_TIMEOUT_MS);
    function fn(id, info) {
      if (id === tabId && info.status === 'complete') { clearTimeout(t); chrome.tabs.onUpdated.removeListener(fn); resolve(); }
    }
    chrome.tabs.onUpdated.addListener(fn);
  });
}

const CHALLENGE = /(sign in|log ?in|verify you are human|captcha|access denied|subscribe to continue|unlock this story)/i;

async function fetchQueue() {
  // permissions.request must run inside the click gesture, before any await
  const perm = chrome.permissions.request({ origins: [ET_ORIGIN] });
  if (!settings.tok) { msg('Paste the pairing token in Settings first.', 'bad'); return; }
  if (!$('#attest').checked) { msg('Tick the personal-research attestation before fetching.', 'bad'); return; }
  if (!(await perm.catch(() => false))) { msg('ET site access is needed to read queued articles.', 'bad'); return; }
  await refreshQueue();
  const todo = queueRun.items.filter((x) => x.st === 'waiting');
  if (!todo.length) { msg('Nothing queued. Add links on the Personal CA Research inbox page or with "Add this tab".'); return; }
  queueRun.active = true; queueRun.cancel = false;
  $('#q-fetch').disabled = true; $('#q-cancel').hidden = false;
  let ok = 0, failed = 0;
  for (const it of todo) {
    if (queueRun.cancel) { it.st = 'skipped'; it.info = 'stopped by you'; continue; }
    it.st = 'running'; it.info = 'opening…'; renderQueue();
    await call(`/v1/research/queue/${it.queue_id}/status`, { method: 'POST', body: JSON.stringify({ status: 'in_progress' }) }).catch(() => {});
    let tab = null;
    try {
      tab = await chrome.tabs.create({ url: it.canonical_url, active: false });
      await waitForLoad(tab.id);
      it.info = 'rendering…'; renderQueue();
      await sleep(RENDER_WAIT_MS);
      const r = await extractFromTab(tab.id, 'page');
      if (!r || r.error) throw new Error((r && r.error) || 'capture failed');
      const chars = r.passages.reduce((a, p) => a + p.length, 0);
      if (CHALLENGE.test(r.title || '') || (chars < 600 && CHALLENGE.test(r.passages.join(' ')))) {
        it.st = 'fail'; it.info = 'login/challenge page — stopped (open it manually, sign in, then retry)';
        await call(`/v1/research/queue/${it.queue_id}/status`, { method: 'POST', body: JSON.stringify({ status: 'failed', error: 'access challenge or login page' }) }).catch(() => {});
        failed++; queueRun.cancel = true; continue;
      }
      if (chars < 600) r.access_warnings.push('very_short_capture — likely preview or partial; verify');
      const src = addSource(r, { queue_id: it.queue_id });
      const j = await call('/v1/research/captures', { method: 'POST', body: JSON.stringify(batchOf([src])) });
      const res = j.items[0] || {};
      src.sent = true;
      it.st = res.status === 'rejected' ? 'fail' : 'ok';
      it.info = `${res.status}${res.reason ? ' — ' + res.reason : ''} · ${r.passages.length} passages${chars < 600 ? ' · ⚠ short' : ''}`;
      res.status === 'rejected' ? failed++ : ok++;
    } catch (e) {
      it.st = 'fail'; it.info = e.message; failed++;
      await call(`/v1/research/queue/${it.queue_id}/status`, { method: 'POST', body: JSON.stringify({ status: 'failed', error: e.message }) }).catch(() => {});
    } finally {
      if (tab) chrome.tabs.remove(tab.id).catch(() => {});
      await save(); render(); renderQueue();
    }
    if (!queueRun.cancel) await sleep(BETWEEN_PAGES_MS);
  }
  queueRun.active = false;
  $('#q-fetch').disabled = false; $('#q-cancel').hidden = true;
  msg(`Queue done: ${ok} captured, ${failed} failed. Open Personal CA → Research inbox.`, failed ? 'bad' : 'good');
  setTimeout(refreshQueue, 1500);
}

async function addTabToQueue() {
  const tab = await activeTab();
  if (!tab || !(tab.url || '').startsWith(ET_PREFIX)) { msg('Open an economictimes.indiatimes.com article first (or grant ET access so its URL is visible).', 'bad'); return; }
  try {
    const r = await call('/v1/research/queue', { method: 'POST', body: JSON.stringify({ urls: [tab.url], symbol: $('#q-sym').value.trim() || null }) });
    msg(r.added.length ? 'Added to queue.' : 'Not added: ' + (r.rejected[0] || {}).reason, r.added.length ? 'good' : 'bad');
    refreshQueue();
  } catch (e) { msg(e.message, 'bad'); }
}

async function scanPage() {
  const tab = await activeTab();
  if (!tab) return;
  let links = [];
  try {
    const [res] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: () => {
      const seen = new Set(), out = [];
      document.querySelectorAll('a[href*="/articleshow/"]').forEach((a) => {
        const h = a.href.split('#')[0].split('?')[0];
        const t = (a.innerText || a.title || '').trim().replace(/\s+/g, ' ');
        if (!h.startsWith('https://economictimes.indiatimes.com/') || seen.has(h) || t.length < 15) return;
        seen.add(h); out.push({ url: h, title: t.slice(0, 160) });
      });
      return out.slice(0, 40);
    } });
    links = (res && res.result) || [];
  } catch (e) { msg('Cannot read this page. Click "Allow ET access" first.', 'bad'); return; }
  if (!links.length) { msg('No ET article links found on this page.', 'bad'); return; }
  $('#scan').innerHTML = `<div class="hint">${links.length} article link(s). Tick the ones you want, then queue them.</div>
    ${links.map((l, i) => `<label class="scan-item"><input type="checkbox" data-scan="${i}"><span>${esc(l.title)}</span></label>`).join('')}
    <button id="scan-q" class="btn sm primary">Queue selected</button>`;
  $('#scan-q').onclick = async () => {
    const urls = [...document.querySelectorAll('[data-scan]:checked')].map((c) => links[+c.dataset.scan].url);
    if (!urls.length) return;
    try {
      const r = await call('/v1/research/queue', { method: 'POST', body: JSON.stringify({ urls, symbol: $('#q-sym').value.trim() || null }) });
      msg(`${r.added.length} queued${r.rejected.length ? `, ${r.rejected.length} skipped` : ''}.`, 'good');
      $('#scan').innerHTML = ''; refreshQueue();
    } catch (e) { msg(e.message, 'bad'); }
  };
}

/* -------------------------------------------------------------- wiring */
$('#allow-et').onclick = async () => {
  const ok = await chrome.permissions.request({ origins: [ET_ORIGIN] }).catch(() => false);
  msg(ok ? 'ET access granted (economictimes.indiatimes.com only). Revoke any time in Chrome → Extensions → Site access.' : 'ET site access was not granted.', ok ? 'good' : 'bad');
};
$('#attest').onchange = save;
$('#cap-page').onclick = () => capture('page');
$('#cap-sel').onclick = () => capture('selection');
$('#search').oninput = render;
$('#clear').onclick = async () => { state.sources = []; await save(); render(); msg('Session cleared. (Earlier sends/exports are not retracted.)'); };
$('#list').onclick = async (e) => { const rm = e.target.dataset.rm; if (rm !== undefined) { state.sources.splice(+rm, 1); await save(); render(); } };
$('#list').onchange = async (e) => { const i = e.target.dataset.sel; if (i !== undefined) { state.sources[+i].selected = e.target.checked; await save(); } };
$('#send').onclick = send;
$('#export').onclick = exportJson;
$('#q-fetch').onclick = fetchQueue;
$('#q-cancel').onclick = () => { queueRun.cancel = true; msg('Stopping after the current article…'); };
$('#q-refresh').onclick = refreshQueue;
$('#q-addtab').onclick = addTabToQueue;
$('#q-scan').onclick = scanPage;
$('#save').onclick = async () => {
  settings.svc = $('#svc').value.trim(); settings.tok = $('#tok').value.trim();
  if (!/^http:\/\/(127\.0\.0\.1|localhost)(:\d+)?\/?$/.test(settings.svc)) { msg('Service URL must be a loopback address (127.0.0.1 or localhost).', 'bad'); return; }
  await chrome.storage.local.set({ svc: settings.svc, tok: settings.tok }); await checkConn(); refreshQueue(); msg('Saved.', 'good');
};
load();
