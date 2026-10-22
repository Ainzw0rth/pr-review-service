(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
  const csrf = document.querySelector('#workflow-form [name=csrfmiddlewaretoken]').value;
  const builtins = new Set(['code', 'security', 'tests']);
  let state, selected = null, dirty = false, busy = false, nextKey = 0;
  let dragged = null, dragSource = null, dropMarker = null, dropBefore = null;
  const status = text => { $('workflow-status').textContent = text; };
  function el(tag, text, cls) { const node = document.createElement(tag); if (text) node.textContent = text; if (cls) node.className = cls; return node; }
  function button(text, label, action, cls = 'text-button') { const node = el('button', text, cls); node.type = 'button'; node.setAttribute('aria-label', label); node.onclick = action; return node; }
  const selectedModel = () => selected?.type === 'model' ? state.models.find(m => m.uiKey === selected.key) : null;
  const selectedAgent = () => selected?.type === 'agent' ? state.agents.find(a => a.uiKey === selected.key) : null;
  function mark() {
    dirty = true; document.querySelector('.workflow-toolbar').dataset.dirty = 'true';
    status('Unsaved changes · new jobs only'); $('workflow-save').disabled = busy; $('model-test').disabled = true;
  }
  function tab(type, focus = false) {
    for (const name of ['models', 'reviewers']) {
      const active = name === type;
      $(`${name}-tab`).setAttribute('aria-selected', String(active)); $(`${name}-tab`).tabIndex = active ? 0 : -1; $(`${name}-library`).hidden = !active;
    }
    if (focus) $(`${type}-tab`).focus();
  }
  function probeStatus() {
    const m = selectedModel(); if (!m) return;
    const probe = (state.probes || []).find(p => p.provider_id === m.id);
    $('model-probe').textContent = probe ? `${probe.model} · ${probe.status}. ${probe.reason}` : 'Not tested here yet.';
    $('model-test').disabled = busy || dirty || !m.id || !m.kind.endsWith('_cli') || (state.probes || []).some(p => ['queued', 'running'].includes(p.status));
  }
  function select(type, key, focus = false) {
    selected = key ? {type, key} : null;
    const m = selectedModel(), a = selectedAgent();
    const panel = $('model-config-panel'); panel.hidden = !m && !a;
    $('model-settings').hidden = !m; $('reviewer-settings').hidden = !a;
    if (m) {
      tab('models'); $('inspector-heading').textContent = 'Edit model';
      $('model-name').value = m.name; $('model-kind').value = m.kind; $('model-id').value = m.model; $('model-effort').value = m.effort || '';
      $('model-kind').disabled = Boolean(m.id); $('model-discard').hidden = Boolean(m.id);
      for (const option of $('model-kind').options) option.disabled = !m.id && !option.value.endsWith('_cli');
      $('model-effort').disabled = m.kind !== 'codex_cli'; probeStatus();
    } else if (a) {
      tab('reviewers'); $('inspector-heading').textContent = 'Edit reviewer';
      $('reviewer-name').value = a.name; $('reviewer-instructions').value = a.instructions || '';
      const builtin = builtins.has(a.key);
      $('reviewer-name').readOnly = builtin; $('reviewer-instructions').readOnly = builtin;
      for (const id of ['reviewer-name', 'reviewer-instructions']) { $(id).hidden = builtin; document.querySelector(`label[for="${id}"]`).hidden = builtin; }
      if (builtin) {
        $('inspector-heading').textContent = `${a.name} reviewer`;
        $('reviewer-builtin-note').textContent = {code:'Built-in review of correctness, behavior and maintainability.', security:'Built-in review of trust boundaries, authorization and sensitive data.', tests:'Built-in review of test coverage and regression risks.'}[a.key];
      }
      $('reviewer-builtin-note').hidden = !builtin; $('reviewer-remove').hidden = builtin;
    }
    highlight();
    if (focus && (m || a)) {
      $(m ? 'model-name' : builtins.has(a.key) ? 'model-close' : 'reviewer-name').focus({preventScroll:true});
      panel.scrollIntoView({behavior:reducedMotion.matches ? 'auto' : 'smooth', block:'nearest'});
    }
  }
  function highlight() {
    for (const node of document.querySelectorAll('[data-model-key], [data-agent-key]')) node.classList.toggle('is-selected', Boolean(selected && ((selected.type === 'model' && node.dataset.modelKey === selected.key) || (selected.type === 'agent' && node.dataset.agentKey === selected.key))));
  }
  function positions() { return new Map([...$('workflow-chain').querySelectorAll('.workflow-node')].map(node => [node.dataset.modelKey, node.getBoundingClientRect()])); }
  function animateNodes(previous) {
    if (reducedMotion.matches) return;
    for (const node of $('workflow-chain').querySelectorAll('.workflow-node')) {
      const before = previous.get(node.dataset.modelKey); if (!before) continue;
      const dy = before.top - node.getBoundingClientRect().top;
      if (Math.abs(dy) > 1) node.animate([{transform:`translateY(${dy}px)`}, {transform:'translateY(0)'}], {duration:200, easing:'cubic-bezier(.2,.8,.2,1)'});
    }
  }
  function focusAction(type, key) {
    const node = [...$(type === 'model' ? 'model-library' : 'reviewer-library').children].find(row => row.dataset[type === 'model' ? 'modelKey' : 'agentKey'] === key)?.querySelector('.workflow-item-action');
    if (node?.getClientRects().length) node.focus({preventScroll:true});
    else $(type === 'model' ? 'workflow-chain' : 'workflow-reviewers').querySelector('button:not(:disabled)')?.focus({preventScroll:true});
  }
  function toggleModel(m) {
    if (busy) return;
    if (m.enabled && state.models.filter(row => row.enabled).length === 1) { status('Keep at least one model in the order.'); return; }
    m.enabled = !m.enabled; mark(); render(); focusAction('model', m.uiKey);
  }
  function toggleAgent(a) {
    if (busy) return;
    const enabled = state.agents.filter(row => row.enabled).length;
    if (a.enabled && enabled === 1) { status('Keep at least one reviewer enabled.'); return; }
    if (!a.enabled && enabled >= 8) { status('Enable up to 8 reviewers. Disable one first.'); return; }
    a.enabled = !a.enabled; mark(); render(); focusAction('agent', a.uiKey);
  }
  function move(key, delta) {
    if (busy) return;
    const active = state.models.filter(m => m.enabled), index = active.findIndex(m => m.uiKey === key), target = active[index + delta];
    if (!target) return;
    const from = state.models.findIndex(m => m.uiKey === key), to = state.models.indexOf(target);
    const [m] = state.models.splice(from, 1); state.models.splice(to, 0, m);
    mark(); render();
    [...$('workflow-chain').children].find(node => node.dataset.modelKey === key)?.querySelector('.workflow-model-edit')?.focus({preventScroll:true});
    status(`${m.name} moved to position ${index + delta + 1}. Unsaved changes.`);
  }
  function render() {
    const previous = positions();
    const previousBranches = new Map([...$('workflow-reviewers').children].map(node => [node.dataset.agentKey, node.getBoundingClientRect()]));
    const branches = new Map([...$('workflow-reviewers').children].map(node => [node.dataset.agentKey, node]));
    $('model-library').replaceChildren(); $('workflow-chain').replaceChildren(); $('reviewer-library').replaceChildren();
    const enabledKeys = new Set(state.agents.filter(a => a.enabled).map(a => a.uiKey));
    for (const node of [...$('workflow-reviewers').children]) if (!enabledKeys.has(node.dataset.agentKey)) node.remove();
    const active = state.models.filter(m => m.enabled); let index = 0;
    for (const m of state.models) {
      const item = el('div', '', 'workflow-library-item'); item.dataset.modelKey = m.uiKey; item.draggable = true;
      item.addEventListener('dragstart', event => startDrag(event, m, item));
      item.append(button(m.name || 'New model', `Edit ${m.name || 'new model'}`, () => select('model', m.uiKey, true), 'workflow-item-name'), el('code', m.model || 'Set model ID'));
      const action = button(m.enabled ? 'Added' : 'Add', `${m.enabled ? 'Disable' : 'Add'} ${m.name}`, () => toggleModel(m), 'text-button workflow-item-action'); action.setAttribute('aria-pressed', String(m.enabled)); item.append(action);
      $('model-library').append(item);
      if (!m.enabled) continue;
      const position = index++;
      const node = el('li', '', 'workflow-node'); node.draggable = true; node.dataset.modelKey = m.uiKey;
      node.addEventListener('dragstart', event => startDrag(event, m, node));
      const handle = el('span', '⋮⋮', 'workflow-drag-handle'); handle.setAttribute('aria-hidden', 'true');
      const edit = button(m.name || 'New model', `Configure ${m.name || 'new model'}`, () => select('model', m.uiKey, true), 'text-button workflow-model-edit'); edit.append(el('span', position === 0 ? 'PRIMARY' : `FALLBACK ${position}`, 'workflow-model-position'));
      node.append(handle, edit, el('code', m.model || 'Set model ID'));
      const controls = el('div', '', 'workflow-node-controls');
      const up = button('↑', `Move ${m.name} up`, () => move(m.uiKey, -1)); up.disabled = position === 0;
      const down = button('↓', `Move ${m.name} down`, () => move(m.uiKey, 1)); down.disabled = position === active.length - 1;
      controls.append(up, down, button('−', `Remove ${m.name} from order`, () => toggleModel(m))); node.append(controls); $('workflow-chain').append(node);
    }
    if (!active.length) $('workflow-chain').append(el('li', 'Add a model from the library.', 'workflow-empty'));
    let branchIndex = 0;
    for (const a of state.agents) {
      const item = el('div', '', 'workflow-library-item'); item.dataset.agentKey = a.uiKey;
      item.append(button(a.name || 'New reviewer', `Edit ${a.name || 'new reviewer'}`, () => select('agent', a.uiKey, true), 'workflow-item-name'), el('code', builtins.has(a.key) ? 'Built-in specialist' : 'Custom specialist'));
      const toggle = button(a.enabled ? 'On' : 'Off', `${a.enabled ? 'Disable' : 'Enable'} ${a.name}`, () => toggleAgent(a), 'text-button workflow-item-action'); toggle.setAttribute('aria-pressed', String(a.enabled)); item.append(toggle); $('reviewer-library').append(item);
      if (a.enabled) {
        let branch = branches.get(a.uiKey);
        if (!branch) { branch = button('', '', () => select('agent', a.uiKey, true), 'workflow-branch'); branch.dataset.agentKey = a.uiKey; }
        const nextBranch = $('workflow-reviewers').children[branchIndex++];
        if (nextBranch !== branch) $('workflow-reviewers').insertBefore(branch, nextBranch || null);
        branch.textContent = a.name || 'New reviewer'; branch.setAttribute('aria-label', `Configure ${a.name || 'new reviewer'}`);
      }
    }
    const count = state.agents.filter(a => a.enabled).length;
    $('reviewer-count').textContent = `${count} ENABLED`;
    if (!count) $('workflow-reviewers').append(el('p', 'Enable a reviewer from the library.', 'workflow-empty'));
    $('reviewer-new').disabled = state.agents.length >= 12;
    $('workflow-version').textContent = `WORKFLOW / ${state.version.slice(0, 8)}`;
    highlight(); animateNodes(previous);
    if (!reducedMotion.matches) for (const node of $('workflow-reviewers').children) {
      const before = previousBranches.get(node.dataset.agentKey); if (!before) continue;
      const after = node.getBoundingClientRect(), dx = before.left - after.left, dy = before.top - after.top;
      if (Math.abs(dx) + Math.abs(dy) > 1) node.animate([{transform:`translate(${dx}px, ${dy}px)`}, {transform:'translate(0, 0)'}], {duration:180, easing:'ease-out'});
    }
  }
  function clearDrag() {
    dragSource?.classList.remove('is-dragging'); dropMarker?.remove(); $('workflow-chain').classList.remove('is-drop-target'); document.body.classList.remove('workflow-dragging');
    dragged = null; dragSource = null; dropMarker = null; dropBefore = null;
  }
  function startDrag(event, model, source) {
    if (busy || event.target.closest('button,input,select,textarea')) { event.preventDefault(); return; }
    clearDrag(); dragged = model.uiKey; dragSource = source;
    event.dataTransfer.setData('text/plain', model.uiKey); event.dataTransfer.effectAllowed = 'move';
    dropMarker = el('li', 'Place model here', 'workflow-drop-marker'); dropMarker.setAttribute('aria-hidden', 'true');
    document.body.classList.add('workflow-dragging'); requestAnimationFrame(() => dragSource?.classList.add('is-dragging'));
    status(`Moving ${model.name}. Drop into model order; Escape cancels.`);
  }
  function showDrop(event) {
    if (!dragged || busy) return;
    event.preventDefault(); event.dataTransfer.dropEffect = 'move';
    const chain = $('workflow-chain'); chain.classList.add('is-drop-target');
    if (dropMarker.parentNode === chain) { const rect = dropMarker.getBoundingClientRect(); if (event.clientY >= rect.top && event.clientY <= rect.bottom) return; }
    const nodes = [...chain.querySelectorAll('.workflow-node')].filter(node => node.dataset.modelKey !== dragged);
    // Compare viewport coordinates to viewport coordinates, including when the page is scrolled.
    const target = nodes.find(node => { const rect = node.getBoundingClientRect(); return event.clientY < rect.top + rect.height / 2; });
    dropBefore = target?.dataset.modelKey || null;
    if (dropMarker.nextElementSibling !== target || dropMarker.parentNode !== chain) chain.insertBefore(dropMarker, target || null);
  }
  $('workflow-chain').addEventListener('dragover', showDrop);
  $('workflow-chain').addEventListener('dragleave', event => { if (!$('workflow-chain').contains(event.relatedTarget)) { dropMarker?.remove(); $('workflow-chain').classList.remove('is-drop-target'); } });
  $('workflow-chain').addEventListener('drop', event => {
    if (!dragged || busy) return;
    event.preventDefault(); showDrop(event);
    const m = state.models.find(row => row.uiKey === dragged); if (!m) { clearDrag(); return; }
    state.models = state.models.filter(row => row !== m);
    const index = state.models.findIndex(row => row.uiKey === dropBefore);
    m.enabled = true; state.models.splice(index < 0 ? state.models.length : index, 0, m);
    clearDrag(); mark(); render();
  });
  document.addEventListener('dragend', () => { clearDrag(); status(dirty ? 'Unsaved changes · new jobs only' : 'Saved workflow · ready for new jobs'); });
  document.addEventListener('keydown', event => { if (event.key === 'Escape' && dragged) clearDrag(); });
  async function request(url, data) {
    const response = await fetch(url, {method:data ? 'POST' : 'GET', headers:data ? {'Content-Type':'application/json', 'X-CSRFToken':csrf} : {}, body:data ? JSON.stringify(data) : undefined, cache:'no-store'});
    let result; try { result = await response.json(); } catch { throw Error('Session expired or service unavailable. Reload and sign in again.'); }
    if (!response.ok) throw Error(result.error || 'Request failed.'); return result;
  }
  function hydrate(data) {
    const selectedIndex = selected ? (selected.type === 'model' ? state.models : state.agents).findIndex(row => row.uiKey === selected.key) : -1;
    const selectedType = selected?.type;
    state = data; state.agents ||= []; state.probes ||= [];
    state.models.forEach(m => { m.uiKey = `model-${m.id}`; }); state.agents.forEach(a => { a.uiKey = a.key; });
    selected = null; render();
    if (selectedIndex >= 0) { const row = (selectedType === 'model' ? state.models : state.agents)[selectedIndex]; select(selectedType, row?.uiKey); }
  }
  $('model-new').onclick = () => {
    const m = {id:null, uiKey:`new-model-${++nextKey}`, name:'New model', kind:'codex_cli', model:'', effort:'', enabled:false};
    state.models.push(m); mark(); render(); select('model', m.uiKey, true);
  };
  $('model-discard').onclick = () => {
    const m = selectedModel(); if (!m || m.id) return;
    if (m.enabled && state.models.filter(row => row.enabled).length === 1) { status('Keep at least one model in the order.'); return; }
    state.models = state.models.filter(row => row !== m); select(null, null); mark(); render(); $('model-new').focus();
  };
  $('reviewer-new').onclick = () => {
    if (state.agents.length >= 12) return;
    const a = {key:null, uiKey:`new-agent-${++nextKey}`, name:'New reviewer', instructions:'', enabled:state.agents.filter(row => row.enabled).length < 8};
    state.agents.push(a); mark(); render(); select('agent', a.uiKey, true);
  };
  $('model-close').onclick = () => { const type = selected?.type; select(null, null); $(type === 'agent' ? 'reviewers-tab' : 'models-tab').focus({preventScroll:true}); };
  $('reviewer-remove').onclick = () => {
    const a = selectedAgent(); if (!a || builtins.has(a.key)) return;
    if (a.enabled && state.agents.filter(row => row.enabled).length === 1) { status('Keep at least one reviewer enabled.'); return; }
    state.agents = state.agents.filter(row => row !== a); select(null, null); mark(); render(); $('reviewer-new').focus();
  };
  function modelInput() {
    const m = selectedModel(); if (!m) return;
    Object.assign(m, {name:$('model-name').value, kind:$('model-kind').value, model:$('model-id').value, effort:$('model-kind').value === 'codex_cli' ? $('model-effort').value : ''});
    $('model-effort').disabled = m.kind !== 'codex_cli'; if ($('model-effort').disabled) $('model-effort').value = '';
    mark(); render();
  }
  $('model-settings').addEventListener('input', modelInput);
  $('model-kind').addEventListener('change', modelInput);
  $('model-effort').addEventListener('change', modelInput);
  $('reviewer-settings').addEventListener('input', () => { const a = selectedAgent(); if (!a || builtins.has(a.key)) return; a.name = $('reviewer-name').value; a.instructions = $('reviewer-instructions').value; mark(); render(); });
  $('model-settings').onsubmit = $('reviewer-settings').onsubmit = event => { event.preventDefault(); status('Draft updated. Save changes to apply.'); };
  for (const type of ['models', 'reviewers']) {
    $(`${type}-tab`).onclick = () => { select(null, null); tab(type); };
    $(`${type}-tab`).onkeydown = event => { if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) { event.preventDefault(); select(null, null); tab(event.key === 'Home' ? 'models' : event.key === 'End' ? 'reviewers' : type === 'models' ? 'reviewers' : 'models', true); } };
  }
  function validate() {
    if (!state.models.some(m => m.enabled)) { status('Add at least one model to the order.'); return false; }
    for (const m of state.models) {
      if (!m.name.trim() || !/^[A-Za-z0-9][A-Za-z0-9_.:\-]{0,119}$/.test(m.model)) { select('model', m.uiKey, true); $('model-settings').reportValidity(); status('Set a display name and exact model ID for every model.'); return false; }
    }
    const count = state.agents.filter(a => a.enabled).length;
    if (count < 1 || count > 8 || state.agents.length > 12) { status('Use 1–8 enabled reviewers, up to 12 in the library.'); return false; }
    for (const a of state.agents) {
      if (!a.name.trim() || (!builtins.has(a.key) && !a.instructions.trim())) { select('agent', a.uiKey, true); $('reviewer-settings').reportValidity(); status('Set a name and instructions for each custom reviewer.'); return false; }
    }
    return true;
  }
  $('workflow-form').onsubmit = async event => {
    event.preventDefault(); if (busy || !state || !dirty || !validate()) return;
    busy = true; $('workflow-save').disabled = true; $('workflow-editor').setAttribute('aria-busy', 'true');
    // Native disabled controls keep the schematic readable and leave navigation accessible.
    const controls = [...$('workflow-editor').querySelectorAll('button,input,select,textarea')].map(node => [node, node.disabled]);
    controls.forEach(([node]) => { node.disabled = true; });
    const payload = {version:state.version, models:state.models.map(({uiKey, ...m}) => m), agents:state.agents.map(({uiKey, ...a}) => a)};
    try { const data = await request('/workflow/state/', payload); controls.forEach(([node, disabled]) => { node.disabled = disabled; }); controls.length = 0; dirty = false; hydrate(data); document.querySelector('.workflow-toolbar').dataset.dirty = 'false'; status('Saved · new jobs will use this workflow.'); }
    catch (error) { status(`${error.message} Draft preserved.`); }
    finally { busy = false; controls.forEach(([node, disabled]) => { node.disabled = disabled; }); $('workflow-editor').removeAttribute('aria-busy'); $('workflow-save').disabled = !dirty; probeStatus(); }
  };
  $('model-test').onclick = async () => {
    const m = selectedModel(); if (!m || dirty || busy) return; $('model-test').disabled = true;
    try { const result = await request(`/workflow/probe/${m.id}/`, {}); $('model-probe').textContent = result.message; }
    catch (error) { $('model-probe').textContent = error.message; }
  };
  window.addEventListener('beforeunload', event => { if (dirty) { event.preventDefault(); event.returnValue = ''; } });
  request('/workflow/state/').then(data => { hydrate(data); $('workflow-editor').hidden = false; status('Saved workflow · ready for new jobs'); }).catch(error => status(error.message));
  setInterval(async () => { if (!state || document.hidden || busy) return; try { const data = await request('/workflow/state/'); state.probes = data.probes || []; probeStatus(); } catch { /* Never replace a draft during polling or a network interruption. */ } }, 5000);
})();
