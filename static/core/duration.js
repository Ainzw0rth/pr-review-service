(() => {
  function update() {
    document.querySelectorAll('[data-review-duration]').forEach(timer => {
      const {start, finish, status} = timer.dataset;
      if (!start || (!finish && status !== 'running')) {
        timer.textContent = start ? 'Unavailable' : 'Not started';
        return;
      }
      const elapsed = ((finish ? Date.parse(finish) : Date.now()) - Date.parse(start)) / 1000;
      if (!Number.isFinite(elapsed)) { timer.textContent = 'Unavailable'; return; }
      const total = Math.max(0, Math.floor(elapsed));
      const hours = Math.floor(total / 3600), minutes = Math.floor(total / 60) % 60, seconds = total % 60;
      const value = hours ? `${hours}h ${minutes}m ${seconds}s` : minutes ? `${minutes}m ${seconds}s` : `${seconds}s`;
      if (timer.textContent !== value) timer.textContent = value;
    });
  }
  update();
  setInterval(() => { if (!document.hidden) update(); }, 1000);
})();
