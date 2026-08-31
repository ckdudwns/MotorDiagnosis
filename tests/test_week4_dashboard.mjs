// Pure DOM unit test; no browser/network and no external dependencies.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../motor_diagnosis/web.py', import.meta.url), 'utf8');
const script = source.match(/<script>([\s\S]*?)<\/script>/)[1];
class Element {
  children = [];
  textContent = '';
  hidden = false;
  value = '';
  addEventListener() {}
  replaceChildren(...children) { this.children = children; this.textContent = ''; }
  appendChild(child) { this.children.push(child); }
}
const elements = new Map();
const context = vm.createContext({
  document: {
    addEventListener() {},
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, new Element());
      return elements.get(id);
    },
    createElement: () => new Element(),
  },
  setInterval() {}, console,
});
vm.runInContext(script, context);
context.rows = [{
  isTest: true, event: {isSynthetic: true, title: '<img src=x onerror=alert(1)>'},
  deliveredAt: '2026-08-31T00:00:00Z',
}];
vm.runInContext('renderNotifications(rows)', context);
const panel = elements.get('notifications');
assert.equal(panel.children.length, 1);
assert.match(panel.children[0].textContent, /\[TEST\] \[SYNTHETIC\] <img/);
assert.equal(panel.children[0].children.length, 0);
vm.runInContext('renderNotifications([])', context);
assert.equal(panel.children.length, 0);
assert.equal(panel.textContent, 'No notifications.');
assert.match(source, /id="injectBtn" hidden/);
assert.match(source, /hidden = !boot.demoEnabled/);
console.log('Week 4 dashboard: notification rendering, XSS-safe text, empty state and demo gating passed.');
