/* global personalCaExtract */
'use strict';

const VERSION = chrome.runtime.getManifest().version;
const ET_ORIGIN = 'https://economictimes.indiatimes.com/*';
const MAX_SOURCES = 10;
const MAX_CHARS = 80000;
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);

let state = { sources: [], sessionId: null };
let settings = { svc: 'http://127.0.0.1:8765', tok: '' };

function msg(text, kind = '') { const m = $('#msg'); m.textContent = text; m.className = 'msg ' + kind; m.hidden = !text; }

async function load() {
  const s = await chrome.storage.session.get(['sources', 'sessionId']);   // cleared on browser restart
  state.sources = s.sources || [];
  state.sessionId = s.sessionId || ('rs-' + crypto.randomUUID());
  const l = await chrome.storage.local.get(['svc', 'tok']);
  settings.svc = l.svc || settings.svc; settings.tok = l.tok || '';
  $('#svc').value = settings.svc; $('#tok').value = settings.tok;
  await save(); render(); checkConn();
}
const save = () => chrome.storage.session.set({ sources: state.sources, sessionId: state.sessionId });

async function checkConn() {
  const c = $('#conn');
  try {
    const r = await fetch(settings.svc.replace(/\/$/, '') + '/v1/health');
    const j = await r.json();
    c.textContent = settings.tok ? `connected · v${j.version}` : 'connected · not paired';
    c.className = 'pill ' + (settings.tok ? 'good' : 'warn');
  } catch (e) { c.textContent = 'service offline'; c.className = 'pill bad'; }
}

async function activeTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab;
}

async function capture(mode) {
  msg('');
  if (state.sources.length >= MAX_SOURCES) { msg(`Session limit is ${MAX_SOURCES} sources. Send or remove some first.`, 'bad'); return; }
  const tab = await activeTab();
  if (!tab) { msg('No active tab.', 'bad'); return; }
  let res;
  try {
    [res] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: personalCaExtract, args: [mode, MAX_CHARS] });
  } catch (e) {
    msg('No access to this page. On ET use "Allow ET & capture". On other sites, click the toolbar icon on that tab first (temporary access), then capture. ' + (e.message || ''), 'bad');
    return;
  }
  const r = res && res.result;
  if (!r || r.error) { msg((r && r.error) || 'Capture failed.', 'bad'); return; }
  if (!r.passages.length) { msg('No readable text found. Try selecting the article text and "Capture selection".', 'bad'); return; }
  const item = { ...r, capture_id: crypto.randomUUID(), captured_at: new Date().toISOString(), selected: true };
  state.sources.push(item); await save(); render();
  const chars = r.passages.reduce((a, p) => a + p.length, 0);
  msg(`Captured ${r.passages.length} passages (${chars.toLocaleString()} chars). Check the last paragraphs and any tables against the page.`, 'good');
}

function render() {
  $('#count').textContent = `${state.sources.length}/${MAX_SOURCES}`;
  const q = $('#search').value.trim().toLowerCase();
  $('#list').innerHTML = state.sources.map((s, i) => {
    const ps = s.passages.map((t, j) => ({ t, id: 'P' + (j + 1) })).filter((p) => !q || p.t.toLowerCase().includes(q));
    const hl = (t) => { if (!q) return esc(t); const k = t.toLowerCase().indexOf(q); return k < 0 ? esc(t) : esc(t.slice(0, k)) + '<mark>' + esc(t.slice(k, k + q.length)) + '</mark>' + esc(t.slice(k + q.length)); };
    return `<div class="src"><div class="row"><input type="checkbox" data-sel="${i}" ${s.selected ? 'checked' : ''} title="Include when sending"><span class="t">${esc(s.title || '(untitled)')}</span><span class="spacer"></span><button class="btn sm" data-rm="${i}">Remove</button></div>
      <div class="u">${esc(s.url)}</div>
      <div class="u">${s.passages.length} passages · ${esc(s.extraction_method)} · published ${esc(s.published_at || 'unknown')}${s.author ? ' · ' + esc(s.author) : ''}</div>
      ${s.access_warnings.filter((w) => w !== 'capture_unverified').map((w) => `<div class="w">⚠ ${esc(w)}</div>`).join('')}
      <details ${q ? 'open' : ''}><summary>${q ? `${ps.length} matching passage(s)` : 'Inspect passages'}</summary>${ps.slice(0, 200).map((p) => `<div class="p"><b>${p.id}</b> ${hl(p.t)}</div>`).join('')}</details></div>`;
  }).join('') || '<p class="hint">No captures yet in this browser session.</p>';
}

function batch() {
  return {
    schema_version: 'capture@1', research_session_id: state.sessionId, extension_version: VERSION,
    items: state.sources.filter((s) => s.selected).map((s) => ({
      capture_id: s.capture_id, url: s.url, title: (s.title || '').slice(0, 500), author: s.author ? s.author.slice(0, 300) : null,
      published_at: s.published_at ? s.published_at.slice(0, 60) : null, captured_at: s.captured_at, extraction_method: s.extraction_method,
      passages: s.passages.map((t, j) => ({ id: 'P' + (j + 1), text: t.slice(0, 20000) })), access_warnings: s.access_warnings.slice(0, 20),
      truncated: !!s.truncated, related_links: (s.related_links || []).slice(0, 50), usage_attested: $('#attest').checked,
    })),
  };
}

async function send() {
  const b = batch();
  if (!b.items.length) { msg('Select at least one source.', 'bad'); return; }
  if (!settings.tok) { msg('Paste the pairing token in Settings first.', 'bad'); return; }
  if (!$('#attest').checked && !confirmNoAttest()) return;
  $('#send').disabled = true;
  try {
    const r = await fetch(settings.svc.replace(/\/$/, '') + '/v1/research/captures', {
      method: 'POST', headers: { 'content-type': 'application/json', 'x-ca-token': settings.tok }, body: JSON.stringify(b) });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || j.detail || `HTTP ${r.status}`);
    $('#receipt').innerHTML = `<div class="rc"><b>Receipt ${esc(j.receipt_id)}</b>${j.replayed ? ' (already received — idempotent)' : ''}
      ${j.items.map((it) => `<div>${it.status === 'accepted' ? '✅' : it.status === 'duplicate' ? '↺' : '✖'} ${esc(it.status)} ${it.source_id ? esc(it.source_id) + ' v' + it.version : ''} ${it.reason ? '— ' + esc(it.reason) : ''}
      ${it.companies && it.companies.resolved.length ? '· ' + it.companies.resolved.map((c) => esc(c.symbol)).join(', ') : ''}${it.companies && it.companies.ambiguous.length ? ' · ambiguous names left unresolved' : ''}</div>`).join('')}
      <div class="hint">${esc(j.note)}</div></div>`;
    msg('Sent. Open Personal CA → Research inbox.', 'good');
  } catch (e) { msg('Send failed: ' + e.message, 'bad'); }
  $('#send').disabled = false;
}

let _warned = false;
function confirmNoAttest() {
  if (_warned) return true;
  _warned = true;
  msg('Without the attestation the captures are stored but never sent to a model. Click Send again to continue anyway.', 'bad');
  return false;
}

function exportJson() {
  const blob = new Blob([JSON.stringify(batch(), null, 1)], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob); a.download = `personal-ca-captures-${new Date().toISOString().slice(0, 19).replace(/:/g, '')}.json`;
  a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  msg('Exported. Import with: ca import-captures <file>', 'good');
}

$('#allow-et').onclick = async () => {
  const ok = await chrome.permissions.request({ origins: [ET_ORIGIN] }).catch(() => false);
  if (!ok) { msg('ET site access was not granted. You can still capture via the toolbar icon (temporary access).', 'bad'); return; }
  const tab = await activeTab();
  if (!tab || !(tab.url || '').startsWith('https://economictimes.indiatimes.com/')) { msg('Access granted for economictimes.indiatimes.com. Open an ET article and click capture.', 'good'); return; }
  capture('page');
};
$('#cap-page').onclick = () => capture('page');
$('#cap-sel').onclick = () => capture('selection');
$('#search').oninput = render;
$('#clear').onclick = async () => { state.sources = []; await save(); render(); msg('Session cleared. (Earlier sends/exports are not retracted.)'); };
$('#list').onclick = async (e) => {
  const rm = e.target.dataset.rm;
  if (rm !== undefined) { state.sources.splice(+rm, 1); await save(); render(); }
};
$('#list').onchange = async (e) => {
  const i = e.target.dataset.sel;
  if (i !== undefined) { state.sources[+i].selected = e.target.checked; await save(); }
};
$('#send').onclick = send;
$('#export').onclick = exportJson;
$('#save').onclick = async () => {
  settings.svc = $('#svc').value.trim(); settings.tok = $('#tok').value.trim();
  if (!/^http:\/\/(127\.0\.0\.1|localhost)(:\d+)?\/?$/.test(settings.svc)) { msg('Service URL must be a loopback address (127.0.0.1 or localhost).', 'bad'); return; }
  await chrome.storage.local.set({ svc: settings.svc, tok: settings.tok }); await checkConn(); msg('Saved.', 'good');
};
load();
