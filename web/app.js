(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const state = { data: [], tag: "All", group: "", q: "", showExpired: false, model: "", tasks: null, done: {} };
  const esc = (value) => String(value ?? "").replace(/[&<>\"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  function markdown(value) {
    const inline = (x) => x.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
      .replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>").replace(/\*([^*]+)\*/g, "<em>$1</em>");
    const out = []; let list = null;
    const close = () => { if (list) { out.push(list.open + list.items.join("") + list.close); list = null; } };
    for (const line of esc(value).split(/\r?\n/)) {
      let m = line.match(/^\s*(#{1,2})\s+(.+)/);
      if (m) { close(); out.push(`<h${m[1].length}>${inline(m[2])}</h${m[1].length}>`); continue; }
      m = line.match(/^\s*[-*]\s+(.+)/);
      if (m) { if (!list || list.open !== "<ul>") { close(); list = { open: "<ul>", close: "</ul>", items: [] }; } list.items.push(`<li>${inline(m[1])}</li>`); continue; }
      m = line.match(/^\s*\d+\.\s+(.+)/);
      if (m) { if (!list || list.open !== "<ol>") { close(); list = { open: "<ol>", close: "</ol>", items: [] }; } list.items.push(`<li>${inline(m[1])}</li>`); continue; }
      close(); if (line.trim()) out.push(`<p>${inline(line)}</p>`);
    }
    close(); return out.join("");
  }
  function showError(error) { $("errorBanner").textContent = error.message || String(error); $("errorBanner").classList.add("show"); }
  try { state.done = JSON.parse(localStorage.getItem("organizer-done") || "{}"); } catch (_) { state.done = {}; }
  function saveDone() { try { localStorage.setItem("organizer-done", JSON.stringify(state.done)); } catch (_) {} }
  function today() { return new Date().toISOString().slice(0, 10); }
  function visible() { return state.data.filter((n) => (state.tag === "All" || n.tag === state.tag) && (!state.group || (n.groups || []).includes(state.group)) && (!state.q || [n.display_text, n.preview, ...(n.groups || [])].join(" ").toLowerCase().includes(state.q.toLowerCase())) && (state.showExpired || !n.deadline_iso || n.deadline_iso >= today()) && n.tag !== "Noise"); }
  function render() {
    const groups = [...new Set(state.data.flatMap((n) => n.groups || []))].sort();
    $("nav").innerHTML = '<button data-group="">◉ Inbox</button>';
    $("groups").innerHTML = groups.map((g) => `<button data-group="${esc(g)}">▐ ${esc(g)}</button>`).join("");
    document.querySelectorAll("[data-group]").forEach((b) => { b.onclick = () => { state.group = b.dataset.group; render(); }; });
    const tags = ["All", "Deadline", "Action", "Schedule", "Opportunity", "Resource", "Attendance"];
    $("toolbar").innerHTML = tags.map((t) => `<button class="chip ${state.tag === t ? "active" : ""}" data-tag="${t}">${t}</button>`).join("");
    document.querySelectorAll("[data-tag]").forEach((b) => { b.onclick = () => { state.tag = b.dataset.tag; render(); }; });
    const list = visible(); $("count").textContent = `${state.data.length} unique notices loaded`; $("subtitle").textContent = `${state.data.length} unique notices loaded`;
    $("feed").innerHTML = list.slice(0, 150).map((n) => `<article class="card" data-id="${esc(n.id)}"><div class="tag">${esc(n.tag)} · ${esc(n.group || "")} · ${esc((n.timestamp || "").slice(0, 16).replace("T", " "))}</div><h2>${esc(n.preview || "")}</h2><div class="body">${esc(n.display_text)}</div>${n.deadline_iso ? `<div class="tag">▣ ${esc(n.deadline_text || n.deadline_iso)} · ${n.deadline_iso}</div>` : ""}</article>`).join("") || '<div class="meta">Nothing here for this filter.</div>';
  }
  function renderTasks() {
    const box = $("tasks");
    if (!state.tasks || !state.tasks.updated_at) { box.innerHTML = '<b>Active Tasks &amp; Deadlines</b><p>No tasks yet. Click Update tasks.</p><button id="regen">Update tasks</button>'; }
    else { box.innerHTML = `<b>Active Tasks &amp; Deadlines</b> <button id="regen">Update tasks</button> <span class="tag">Updated ${new Date(state.tasks.updated_at).toLocaleTimeString()}</span>` + (state.tasks.tasks.length ? state.tasks.tasks.map((x) => `<details class="task"><summary><input class="task-done" type="checkbox" data-task="${esc(x.id)}" ${state.done[x.id] ? "checked" : ""}> <span class="badge ${x.bucket}">${x.bucket.replace("_", " ").toUpperCase()}</span>${esc(x.title)} ${x.due_text ? `<span class="tag">${esc(x.due_text)}</span>` : ""}</summary><ol>${(x.steps || []).map((s) => `<li>${esc(s)}</li>`).join("")}</ol><a class="source" data-source="${esc(x.source_notice_id)}">Show source notice</a></details>`).join("") : "<p>No actionable tasks found.</p>"); }
    $("regen").onclick = updateTasks;
    document.querySelectorAll(".task-done").forEach((i) => { i.onclick = (e) => { e.stopPropagation(); state.done[i.dataset.task] = i.checked; saveDone(); }; });
    document.querySelectorAll(".source").forEach((a) => { a.onclick = () => { const card = document.querySelector(`[data-id="${a.dataset.source}"]`); if (card) { card.scrollIntoView({ behavior: "smooth" }); card.style.outline = "3px solid #e6a23c"; } }; });
  }
  async function loadTasks() { state.tasks = await (await fetch("/api/tasks")).json(); renderTasks(); }
  async function updateTasks() {
    const box = $("tasks"); box.innerHTML = "<b>Active Tasks &amp; Deadlines</b><p>Checking for changes…</p>";
    try {
      const response = await fetch("/api/tasks/update", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ model: state.model }) });
      const plan = await response.json(); if (response.status === 409) throw Error(plan.error);
      if (plan.message === "Up to date") { box.innerHTML = "<b>Active Tasks &amp; Deadlines</b><p>Up to date.</p><button id=regen>Update tasks</button>"; $("regen").onclick = updateTasks; return; }
      box.innerHTML = `<b>Active Tasks &amp; Deadlines</b><p>${plan.new} new, ${plan.changed} changed, ${plan.removed} removed. ${plan.model_calls_planned} model calls planned.</p>`;
      let status; do { await new Promise((resolve) => setTimeout(resolve, 700)); status = await (await fetch("/api/tasks/status")).json(); box.querySelector("p").textContent = status.state === "running" ? `Updating ${status.done} of ${status.total} notices…` : status.state; } while (["starting", "running"].includes(status.state));
      if (status.state === "error") throw Error(status.error); box.querySelector("p").textContent = `Added ${status.added}, updated ${status.updated}, removed ${status.removed}`; await loadTasks();
    } catch (e) { box.innerHTML = `<b>Active Tasks &amp; Deadlines</b><p>${esc(e.message)}</p><button id=regen>Update tasks</button>`; $("regen").onclick = updateTasks; }
  }
  async function status() { const j = await (await fetch("/api/status")).json(); state.model = (j.models || [])[0] || ""; $("models").innerHTML = (j.models || []).map((x) => `<option>${esc(x)}</option>`).join("") || "<option>No model loaded</option>"; $("badge").textContent = j.connected ? "Connected" : "Offline"; $("status").textContent = j.connected ? `Connected · ${j.today_label}` : (j.error || "Offline"); }
  async function ask(question) { $("synthesis").innerHTML = "Loading…"; try { const j = await (await fetch("/api/chat", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ model: state.model, question, notices: visible() }) })).json(); if (j.error) throw Error(j.error); $("synthesis").innerHTML = markdown(j.answer); $("log").innerHTML += `<div class="bubble user">${markdown(question)}</div><div class="bubble ai">${markdown(j.answer)}</div>`; } catch (e) { showError(e); } }
  async function load() { try { state.data = await (await fetch("/api/notices")).json(); render(); await loadTasks(); await status(); } catch (e) { showError(e); } }
  $("search").oninput = (e) => { state.q = e.target.value; render(); }; $("expired").onchange = (e) => { state.showExpired = e.target.checked; render(); }; $("send").onclick = () => { const q = $("question").value.trim(); if (q) { $("question").value = ""; ask(q); } }; $("question").onkeydown = (e) => { if (e.key === "Enter") $("send").click(); }; $("synthesize").onclick = () => ask("Give a short summary of the visible notices: what is urgent, what is due this week, and what should I do next?"); $("retry").onclick = status; load();
}());
