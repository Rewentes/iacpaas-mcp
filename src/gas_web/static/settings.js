const $ = id => document.getElementById(id);
function option(id, name = id) {
  const node = document.createElement('option');
  node.value = id;
  node.textContent = name;
  return node;
}

export function initSettings(api) {
  let saved, busy = false, catalogVersion = 0;
  let auth = null, authVersion = 0, pollTimer = null, polls = 0, pollDeadline = 0;
  let codexTestedModel = null;
  const catalogs = new Map();
  const select = $('model-select');
  const dialog = $('settings-dialog');
  const provider = $('settings-provider');
  const model = $('settings-model');
  const key = $('settings-key');
  const clearKey = $('settings-clear-key');
  const notice = text => { $('model-notice').textContent = text; };
  function errorText(error = null) {
    $('settings-error').hidden = !error;
    $('settings-error').textContent = error?.message || '';
  }
  function dirty() {
    return !saved || provider.value !== saved.provider || model.value.trim() !== saved.model || !!key.value || clearKey.checked;
  }
  function controls() {
    select.disabled = busy || !saved;
    for (const id of ['settings-provider', 'settings-model', 'settings-key', 'settings-clear-key', 'settings-save', 'models-refresh']) $(id).disabled = busy;
    key.disabled = busy || provider.value === 'codex';
    clearKey.disabled = busy || provider.value === 'codex';
    $('settings-test').disabled = busy || dirty();
    $('codex-login').disabled = busy || !auth?.installed || auth.connected;
    $('codex-logout').disabled = busy || !(auth?.connected || auth?.login_pending);
    $('settings-form').setAttribute('aria-busy', String(busy));
  }
  function status() {
    if (!saved || provider.value !== saved.provider) {
      $('settings-status').textContent = 'Сохраните настройки, чтобы узнать статус подключения.';
      return;
    }
    if (dirty()) {
      $('settings-status').textContent = 'Изменения не сохранены. Сохраните их перед проверкой.';
      return;
    }
    // Codex "connected" reports authentication, not a model inference.
    const tested = saved.provider === 'codex' ? codexTestedModel === saved.model && auth?.connected : saved.connection_status === 'connected';
    const keyStatus = saved.provider === 'codex' ? '' : `${saved.api_key_set ? 'API-ключ сохранён.' : 'API-ключ не задан.'} `;
    $('settings-status').textContent = keyStatus + (tested ? 'Проверка модели пройдена.' : 'Доступ к модели не проверен.');
    if (saved.connection_status === 'error' || saved.connection_status === 'unavailable') $('settings-status').textContent += ' Последняя проверка не удалась.';
  }
  function render(profile) {
    saved = profile;
    const items = catalogs.get(profile.provider) || [];
    select.replaceChildren(option(profile.model), ...items.filter(item => item.id !== profile.model).map(item => option(item.id, item.name || item.id)));
    select.value = profile.model;
    $('model').textContent = `Модель: ${profile.model}`;
    $('model').title = profile.model;
    controls();
  }
  async function catalog(id) {
    const version = ++catalogVersion;
    if (dialog.open) $('catalog-status').textContent = 'Загрузка моделей…';
    try {
      const data = await api.models(id);
      catalogs.set(id, data.items);
      if (saved?.provider === id) render(saved);
      if (version !== catalogVersion || !dialog.open || provider.value !== id) return;
      $('settings-models').replaceChildren(...data.items.map(item => option(item.id, item.name || item.id)));
      $('catalog-status').textContent = `Моделей в списке: ${data.items.length}.`;
    } catch (error) {
      if (version !== catalogVersion) return;
      const text = `${error.message} Можно ввести ID модели вручную.`;
      if (dialog.open) $('catalog-status').textContent = text;
      else notice(text);
    }
  }
  async function load() {
    try {
      render(await api.settings());
    } catch (error) { notice(error.message); }
  }
  function fill() {
    provider.value = saved.provider;
    model.value = saved.model;
    key.value = ''; clearKey.checked = false;
    mode(); status(); controls();
  }
  function clearDevice() {
    $('codex-device').hidden = true;
    $('codex-code').textContent = '';
    $('codex-link').removeAttribute('href');
  }
  function stopPolling() {
    ++authVersion;
    clearTimeout(pollTimer); pollTimer = null;
    polls = 0; pollDeadline = Date.now() + 180000;
  }
  function mode() {
    const subscription = provider.value === 'codex';
    $('api-key-fields').hidden = subscription;
    $('codex-auth').hidden = !subscription;
  }
  function renderAuth(value) {
    if (!value.connected) codexTestedModel = null;
    auth = value;
    $('codex-status').textContent = !value.installed ? 'Codex не установлен на сервере.' : value.connected ? 'Вход через ChatGPT выполнен.' : value.login_pending ? 'Ожидаем подтверждение входа.' : 'Вход через ChatGPT не выполнен.';
    if (!value.login_pending) clearDevice();
    status(); controls();
  }
  function schedulePoll(version) {
    if (version !== authVersion || !dialog.open || provider.value !== 'codex' || !auth?.login_pending) return;
    if (polls >= 90 || Date.now() >= pollDeadline) {
      clearDevice();
      $('codex-status').textContent = 'Ожидание завершено. Запросите код входа ещё раз.';
      return;
    }
    pollTimer = setTimeout(() => { ++polls; refreshAuth(version, true); }, 2000);
  }
  async function refreshAuth(version, polling = false) {
    try {
      const value = await api.codexStatus();
      if (version !== authVersion || !dialog.open || provider.value !== 'codex') return;
      renderAuth(value);
      if (value.connected && polling) catalog('codex');
      schedulePoll(version);
    } catch (error) {
      if (version !== authVersion || !dialog.open) return;
      $('codex-status').textContent = 'Не удалось проверить вход. Откройте настройки ещё раз.';
      errorText(error);
    }
  }
  async function persist(body) {
    await api.saveSettings(body);
    key.value = ''; clearKey.checked = false;
    const profile = await api.settings();
    if (profile.provider !== body.provider || profile.model !== body.model ||
        (body.clear_api_key && profile.api_key_set) || (body.api_key && !profile.api_key_set)) {
      throw new Error('Сервер не подтвердил сохранение настроек. Обновите страницу.');
    }
    render(profile);
  }
  select.addEventListener('change', async () => {
    if (!saved || busy) return;
    busy = true; controls();
    try {
      await persist({provider: saved.provider, model: select.value});
      notice('Модель сохранена.');
    } catch (error) { select.value = saved.model; notice(error.message); }
    finally { busy = false; controls(); }
  });
  $('settings-open').addEventListener('click', async () => {
    dialog.showModal(); errorText(); notice('');
    $('key-warning').hidden = location.protocol !== 'http:';
    busy = true; controls();
    try { render(await api.settings()); fill(); }
    catch (error) { errorText(error); }
    finally { busy = false; controls(); }
    if (saved && dialog.open) {
      catalog(provider.value);
      if (provider.value === 'codex') { stopPolling(); refreshAuth(authVersion); }
    }
  });
  $('settings-close').addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => {
    key.value = ''; clearKey.checked = false; ++catalogVersion;
    stopPolling(); clearDevice();
    const target = $('sidebar').inert ? $('nav-toggle') : $('settings-open');
    target.focus({preventScroll:true});
  });
  provider.addEventListener('change', () => {
    key.value = ''; clearKey.checked = false; model.value = provider.value === saved?.provider ? saved.model : '';
    stopPolling(); clearDevice(); mode();
    $('settings-models').replaceChildren();
    errorText(); status(); controls(); catalog(provider.value);
    if (provider.value === 'codex') refreshAuth(authVersion);
  });
  for (const input of [model, key, clearKey]) input.addEventListener('input', () => { status(); controls(); });
  $('models-refresh').addEventListener('click', () => catalog(provider.value));
  $('codex-login').addEventListener('click', async () => {
    if (busy || provider.value !== 'codex' || !auth?.installed || auth.connected) return;
    stopPolling(); clearDevice(); codexTestedModel = null;
    const version = authVersion;
    busy = true; controls(); errorText();
    try {
      const login = await api.codexLogin();
      if (version !== authVersion || !dialog.open) return;
      let url;
      try { url = new URL(login.verification_url); } catch { /* Invalid addresses are rejected below. */ }
      if (!url || url.origin !== 'https://auth.openai.com' || url.username || url.password) throw new Error('Сервер вернул недопустимый адрес входа ChatGPT.');
      if (typeof login.user_code !== 'string' || !login.user_code || login.user_code.length > 128) throw new Error('Сервер не выдал код входа. Повторите попытку.');
      $('codex-code').textContent = login.user_code;
      $('codex-link').href = url.href;
      $('codex-device').hidden = false;
      renderAuth({installed: true, connected: false, login_pending: true});
      schedulePoll(version);
    } catch (error) { if (version === authVersion && dialog.open) errorText(error); }
    finally { busy = false; controls(); }
  });
  $('codex-logout').addEventListener('click', async () => {
    if (busy) return;
    stopPolling(); clearDevice(); codexTestedModel = null;
    const version = authVersion;
    busy = true; controls(); errorText();
    try {
      await api.codexLogout();
      render(await api.settings());
      status();
      await refreshAuth(version);
    } catch (error) { if (dialog.open) errorText(error); }
    finally { busy = false; controls(); }
  });
  $('settings-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (busy) return;
    const body = {provider: provider.value, model: model.value.trim()};
    if (!body.model) { errorText(new Error('Введите ID модели.')); model.focus(); return; }
    if (body.provider !== 'codex') {
      if (clearKey.checked) body.clear_api_key = true;
      else if (key.value.trim()) body.api_key = key.value.trim();
    }
    busy = true; controls(); errorText();
    try {
      await persist(body);
      status(); notice('Настройки сохранены.');
      catalog(saved.provider);
    } catch (error) { errorText(error); }
    finally { busy = false; controls(); }
  });
  $('settings-test').addEventListener('click', async () => {
    if (busy || dirty()) return;
    busy = true; controls(); errorText();
    $('settings-status').textContent = 'Проверяем модель…';
    try {
      const result = await api.testSettings();
      if (result.ok !== true) throw new Error('Сервер не подтвердил ответ модели. Повторите проверку.');
      if (saved.provider === 'codex') codexTestedModel = saved.model;
      render(await api.settings()); status();
    } catch (error) {
      $('settings-status').textContent = 'Проверка модели не пройдена.';
      if (saved?.provider === 'codex') codexTestedModel = null;
      errorText(error);
    } finally { busy = false; controls(); }
  });
  load();
}
