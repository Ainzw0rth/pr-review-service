(() => {
  const form = document.querySelector('[data-queue-filters]');
  if (!form) return;
  const results = document.querySelector('[data-queue-results]');
  const state = document.querySelector('[data-filter-state]');
  let timer, controller;
  async function update(url, history = true) {
    controller?.abort();
    const current = new AbortController(); controller = current;
    const timeout = setTimeout(() => current.abort(), 15000);
    results.setAttribute('aria-busy', 'true'); state.textContent = 'Updating queue…';
    const visible = new URL(url, location.href); visible.searchParams.delete('partial');
    const request = new URL(visible); request.searchParams.set('partial', '1');
    try {
      const response = await fetch(request, {signal:current.signal, credentials:'same-origin', cache:'no-store'});
      if (response.redirected) { location.assign(response.url); return; }
      if (!response.ok) throw Error('Unavailable');
      const html = await response.text();
      if (controller !== current) return;
      results.innerHTML = html; // Same-origin, server-rendered, Django-escaped fragment only.
      if (history) window.history.replaceState(null, '', visible);
      state.textContent = 'Queue updated.';
    } catch {
      if (controller === current) state.textContent = 'Could not update. Change a filter to retry.';
    } finally {
      clearTimeout(timeout);
      if (controller === current) results.removeAttribute('aria-busy');
    }
  }
  function selection() {
    const url = new URL(location.pathname, location.origin);
    for (const [key,value] of new FormData(form)) if (value) url.searchParams.set(key,value);
    return url;
  }
  form.addEventListener('submit', e => { e.preventDefault(); clearTimeout(timer); update(selection()); });
  form.querySelectorAll('select').forEach(select => select.addEventListener('change', () => {clearTimeout(timer); update(selection());}));
  form.querySelector('[name=q]').addEventListener('input', () => {clearTimeout(timer); controller?.abort(); timer=setTimeout(()=>update(selection()),350);});
  form.querySelector('[data-clear-filters]').addEventListener('click', e => {e.preventDefault(); clearTimeout(timer); form.querySelectorAll('input,select').forEach(el=>el.value=''); update(selection());});
  results.addEventListener('click', e => {const link=e.target.closest('.pagination a'); if (!link || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return; e.preventDefault(); update(link.href);});
  window.addEventListener('popstate', () => {const params=new URL(location.href).searchParams; for(const field of form.querySelectorAll('[name]')) field.value=params.get(field.name)||''; update(location.href,false);});
})();
