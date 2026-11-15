// Injected into the active tab ONLY on an explicit user click. Reads already-rendered,
// visible text. It does not read cookies, hidden app state, or bypass paywalls/challenges.
// Exposed as a function the side panel passes to chrome.scripting.executeScript.

// eslint-disable-next-line no-unused-vars
function personalCaExtract(mode, maxChars) {
  const clean = (s) => (s || '').replace(/ /g, ' ').replace(/[ \t]+/g, ' ').replace(/\s*\n\s*/g, '\n').trim();
  const meta = (sel) => { const el = document.querySelector(sel); return el ? (el.getAttribute('content') || el.getAttribute('datetime') || el.textContent || '').trim() : null; };
  const visible = (el) => { if (!el || !el.getClientRects().length) return false; const cs = getComputedStyle(el); return cs.visibility !== 'hidden' && cs.display !== 'none' && parseFloat(cs.opacity || '1') > 0.05; };

  const out = {
    url: (document.querySelector('link[rel="canonical"]') || {}).href || location.href,
    title: meta('meta[property="og:title"]') || (document.querySelector('h1') || {}).innerText || document.title,
    author: meta('meta[name="author"]') || meta('[rel="author"]') || meta('.author, .byline, .artByline'),
    published_at: meta('meta[property="article:published_time"]') || meta('meta[itemprop="datePublished"]') || meta('time[datetime]'),
    extraction_method: mode, passages: [], access_warnings: ['capture_unverified'], truncated: false, related_links: [],
  };

  if (mode === 'selection') {
    const sel = clean(String(window.getSelection() || ''));
    if (!sel) return { error: 'Nothing selected. Select the article text first.' };
    sel.split(/\n{1,}/).map(clean).filter((t) => t.length > 1).forEach((t) => out.passages.push(t));
    out.access_warnings.push('selection_only — completeness depends on what you selected');
  } else {
    const candidates = ['.artText', '.artData', '.article_wrap', '.primeContent', '.prime_article', '[itemprop="articleBody"]', 'article', 'main'];
    let root = null, used = null;
    for (const c of candidates) {
      const els = [...document.querySelectorAll(c)].filter(visible);
      const best = els.sort((a, b) => b.innerText.length - a.innerText.length)[0];
      if (best && best.innerText.trim().length > 400) { root = best; used = c; break; }
    }
    if (!root) { root = document.body; used = 'body'; }
    if (candidates.indexOf(used) > 5 || used === 'body') out.access_warnings.push(`fallback_selector:${used} — may include unrelated page content`);
    // ET article bodies often use bare text + <br>; the line-split fallback below covers that.
    const blocks = root.querySelectorAll('p, h2, h3, h4, li, blockquote, table');
    const seen = new Set();
    blocks.forEach((b) => {
      if (!visible(b)) return;
      if (b.closest('nav, footer, aside, .ad, .ads, [class*="related"], [class*="newsletter"]')) return;
      let t;
      if (b.tagName === 'TABLE') {
        t = [...b.querySelectorAll('tr')].map((tr) => [...tr.querySelectorAll('th,td')].map((c) => clean(c.innerText)).join(' | ')).join('\n');
        if (t) t = '[table]\n' + t;
      } else {
        if (b.closest('table')) return;
        t = clean(b.innerText);
      }
      if (!t || t.length < 2 || seen.has(t)) return;
      seen.add(t); out.passages.push(t);
    });
    // Tables first: real <table>s and widget grids (role=table/grid, or class names containing "table"),
    // anywhere in the content column. Rows keep their cells together ("Brigade | 6 | Strong Buy | 43%").
    const tableTexts = [];
    const structure = [];
    const tableLike = [...document.querySelectorAll('table, [role="table"], [role="grid"], [class*="table" i], [class*="Table"]')]
      .filter((el) => visible(el) && !el.closest('nav, footer, aside, header'))
      .filter((el, _, all) => !all.some((o) => o !== el && o.contains(el) && o.matches('table, [role="table"], [role="grid"]')));
    tableLike.forEach((el) => {
      let rows = [...el.querySelectorAll('tr, [role="row"]')]
        .map((r) => [...r.querySelectorAll('th, td, [role="cell"], [role="gridcell"], [role="columnheader"], [role="rowheader"]')]
          .map((c) => clean(c.innerText)).filter(Boolean).join(' | '))
        .filter((r) => r.replace(/[|\s]/g, ''));
      if (rows.length < 2) {
        // div grid without roles: descend through single-child wrappers, then children = rows,
        // grandchildren = cells (cells are often inline spans whose innerText has no separators)
        let box = el;
        for (let d = 0; d < 6; d++) {
          const vis = [...box.children].filter(visible);
          if (vis.length !== 1) break;
          box = vis[0];
        }
        const kids = [...box.children].filter(visible);
        const withCells = kids.filter((k) => k.children.length >= 2);
        if (kids.length >= 3 && withCells.length >= kids.length * 0.8) {
          rows = withCells.map((k) => [...k.children].map((c) => clean(c.innerText)).filter(Boolean).join(' | ')).filter(Boolean);
        }
      }
      if (rows.length < 2 || rows.join('').length < 40) return;
      const t = '[table]\n' + rows.join('\n');
      if (seen.has(t) || tableTexts.some((x) => x.includes(t.slice(8, 200)))) return;
      seen.add(t); tableTexts.push(t);
      structure.push(`${el.tagName.toLowerCase()}${el.className && typeof el.className === 'string' ? '.' + el.className.trim().split(/\s+/)[0] : ''}:${rows.length}rows`);
    });
    if (out.passages.length < 3) {
      // Bare-text bodies (<br>-separated): keep long lines as paragraphs and GROUP short lines
      // (table cells, figures, labels) instead of dropping them.
      let buf = [];
      const flush = () => { if (buf.length) { const t = buf.join(' | '); if (!seen.has(t)) { seen.add(t); out.passages.push(t); } buf = []; } };
      clean(root.innerText).split(/\n+/).map((t) => t.trim()).filter(Boolean).forEach((t) => {
        if (t.length > 80) { flush(); if (!seen.has(t)) { seen.add(t); out.passages.push(t); } return; }
        buf.push(t);
        if (buf.join(' | ').length > 400) flush();
      });
      flush();
      out.access_warnings.push('paragraph_structure_not_found — text split by lines (short lines grouped)');
    }
    tableTexts.forEach((t) => out.passages.push(t));
    out.access_warnings.push(structure.length ? `tables_found: ${structure.slice(0, 5).join(', ')}` : 'tables_found: none');
    const pay = [...document.querySelectorAll('[class*="paywall"], [class*="subscribe"], [class*="prime_lock"], [class*="locked"]')].filter(visible);
    if (pay.length) out.access_warnings.push('possible_paywall_or_preview — verify the end of the article is present');
    out.related_links = [...root.querySelectorAll('a[href]')].map((a) => a.href).filter((h) => h.startsWith(location.origin) && h !== location.href).slice(0, 30);
  }
  let total = 0; const kept = [];
  for (const p of out.passages) {
    if (total + p.length > maxChars) { out.truncated = true; break; }
    kept.push(p); total += p.length;
  }
  out.passages = kept;
  if (out.truncated) out.access_warnings.push(`truncated_at_${maxChars}_chars`);
  return out;
}
