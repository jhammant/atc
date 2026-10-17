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
    classList: {toggle() {}},
    addEventListener(name, fn) { listeners.set(name, fn); },
    fire(name) { return listeners.get(name)?.(); }, querySelectorAll: () => [],
    append(...children) { this.childNodes.push(...children); },
    replaceChildren(...children) { this.childNodes = children; }};
}
function page(fetch, credentials = {}) {
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
    navigator: {credentials}, atob, btoa,
    window: {addEventListener() {}, isSecureContext: true, PublicKeyCredential: function () {}}, setInterval(fn, ms) { assert.equal(ms, 5000); tick = fn; }});
  vm.runInContext(source, context);
  return {get, document, tick: () => tick(), settled: () => vm.runInContext("refreshing", context),
    navigate(hash) { context.location.hash = hash; vm.runInContext("navigate()", context); }};
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

function approvalItem(host = null) {
  return {kind: "approval", id: "Abcdef1234567890_", host, sid: null, title: "Approval Abcdef1234567890_",
    summary: "Type <script>example</script> into beta", detail: "Type <script>example</script> into beta",
    caller: "alpha-11", client: "autonomous-client", expires: 1800000000, hint: "Approve with a passkey."};
}
function acceptDialog(p) {
  const dialog = p.get("confirm-dialog");
  dialog.showModal = () => { dialog.returnValue = "confirm"; dialog.fire("close"); };
}
function credential() {
  const bytes = () => new Uint8Array([1, 2, 3]).buffer;
  return {id: "AQID", rawId: bytes(), type: "public-key", response: {
    clientDataJSON: bytes(), authenticatorData: bytes(), signature: bytes(), userHandle: null}};
}

test("approval cards show owner, machine and expiry, then request a fresh assertion for that action", async () => {
  const data = model(), item = approvalItem("laptop"), calls = [];
  data.needs = [item]; data.counts = {needs: 1, needs_urgent: 1};
  const path = "/api/web/approvals/" + item.id + "/";
  const p = page(async (url, options) => {
    const body = options.body ? JSON.parse(options.body) : undefined;
    calls.push({url, body});
    if (url.endsWith("/model")) return response(200, data);
    if (url === path + "options") return response(200, {challenge: "bm9uY2U", userVerification: "required",
      allowCredentials: [{type: "public-key", id: "AQID"}], rpId: "atc.example.com"});
    assert.equal(url, path + "approve");
    assert.equal(body.challenge, "bm9uY2U"); assert.equal(body.host, "laptop");
    assert.equal(body.response.signature, "AQID");
    assert.equal(body.id, "AQID"); assert.equal(body.rawId, "AQID");
    return response(200, {ok: true, text: "Approval approved."});
  }, {async get({publicKey}) {
    calls.push({url: "passkey"});
    assert.equal(publicKey.userVerification, "required");
    assert.equal(Buffer.from(publicKey.challenge).toString(), "nonce");
    assert.equal(Buffer.from(publicKey.allowCredentials[0].id).toString("hex"), "010203");
    return credential();
  }});
  await p.settled();
  acceptDialog(p);
  const cards = descendants(p.get("content"));
  assert.ok(cards.some((n) => n.textContent === item.summary));
  assert.ok(cards.some((n) => n.textContent === "laptop"));
  assert.ok(cards.some((n) => /Caller: alpha-11 · autonomous-client · Expires/.test(n.textContent)));

  await cards.find((n) => n.textContent === "Approve with passkey").fire("click");

  assert.deepEqual(calls.filter((c) => !c.url.endsWith("/model")).map((c) => c.url),
    [path + "options", "passkey", path + "approve"]);
  assert.deepEqual(calls.find((c) => c.url === path + "options").body, {host: "laptop"});
  assert.equal(p.get("result").textContent, "Approval approved.");
  assert.match(p.get("confirm-summary").textContent, /Caller: alpha-11 · laptop · Expires/);
});

test("denying an approval only uses the signed-in session", async () => {
  const data = model(), item = approvalItem(), calls = [];
  data.needs = [item];
  const p = page(async (url, options) => {
    if (url.endsWith("/model")) return response(200, data);
    calls.push({url, body: JSON.parse(options.body)});
    return response(200, {ok: true, text: "Approval denied."});
  }, {get() { assert.fail("deny must not request a passkey"); }});
  await p.settled();
  acceptDialog(p);

  await descendants(p.get("content")).find((n) => n.textContent === "Deny").fire("click");

  assert.deepEqual(calls, [{url: "/api/web/approvals/" + item.id + "/deny", body: {host: "local"}}]);
  assert.equal(p.get("result").textContent, "Approval denied.");
});

test("cancelled passkey and failed approval request never send a decision or retry it", async () => {
  for (const fault of ["cancel", "options", "lost decision"]) {
    const data = model(), item = approvalItem(), calls = [];
    data.needs = [item];
    const p = page(async (url) => {
      if (url.endsWith("/model")) return response(200, data);
      calls.push(url);
      if (fault === "options") return response(409, {text: "Approval expired"});
      if (url.endsWith("/options")) return response(200, {challenge: "bm9uY2U", userVerification: "required"});
      throw new TypeError("Connection lost");
    }, {async get() {
      if (fault === "cancel") throw new Error("Passkey cancelled");
      return credential();
    }});
    await p.settled();
    acceptDialog(p);

    await descendants(p.get("content")).find((n) => n.textContent === "Approve with passkey").fire("click");
    await p.tick();

    assert.equal(calls.filter((url) => url.endsWith("/approve")).length, fault === "lost decision" ? 1 : 0);
    assert.equal(calls.filter((url) => url.endsWith("/options")).length, 1);
    assert.match(p.get("result").textContent, /cancelled|expired|Connection lost/);
  }
});

test("Later has a next-day preview, an ordered full view and text-only plug-in data", async () => {
  const data = model(), now = 1800000000;
  data.later = [
    {title: "Overdue", when: now - 60, in: -60, source: "cards", kind: "study", host: null, sid: null},
    {title: "Wake soon", when: now + 600, in: 600, source: "alpha", kind: "wakeup", host: null, sid: "alpha-11"},
    {title: "<script>check</script>", when: now + 7200, in: 7200, source: "<b>cards</b>", kind: "study", host: "laptop", sid: null, detail: "<img src=x>", stale: true},
    {title: "Tomorrow", when: now + 90000, in: 90000, source: "notes", kind: "reminder", host: null, sid: null},
    {title: "Undated note", when: null, in: null, source: "notes", kind: "reminder", host: null, sid: null},
  ];
  data.needs = data.later.slice(0, 2).map((item) => ({...item, kind: "later", hint: "Check the source."}));
  data.counts.needs = 2;
  data.later_errors = ["Later plug-in bad failed or returned invalid data; skipped."];
  const p = page(async () => response(200, data));
  await p.settled();

  let nodes = descendants(p.get("content"));
  assert.deepEqual(nodes.filter((n) => n.tag === "strong" || n.tag === "a").map((n) => n.textContent),
    ["Overdue", "Wake soon", "<script>check</script>"]);
  assert.ok(nodes.some((n) => n.textContent === "Later · due within an hour"));
  assert.ok(nodes.some((n) => n.textContent === "Later · next 24 hours"));
  assert.equal(nodes.find((n) => n.textContent === "Wake soon").href, "#session?sid=alpha-11&host=local");
  assert.equal(p.get("needs-count").textContent, 2);

  p.navigate("#later");
  await p.settled();
  nodes = descendants(p.get("content"));
  assert.equal(p.get("view-title").textContent, "Later");
  assert.deepEqual(nodes.filter((n) => n.tag === "strong" || n.tag === "a").map((n) => n.textContent), data.later.map((n) => n.title));
  assert.ok(nodes.some((n) => /1m overdue/.test(n.textContent)));
  assert.ok(nodes.some((n) => n.textContent === "Undated"));
  assert.ok(nodes.some((n) => n.textContent === "laptop · <b>cards</b>"));
  assert.ok(nodes.some((n) => n.textContent === "<img src=x>"));
  assert.ok(nodes.some((n) => n.textContent === "Last seen; this machine isn't answering."));
  assert.equal(nodes.filter((n) => n.textContent === data.later_errors[0]).length, 1);
  assert.equal(nodes.some((n) => ["script", "img", "b"].includes(n.tag)), false);
});

test("Later's empty view keeps remote plug-in failures visible", async () => {
  const data = model();
  data.later = [];
  data.hosts = [{host: "laptop", ok: true, later_errors: ["Later plug-in cards failed; skipped."]}];
  const p = page(async () => response(200, data));
  await p.settled();
  p.navigate("#later");
  await p.settled();

  const nodes = descendants(p.get("content"));
  assert.ok(nodes.some((n) => n.textContent === "Nothing scheduled for later."));
  assert.ok(nodes.some((n) => n.textContent === "laptop: Later plug-in cards failed; skipped."));
});
