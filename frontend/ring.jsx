import React, {useEffect, useState} from 'react';
import {createRoot} from 'react-dom/client';
import Select from '@jetbrains/ring-ui-built/components/select/select';
import Theme, {ThemeProvider, applyTheme} from '@jetbrains/ring-ui-built/components/global/theme';
import {getButtonClasses} from '@jetbrains/ring-ui-built/components/button/button.classes';
import {setTranslations} from '@jetbrains/ring-ui-built/components/i18n/i18n';
import '@jetbrains/ring-ui-built/components/style.css';
import '@fontsource/inter/400.css';
import '@fontsource/inter/500.css';
import '@fontsource/inter/600.css';

const bridges = new WeakMap();
const isDark = () => document.documentElement.dataset.theme === 'dark';
setTranslations({filterItems:'Поиск…', noOptionsFound:'Ничего не найдено', selectOption:'Выберите значение', clear:'Очистить', clearTitle:'Очистить', loading:'Загрузка…'});

export function enhanceButton(button) {
  const inline = button.classList.contains('icon-button') || button.classList.contains('new-chat') || button.classList.contains('sidebar-settings') ||
    button.classList.contains('conversation-item') || !!button.closest('.message-actions');
  for (const name of [...button.classList]) if (name.startsWith('ring-button-')) button.classList.remove(name);
  button.classList.add(...getButtonClasses({
    height:'L', inline,
    primary:button.classList.contains('primary') || button.classList.contains('send'),
  }).split(' ').filter(Boolean));
  return button;
}

function Picker({native, list, editable}) {
  const [, update] = useState(0);
  const [dark, setDark] = useState(isDark);
  const [query, setQuery] = useState('');
  useEffect(() => {
    const refresh = () => update(value => value + 1);
    const bridge = bridges.get(native);
    bridge.refresh = refresh;
    const observer = new MutationObserver(refresh);
    observer.observe(native, {childList:true, subtree:true, attributes:true, attributeFilter:['disabled','selected']});
    if (list) observer.observe(list, {childList:true, subtree:true});
    const theme = () => setDark(isDark());
    window.addEventListener('iacpaas-theme', theme);
    return () => { observer.disconnect(); bridge.refresh = null; window.removeEventListener('iacpaas-theme', theme); };
  }, [native, list]);
  const catalogue = [...(list || native).querySelectorAll('option')].map(option => ({key:option.value, label:option.textContent}));
  const matching = query ? catalogue.filter(item=>(item.key+' '+item.label).toLocaleLowerCase().includes(query.toLocaleLowerCase())) : catalogue;
  const data = matching.slice(0, 100);
  const selected = catalogue.find(item => item.key === native.value) || (native.value ? {key:native.value, label:native.value} : null);
  const commit = value => {
    native.value = value;
    native.dispatchEvent(new Event(editable ? 'input' : 'change', {bubbles:true}));
  };
  return <ThemeProvider theme={dark ? Theme.DARK : Theme.LIGHT} passToPopups className="ring-picker-theme">
    <Select id={native.id+'-control'} type={editable ? Select.Type.INPUT : Select.Type.BUTTON}
      data={data} selected={selected} disabled={native.disabled} height="L" size={Select.Size.FULL}
      label={editable ? null : native.getAttribute('aria-label') || 'Выберите значение'} filter={native.id!=='theme-select'}
      inputPlaceholder={editable ? 'Название или ID модели' : 'Поиск…'}
      maxHeight={320} minWidth={native.id==='model-select' ? 280 : 220}
      notFoundMessage={editable ? 'Введите ID и нажмите Enter' : 'Ничего не найдено'}
      loadingMessage="Загрузка…" ringPopupTarget={native.closest('dialog') ? 'settings' : null}
      popupClassName="iacpaas-ring-menu" className="iacpaas-ring-select"
      renderOptimization={false}
      renderTopToolbar={matching.length>100 ? ()=> <p className="ring-catalog-hint">Уточните поиск, чтобы увидеть остальные модели</p> : undefined}
      onChange={item => { if (item) commit(String(editable && typeof item.key==='number' ? item.label : item.key)); }}
      allowAny={editable} add={editable ? {label:value=>'Использовать «'+value+'»', prefix:''} : null}
      onAdd={editable ? value=>commit(value) : undefined}
      onFilter={value => {
        setQuery(value);
        if (editable) {
          bridges.get(native).setRaw(value);
          native.dispatchEvent(new Event('input', {bubbles:true}));
        }
      }}
      onClose={()=>setQuery('')}
    />
  </ThemeProvider>;
}

function enhanceSelect(native, list=null) {
  if (bridges.has(native)) return;
  const host = document.createElement('div');
  host.className = 'ring-picker-host';
  host.dataset.control = native.id;
  native.after(host);
  native.hidden = true;
  native.tabIndex = -1;
  native.setAttribute('aria-hidden','true');
  const editable = native.tagName === 'INPUT';
  if (editable) native.removeAttribute('list');
  if (editable) native.removeAttribute('required');
  const prototype = editable ? HTMLInputElement.prototype : HTMLSelectElement.prototype;
  const descriptor = Object.getOwnPropertyDescriptor(prototype, 'value');
  const bridge = {refresh:null, setRaw:value => descriptor.set.call(native,value)};
  bridges.set(native, bridge);
  Object.defineProperty(native, 'value', {
    configurable:true,
    get:()=>descriptor.get.call(native),
    set:value=>{ descriptor.set.call(native,value); bridge.refresh?.(); },
  });
  native.focus = options => host.querySelector('input,[role=combobox]')?.focus(options);
  for (const label of document.querySelectorAll('label')) {
    if (label.htmlFor === native.id) {
      label.htmlFor = native.id+'-control';
      native.setAttribute('aria-label', label.textContent);
    }
  }
  createRoot(host).render(<Picker native={native} list={list} editable={editable}/>);
}

export function initRingUI() {
  const sync = () => applyTheme(isDark() ? Theme.DARK : Theme.LIGHT, document.documentElement);
  sync();
  window.addEventListener('iacpaas-theme', sync);
  for (const button of document.querySelectorAll('button')) enhanceButton(button);
  for (const id of ['settings-provider','theme-select']) enhanceSelect(document.getElementById(id));
  enhanceSelect(document.getElementById('settings-model'), document.getElementById('settings-models'));
}
