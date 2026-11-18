const $ = id => document.getElementById(id);
export function initPlatformSettings(api) {
  const form = $('platform-form'), username = $('platform-username'), password = $('platform-password');
  const clear = $('platform-clear-password'), status = $('platform-status');
  const paths = Array.from(form.querySelectorAll('[data-resource-key]'));
  let saved = null, busy = false;
  function controls() {
    const disabled = busy || !saved?.local_mcp;
    for (const node of form.querySelectorAll('input,button')) node.disabled = disabled;
    $('platform-test').disabled = disabled || !saved?.configured || username.value.trim() !== saved.username || !!password.value || clear.checked || paths.some(node => node.value.trim() !== (saved.resource_paths?.[node.dataset.resourceKey] || ''));
    form.setAttribute('aria-busy', String(busy));
  }
  function render(value) {
    saved = value; username.value = value.username; password.value = ''; clear.checked = false;
    for (const node of paths) node.value = value.resource_paths?.[node.dataset.resourceKey] || '';
    password.placeholder = value.password_set ? 'Пароль сохранён' : 'Введите пароль IACPaaS';
    status.textContent = !value.local_mcp ? 'Для удалённого MCP вход и ресурсы настраиваются на его сервере.' : value.configured ? `Вход и пути ресурсов ${value.source === 'environment' ? 'заданы по умолчанию или на сервере' : 'сохранены'}. Доступ можно проверить.` : 'Укажите логин и пароль платформы.';
  }
  $('settings-open').addEventListener('click', async () => {
    busy = true; controls();
    try { render(await api.platformSettings()); }
    catch (error) { status.textContent = error.message; }
    finally { busy = false; controls(); }
  });
  form.addEventListener('input', controls);
  form.addEventListener('change', controls);
  form.addEventListener('submit', async event => {
    event.preventDefault(); if (busy) return;
    busy = true; controls(); status.textContent = 'Сохраняем вход IACPaaS…';
    try { render(await api.savePlatformSettings({username:username.value, password:password.value, clear_password:clear.checked,
      resource_paths:Object.fromEntries(paths.map(node => [node.dataset.resourceKey, node.value.trim()]))})); }
    catch (error) { status.textContent = error.message; }
    finally { busy = false; controls(); }
  });
  $('platform-default-paths').addEventListener('click', () => {
    if (busy || !saved?.local_mcp) return;
    for (const node of paths) node.value = saved.resource_fields?.find(field => field.key === node.dataset.resourceKey)?.default_path || '';
    status.textContent = 'Типовые пути заполнены. Проверьте расположение ресурсов и сохраните настройки.';
    controls();
  });
  $('platform-test').addEventListener('click', async () => {
    if (busy) return;
    busy = true; controls(); status.textContent = 'Проверяем вход и чтение ресурса…';
    try { status.textContent = (await api.testPlatformSettings()).message; }
    catch (error) { status.textContent = error.message; }
    finally { busy = false; controls(); }
  });
  $('settings-dialog').addEventListener('close', () => { password.value = ''; clear.checked = false; });
}
