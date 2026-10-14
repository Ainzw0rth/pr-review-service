(() => {
  const reduce = matchMedia('(prefers-reduced-motion: reduce)');
  // Only elements present at navigation get an entrance. Polling replacements do not.
  if (!reduce.matches) {
    document.querySelectorAll('main > section, main > .panel, main > .auth-card, #content-main > .module').forEach((element, index) => {
      element.style.setProperty('--entry-delay', `${Math.min(index, 4) * 45}ms`);
      element.classList.add('motion-enter');
      element.addEventListener('animationend', () => element.classList.remove('motion-enter'), {once:true});
    });
  }
  // Initials remain available if GitHub's avatar host is unreachable.
  document.addEventListener('error', event => {
    if (event.target.matches?.('.avatar img')) event.target.hidden = true;
  }, true);
  document.querySelectorAll('.avatar img').forEach(img => {
    if (img.complete && !img.naturalWidth) img.hidden = true;
  });
  document.addEventListener('click', event => {
    const summary = event.target.closest('summary');
    if (!summary || reduce.matches) return;
    requestAnimationFrame(() => {
      const details = summary.parentElement;
      if (!details?.open) return;
      for (const child of details.children) {
        if (child.tagName !== 'SUMMARY') child.animate(
          [{opacity:0, transform:'translateY(-3px)'}, {opacity:1, transform:'translateY(0)'}],
          {duration:180, easing:'ease-out'},
        );
      }
    });
  });
})();
