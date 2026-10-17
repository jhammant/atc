"use strict";
const $ = (id) => document.getElementById(id);
const groups = ["blocked", "waiting", "working", "parked", "background", "other"];
const labels = {needs: "Needs you", fleet: "Fleet", comms: "Comms", quota: "Quota", closed: "Closed", account: "Passkeys", session: "Session",
  blocked: "Blocked on a prompt", waiting: "Waiting for you", working: "Working", parked: "Parked", background: "Background", other: "Other"};
let model = null, signedIn = false, busy = false, refreshing = null, generation = 0;
let view = "needs", selected = null;
const node = (tag, text, cls) => {
  const n = document.createElement(tag);
  if (text !== undefined && text !== null) n.textContent = String(text);
  if (cls) n.className = cls;
  return n;
};
function status(text, error = false) { $("result").textContent = text; $("result").classList.toggle("error", error); }
function showSignin() {
  signedIn = false; model = null; generation++;
  $("signin").hidden = false; $("workspace").hidden = true;
  $("content").replaceChildren(); $("passkeys").replaceChildren();
  $("message").value = ""; $("new-name").value = "";
  document.title = "atc · Sign in"; $("connection").textContent = "Signed out";
}
async function api(path, body) {
  const options = {credentials: "same-origin", cache: "no-store", headers: {}};
  if (body !== undefined) {
    options.method = "POST"; options.headers = {"Content-Type": "application/json", "X-Atc-Web": "1"};
    options.body = JSON.stringify(body);
  }
  const response = await fetch("/api/web/" + path, options);
  const result = await response.json();
  if (!response.ok) {
    if (response.status === 401 && !path.startsWith("auth/")) showSignin();
    const error = new Error(result.text || result.detail || `Request failed (${response.status})`);
    error.status = response.status;
    throw error;
  }
  return result;
}
function decode(value) {
  const raw = atob(value.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(value.length / 4) * 4, "="));
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}
function encode(value) {
  return btoa(String.fromCharCode(...new Uint8Array(value))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
async function authenticate(kind, form) {
  if (!window.isSecureContext || !navigator.credentials || !window.PublicKeyCredential) {
    throw new Error("Passkeys need HTTPS and a browser that supports WebAuthn.");
  }
  const fields = form ? Object.fromEntries(new FormData(form)) : {};
  const options = await api(`auth/${kind}/options`, fields);
  const challenge = options.challenge;
  options.challenge = decode(challenge);
  for (const key of ["allowCredentials", "excludeCredentials"]) {
    if (options[key]) options[key] = options[key].map((c) => ({...c, id: decode(c.id)}));
  }
  if (options.user) options.user.id = decode(options.user.id);
  const credential = kind === "register" ? await navigator.credentials.create({publicKey: options}) : await navigator.credentials.get({publicKey: options});
  if (!credential) throw new Error("No passkey was returned. Try again.");
  const response = {clientDataJSON: encode(credential.response.clientDataJSON)};
  for (const field of ["attestationObject", "authenticatorData", "signature", "userHandle"]) {
    if (credential.response[field]) response[field] = encode(credential.response[field]);
  }
  await api(`auth/${kind}/finish`, {challenge, id: credential.id, rawId: encode(credential.rawId), type: credential.type, response});
  if (form) form.reset();
  signedIn = true; $("signin").hidden = true; $("workspace").hidden = false;
  status(kind === "register" ? "Passkey added." : "Signed in.");
}
function confirmAction(summary) {
  $("confirm-summary").textContent = summary;
  const dialog = $("confirm-dialog"); dialog.returnValue = "cancel";
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "confirm"), {once: true});
    dialog.showModal();
  });
}
function disableActions(disabled) {
  document.querySelectorAll("button").forEach((button) => {
    if (!button.closest("dialog")) button.disabled = disabled || (button.hasAttribute("data-lever") && model && !model.writes);
  });
}
async function action(fn) {
  if (busy) return;
  busy = true; disableActions(true); status("");
  try {
    if (refreshing) await refreshing;
    await fn();
  } catch (error) { status(error.message, true); }
  finally { busy = false; disableActions(false); if (signedIn) refresh(); }
}
async function lever(tool, args) {
  const preview = await api("tools/" + tool, {arguments: {...args, dry_run: true}});
  if (!preview.ok) { status(preview.text, true); return; }
  if (!await confirmAction(preview.text)) { status("Cancelled. Nothing was done."); return; }
  const result = await api("tools/" + tool, {arguments: {...args, dry_run: false}});
  status(result.text, !result.ok);
}
function age(seconds) {
  if (seconds === null || seconds === undefined) return "never seen";
  if (seconds < 60) return `${Math.floor(seconds)}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  return `${Math.floor(seconds / 3600)}h ago`;
}
function when(ts) { return ts ? new Date(ts * 1000).toLocaleString() : "Time unknown"; }
function sessionURL(s, host) { return "#session?" + new URLSearchParams({sid: s.sid, host: host || "local"}); }
function helpers(s, parent) {
  const list = node("ul", null, "children");
  for (const team of s.teams || []) {
    const row = node("li", `Workflow ${team.name}`);
    if (team.marks) row.append(node("p", team.marks.map(([state, phase]) => `${phase}: ${state === "now" ? "current" : state}`).join(" / ")));
    else if (team.phases) row.append(node("p", team.phases.join(" / ")));
    for (const helper of team.agents || []) row.append(node("p", helperText(helper)));
    list.append(row);
  }
  for (const helper of s.helpers || []) list.append(node("li", helperText(helper)));
  for (const worker of s.workers || []) list.append(node("li", `Worker ${worker.pid} · ${worker.command || worker.tool || ""}`));
  if (list.childNodes.length) parent.append(list);
}
function helperText(h) { return `${h.label || h.name || h.id || "Helper"} · ${h.finished ? "finished" : h.working ? "working" : "waiting"}${h.model ? " · " + h.model : ""} — ${h.doing || ""}`; }
function sessionCard(s, host, stale = false) {
  const card = node("article", null, "session " + (groups.includes(s.group) ? s.group : "other"));
  const head = node("header"), link = node("a", s.display_name || s.title || s.name || s.sid);
  link.href = sessionURL(s, host);
  head.append(link, node("span", `${labels[s.group] || s.state || "Unknown"}${stale ? " (last seen)" : ""}`, "state"));
  card.append(head, node("p", `${host || model.machine || "Hub"} · ${s.cwd || ""}`, "meta"));
  card.append(node("p", s.group === "waiting" ? s.said || s.doing : s.doing, "words"));
  if (s.group === "blocked") card.append(node("p", "Answer this prompt in that session.", "notice"));
  if (s.alert) card.append(node("p", s.alert, "notice"));
  helpers(s, card);
  return card;
}
function machines() {
  return [{host: model.machine || "Hub", local: true, ok: true, groups: model.groups || {}, agents: model.agents || [], swarms: model.swarms || []}, ...(model.hosts || [])];
}
function swarm(s) {
  const box = node("details"), title = node("summary", `${s.name || s.id} · ${s.state || ""}`);
  box.append(title);
  const list = node("ul", null, "children");
  for (const u of s.units || []) {
    list.append(node("li", `${u.id || u.name} · ${u.state || ""} · ${u.arm || u.model || u.pool || ""}${u.pid ? " · pid " + u.pid : ""}`));
    if (u.last_message || u.last) list.append(node("li", typeof (u.last_message || u.last) === "string" ? u.last_message || u.last : (u.last_message || u.last).text || ""));
  }
  if (s.last_message) box.append(node("p", `${s.last_message.from}: ${s.last_message.text}`, "words"));
  box.append(list); return box;
}
function fleetView() {
  const fragment = document.createDocumentFragment();
  for (const machine of machines()) {
    const box = node("section", null, "machine");
    box.append(node("h2", machine.host));
    if (!machine.ok) {
      const error = String(machine.error || "Not answering").trim().replace(/[.!]+$/, "");
      box.append(node("p", `${error}. Last seen: ${age(machine.age)}.`, "notice"));
    }
    for (const group of groups) {
      const sessions = (machine.groups || {})[group] || [];
      if (sessions.length) box.append(node("h3", `${labels[group]} (${sessions.length})`));
      for (const s of sessions) box.append(sessionCard(s, machine.local ? "local" : machine.host, !machine.ok));
    }
    if ((machine.swarms || []).length) box.append(node("h3", "Route swarms"));
    for (const s of machine.swarms || []) box.append(swarm(s));
    if ((machine.agents || []).length) box.append(node("h3", "Other agent processes"));
    for (const a of machine.agents || []) box.append(node("p", `${a.tool || "Agent"} · pid ${a.pid} · ${a.parent || ""} · ${a.command || ""}`, "words"));
    fragment.append(box);
  }
  return fragment;
}
function needsView() {
  const fragment = document.createDocumentFragment();
  for (const item of model.needs || []) {
    const card = node("article", null, "session " + (groups.includes(item.kind) ? item.kind : "other"));
    const head = node("header"), title = node(item.sid ? "a" : "strong", item.title);
    if (item.sid) title.href = sessionURL(item, item.host);
    head.append(title);
    card.append(head, node("p", item.host || model.machine || "Hub", "meta"),
      node("p", item.detail, "words"), node("p", item.hint, "notice"));
    fragment.append(card);
  }
  if (!(model.needs || []).length) fragment.append(node("p", "Nobody needs you. The queue is empty."));
  for (const machine of model.hosts || []) {
    if (!machine.ok) {
      const error = String(machine.error || "Not answering").trim().replace(/[.!]+$/, "");
      fragment.append(node("p", `${machine.host}: ${error}. Last seen: ${age(machine.age)}.`, "notice"));
    }
  }
  return fragment;
}
function comms(items) {
  const fragment = document.createDocumentFragment();
  for (const c of items) {
    const row = node("details", null, "comms-row");
    const summary = node("summary", `${c.from} → ${c.to}: ${c.summary || "Message"}`);
    row.append(summary, node("p", when(c.ts), "meta"), node("pre", c.body || c.summary || ""));
    fragment.append(row);
  }
  if (!items.length) fragment.append(node("p", "No messages match this view."));
  return fragment;
}
function button(text, fn, isLever = true) {
  const b = node("button", text); b.type = "button";
  if (isLever) b.dataset.lever = "dynamic";
  b.addEventListener("click", () => action(fn)); return b;
}
function drawModel() {
  const count = model.counts.needs;
  $("needs-count").textContent = count; document.title = `(${count}) atc · ${labels[view] || "Needs you"}`;
  $("read-only").hidden = model.writes;
  if (view === "session" || view === "account") return;
  const content = $("content"), frag = document.createDocumentFragment();
  if (view === "needs") frag.append(needsView());
  if (view === "fleet") frag.append(fleetView());
  if (view === "comms") {
    const filter = $("filter").value.toLocaleLowerCase();
    frag.append(comms((model.comms || []).filter((c) => `${c.from} ${c.to}`.toLocaleLowerCase().includes(filter))));
  }
  if (view === "quota") {
    frag.append(node("pre", model.quota_text || "Quota is unavailable."));
    const q = model.quota || {};
    frag.append(node("h2", `Swarm cap: ${q.swarmCap === null ? "unavailable" : q.swarmCap}`), node("p", q.swarmCapPinned ? "Pinned for two hours" : "Automatic"));
    const controls = node("div", null, "actions");
    for (const [name, direction] of [["Lower", "down"], ["Raise", "up"], ["Auto", "auto"]]) controls.append(button(name, () => lever("atc_cap", {direction})));
    frag.append(controls);
  }
  if (view === "closed") {
    for (const c of model.closed || []) {
      const row = node("article", null, "session");
      row.append(node("h2", c.title || c.sid), node("p", `${c.cwd || ""} · Closed ${when(c.closed_at)}`, "meta"),
        node("pre", c.last_words || ""), node("p", c.next_step ? "Next: " + c.next_step : ""));
      if (c.repo) row.append(node("p", gitText(c.repo), "muted"));
      row.append(button("Restore", () => lever("atc_restore", {session: c.sid}))); frag.append(row);
    }
    if (!frag.childNodes.length) frag.append(node("p", "No closed sessions in the last 14 days."));
  }
  replaceContent(content, frag);
}
function replaceContent(target, fragment) {
  const open = new Set([...target.querySelectorAll("details[open]")].map((d) => d.querySelector("summary").textContent));
  target.replaceChildren(fragment);
  target.querySelectorAll("details").forEach((d) => { d.open = open.has(d.querySelector("summary").textContent); });
}
function gitText(repo) { return `${repo.branch || "Git"}: ${repo.dirty || 0} uncommitted files, ${repo.unpushed || 0} unpushed commits`; }
function drawSession(s) {
  const frag = document.createDocumentFragment();
  $("view-title").textContent = s.display_name || s.name || "Session";
  if (s.remote) frag.append(node("p", `Remote snapshot · ${s.ok ? "read" : "last seen"} ${age(s.age)}. Step-by-step activity and full last words are available on that machine.`, "notice"));
  const facts = node("dl");
  for (const [key, value] of [["State", s.state], ["Doing", s.doing], ["Folder", s.cwd], ["Session", s.sid], ["Machine", s.host], ["Process", s.pid], ["Terminal", s.tty], ["Version", s.version], ["Git", s.repo ? gitText(s.repo) : "Unavailable"]]) {
    facts.append(node("dt", key), node("dd", value || "—"));
  }
  frag.append(facts);
  if (s.group === "blocked") frag.append(node("p", "Answer this prompt in that session. atc cannot type an answer here.", "notice"));
  if (s.alert) frag.append(node("p", s.alert, "notice"));
  frag.append(node("h2", "Last words"), node("pre", s.last_words || "No words yet."));
  helpers(s, frag);
  frag.append(node("h2", "Recent steps"));
  for (const step of s.steps || []) frag.append(node("p", `${when(step.ts)} · ${step.agent} · ${step.tool}: ${step.detail}`, "words"));
  if (!(s.steps || []).length) frag.append(node("p", "No recent steps available.", "muted"));
  frag.append(node("h2", "Recent messages"), comms(s.messages || []));
  replaceContent($("content"), frag);
}
async function drawAccount() {
  const data = await api("passkeys");
  if (view !== "account") return;
  const fragment = document.createDocumentFragment();
  fragment.append(node("p", `Signed in with ${data.name}.`));
  for (const key of data.passkeys) {
    const row = node("article", null, "session");
    row.append(node("h3", key.name), node("p", `Added ${key.created} · Last used ${key.last_used || "not yet"}`, "meta"));
    row.append(button("Remove", async () => {
      const last = data.passkeys.length === 1;
      if (!await confirmAction(`Remove ${key.name}?${last ? " This is your last passkey. You will need atc web-enroll on the hub to sign in again." : " Sessions using it will be signed out."}`)) return;
      await api("passkeys/remove", {id: key.id, confirm_last: last}); status("Passkey removed.");
    }, false));
    fragment.append(row);
  }
  $("passkeys").replaceChildren(fragment);
}
function navigate() {
  status("");
  const [name, query] = location.hash.slice(1).split("?");
  view = ["needs", "fleet", "comms", "quota", "closed", "account", "session"].includes(name) ? name : "needs";
  const args = new URLSearchParams(query || "");
  const next = view === "session" ? {session: args.get("sid") || "", host: args.get("host") || "local"} : null;
  if (JSON.stringify(next) !== JSON.stringify(selected)) { $("message").value = ""; $("new-name").value = ""; }
  selected = next; generation++;
  $("view-title").textContent = labels[view];
  $("session-actions").hidden = view !== "session";
  $("account").hidden = view !== "account"; $("comms-filter").hidden = view !== "comms";
  $("content").replaceChildren();
  document.querySelectorAll("nav a").forEach((a) => {
    if (a.dataset.view === view) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  });
  if (model) drawModel();
  refresh();
}
function refresh() {
  if (document.hidden || busy) return Promise.resolve();
  if (refreshing) return refreshing;
  const current = generation;
  refreshing = (async () => {
    try {
      const next = await api("model");
      signedIn = true; $("signin").hidden = true; $("workspace").hidden = false; model = next;
      if (current !== generation) return;
      drawModel();
      if (view === "session" && selected) {
        const s = await api("session/" + encodeURIComponent(selected.session) + "?" + new URLSearchParams({host: selected.host}));
        if (current === generation) drawSession(s);
      } else if (view === "account") await drawAccount();
      $("connection").textContent = model.error ? "Reading failed · showing last snapshot" : "Updated " + new Date().toLocaleTimeString();
      disableActions(busy);
    } catch (error) {
      if (signedIn) { $("connection").textContent = "Connection lost · showing last snapshot"; status(error.message, true); }
      else if (error.status !== 401) $("connection").textContent = "Can't reach atc, retrying…";
    }
  })().finally(() => { refreshing = null; if (current !== generation && signedIn) refresh(); });
  return refreshing;
}
$("login").addEventListener("click", () => action(() => authenticate("login")));
for (const id of ["enroll-first", "enroll-more"]) $(id).addEventListener("submit", (e) => { e.preventDefault(); action(() => authenticate("register", e.target)); });
$("signout").addEventListener("click", () => action(async () => { await api("signout", {}); showSignin(); status("Signed out."); }));
$("message-form").addEventListener("submit", (e) => {
  e.preventDefault();
  if (selected) { const args = {...selected, text: $("message").value}; action(() => lever("atc_message", args)); }
});
$("rename-form").addEventListener("submit", (e) => {
  e.preventDefault();
  if (selected) { const args = {...selected, name: $("new-name").value}; action(() => lever("atc_rename", args)); }
});
document.querySelectorAll("#session-actions button[type=button]").forEach((b) => b.addEventListener("click", () => {
  if (selected) { const args = {...selected}; if (b.dataset.how) args.how = b.dataset.how; action(() => lever(b.dataset.lever, args)); }
}));
$("filter").addEventListener("input", () => { if (model) drawModel(); });
$("refresh").addEventListener("click", () => refresh());
window.addEventListener("hashchange", navigate);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
setInterval(refresh, 5000);
navigate();
