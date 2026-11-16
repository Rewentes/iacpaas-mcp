export function initConversationRename({api, onRenamed, announce = () => {}, onClose = () => {}}) {
  const node = id => document.getElementById(id);
  const dialog = node('rename-dialog');
  const form = node('rename-form');
  const input = node('rename-name');
  const error = node('rename-error');
  const save = node('rename-save');
  const cancel = node('rename-cancel');
  let current = null;
  let saving = false;

  function clearError() {
    error.textContent = '';
    input.setCustomValidity('');
  }
  input.addEventListener('input', clearError);
  cancel.addEventListener('click', () => { if (!saving) dialog.close(); });
  dialog.addEventListener('cancel', event => { if (saving) event.preventDefault(); });
  dialog.addEventListener('close', () => {
    const id = current?.id;
    current = null;
    input.value = '';
    clearError();
    if (id) onClose(id);
  });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (!current || saving) return;
    const title = input.value.replace(/\s+/gu, ' ').trim();
    if (!title || title.length > 200) {
      input.setCustomValidity('Введите название от 1 до 200 символов.');
      input.reportValidity();
      return;
    }
    clearError();
    saving = true;
    input.disabled = save.disabled = cancel.disabled = true;
    try {
      const result = await api.rename(current.id, title);
      onRenamed(result.conversation);
      announce('Название диалога изменено.');
      dialog.close();
    } catch (failure) {
      error.textContent = failure.message || 'Не удалось сохранить название.';
    } finally {
      saving = false;
      input.disabled = save.disabled = cancel.disabled = false;
      if (dialog.open) input.focus();
    }
  });
  return {
    open(conversation) {
      if (saving) return;
      current = conversation;
      input.value = conversation.title;
      clearError();
      dialog.showModal();
      input.focus();
      input.select();
    },
  };
}
