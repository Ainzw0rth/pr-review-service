(() => {
  const panel = document.querySelector('[data-activity-url]');
  if (!panel) return;
  const log = panel.querySelector('.activity-log');
  const connection = panel.querySelector('.activity-connection');
  const heartbeat = panel.querySelector('.activity-heartbeat');
  const pause = panel.querySelector('.activity-pause');
  let paused = false;
  let stopped = false;
  let busy = false;
  let latest = 0;
  const utc = (value) => value ? new Date(value).toISOString().replace('T', ' ').replace('.000Z', ' UTC') : '';
  const set = (selector, value) => {
    const element = document.querySelector(selector);
    if (element) element.textContent = value;
  };
  async function poll() {
    if (paused || stopped || busy || document.hidden) return;
    busy = true;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch(panel.dataset.activityUrl, {
        credentials: 'same-origin', cache: 'no-store', signal: controller.signal,
      });
      if (response.redirected || [401, 403, 404].includes(response.status)) {
        stopped = true;
        connection.textContent = 'Feed unavailable. Sign in again or check workspace access.';
        return;
      }
      if (!response.ok) throw new Error('Unavailable');
      const data = await response.json();
      // A pause during an in-flight fetch also freezes rendering.
      if (paused) return;
      set('[data-job-status]', `${data.status_label}.`);
      const badge = document.querySelector('[data-job-badge]');
      badge.textContent = data.status_label;
      badge.className = `status status-${data.status}`;
      set('[data-job-provider]', data.provider || 'Not assigned');
      set('[data-job-started]', utc(data.started_at) || 'Not started');
      set('[data-job-finished]', utc(data.finished_at) || 'Not finished');
      const note = document.querySelector('[data-job-note]');
      note.hidden = !data.reason;
      note.querySelector('p').textContent = data.reason || '';
      connection.textContent = `Connected · ${data.status_label}${data.provider ? ` · ${data.provider}` : ''}`;
      if (data.status === 'running') {
        const age = data.heartbeat_at ? Math.max(0, Math.floor((Date.now() - Date.parse(data.heartbeat_at)) / 1000)) : null;
        heartbeat.textContent = age === null ? 'Waiting for the first worker heartbeat.' :
          `Last worker heartbeat ${age}s ago.${age > 30 ? ' Heartbeat delayed; worker may be unavailable.' : ''}`;
      } else if (data.next_retry_at && data.status === 'quota') {
        heartbeat.textContent = `Next availability check: ${utc(data.next_retry_at)}. Not a guaranteed quota reset.`;
      } else {
        heartbeat.textContent = data.status === 'queued' ? 'Waiting for the serial worker.' : 'Worker not currently reviewing this attempt.';
      }
      const follow = log.scrollHeight - log.scrollTop - log.clientHeight < 50;
      for (const event of data.events) {
        if (event.id <= latest) continue;
        log.querySelector('.activity-empty')?.remove();
        const line = document.createElement('li');
        const time = document.createElement('time');
        time.dateTime = event.at;
        time.textContent = utc(event.at);
        const text = document.createElement('span');
        text.textContent = event.message;
        line.append(time, text);
        log.append(line);
        latest = event.id;
      }
      while (log.children.length > 100) log.firstElementChild.remove();
      if (!data.events.length && !latest) log.querySelector('.activity-empty').textContent =
        'No recorded activity yet. Older attempts have no historical activity log.';
      if (follow) log.scrollTop = log.scrollHeight;
    } catch {
      if (!paused) connection.textContent = 'Connection interrupted. Retrying; displayed activity may be stale.';
    } finally {
      clearTimeout(timeout);
      busy = false;
    }
  }
  pause.addEventListener('click', () => {
    paused = !paused;
    pause.textContent = paused ? 'Resume updates' : 'Pause updates';
    pause.setAttribute('aria-pressed', String(paused));
    if (paused) connection.textContent = 'Display paused. The review continues in the background.';
    else poll();
  });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });
  poll();
  setInterval(poll, 3000);
})();
