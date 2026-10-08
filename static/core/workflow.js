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
    $('model-empty').hidden = Boolean(m); $('model-settings').hidden = !m;
    if (!m) return;
    $('model-name').value = m.name; $('model-kind').value = m.kind; $('model-id').value = m.model; $('model-effort').value = m.effort;
    $('model-kind').disabled = Boolean(m.id);
    for (const option of $('model-kind').options) option.disabled = !m.id && !option.value.endsWith('_cli');
    $('model-effort').disabled = m.kind !== 'codex_cli';
    $('model-test').disabled = !m.id || dirty || !m.kind.endsWith('_cli');
    probeStatus();
  }
  function probeStatus() {
    const m = selectedModel(); if (!m) return;
    const p = state.probes.find(p => p.provider_id === m.id);
    $('model-probe').textContent = p ? `${p.model} · ${p.status}. ${p.reason}` : 'Not tested here yet.';
  }
  function move(key, delta) {
    const active = state.models.filter(m => m.enabled);
    const i = active.findIndex(m => m.key === key), target = active[i + delta];
    if (!target) return;
    const from = state.models.findIndex(m => m.key === key), to = state.models.indexOf(target);
    const [m] = state.models.splice(from, 1); state.models.splice(to, 0, m);
    mark(); render();
  }
  function drop(before) {
    if (busy) return;
    const m = state.models.find(m => m.key === dragged); if (!m || dragged === before) return;
    state.models = state.models.filter(item => item !== m);
    const index = state.models.findIndex(item => item.key === before);
    m.enabled = true; state.models.splice(index < 0 ? state.models.length : index, 0, m);
    mark(); render();
  }
  function render() {
    $('model-library').replaceChildren(); $('workflow-chain').replaceChildren();
    let index = 0;
    for (const m of state.models) {
      const item = el('div', '', 'workflow-model'); item.draggable = true;
      item.addEventListener('dragstart', e => { dragged = m.key; e.dataTransfer.setData('text/plain', m.key); e.dataTransfer.effectAllowed = 'move'; });
      item.append(button(m.name || 'New model', `Edit ${m.name}`, () => select(m.key)), el('code', m.model || 'Set model ID'));
      if (!m.id) item.append(button('Discard', 'Discard new model', () => { state.models = state.models.filter(row => row !== m); if (selected === m.key) selected = null; mark(); render(); select(selected); }));
      const add = button(m.enabled ? 'In sequence' : 'Add →', `Add ${m.name} to sequence`, () => { m.enabled = true; mark(); render(); });
      add.disabled = m.enabled; item.append(add); $('model-library').append(item);
      if (!m.enabled) continue;
      const node = el('li', '', 'workflow-node'); node.draggable = true;
      node.addEventListener('dragstart', e => { dragged = m.key; e.dataTransfer.setData('text/plain', m.key); });
      node.addEventListener('dragover', e => { e.preventDefault(); });
      node.addEventListener('drop', e => { e.preventDefault(); e.stopPropagation(); drop(m.key); });
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
  }
  async function request(url, data) {
    const response = await fetch(url, {method: data ? 'POST' : 'GET', headers: data ? {'Content-Type': 'application/json', 'X-CSRFToken': csrf} : {}, body: data ? JSON.stringify(data) : undefined, cache: 'no-store'});
    let result; try { result = await response.json(); } catch { throw Error('Session expired or service unavailable. Reload and sign in again.'); }
    if (!response.ok) throw Error(result.error || 'Request failed.'); return result;
  }
  function hydrate(data) { const selectedIndex = state?.models.findIndex(m => m.key === selected) ?? -1; state = data; state.models.forEach(m => { m.key = `model-${m.id}`; }); selected = state.models[selectedIndex]?.key ?? null; render(); }
  $('model-new').onclick = () => { const m = {id: null, key: `new-${++nextKey}`, name: 'New model', kind: 'codex_cli', model: '', effort: '', enabled: false}; state.models.push(m); selected = m.key; mark(); render(); $('model-name').focus(); };
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
  $('workflow-chain').addEventListener('dragover', e => e.preventDefault());
  $('workflow-chain').addEventListener('drop', e => { e.preventDefault(); drop(null); });
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
  document.addEventListener('dragend', () => { dragged = null; });
  window.addEventListener('beforeunload', e => { if (dirty) { e.preventDefault(); e.returnValue = ''; } });
  request('/workflow/state/').then(data => { hydrate(data); $('workflow-editor').hidden = false; status('Saved configuration · drag nodes or use arrow buttons to reorder.'); }).catch(error => status(error.message));
  setInterval(async () => { if (!state || document.hidden || busy) return; try { const data = await request('/workflow/state/'); state.probes = data.probes; probeStatus(); if (!dirty && selectedModel()) $('model-test').disabled = !selectedModel().id || !selectedModel().kind.endsWith('_cli') || data.probes.some(p => ['queued', 'running'].includes(p.status)); } catch { /* Keep draft intact during network interruptions. */ } }, 5000);
})();
