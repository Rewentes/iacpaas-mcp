(() => {
  const system = window.matchMedia('(prefers-color-scheme: dark)');
  let preference = 'system';
  try { preference = localStorage.getItem('iacpaas.theme') || 'system'; } catch {}
  if (!['light', 'dark', 'system'].includes(preference)) preference = 'system';
  function apply() {
    const effective = preference === 'system' ? (system.matches ? 'dark' : 'light') : preference;
    document.documentElement.dataset.theme = effective;
    document.documentElement.style.colorScheme = effective;
    // Apply the pinned Ring UI theme before its stylesheet is painted.
    document.documentElement.classList.toggle('ring-variables-light', effective === 'light');
    document.documentElement.classList.toggle('ring-variables_dark-dark', effective === 'dark');
    document.documentElement.classList.toggle('ring-ui-theme-dark', effective === 'dark');
    window.dispatchEvent(new Event('iacpaas-theme'));
    const select = document.getElementById('theme-select');
    if (select) select.value = preference;
    const toggle = document.getElementById('theme-toggle');
    if (toggle) {
      const next = effective === 'dark' ? 'светлую' : 'тёмную';
      toggle.setAttribute('aria-label', `Включить ${next} тему`);
      toggle.title = `Включить ${next} тему`;
      toggle.dataset.theme = effective;
    }
  }
  window.iacpaasTheme = {
    set(value) {
      if (!['light', 'dark', 'system'].includes(value)) return;
      preference = value;
      try { localStorage.setItem('iacpaas.theme', value); } catch {}
      apply();
    },
  };
  system.addEventListener('change', apply);
  document.addEventListener('DOMContentLoaded', () => {
    apply();
    document.getElementById('theme-select').addEventListener('change', event => window.iacpaasTheme.set(event.target.value));
    document.getElementById('theme-toggle').addEventListener('click', () => window.iacpaasTheme.set(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark'));
  });
  apply();
})();
