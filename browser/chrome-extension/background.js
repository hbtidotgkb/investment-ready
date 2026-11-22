/* Personal CA — background worker.
 * - Opens the side panel on toolbar click.
 * - Heartbeat + queue poll every minute (tells the platform the extension is installed).
 * - Auto-fetch (opt-in): captures queued ET articles in background tabs of THIS signed-in Chrome,
 *   one at a time, then closes each tab. Stops at login/challenge pages. Never reads cookies.
 * - "Fetch now" can be triggered from the side panel or the local Personal CA page.
 */
/* global personalCaExtract */
importScripts('extractor.js');

const VERSION = chrome.runtime.getManifest().version;
const ET_ORIGIN = 'https://economictimes.indiatimes.com/*';
const MAX_CHARS = 80000;
const PAGE_TIMEOUT_MS = 45000;
const RENDER_WAIT_MS = 2500;
const BETWEEN_PAGES_MS = 3000;
const CHALLENGE = /(sign in|log ?in|verify you are human|captcha|access denied|subscribe to continue|unlock this story)/i;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let running = false;
let cancelRequested = false;

chrome.runtime.onInstalled.addListener(() => {
  chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => {});
  chrome.alarms.create('poll', { periodInMinutes: 1 });
});
chrome.runtime.onStartup.addListener(() => chrome.alarms.create('poll', { periodInMinutes: 1 }));
chrome.alarms.onAlarm.addListener((a) => { if (a.name === 'poll') poll(false); });

async function settings() {
  const l = await chrome.storage.local.get(['svc', 'tok', 'autoFetch', 'attest']);
  return { svc: (l.svc || 'http://127.0.0.1:8765').replace(/\/$/, ''), tok: l.tok || '', autoFetch: !!l.autoFetch, attest: !!l.attest };
}

async function call(s, path, opts = {}) {
  const r = await fetch(s.svc + path, { ...opts, headers: { 'content-type': 'application/json', 'x-ca-token': s.tok,
    'x-ca-ext-version': VERSION, 'x-ca-ext-id': chrome.runtime.id, ...(opts.headers || {}) } });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || j.detail || `HTTP ${r.status}`);
  return j;
}

async function setRun(update) {
  const cur = (await chrome.storage.session.get('queueRun')).queueRun || { items: [], log: [] };
  const next = { ...cur, ...update, updated: Date.now() };
  await chrome.storage.session.set({ queueRun: next });
  return next;
}

async function badge(text, color) {
  await chrome.action.setBadgeText({ text });
  if (color) await chrome.action.setBadgeBackgroundColor({ color });
}

/** Heartbeat + optional auto-fetch. */
async function poll(force) {
  const s = await settings();
  if (!s.tok) { await badge('!', '#c0302f'); return { error: 'not paired' }; }
  let q;
  try { q = await call(s, '/v1/research/queue'); } catch (e) { await badge('off', '#7a7974'); return { error: e.message }; }
  await badge(q.length ? String(q.length) : '', '#2a78d6');
  if (q.length && (force || s.autoFetch)) return fetchQueue(s, force);
  return { queued: q.length };
}

async function waitForLoad(tabId) {
  return new Promise((resolve, reject) => {
    const t = setTimeout(() => { chrome.tabs.onUpdated.removeListener(fn); reject(new Error('page load timed out')); }, PAGE_TIMEOUT_MS);
    function fn(id, info) { if (id === tabId && info.status === 'complete') { clearTimeout(t); chrome.tabs.onUpdated.removeListener(fn); resolve(); } }
    chrome.tabs.onUpdated.addListener(fn);
  });
}

async function collectLinks(tabId, limit) {
  const [res] = await chrome.scripting.executeScript({ target: { tabId }, args: [limit], func: (max) => {
    const seen = new Set(), out = [];
    document.querySelectorAll('a[href*="/articleshow/"]').forEach((a) => {
      const h = a.href.split('#')[0].split('?')[0];
      const t = (a.innerText || a.title || '').trim().replace(/\s+/g, ' ');
      if (!h.startsWith('https://economictimes.indiatimes.com/') || seen.has(h) || t.length < 15) return;
      seen.add(h); out.push({ url: h, title: t.slice(0, 160), prime: h.includes('/prime/') });
    });
    out.sort((a, b) => Number(b.prime) - Number(a.prime));
    return out.slice(0, max);
  } });
  return (res && res.result) || [];
}

function payload(r, it, s, sessionId) {
  return { schema_version: 'capture@1', research_session_id: sessionId, extension_version: VERSION, items: [{
    capture_id: crypto.randomUUID(), url: r.url, title: (r.title || '').slice(0, 500), author: r.author ? r.author.slice(0, 300) : null,
    published_at: r.published_at ? r.published_at.slice(0, 60) : null, captured_at: new Date().toISOString(), extraction_method: r.extraction_method,
    passages: r.passages.map((t, j) => ({ id: 'P' + (j + 1), text: t.slice(0, 20000) })), access_warnings: r.access_warnings.slice(0, 20),
    truncated: !!r.truncated, related_links: (r.related_links || []).slice(0, 50), usage_attested: s.attest, queue_id: it.queue_id }] };
}

async function fetchQueue(s, manual) {
  if (running) return { busy: true };
  if (!s.attest) { await setRun({ status: 'blocked', message: 'Tick the personal-research attestation in the side panel first.' }); return { error: 'not attested' }; }
  if (!(await chrome.permissions.contains({ origins: [ET_ORIGIN] }))) {
    await setRun({ status: 'blocked', message: 'ET site access not granted — open the side panel and click "Allow ET access".' });
    await badge('!', '#c0302f');
    return { error: 'no ET permission' };
  }
  running = true; cancelRequested = false;
  const sessionId = 'rs-bg-' + new Date().toISOString().slice(0, 10);
  const cfg = await call(s, '/v1/research/settings').catch(() => ({ scan_max_links: 15 }));
  let done = 0, failed = 0;
  const items = [];
  await setRun({ status: 'running', message: manual ? 'Fetching (manual)…' : 'Auto-fetching queued articles…', items, started: Date.now() });
  try {
    for (let round = 0; round < 6 && !cancelRequested; round++) {
      const q = await call(s, '/v1/research/queue');
      if (!q.length) break;
      for (const it of q) {
        if (cancelRequested) break;
        const row = { queue_id: it.queue_id, url: it.canonical_url, kind: it.kind, symbol: it.symbol, st: 'running', info: 'opening…' };
        items.push(row); await setRun({ items });
        await call(s, `/v1/research/queue/${it.queue_id}/status`, { method: 'POST', body: JSON.stringify({ status: 'in_progress' }) }).catch(() => {});
        let tab = null;
        try {
          tab = await chrome.tabs.create({ url: it.canonical_url, active: false });
          await waitForLoad(tab.id);
          await sleep(RENDER_WAIT_MS);
          if (it.kind === 'scan') {
            const links = await collectLinks(tab.id, cfg.scan_max_links || 15);
            const r2 = links.length ? await call(s, '/v1/research/queue', { method: 'POST', body: JSON.stringify({ urls: links.map((l) => l.url), symbol: it.symbol, note: 'from topic scan' }) }) : { added: [] };
            await call(s, `/v1/research/queue/${it.queue_id}/status`, { method: 'POST', body: JSON.stringify({ status: 'done' }) }).catch(() => {});
            Object.assign(row, { st: 'ok', info: `topic scan: ${links.length} links, ${r2.added.length} new queued` });
            continue;
          }
          const [res] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: personalCaExtract, args: ['page', MAX_CHARS] });
          const r = res && res.result;
          if (!r || r.error) throw new Error((r && r.error) || 'capture failed');
          const chars = r.passages.reduce((a, p) => a + p.length, 0);
          if (CHALLENGE.test(r.title || '') || (chars < 600 && CHALLENGE.test(r.passages.join(' ')))) {
            await call(s, `/v1/research/queue/${it.queue_id}/status`, { method: 'POST', body: JSON.stringify({ status: 'failed', error: 'login or challenge page — open it once, sign in, then re-queue' }) }).catch(() => {});
            Object.assign(row, { st: 'fail', info: 'login/challenge page — stopped' });
            failed++; cancelRequested = true; break;
          }
          if (chars < 600) r.access_warnings.push('very_short_capture — likely preview or partial; verify');
          const rec = await call(s, '/v1/research/captures', { method: 'POST', body: JSON.stringify(payload(r, it, s, sessionId)) });
          const one = rec.items[0] || {};
          Object.assign(row, { st: one.status === 'rejected' ? 'fail' : 'ok', info: `${one.status}${one.reason ? ' — ' + one.reason : ''} · ${r.passages.length} passages`, title: r.title });
          one.status === 'rejected' ? failed++ : done++;
        } catch (e) {
          Object.assign(row, { st: 'fail', info: e.message });
          failed++;
          await call(s, `/v1/research/queue/${it.queue_id}/status`, { method: 'POST', body: JSON.stringify({ status: 'failed', error: e.message }) }).catch(() => {});
        } finally {
          if (tab) chrome.tabs.remove(tab.id).catch(() => {});
          await setRun({ items });
        }
        await sleep(BETWEEN_PAGES_MS);
      }
    }
  } finally {
    running = false;
    await setRun({ status: 'idle', message: `Last run: ${done} captured, ${failed} failed${cancelRequested ? ' (stopped)' : ''}.`, finished: Date.now() });
    await badge(failed ? '!' : '', failed ? '#c0302f' : '#2a78d6');
  }
  return { done, failed };
}

/* ---------- opt-in: capture ET articles you open (content script registered only when enabled) ---------- */
async function syncAutoCapture() {
  const { autoCaptureOnVisit } = await chrome.storage.local.get('autoCaptureOnVisit');
  const allowed = await chrome.permissions.contains({ origins: [ET_ORIGIN] });
  const existing = await chrome.scripting.getRegisteredContentScripts({ ids: ['ca-autocapture'] }).catch(() => []);
  if (autoCaptureOnVisit && allowed && !existing.length) {
    await chrome.scripting.registerContentScripts([{ id: 'ca-autocapture', matches: ['https://economictimes.indiatimes.com/*'],
      js: ['extractor.js', 'autocapture.js'], runAt: 'document_idle', persistAcrossSessions: true }]).catch(() => {});
  } else if ((!autoCaptureOnVisit || !allowed) && existing.length) {
    await chrome.scripting.unregisterContentScripts({ ids: ['ca-autocapture'] }).catch(() => {});
  }
}
chrome.storage.onChanged.addListener((ch, area) => { if (area === 'local' && ch.autoCaptureOnVisit) syncAutoCapture(); });
chrome.permissions.onAdded.addListener(syncAutoCapture);
chrome.permissions.onRemoved.addListener(syncAutoCapture);
chrome.runtime.onStartup.addListener(syncAutoCapture);
chrome.runtime.onInstalled.addListener(syncAutoCapture);

async function autoCapture(r) {
  const s = await settings();
  if (!s.tok || !s.attest) { await badge('!', '#c0302f'); return; }
  const { autoSent = [] } = await chrome.storage.session.get('autoSent');
  if (autoSent.includes(r.url)) return;
  try {
    const rec = await call(s, '/v1/research/captures', { method: 'POST', body: JSON.stringify(payload(r, { queue_id: null }, s, 'rs-visit-' + new Date().toISOString().slice(0, 10))) });
    const one = rec.items[0] || {};
    await chrome.storage.session.set({ autoSent: [...autoSent.slice(-200), r.url] });
    const log = (await chrome.storage.session.get('visitLog')).visitLog || [];
    log.push({ t: Date.now(), title: r.title, status: one.status, companies: (one.companies && one.companies.resolved || []).map((c) => c.symbol) });
    await chrome.storage.session.set({ visitLog: log.slice(-25) });
    await badge('✓', '#0a7d3c');
    setTimeout(() => badge('', '#2a78d6'), 4000);
  } catch (e) { await badge('!', '#c0302f'); }
}

chrome.runtime.onMessage.addListener((m, _sender, reply) => {
  if (m && m.type === 'autoCapture') { autoCapture(m.result); return false; }
  if (m && m.type === 'autoCaptureShort') { badge('?', '#9a6400'); return false; }
  if (m && m.type === 'fetchNow') { settings().then((s) => fetchQueue(s, true)).then(reply); return true; }
  if (m && m.type === 'stop') { cancelRequested = true; reply({ ok: true }); }
  if (m && m.type === 'poll') { poll(false).then(reply); return true; }
  return false;
});

// The local Personal CA page (127.0.0.1 / localhost only) may ask for an immediate fetch.
chrome.runtime.onMessageExternal.addListener((m, sender, reply) => {
  const origin = sender.origin || (sender.url ? new URL(sender.url).origin : '');
  if (!/^http:\/\/(127\.0\.0\.1|localhost)(:\d+)?$/.test(origin)) { reply({ error: 'origin not allowed' }); return false; }
  if (m && m.type === 'fetchNow') { settings().then((s) => fetchQueue(s, true)).then(reply); return true; }
  if (m && m.type === 'ping') { reply({ ok: true, version: VERSION, running }); return false; }
  return false;
});
