// Pure DOM unit test; no browser/network and no external dependencies.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../motor_diagnosis/web.py', import.meta.url), 'utf8');
const script = source.match(/<script>([\s\S]*?)<\/script>/)[1];
const canvasCalls = [];
const canvasContext = {
  arc(...args) { canvasCalls.push(['arc', ...args]); }, beginPath() {}, clearRect() {},
  fill() {}, fillRect() {}, fillText() {}, lineTo(...args) { canvasCalls.push(['lineTo', ...args]); },
  moveTo(...args) { canvasCalls.push(['moveTo', ...args]); }, setLineDash() {},
  stroke() {}, measureText(text) { return {width: String(text).length * 8}; },
};
class Element {
  children = [];
  textContent = '';
  hidden = false;
  value = '';
  width = 900;
  height = 280;
  listeners = {};
  addEventListener(name, callback) { this.listeners[name] = callback; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; this.textContent = ''; }
  appendChild(child) { this.children.push(child); }
  getContext() { return canvasContext; }
  getBoundingClientRect() { return {left: 0, width: 900}; }
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
vm.runInContext('renderModelResult([])', context);
const modelPanel = elements.get('modelResult');
assert.match(modelPanel.textContent, /not mapped to anomalyScore/);
context.models = [{
  version: '<model-v1>', deploymentStatus: 'not_deployed', artifactVerified: false,
  metrics: {f1: 1}, errorCases: ['<img src=x onerror=alert(1)>'],
  domainGap: 'different sensor', fieldCalibrationPlan: 'collect normal data',
  limitations: 'demo only', baselineVersion: 'BASELINE-001',
  baselineSnapshot: {features: {rms: 0.08, peakHz: 30}},
}];
vm.runInContext('renderModelResult(models)', context);
assert.equal(modelPanel.children[0].textContent, '<model-v1> · not_deployed');
assert.match(modelPanel.children[1].children[1], /not deployment/);
assert.match(modelPanel.children[3].children[0].textContent, /Baseline version/);
assert.match(modelPanel.children[4].children[0].textContent, /Baseline features/);
assert.match(modelPanel.children[5].children[0].textContent, /Metrics/);
assert.match(modelPanel.children[6].children[1], /<img src=x/);
assert.equal(modelPanel.children[6].children.filter(child => child instanceof Element).length, 1);
assert.match(source, /api\(`\/api\/model-versions\?siteId=/);
vm.runInContext(`draw([
  {timestamp: '2026-09-01T00:00:00Z', vibrationRmsRaw: 0.08, acousticRmsRaw: 0.007, rpm: 1800},
  {timestamp: '2026-09-01T00:00:01Z', vibrationRmsRaw: 0.5, acousticRmsRaw: 0.06, rpm: 1650},
  {timestamp: '2026-09-01T00:00:10Z', vibrationRmsRaw: 0.9, acousticRmsRaw: 0.12, rpm: 1500},
], {vibrationRmsRaw: 'raw', acousticRmsRaw: 'raw'}, [
  {occurredAt: '2026-09-01T00:00:01Z'},
])`, context);
const expectedX = 34 + (900 - 68) * 0.1;
assert.ok(canvasCalls.some(call => call[0] === 'arc' && Math.abs(call[1] - expectedX) < 0.001));
assert.ok(canvasCalls.some(call => call[0] === 'lineTo' && Math.abs(call[1] - expectedX) < 0.001));
assert.equal(vm.runInContext(`chartPointIndexAtRatio([
  {timestamp: '2026-09-01T00:00:00Z'},
  {timestamp: '2026-09-01T00:00:01Z'},
  {timestamp: '2026-09-01T00:00:10Z'},
], 0.1)`, context), 1);
vm.runInContext(`draw(Array.from({length: 121}, (_, index) => ({
  timestamp: new Date(Date.UTC(2026, 8, 1, 0, 0, index)).toISOString(),
  vibrationRmsRaw: index, acousticRmsRaw: index, rpm: 1800,
})), {vibrationRmsRaw: 'raw', acousticRmsRaw: 'raw'})`, context);
const chart = elements.get('chart');
const hint = elements.get('chartHint');
chart.listeners.mousemove({clientX: 34});
assert.match(hint.textContent, /00:00:00\.000Z/);
chart.listeners.mousemove({clientX: 450});
assert.match(hint.textContent, /00:01:00\.000Z/);
chart.listeners.mousemove({clientX: 866});
assert.match(hint.textContent, /00:02:00\.000Z/);
assert.match(source, /draw\(telem\.points, telem\.units, events\)/);
console.log('Week 4 dashboard: notification rendering, XSS-safe text, empty state and demo gating passed.');
