"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const state = { name: "Assistant", busy: false, events: null, tab: "reminders" };

// ---------- API ----------
async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || (opts.body ? "POST" : "GET"),
    headers: opts.body ? { "Content-Type": "application/json" } : {},
    body: opts.body ? JSON.stringify(opts.body) : undefined,
    credentials: "same-origin",
  });
  if (res.status === 401 && path !== "/api/login") { showLogin(); throw new Error("not logged in"); }
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  return res.json();
}

// ---------- Tiny, safe markdown ----------
function escapeHtml(s) {
  return s.replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function inline(s) {
  return s
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>")
    .replace(/~~([^~]+)~~/g, "<del>$1</del>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
    .replace(/(^|\s)(https?:\/\/[^\s<]+)/g, '$1<a href="$2" target="_blank" rel="noopener">$2</a>');
}
function markdown(src) {
  const lines = escapeHtml(src.trim()).split("\n");
  let html = "", list = null, para = [], code = null;
  const flushPara = () => { if (para.length) { html += `<p>${inline(para.join("<br>"))}</p>`; para = []; } };
  const closeList = () => { if (list) { html += `</${list}>`; list = null; } };
  for (const line of lines) {
    if (code !== null) {
      if (line.startsWith("```")) { html += `<pre><code>${code}</code></pre>`; code = null; } else code += line + "\n";
      continue;
    }
    if (line.startsWith("```")) { flushPara(); closeList(); code = ""; continue; }
    let m;
    if ((m = line.match(/^\s*[-*•]\s+(.*)/))) {
      flushPara(); if (list !== "ul") { closeList(); html += "<ul>"; list = "ul"; }
      html += `<li>${inline(m[1])}</li>`;
    } else if ((m = line.match(/^\s*\d+[.)]\s+(.*)/))) {
      flushPara(); if (list !== "ol") { closeList(); html += "<ol>"; list = "ol"; }
      html += `<li>${inline(m[1])}</li>`;
    } else if ((m = line.match(/^#{1,4}\s+(.*)/))) {
      flushPara(); closeList(); html += `<h4>${inline(m[1])}</h4>`;
    } else if (!line.trim()) {
      flushPara(); closeList();
    } else {
      closeList(); para.push(line);
    }
  }
  if (code !== null) html += `<pre><code>${code}</code></pre>`;
  flushPara(); closeList();
  return html;
}

// ---------- Auth ----------
function showLogin() {
  $("#app").hidden = true;
  $("#login").hidden = false;
  if (state.events) { state.events.close(); state.events = null; }
  setTimeout(() => $("#password").focus(), 50);
}

$("#login-form").addEventListener("submit", async e => {
  e.preventDefault();
  $("#login-error").textContent = "";
  try {
    await api("/api/login", { body: { password: $("#password").value } });
    $("#password").value = "";
    start();
  } catch (err) {
    $("#login-error").textContent = err.message;
  }
});

// ---------- Chat ----------
const messagesEl = $("#messages");
const input = $("#input");
const sendBtn = $("#send");

function fmtTime(iso) {
  const d = iso ? new Date(iso) : new Date();
  return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

function scrollDown(force) {
  const near = messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight < 160;
  if (force || near) messagesEl.scrollTop = messagesEl.scrollHeight;
}

function addMessage(role, text, opts = {}) {
  $("#empty").hidden = true;
  const wrap = document.createElement("div");
  wrap.className = `msg ${role === "user" ? "user" : "bot"}`;
  const via = opts.source === "discord" ? " · via Discord" : opts.source === "checkin" ? " · check-in" : "";
  if (role === "user") {
    wrap.innerHTML = `<div><div class="bubble"></div><div class="meta">${fmtTime(opts.at)}${via}</div></div>`;
    $(".bubble", wrap).textContent = text;
  } else {
    wrap.innerHTML = `<div class="orb" aria-hidden="true"></div>
      <div><div class="tools"></div><div class="bubble"></div><div class="meta">${fmtTime(opts.at)}${via}</div></div>`;
    $(".bubble", wrap).innerHTML = text ? markdown(text) : '<span class="typing"><i></i><i></i><i></i></span>';
    if (!$(".tools", wrap).children.length) $(".tools", wrap).hidden = true;
  }
  if (opts.source === "checkin") wrap.classList.add("checkin");
  messagesEl.appendChild(wrap);
  scrollDown(true);
  return wrap;
}

async function send(text) {
  text = text.trim();
  if (!text || state.busy) return;
  state.busy = true;
  input.value = "";
  autosize();
  updateSend();
  addMessage("user", text);
  const bot = addMessage("bot", "");
  bot.classList.add("thinking");
  const bubble = $(".bubble", bot);
  const tools = $(".tools", bot);
  let reply = "";
  let changed = false;

  try {
    const res = await fetch("/api/chat", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text }),
    });
    if (res.status === 401) { showLogin(); return; }
    if (!res.ok) throw new Error(`server error ${res.status}`);
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl); buf = buf.slice(nl + 1);
        if (!line.trim()) continue;
        const ev = JSON.parse(line);
        if (ev.type === "token") {
          reply += ev.text;
          bubble.innerHTML = markdown(reply) || '<span class="typing"><i></i><i></i><i></i></span>';
        } else if (ev.type === "tool") {
          const chip = document.createElement("span");
          chip.className = "tool-chip" + (ev.result.startsWith("Error") ? " err" : "");
          chip.textContent = ev.label;
          chip.title = ev.result;
          tools.appendChild(chip);
          tools.hidden = false;
          changed = true;
        } else if (ev.type === "done") {
          bubble.innerHTML = markdown(ev.text);
        } else if (ev.type === "error") {
          bot.classList.add("error");
          bubble.textContent = ev.text;
        }
        scrollDown();
      }
    }
  } catch (err) {
    bot.classList.add("error");
    bubble.textContent = `Something went wrong: ${err.message}`;
  } finally {
    bot.classList.remove("thinking");
    state.busy = false;
    updateSend();
    if (changed) refreshPanel();
    if (matchMedia("(hover: hover)").matches) input.focus();
  }
}

function autosize() {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 180) + "px";
}
function updateSend() { sendBtn.disabled = state.busy || !input.value.trim(); }

input.addEventListener("input", () => { autosize(); updateSend(); });
input.addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing && matchMedia("(hover: hover)").matches) {
    e.preventDefault();
    send(input.value);
  }
});
$("#composer").addEventListener("submit", e => { e.preventDefault(); send(input.value); });
$$(".chip").forEach(c => c.addEventListener("click", () => send(c.textContent)));

async function loadHistory() {
  const msgs = await api("/api/history");
  $$(".msg", messagesEl).forEach(m => m.remove());
  $("#empty").hidden = msgs.length > 0;
  for (const m of msgs) addMessage(m.role, m.content, { at: m.created_at, source: m.source });
  scrollDown(true);
}

// ---------- Side panel ----------
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}
function removeBtn(onClick, label = "Remove") {
  const b = el("button", "x", "×");
  b.title = label; b.setAttribute("aria-label", label);
  b.addEventListener("click", onClick);
  return b;
}

async function loadReminders() {
  const data = await api("/api/reminders");
  const ul = $("#reminder-list");
  ul.replaceChildren();
  if (!data.upcoming.length) ul.appendChild(el("li", "empty-note", "No upcoming reminders. Try “remind me tomorrow at 9 to call mom”."));
  for (const r of data.upcoming) {
    const li = el("li", "item");
    const body = el("div", "body");
    body.appendChild(el("div", "title", r.text));
    const sub = el("div", "sub", r.when);
    if (r.repeat) sub.appendChild(el("span", "rep", ` · ↻ ${r.repeat}`));
    body.appendChild(sub);
    li.append(body, removeBtn(async () => { await api(`/api/reminders/${r.id}`, { method: "DELETE" }); loadReminders(); }, "Cancel reminder"));
    ul.appendChild(li);
  }
  const recent = $("#recent-list");
  recent.replaceChildren();
  $("#recent-wrap").hidden = !data.recent.length;
  for (const r of data.recent) {
    const li = el("li", "item");
    const cb = el("input", "check"); cb.type = "checkbox"; cb.title = "Mark done";
    cb.addEventListener("change", async () => { await api(`/api/reminders/${r.id}/done`, { method: "POST" }); loadReminders(); });
    const body = el("div", "body");
    body.append(el("div", "title", r.text), el("div", "sub", `sent ${r.when}`));
    li.append(cb, body);
    recent.appendChild(li);
  }
  const badge = $("#badge");
  badge.hidden = !data.recent.length;
  badge.textContent = data.recent.length;
}

async function loadTodos() {
  const items = await api("/api/todos");
  const box = $("#todo-groups");
  box.replaceChildren();
  const groups = {};
  for (const t of items) (groups[t.list] ||= []).push(t);
  const names = Object.keys(groups).sort((a, b) => (a === "to-do" ? -1 : b === "to-do" ? 1 : a.localeCompare(b)));
  $("#list-names").replaceChildren(...names.map(n => { const o = el("option"); o.value = n; return o; }));
  if (!names.length) box.appendChild(el("div", "empty-note", "No lists yet. Try “add bread and eggs to groceries”."));
  for (const name of names) {
    const open = groups[name].filter(t => !t.done).length;
    const head = el("div", "group-title");
    head.append(el("span", "", name), el("span", "count", `${open} left`));
    box.appendChild(head);
    const ul = el("ul", "list");
    for (const t of groups[name]) {
      const li = el("li", "item" + (t.done ? " done" : ""));
      const cb = el("input", "check"); cb.type = "checkbox"; cb.checked = t.done;
      cb.addEventListener("change", async () => { await api(`/api/todos/${t.id}/toggle`, { method: "POST" }); loadTodos(); });
      const body = el("div", "body"); body.appendChild(el("div", "title", t.text));
      li.append(cb, body, removeBtn(async () => { await api(`/api/todos/${t.id}`, { method: "DELETE" }); loadTodos(); }));
      ul.appendChild(li);
    }
    box.appendChild(ul);
  }
  $("#clear-done").hidden = !items.some(t => t.done);
}

async function loadMemories() {
  const items = await api("/api/memories");
  const ul = $("#memory-list");
  ul.replaceChildren();
  if (!items.length) ul.appendChild(el("li", "empty-note", "Nothing yet. Tell me about yourself and I'll remember."));
  for (const m of items) {
    const li = el("li", "item");
    const body = el("div", "body"); body.appendChild(el("div", "title", m.fact));
    li.append(body, removeBtn(async () => { await api(`/api/memories/${m.id}`, { method: "DELETE" }); loadMemories(); }, "Forget"));
    ul.appendChild(li);
  }
}

async function loadGoals() {
  const goals = await api("/api/goals");
  const box = $("#goal-list");
  box.replaceChildren();
  if (!goals.length) box.appendChild(el("div", "empty-note", "No goals yet. Try “help me make a plan to run a 5K”."));
  for (const g of goals) {
    const card = el("div", "goal" + (g.status !== "active" ? " closed" : ""));
    const head = el("div", "goal-head");
    const title = el("div", "goal-title", g.title);
    const meta = el("div", "sub", [
      g.status === "done" ? "🎉 completed" : g.status === "dropped" ? "dropped" : `${g.done}/${g.total} steps`,
      g.target && g.status === "active" ? `target ${g.target}` : "",
    ].filter(Boolean).join(" · "));
    const titleWrap = el("div", "body"); titleWrap.append(title, meta);
    const menu = el("select", "goal-menu");
    menu.setAttribute("aria-label", "Goal actions");
    const actions = g.status === "active" ? [["done", "Mark complete"], ["dropped", "Drop goal"]] : [["active", "Reactivate"]];
    for (const [v, label] of [["", "⋯"], ...actions, ["delete", "Delete"]]) {
      const o = el("option", "", label); o.value = v; menu.appendChild(o);
    }
    menu.addEventListener("change", async () => {
      const v = menu.value; menu.value = "";
      if (v === "delete") { if (confirm(`Delete “${g.title}” and its plan?`)) await api(`/api/goals/${g.id}`, { method: "DELETE" }); }
      else if (v) await api(`/api/goals/${g.id}/status`, { body: { status: v } });
      loadGoals();
    });
    head.append(titleWrap, menu);
    const bar = el("div", "progress");
    const fill = el("span"); fill.style.width = g.total ? `${Math.round((g.done / g.total) * 100)}%` : "0%";
    bar.appendChild(fill);
    card.append(head, bar);

    const ul = el("ul", "list steps");
    for (const st of g.steps) {
      const li = el("li", "item" + (st.done ? " done" : ""));
      const cb = el("input", "check"); cb.type = "checkbox"; cb.checked = st.done;
      cb.addEventListener("change", async () => { await api(`/api/steps/${st.id}/toggle`, { method: "POST" }); loadGoals(); });
      const body = el("div", "body"); body.appendChild(el("div", "title", st.text));
      if (st.due && !st.done) body.appendChild(el("div", "sub" + (st.overdue ? " overdue" : ""), (st.overdue ? "overdue · " : "") + st.due));
      li.append(cb, body, removeBtn(async () => { await api(`/api/steps/${st.id}`, { method: "DELETE" }); loadGoals(); }));
      ul.appendChild(li);
    }
    card.appendChild(ul);
    if (g.status === "active") {
      const add = el("form", "step-add");
      const inp = el("input"); inp.placeholder = "Add a step…"; inp.required = true;
      const due = el("input", "narrow"); due.placeholder = "when?";
      add.append(inp, due);
      add.addEventListener("submit", async e => {
        e.preventDefault();
        try { await api(`/api/goals/${g.id}/steps`, { body: { text: inp.value, due: due.value || null } }); loadGoals(); }
        catch (err) { toast({ head: "Couldn't add step", text: err.message }); }
      });
      card.appendChild(add);
    }
    if (g.notes.length) {
      const notes = el("div", "goal-notes");
      for (const n of g.notes) notes.appendChild(el("div", "", "📝 " + n));
      card.appendChild(notes);
    }
    box.appendChild(card);
  }
}

function refreshPanel() {
  return Promise.all([loadReminders(), loadTodos(), loadGoals(), loadMemories()]).catch(() => {});
}

function setTab(tab) {
  state.tab = tab;
  $$(".tab").forEach(t => t.classList.toggle("active", t.dataset.tab === tab));
  $$(".tab-body").forEach(b => (b.hidden = b.dataset.body !== tab));
  try { localStorage.setItem("tab", tab); } catch {}
}
$$(".tab").forEach(t => t.addEventListener("click", () => setTab(t.dataset.tab)));

function openPanel(open) {
  $("#panel").classList.toggle("open", open);
  $("#scrim").hidden = !open;
}
$("#panel-toggle").addEventListener("click", () => openPanel(true));
$("#panel-close").addEventListener("click", () => openPanel(false));
$("#scrim").addEventListener("click", () => openPanel(false));

$("#reminder-form").addEventListener("submit", async e => {
  e.preventDefault();
  const f = e.target;
  $("#reminder-error").textContent = "";
  try {
    await api("/api/reminders", { body: { text: f.text.value, when: f.when.value } });
    f.reset(); loadReminders();
  } catch (err) { $("#reminder-error").textContent = err.message; }
});
$("#todo-form").addEventListener("submit", async e => {
  e.preventDefault();
  const f = e.target;
  const body = { text: f.text.value, list: f.list.value || null };
  f.text.value = ""; f.text.focus();
  await api("/api/todos", { body });
  loadTodos();
});
$("#memory-form").addEventListener("submit", async e => {
  e.preventDefault();
  const fact = e.target.fact.value;
  e.target.reset();
  await api("/api/memories", { body: { fact } });
  loadMemories();
});
$("#goal-form").addEventListener("submit", async e => {
  e.preventDefault();
  const title = e.target.title.value;
  e.target.reset();
  await api("/api/goals", { body: { title } });
  loadGoals();
});
$("#clear-done").addEventListener("click", async () => { await api("/api/todos/clear-done", { method: "POST" }); loadTodos(); });

// ---------- Menu ----------
$("#menu-btn").addEventListener("click", e => { e.stopPropagation(); $("#menu").hidden = !$("#menu").hidden; });
document.addEventListener("click", () => ($("#menu").hidden = true));
$("#menu").addEventListener("click", async e => {
  const action = e.target.dataset.action;
  if (action === "logout") { await api("/api/logout", { method: "POST" }); showLogin(); }
  if (action === "clear" && confirm("Clear the chat history? Reminders, lists and memories are kept.")) {
    await api("/api/history", { method: "DELETE" }); loadHistory();
  }
  if (action === "checkins") openCheckins();
  if (action === "notify" && "Notification" in window) {
    const p = await Notification.requestPermission();
    toast({ head: "Notifications", text: p === "granted" ? "Browser notifications are on for this device." : "Notifications were blocked." });
  }
});

// ---------- Check-in settings ----------
const dialog = $("#checkins");
async function openCheckins() {
  const cfg = await api("/api/checkins");
  const f = $("#checkin-form");
  f.mode.value = cfg.mode;
  f.morning.value = cfg.morning; f.midday.value = cfg.midday; f.evening.value = cfg.evening;
  const [qs, qe] = (cfg.quiet_hours || "-").split("-");
  f.quiet_start.value = qs || ""; f.quiet_end.value = qe || "";
  f.weather_location.value = cfg.weather_location || "";
  $("#checkin-error").textContent = "";
  syncCheckinFields();
  dialog.showModal();
}
function syncCheckinFields() {
  const f = $("#checkin-form"), mode = f.mode.value;
  f.midday.disabled = mode !== "coach";
  f.evening.disabled = mode === "light" || mode === "off";
  f.morning.disabled = mode === "off";
}
$("#checkin-form").mode.addEventListener("change", syncCheckinFields);
$("#checkin-cancel").addEventListener("click", () => dialog.close());
$("#checkin-form").addEventListener("submit", async e => {
  e.preventDefault();
  const f = e.target;
  const quiet = f.quiet_start.value && f.quiet_end.value ? `${f.quiet_start.value}-${f.quiet_end.value}` : "";
  try {
    await api("/api/checkins", {
      method: "PUT",
      body: { mode: f.mode.value, morning: f.morning.value, midday: f.midday.value, evening: f.evening.value,
              quiet_hours: quiet, weather_location: f.weather_location.value.trim() },
    });
    dialog.close();
    toast({ head: "Check-ins", text: "Saved ✓" });
  } catch (err) { $("#checkin-error").textContent = err.message; }
});

// ---------- Live reminder events ----------
function toast({ head, text, actions = [] }) {
  const t = el("div", "toast");
  t.append(el("div", "t-head", head), el("div", "t-text", text));
  if (actions.length) {
    const row = el("div", "t-actions");
    for (const a of actions) {
      const b = el("button", a.primary ? "primary" : "", a.label);
      b.addEventListener("click", async () => { await a.run(); t.remove(); });
      row.appendChild(b);
    }
    t.appendChild(row);
  } else {
    setTimeout(() => t.remove(), 4000);
  }
  $("#toasts").appendChild(t);
  return t;
}

function connectEvents() {
  if (state.events) state.events.close();
  const es = new EventSource("/api/events");
  state.events = es;
  es.onmessage = e => {
    const ev = JSON.parse(e.data);
    if (ev.type === "changed") { refreshPanel(); return; }
    if (ev.type === "message") {
      if (!state.busy) addMessage("bot", ev.text, { source: "checkin" });
      else setTimeout(() => addMessage("bot", ev.text, { source: "checkin" }), 1500);
      if ("Notification" in window && Notification.permission === "granted" && document.hidden) {
        const n = new Notification(state.name, { body: ev.text.replace(/[*_~`]/g, "").slice(0, 180), tag: `msg-${ev.kind}` });
        n.onclick = () => { window.focus(); n.close(); };
      }
      return;
    }
    if (ev.type !== "reminder") return;
    const t = toast({
      head: `⏰ Reminder · ${ev.when}${ev.late ? " (late)" : ""}`,
      text: ev.text,
      actions: [
        { label: "Done", primary: true, run: () => api(`/api/reminders/${ev.id}/done`, { method: "POST" }).then(refreshPanel) },
        { label: "Snooze 10m", run: () => api(`/api/reminders/${ev.id}/snooze`, { body: { minutes: 10 } }).then(refreshPanel) },
        { label: "Dismiss", run: async () => {} },
      ],
    });
    if ("Notification" in window && Notification.permission === "granted" && document.hidden) {
      const n = new Notification("⏰ " + ev.text, { body: `${state.name} reminder`, tag: `rem-${ev.id}` });
      n.onclick = () => { window.focus(); n.close(); };
    }
    refreshPanel();
    return t;
  };
}

async function checkHealth() {
  const dot = $("#status-dot"), txt = $("#status-text");
  try {
    const h = await api("/api/health");
    dot.className = "dot " + (h.ok ? "ok" : "bad");
    txt.textContent = h.ok ? `online · ${h.model}` : h.error;
  } catch {
    dot.className = "dot bad"; txt.textContent = "offline";
  }
}

// ---------- Boot ----------
function greeting() {
  const h = new Date().getHours();
  return h < 5 ? "Up late?" : h < 12 ? "Good morning" : h < 17 ? "Good afternoon" : "Good evening";
}

async function start() {
  $("#login").hidden = true;
  $("#app").hidden = false;
  try { setTab(localStorage.getItem("tab") || "reminders"); } catch { setTab("reminders"); }
  await Promise.all([loadHistory(), refreshPanel()]);
  connectEvents();
  checkHealth();
  if (matchMedia("(hover: hover)").matches) input.focus();
}

(async () => {
  const me = await fetch("/api/me").then(r => r.json());
  state.name = me.name;
  document.title = me.name;
  $("#brand-name").textContent = me.name;
  $("#login-title").textContent = `Hi, I'm ${me.name}`;
  $("#greeting").textContent = `${greeting()}${me.user ? ", " + me.user : ""}!`;
  $$(".empty .muted")[0].textContent =
    `I'm ${me.name}. I can set reminders${me.discord ? " (sent to your Discord)" : ""}, keep your lists, turn goals into plans, look things up, and check in on you.`;
  if (me.authed) start(); else showLogin();
  setInterval(() => { if (!$("#app").hidden) checkHealth(); }, 60000);
})();
