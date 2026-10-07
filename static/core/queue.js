(() => {
  const form = document.querySelector('[data-queue-filters]');
  if (!form) return;
  const results = document.querySelector('[data-queue-results]');
  const state = document.querySelector('[data-filter-state]');
  let timer, controller, pendingInput = false;
  const jakartaTime = new Intl.DateTimeFormat('en-GB', {timeZone:'Asia/Jakarta',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'});
  async function update(url, history = true, background = false) {
    if (background && (controller || pendingInput || document.hidden || results.contains(document.activeElement))) return;
    controller?.abort();
    const current = new AbortController(); controller = current;
    const timeout = setTimeout(() => current.abort(), 15000);
    results.setAttribute('aria-busy', 'true');
    if (!background) state.textContent = 'Updating queue…';
    const visible = new URL(url, location.href); visible.searchParams.delete('partial');
    const request = new URL(visible); request.searchParams.set('partial', '1');
    try {
      const response = await fetch(request, {signal:current.signal, credentials:'same-origin', cache:'no-store'});
      if (response.redirected) { location.assign(response.url); return; }
      if (!response.ok) throw Error('Unavailable');
      const html = await response.text();
      if (controller !== current) return;
      if (background && results.contains(document.activeElement)) return;
      const scrollLeft = results.querySelector('.table-scroll')?.scrollLeft || 0;
      if (results.innerHTML !== html) results.innerHTML = html; // Same-origin, server-rendered, Django-escaped fragment only.
      const table = results.querySelector('.table-scroll');
      if (table) table.scrollLeft = scrollLeft;
      const counts = results.querySelector('[data-queue-counts]');
      for (const metric of document.querySelectorAll('[data-queue-metric]')) {
        const value = counts?.dataset[metric.dataset.queueMetric];
        if (/^\d+$/.test(value || '')) metric.textContent = value.padStart(2, '0');
      }
      if (history) window.history.replaceState(null, '', visible);
      state.textContent = `Live · updated ${jakartaTime.format(new Date())} WIB · every 5s`;
    } catch {
      if (controller === current) state.textContent = 'Connection interrupted · showing previous data; retrying automatically.';
    } finally {
      clearTimeout(timeout);
      if (controller === current) { results.removeAttribute('aria-busy'); controller = null; }
    }
  }
  function selection() {
    const url = new URL(location.pathname, location.origin);
    for (const [key,value] of new FormData(form)) if (value) url.searchParams.set(key,value);
    return url;
  }
  form.addEventListener('submit', e => { e.preventDefault(); clearTimeout(timer); pendingInput=false; update(selection()); });
  form.querySelectorAll('select').forEach(select => select.addEventListener('change', () => {clearTimeout(timer); pendingInput=false; update(selection());}));
  form.querySelector('[name=q]').addEventListener('input', () => {clearTimeout(timer); controller?.abort(); pendingInput=true; timer=setTimeout(()=>{pendingInput=false;update(selection());},350);});
  form.querySelector('[data-clear-filters]').addEventListener('click', e => {e.preventDefault(); clearTimeout(timer); pendingInput=false; form.querySelectorAll('input,select').forEach(el=>el.value=''); update(selection());});
  results.addEventListener('click', e => {const link=e.target.closest('.pagination a'); if (!link || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return; e.preventDefault(); update(link.href);});
  window.addEventListener('popstate', () => {clearTimeout(timer); pendingInput=false; const params=new URL(location.href).searchParams; for(const field of form.querySelectorAll('[name]')) field.value=params.get(field.name)||''; update(location.href,false);});
  const refresh = () => update(location.href, false, true);
  setInterval(refresh, 5000);
  document.addEventListener('visibilitychange', refresh);
})();
