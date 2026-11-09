export class ApiError extends Error {
  constructor(message, status = 0, code = 'network_error', retryable = true) {
    super(message);
    this.status = status;
    this.code = code;
    this.retryable = retryable;
  }
}

export function createSubmission(content) {
  return Object.freeze({content, request_id: globalThis.crypto.randomUUID()});
}

export class Api {
  constructor(fetcher = globalThis.fetch.bind(globalThis)) {
    this.fetcher = fetcher;
    this.inflight = new Map();
  }

  async request(path, options = {}) {
    let response;
    try {
      response = await this.fetcher(`/api${path}`, {credentials: 'same-origin', ...options});
    } catch {
      throw new ApiError('Связь с сервером прервана. Результат отправки может быть неизвестен.');
    }
    let data;
    try { data = await response.json(); } catch {
      throw new ApiError('Сервер вернул неожиданный ответ.', response.status, 'invalid_response', true);
    }
    if (!response.ok) {
      const error = data?.error;
      throw new ApiError(typeof error?.message === 'string' ? error.message : 'Запрос не выполнен.', response.status,
        error?.code || 'http_error', error?.retryable === true);
    }
    return data;
  }

  bootstrap() {
    if (!this.identity) {
      this.identity = this.request('/me').catch(error => { this.identity = null; throw error; });
    }
    return this.identity;
  }

  async post(path, body) {
    const identity = await this.bootstrap();
    if (!identity.csrf_token) throw new ApiError('Сервер не выдал защитный токен. Обновите страницу.', 0, 'csrf_missing', false);
    return this.request(path, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': identity.csrf_token}, body: JSON.stringify(body)});
  }

  async streamPost(path, body, onEvent) {
    const identity = await this.bootstrap();
    if (!identity.csrf_token) throw new ApiError('Обновите страницу для отправки запроса.', 0, 'csrf_missing', false);
    let response;
    try {
      response = await this.fetcher(`/api${path}`, {
        method:'POST', credentials:'same-origin',
        headers:{'Content-Type':'application/json', 'X-CSRF-Token':identity.csrf_token, Accept:'text/event-stream'},
        body:JSON.stringify(body),
      });
    } catch {
      throw new ApiError('Связь прервана. Проверьте историю перед повтором запроса.');
    }
    const unwrap = (data, status) => {
      if (status >= 400) throw new ApiError(data?.error?.message || 'Запрос не выполнен.', status, data?.error?.code || 'http_error', data?.error?.retryable === true);
      return data;
    };
    if (!response.headers.get('content-type')?.includes('text/event-stream')) {
      return unwrap(await response.json(), response.status);
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let pending = '';
    try {
      while (true) {
        const {value, done} = await reader.read();
        pending += decoder.decode(value, {stream:!done});
        let boundary;
        while ((boundary = pending.indexOf('\n\n')) !== -1) {
          const frame = pending.slice(0, boundary); pending = pending.slice(boundary + 2);
          const kind = frame.split('\n').find(line => line.startsWith('event:'))?.slice(6).trim();
          const data = frame.split('\n').filter(line => line.startsWith('data:')).map(line => line.slice(5).trimStart()).join('\n');
          if (!data) continue;
          const payload = JSON.parse(data);
          if (kind === 'done') return unwrap(payload.result, payload.status);
          if (kind === 'progress') onEvent(payload);
        }
        if (pending.length > 262144) throw new ApiError('Слишком большое сообщение сервера.', 0, 'invalid_response', false);
        if (done) break;
      }
      throw new ApiError('Поток ответа прерван. Проверьте историю диалога.');
    } finally {
      await reader.cancel().catch(() => {});
      reader.releaseLock();
    }
  }

  settings() { return this.request('/settings'); }
  models(provider) { return this.request(`/models?provider=${encodeURIComponent(provider)}`); }
  saveSettings(settings) { return this.post('/settings', settings); }
  testSettings() { return this.post('/settings/test', {}); }
  codexStatus() { return this.request('/codex/status'); }
  codexLogin() { return this.post('/codex/login/start', {}); }
  codexLogout() { return this.post('/codex/logout', {}); }
  manage(id, action) { return this.post(`/conversations/${encodeURIComponent(id)}/manage`, {action}); }
  platformSettings() { return this.request('/platform/settings'); }
  savePlatformSettings(body) { return this.post('/platform/settings', body); }
  testPlatformSettings() { return this.post('/platform/settings/test', {}); }
  list() { return this.request('/conversations'); }
  history(id, before = null) {
    const cursor = before === null ? '' : `&before=${encodeURIComponent(before)}`;
    return this.request(`/conversations/${encodeURIComponent(id)}/messages?limit=50${cursor}`);
  }
  create(title) { return this.post('/conversations', {title}); }
  revise(id, revision) { return this.post(`/conversations/${encodeURIComponent(id)}/revisions`, revision); }
  send(id, submission, onEvent = null) {
    const key = `${id}:${submission.request_id}`;
    if (!this.inflight.has(key)) {
      const path = `/conversations/${encodeURIComponent(id)}/messages`;
      const pending = (onEvent ? this.streamPost(path, submission, onEvent) : this.post(path, submission))
        .finally(() => this.inflight.delete(key));
      this.inflight.set(key, pending);
    }
    return this.inflight.get(key);
  }
}
