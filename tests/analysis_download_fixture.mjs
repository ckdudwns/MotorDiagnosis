// Real local HTTP -> shipped preview/download handlers -> captured browser Blob.
import assert from 'node:assert/strict';
import {harness} from './dashboard_harness.mjs';

let input = '';
for await (const chunk of process.stdin) input += chunk;
const {baseUrl, token, row} = JSON.parse(input);
const h = harness();
let original, saved, requests = 0, clicks = 0;
h.context.downloadToken = token;
h.context.row = row;
h.run('token=downloadToken');
h.context.fetch = async (path, options) => {
  assert.equal(path, `/api/analysis/${row.id}`);
  assert.equal(options.headers.authorization, `Bearer ${token}`);
  requests++;
  const response = await fetch(new URL(path, baseUrl), options);
  assert.equal(response.status, 200);
  original = await response.clone().text();
  return response;
};
h.context.URL = {createObjectURL(blob) {saved=blob; return 'blob:fixture';}, revokeObjectURL() {}};
h.Element.prototype.click = function () {if (this.tagName==='a') clicks++;};
h.run('renderAnalysisRows($("opsAnalysisRows"),{items:[row]},()=>true)');
const card = h.get('opsAnalysisRows').children[0];
await card.children.find(element => element.tagName==='button').listeners.click();
const preview = card.children.at(-1);
const download = preview.children.find(element => element.tagName==='button');
assert.ok(download, preview.textContent);
download.listeners.click();
assert.equal(clicks, 1);
assert.equal(requests, 1);
assert.equal(saved.type, 'application/json');
console.log(JSON.stringify({original, downloaded:await saved.text()}));
