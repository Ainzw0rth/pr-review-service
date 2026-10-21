(() => {
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  document.querySelectorAll('[data-toast]').forEach(toast => {
    let timer, closing = false;
    const close = () => {
      if (closing) return;
      closing = true;
      clearTimeout(timer);
      if (toast.contains(document.activeElement)) {
        const target = document.querySelector('[data-bulk-retry] button, [data-retry-label]') || document.querySelector('main h1');
        if (target) { if (!target.matches('button')) target.tabIndex = -1; target.focus(); }
      }
      if (reduced.matches) { toast.remove(); return; }
      toast.animate([{opacity:1,transform:'translateY(0)'},{opacity:0,transform:'translateY(6px)'}], {duration:160,easing:'ease-out',fill:'forwards'}).finished.then(() => toast.remove());
    };
    const pause = () => clearTimeout(timer);
    const resume = () => {
      pause();
      if (toast.dataset.level !== 'error' && !toast.matches(':hover') && !toast.contains(document.activeElement)) timer = setTimeout(close, 8000);
    };
    toast.querySelector('[data-toast-dismiss]').addEventListener('click', close);
    toast.addEventListener('pointerenter', pause);
    toast.addEventListener('pointerleave', resume);
    toast.addEventListener('focusin', pause);
    toast.addEventListener('focusout', () => setTimeout(resume, 0));
    resume();
  });
})();
