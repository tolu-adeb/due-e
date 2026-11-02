"use strict";
/* DUE-E web app — vanilla JS, no build step. Everything user-supplied goes through esc(). */

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const KIND_COLOR = { productive: "var(--productive)", class: "var(--class)", health: "var(--health)", maintenance: "var(--maintenance)", leisure: "var(--leisure)", waste: "var(--waste)" };
const KIND_HELP = {
  productive: "counts toward your daily target (study, projects, work)",
  class: "attendance — you're expected to go, so it's its own counter",
  health: "sleep, exercise, recovery",
  maintenance: "necessary stuff — meals, errands, admin",
  leisure: "fun you chose — not waste",
  waste: "time you'd rather have back (scrolling…)",
};

const S = { tab: "today", day: null, state: null, insightsDays: 7, prefill: null, showClasses: (() => { try { return !!localStorage.getItem("duee.showClasses"); } catch { return false; } })() };

async function api(path, { method = "GET", body } = {}) {
  const res = await fetch(path, {
    method, credentials: "same-origin",
    headers: body !== undefined ? { "Content-Type": "application/json" } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

let toastTimer;
function toast(msg, err = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast show" + (err ? " err" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.className = "toast"), 2600);
}
const run = (fn) => async (...a) => { try { await fn(...a); } catch (e) { toast(e.message, true); } };

/* ── formatting ─────────────────────────────────────────────── */
const hm = (m) => { m = Math.round(m || 0); const h = Math.floor(m / 60), r = m % 60; return h ? `${h}h${r ? " " + String(r).padStart(2, "0") + "m" : ""}` : `${r}m`; };
const tm = (iso) => new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
const dShort = (iso) => new Date(iso).toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
const localInput = (iso) => { const d = new Date(iso); const p = (n) => String(n).padStart(2, "0"); return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`; };
function rel(iso) {
  const ms = new Date(iso) - Date.now(), past = ms < 0, m = Math.abs(ms) / 60000;
  const s = m < 60 ? `${Math.round(m)}m` : m < 1440 ? `${Math.floor(m / 60)}h ${Math.round(m % 60)}m` : `${Math.floor(m / 1440)}d ${Math.floor((m % 1440) / 60)}h`;
  return past ? `${s} ago` : `in ${s}`;
}
function addDays(isoDay, n) { const d = new Date(isoDay + "T12:00:00"); d.setDate(d.getDate() + n); return d.toISOString().slice(0, 10); }
const dayName = (isoDay) => (isoDay === S.state?.today ? "Today" : isoDay === addDays(S.state?.today || isoDay, -1) ? "Yesterday" : dShort(isoDay + "T12:00:00"));
const catOptions = (sel, auto = true) => (auto ? `<option value="">auto</option>` : "")
  + (S.state?.categories || []).map((c) => `<option ${c.name === sel ? "selected" : ""}>${esc(c.name)}</option>`).join("")
  + `<option value="__new">+ new type…</option>`;

/* ── custom activity types ── */
function newTypeDialog(prefill = "") {
  return new Promise((resolve) => {
    let dlg = $("#type-dlg");
    if (!dlg) { dlg = document.createElement("dialog"); dlg.id = "type-dlg"; document.body.appendChild(dlg); }
    dlg.innerHTML = `<form method="dialog" class="stack">
      <h2 class="mono" style="margin:0;font-size:13px;letter-spacing:.08em;color:var(--muted)">NEW ACTIVITY TYPE</h2>
      <label class="f">name<input type="text" name="name" required maxlength="24" value="${esc(prefill)}" placeholder="music, reading for fun, clubs…"></label>
      <div class="kinds">${(S.state?.kinds || Object.keys(KIND_HELP)).map((k, i) => `<label class="kind"><input type="radio" name="kind" value="${k}" ${i === 0 ? "checked" : ""}>
        <span><b style="color:${KIND_COLOR[k]}">${k}</b><br><span class="muted">${KIND_HELP[k] || ""}</span></span></label>`).join("")}</div>
      <div class="row"><button class="btn primary" value="ok">create</button><button class="btn ghost" value="cancel" formnovalidate>cancel</button></div>
    </form>`;
    dlg.onclose = async () => {
      if (dlg.returnValue !== "ok") return resolve(null);
      const f = Object.fromEntries(new FormData($("form", dlg)));
      try {
        const r = await api("/api/categories", { method: "POST", body: { name: f.name, kind: f.kind } });
        if (!S.state.categories.some((c) => c.name === r.name)) S.state.categories.splice(-1, 0, { name: r.name, kind: r.kind });
        toast(r.created ? `new type: ${r.name} (${r.kind})` : `${r.name} already exists`);
        resolve(r.name);
      } catch (e) { toast(e.message, true); resolve(null); }
    };
    dlg.showModal();
    $("input[name=name]", dlg).focus();
  });
}
// any category <select> offers "+ new type…"; activity rows also save the change right away
document.addEventListener("focusin", (e) => { if (e.target.matches?.("select[data-cat]")) e.target.dataset.prev = e.target.value; });
document.addEventListener("change", run(async (e) => {
  const sel = e.target;
  if (!sel.matches?.("select[data-cat]")) return;
  if (sel.value === "__new") {
    const name = await newTypeDialog();
    if (!name) { sel.value = sel.dataset.prev || ""; return; }
    if (![...sel.options].some((o) => o.value === name)) sel.add(new Option(name, name), sel.options[sel.options.length - 1]);
    sel.value = name;
  }
  if (sel.dataset.actId) {
    const r = await api(`/api/activities/${sel.dataset.actId}`, { method: "PATCH", body: { category: sel.value } });
    toast(`${r.label} → ${r.category} · DUE-E will remember that`);
    if (S.tab === "today") await loadToday();
  }
}));
const kindOf = (cat) => (S.state?.categories || []).find((c) => c.name === cat)?.kind || "maintenance";

/* ── command line ───────────────────────────────────────────── */
function parseDur(tok) {
  const m = /^(?:(\d+(?:\.\d+)?)h)?(?:(\d+)m?)?$/i.exec(tok);
  if (!m || (!m[1] && !m[2])) return null;
  return Math.round((parseFloat(m[1] || 0) * 60) + parseInt(m[2] || 0));
}
async function runCommand(raw) {
  const text = raw.trim();
  if (!text) return;
  let toks = text.split(/\s+/);
  const pull = (re) => { const i = toks.findIndex((t) => re.test(t)); return i < 0 ? null : toks.splice(i, 1)[0]; };
  let cat = null, newKind = null;
  const ctok = pull(/^#[\w-]+(:\w+)?$/);
  if (ctok) {
    [cat, newKind] = ctok.slice(1).toLowerCase().split(":");
    const known = (S.state?.categories || []).some((c) => c.name === cat);
    if (!known && newKind) await api("/api/categories", { method: "POST", body: { name: cat, kind: newKind } });
    else if (!known) { cat = await newTypeDialog(cat); if (!cat) return; }
    if (!known) S.state = null;  // reload categories on next render
  }
  const f = pull(/^f[1-5]$/i);
  const focus = f ? +f[1] : null;
  const head = toks[0]?.toLowerCase();
  if (head === "help" || head === "?") {
    toast("1h30 label f4 #type  ·  #newtype:leisure  ·  start label  ·  stop f3  ·  today/planner/habits/insights");
    return;
  }
  if (["today", "planner", "habits", "insights"].includes(head)) return switchTab(head);
  if (head === "stop") {
    const r = await api("/api/timer/stop", { method: "POST", body: { focus } });
    toast(r.id ? `■ stopped "${r.label}" · ${hm(r.minutes)}` : "no timer running");
  } else if (head === "start") {
    const label = toks.slice(1).join(" ");
    const r = await api("/api/timer/start", { method: "POST", body: { label, category: cat } });
    toast(`▶ ${r.label} · ${r.category}` + (r.stopped_previous ? ` (stopped ${r.stopped_previous.label})` : ""));
  } else {
    const mins = parseDur(toks[0]);
    if (mins == null) throw new Error("start with a duration (45m, 1h30) — or 'start', 'stop', 'help'");
    const label = toks.slice(1).join(" ");
    const r = await api("/api/log", { method: "POST", body: { label, minutes: mins, category: cat, focus } });
    toast(`✓ logged ${hm(r.minutes)} ${r.label} → ${r.category}`);
  }
  $("#cmd-input").value = "";
  await load();
}

/* ── today ──────────────────────────────────────────────────── */
async function loadToday() {
  const st = await api("/api/state" + (S.day ? `?day=${S.day}` : ""));
  S.state = st;
  S.day = st.day;
  renderFeeds(st.feeds);
  const ds = st.day_stats, t = ds.totals;
  const el = $("#tab-today");
  const tracked = st.activities.reduce((a, x) => a + (x.minutes || 0), 0);
  el.innerHTML = `
  <div class="stats">
    <div class="stat"><div class="k">day score</div><div class="v">${ds.score ?? "—"}<small>${ds.score != null ? "/100" : ""}</small></div></div>
    <div class="stat"><div class="k">productive</div><div class="v">${hm(t.productive)}<small> / ${hm(t.target || 0)}</small></div></div>
    <div class="stat"><div class="k">wasted</div><div class="v" style="color:${t.waste > 60 ? "var(--red)" : "inherit"}">${hm(t.waste)}</div></div>
    <div class="stat"><div class="k">class</div><div class="v" style="color:${t.classes_planned && t.classes_attended < t.classes_planned ? "var(--amber)" : "inherit"}">${t.classes_planned ? `${t.classes_attended}/${t.classes_planned}` : hm(t.class)}<small>${t.classes_planned ? " attended" : ""}</small></div></div>
    <div class="stat"><div class="k">plan followed</div><div class="v">${t.planned ? Math.round(100 * t.honored / t.planned) + "%" : "—"}<small> ${t.planned ? hm(t.honored) + " of " + hm(t.planned) : ""}</small></div></div>
  </div>
  <div class="grid g2">
    <div class="stack">
      <div class="card" id="timer-card"></div>
      <div class="card">
        <h2>log something</h2>
        <form id="log-form" class="form-grid">
          <label class="f wide">what<input type="text" name="label" required placeholder="EECS 281 project, gym, tiktok…"></label>
          <label class="f">minutes<input type="number" name="minutes" min="1" max="1440" required placeholder="45"></label>
          <label class="f">category<select name="category" data-cat>${catOptions()}</select></label>
          <label class="f">ended at<input type="datetime-local" name="end"></label>
          <label class="f">focus<select name="focus"><option value="">—</option>${[1, 2, 3, 4, 5].map((n) => `<option>${n}</option>`).join("")}</select></label>
          <label class="f wide">counts as prep for<select name="event_id"><option value="">—</option>${st.events.map((e) => `<option value="${e.id}">${esc(e.title)}</option>`).join("")}</select></label>
          <div class="full row"><button class="btn primary">log it</button><span class="muted mono" style="font-size:12px">tip: the command bar does this faster</span></div>
        </form>
      </div>
      <div class="card">
        <h2>${esc(dayName(st.day))}'s log <span class="spacer"></span><span class="dim">${hm(tracked)} tracked</span></h2>
        <div class="list">${st.activities.length ? st.activities.map((a) => `
          <div class="li">
            <span class="t">${tm(a.start)}</span>
            <div><div class="title">${esc(a.label)}<select class="cat-select tag ${kindOf(a.category)}" data-cat data-act-id="${a.id}" title="change type">${catOptions(a.category, false)}</select>${a.focus ? `<span class="tag">f${a.focus}</span>` : ""}</div>
                 <div class="sub mono">${hm(a.minutes)} · ${esc(a.source)}</div></div>
            <button class="btn sm ghost danger" data-del="${a.id}" title="delete">✕</button>
          </div>`).join("") : `<div class="empty">nothing logged yet</div>`}</div>
      </div>
    </div>
    <div class="stack">
      <div class="card">
        <h2>close out the day <span class="spacer"></span>
          <button class="btn sm ghost" data-day="-1">‹</button><span class="mono" style="color:var(--text)">${esc(dayName(st.day))}</span>
          <button class="btn sm ghost" data-day="1" ${st.day >= st.today ? "disabled" : ""}>›</button></h2>
        ${(() => {
          const cls = st.review.filter((i) => i.is_class), rest = st.review.filter((i) => !i.is_class);
          if (!st.review.length) return `<div class="empty">nothing was planned — add a plan calendar feed or schedule prep in the Planner</div>`;
          return (cls.length ? `<h3 class="sub-h">attendance <span class="dim">· not part of planning — just did you go?</span></h3>
            <div class="list">${cls.map(attendRow).join("")}</div>` : "")
            + (rest.length ? `<h3 class="sub-h">planned</h3><div class="list">${rest.map(reviewRow).join("")}</div>` : "");
        })()}
      </div>
      ${st.club_events.length ? `<div class="card">
        <h2>club events · next 7 days <span class="spacer"></span><span class="dim">going = blocks time + shows in close-out</span></h2>
        <div>${st.club_events.map((c) => `<div class="dl" data-club="${esc(c.ref)}">
            <div><div class="title">${esc(c.title)}<span class="tag">${esc(c.source)}</span></div>
            <div class="sub mono muted">${dShort(c.start)} ${tm(c.start)}–${tm(c.end)} · ${rel(c.start)}</div></div>
            <div class="seg"><button class="${c.going ? "on" : ""}" data-going="1">going</button><button class="${c.going ? "" : "on cxl"}" data-going="0">skip</button></div>
          </div>`).join("")}</div>
      </div>` : ""}
      <div class="card">
        <h2>deadlines · next 7 days</h2>
        <div>${st.deadlines.length ? st.deadlines.map((d, i) => {
          const h = (new Date(d.due) - Date.now()) / 3.6e6;
          return `<div class="dl"><div><div class="title">${d.url ? `<a href="${esc(d.url)}" target="_blank" rel="noopener">${esc(d.title)}</a>` : esc(d.title)}${d.course ? `<span class="tag">${esc(d.course)}</span>` : ""}</div>
            <div class="sub mono ${h < 24 ? "urgent" : h < 48 ? "soon" : "muted"}">${dShort(d.due)} ${d.all_day ? "all day" : tm(d.due)} · ${rel(d.due)}</div></div>
            ${d.task ? taskChip(d.task) : `<button class="btn sm" data-plan="${i}">plan prep</button>`}</div>`;
        }).join("") : `<div class="empty">radar clear ✅</div>`}</div>
      </div>
    </div>
  </div>`;
  renderTimer();

  $("#log-form").onsubmit = run(async (ev) => {
    ev.preventDefault();
    const f = Object.fromEntries(new FormData(ev.target));
    const body = { label: f.label, minutes: +f.minutes, category: f.category || null, focus: f.focus ? +f.focus : null,
                   end: f.end || null, event_id: f.event_id ? +f.event_id : null };
    const r = await api("/api/log", { method: "POST", body });
    toast(`✓ ${hm(r.minutes)} ${r.label} → ${r.category}`);
    await loadToday();
  });
  $$("[data-del]", el).forEach((b) => (b.onclick = run(async () => { await api(`/api/activities/${b.dataset.del}`, { method: "DELETE" }); await loadToday(); })));
  $$("[data-day]", el).forEach((b) => (b.onclick = run(async () => { S.day = addDays(S.day, +b.dataset.day); await loadToday(); })));
  $$("[data-going]", el).forEach((b) => (b.onclick = run(async () => {
    await api("/api/rsvp", { method: "POST", body: { ref: b.closest("[data-club]").dataset.club, going: b.dataset.going === "1" } });
    toast(b.dataset.going === "1" ? "✅ going — it now blocks time for planning" : "skipped");
    await loadToday();
  })));
  $$("[data-plan]", el).forEach((b) => (b.onclick = () => { S.prefill = st.deadlines[+b.dataset.plan]; switchTab("planner"); }));
  $$("[data-confirm]", el).forEach((b) => (b.onclick = run(async () => {
    const [ref, status] = [b.closest("[data-ref]").dataset.ref, b.dataset.confirm];
    const row = b.closest("[data-ref]");
    let actual = null;
    if (status === "partial") {
      const v = prompt(row.querySelector(".tag.class") ? "How many minutes were you there?" : "How many minutes did you actually do?", Math.round(+row.dataset.planned / 2));
      if (v === null) return;
      actual = parseInt(v) || null;
    }
    await api("/api/confirm", { method: "POST", body: { ref, status, day: S.day, actual_minutes: actual, category: $("select", row)?.value || null } });
    await loadToday();
  })));
}

function taskChip(t) {
  const [cls, txt] = t.short ? ["urgent", `⚠ ${hm(t.short)} short`]
    : t.scheduled ? ["ok", `${hm(t.scheduled)} planned`]
    : t.needed ? ["soon", `${hm(t.needed)} to plan`] : ["muted", "no prep needed"];
  return `<a class="chip ${cls}" href="#planner" title="auto-planned from Canvas — see the Planner">${txt}</a>`;
}

function attendRow(i) {
  const st = i.status;
  const b = (s, label, cls = "") => `<button class="${cls} ${st === s ? "on" : ""}" data-confirm="${s}" title="${s}">${label}</button>`;
  return `<div class="li ${st === "done" ? "done-row" : ""}" data-ref="${esc(i.ref)}" data-planned="${i.planned_minutes}">
    <span class="t">${tm(i.start)}<br><span class="dim">${hm(i.planned_minutes)}</span></span>
    <div><div class="title">${esc(i.title)}<span class="tag class">class</span></div></div>
    <div class="seg">${b("done", "✓ went")}${b("partial", "½ left early", "part")}${b("skipped", "✕ missed", "skip")}${b("cancelled", "— cancelled", "cxl")}</div>
  </div>`;
}

function reviewRow(i) {
  const st = i.status;
  const b = (s, label, cls = "") => `<button class="${cls} ${st === s ? "on" : ""}" data-confirm="${s}" title="${s}">${label}</button>`;
  return `<div class="li ${st === "done" ? "done-row" : ""}" data-ref="${esc(i.ref)}" data-planned="${i.planned_minutes}">
    <span class="t">${tm(i.start)}<br><span class="dim">${hm(i.planned_minutes)}</span></span>
    <div><div class="title">${esc(i.title)}<span class="tag ${i.kind}">${i.kind === "calendar" ? "cal" : i.kind}</span></div>
      <div class="row" style="margin-top:4px"><select data-cat style="padding:2px 6px;font-size:12px">${catOptions(i.category, false)}</select>
      ${st === "partial" ? `<span class="mono soon" style="font-size:12px">${hm(i.actual_minutes)}</span>` : ""}</div></div>
    <div class="seg">${b("done", "✓ did it")}${b("partial", "½", "part")}${b("skipped", "✕", "skip")}${b("cancelled", "—", "cxl")}</div>
  </div>`;
}

let tick;
function renderTimer() {
  const c = $("#timer-card");
  if (!c) return;
  const t = S.state?.timer;
  clearInterval(tick);
  if (!t) {
    c.innerHTML = `<h2>timer</h2>
      <form id="timer-form" class="row"><input type="text" name="label" placeholder="what are you starting?" style="flex:1" required>
      <select name="category" data-cat>${catOptions()}</select><button class="btn primary">▶ start</button></form>`;
    $("#timer-form").onsubmit = run(async (ev) => {
      ev.preventDefault();
      const f = Object.fromEntries(new FormData(ev.target));
      await api("/api/timer/start", { method: "POST", body: { label: f.label, category: f.category || null } });
      await loadToday();
    });
    return;
  }
  c.innerHTML = `<h2>timer <span class="pulse"></span></h2>
    <div class="timer"><div class="elapsed" id="elapsed">0:00</div>
      <div style="flex:1"><div class="what">${esc(t.label)}<span class="tag ${kindOf(t.category)}">${esc(t.category)}</span></div>
      <div class="sub mono muted" style="font-size:12px">since ${tm(t.start)} · how focused were you?</div></div>
      <div class="seg">${[1, 2, 3, 4, 5].map((n) => `<button data-stop="${n}">f${n}</button>`).join("")}<button data-stop="">■ stop</button></div></div>`;
  const upd = () => { const s = Math.floor((Date.now() - new Date(t.start)) / 1000); const e = $("#elapsed"); if (e) e.textContent = `${Math.floor(s / 3600)}:${String(Math.floor(s / 60) % 60).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`; };
  upd(); tick = setInterval(upd, 1000);
  $$("[data-stop]", c).forEach((b) => (b.onclick = run(async () => {
    const r = await api("/api/timer/stop", { method: "POST", body: { focus: b.dataset.stop ? +b.dataset.stop : null } });
    toast(`■ ${r.label} · ${hm(r.minutes)}`);
    await loadToday();
  })));
}

function renderFeeds(feeds) {
  $("#feeds").innerHTML = (feeds || []).map((f) => `<span class="${f.ok ? "" : f.used_cache ? "cached" : "bad"}" title="${esc(f.error || "ok")}"><span class="dot"></span>${esc(f.name)}</span>`).join("");
}

/* ── planner ────────────────────────────────────────────────── */
async function loadPlanner() {
  if (!S.state) S.state = await api("/api/state");
  const [events, ag] = await Promise.all([api("/api/events"), api("/api/agenda?days=7")]);
  const el = $("#tab-planner");
  const pre = S.prefill; S.prefill = null;
  el.innerHTML = `
  <div class="grid g2">
    <div class="card">
      <h2>add an event, test or project</h2>
      <form id="ev-form" class="form-grid">
        <label class="f wide">title<input type="text" name="title" required value="${esc(pre?.title || "")}" placeholder="EECS 370 Midterm"></label>
        <label class="f">type<select name="kind">${["exam", "quiz", "project", "assignment", "essay", "lab", "reading", "presentation", "interview", "event"].map((k) => `<option ${k === (pre ? "assignment" : "exam") ? "selected" : ""}>${k}</option>`).join("")}</select></label>
        <label class="f">course<input type="text" name="course" value="${esc(pre?.course || "")}" placeholder="EECS 370"></label>
        <label class="f wide">when / due<input type="datetime-local" name="at" required value="${pre ? localInput(pre.due) : ""}"></label>
        <label class="f">takes (min)<input type="number" name="duration" min="0" value="${pre ? 0 : ""}" placeholder="120"></label>
        <label class="f">difficulty<select name="difficulty">${[1, 2, 3, 4, 5].map((n) => `<option ${n === 3 ? "selected" : ""}>${n}</option>`).join("")}</select></label>
        <label class="f">how ready are you?<select name="readiness">${["1 · not started", "2 · a little", "3 · halfway", "4 · mostly", "5 · ready"].map((l, i) => `<option value="${i + 1}">${l}</option>`).join("")}</select></label>
        <label class="f">work hours (optional)<input type="number" step="0.5" min="0" name="prep_hours" placeholder="auto — the reader guesses"></label>
        <label class="f wide">notes<input type="text" name="notes" placeholder="chapters 4–6, cheat sheet allowed…"></label>
        <div class="full row"><button class="btn primary">add + plan prep</button>
          <span class="muted mono" style="font-size:12px">Canvas assignments show up here on their own · prep is only scheduled if you need it</span></div>
      </form>
    </div>
    <div class="card">
      <h2>upcoming <span class="spacer"></span><button class="btn sm" id="sync-tasks" title="pull Canvas assignments in and plan work time">⇣ sync Canvas</button>
        <button class="btn sm" id="replan-all">⟳ replan everything</button>
        <a class="btn sm" href="/plan.ics" title="subscribe to this in your calendar app">plan.ics</a></h2>
      ${events.length ? plannerSummary(events) : ""}
      <div>${events.length ? events.map(eventCard).join("") : `<div class="empty">no tasks yet — sync Canvas, add an exam or project, or hit "plan prep" on a deadline</div>`}</div>
    </div>
  </div>
  <div class="card" style="margin-top:16px">
    <h2>next 7 days <span class="spacer"></span>
      <label class="toggle" title="classes are always busy time — hidden here to keep the plan readable"><input type="checkbox" id="show-classes" ${S.showClasses ? "checked" : ""}> show classes</label>
      <span class="legend"><span><i style="background:var(--blue)"></i>your calendar</span><span><i style="background:var(--green)"></i>prep</span>
      <span><i style="background:var(--violet)"></i>habit</span><span><i style="background:none;border:1px dashed var(--muted)"></i>club (not going)</span><span><i style="background:var(--red)"></i>event</span><span><i style="background:var(--amber)"></i>deadline</span></span></h2>
    <div class="agenda">${renderAgenda(ag)}</div>
  </div>`;

  $("#ev-form").onsubmit = run(async (ev) => {
    ev.preventDefault();
    const f = Object.fromEntries(new FormData(ev.target));
    const e = await api("/api/events", { method: "POST", body: { ...f, duration: +f.duration || 0, difficulty: +f.difficulty, readiness: +f.readiness, prep_hours: f.prep_hours === "" ? null : +f.prep_hours } });
    toast(e.prep.needs_prep ? `planned ${hm(e.prep.scheduled_minutes)} of prep for ${e.title}` : `${e.title}: ${e.prep.reason}`);
    await loadPlanner();
  });
  $("#sync-tasks").onclick = run(async () => {
    const r = await api("/api/tasks/sync", { method: "POST", body: {} });
    toast(r.created || r.updated || r.linked ? `Canvas: ${r.created} new · ${r.updated} updated · ${r.linked} linked` : "Canvas tasks already up to date");
    await loadPlanner();
  });
  $("#show-classes").onchange = (ev) => { S.showClasses = ev.target.checked; try { localStorage.setItem("duee.showClasses", S.showClasses ? "1" : ""); } catch {} $(".agenda", el).innerHTML = renderAgenda(ag); };
  $("#replan-all").onclick = run(async () => { await api("/api/replan", { method: "POST", body: {} }); toast("replanned everything"); await loadPlanner(); });
  $$("[data-ev]", el).forEach((card) => {
    const id = card.dataset.ev;
    $("[data-act=replan]", card).onclick = run(async () => { await api(`/api/events/${id}/replan`, { method: "POST", body: {} }); toast("replanned"); await loadPlanner(); });
    $("[data-act=done]", card).onclick = run(async () => { await api(`/api/events/${id}`, { method: "PATCH", body: { done: true } }); await loadPlanner(); });
    $("[data-act=del]", card).onclick = run(async () => { if (!confirm("Delete this event and its planned prep?")) return; await api(`/api/events/${id}`, { method: "DELETE" }); await loadPlanner(); });
    $("[data-act=ready]", card).onchange = run(async (ev) => { await api(`/api/events/${id}`, { method: "PATCH", body: { readiness: +ev.target.value } }); toast("updated · work time replanned"); await loadPlanner(); });
    $("[data-act=hours]", card).onchange = run(async (ev) => {
      const v = ev.target.value.trim();
      await api(`/api/events/${id}`, { method: "PATCH", body: { prep_hours: v === "" ? null : +v } });
      toast(v === "" ? "back to the reader's estimate" : `estimate set to ${v}h · replanned`); await loadPlanner();
    });
    const ign = $("[data-act=ignore]", card);
    if (ign) ign.onclick = run(async () => { await api(`/api/events/${id}`, { method: "PATCH", body: { ignored: true } }); toast("hidden — it won't come back on sync"); await loadPlanner(); });
  });
}

function plannerSummary(events) {
  const week = Date.now() + 7 * 864e5;
  const soon = events.filter((e) => new Date(e.at) < week);
  const need = soon.reduce((a, e) => a + (e.prep.remaining_minutes || 0), 0);
  const sched = soon.reduce((a, e) => a + (e.prep.scheduled_minutes || 0), 0);
  const short = events.reduce((a, e) => a + (e.prep.unscheduled_minutes || 0), 0);
  return `<div class="plan-sum mono">this week: <b>${soon.length}</b> due · <b>${hm(need)}</b> of work left · <b>${hm(sched)}</b> scheduled
    ${short ? `· <span class="urgent">${hm(short)} won't fit</span>` : `· <span class="ok">everything fits</span>`}</div>`;
}

function eventCard(e) {
  const p = e.prep, need = p.needed_minutes || 0, r = e.read || {};
  const pct = (x) => need ? Math.min(100, (100 * x) / need) : 0;
  const planned = e.blocks.filter((b) => b.status === "planned" && new Date(b.end) > Date.now());
  const isTask = r.progress_label === "progress";
  const levels = isTask ? ["not started", "started", "halfway", "mostly done", "basically done"] : ["not started", "a little", "halfway", "mostly", "ready"];
  const title = e.url ? `<a href="${esc(e.url)}" target="_blank" rel="noopener">${esc(e.title)}</a>` : esc(e.title);
  const conf = r.confidence == null ? "" : r.confidence >= 0.75 ? "sure" : r.confidence >= 0.5 ? "fairly sure" : "rough guess";
  return `<div class="ev" data-ev="${e.id}">
    <div class="ev-head"><span class="title">${title}</span><span class="tag">${esc(e.kind)}</span>${e.course ? `<span class="tag">${esc(e.course)}</span>` : ""}
      ${e.source === "canvas" ? `<span class="tag src">canvas</span>` : ""}${r.deep && (p.needs_prep || p.scheduled_minutes) ? `<span class="tag deep" title="needs focus — gets long blocks in your best hours">deep</span>` : ""}
      <span class="when">${dShort(e.at)} ${tm(e.at)} · ${rel(e.at)}</span></div>
    ${r.summary || r.why?.length ? `<details class="read"><summary>${esc(r.summary || "how this was planned")}${conf ? ` <span class="dim">· ${conf}${r.source && r.source !== "rules" && r.source !== "you" ? ` · ${esc(r.source)}` : ""}</span>` : ""}</summary>
      <ul>${(r.why || []).map((w) => `<li>${esc(w)}</li>`).join("")}</ul></details>` : ""}
    ${p.needs_prep || p.scheduled_minutes ? `
      <div class="prep-line"><span>${isTask ? "work" : "prep"}</span>
        <div class="bar"><span style="width:${pct(p.logged_minutes)}%;background:var(--green)"></span><span style="width:${pct(p.scheduled_minutes)}%;background:var(--green-dim)"></span></div>
        <span>${hm(p.logged_minutes)} done · ${hm(p.scheduled_minutes)} planned · ${hm(need)} needed</span></div>
      ${p.unscheduled_minutes ? `<div class="warn-msg">⚠ couldn't fit ${hm(p.unscheduled_minutes)} — free up time or lower the estimate</div>` : ""}
      ${planned.length ? `<div class="sub mono muted" style="margin-top:6px;font-size:12px">next: ${planned.slice(0, 3).map((b) => `${dShort(b.start)} ${tm(b.start)}–${tm(b.end)}`).join(" · ")}${planned.length > 3 ? ` +${planned.length - 3}` : ""}</div>` : ""}`
      : `<div class="ok-msg">✓ ${esc(p.reason)}</div>`}
    <div class="ev-actions">
      <select data-act="ready" title="${isTask ? "how far along are you?" : "how ready are you?"}">${levels.map((l, i) => `<option value="${i + 1}" ${e.readiness === i + 1 ? "selected" : ""}>${r.progress_label || "readiness"}: ${l}</option>`).join("")}</select>
      <label class="hrs" title="override the estimate (hours of work in total) — leave blank for the reader's guess"><input data-act="hours" type="number" step="0.5" min="0" value="${e.prep_hours ?? ""}" placeholder="${p.needed_hours ?? ""}">h</label>
      <button class="btn sm" data-act="replan">⟳ replan</button><button class="btn sm" data-act="done">✓ done</button>
      ${e.source === "canvas" ? `<button class="btn sm ghost" data-act="ignore" title="not really a task (e.g. an attendance check) — hide it">not a task</button>` : ""}
      <button class="btn sm ghost danger" data-act="del">delete</button></div>
  </div>`;
}

function renderAgenda(ag) {
  const today = S.state?.today;
  const out = [];
  for (let i = 0; i < ag.days; i++) {
    const day = addDays(ag.start, i);
    const on = (iso) => iso.slice(0, 10) === day;
    const items = [
      ...ag.calendar.filter((c) => on(c.start) && (S.showClasses || !c.is_class)).map((c) => ({ ...c, cls: c.is_class ? `${c.kind} class` : c.kind })),
      ...ag.blocks.filter((b) => b.day === day).map((b) => ({ ...b, cls: `${b.kind} s-${b.status}` })),
      ...ag.events.filter((e) => on(e.start)).map((e) => ({ ...e, cls: "event", title: `${e.event_kind}: ${e.title}` })),
      ...ag.deadlines.filter((d) => on(d.due)).map((d) => ({ ...d, start: d.due, cls: "deadline", title: `due: ${d.title}` })),
    ].sort((a, b) => a.start.localeCompare(b.start));
    out.push(`<div class="aday ${day === today ? "today" : ""}"><h3>${esc(dayName(day))}</h3>
      ${items.map((x) => `<div class="ab ${x.cls}"><span class="tm">${tm(x.start)}${x.end ? "–" + tm(x.end) : ""}</span>${esc(x.title)}</div>`).join("") || `<div class="dim mono" style="font-size:11px">free</div>`}</div>`);
  }
  return out.join("");
}

/* ── habits ─────────────────────────────────────────────────── */
async function loadHabits() {
  if (!S.state) S.state = await api("/api/state");
  const habits = await api("/api/habits");
  const el = $("#tab-habits");
  el.innerHTML = `
  <div class="grid g2">
    <div class="card">
      <h2>add a habit</h2>
      <form id="hb-form" class="form-grid">
        <label class="f wide">habit<input type="text" name="name" required placeholder="gym, leetcode, gaming, call home…"></label>
        <label class="f">category<select name="category" data-cat>${catOptions()}</select></label>
        <label class="f">minutes<input type="number" name="minutes" min="5" max="480" value="45"></label>
        <label class="f">times / week<select name="per_week">${[1, 2, 3, 4, 5, 6, 7].map((n) => `<option ${n === 3 ? "selected" : ""} value="${n}">${n === 7 ? "daily" : n + "×"}</option>`).join("")}</select></label>
        <label class="f">best time<select name="window">${["any", "morning", "afternoon", "evening"].map((w) => `<option>${w}</option>`).join("")}</select></label>
        <div class="full row"><button class="btn primary">add + schedule</button>
          <span class="muted mono" style="font-size:12px">fun stuff counts too — leisure lands after the work, guilt-free</span></div>
      </form>
    </div>
    <div class="card">
      <h2>your habits <span class="spacer"></span><button class="btn sm" id="hb-sched">⟳ schedule this week</button></h2>
      <div>${habits.length ? habits.map((h) => `
        <div class="hb ${h.active ? "" : "paused"}" data-hb="${h.id}">
          <div><div class="title" style="font-weight:600">${esc(h.name)}<span class="tag ${kindOf(h.category)}">${esc(h.category)}</span></div>
            <div class="sub mono muted" style="font-size:12px">${h.minutes}m · ${h.per_week === 7 ? "daily" : h.per_week + "×/wk"} · ${esc(h.window)}${h.streak_weeks ? ` · 🔥 ${h.streak_weeks}wk` : ""}</div></div>
          <div class="dots" title="${h.done_this_week}/${h.per_week} this week">${Array.from({ length: h.per_week }, (_, i) => `<i class="${i < h.done_this_week ? "on" : ""}"></i>`).join("")}</div>
          <div class="sub mono muted" style="font-size:12px">${!h.active ? "paused" : h.done_this_week >= h.per_week ? `<span style="color:var(--violet)">✓ done for the week</span>` : h.upcoming.length ? "next: " + h.upcoming.slice(0, 2).map((b) => `${dShort(b.start)} ${tm(b.start)}`).join(" · ") : "no free slot — try another time window"}</div>
          <div class="row"><button class="btn sm" data-act="toggle">${h.active ? "pause" : "resume"}</button><button class="btn sm ghost danger" data-act="del">✕</button></div>
        </div>`).join("") : `<div class="empty">no habits yet</div>`}</div>
    </div>
  </div>`;
  $("#hb-form").onsubmit = run(async (ev) => {
    ev.preventDefault();
    const f = Object.fromEntries(new FormData(ev.target));
    await api("/api/habits", { method: "POST", body: { ...f, category: f.category || null, minutes: +f.minutes, per_week: +f.per_week } });
    toast(`added ${f.name}`);
    await loadHabits();
  });
  $("#hb-sched").onclick = run(async () => { const r = await api("/api/habits/schedule", { method: "POST", body: {} }); toast(`scheduled ${r.placed} block(s)` + (r.missed.length ? ` · no room for ${r.missed.join(", ")}` : "")); await loadHabits(); });
  $$("[data-hb]", el).forEach((row) => {
    const id = row.dataset.hb, active = !row.classList.contains("paused");
    $("[data-act=toggle]", row).onclick = run(async () => { await api(`/api/habits/${id}`, { method: "PATCH", body: { active: active ? 0 : 1 } }); await api("/api/habits/schedule", { method: "POST", body: {} }); await loadHabits(); });
    $("[data-act=del]", row).onclick = run(async () => { await api(`/api/habits/${id}`, { method: "DELETE" }); await loadHabits(); });
  });
}

/* ── insights ───────────────────────────────────────────────── */
async function loadInsights() {
  const an = await api(`/api/insights?days=${S.insightsDays}`);
  const el = $("#tab-insights");
  const t = an.totals;
  const maxDay = Math.max(60, ...an.daily.map((d) => d.productive + d.class + d.health + d.maintenance + d.leisure + d.waste), ...an.daily.map((d) => d.target));
  const maxCat = Math.max(1, ...an.by_category.map((c) => c.minutes));
  const maxHour = Math.max(1, ...an.focus_by_hour.map((h) => h.minutes));
  const hours = Array.from({ length: 24 }, (_, h) => an.focus_by_hour.find((x) => x.hour === h));
  const R = 50, C = 2 * Math.PI * R, sc = an.score ?? 0;
  const ringColor = sc >= 75 ? "var(--green)" : sc >= 50 ? "var(--amber)" : "var(--red)";
  const compNames = { output: "output", follow_through: "follow-through", attendance: "class attendance", deep_work: "deep work", waste_control: "waste control" };
  el.innerHTML = `
  <div class="row" style="margin-bottom:14px"><div class="seg">${[7, 14, 30].map((d) => `<button data-range="${d}" class="${d === S.insightsDays ? "on" : ""}">${d}d</button>`).join("")}</div>
    <span class="muted mono" style="font-size:12px">${an.range.logged_days} of ${an.range.days} days tracked</span></div>
  <div class="stats">
    <div class="stat"><div class="k">productive</div><div class="v">${hm(t.productive)}<small> / ${hm(t.target)}</small></div></div>
    <div class="stat"><div class="k">deep work</div><div class="v">${hm(t.deep)}</div></div>
    <div class="stat"><div class="k">class</div><div class="v">${t.classes_planned ? Math.round(100 * t.classes_attended / t.classes_planned) + "%" : hm(t.class)}<small>${t.classes_planned ? ` ${t.classes_attended}/${t.classes_planned} attended` : ""}</small></div></div>
    <div class="stat"><div class="k">wasted</div><div class="v" style="color:var(--red)">${hm(t.waste)}</div></div>
    <div class="stat"><div class="k">recoverable / week</div><div class="v" style="color:var(--green)">${hm(an.recoverable_minutes_per_week)}</div></div>
  </div>
  <div class="grid g2">
    <div class="stack">
      <div class="card"><h2>how optimal</h2>
        <div class="score">
          <svg class="ring" viewBox="0 0 120 120"><circle cx="60" cy="60" r="${R}" fill="none" stroke="var(--line)" stroke-width="10"/>
            <circle cx="60" cy="60" r="${R}" fill="none" stroke="${ringColor}" stroke-width="10" stroke-linecap="round" stroke-dasharray="${(C * sc) / 100} ${C}" transform="rotate(-90 60 60)"/>
            <text x="60" y="70" text-anchor="middle">${an.score ?? "—"}</text></svg>
          <div class="comp">${Object.entries(compNames).map(([k, n]) => an.components[k] == null ? `<div class="r"><span>${n}</span><span class="dim">no data</span><span></span></div>` :
            `<div class="r"><span>${n}</span><div class="bar"><span style="width:${an.components[k]}%;background:${an.components[k] >= 75 ? "var(--green)" : an.components[k] >= 50 ? "var(--amber)" : "var(--red)"}"></span></div><span>${an.components[k]}%</span></div>`).join("")}</div>
        </div></div>
      <div class="card"><h2>where the time went</h2>
        ${an.by_category.length ? an.by_category.map((c) => `<div class="hbar"><span>${esc(c.name)}</span><div class="bar"><span style="width:${(100 * c.minutes) / maxCat}%;background:${KIND_COLOR[c.kind]}"></span></div><span class="muted">${hm(c.per_day)}/day</span></div>`).join("") : `<div class="empty">nothing tracked in this range</div>`}
        ${an.top_wasters.length ? `<div class="sub mono muted" style="margin-top:10px;font-size:12px">top sinks: ${an.top_wasters.map((w) => `${esc(w.label)} ${hm(w.minutes)}`).join(" · ")}</div>` : ""}
      </div>
    </div>
    <div class="stack">
      <div class="card"><h2>daily breakdown <span class="spacer"></span><span class="legend">${Object.entries(KIND_COLOR).map(([k, c]) => `<span><i style="background:${c}"></i>${k}</span>`).join("")}<span><i style="background:var(--amber);height:2px"></i>target</span></span></h2>
        <div class="days">${an.daily.map((d) => `<div class="dcol" title="${d.day}: ${hm(d.productive)} productive">
          <div class="target" style="bottom:calc(${(100 * d.target) / maxDay}% * 0.86 + 18px)"></div>
          <div class="stack-bar" style="height:${(86 * (d.productive + d.class + d.health + d.maintenance + d.leisure + d.waste)) / maxDay}%">
            ${["productive", "class", "health", "maintenance", "leisure", "waste"].map((k) => d[k] ? `<span style="flex:${d[k]};background:${KIND_COLOR[k]}"></span>` : "").join("")}</div>
          <div class="lbl">${an.daily.length <= 14 ? new Date(d.day + "T12:00").toLocaleDateString([], { weekday: "short" }) : d.day.slice(8)}</div></div>`).join("")}</div>
      </div>
      <div class="card"><h2>when you work · focus by hour</h2>
        <div class="heat">${hours.map((h, i) => `<div title="${i}:00 — ${h ? hm(h.minutes) + (h.focus ? " · focus " + h.focus : "") : "nothing"}" style="background:${h ? `rgba(55,245,160,${0.15 + (0.85 * h.minutes) / maxHour})` : "var(--line)"};${an.best_hours.includes(i) ? "outline:1.5px solid var(--amber);outline-offset:1px" : ""}"></div>`).join("")}</div>
        <div class="heat-l">${hours.map((_, i) => `<span>${i % 3 === 0 ? i : ""}</span>`).join("")}</div>
        <div class="sub mono muted" style="margin-top:8px;font-size:12px">${an.best_hours.length ? `best hours (outlined): ${an.best_hours.map((h) => `${h % 12 || 12}${h < 12 ? "am" : "pm"}`).join(", ")} — DUE-E puts prep there first once you've rated focus a few times` : "rate your focus (f1–f5) when you stop timers to find your best hours"}</div>
      </div>
    </div>
  </div>
  <div class="grid g2" style="margin-top:16px">
    <div class="card"><h2>what to change</h2>
      ${an.suggestions.length ? an.suggestions.map((s) => `<div class="sugg"><div class="h">${esc(s.title)}${s.minutes_per_week ? `<span class="gain">+${hm(s.minutes_per_week)}/wk</span>` : ""}</div><p>${esc(s.detail)}</p></div>`).join("") : `<div class="empty">${an.range.logged_days ? "no red flags — keep going" : "track a few days to get suggestions"}</div>`}
    </div>
    <div class="card"><h2>claude review <span class="spacer"></span><button class="btn sm" id="ai-btn" ${an.ai_available ? "" : "disabled"}>ask claude</button></h2>
      <div id="ai-out" class="ai-out muted">${an.ai_available ? "Sends this range's totals (no notes) to Claude for a second opinion." : "Off. Set <span class='mono'>ai.enabled: true</span> and <span class='mono'>ANTHROPIC_API_KEY</span> to turn it on — the rule-based suggestions work without it."}</div>
    </div>
  </div>`;
  $$("[data-range]", el).forEach((b) => (b.onclick = run(async () => { S.insightsDays = +b.dataset.range; await loadInsights(); })));
  const ab = $("#ai-btn");
  if (ab) ab.onclick = run(async () => { $("#ai-out").textContent = "thinking…"; const r = await api("/api/ai-review", { method: "POST", body: { days: S.insightsDays } }); $("#ai-out").textContent = r.text; $("#ai-out").classList.remove("muted"); });
}

/* ── shell ──────────────────────────────────────────────────── */
const loaders = { today: loadToday, planner: loadPlanner, habits: loadHabits, insights: loadInsights };
const load = run(() => loaders[S.tab]());
function switchTab(name) {
  S.tab = name;
  $$(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tab").forEach((t) => t.classList.toggle("active", t.id === `tab-${name}`));
  history.replaceState(null, "", `#${name}`);
  load();
}
$$(".tabs button").forEach((b) => (b.onclick = () => switchTab(b.dataset.tab)));
$("#cmd").onsubmit = (ev) => { ev.preventDefault(); run(runCommand)($("#cmd-input").value); };
document.addEventListener("keydown", (e) => { if (e.key === "/" && document.activeElement.tagName !== "INPUT") { e.preventDefault(); $("#cmd-input").focus(); } });
setInterval(() => ($("#clock").textContent = new Date().toLocaleTimeString([], { weekday: "short", hour: "numeric", minute: "2-digit" })), 1000);
window.addEventListener("hashchange", () => { const h = location.hash.slice(1); if (loaders[h] && h !== S.tab) switchTab(h); });
switchTab(loaders[location.hash.slice(1)] ? location.hash.slice(1) : "today");
