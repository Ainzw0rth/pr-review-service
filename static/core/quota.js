(() => {
  const panel = document.querySelector('[data-quota-url]');
  if (!panel) return;
  let busy = false;
  async function refresh() {
    if (busy || document.hidden) return;
    busy = true;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch(panel.dataset.quotaUrl, {credentials:'same-origin',cache:'no-store',signal:controller.signal});
      if (response.redirected || response.status === 403) { panel.replaceChildren(); return; }
      if (response.ok) panel.innerHTML = await response.text(); // Django-escaped, member-only HTML.
    } catch { /* Keep last observed timestamps visible. */ }
    finally { clearTimeout(timeout); busy = false; }
  }
  setInterval(refresh, 15000);
  document.addEventListener('visibilitychange', refresh);
})();
