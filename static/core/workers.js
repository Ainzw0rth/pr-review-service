(() => {
  const root = document.querySelector('.workers-page');
  if (!root) return;
  const status = document.querySelector('#fleet-refresh');
  let busy = false, stopped = false;
  async function refresh() {
    if (busy || stopped || document.hidden) return;
    busy = true;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 8000);
    try {
      const response = await fetch(root.dataset.stateUrl, {headers: {'Accept': 'application/json'}, cache: 'no-store', signal: controller.signal});
      if (response.status === 403 || response.status === 401 || response.redirected) {
        stopped = true;
        throw new Error('Access denied. Refresh stopped; sign in again.');
      }
      if (!response.ok) throw new Error('Updates unavailable. Displayed data may be stale.');
      const data = await response.json();
      root.querySelectorAll('[data-stat]').forEach(node => { node.textContent = data[node.dataset.stat]; });
      const ids = new Set(data.workers.map(worker => worker.id));
      root.querySelectorAll('[data-worker]').forEach(card => {
        if (!ids.has(card.dataset.worker)) document.querySelector('#fleet-change').hidden = false;
      });
      data.workers.forEach(worker => {
        const card = root.querySelector(`[data-worker="${CSS.escape(worker.id)}"]`);
        if (!card) { document.querySelector('#fleet-change').hidden = false; return; }
        card.querySelectorAll('[data-field]').forEach(node => {
          let value = worker[node.dataset.field];
          if (node.dataset.field === 'keepalive') value = worker.keepalive?.status || 'Not configured';
          if (node.dataset.field === 'last_success') value = worker.keepalive?.last_success_at || 'Never';
          if (node.dataset.field === 'next_due') value = worker.keepalive?.next_due_at || 'Not scheduled';
          if (Array.isArray(value)) value = value.join(' · ') || (node.dataset.field === 'work' ? 'No active assignments' : 'No readiness problems reported');
          node.textContent = value ?? 'Unknown';
        });
      });
      status.textContent = `Updated ${new Date(data.updated_at).toLocaleTimeString()}. Live updates every 10 seconds.`;
    } catch (error) {
      status.textContent = stopped ? error.message : 'Updates unavailable. Displayed data may be stale; retrying.';
    } finally { clearTimeout(timeout); busy = false; }
  }
  setInterval(refresh, 10000);
  document.addEventListener('visibilitychange', refresh);
  root.querySelectorAll('form').forEach(form => form.addEventListener('submit', () => {
    form.setAttribute('aria-busy', 'true');
    // Preserve successful submitter name/value; do not disable it before submission.
  }));
})();
