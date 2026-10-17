"use strict";
// Exercise polling and text rendering with fetch/timer controls; no browser or packages needed.
const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const {join} = require("node:path");
const {test} = require("node:test");
const vm = require("node:vm");
const source = readFileSync(join(__dirname, "../web/app.js"), "utf8");

function element(tag = "div") {
  const listeners = new Map();
  return {tag, textContent: "", childNodes: [], hidden: false, value: "", dataset: {},
    classList: {toggle() {}}, addEventListener(type, fn) { listeners.set(type, fn); }, querySelectorAll: () => [],
    trigger(type) { return listeners.get(type)?.(); }, closest() { return null; },
    hasAttribute(name) { return name === "data-lever" && Boolean(this.dataset.lever); },
    showModal() { this.open = true; },
    append(...children) { this.childNodes.push(...children); },
    replaceChildren(...children) { this.childNodes = children; }};
}
function page(fetch, hash = "") {
  const elements = new Map();
  const get = (id) => {
    if (!elements.has(id)) elements.set(id, element());
    return elements.get(id);
  };
  get("connection").textContent = "Connecting…";
  const document = {hidden: false, getElementById: get,
    querySelectorAll: (selector) => selector === "button" ? [...elements.values()].flatMap(descendants).filter((n) => n.tag === "button") : [],
    addEventListener() {},
    createElement: element, createDocumentFragment: () => element("fragment")};
  let tick;
  const location = {hash};
  const context = vm.createContext({document, fetch, URLSearchParams, location,
    window: {addEventListener() {}}, setInterval(fn, ms) { assert.equal(ms, 5000); tick = fn; }});
  vm.runInContext(source, context);
  return {get, document, tick: () => tick(), settled: () => vm.runInContext("refreshing", context),
    navigate(hash) { location.hash = hash; vm.runInContext("navigate()", context); },
    confirm(accepted) { const dialog = get("confirm-dialog"); dialog.returnValue = accepted ? "confirm" : "cancel";
      dialog.open = false; dialog.trigger("close"); }};
}
function response(status, body) {
  return {ok: status === 200, status, json: async () => body};
}
function model() {
  return {machine: "hub", groups: {}, hosts: [], needs: [], counts: {needs: 0, needs_urgent: 0}, writes: true};
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
  assert.equal(p.get("needs-count").textContent, 0);
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
  assert.match(p.get("connection").textContent, /^Updated /);
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

  const notices = ["laptop: Not answering. Last seen: 5m ago.", "server1: Connection refused. Last seen: never seen."];
  assert.deepEqual(content.filter((n) => n.className === "notice").map((n) => n.textContent), notices);
  for (const host of ["laptop", "server1"]) {
    assert.equal(content.map((n) => n.textContent).join("\n").split(host).length - 1, 1);
  }
  assert.equal(content.some((n) => n.textContent === "hub"), false);
  // The ordered queue uses per-item machine labels instead of machine headings.
  // A cached item must not hide its machine's specific connection error.
  data.needs = [
    {kind: "amail_approval", host: "laptop", sid: null, title: "a-mail 2 approvals", detail: "Counts only",
      since: null, hint: "Answer in Roost or the a-mail app."},
    {kind: "waiting", host: null, sid: "beta-22", title: "beta", detail: "Done",
      since: 60, hint: "Continue in that session."},
  ];
  data.counts = {needs: 2, needs_urgent: 1};
  await p.tick();
  const queued = descendants(p.get("content"));
  assert.deepEqual(queued.filter((n) => n.className === "meta").map((n) => n.textContent), ["laptop", "hub"]);
  assert.deepEqual(queued.filter((n) => n.tag === "strong" || n.tag === "a").map((n) => n.textContent),
    ["a-mail 2 approvals", "beta"]);
  assert.equal(queued.filter((n) => n.textContent === "hub").length, 1);
  for (const notice of notices) assert.equal(queued.filter((n) => n.textContent === notice).length, 1);
  assert.equal(p.get("needs-count").textContent, 2);
  assert.match(p.get("connection").textContent, /^Updated /);
});

function jobsModel() {
  return {...model(), jobs: {
    local: {ok: true, error: null, jobs: [
      {name: "failed", failing: true, state: "failed", last_exit: 1, purpose: "<script>job output</script>"},
      {name: "working", running: true, state: "running"},
      {name: "due-next", next_run: 1900000000, schedule: "every 300s"},
      {name: "expired", expired: true, state: "EXPIRED"},
    ], apps: [{name: "dashboard", status: "running", health: "healthy", url: "https://example.com"}]},
    server1: {ok: false, error: "jobs: host unavailable", jobs: [{name: "remote-failure", failing: true}], apps: []},
  }};
}
const flush = () => new Promise((resolve) => setImmediate(resolve));

test("Jobs view renders per host in model order, treats content as text, and labels expired and stale rows", async () => {
  const p = page(async () => response(200, jobsModel()), "#jobs");
  await p.settled();
  const content = descendants(p.get("content"));
  assert.equal(p.get("jobs-link").hidden, false);
  assert.deepEqual(content.filter((n) => n.tag === "a").map((n) => n.textContent),
    ["failed", "working", "due-next", "expired", "remote-failure"]);
  assert.deepEqual(content.filter((n) => n.tag === "h2").map((n) => n.textContent), ["hub", "server1"]);
  assert.ok(content.some((n) => n.textContent === "<script>job output</script>" && n.tag === "p"));
  assert.ok(content.some((n) => n.textContent === "Expired"));
  assert.ok(content.some((n) => n.textContent === "App · dashboard"));
  assert.ok(content.some((n) => n.textContent.startsWith("Due approximately")));
  assert.ok(content.some((n) => n.textContent.includes("Unavailable: jobs: host unavailable")));
  assert.equal(content.filter((n) => n.tag === "button").length, 4); // stale remote cannot run
});

test("Run now previews, cancels without mutation, and only executes after the existing confirm dialog", async () => {
  const calls = [], data = jobsModel();
  data.jobs.server1.ok = true; data.jobs.server1.error = null;
  const p = page(async (path, options) => {
    if (path === "/api/web/model") return response(200, data);
    const args = JSON.parse(options.body).arguments;
    calls.push({path, args});
    return response(200, {ok: true, text: args.dry_run ? "run job remote-failure now on server1" : "triggered remote-failure on server1"});
  }, "#jobs");
  await p.settled();
  const run = () => descendants(p.get("content")).filter((n) => n.tag === "button").at(-1).trigger("click");
  let pending = run();
  await flush();
  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0], {path: "/api/web/tools/atc_run_job", args: {host: "server1", job: "remote-failure", dry_run: true}});
  assert.equal(p.get("confirm-dialog").open, true);
  assert.match(p.get("confirm-summary").textContent, /remote-failure.*server1/);
  p.confirm(false);
  await pending; await p.settled();
  assert.equal(calls.length, 1);
  assert.match(p.get("result").textContent, /Cancelled/);
  pending = run();
  await flush();
  assert.equal(calls.length, 2);
  p.confirm(true);
  await pending; await p.settled();
  assert.equal(calls.length, 3);
  assert.deepEqual(calls[2].args, {host: "server1", job: "remote-failure", dry_run: false});
  assert.match(p.get("result").textContent, /triggered remote-failure on server1/);
});

test("failing needs items open job status, reads show log as text, and read-only jobs disable Run", async () => {
  const data = jobsModel();
  data.writes = false;
  data.needs = [{kind: "job", host: "local", job: "failed", title: "failed", detail: "Last exit 1", hint: "Open Jobs"}];
  data.counts.needs = 1;
  const calls = [];
  const p = page(async (path) => {
    calls.push(path);
    return response(200, path === "/api/web/model" ? data : {text: "Recent log: <script>untrusted</script>"});
  });
  await p.settled();
  const link = descendants(p.get("content")).find((n) => n.tag === "a");
  assert.equal(link.href, "#job?job=failed&host=local");
  p.navigate(link.href);
  await p.settled();
  const content = descendants(p.get("content"));
  assert.ok(calls.includes("/api/web/job/failed?host=local"));
  assert.ok(content.some((n) => n.tag === "pre" && n.textContent === "Recent log: <script>untrusted</script>"));
  assert.equal(content.find((n) => n.tag === "button").disabled, true);
  assert.equal(p.get("session-actions").hidden, true);
});

test("missing loom hides the Jobs navigation entry", async () => {
  const p = page(async () => response(200, model()));
  await p.settled();
  assert.equal(p.get("jobs-link").hidden, true);
});
