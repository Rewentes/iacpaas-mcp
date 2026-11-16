"""Exercise the production rename controller without browser or network calls."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(
    shutil.which("node") is None, reason="Node.js is needed for the frontend controller test"
)
def test_rename_keeps_failed_input_retries_once_and_cancels_without_saving():
    module = Path(__file__).resolve().parents[1] / "src/gas_web/static/conversation_titles.js"
    script = r"""
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
class Element {
  value = ''; disabled = false; textContent = ''; open = false; events = new Map();
  addEventListener(name, handler) { this.events.set(name, handler); }
  setCustomValidity(text) { this.validity = text; }
  reportValidity() { this.reported = true; }
  focus() { this.focused = true; }
  select() { this.selected = true; }
  showModal() { this.open = true; }
  close() { this.open = false; this.events.get('close')?.({}); }
  async dispatch(name) { await this.events.get(name)({preventDefault(){}}); }
}
const nodes = Object.fromEntries(['rename-dialog','rename-form','rename-name','rename-error','rename-save','rename-cancel'].map(id=>[id,new Element()]));
globalThis.document = {getElementById:id=>nodes[id]};
const source=readFileSync(process.argv[2],'utf8');
const {initConversationRename}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
const submitted=[], renamed=[], closed=[];
let fail=true;
const controller=initConversationRename({
  api:{async rename(id,title){submitted.push({id,title});if(fail)throw new Error('Не удалось подключиться');return {conversation:{id,title}};}},
  onRenamed:chat=>renamed.push(chat),onClose:id=>closed.push(id),
});
controller.open({id:'chat',title:'Исходное'});
assert.equal(nodes['rename-dialog'].open,true);
assert.equal(nodes['rename-name'].value,'Исходное');
assert.equal(nodes['rename-name'].selected,true);
nodes['rename-name'].value='  ';
await nodes['rename-form'].dispatch('submit');
assert.equal(submitted.length,0);
assert.equal(nodes['rename-name'].reported,true);
nodes['rename-name'].value='  Аргон   и гелий  ';
await nodes['rename-form'].dispatch('submit');
assert.equal(nodes['rename-name'].value,'  Аргон   и гелий  ');
assert.equal(nodes['rename-dialog'].open,true);
assert.equal(nodes['rename-error'].textContent,'Не удалось подключиться');
assert.equal(nodes['rename-save'].disabled,false);
fail=false;
await nodes['rename-form'].dispatch('submit');
assert.equal(nodes['rename-dialog'].open,false);
assert.equal(nodes['rename-name'].value,'');
assert.deepEqual(submitted,[{id:'chat',title:'Аргон и гелий'},{id:'chat',title:'Аргон и гелий'}]);
assert.deepEqual(renamed,[{id:'chat',title:'Аргон и гелий'}]);
controller.open({id:'other',title:'Другой'});
nodes['rename-name'].value='Не сохранять';
await nodes['rename-cancel'].dispatch('click');
assert.equal(submitted.length,2);
assert.deepEqual(closed,['chat','other']);
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-", str(module)],
        input=script,
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
