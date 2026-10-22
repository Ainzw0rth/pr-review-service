(() => {
  const form = document.querySelector('[data-settings-save]');
  let dirty = false, submitting = false;
  const pendingButtons = new Map();
  const fields = form ? [...document.querySelectorAll('input,select,textarea')].filter(field => field.form === form && field.type !== 'hidden') : [];
  const original = fields.map(field => field.type === 'checkbox' ? field.checked : field.value);
  function update() {
    document.querySelectorAll('.settings-repository').forEach(repository => {
      const approve = repository.querySelector('[name^="auto_approve_"]');
      const merge = repository.querySelector('[name^="auto_merge_"]');
      if (!approve || !merge) return;
      if (!approve.checked) merge.checked = false;
      merge.disabled = !approve.checked;
      repository.querySelector('.settings-policy-summary').textContent = merge.checked ? 'Auto-approve & merge' : approve.checked ? 'Auto-approve' : 'Manual approval';
    });
    dirty = fields.some((field, index) => (field.type === 'checkbox' ? field.checked : field.value) !== original[index]);
    if (form) form.dataset.dirty = String(dirty);
    const state = document.querySelector('.settings-save-state');
    if (state) state.textContent = dirty ? 'Unsaved changes' : 'No unsaved changes';
    document.querySelectorAll('[data-related-unlimited]').forEach(toggle => {
      const input = document.querySelector(`[data-related-limit="${toggle.dataset.relatedUnlimited}"]`);
      if (input) input.disabled = toggle.checked;
    });
  }
  fields.forEach(field => field.addEventListener('input', update));
  update();
  document.querySelectorAll('[data-settings-action], [data-settings-save]').forEach(action => action.addEventListener('submit', () => {
    if (action !== form && dirty) return;
    submitting = true;
    action.setAttribute('aria-busy', 'true');
    const button = action.querySelector('button[type=submit]');
    if (button) { pendingButtons.set(button, [...button.childNodes]); button.disabled = true; button.textContent = 'Saving…'; }
  }));
  window.addEventListener('beforeunload', event => { if (dirty && !submitting) { event.preventDefault(); event.returnValue = ''; } });
  window.addEventListener('pageshow', () => {
    submitting = false;
    document.querySelectorAll('[aria-busy=true]').forEach(action => action.removeAttribute('aria-busy'));
    pendingButtons.forEach((children, button) => { button.disabled = false; button.replaceChildren(...children); });
    pendingButtons.clear();
  });
})();
