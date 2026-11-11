"""Exercise the production settings controller across a failed save and retry."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which('node') is None, reason='Node.js is needed for the frontend controller test')
def test_platform_form_keeps_failed_input_clears_success_and_clears_on_close():
    module = Path(__file__).resolve().parents[1] / 'src/gas_web/static/platform_settings.js'
    script = r'''
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

class Element {
  value = '';
  checked = false;
  disabled = false;
  textContent = '';
  events = new Map();
  addEventListener(name, handler) { this.events.set(name, handler); }
  setAttribute() {}
  querySelectorAll() { return inputs; }
  async dispatch(name) { await this.events.get(name)({preventDefault() {}}); }
}
const ids = ['platform-form', 'platform-username', 'platform-password',
  'platform-clear-password', 'platform-status', 'platform-save', 'platform-test',
  'settings-open', 'settings-dialog'];
const nodes = Object.fromEntries(ids.map(id => [id, new Element()]));
const inputs = ['platform-username', 'platform-password', 'platform-clear-password',
  'platform-save', 'platform-test'].map(id => nodes[id]);
globalThis.document = {getElementById: id => nodes[id]};
const content = readFileSync(process.argv[2], 'utf8');
const {initPlatformSettings} = await import('data:text/javascript;base64,' + Buffer.from(content).toString('base64'));
const profile = {username:'fixture-login', password_set:true, configured:true,
  source:'saved', local_mcp:true};
let fail = true;
const submissions = [];
initPlatformSettings({
  async platformSettings() { return profile; },
  async savePlatformSettings(body) {
    submissions.push({...body});
    if (fail) throw new Error('Fixture connection failure');
    return profile;
  },
});
await nodes['settings-open'].dispatch('click');
nodes['platform-password'].value = 'fixture-replacement-password';
await nodes['platform-form'].dispatch('submit');
assert.equal(nodes['platform-password'].value, 'fixture-replacement-password');
assert.equal(nodes['platform-status'].textContent, 'Fixture connection failure');
assert.equal(nodes['platform-save'].disabled, false);
fail = false;
await nodes['platform-form'].dispatch('submit');
assert.equal(submissions.length, 2);
assert.deepEqual(submissions[1], submissions[0]);
assert.equal(nodes['platform-password'].value, '');
nodes['platform-password'].value = 'fixture-unsaved-password';
await nodes['settings-dialog'].dispatch('close');
assert.equal(nodes['platform-password'].value, '');
'''
    result = subprocess.run(
        ['node', '--input-type=module', '-', str(module)], input=script,
        text=True, encoding='utf-8', capture_output=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
