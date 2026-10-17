"use strict";
// Exercise polling and text rendering with fetch/timer controls; no browser or packages needed.
const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const {join} = require("node:path");
const {test} = require("node:test");
const vm = require("node:vm");
const source = readFileSync(join(__dirname, "../web/app.js"), "utf8");

function element(tag = "div") {
  return {tag, textContent: "", childNodes: [], hidden: false, value: "", dataset: {},
    classList: {toggle() {}}, addEventListener() {}, querySelectorAll: () => [],
    append(...children) { this.childNodes.push(...children); },
    replaceChildren(...children) { this.childNodes = children; }};
}
function page(fetch) {
  const elements = new Map();
  const get = (id) => {
    if (!elements.has(id)) elements.set(id, element());
    return elements.get(id);
  };
  get("connection").textContent = "Connecting…";
  const document = {hidden: false, getElementById: get, querySelectorAll: () => [], addEventListener() {},
    createElement: element, createDocumentFragment: () => element("fragment")};
  let tick;
  const context = vm.createContext({document, fetch, URLSearchParams, location: {hash: ""},
    window: {addEventListener() {}}, setInterval(fn, ms) { assert.equal(ms, 5000); tick = fn; }});
  vm.runInContext(source, context);
  return {get, document, tick: () => tick(), settled: () => vm.runInContext("refreshing", context)};
}
function response(status, body) {
  return {ok: status === 200, status, json: async () => body};
}
function model() {
  return {machine: "hub", groups: {}, hosts: [], writes: true};
}
function descendants(root) {
  return [root, ...root.childNodes.flatMap(descendants)];
}

test("initial network/proxy failures stay visible and retry through sign-in to recovery", async () => {
  let calls = 0;
  const p = page(async () => {
    calls++;
    if (calls === 1) throw new TypeError("Network unavailable");
    if (calls === 2) return {ok: false, status: 502, json: async () => { throw new SyntaxError("Proxy sent HTML"); }};
    if (calls === 3) return response(401, {detail: "Sign in with a passkey"});
    return response(200, model());
  });

  await p.settled();
  assert.match(p.get("connection").textContent, /Can't reach atc, retrying/);
  await p.tick();
  assert.match(p.get("connection").textContent, /Can't reach atc, retrying/);
  await p.tick();
  assert.equal(p.get("connection").textContent, "Signed out");
  assert.equal(p.get("signin").hidden, false);
  await p.tick();

  assert.equal(calls, 4);
  assert.match(p.get("connection").textContent, /^Updated /);
  assert.equal(p.get("signin").hidden, true);
  assert.equal(p.get("workspace").hidden, false);
});

test("retry polling preserves one request in flight and pauses when hidden", async () => {
  let calls = 0, resolve;
  const p = page(() => {
    calls++;
    return new Promise((done) => { resolve = done; });
  });

  const first = p.settled();
  assert.equal(p.tick(), first);
  assert.equal(p.tick(), first);
  assert.equal(calls, 1);
  resolve(response(200, model()));
  await first;
  p.document.hidden = true;
  await p.tick();

  assert.equal(calls, 1);
});

test("Needs you names the hub and an unavailable host once, retaining specific errors", async () => {
  const data = model();
  data.hosts = [{host: "laptop", ok: false, age: 300, error: "Not answering"},
    {host: "server1", ok: false, age: null, error: "Connection refused."}];
  const p = page(async () => response(200, data));

  await p.settled();
  const content = descendants(p.get("content"));

  assert.deepEqual(content.filter((n) => n.tag === "h2").map((n) => n.textContent), ["laptop", "server1"]);
  assert.deepEqual(content.filter((n) => n.className === "notice").map((n) => n.textContent),
    ["Not answering. Last seen: 5m ago.", "Connection refused. Last seen: never seen."]);
  // Needs you omits an empty hub; a waiting session makes its heading visible.
  data.groups.waiting = [{sid: "beta-22", name: "beta", group: "waiting", said: "Done"}];
  await p.tick();
  assert.equal(descendants(p.get("content")).find((n) => n.tag === "h2").textContent, "hub");
});
