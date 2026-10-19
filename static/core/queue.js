(() => {
  const form = document.querySelector('[data-queue-filters]');
  if (!form) return;
  const results = document.querySelector('[data-queue-results]');
  const state = document.querySelector('[data-filter-state]');
  let timer, controller, pendingInput = false;
  const health = document.querySelector('[data-service-health]');
  function healthSummary() {
    if (!health) return;
    const waiting = health.querySelectorAll('[data-quota-state="exhausted"]').length;
    const unknown = health.querySelectorAll('[data-quota-state="unknown"]').length;
    const issues = health.querySelectorAll('[data-graph-state="invalid"],[data-graph-state="unavailable"]').length;
    const summary = health.querySelector('[data-health-summary]');
    summary.textContent = [waiting ? `${waiting} account${waiting === 1 ? '' : 's'} waiting` : unknown ? 'Quota status unavailable' : 'Accounts available', issues ? `${issues} graph status${issues === 1 ? '' : 'es'} need checking` : 'Repository readiness'].join(' · ');
    summary.classList.toggle('health-attention', Boolean(waiting || issues));
  }
  if (health) {
    healthSummary();
    new MutationObserver(healthSummary).observe(health.querySelector('[data-health-content]'), {childList:true,subtree:true});
  }
  function peopleSummary() {
    const active = ['assignee','actor'].filter(name => form.elements[name].value).length;
    form.querySelector('.filter-more summary').textContent = active ? `People filters · ${active} active` : 'People filters';
  }
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
      const fragment = document.createElement('template');
      fragment.innerHTML = html; // Same-origin, server-rendered, Django-escaped fragment only.
      const queue = fragment.content.querySelector('[data-queue-results]');
      const graph = fragment.content.querySelector('[data-graph-results]');
      if (!queue || !graph) throw Error('Invalid dashboard fragment');
      if (results.innerHTML !== queue.innerHTML) {
        results.innerHTML = queue.innerHTML;
        if (!background && !matchMedia('(prefers-reduced-motion: reduce)').matches) {
          results.animate([{opacity:.4, transform:'translateY(4px)'}, {opacity:1, transform:'translateY(0)'}], {duration:220, easing:'ease-out'});
        }
      }
      for (const [name, attribute] of [['assignee', 'assignee'], ['actor', 'requester']]) {
        const choices = fragment.content.querySelector(`[data-${attribute}-options]`);
        const field = form.querySelector(`[name=${name}]`);
        if (choices && field && document.activeElement !== field && field.innerHTML !== choices.innerHTML) {
          field.innerHTML = choices.innerHTML;
        }
      }
      const graphResults = document.querySelector('[data-graph-results]');
      if (graphResults && graphResults.innerHTML !== graph.innerHTML) graphResults.innerHTML = graph.innerHTML;
      const table = results.querySelector('.table-scroll');
      if (table) table.scrollLeft = scrollLeft;
      const counts = results.querySelector('[data-queue-counts]');
      for (const metric of document.querySelectorAll('[data-queue-metric]')) {
        const value = counts?.dataset[metric.dataset.queueMetric];
        if (/^\d+$/.test(value || '')) metric.textContent = value;
      }
      if (history) window.history.replaceState(null, '', visible);
      peopleSummary();
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
  peopleSummary();
  setInterval(refresh, 5000);
  document.addEventListener('visibilitychange', refresh);
})();
