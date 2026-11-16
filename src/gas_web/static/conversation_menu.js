export function initConversationMenu({onAction, canOpen = () => true, createButton}) {
  const menu = document.createElement('div');
  menu.id = 'conversation-actions-menu';
  menu.className = 'conversation-context-menu';
  menu.setAttribute('role', 'menu');
  menu.setAttribute('aria-label', 'Действия с диалогом');
  menu.hidden = true;
  document.body.append(menu);
  let current = null;
  let anchor = null;
  let focusTarget = null;

  function close(restoreFocus = false) {
    menu.hidden = true;
    anchor?.setAttribute('aria-expanded', 'false');
    const target = focusTarget;
    current = anchor = focusTarget = null;
    if (restoreFocus && target?.isConnected) target.focus({preventScroll:true});
  }
  function open(item, options) {
    if (!canOpen()) return false;
    if (options.toggle && !menu.hidden && anchor === options.anchor) {
      close(true);
      return false;
    }
    close();
    current = item;
    anchor = options.anchor;
    focusTarget = options.focusTarget || anchor;
    anchor.setAttribute('aria-expanded', 'true');
    menu.replaceChildren();
    for (const [action, label] of [
      ['rename', 'Переименовать'],
      [item.archived ? 'restore' : 'archive', item.archived ? 'Восстановить' : 'В архив'],
      ['delete', 'Удалить'],
    ]) {
      const button = createButton ? createButton(label) : document.createElement('button');
      if (!createButton) button.textContent = label;
      button.type = 'button';
      button.setAttribute('role', 'menuitem');
      if (action === 'delete') button.classList.add('danger');
      button.addEventListener('click', () => {
        const selected = current;
        close();
        if (selected && canOpen()) onAction(selected, action);
      });
      menu.append(button);
    }
    menu.hidden = false;
    const rect = menu.getBoundingClientRect();
    const origin = anchor.getBoundingClientRect();
    const x = Number.isFinite(options.x) ? options.x : origin.right - rect.width;
    const y = Number.isFinite(options.y) ? options.y : origin.bottom + 4;
    menu.style.left = `${Math.max(8, Math.min(x, window.innerWidth - rect.width - 8))}px`;
    menu.style.top = `${Math.max(8, Math.min(y, window.innerHeight - rect.height - 8))}px`;
    menu.querySelector('[role="menuitem"]')?.focus();
    return true;
  }
  menu.addEventListener('contextmenu', event => event.preventDefault());
  menu.addEventListener('keydown', event => {
    const buttons = [...menu.querySelectorAll('[role="menuitem"]')];
    const index = buttons.indexOf(document.activeElement);
    let next;
    if (event.key === 'ArrowDown') next = (index + 1) % buttons.length;
    if (event.key === 'ArrowUp') next = (index - 1 + buttons.length) % buttons.length;
    if (event.key === 'Home') next = 0;
    if (event.key === 'End') next = buttons.length - 1;
    if (next !== undefined) { event.preventDefault(); buttons[next]?.focus(); }
    if (event.key === 'Tab') close(true);
  });
  document.addEventListener('keydown', event => {
    if (!menu.hidden && event.key === 'Escape') { event.preventDefault(); close(true); }
  });
  document.addEventListener('pointerdown', event => {
    if (!menu.hidden && !menu.contains(event.target) && !anchor?.contains(event.target)) close();
  });
  document.addEventListener('contextmenu', event => {
    if (!menu.contains(event.target)) close();
  });
  document.addEventListener('scroll', event => {
    if (!menu.contains(event.target)) close();
  }, true);
  window.addEventListener('resize', () => close());
  return {open, close};
}
