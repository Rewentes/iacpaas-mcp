import {Api, createSubmission} from './api.js';
import {renderMarkdown} from './markdown.js';
import {pickStarters} from './starter_questions.js';
import {initPlatformSettings} from './platform_settings.js';
import {initSettings} from './settings.js';
import {initRingUI, enhanceButton} from './ring.js';

initRingUI();

const api = new Api();
const $ = id => document.getElementById(id);
const state = {ready: false, busy: false, conversation: null, items: [], messages: [], before: null, attempts: new Map(), drafts: new Map(), editing: null, edits: new Map(), archive: false};
let retryAction = null;
let starterConversation;

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  if (tag === 'button') enhanceButton(node);
  return node;
}
function dateLabel(value, time = false) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleString('ru-RU', time ? {hour:'2-digit', minute:'2-digit'} : {day:'numeric',month:'short'});
}
function announce(text = '') { $('activity').textContent = text; }
function showError(error, retry = null, label = 'Повторить') {
  $('error-text').textContent = error.message || 'Запрос не выполнен.';
  $('error').hidden = false;
  retryAction = retry;
  $('retry').hidden = !retry;
  $('retry').textContent = label;
  $('error').scrollIntoView({block: 'nearest'});
}
function clearError() {
  $('error').hidden = true;
  retryAction = null;
}
function controls() {
  const locked = !state.ready || state.busy;
  const archived = !!state.conversation?.archived;
  $('archive-toggle').disabled = locked;
  $('new-chat').disabled = locked;
  const pending = state.attempts.has(state.conversation?.id);
  $('prompt').disabled = locked || archived;
  $('prompt').placeholder = archived ? 'Диалог в архиве. Восстановите его через меню чата.' : 'Спросите о газах, свойствах или источниках…';
  $('prompt').readOnly = pending;
  $('send').disabled = locked || archived || pending || !$('prompt').value.trim();
  for (const button of $('conversations').querySelectorAll('button')) button.disabled = locked;
  for (const button of document.querySelectorAll('[data-prompt]')) button.disabled = locked;
  $('older').disabled = locked;
  $('retry').disabled = state.busy;
  $('activity').dataset.busy = String(state.busy);
  $('composer').setAttribute('aria-busy', String(state.busy));
  for (const button of document.querySelectorAll('[data-message-action], .editor-actions button')) button.disabled = locked || archived || pending;
  for (const input of document.querySelectorAll('.message-editor textarea')) input.disabled = locked || pending;
  resizePrompt();
}
function renderConversations() {
  const list = element('ul');
  for (const item of state.items.filter(item => !!item.archived === state.archive)) {
    const li = element('li');
    const button = element('button', undefined, 'conversation-item');
    button.type = 'button';
    button.title = item.title;
    button.append(element('span', item.title || 'Без названия', 'conversation-title'));
    const date = element('time', dateLabel(item.updated_at));
    date.dateTime = item.updated_at;
    button.append(date);
    if (item.id === state.conversation?.id) button.setAttribute('aria-current', 'page');
    button.addEventListener('click', () => openConversation(item.id));
    const menu = element('details', undefined, 'conversation-menu');
    const trigger = element('summary', '⋯');
    trigger.setAttribute('aria-label', `Действия: ${item.title}`);
    const actions = element('div', undefined, 'conversation-menu-items');
    for (const [action, label] of [[item.archived ? 'restore' : 'archive', item.archived ? 'Восстановить' : 'В архив'], ['delete', 'Удалить']]) {
      const control = element('button', label);
      control.type = 'button';
      if (action === 'delete') control.classList.add('danger');
      control.addEventListener('click', () => { menu.open = false; manageConversation(item, action); });
      actions.append(control);
    }
    menu.append(trigger, actions);
    li.append(button, menu); list.append(li);
  }
  $('conversations').replaceChildren(list.children.length ? list : element('p', state.archive ? 'Архив пуст.' : 'Здесь появятся ваши диалоги.', 'muted'));
  $('archive-toggle').textContent = state.archive ? '← К диалогам' : 'Архив';
  $('conversation-section').textContent = state.archive ? 'Архив' : 'Диалоги';
  controls();
}
async function manageConversation(item, action) {
  if (state.busy) return;
  if (action === 'delete') {
    $('delete-title').textContent = `Удалить «${item.title}»?`;
    const confirmed = await new Promise(resolve => {
      const dialog = $('delete-dialog');
      dialog.addEventListener('close', () => resolve(dialog.returnValue === 'delete'), {once:true});
      dialog.showModal();
    });
    if (!confirmed) return;
  }
  state.busy = true; controls(); clearError();
  try {
    await api.manage(item.id, action);
    if (action === 'delete') {
      state.items = state.items.filter(value => value.id !== item.id);
      state.attempts.delete(item.id); state.drafts.delete(item.id);
      if (state.conversation?.id === item.id) {
        state.conversation = null; state.messages = []; state.before = null; state.editing = null;
        $('prompt').value = ''; renderMessages(); renderContext(null);
      }
    } else {
      item.archived = action === 'archive';
      if (state.conversation?.id === item.id) {
        state.conversation.archived = item.archived;
        if (!state.messages.length) renderMessages();
      }
      announce(action === 'archive' ? 'Диалог в архиве. Его можно восстановить.' : 'Диалог восстановлен.');
    }
    renderConversations();
  } catch (error) { showError(error); }
  finally { state.busy = false; controls(); }
}
$('archive-toggle').addEventListener('click', () => {
  if (state.busy) return;
  state.archive = !state.archive; renderConversations();
});
function renderContext(context) {
  if (!context || !Number.isFinite(context.limit) || context.limit <= 0) {
    $('context').textContent = 'Контекст: пока нет данных';
    return;
  }
  const number = value => Number.isFinite(value) && value >= 0 ? value.toLocaleString('ru-RU') : '—';
  $('context').textContent = `Контекст ≈ ${number(context.estimated_input_tokens)} / ${number(context.limit)} токенов · Сжатия: ${number(context.compactions)}`;
  $('context').title = 'Приблизительная оценка входного контекста, не фактический расход токенов. Полная история сохраняется после сжатия.';
}
function sourceText(value) {
  if (value === null || value === undefined) return '—';
  return typeof value === 'object' ? JSON.stringify(value) : String(value);
}
function renderMessage(message) {
  const assistant = message.role === 'assistant';
  const article = element('article', undefined, `message ${assistant ? 'assistant' : 'user'}`);
  article.dataset.messageId = message.id;
  const header = element('div', undefined, 'message-header');
  const avatar = element('span', assistant ? '◈' : 'В', 'avatar');
  avatar.setAttribute('aria-hidden', 'true');
  const time = element('time', dateLabel(message.created_at, true));
  time.dateTime = message.created_at;
  header.append(avatar, element('span', assistant ? 'Помощник IACPaaS' : 'Вы'), time);
  article.append(header);
  if (state.editing === message.id) {
    article.classList.add('editing');
    const form = element('form', undefined, 'message-editor');
    const input = element('textarea');
    input.value = state.edits.get(message.id) ?? message.content;
    input.maxLength = 20000;
    input.setAttribute('aria-label', 'Исправленный запрос');
    input.addEventListener('input', () => state.edits.set(message.id, input.value));
    const help = element('p', 'Отправка создаст новую версию диалога с этого места. Исходная история сохранится.', 'edit-help');
    const actions = element('div', undefined, 'editor-actions');
    const cancel = element('button', 'Отмена');
    cancel.type = 'button';
    cancel.addEventListener('click', () => { state.editing = null; renderMessages(); controls(); focusMessageAction(message.id); });
    const save = element('button', 'Сохранить и отправить', 'primary');
    save.type = 'submit';
    actions.append(cancel, save);
    form.append(input, help, actions);
    form.addEventListener('submit', event => {
      event.preventDefault();
      if (!input.value.trim() || state.busy) return;
      runRevision(state.conversation.id, {message_id:message.id, action:'edit', content:input.value, request_id:crypto.randomUUID()});
    });
    form.addEventListener('keydown', event => { if (event.key === 'Escape') { event.preventDefault(); cancel.click(); } });
    article.append(form);
    return article;
  }
  if (assistant) {
    if (Array.isArray(message.verification?.progress) && message.verification.progress.length) {
      const details = element('details', undefined, 'work-log');
      details.append(element('summary', 'Ход работы'));
      const log = element('ol');
      for (const event of message.verification.progress) log.append(element('li', event.text, `work-${event.kind}`));
      details.append(log); article.append(details);
    }
    article.append(renderMarkdown(message.content, id => {
      const source = document.getElementById(`source-${message.id}-${id}`);
      if (!source) return;
      source.closest('details').open = true;
      source.tabIndex = -1; source.focus({preventScroll:true});
      source.scrollIntoView({block:'nearest', behavior:reducedMotion.matches ? 'instant' : 'smooth'});
    }));
  } else article.append(element('div', message.content, 'message-content'));
  if (message.state === 'pending' || message.state === 'failed') {
    article.append(element('p', message.state === 'pending' ? 'Ожидаем ответ…' : 'Ответ не получен. Можно исправить запрос и отправить снова.', `message-state ${message.state}`));
  }
  if (assistant) {
    const verification = message.verification;
    const labels = {partial:'Частичный ответ',reviewed:'Сверено по источникам',grounded:'Есть источники',verified:'Поля сверены',rejected:'Ответ не прошёл сверку',review_unavailable:'Проверка не завершена',insufficient_data:'Недостаточно данных',not_applicable:'Проверка не применима',error:'Ошибка проверки'};
    const status = Object.hasOwn(labels, verification?.status) ? verification.status : 'unknown';
    if (status !== 'not_applicable') article.append(element('span', labels[status] || 'Проверка не указана', `verification ${status}`));
    if (verification?.reason && status !== 'not_applicable') article.append(element('p', verification.reason, 'verification-reason'));
  }
  const review = message.verification?.review;
  const validation = message.verification?.validation;
  if (assistant && validation?.numeric_check) {
    const details = element('details', undefined, 'answer-review');
    details.append(element('summary', 'Причина отказа'));
    for (const quantity of validation.numeric_check.unmatched_quantities || []) details.append(element('p', `Не подтверждено источниками: ${quantity.value} ${quantity.unit}.`));
    details.append(element('p', 'Это ошибка подготовки ответа агентом. Она не означает, что пользователь указал недостаточно данных.'));
    article.append(details);
  }
  if (assistant && review) {
    const details = element('details', undefined, 'answer-review');
    details.append(element('summary', 'Проверка утверждений'));
    details.append(element('p', review.reason || 'Автоматическая проверка по источникам.'));
    if (Array.isArray(review.blocks) && review.blocks.length) {
      const list = element('ol');
      const statuses = {supported:'Подтверждено источниками',not_factual:'Пояснение',unsupported:'Нет подтверждения',contradicted:'Противоречие данным'};
      for (const block of review.blocks) {
        const item = element('li');
        item.append(element('strong', statuses[block.status] || 'Не проверено'));
        if (block.text) { const quote = renderMarkdown(block.text); quote.classList.remove('message-content'); quote.classList.add('review-quote'); item.append(quote); }
        item.append(element('p', block.reason));
        list.append(item);
      }
      details.append(list);
    }
    if (Array.isArray(review.issues)) {
      for (const issue of review.issues) details.append(element('p', issue.code === 'unit_mismatch' ? 'Единица не совпала с источником.' : 'Направление нормативной границы не совпало с источником.'));
    }
    details.append(element('p', 'Автоматическая сверка не заменяет проверку первоисточника.', 'muted'));
    article.append(details);
  }
  if (Array.isArray(message.sources) && message.sources.length) {
    const details = element('details', undefined, 'sources');
    details.append(element('summary', `Источники · ${message.sources.length}`));
    const list = element('ol');
    const fields = {id:'ID свидетельства',resource_path:'Ресурс',concept_id:'Понятие',field:'Поле',value:'Значение',unit:'Единица',operator:'Оператор'};
    for (const source of message.sources) {
      const li = element('li');
      li.id = `source-${message.id}-${source.id}`;
      const dl = element('dl');
      for (const [key, label] of Object.entries(fields)) {
        dl.append(element('dt', label));
        const dd = element('dd');
        dd.append(element(key === 'resource_path' || key === 'concept_id' ? 'code' : 'span', sourceText(source[key])));
        dl.append(dd);
      }
      li.append(dl); list.append(li);
    }
    const documents = message.verification?.documents;
    if (Array.isArray(documents) && documents.length) {
      const raw = element('details', undefined, 'mcp-documents');
      raw.append(element('summary', `Полные результаты MCP · ${documents.length}`));
      for (const document of documents) {
        raw.append(element('p', document.result?.data?.path || document.id));
        const pre = element('pre'); pre.textContent = JSON.stringify(document.result, null, 2); raw.append(pre);
      }
      details.append(raw);
    }
    details.append(list); article.append(details);
    animateDetails(details);
  }
  if (message.state !== 'pending' && !message.local) {
    const actions = element('div', undefined, 'message-actions');
    const button = element('button', assistant ? 'Сгенерировать снова' : 'Изменить запрос');
    button.type = 'button';
    button.dataset.messageAction = assistant ? 'regenerate' : 'edit';
    button.title = assistant ? 'Новый ответ в отдельной версии диалога. Исходная история сохранится.' : 'Исправить запрос и продолжить с этого места';
    button.addEventListener('click', () => {
      if (state.busy || state.attempts.has(state.conversation?.id)) return;
      if (assistant) runRevision(state.conversation.id, {message_id:message.id, action:'regenerate', request_id:crypto.randomUUID()});
      else {
        state.editing = message.id; renderMessages(); controls();
        const input = $('messages').querySelector('.message-editor textarea');
        input?.focus({preventScroll:true});
        input?.scrollIntoView({block:'nearest'});
      }
    });
    const copy = element('button', 'Копировать');
    copy.type = 'button';
    copy.addEventListener('click', async () => {
      try { await navigator.clipboard.writeText(message.content); copy.textContent = 'Скопировано'; }
      catch { announce('Не удалось скопировать. Выделите текст сообщения.'); }
    });
    actions.append(button, copy);
    enhanceButton(button); enhanceButton(copy);
    article.append(actions);
  }
  return article;
}
function renderMessages(animate = false) {
  const area = document.querySelector('.composer-area');
  const before = area.getBoundingClientRect();
  const chat = document.querySelector('.chat');
  const wasEmpty = chat.dataset.empty === 'true';
  const existing = new Set([...$('messages').children].map(node => node.dataset.messageId));
  const expanded = new Set([...$('messages').querySelectorAll('.message details[open]')].map(node => `${node.closest('.message').dataset.messageId}:${node.className}`));
  $('messages').replaceChildren(...state.messages.map(message => {
    const node = renderMessage(message);
    if (animate && !existing.has(message.id)) node.classList.add('message-enter');
    return node;
  }));
  for (const node of $('messages').querySelectorAll('.message details')) if (expanded.has(`${node.closest('.message').dataset.messageId}:${node.className}`)) node.open = true;
  $('welcome').hidden = state.messages.length > 0;
  $('starter-actions').hidden = state.messages.length > 0 || !!state.conversation?.archived;
  if (!state.messages.length && starterConversation !== (state.conversation?.id || null)) renderStarters();
  chat.dataset.empty = String(state.messages.length === 0);
  if (wasEmpty !== (state.messages.length === 0) && !reducedMotion.matches) {
    const delta = before.top - area.getBoundingClientRect().top;
    area.animate([{transform:`translateY(${delta}px)`},{transform:'translateY(0)'}], {duration:220,easing:'cubic-bezier(.2,0,0,1)'});
  }
  $('chat-title').textContent = state.conversation?.title || 'Новый диалог';
  $('older').hidden = state.before === null;
  updateScrollButton();
}
async function openConversation(id) {
  if (state.busy) return;
  clearError(); state.busy = true; controls(); announce('Загружаем диалог…');
  try {
    const page = await api.history(id);
    state.drafts.set(state.conversation?.id || 'new', $('prompt').value);
    state.conversation = page.conversation;
    state.editing = null;
    $('prompt').value = state.drafts.get(id) || '';
    state.messages = page.messages;
    state.before = page.has_more && page.next_before ? page.next_before : null;
    renderMessages(); renderContext(page.context); renderConversations();
    const attempt = state.attempts.get(id);
    if (attempt?.error) {
      $('prompt').value = attempt.submission.content;
      showError(attempt.error, () => sendAttempt(attempt), 'Повторить ту же отправку');
    }
    scrollToLatest(); closeSidebar();
  } catch (error) { showError(error, () => openConversation(id)); }
  finally { state.busy = false; controls(); announce(); }
}
async function start() {
  clearError(); announce('Подключаем историю…');
  try {
    const identity = await api.bootstrap();
    $('model').textContent = identity.model === 'deepseek/deepseek-v4.1-flash' ? 'DeepSeek V4.1 Flash' : `Модель: ${identity.model || 'не указана'}`;
    $('model').title = identity.model || '';
    $('backend').textContent = /mock/i.test(identity.backend || '') ? 'MCP: тестовые данные (mock)' : /IACPaaS/.test(identity.backend || '') ? 'MCP: IACPaaS · проверка при чтении' : 'MCP: источник пока не проверен';
    $('connection').textContent = 'Сервер доступен';
    $('connection').parentElement.dataset.state = 'ready';
    $('connection').title = `Транспорт MCP: ${identity.backend || 'не указан'}. Доступность модели и платформы проверяется при запросе.`;
    state.items = (await api.list()).items;
    state.ready = true; renderConversations(); renderStarters();
    initSettings(api); initPlatformSettings(api);
  } catch (error) {
    $('connection').textContent = 'Нет соединения';
    $('connection').parentElement.dataset.state = 'error';
    showError(error, start, 'Подключиться снова');
  } finally { controls(); announce(); }
}
async function loadOlder() {
  if (state.busy || !state.before || !state.conversation) return;
  state.busy = true; clearError(); controls(); announce('Загружаем ранние сообщения…');
  const transcript = $('transcript');
  const height = transcript.scrollHeight;
  const top = transcript.scrollTop;
  try {
    const page = await api.history(state.conversation.id, state.before);
    const existing = new Set(state.messages.map(message => message.id));
    state.messages = [...page.messages.filter(message => !existing.has(message.id)), ...state.messages];
    state.before = page.has_more && page.next_before ? page.next_before : null;
    renderMessages();
    transcript.scrollTop = top + transcript.scrollHeight - height;
    if ($('older').hidden) transcript.focus();
  } catch (error) { showError(error, loadOlder); }
  finally { state.busy = false; controls(); announce(); }
}
async function newConversation() {
  if (!state.ready || state.busy) return;
  clearError(); state.busy = true; controls(); announce('Создаём диалог…');
  try {
    const conversation = await api.create('Новый диалог');
    state.drafts.set(state.conversation?.id || 'new', $('prompt').value);
    state.conversation = conversation;
    state.editing = null;
    state.archive = false; state.items.unshift(conversation);
    state.messages = []; state.before = null;
    $('prompt').value = '';
    renderConversations(); renderMessages(); renderContext(null); closeSidebar();
  } catch (error) { showError(error, null); }
  finally { state.busy = false; controls(); announce(); $('prompt').focus(); }
}
function nearLatest() { const node = $('transcript'); return node.scrollHeight - node.scrollTop - node.clientHeight < 100; }
function updateScrollButton() { $('scroll-latest').hidden = nearLatest(); }
const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
function scrollToLatest(smooth = false) {
  $('transcript').scrollTo({top:$('transcript').scrollHeight, behavior:smooth && !reducedMotion.matches ? 'smooth' : 'instant'});
  updateScrollButton();
}
function animateDetails(details) {
  let animation, target;
  details.querySelector('summary').addEventListener('click', event => {
    event.preventDefault();
    target = animation ? !target : !details.open;
    const from = details.getBoundingClientRect().height;
    animation?.cancel();
    if (reducedMotion.matches) { details.open = target; details.style.overflow = ''; animation = null; return; }
    details.open = true;
    const to = target ? details.scrollHeight + 2 : details.querySelector('summary').getBoundingClientRect().height + 2;
    details.style.overflow = 'hidden';
    animation = details.animate([{height:from+'px'},{height:to+'px'}],{duration:180,easing:'cubic-bezier(.2,0,0,1)'});
    animation.onfinish = () => { details.open = target; details.style.overflow = ''; animation = null; };
  });
}
function resizePrompt() { const node = $('prompt'); node.style.height = 'auto'; node.style.height = Math.min(160, Math.max(30, node.scrollHeight)) + 'px'; }
function focusMessageAction(id) {
  [...$('messages').querySelectorAll('.message')].find(node => node.dataset.messageId === id)?.querySelector('button')?.focus({preventScroll:true});
}
async function runRevision(originId, revision) {
  if (state.busy) return;
  state.busy = true; clearError(); controls(); announce('Создаём новую версию диалога…');
  try {
    const branch = await api.revise(originId, revision);
    const page = await api.history(branch.conversation.id);
    state.drafts.set(originId, $('prompt').value);
    state.conversation = branch.conversation;
    state.items = [branch.conversation, ...state.items.filter(item => item.id !== branch.conversation.id)];
    state.messages = page.messages;
    state.before = page.has_more ? page.next_before : null;
    state.editing = null;
    $('prompt').value = branch.content;
    const attempt = {submission:Object.freeze({content:branch.content, request_id:branch.request_id}), error:null};
    state.attempts.set(branch.conversation.id, attempt);
    state.messages.push({id:branch.request_id, role:'user', content:branch.content, created_at:new Date().toISOString(), state:'pending', sources:[], local:true});
    renderConversations(); renderMessages(true); renderContext(page.context); scrollToLatest(true);
    await sendAttempt(attempt);
  } catch (error) { showError(error, () => runRevision(originId, revision), 'Повторить создание версии'); announce('Не удалось создать новую версию. Исходный диалог сохранён.'); }
  finally { state.busy = false; controls(); }
}
async function submitMessage(event) {
  event?.preventDefault();
  if (!state.ready || state.busy || state.attempts.has(state.conversation?.id)) return;
  const content = $('prompt').value.trim();
  if (!content) return;
  state.busy = true; controls(); clearError();
  try {
    if (!state.conversation) {
      announce('Создаём диалог…');
      state.conversation = await api.create(content.slice(0, 70));
      state.items.unshift(state.conversation);
      renderConversations();
    }
    const attempt = {submission: createSubmission(content), error: null};
    state.attempts.set(state.conversation.id, attempt);
    state.messages.push({id: attempt.submission.request_id, role:'user',content,created_at:new Date().toISOString(),state:'pending',sources:[],local:true});
    renderMessages(true); scrollToLatest(true);
    await sendAttempt(attempt);
  } catch (error) { showError(error); announce('Запрос не выполнен.'); }
  finally { state.busy = false; controls(); if (nearLatest()) $('prompt').focus({preventScroll:true}); }
}
async function sendAttempt(attempt) {
  attempt.error = null;
  clearError(); state.busy = true; controls(); announce('Ожидаем ответ. Помощник может обращаться к ресурсам IACPaaS…');
  try {
    const live = element('article', undefined, 'message assistant live');
    live.setAttribute('aria-label', 'Ход работы помощника');
    const heading = element('div', undefined, 'message-header');
    heading.append(element('span', '◈', 'avatar'), element('span', 'Помощник IACPaaS'));
    const log = element('div', undefined, 'live-log');
    live.append(heading, log); $('messages').append(live);
    const result = await api.send(state.conversation.id, attempt.submission, event => {
      if (!event || typeof event.text !== 'string') return;
      const follow = nearLatest();
      const kind = ['commentary','status','tool_start','tool_end'].includes(event.kind) ? event.kind : 'status';
      log.append(element('p', event.text, `work-${kind}`));
      live.dataset.phase = kind;
      announce(event.text);
      if (follow) scrollToLatest(); else updateScrollButton();
    });
    const follow = nearLatest();
    const replaced = new Set([attempt.submission.request_id, result.user_message.id, result.assistant_message.id]);
    state.messages = state.messages.filter(message => !replaced.has(message.id));
    state.messages.push(result.user_message, result.assistant_message);
    state.attempts.delete(state.conversation.id);
    $('prompt').value = '';
    state.conversation.updated_at = result.assistant_message.created_at;
    state.items = [state.conversation, ...state.items.filter(item => item.id !== state.conversation.id)];
    renderMessages(true); renderContext(result.context); renderConversations(); if (follow) scrollToLatest(true); else updateScrollButton();
    announce('Ответ получен.');
  } catch (error) {
    attempt.error = error;
    const retryable = error.retryable || error.status === 409;
    if (!retryable) {
      state.attempts.delete(state.conversation.id);
      const local = state.messages.find(message => message.id === attempt.submission.request_id);
      if (local) local.state = 'failed';
      renderMessages();
    }
    showError(error, retryable ? () => sendAttempt(attempt) : () => openConversation(state.conversation.id), retryable ? 'Повторить ту же отправку' : 'Обновить историю');
    announce('');
  } finally { document.querySelectorAll('.message.live').forEach(node => node.remove()); state.busy = false; controls(); }
}
$('composer').addEventListener('submit', submitMessage);
$('prompt').addEventListener('input', controls);
$('prompt').addEventListener('keydown', event => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) submitMessage(event);
});
function renderStarters() {
  starterConversation = state.conversation?.id || null;
  $('starter-list').replaceChildren(...pickStarters().map(item => {
    const button = element('button'); button.type = 'button'; button.dataset.prompt = item.question;
    button.append(element('strong', item.title), element('span', item.question, 'suggestion-description'));
    button.addEventListener('click', () => {
      if (state.busy) return;
      $('prompt').value = item.question; controls(); $('prompt').focus({preventScroll:true});
    });
    return button;
  }));
  controls();
}
$('new-chat').addEventListener('click', newConversation);
$('older').addEventListener('click', loadOlder);
$('retry').addEventListener('click', () => { if (!state.busy) retryAction?.(); });
$('transcript').addEventListener('scroll', updateScrollButton, {passive:true});
$('scroll-latest').addEventListener('click', () => scrollToLatest(true));
const narrow = window.matchMedia('(max-width: 900px)');
function closeSidebar() {
  const wasOpen = document.body.classList.contains('sidebar-open');
  document.body.classList.remove('sidebar-open');
  $('sidebar-backdrop').hidden = true;
  $('nav-toggle').setAttribute('aria-expanded', 'false');
  $('main').inert = false;
  $('sidebar').inert = narrow.matches;
  $('sidebar').removeAttribute('role'); $('sidebar').removeAttribute('aria-modal');
  if (wasOpen && narrow.matches) $('nav-toggle').focus({preventScroll:true});
}
$('nav-toggle').addEventListener('click', () => {
  document.body.classList.add('sidebar-open');
  $('sidebar-backdrop').hidden = false;
  $('nav-toggle').setAttribute('aria-expanded','true');
  $('sidebar').inert = false;
  $('sidebar').setAttribute('role','dialog'); $('sidebar').setAttribute('aria-modal','true');
  $('main').inert = true;
  const focusDrawer = () => {
    if (document.body.classList.contains('sidebar-open') && !$('sidebar').contains(document.activeElement)) $('sidebar-close').focus({preventScroll:true});
  };
  requestAnimationFrame(focusDrawer);
  // Inert focus cleanup and the opening transition can finish after the first frame.
  setTimeout(focusDrawer, 240);
});
$('sidebar-close').addEventListener('click', closeSidebar);
$('settings-open').addEventListener('click', closeSidebar);
$('sidebar-backdrop').addEventListener('click', closeSidebar);
document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && document.body.classList.contains('sidebar-open')) {
    event.preventDefault(); closeSidebar();
  }
});
$('sidebar').addEventListener('keydown', event => {
  if (!document.body.classList.contains('sidebar-open')) return;
  if (event.key === 'Escape') { event.preventDefault(); closeSidebar(); }
  if (event.key === 'Tab') {
    const nodes = [...$('sidebar').querySelectorAll('a,button:not(:disabled)')].filter(node => node.getClientRects().length);
    const first = nodes[0], last = nodes.at(-1);
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }
});
narrow.addEventListener('change', closeSidebar);
closeSidebar();
start();
