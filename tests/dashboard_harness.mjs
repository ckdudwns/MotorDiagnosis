// Shared deterministic DOM; execute the shipped script, including its real API helper.
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {randomUUID} from 'node:crypto';

export const source = readFileSync(new URL('../motor_diagnosis/web.py', import.meta.url), 'utf8');
const script = source.match(/<script>([\s\S]*?)<\/script>/)[1];
export function harness() {
  const calls = [], requests = [], elements = new Map(), intervals = [];
  const canvas = Object.fromEntries(['arc','beginPath','clearRect','fill','fillRect','fillText','lineTo','moveTo','setLineDash','stroke'].map(name => [name,(...args) => calls.push([name,...args])]));
  canvas.measureText = text => ({width:String(text).length * 8});
  class Element {
    children = []; textContent = ''; hidden = false; disabled = false; value = '';
    width = 900; height = 280; listeners = {}; dataset = {};
    attributes = {}; style = {};
    constructor(tag = 'div') {this.tagName = tag;}
    addEventListener(name, callback) {this.listeners[name] = callback;}
    setAttribute(name, value) {this.attributes[name] = String(value);}
    getAttribute(name) {return this.attributes[name] ?? null;}
    append(...children) {this.children.push(...children);}
    appendChild(child) {this.children.push(child);}
    replaceChildren(...children) {this.children = children; this.textContent = ''; if (this.tagName === 'select') this.value = children[0]?.value || '';}
    getContext() {return canvas;}
    getBoundingClientRect() {return {left:0,width:900};}
    click() {} remove() {}
  }
  const selectIds = new Set([...source.matchAll(/<select id="([^"]+)"/g)].map(match => match[1]));
  const get = id => {if (!elements.has(id)) elements.set(id,new Element(selectIds.has(id) ? 'select' : 'div')); return elements.get(id);};
  const context = vm.createContext({
    document:{body:new Element(),getElementById:get,createElement:tag => new Element(tag),addEventListener() {}},
    URL, URLSearchParams, atob, Blob, crypto:{randomUUID}, setTimeout, console, setInterval:fn => intervals.push(fn), confirm:() => true, alert:message => {throw new Error(message);},
    fetch:async (path,options = {}) => {
      requests.push({path,options});
      const body = await context.respond(path,options);
      return {ok:true,status:200,headers:{get:() => 'application/json'},json:async () => body,text:async () => JSON.stringify(body)};
    },
  });
  context.respond = async () => {throw new Error('Unexpected request');};
  const run = code => vm.runInContext(code,context);
  vm.runInContext(script,context);
  run(`permissions = ['*']; token = 'test-token'; sites = [{id:'S1',name:'Site 1'}];`);
  get('siteSelect').value = 'S1'; get('assetSelect').value = 'A1'; get('periodSelect').value = '24';
  get('eventSort').value = 'occurredAt_desc';
  return {run,get,context,requests,calls,intervals,Element};
}
