/* global personalCaExtract */
// Opt-in "capture ET articles I open". Registered by the background worker ONLY after you switch
// the option on (and ET access is granted). Reads the visible article you are already reading,
// waits for lazy-loaded text, and hands it to the background worker. Never opens pages itself.
(() => {
  const TRIES = [1500, 4000, 8000, 15000];   // ms: wait for paywalled/lazy text to render
  const MIN_CHARS = 600;
  let sentFor = null;

  function attempt(last) {
    const url = location.href.split('#')[0].split('?')[0];
    if (sentFor === url || !/\/articleshow\//.test(url)) return;
    let r;
    try { r = personalCaExtract('page', 80000); } catch (e) { return; }
    const chars = (r.passages || []).reduce((a, p) => a + p.length, 0);
    if (chars < MIN_CHARS) {
      if (last) chrome.runtime.sendMessage({ type: 'autoCaptureShort', url, chars });
      return;
    }
    sentFor = url;
    chrome.runtime.sendMessage({ type: 'autoCapture', result: r });
  }

  const schedule = () => TRIES.forEach((ms, i) => setTimeout(() => attempt(i === TRIES.length - 1), ms));
  schedule();
  let lastUrl = location.href;
  setInterval(() => { if (location.href !== lastUrl) { lastUrl = location.href; sentFor = null; schedule(); } }, 1000);
})();
