"""The same menu serves the pointer and overflow button, with keyboard dismissal."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which('node') is None, reason='Node.js is needed for the menu controller test')
def test_context_menu_targets_chat_stays_in_viewport_and_supports_keyboard():
    module = Path(__file__).resolve().parents[1]/'src/gas_web/static/conversation_menu.js'
    script = r'''
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
class Node {
  children=[]; attributes=new Map(); events=new Map(); style={}; hidden=false; isConnected=true;
  classList={add() {}};
  append(child) { this.children.push(child); }
  replaceChildren() { this.children=[]; }
  setAttribute(name,value) { this.attributes.set(name,value); }
  addEventListener(name,handler) { this.events.set(name,handler); }
  contains(target) { return this===target || this.children.some(c=>c.contains(target)); }
  querySelectorAll() { return this.children.filter(c=>c.attributes.get('role')==='menuitem'); }
  querySelector() { return this.querySelectorAll()[0]; }
  getBoundingClientRect() { return {width:190,height:120,right:240,bottom:80}; }
  focus() { document.activeElement=this; }
  emit(name,props={}) { let prevented=false;this.events.get(name)?.({target:this,preventDefault(){prevented=true;},...props});return prevented; }
}
const body=new Node(), outside=new Node();
const docEvents=new Map(),windowEvents=new Map();
globalThis.document={body,activeElement:null,createElement:()=>new Node(),addEventListener:(n,h)=>docEvents.set(n,h)};
globalThis.window={innerWidth:800,innerHeight:600,addEventListener:(n,h)=>windowEvents.set(n,h)};
const source=readFileSync(process.argv[2],'utf8');
const {initConversationMenu}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
const actions=[];let allowed=true;
const controller=initConversationMenu({canOpen:()=>allowed,onAction:(chat,action)=>actions.push([chat.id,action])});
const menu=body.children[0],anchor=new Node(),row=new Node();
assert.equal(menu.hidden,true);
controller.open({id:'a',archived:false},{anchor,focusTarget:row,x:790,y:590});
assert.equal(menu.style.left,'602px');assert.equal(menu.style.top,'472px');
assert.deepEqual(menu.children.map(b=>b.textContent),['Переименовать','В архив','Удалить']);
assert.equal(document.activeElement,menu.children[0]);
menu.emit('keydown',{key:'ArrowDown'});assert.equal(document.activeElement,menu.children[1]);
menu.emit('keydown',{key:'End'});assert.equal(document.activeElement,menu.children[2]);
menu.emit('keydown',{key:'ArrowDown'});assert.equal(document.activeElement,menu.children[0]);
docEvents.get('keydown')({key:'Escape',preventDefault(){}});
assert.equal(menu.hidden,true);assert.equal(document.activeElement,row);
assert.equal(anchor.attributes.get('aria-expanded'),'false');
controller.open({id:'b',archived:true},{anchor});
assert.equal(menu.children[1].textContent,'Восстановить');
menu.children[0].emit('click');assert.deepEqual(actions,[['b','rename']]);assert.equal(menu.hidden,true);
controller.open({id:'a',archived:false},{anchor});menu.children[2].emit('click');
assert.deepEqual(actions,[['b','rename'],['a','delete']]);
controller.open({id:'a'},{anchor,toggle:true});controller.open({id:'a'},{anchor,toggle:true});
assert.equal(menu.hidden,true);
controller.open({id:'a'},{anchor});docEvents.get('pointerdown')({target:outside});assert.equal(menu.hidden,true);
controller.open({id:'a'},{anchor});docEvents.get('scroll')({target:menu});assert.equal(menu.hidden,false);
docEvents.get('scroll')({target:outside});assert.equal(menu.hidden,true);
controller.open({id:'a'},{anchor});windowEvents.get('resize')();assert.equal(menu.hidden,true);
allowed=false;assert.equal(controller.open({id:'a'},{anchor}),false);assert.equal(menu.hidden,true);
'''
    result=subprocess.run(['node','--input-type=module','-',str(module)],input=script,
                          text=True,encoding='utf-8',capture_output=True,timeout=20)
    assert result.returncode==0,result.stderr
