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
    if (out.passages.length < 3) {
      clean(root.innerText).split(/\n{1,}/).filter((t) => t.length > 30).forEach((t) => { if (!seen.has(t)) out.passages.push(t); });
      out.access_warnings.push('paragraph_structure_not_found — text split by lines');
    }
    // Data tables (e.g. "stocks with upside" lists) are sometimes rendered outside the text block.
    document.querySelectorAll('table').forEach((tb) => {
      if (root.contains(tb) || !visible(tb) || tb.closest('nav, footer, aside, header')) return;
      const rows = [...tb.querySelectorAll('tr')].map((tr) => [...tr.querySelectorAll('th,td')].map((c) => clean(c.innerText)).join(' | ')).filter((r) => r.replace(/[|\s]/g, ''));
      if (rows.length < 2) return;
      const t = '[table]\n' + rows.join('\n');
      if (!seen.has(t)) { seen.add(t); out.passages.push(t); }
    });
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
