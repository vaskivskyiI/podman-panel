"use strict";
const $ = (s, el = document) => el.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const GB = 1024 ** 3;
const fmtBytes = (b) => !b ? "0 B" : b >= GB ? (b / GB).toFixed(1) + " GB" : b >= 1048576 ? (b / 1048576).toFixed(0) + " MB" : (b / 1024).toFixed(0) + " kB";
const ago = (t) => {
  if (!t) return "never";
  const s = Date.now() / 1000 - t;
  if (s < 90) return "just now";
  if (s < 5400) return Math.round(s / 60) + " min ago";
  if (s < 129600) return Math.round(s / 3600) + " h ago";
  return Math.round(s / 86400) + " days ago";
};

let S = null;            // last /api/state
let tab = localGet("tab") || "overview";
let filter = { account: "", q: "", only: "" };
let stats = {};          // account -> {container name -> stats}
let pollTimer = null;
let openJob = null;

function localGet(k) { try { return localStorage.getItem("pp_" + k); } catch { return null; } }
function localSet(k, v) { try { localStorage.setItem("pp_" + k, v); } catch { /* private mode */ } }

async function api(path, body) {
  const opts = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json", "X-Panel": "1" }, body: JSON.stringify(body) };
  const r = await fetch("/api/" + path, opts);
  const data = await r.json().catch(() => ({ error: "bad response" }));
  if (r.status === 401 && path !== "login") { showLogin(); throw new Error("login required"); }
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}

function toast(msg, bad) {
  const t = $("#toast");
  t.textContent = msg; t.className = "toast" + (bad ? " bad" : "");
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.add("hidden"), bad ? 6000 : 3000);
}

function showLogin() {
  clearInterval(pollTimer);
  $("#app").classList.add("hidden"); $("#login").classList.remove("hidden"); $("#pw").focus();
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#login-err").textContent = "";
  try {
    await api("login", { password: $("#pw").value, code: $("#code").value });
    $("#pw").value = ""; $("#code").value = "";
    start();
  } catch (err) { $("#login-err").textContent = err.message; }
});

async function start() {
  $("#login").classList.add("hidden"); $("#app").classList.remove("hidden");
  await refresh();
  clearInterval(pollTimer);
  pollTimer = setInterval(() => { if (!document.hidden) refresh(); }, 10000);
}

async function refresh(force) {
  try {
    S = await api("state" + (force ? "?force=1" : ""));
    if (force || !document.querySelector(".menu-list:not(.hidden)")) render();  // don't close an open menu
  } catch (e) { if (e.message !== "login required") toast(e.message, true); }
}

// ---------- derived data
const allStacks = () => S.accounts.flatMap((a) => a.stacks.map((s) => ({ ...s, account: a.account })));
function stackUpdate(acct, st) {
  const u = S.updates[acct] || {};
  const imgs = st.containers.map((c) => (u.images || {})[c.image]).filter(Boolean);
  if (imgs.some((i) => i.status === "outdated")) return "image";
  const dirs = new Set(st.sources);
  const custom = st.containers.filter((c) => (c.image || "").startsWith("localhost/"));
  const imgTime = custom.length ? Math.min(...custom.map((c) => c.image_created || 0)) : 0;
  for (const d of dirs) {
    const src = (u.sources || {})[d];
    if (!src) continue;
    if (src.behind) return "source";
    if (imgTime && src.commit_time > imgTime + 60) return "rebuild";
  }
  return null;
}
function stackState(st) {
  if (st.total && st.running === st.total) {
    const unhealthy = st.containers.some((c) => c.health === "unhealthy");
    return unhealthy ? ["warn", "unhealthy"] : ["ok", "running"];
  }
  if (st.running) return ["warn", `${st.running}/${st.total} running`];
  return ["off", (st.status || "stopped").toLowerCase()];
}
const insightsFor = (acct, key) => S.insights.filter((i) => i.account === acct && i.stack === key && i.severity === "warn");

// ---------- render
function render() {
  $("#hostname").textContent = S.host.hostname;
  const h = S.host;
  const disk = h.disk_used / h.disk_total, mem = 1 - h.mem_available / h.mem_total;
  $("#meters").innerHTML = [
    meter("Disk /", disk, `${fmtBytes(h.disk_used)} of ${fmtBytes(h.disk_total)}`),
    meter("Memory", mem, `${fmtBytes(h.mem_total - h.mem_available)} of ${fmtBytes(h.mem_total)}`),
    meter("Load", Math.min(h.load[0] / h.cpus, 1), `${h.load.map((x) => x.toFixed(2)).join(" ")} · ${h.cpus} CPU`),
  ].join("");
  const nUpd = S.insights.filter((i) => i.severity === "update").length;
  const nWarn = S.insights.filter((i) => i.severity === "warn").length;
  const nJobs = S.jobs.filter((j) => j.status === "running" || j.status === "queued").length;
  badge("#badge-updates", nUpd); badge("#badge-insights", nWarn); badge("#badge-jobs", nJobs);
  const ub = $("#btn-updates");
  ub.disabled = S.update_state.running;
  ub.textContent = S.update_state.running ? "Checking…" : "Check updates";
  ub.title = "Last check: " + ago(S.update_state.finished);
  document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  const keepScroll = window.scrollY;
  const searching = document.activeElement?.id === "f-q";
  $("#view").innerHTML = ({ overview, stacks: stacksView, updates: updatesView, insights: insightsView, images: imagesView, jobs: jobsView })[tab]();
  window.scrollTo(0, keepScroll);
  const q = $("#f-q");
  if (q && searching) { q.focus(); q.setSelectionRange(q.value.length, q.value.length); }
}
function meter(label, frac, text) {
  return `<div class="meter${frac > 0.85 ? " hot" : ""}"><span>${esc(label)} · ${Math.round(frac * 100)}%</span><div class="bar"><i style="width:${(frac * 100).toFixed(1)}%"></i></div><span>${esc(text)}</span></div>`;
}
function badge(sel, n) { const b = $(sel); b.textContent = n; b.classList.toggle("hidden", !n); }

function overview() {
  const st = allStacks();
  const running = st.filter((s) => s.running).length;
  const noBoot = st.filter((s) => s.running && !s.autostart).length;
  const upd = S.insights.filter((i) => i.severity === "update");
  const warns = S.insights.filter((i) => i.severity === "warn");
  const reclaim = S.accounts.reduce((n, a) => n + ((a.df || []).find((d) => d.Type === "Images")?.RawReclaimable || 0), 0);
  let html = `<div class="grid">
    ${tile(running + " / " + st.length, "stacks running", `${st.reduce((n, s) => n + s.total, 0)} containers · ${S.accounts.length} accounts`)}
    ${tile(upd.length, "updates available", "last check " + ago(S.update_state.finished))}
    ${tile(warns.length, "warnings", noBoot ? `${noBoot} running stacks won't start on boot` : "nothing urgent")}
    ${tile(fmtBytes(reclaim), "reclaimable image space", `${fmtBytes(S.host.disk_free)} free on /`)}
  </div>`;
  html += `<h3>Accounts</h3><div class="grid">` + S.accounts.map((a) => {
    const run = a.stacks.filter((s) => s.running).length;
    return `<div class="card"><div style="display:flex;justify-content:space-between;align-items:center"><b>${esc(a.account)}</b>
      ${a.error ? pill("bad", "error") : a.rootful ? pill("accent", "rootful") : a.lingering ? pill("ok", "lingering") : pill("warn", "no linger")}</div>
      <div class="muted small">${a.error ? esc(a.error) : `${run}/${a.stacks.length} stacks running · ${a.images.length} images`}</div>
      <div style="margin-top:8px"><button class="btn sm" data-act="goto-account" data-account="${esc(a.account)}">Show stacks</button></div></div>`;
  }).join("") + `</div>`;
  const ps = S.panel.source;
  html += `<h3>Panel</h3><div class="card" style="display:flex;gap:12px;align-items:center;flex-wrap:wrap">
    <div><code>${esc(S.panel.source_dir)}</code>${ps ? ` · <code>${esc(ps.commit)}</code> ${esc(ps.subject)} <span class="muted small">(${ago(ps.commit_time)})</span>` : ` <span class="muted small">· not a git checkout, update by running install.sh from a new copy</span>`}</div>
    ${ps && ps.behind ? pill("info", ps.behind + " new commits") : ps ? pill("ok", "up to date") : ""}
    ${ps && ps.fetch_error ? `<span class="muted small">fetch: ${esc(ps.fetch_error)}</span>` : ""}
    <span class="spacer" style="flex:1"></span>
    ${ps ? `<button class="btn sm${ps.behind ? " primary" : ""}" data-act="op" data-op="self_update">Update panel</button>` : ""}</div>`;
  const top = S.insights.filter((i) => i.severity === "warn" || i.severity === "update").slice(0, 8);
  if (top.length) html += `<h3>Needs attention</h3><div class="card" style="padding:0">${top.map(insightRow).join("")}</div>`;
  return html;
}
const tile = (big, label, sub) => `<div class="card tile"><div class="big">${esc(big)}</div><div>${esc(label)}</div><div class="sub">${esc(sub)}</div></div>`;
const pill = (cls, text) => `<span class="pill ${cls}">${esc(text)}</span>`;

function stacksView() {
  const accts = S.accounts.map((a) => a.account);
  let list = allStacks();
  if (filter.account) list = list.filter((s) => s.account === filter.account);
  if (filter.q) { const q = filter.q.toLowerCase(); list = list.filter((s) => s.name.toLowerCase().includes(q) || s.containers.some((c) => (c.name + " " + c.image).toLowerCase().includes(q))); }
  if (filter.only === "running") list = list.filter((s) => s.running);
  if (filter.only === "stopped") list = list.filter((s) => !s.running);
  if (filter.only === "noboot") list = list.filter((s) => s.running && !s.autostart);
  if (filter.only === "updates") list = list.filter((s) => stackUpdate(s.account, s));
  const tb = `<div class="toolbar">
    <select id="f-account" aria-label="Account"><option value="">All accounts</option>${accts.map((a) => `<option ${a === filter.account ? "selected" : ""}>${esc(a)}</option>`).join("")}</select>
    <select id="f-only" aria-label="Show">${[["", "All stacks"], ["running", "Running"], ["stopped", "Stopped"], ["noboot", "Not starting on boot"], ["updates", "With updates"]].map(([v, l]) => `<option value="${v}" ${v === filter.only ? "selected" : ""}>${l}</option>`).join("")}</select>
    <input id="f-q" placeholder="Search name or image" value="${esc(filter.q)}" aria-label="Search">
    <span class="spacer"></span>
    <button class="btn" data-act="load-stats">Load CPU / memory</button>
  </div>`;
  if (!list.length) return tb + `<div class="empty">No stacks match.</div>`;
  return tb + list.map(stackCard).join("");
}

function stackCard(st) {
  const [cls, label] = stackState(st);
  const upd = stackUpdate(st.account, st);
  const warns = insightsFor(st.account, st.key);
  const k = `data-account="${esc(st.account)}" data-stack="${esc(st.key)}"`;
  const unit = st.unit;
  const unitPill = unit ? (unit.active === "active" ? pill("ok", "systemd") : st.running ? pill("warn", "outside systemd") : pill("off", "systemd " + unit.active)) : pill("off", "no unit");
  const updPill = upd === "image" ? pill("info", "image update") : upd === "source" ? pill("info", "new commits") : upd === "rebuild" ? pill("info", "rebuild needed") : "";
  const script = st.scripts[0];
  const rows = st.containers.map((c) => {
    const s = (stats[st.account] || {})[c.name];
    const ports = c.ports.map((p) => `${p.host_ip && p.host_ip !== "0.0.0.0" ? p.host_ip + ":" : ""}${p.host}→${p.ctr}`).join(", ");
    const health = c.health ? pill(c.health === "healthy" ? "ok" : c.health === "unhealthy" ? "bad" : "warn", c.health) : "";
    const imgU = ((S.updates[st.account] || {}).images || {})[c.image];
    return `<tr><td><b>${esc(c.name)}</b><div class="img">${esc(c.image)}${imgU && imgU.status === "outdated" ? " · <span style='color:var(--info)'>newer digest</span>" : ""}</div></td>
      <td>${pill(c.state === "running" ? "ok" : "off", c.state)} ${health}<div class="muted small">${esc(c.status)}</div></td>
      <td class="mono">${esc(ports) || '<span class="muted">—</span>'}</td>
      <td class="mono small">${s ? esc(s.cpu) + "<br>" + esc(s.mem) : '<span class="muted">—</span>'}</td>
      <td style="text-align:right;white-space:nowrap"><button class="btn sm" data-act="logs" data-account="${esc(st.account)}" data-container="${esc(c.name)}">Logs</button>
      <button class="btn sm" data-act="inspect" data-account="${esc(st.account)}" data-container="${esc(c.name)}">Details</button></td></tr>`;
  }).join("");
  return `<section class="stack">
    <div class="stack-head">
      <span class="name">${esc(st.name)}</span><span class="acct">${esc(st.account)} · ${st.kind}</span>
      ${pill(cls, label)} ${unitPill} ${updPill} ${st.rollback.length ? pill("off", "rollback available") : ""} ${warns.length ? `<span class="pill warn" title="${esc(warns.map((w) => w.title).join("\n"))}">${warns.length} warning${warns.length > 1 ? "s" : ""}</span>` : ""}
      <span class="grow"></span>
      <label class="switch" title="${unit ? esc(unit.name) + " (" + esc(unit.file_state) + ")" : "Creates a systemd unit and enables it"}"><input type="checkbox" data-act="autostart" ${k} ${st.autostart ? "checked" : ""}>Start on boot</label>
      <div class="actions">
        ${st.running ? `<button class="btn sm" data-act="op" data-op="restart" ${k}>Restart</button><button class="btn sm" data-act="op" data-op="stop" ${k}>Stop</button>` : `<button class="btn sm primary" data-act="op" data-op="start" ${k}>Start</button>`}
        <button class="btn sm${upd ? " primary" : ""}" data-act="op" data-op="update" ${k} title="${script ? "Runs " + esc(script.path) + " update" : "Pulls images and recreates through systemd"}">Update</button>
        <div class="menu"><button class="btn sm" data-act="menu" aria-haspopup="true">⋯</button>
          <div class="menu-list hidden">
            ${st.scripts.map((s) => `<button data-act="op" data-op="run_script" data-script="${esc(s.path)}" data-arg="" ${k}>Run ${esc(s.path.split("/").slice(-2).join("/"))}</button>
              ${s.update_arg ? `<button data-act="op" data-op="run_script" data-script="${esc(s.path)}" data-arg="update" ${k}>Run ${esc(s.path.split("/").pop())} update</button>` : ""}`).join("")}
            ${st.scripts.length ? "<hr>" : ""}
            ${st.rollback.length && unit ? `<button data-act="op" data-op="rollback" data-images="${esc(st.rollback.join(", "))}" ${k}>Roll back to previous image</button><hr>` : ""}
            ${unit ? `<button data-act="unit" data-account="${esc(st.account)}" data-unit="${esc(unit.name)}">View systemd unit</button>` : ""}
            ${unit && unit.active !== "active" && st.running ? `<button data-act="op" data-op="adopt" ${k}>Restart under systemd</button>` : ""}
            <button data-act="op" data-op="regenerate_unit" ${k}>Regenerate systemd unit</button>
            ${!st.running ? `<hr><button class="danger" data-act="op" data-op="remove" ${k}>Remove ${st.kind}</button>` : ""}
          </div></div>
      </div>
    </div>
    <div class="stack-body table-wrap"><table class="ctr-table"><thead><tr><th>Container</th><th>State</th><th>Ports</th><th>CPU / Mem</th><th></th></tr></thead><tbody>${rows || `<tr><td colspan="5" class="muted">No containers</td></tr>`}</tbody></table></div>
    ${st.scripts.length ? `<div class="muted small" style="padding:6px 16px 10px">Script: <code>${esc(st.scripts.map((s) => s.path).join(", "))}</code>${st.sources.length ? ` · source: <code>${esc(st.sources.join(", "))}</code>` : ""}</div>` : ""}
  </section>`;
}

function updatesView() {
  const n = new Set(S.insights.filter((i) => i.severity === "update" && i.action).map((i) => i.account + i.stack)).size;
  let html = `<div class="toolbar"><span class="muted">Registry digests are compared with the local image; custom images are compared with their git checkout. Last check: ${esc(ago(S.update_state.finished))}${S.update_state.running ? " (running…)" : ""}.</span>
    <span class="spacer"></span><button class="btn primary" data-act="update-all" ${n ? "" : "disabled"}>Update all (${n})</button></div>`;
  if (!Object.keys(S.updates).length) return html + `<div class="empty">No check has finished yet. Use “Check updates”.</div>`;
  const order = { outdated: 0, unknown: 1, current: 2, pinned: 3, custom: 4 };
  for (const a of S.accounts) {
    const u = S.updates[a.account];
    if (!u) continue;
    const users = (ref) => a.stacks.filter((s) => s.containers.some((c) => c.image === ref));
    const imgs = Object.entries(u.images).filter(([, v]) => v.status !== "custom").sort((x, y) => order[x[1].status] - order[y[1].status] || x[0].localeCompare(y[0]));
    html += `<h3>${esc(a.account)}</h3><div class="card"><div class="table-wrap"><table><thead><tr><th>Image</th><th>Status</th><th>Used by</th><th></th></tr></thead><tbody>` +
      imgs.map(([ref, v]) => {
        const by = users(ref);
        const p = v.status === "outdated" ? pill("info", "update available") : v.status === "current" ? pill("ok", "up to date") : v.status === "pinned" ? pill("off", "pinned digest") : pill("warn", "unknown");
        return `<tr><td class="img">${esc(ref)}</td><td>${p}${v.error ? `<div class="muted small">${esc(v.error)}</div>` : ""}</td><td>${by.map((s) => esc(s.name)).join(", ")}</td>
          <td style="text-align:right">${v.status === "outdated" ? by.map((s) => `<button class="btn sm primary" data-act="op" data-op="update" data-account="${esc(a.account)}" data-stack="${esc(s.key)}">Update ${esc(s.name)}</button>`).join(" ") : ""}</td></tr>`;
      }).join("") + `</tbody></table></div></div>`;
    const srcs = Object.values(u.sources || {});
    if (srcs.length) {
      html += `<div class="card" style="margin-top:10px"><div class="table-wrap"><table><thead><tr><th>Custom source</th><th>Latest commit</th><th>State</th><th>Stacks</th></tr></thead><tbody>` +
        srcs.map((s) => {
          const stk = a.stacks.filter((st) => st.sources.includes(s.path));
          const states = stk.map((st) => stackUpdate(a.account, st));
          const p = !s.git ? pill("off", "not a git checkout") : s.behind ? pill("info", s.behind + " commits behind") : states.includes("rebuild") ? pill("info", "image older than source") : pill("ok", "image current");
          return `<tr><td class="mono">${esc(s.path)}</td><td>${s.commit ? `<code>${esc(s.commit)}</code> ${esc(s.subject)}<div class="muted small">${ago(s.commit_time)}${s.dirty ? ` · ${s.dirty} uncommitted changes` : ""}</div>` : "—"}</td>
            <td>${p}${s.fetch_error ? `<div class="muted small">fetch: ${esc(s.fetch_error)}</div>` : ""}</td>
            <td>${stk.map((st) => `<button class="btn sm${stackUpdate(a.account, st) ? " primary" : ""}" data-act="op" data-op="update" data-account="${esc(a.account)}" data-stack="${esc(st.key)}">Rebuild ${esc(st.name)}</button>`).join(" ")}</td></tr>`;
        }).join("") + `</tbody></table></div></div>`;
    }
  }
  return html;
}

function insightRow(i) {
  const sev = { warn: ["warn", "warning"], update: ["info", "update"], info: ["accent", "info"], tip: ["off", "tip"] }[i.severity];
  const label = i.action ? ({ prune_dangling: "Prune", prune_unused: "Prune unused", enable_linger: "Enable linger", remove_orphan_unit: "Remove unit", autostart_on: "Enable start on boot", adopt: "Restart under systemd", remove: "Remove", update: "Update" })[i.action.op] || "Fix" : "";
  const data = i.action ? Object.entries(i.action).map(([k, v]) => `data-${k}="${esc(v)}"`).join(" ") : "";
  return `<div class="insight">${pill(sev[0], sev[1])}<div><div class="t">${esc(i.title)}${i.account ? ` <span class="muted small">· ${esc(i.account)}</span>` : ""}</div><div class="d">${esc(i.detail)}</div></div>
    <div>${i.action ? `<button class="btn sm" data-act="op" ${data}>${esc(label)}</button>` : ""}</div></div>`;
}
function insightsView() {
  if (!S.insights.length) return `<div class="empty">No suggestions. Everything looks tidy.</div>`;
  const groups = [["warn", "Warnings"], ["update", "Updates"], ["info", "Housekeeping"], ["tip", "Suggestions"]];
  return groups.map(([sev, title]) => {
    const items = S.insights.filter((i) => i.severity === sev);
    return items.length ? `<h3>${title} (${items.length})</h3><div class="card" style="padding:0">${items.map(insightRow).join("")}</div>` : "";
  }).join("");
}

function imagesView() {
  return S.accounts.map((a) => {
    const df = (a.df || []).find((d) => d.Type === "Images") || {};
    const imgs = [...a.images].sort((x, y) => (y.containers > 0) - (x.containers > 0) || y.size - x.size);
    const dang = imgs.filter((i) => i.dangling && !i.containers);
    return `<h3>${esc(a.account)}</h3><div class="toolbar"><span>${imgs.length} images · ${fmtBytes(df.RawSize)} · reclaimable ${fmtBytes(df.RawReclaimable)}</span><span class="spacer"></span>
      <button class="btn sm" data-act="op" data-op="prune_dangling" data-account="${esc(a.account)}" ${dang.length ? "" : "disabled"}>Prune ${dang.length} dangling</button>
      <button class="btn sm danger" data-act="op" data-op="prune_unused" data-account="${esc(a.account)}">Prune all unused</button></div>
      <div class="card"><div class="table-wrap"><table><thead><tr><th>Image</th><th>ID</th><th>Size</th><th>Created</th><th>In use</th></tr></thead><tbody>${imgs.slice(0, 200).map((i) =>
        `<tr><td class="img">${i.names.length ? i.names.map(esc).join("<br>") : '<span class="muted">&lt;none&gt;</span>'}</td><td class="mono">${esc(i.id)}</td><td>${fmtBytes(i.size)}</td><td>${ago(i.created)}</td><td>${i.containers ? pill("ok", i.containers + "") : pill("off", "no")}</td></tr>`).join("")}</tbody></table></div></div>`;
  }).join("");
}

function jobsView() {
  let html = !S.jobs.length ? `<div class="empty">No jobs yet. Actions like update, restart and prune run here.</div>` :
    `<div class="card" style="padding:0"><div class="table-wrap"><table><thead><tr><th>Job</th><th>Status</th><th>Started</th><th>Duration</th><th></th></tr></thead><tbody>` +
    S.jobs.map((j) => `<tr><td>${esc(j.title)}</td><td>${jobPill(j.status)}</td><td>${ago(j.started)}</td><td>${j.ended ? Math.round(j.ended - j.started) + " s" : "…"}</td>
      <td style="text-align:right"><button class="btn sm" data-act="job" data-job="${j.id}">Output</button></td></tr>`).join("") + `</tbody></table></div></div>`;
  html += `<h3>Activity log</h3><div class="toolbar"><span class="muted small">Every sign-in, failed sign-in and change, from the server's audit log.</span><span class="spacer"></span>
    <button class="btn sm" data-act="audit">Show</button><button class="btn sm danger" data-act="logout-all">Sign out all sessions</button></div><div id="audit"></div>`;
  return html;
}
async function showAudit() {
  try {
    const r = await api("audit");
    const what = (e) => e.event === "action" ? `${String(e.op).replace(/_/g, " ")} ${e.account}${e.stack ? "/" + e.stack.split(":")[1] : ""}${e.unit ? " " + e.unit : ""}${e.arg ? " " + e.arg : ""}` : e.event.replace(/_/g, " ") + (e.reason ? ": " + e.reason : "");
    $("#audit").innerHTML = `<div class="card" style="padding:0"><div class="table-wrap"><table><thead><tr><th>When</th><th>What</th><th>From</th></tr></thead><tbody>` +
      (r.entries.map((e) => `<tr><td title="${esc(new Date(e.time * 1000).toLocaleString())}">${ago(e.time)}</td><td>${e.event === "login_failed" ? pill("bad", "failed") + " " : ""}${esc(what(e))}</td><td class="mono small">${esc(e.client)}</td></tr>`).join("") || `<tr><td colspan="3" class="muted">Empty</td></tr>`) +
      `</tbody></table></div></div>`;
  } catch (err) { toast(err.message, true); }
}
const jobPill = (s) => pill({ succeeded: "ok", failed: "bad", running: "info", queued: "off", cancelled: "warn" }[s] || "off", s);

// ---------- modal
function modal(title, html) {
  $("#modal-title").textContent = title;
  $("#modal-body").innerHTML = html;
  const d = $("#modal");
  if (!d.open) d.showModal();
}
$("#modal-close").addEventListener("click", () => $("#modal").close());
$("#modal").addEventListener("close", () => { openJob = null; clearInterval(logTimer); });

async function showJob(id) {
  openJob = id;
  let offset = 0;
  modal("Job", `<div id="job-head" class="toolbar"></div><pre class="out" id="job-out"></pre>`);
  while (openJob === id) {
    let j;
    try { j = await api(`jobs/${id}?offset=${offset}`); } catch (e) { toast(e.message, true); return; }
    if (openJob !== id) return;
    $("#modal-title").textContent = j.title;
    $("#job-head").innerHTML = `${jobPill(j.status)} <span class="muted small">${esc(j.account)}</span><span class="spacer"></span>${j.status === "running" ? `<button class="btn sm danger" data-act="cancel-job" data-job="${j.id}">Cancel</button>` : ""}`;
    const out = $("#job-out");
    const atBottom = out.scrollTop + out.clientHeight >= out.scrollHeight - 30;
    out.textContent += j.output;
    offset = j.offset;
    if (atBottom) out.scrollTop = out.scrollHeight;
    if (j.status !== "running" && j.status !== "queued") { refresh(true); return; }
    await new Promise((r) => setTimeout(r, 1000));
  }
}

// ---------- actions
const CONFIRM = {
  stop: "Stop {name}?",
  remove: "Remove {name}? Containers are deleted; volumes and bind-mounted data are kept.",
  adopt: "Stop {name} and start it again through its systemd unit? The unit recreates it from its saved definition (short downtime).",
  update: "Update {name}? It will be recreated with the new image (short downtime).",
  run_script: "Run {script} {arg} as {account}? This recreates the stack.",
  prune_unused: "Remove every image not used by a container for {account}? Base images of your builds are pulled again next build.",
  prune_dangling: "Remove all dangling images for {account}?",
  remove_orphan_unit: "Disable and delete {unit} and its container units?",
  regenerate_unit: "Rewrite the systemd unit files of {name} from its current definition?",
  enable_linger: "Enable lingering for {account}?",
  rollback: "Roll {name} back to the image it ran before the last update ({images})? It is recreated through its systemd unit.",
  self_update: "Update the panel? It pulls its source, reinstalls and restarts. You will need to sign in again.",
};
async function runOp(d) {
  const body = { op: d.op, account: d.account };
  for (const k of ["stack", "unit", "script", "arg"]) if (d[k] !== undefined) body[k] = d[k];
  const name = (d.stack || "").split(":")[1] || "";
  const msg = CONFIRM[d.op];
  if (msg && !confirm(msg.replace("{name}", name).replace("{account}", d.account).replace("{unit}", d.unit || "").replace("{script}", d.script || "").replace("{arg}", d.arg || "").replace("{images}", d.images || ""))) return;
  try {
    const r = await api("action", body);
    toast("Started: " + d.op.replace(/_/g, " ") + (name ? " " + name : ""));
    showJob(r.job);
  } catch (e) { toast(e.message, true); }
}

document.addEventListener("click", async (e) => {
  const t = e.target.closest("[data-act], .tabs button");
  document.querySelectorAll(".menu-list").forEach((m) => { if (!t || !m.parentElement.contains(t) || t.dataset.act !== "menu") m.classList.add("hidden"); });
  if (!t) return;
  if (t.matches(".tabs button")) { tab = t.dataset.tab; localSet("tab", tab); render(); return; }
  const d = t.dataset;
  switch (d.act) {
    case "menu": t.nextElementSibling.classList.toggle("hidden"); break;
    case "op": runOp(d); break;
    case "goto-account": filter.account = d.account; tab = "stacks"; render(); break;
    case "job": showJob(+d.job); break;
    case "cancel-job": api(`jobs/${d.job}/cancel`, {}).catch((err) => toast(err.message, true)); break;
    case "logs": showLogs(d.account, d.container); break;
    case "audit": showAudit(); break;
    case "update-all": {
      if (!confirm("Update every stack with an available update? They run one after another per account, each recreated with short downtime. Previous images are kept for rollback.")) break;
      try {
        const r = await api("action", { op: "update_all" });
        toast(`Queued ${r.jobs.length} updates` + (r.skipped.length ? `, skipped ${r.skipped.length}` : ""), !!r.skipped.length);
        if (r.skipped.length) modal("Skipped", `<pre class="out">${esc(r.skipped.join("\n"))}</pre>`);
        tab = "jobs"; localSet("tab", tab); refresh(true);
      } catch (err) { toast(err.message, true); }
      break;
    }
    case "logout-all":
      if (confirm("Sign out every session, including this one?")) { await api("logout-all", {}).catch(() => {}); showLogin(); }
      break;
    case "unit": {
      try { const r = await api(`unit?account=${encodeURIComponent(d.account)}&unit=${encodeURIComponent(d.unit)}`); modal(d.unit, `<pre class="out">${esc(r.output)}</pre>`); }
      catch (err) { toast(err.message, true); }
      break;
    }
    case "inspect": showInspect(d.account, d.container); break;
    case "load-stats": loadStats(t); break;
  }
});
document.addEventListener("change", (e) => {
  const t = e.target;
  if (t.dataset.act === "autostart") {
    t.checked = !t.checked;  // reflect real state after the job
    runOp({ op: t.checked ? "autostart_off" : "autostart_on", account: t.dataset.account, stack: t.dataset.stack });
  }
  if (t.id === "f-account") { filter.account = t.value; render(); }
  if (t.id === "f-only") { filter.only = t.value; render(); }
});
document.addEventListener("input", (e) => {
  if (e.target.id === "f-q") { filter.q = e.target.value; render(); }
});

let logTimer = null;
function showLogs(account, container) {
  clearInterval(logTimer);
  modal(`Logs: ${container}`, `<div class="toolbar">
      <select id="log-tail" aria-label="Lines">${[200, 1000, 5000].map((n) => `<option value="${n}">Last ${n} lines</option>`).join("")}</select>
      <input id="log-filter" placeholder="Filter lines" aria-label="Filter lines">
      <label class="switch"><input type="checkbox" id="log-follow">Follow</label>
      <span class="spacer"></span>
      <button class="btn sm" id="log-refresh">Refresh</button><button class="btn sm" id="log-save">Download</button></div>
    <pre class="out" id="log-out">Loading…</pre>`);
  let raw = "";
  const show = () => {
    const o = $("#log-out");
    if (!o) return;
    const atBottom = o.scrollTop + o.clientHeight >= o.scrollHeight - 30;
    const f = $("#log-filter").value.toLowerCase();
    o.textContent = (f ? raw.split("\n").filter((l) => l.toLowerCase().includes(f)).join("\n") : raw) || "(no output)";
    if (atBottom) o.scrollTop = o.scrollHeight;
  };
  const load = async () => {
    try { raw = (await api(`logs?account=${encodeURIComponent(account)}&container=${encodeURIComponent(container)}&tail=${$("#log-tail").value}`)).output; }
    catch (err) { raw = err.message; }
    show();
  };
  $("#log-refresh").addEventListener("click", load);
  $("#log-tail").addEventListener("change", load);
  $("#log-filter").addEventListener("input", show);
  $("#log-follow").addEventListener("change", (e) => {
    clearInterval(logTimer);
    if (e.target.checked) logTimer = setInterval(load, 3000);
  });
  $("#log-save").addEventListener("click", () => {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([raw], { type: "text/plain" }));
    a.download = `${container}-${new Date().toISOString().slice(0, 19).replace(/:/g, "")}.log`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  });
  $("#log-out").scrollTop = 0;
  load().then(() => { const o = $("#log-out"); if (o) o.scrollTop = o.scrollHeight; });
}

async function showInspect(account, container) {
  modal(container, "Loading…");
  try {
    const r = await api(`inspect?account=${encodeURIComponent(account)}&container=${encodeURIComponent(container)}`);
    const row = (k, v) => `<div>${esc(k)}</div><div>${v}</div>`;
    $("#modal-body").innerHTML = `<div class="kv">
      ${row("Image", `<code>${esc(r.image)}</code>`)}${row("Command", `<code>${esc((r.command || []).join(" "))}</code>`)}
      ${row("Restart policy", esc(r.restart || "none"))}${row("Network", esc(r.network))}
      ${row("Mounts", r.mounts.map((m) => `<code>${esc(m.src || "(volume)")} → ${esc(m.dst)}</code>${m.rw ? "" : " (ro)"}`).join("<br>") || "—")}
      ${row("Environment", r.env.map((x) => `<code>${esc(x)}</code>`).join(" ") || "—")}
      ${row("Labels", Object.entries(r.labels).map(([k, v]) => `<code>${esc(k)}=${esc(v)}</code>`).join("<br>") || "—")}
    </div><p class="muted small">Environment values are hidden.</p>`;
  } catch (err) { $("#modal-body").textContent = err.message; }
}

async function loadStats(btn) {
  btn.disabled = true; btn.textContent = "Loading…";
  const accts = filter.account ? [filter.account] : S.accounts.map((a) => a.account);
  await Promise.all(accts.map(async (a) => {
    try {
      const r = await api(`stats?account=${encodeURIComponent(a)}`);
      stats[a] = {};
      for (const s of r.stats) {
        const name = s.name || s.Name;
        const cpu = s.cpu_percent ?? s.CPU ?? s.CPUPerc;
        const mem = s.mem_usage ?? s.MemUsage ?? (s.mem_usage_bytes ? fmtBytes(s.mem_usage_bytes) : "");
        stats[a][name] = { cpu: typeof cpu === "number" ? cpu.toFixed(1) + "%" : cpu, mem: typeof mem === "number" ? fmtBytes(mem) : String(mem).split("/")[0].trim() };
      }
    } catch (err) { toast(`${a}: ${err.message}`, true); }
  }));
  render();
}

$("#btn-refresh").addEventListener("click", () => refresh(true));
$("#btn-updates").addEventListener("click", async () => {
  try { await api("check-updates", {}); toast("Update check started"); refresh(); } catch (e) { toast(e.message, true); }
});
$("#btn-logout").addEventListener("click", async () => { await api("logout", {}).catch(() => {}); showLogin(); });

api("state").then((s) => { S = s; start(); }).catch(() => {});
