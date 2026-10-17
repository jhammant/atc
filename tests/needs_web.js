"use strict";
// A small DOM harness for the real app.js renderer; no browser dependencies or network access.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
class Element {
  constructor(tag) { this.tagName = tag; this.childNodes = []; this.textContent = ""; this.dataset = {}; this.classList = {toggle() {}}; }
  append(...items) {
    for (const item of items) this.childNodes.push(...(item.tagName === "fragment" ? item.childNodes : [item]));
  }
  replaceChildren(...items) { this.childNodes = []; this.append(...items); }
  addEventListener() {}
  querySelectorAll() { return []; }
}
const elements = new Map();
const document = {
  hidden: true,
  createElement: (tag) => new Element(tag),
  createDocumentFragment: () => new Element("fragment"),
  getElementById: (id) => {
    if (!elements.has(id)) elements.set(id, new Element("div"));
    return elements.get(id);
  },
  querySelectorAll: () => [],
  addEventListener() {},
};
const input = JSON.parse(fs.readFileSync(0, "utf8"));
const context = vm.createContext({document, input, URLSearchParams, location: {hash: "#needs"},
  window: {addEventListener() {}}, setInterval() {}});
vm.runInContext(fs.readFileSync(path.join(__dirname, "../web/app.js"), "utf8"), context);
vm.runInContext("model = input; drawModel();", context);
const cards = elements.get("content").childNodes;
assert.equal(cards.length, input.needs.length);
for (let i = 0; i < cards.length; i++) {
  const item = input.needs[i], card = cards[i], title = card.childNodes[0].childNodes[0];
  assert.equal(title.textContent, item.title);
  assert.equal(title.tagName, item.sid ? "a" : "strong");
  if (item.sid) assert.ok(title.href.includes(encodeURIComponent(item.sid)));
  else assert.equal(title.href, undefined);
  assert.equal(card.childNodes[1].textContent, item.host || input.machine);
  assert.equal(card.childNodes[2].textContent, item.detail);
  assert.equal(card.childNodes[3].textContent, item.hint);
}
assert.equal(cards[1].childNodes[0].childNodes[0].textContent, "<script>example</script>");
assert.equal(elements.get("needs-count").textContent, input.counts.needs);
input.needs = []; input.counts.needs = 0;
vm.runInContext("drawModel();", context);
assert.match(elements.get("content").childNodes[0].textContent, /Nobody needs you/);
assert.equal(elements.get("needs-count").textContent, 0);
process.stdout.write("queue rendering passed\n");
