(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  let state, selected = null, dragged = null, dirty = false, nextKey = 0, busy = false;
  const status = text => { $('workflow-status').textContent = text; };
  const csrf = document.querySelector('#workflow-form [name=csrfmiddlewaretoken]').value;
  const mark = () => { dirty = true; status('Unsaved changes · applies to new jobs only'); $('workflow-save').disabled = false; $('model-test').disabled = true; };
  function el(tag, text, cls) { const node = document.createElement(tag); if (text) node.textContent = text; if (cls) node.className = cls; return node; }
  function button(text, label, action) { const b = el('button', text, 'text-button'); b.type = 'button'; b.setAttribute('aria-label', label); b.onclick = action; return b; }
  function selectedModel() { return state.models.find(m => m.key === selected); }
  function select(key) {
    selected = key;
    const m = selectedModel();
    const panel = $('model-config-panel'), opening = panel.hidden && Boolean(m);
    panel.hidden = !m;
    document.querySelector('.workflow-layout').classList.toggle('has-selection',Boolean(m));
    $('model-empty').hidden = Boolean(m); $('model-settings').hidden = !m;
    if (!m) return;
    $('model-name').value = m.name; $('model-kind').value = m.kind; $('model-id').value = m.model; $('model-effort').value = m.effort;
    $('model-kind').disabled = Boolean(m.id);
    for (const option of $('model-kind').options) option.disabled = !m.id && !option.value.endsWith('_cli');
    $('model-effort').disabled = m.kind !== 'codex_cli';
    $('model-test').disabled = !m.id || dirty || !m.kind.endsWith('_cli');
    probeStatus();
    if (opening && !reducedMotion.matches) panel.animate([{opacity:0,transform:'translateY(6px)'},{opacity:1,transform:'translateY(0)'}],{duration:180,easing:'ease-out'});
    if (opening && innerWidth < 900) panel.scrollIntoView({behavior:reducedMotion.matches ? 'auto' : 'smooth',block:'nearest'});
  }
  function probeStatus() {
    const m = selectedModel(); if (!m) return;
    const p = state.probes.find(p => p.provider_id === m.id);
    $('model-probe').textContent = p ? `${p.model} · ${p.status}. ${p.reason}` : 'Not tested here yet.';
  }
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  let dragSource = null, dragPreview = null, dropMarker = null, dropBefore = null;
  const positions = () => new Map([...document.querySelectorAll('#workflow-chain .workflow-node')].map(node => [node.dataset.key, node.getBoundingClientRect()]));
  function animateNodes(previous) {
    if (reducedMotion.matches) return;
    for (const node of $('workflow-chain').querySelectorAll('.workflow-node')) {
      const before = previous.get(node.dataset.key); if (!before) continue;
      const after = node.getBoundingClientRect(), dx = before.left - after.left, dy = before.top - after.top;
      if (Math.abs(dx) + Math.abs(dy) > 1) node.animate([
        {transform: `translate(${dx}px, ${dy}px)`}, {transform: 'translate(0, 0)'}
      ], {duration: 220, easing: 'cubic-bezier(.2,.8,.2,1)'});
    }
  }
  function clearDrag() {
    dragSource?.classList.remove('is-dragging');
    dragPreview?.remove(); dropMarker?.remove();
    $('workflow-chain').classList.remove('is-drop-target');
    document.body.classList.remove('workflow-dragging');
    dragged = null; dragSource = null; dragPreview = null; dropMarker = null; dropBefore = null;
  }
  function startDrag(event, model, source) {
    if (busy) { event.preventDefault(); return; }
    clearDrag();
    for (const node of $('workflow-chain').querySelectorAll('.workflow-node')) node.getAnimations().forEach(animation => animation.cancel());
    dragged = model.key; dragSource = source;
    event.dataTransfer.setData('text/plain', model.key);
    event.dataTransfer.effectAllowed = 'move';
    const rect = source.getBoundingClientRect();
    dragPreview = el('div', '', 'workflow-drag-preview');
    dragPreview.style.width = `${Math.min(rect.width, 380)}px`;
    dragPreview.style.left = `${rect.left}px`; dragPreview.style.top = `${rect.top}px`;
    dragPreview.setAttribute('aria-hidden', 'true');
    dragPreview.append(el('span', '⋮⋮  MOVE REVIEWER', 'eyebrow'), el('strong', model.name || 'New model'), el('code', model.model), el('span', 'Drop into the review sequence', 'note'));
    document.body.append(dragPreview);
    event.dataTransfer.setDragImage(dragPreview, Math.min(event.clientX - rect.left, 160), 28);
    dropMarker = el('li', 'Drop here', 'workflow-drop-marker');
    dropMarker.setAttribute('aria-hidden', 'true');
    document.body.classList.add('workflow-dragging');
    status(`Moving ${model.name}. Drop at a marker to place it; press Escape to cancel.`);
    // Capture the solid preview before dimming the original card.
    requestAnimationFrame(() => { dragPreview?.remove(); dragSource?.classList.add('is-dragging'); });
  }
  function showDrop(event) {
    if (!dragged || busy) return;
    event.preventDefault(); event.dataTransfer.dropEffect = 'move';
    const chain = $('workflow-chain'); chain.classList.add('is-drop-target');
    if (event.target.closest('.workflow-drop-marker')) return;
    const top = chain.getBoundingClientRect().top;
    const nodes = [...chain.querySelectorAll('.workflow-node')].filter(node => node.dataset.key !== dragged);
    const target = nodes.find(node => event.clientY < top + node.offsetTop + node.offsetHeight / 2);
    const before = target?.dataset.key ?? null;
    if (dropMarker.parentNode === chain && before === dropBefore) return;
    const previous = positions();
    for (const node of chain.querySelectorAll('.workflow-node')) node.getAnimations().forEach(animation => animation.cancel());
    dropBefore = before; chain.insertBefore(dropMarker, target || null); animateNodes(previous);
  }
  function move(key, delta) {
    const active = state.models.filter(m => m.enabled);
    const i = active.findIndex(m => m.key === key), target = active[i + delta];
    if (!target) return;
    const from = state.models.findIndex(m => m.key === key), to = state.models.indexOf(target);
    const [m] = state.models.splice(from, 1); state.models.splice(to, 0, m);
    mark(); render();
    [...$('workflow-chain').querySelectorAll('.workflow-node')].find(node => node.dataset.key === key)?.querySelector('button')?.focus();
  }
  function drop(before) {
    if (busy) return;
    const m = state.models.find(m => m.key === dragged); if (!m || dragged === before) return;
    state.models = state.models.filter(item => item !== m);
    const index = state.models.findIndex(item => item.key === before);
    m.enabled = true; state.models.splice(index < 0 ? state.models.length : index, 0, m);
    clearDrag(); mark(); render();
  }
  function render() {
    const previous = positions();
    $('model-library').replaceChildren(); $('workflow-chain').replaceChildren();
    let index = 0;
    for (const m of state.models) {
      const item = el('div', '', 'workflow-model'); item.draggable = true;
      item.addEventListener('dragstart', e => startDrag(e, m, item));
      item.append(el('span', '⋮⋮', 'workflow-drag-handle'));
      item.append(button(m.name || 'New model', `Edit ${m.name}`, () => select(m.key)), el('code', m.model || 'Set model ID'));
      if (!m.id) item.append(button('Discard', 'Discard new model', () => { state.models = state.models.filter(row => row !== m); if (selected === m.key) selected = null; mark(); render(); select(selected); }));
      const add = button(m.enabled ? 'In sequence' : 'Add →', `Add ${m.name} to sequence`, () => { m.enabled = true; mark(); render(); });
      add.disabled = m.enabled; item.append(add); if (!m.enabled) $('model-library').append(item);
      if (!m.enabled) continue;
      const node = el('li', '', 'workflow-node'); node.draggable = true;
      node.dataset.key = m.key;
      node.addEventListener('dragstart', e => startDrag(e, m, node));
      const handle = el('span', '⋮⋮', 'workflow-drag-handle'); handle.setAttribute('aria-hidden', 'true'); node.append(handle);
      node.append(el('span', String(++index).padStart(2, '0'), 'section-number'), button(m.name || 'New model', `Configure ${m.name}`, () => select(m.key)), el('code', m.model));
      const controls = el('div', '', 'workflow-node-controls');
      const up = button('↑', `Move ${m.name} up`, () => move(m.key, -1)); up.disabled = index === 1;
      const down = button('↓', `Move ${m.name} down`, () => move(m.key, 1)); down.disabled = index === state.models.filter(m => m.enabled).length;
      controls.append(up, down, button('Remove', `Remove ${m.name} from sequence`, () => { m.enabled = false; mark(); render(); }));
      node.append(controls); $('workflow-chain').append(node);
    }
    if (!index) $('workflow-chain').append(el('li', 'Add a model to begin.', 'workflow-terminal'));
    $('workflow-version').textContent = `WORKFLOW / ${state.version.slice(0, 8)}`;
    if (selected) select(selected);
    animateNodes(previous);
  }
  async function request(url, data) {
    const response = await fetch(url, {method: data ? 'POST' : 'GET', headers: data ? {'Content-Type': 'application/json', 'X-CSRFToken': csrf} : {}, body: data ? JSON.stringify(data) : undefined, cache: 'no-store'});
    let result; try { result = await response.json(); } catch { throw Error('Session expired or service unavailable. Reload and sign in again.'); }
    if (!response.ok) throw Error(result.error || 'Request failed.'); return result;
  }
  function hydrate(data) { const selectedIndex = state?.models.findIndex(m => m.key === selected) ?? -1; state = data; state.models.forEach(m => { m.key = `model-${m.id}`; }); selected = state.models[selectedIndex]?.key ?? null; render(); }
  $('model-new').onclick = () => { const m = {id: null, key: `new-${++nextKey}`, name: 'New model', kind: 'codex_cli', model: '', effort: '', enabled: false}; state.models.push(m); selected = m.key; mark(); render(); $('model-name').focus(); };
  $('model-close').onclick = () => { select(null); document.querySelector('#workflow-chain button, [data-model-library] summary')?.focus({preventScroll:true}); };
  $('model-kind').onchange = () => { $('model-effort').disabled = $('model-kind').value !== 'codex_cli'; if ($('model-effort').disabled) $('model-effort').value = ''; };
  $('model-settings').onsubmit = e => {
    e.preventDefault(); const m = selectedModel();
    Object.assign(m, {name: $('model-name').value.trim(), kind: $('model-kind').value, model: $('model-id').value.trim(), effort: $('model-effort').value}); mark(); render();
  };
  $('model-settings').addEventListener('input', () => {
    const m = selectedModel();
    Object.assign(m, {name: $('model-name').value.trim(), kind: $('model-kind').value, model: $('model-id').value.trim(), effort: $('model-kind').value === 'codex_cli' ? $('model-effort').value : ''});
    mark();
  });
  $('workflow-chain').addEventListener('dragover', showDrop);
  $('workflow-chain').addEventListener('dragleave', e => {
    if (!$('workflow-chain').contains(e.relatedTarget)) {
      dropMarker?.remove(); $('workflow-chain').classList.remove('is-drop-target');
    }
  });
  $('workflow-chain').addEventListener('drop', e => {
    if (!dragged) return;
    e.preventDefault();
    if (dropMarker?.parentNode !== $('workflow-chain')) showDrop(e);
    drop(dropBefore);
  });
  $('workflow-form').onsubmit = async e => {
    e.preventDefault(); if (busy || (selectedModel() && !$('model-settings').reportValidity())) return; busy = true; $('workflow-save').disabled = true; $('workflow-editor').inert = true;
    try { const data = await request('/workflow/state/', {version: state.version, models: state.models}); dirty = false; hydrate(data); status('Saved · new jobs will use this workflow.'); }
    catch (error) { status(error.message); $('workflow-save').disabled = false; }
    finally { busy = false; $('workflow-editor').inert = false; }
  };
  $('model-test').onclick = async () => {
    const m = selectedModel(); if (!m || dirty) return; $('model-test').disabled = true;
    try { const result = await request(`/workflow/probe/${m.id}/`, {}); $('model-probe').textContent = result.message; }
    catch (error) { $('model-probe').textContent = error.message; }
  };
  document.addEventListener('dragend', () => { clearDrag(); status(dirty ? 'Unsaved changes · applies to new jobs only' : 'Saved configuration · drag nodes or use arrow buttons to reorder.'); });
  window.addEventListener('beforeunload', e => { if (dirty) { e.preventDefault(); e.returnValue = ''; } });
  request('/workflow/state/').then(data => { hydrate(data); $('workflow-editor').hidden = false; status('Saved configuration · drag nodes or use arrow buttons to reorder.'); }).catch(error => status(error.message));
  setInterval(async () => { if (!state || document.hidden || busy) return; try { const data = await request('/workflow/state/'); state.probes = data.probes; probeStatus(); if (!dirty && selectedModel()) $('model-test').disabled = !selectedModel().id || !selectedModel().kind.endsWith('_cli') || data.probes.some(p => ['queued', 'running'].includes(p.status)); } catch { /* Keep draft intact during network interruptions. */ } }, 5000);
})();
