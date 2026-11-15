// Opens the side panel when the toolbar icon is clicked. No background capture, no cookies,
// no network access except the user's own local Personal CA service.
chrome.runtime.onInstalled.addListener(() => {
  chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => {});
});
