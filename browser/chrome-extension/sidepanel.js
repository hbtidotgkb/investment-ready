/* global personalCaExtract */
'use strict';

const VERSION = chrome.runtime.getManifest().version;
const ET_ORIGIN = 'https://economictimes.indiatimes.com/*';
const ET_PREFIX = 'https://economictimes.indiatimes.com/';
const MAX_SOURCES = 20;
const MAX_CHARS = 80000;
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);

let state = { sources: [], sessionId: null };
let settings = { svc: 'http://127.0.0.1:8765', tok: '' };

function msg(text, kind = '') { const m = $('#msg'); m.textContent = text; m.className = 'msg ' + kind; m.hidden = !text; }
const svc = (p) => settings.svc.replace(/\/$/, '') + p;

async function call(path, opts = {}) {
  const r = await fetch(svc(path), { ...opts, headers: { 'content-type': 'application/json', 'x-ca-token': settings.tok,
    'x-ca-ext-version': VERSION, 'x-ca-ext-id': chrome.runtime.id, ...(opts.headers || {}) } });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || j.detail || `HTTP ${r.status}`);
  return j;
}

async function load() {
  const s = await chrome.storage.session.get(['sources', 'sessionId', 'queueRun']);   // cleared on browser restart
  state.sources = s.sources || [];
  state.sessionId = s.sessionId || ('rs-' + crypto.randomUUID());
  const l = await chrome.storage.local.get(['svc', 'tok', 'attest', 'autoFetch']);
  settings.svc = l.svc || settings.svc; settings.tok = l.tok || '';
  $('#svc').value = settings.svc; $('#tok').value = settings.tok;
  $('#attest').checked = !!l.attest; $('#auto').checked = l.autoFetch !== false && !!l.tok;
  if (l.autoFetch === undefined && l.tok) await chrome.storage.local.set({ autoFetch: true });
  await saveSession(); render(); renderRun(s.queueRun); checkConn(); refreshQueue();
  if (!settings.tok) { msg('First step: paste the pairing token from Personal CA → System into Settings (bottom of this panel).', 'bad'); document.querySelector('.settings').open = true; }
}
const saveSession = () => chrome.storage.session.set({ sources: state.sources, sessionId: state.sessionId });

async function checkConn() {
  const c = $('#conn');
  try {
    const j = await (await fetch(svc('/v1/health'))).json();
    c.textContent = settings.tok ? `connected · ext v${VERSION} · platform v${j.version}` : 'connected · not paired';
    c.className = 'pill ' + (settings.tok ? 'good' : 'warn');
  } catch (e) { c.textContent = 'service offline — run ca serve'; c.className = 'pill bad'; }
}

async function activeTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab;
}

/* ------------------------------------------------------- manual capture */
async function capture(mode) {
  msg('');
  const tab = await activeTab();
  if (!tab) { msg('No active tab.', 'bad'); return; }
  let r;
  try {
    const [res] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: personalCaExtract, args: [mode, MAX_CHARS] });
    r = res && res.result;
  } catch (e) {
    msg('No access to this page. On ET click "Allow ET access" first. ' + (e.message || ''), 'bad');
    return;
  }
  if (!r || r.error) { msg((r && r.error) || 'Capture failed.', 'bad'); return; }
  if (!r.passages.length) { msg('No readable text found. Try selecting the article text and "Capture selection".', 'bad'); return; }
  const item = { ...r, capture_id: crypto.randomUUID(), captured_at: new Date().toISOString(), selected: true };
  if (state.sources.length >= MAX_SOURCES) state.sources.shift();
  state.sources.push(item); await saveSession(); render();
  const chars = r.passages.reduce((a, p) => a + p.length, 0);
  msg(`Captured ${r.passages.length} passages (${chars.toLocaleString()} chars). Check the end and any tables, then Send.`, 'good');
}

function render() {
  $('#count').textContent = `${state.sources.length}/${MAX_SOURCES}`;
  const q = $('#search').value.trim().toLowerCase();
  $('#list').innerHTML = state.sources.map((s, i) => {
    const ps = s.passages.map((t, j) => ({ t, id: 'P' + (j + 1) })).filter((p) => !q || p.t.toLowerCase().includes(q));
    const hl = (t) => { if (!q) return esc(t); const k = t.toLowerCase().indexOf(q); return k < 0 ? esc(t) : esc(t.slice(0, k)) + '<mark>' + esc(t.slice(k, k + q.length)) + '</mark>' + esc(t.slice(k + q.length)); };
    return `<div class="src"><div class="row"><input type="checkbox" data-sel="${i}" ${s.selected ? 'checked' : ''}><span class="t">${esc(s.title || '(untitled)')}</span><span class="spacer"></span>${s.sent ? '<span class="pill good">sent</span>' : ''}<button class="btn sm" data-rm="${i}">Remove</button></div>
      <div class="u">${esc(s.url)}</div><div class="u">${s.passages.length} passages · ${esc(s.extraction_method)} · published ${esc(s.published_at || 'unknown')}</div>
      ${s.access_warnings.filter((w) => w !== 'capture_unverified').map((w) => `<div class="w">⚠ ${esc(w)}</div>`).join('')}
      <details ${q ? 'open' : ''}><summary>${q ? `${ps.length} matching passage(s)` : 'Inspect passages'}</summary>${ps.slice(0, 200).map((p) => `<div class="p"><b>${p.id}</b> ${hl(p.t)}</div>`).join('')}</details></div>`;
  }).join('') || '<p class="hint">No manual captures in this browser session.</p>';
}

async function send() {
  const sel = state.sources.filter((s) => s.selected);
  if (!sel.length) { msg('Select at least one source.', 'bad'); return; }
  if (!settings.tok) { msg('Paste the pairing token in Settings first.', 'bad'); return; }
  if (!$('#attest').checked) { msg('Tick the personal-research attestation first.', 'bad'); return; }
  $('#send').disabled = true;
  try {
    const body = { schema_version: 'capture@1', research_session_id: state.sessionId, extension_version: VERSION, items: sel.map((s) => ({
      capture_id: s.capture_id, url: s.url, title: (s.title || '').slice(0, 500), author: s.author ? s.author.slice(0, 300) : null,
      published_at: s.published_at ? s.published_at.slice(0, 60) : null, captured_at: s.captured_at, extraction_method: s.extraction_method,
      passages: s.passages.map((t, j) => ({ id: 'P' + (j + 1), text: t.slice(0, 20000) })), access_warnings: s.access_warnings.slice(0, 20),
      truncated: !!s.truncated, related_links: (s.related_links || []).slice(0, 50), usage_attested: true })) };
    const j = await call('/v1/research/captures', { method: 'POST', body: JSON.stringify(body) });
    sel.forEach((s) => (s.sent = true)); await saveSession(); render();
    $('#receipt').innerHTML = `<div class="rc"><b>Receipt ${esc(j.receipt_id)}</b>${j.items.map((it) => `<div>${it.status === 'accepted' ? '✅' : it.status === 'duplicate' ? '↺' : '✖'} ${esc(it.status)} ${it.reason ? '— ' + esc(it.reason) : ''}${it.companies && it.companies.resolved.length ? ' · ' + it.companies.resolved.map((c) => esc(c.symbol)).join(', ') : ''}</div>`).join('')}</div>`;
    msg('Sent. See Personal CA → Research inbox.', 'good');
  } catch (e) { msg('Send failed: ' + e.message, 'bad'); }
  $('#send').disabled = false;
}

function exportJson() {
  const blob = new Blob([JSON.stringify({ schema_version: 'capture@1', research_session_id: state.sessionId, extension_version: VERSION,
    items: state.sources.filter((s) => s.selected) }, null, 1)], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob); a.download = `personal-ca-captures-${Date.now()}.json`;
  a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}

/* ------------------------------------------------- queue (background) */
async function refreshQueue() {
  if (!settings.tok) { $('#qcount').textContent = 'not paired'; return; }
  try {
    const q = await call('/v1/research/queue');
    $('#qcount').textContent = `${q.length} waiting`;
    $('#qcount').className = 'pill ' + (q.length ? 'warn' : 'good');
  } catch (e) { $('#qcount').textContent = 'offline'; }
}

function renderRun(run) {
  if (!run) { $('#qrun').innerHTML = ''; $('#qlist').innerHTML = ''; return; }
  const icon = { running: '●', ok: '✓', fail: '✖' };
  $('#qrun').innerHTML = `<b>${esc(run.status || '')}</b> ${esc(run.message || '')}`;
  $('#qlist').innerHTML = (run.items || []).slice(-25).map((it) => `<div class="qi ${it.st === 'ok' ? 'ok' : it.st === 'fail' ? 'fail' : 'run'}"><span class="st">${icon[it.st] || '○'}</span>
    <span>${esc((it.title || it.url).replace(ET_PREFIX, 'ET/').slice(0, 90))}${it.symbol ? ` <span class="pill">${esc(it.symbol)}</span>` : ''}${it.kind === 'scan' ? ' <span class="pill">scan</span>' : ''}<br><span class="muted">${esc(it.info || '')}</span></span></div>`).join('');
}
chrome.storage.onChanged.addListener((ch, area) => { if (area === 'session' && ch.queueRun) { renderRun(ch.queueRun.newValue); refreshQueue(); } });

async function fetchNow() {
  // permissions.request must run inside the click gesture
  const ok = await chrome.permissions.request({ origins: [ET_ORIGIN] }).catch(() => false);
  if (!ok) { msg('ET site access is needed to read queued articles.', 'bad'); return; }
  if (!$('#attest').checked) { msg('Tick the personal-research attestation first.', 'bad'); return; }
  msg('Fetching in the background — you can close this panel.', 'good');
  chrome.runtime.sendMessage({ type: 'fetchNow' }).then((r) => { if (r && r.error) msg(r.error, 'bad'); });
}

async function addTabToQueue() {
  const tab = await activeTab();
  if (!tab || !(tab.url || '').startsWith(ET_PREFIX)) { msg('Open an economictimes.indiatimes.com article first (and allow ET access).', 'bad'); return; }
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
        const h = a.href.split('#')[0].split('?')[0]; const t = (a.innerText || a.title || '').trim().replace(/\s+/g, ' ');
        if (!h.startsWith('https://economictimes.indiatimes.com/') || seen.has(h) || t.length < 15) return;
        seen.add(h); out.push({ url: h, title: t.slice(0, 160) });
      });
      return out.slice(0, 40);
    } });
    links = (res && res.result) || [];
  } catch (e) { msg('Cannot read this page. Click "Allow ET access" first.', 'bad'); return; }
  if (!links.length) { msg('No ET article links found on this page.', 'bad'); return; }
  $('#scan').innerHTML = `<div class="hint">${links.length} article link(s) — tick and queue.</div>
    ${links.map((l, i) => `<label class="scan-item"><input type="checkbox" data-scan="${i}"><span>${esc(l.title)}</span></label>`).join('')}
    <button id="scan-q" class="btn sm primary">Queue selected</button>`;
  $('#scan-q').onclick = async () => {
    const urls = [...document.querySelectorAll('[data-scan]:checked')].map((c) => links[+c.dataset.scan].url);
    if (!urls.length) return;
    try { const r = await call('/v1/research/queue', { method: 'POST', body: JSON.stringify({ urls, symbol: $('#q-sym').value.trim() || null }) }); msg(`${r.added.length} queued.`, 'good'); $('#scan').innerHTML = ''; refreshQueue(); }
    catch (e) { msg(e.message, 'bad'); }
  };
}

/* -------------------------------------------------------------- wiring */
$('#allow-et').onclick = async () => {
  const ok = await chrome.permissions.request({ origins: [ET_ORIGIN] }).catch(() => false);
  msg(ok ? 'ET access granted (economictimes.indiatimes.com only).' : 'ET site access was not granted.', ok ? 'good' : 'bad');
};
$('#attest').onchange = () => chrome.storage.local.set({ attest: $('#attest').checked });
$('#visit').onchange = async () => {
  if ($('#visit').checked) {
    const ok = await chrome.permissions.request({ origins: [ET_ORIGIN] }).catch(() => false);
    if (!ok || !$('#attest').checked) { $('#visit').checked = false; msg(!ok ? 'ET access is needed for this.' : 'Tick the personal-research attestation first.', 'bad'); return; }
  }
  await chrome.storage.local.set({ autoCaptureOnVisit: $('#visit').checked });
  msg($('#visit').checked ? 'On: ET articles you open are captured after they finish loading.' : 'Off.', 'good');
};
function renderVisitLog(log) {
  $('#visitlog').innerHTML = (log || []).slice(-5).reverse().map((l) => `${l.status === 'accepted' ? '✓' : l.status === 'duplicate' ? '↺' : '✖'} ${esc((l.title || '').slice(0, 60))}${l.companies.length ? ' · ' + esc(l.companies.join(', ')) : ' · no stock matched'}`).join('<br>');
}
chrome.storage.onChanged.addListener((ch, area) => { if (area === 'session' && ch.visitLog) renderVisitLog(ch.visitLog.newValue); });
chrome.storage.local.get('autoCaptureOnVisit').then((v) => { $('#visit').checked = !!v.autoCaptureOnVisit; });
chrome.storage.session.get('visitLog').then((v) => renderVisitLog(v.visitLog));
$('#auto').onchange = () => { chrome.storage.local.set({ autoFetch: $('#auto').checked }); msg($('#auto').checked ? 'Auto-fetch on: new queued articles are captured within a minute.' : 'Auto-fetch off.', 'good'); };
$('#cap-page').onclick = () => capture('page');
$('#cap-sel').onclick = () => capture('selection');
$('#search').oninput = render;
$('#clear').onclick = async () => { state.sources = []; await saveSession(); render(); };
$('#list').onclick = async (e) => { const rm = e.target.dataset.rm; if (rm !== undefined) { state.sources.splice(+rm, 1); await saveSession(); render(); } };
$('#list').onchange = async (e) => { const i = e.target.dataset.sel; if (i !== undefined) { state.sources[+i].selected = e.target.checked; await saveSession(); } };
$('#send').onclick = send;
$('#export').onclick = exportJson;
$('#q-fetch').onclick = fetchNow;
$('#q-cancel').onclick = () => chrome.runtime.sendMessage({ type: 'stop' });
$('#q-refresh').onclick = () => chrome.runtime.sendMessage({ type: 'poll' }).then(refreshQueue);
$('#q-addtab').onclick = addTabToQueue;
$('#q-scan').onclick = scanPage;
$('#save').onclick = async () => {
  settings.svc = $('#svc').value.trim(); settings.tok = $('#tok').value.trim();
  if (!/^http:\/\/(127\.0\.0\.1|localhost)(:\d+)?\/?$/.test(settings.svc)) { msg('Service URL must be 127.0.0.1 or localhost.', 'bad'); return; }
  await chrome.storage.local.set({ svc: settings.svc, tok: settings.tok });
  await checkConn(); await refreshQueue(); chrome.runtime.sendMessage({ type: 'poll' });
  msg('Saved and paired. Now tick the attestation and click "Allow ET access".', 'good');
};
load();
