/* Kovan arayüzü — bağımlılıksız, CSP uyumlu (inline script/style yok).
   Tüm metinler textContent ile yazılır; yalnız sunucuda nh3 ile temizlenmiş doküman HTML'i innerHTML kullanır. */
"use strict";

const CSRF = document.querySelector('meta[name="csrf-token"]').content;
const $ = (sel, root = document) => root.querySelector(sel);

const S = {
  projects: [], categories: [], runtimes: {}, settings: {}, launchers: [],
  filter: { kind: "all", value: null }, search: "", selected: new Set(),
  detail: null,            // { id, tab }
  logs: {}, logSeq: {}, errors: {}, errorCount: {},
  logUI: { stream: "all", q: "", paused: false, ts: true },
  draft: null,             // yapılandırma sekmesi taslağı
  ws: null,
};

const STATE_LABEL = { running: "Çalışıyor", starting: "Başlatılıyor", stopping: "Durduruluyor",
  crashed: "Çöktü", stopped: "Durdu", unhealthy: "Yanıt vermiyor", external: "Dışarıdan kapatıldı",
  orphaned: "Yetim süreç", backoff: "Yeniden başlatılıyor", crash_loop: "Çökme döngüsü", adopted: "Devralındı" };
const END_REASON = { user_stop: "Kullanıcı durdurdu", exited_ok: "Tamamlandı", external_stop: "Dışarıdan kapatıldı",
  killed: "Öldürüldü", crashed: "Çöktü", spawn_failed: "Başlatılamadı", panel_restart: "Panel yeniden başladı",
  panel_shutdown: "Panel kapandı" };
const PROBLEM_STATES = ["crashed", "unhealthy", "orphaned", "crash_loop", "external"];
const LAUNCHER_LABEL = { script: "script", module: "module", uvicorn: "uvicorn", flask: "flask",
  django: "django", streamlit: "streamlit", custom: "özel" };

// ---------------------------------------------------------------- yardımcılar
function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "text") el.textContent = v;
    else if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "style") Object.assign(el.style, v);
    else if (k === "vars") for (const [n, val] of Object.entries(v)) el.style.setProperty(n, val);
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

// append/replaceChildren null'ı "null" metnine, diziyi "[object …]" metnine çevirir; bu yardımcılar düzleştirip temizler
const clean = c => c.flat(Infinity).filter(x => x !== null && x !== undefined && x !== false);
const put = (el, ...c) => el.replaceChildren(...clean(c));
const add = (el, ...c) => el.append(...clean(c));

async function api(method, url, body) {
  const opts = { method, headers: { "X-CSRF-Token": CSRF }, credentials: "same-origin" };
  if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  const res = await fetch(url, opts);
  if (res.status === 401) { location.reload(); throw new Error("Oturum sona erdi"); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) { const e = new Error(data.error || data.detail || `Hata ${res.status}`); e.data = data; e.status = res.status; throw e; }
  return data;
}

function toast(msg, kind = "") {
  const t = h("div", { class: `toast ${kind}`, role: "status", text: msg });
  add($("#toasts"), t);
  setTimeout(() => t.remove(), kind === "err" ? 7000 : 3500);
}

function fmtDuration(sec) {
  sec = Math.max(0, Math.floor(sec));
  const d = Math.floor(sec / 86400), hh = Math.floor(sec % 86400 / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60;
  if (d) return `${d}g ${hh}s`;
  if (hh) return `${hh}s ${m}dk`;
  if (m) return `${m}dk ${s}sn`;
  return `${s}sn`;
}
const fmtTime = ts => new Date(ts * 1000).toLocaleTimeString("tr-TR", { hour12: false });
const fmtDate = ts => new Date(ts * 1000).toLocaleString("tr-TR", { hour12: false });
const rt = id => S.runtimes[id] || { state: "stopped", ports: [] };
const proj = id => S.projects.find(p => p.id === id);
const cat = id => S.categories.find(c => c.id === id);
const isActive = st => ["running", "starting", "stopping", "unhealthy", "adopted"].includes(st);

function icon(name) {
  const paths = {
    play: "M7 5v14l11-7z", stop: "M6 6h12v12H6z",
    restart: "M4 12a8 8 0 1 0 2.3-5.6M4 4v4h4", trash: "M5 7h14M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3",
    edit: "M4 20h4L19 9l-4-4L4 16zM14 6l4 4", up: "m6 15 6-6 6 6", down: "m6 9 6 6 6-6", x: "M6 6l12 12M18 6 6 18",
  };
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24"); svg.setAttribute("fill", name === "play" || name === "stop" ? "currentColor" : "none");
  svg.setAttribute("stroke", "currentColor"); svg.setAttribute("stroke-width", "2"); svg.setAttribute("stroke-linecap", "round");
  const p = document.createElementNS("http://www.w3.org/2000/svg", "path"); p.setAttribute("d", paths[name]); svg.append(p);
  return svg;
}

function stateBadge(state, r) {
  let label = STATE_LABEL[state] || state;
  if (state === "crashed" && r?.end_reason === "killed") label = "Öldürüldü";
  if (state === "backoff" && r?.next_restart_at) label += ` · ${Math.max(0, Math.ceil(r.next_restart_at - Date.now() / 1000))} sn`;
  return h("span", { class: `badge st-${state}`, title: r?.reason_text || "",
    "data-backoff": state === "backoff" && r?.next_restart_at ? r.next_restart_at : null },
    h("span", { class: "dot" }), label);
}

function catChip(cid) {
  const c = cat(cid);
  if (!c) return h("span", { class: "chip muted", text: "Kategorisiz" });
  const parent = c.parent_id ? cat(c.parent_id) : null;
  return h("span", { class: "chip", vars: { "--c": c.color } }, h("span", { class: "sw" }),
    parent ? `${parent.name} › ${c.name}` : c.name);
}

function portLinks(p) {
  const r = rt(p.id);
  const profile = activeProfile(p);
  if (r.ports && r.ports.length) {
    return h("span", { class: "row wrap" }, r.ports.slice(0, 4).map(port =>
      h("a", { class: "port-link", href: `http://127.0.0.1:${port}/`, target: "_blank", rel: "noopener noreferrer",
        onclick: e => e.stopPropagation(), text: `:${port}` })));
  }
  if (profile.port) return h("span", { class: "port-cfg", title: "Yapılandırılan port", text: `:${profile.port}` });
  return h("span", { class: "muted", text: "—" });
}

const activeProfile = p => p.profiles.find(x => x.name === p.active_profile) || p.profiles[0];

// ---------------------------------------------------------------- veri yükleme
async function loadState() {
  const d = await api("GET", "/api/state");
  Object.assign(S, { projects: d.projects, categories: d.categories, runtimes: d.runtimes,
    settings: d.settings, launchers: d.launchers, unread: d.unread_notifications });
  $("#metrics-toggle").checked = !!d.settings.metrics_enabled;
  renderBell();
  const qb = $("#quarantine-banner");
  qb.classList.toggle("hidden", !d.quarantine);
  if (d.quarantine) put(qb, `${d.quarantine} yapılandırma kaydı okunamadı ancak silinmedi. `,
    h("a", { href: "#", text: "Ayrıntılar", onclick: ev => { ev.preventDefault(); openSettings("diag"); } }));
  renderAll();
  // Sayfa yenilendiğinde çökmüş projelerin son hatası listede görünsün
  for (const [id, r] of Object.entries(d.runtimes)) if (r.state === "crashed" && !S.errors[id]) loadErrors(Number(id)).then(renderList);
}

function renderAll() {
  renderNav(); renderSummary(); renderList(); renderBulkbar();
  if (S.detail) renderDrawerHead();
}

// ---------------------------------------------------------------- kenar çubuğu
function filteredProjects() {
  const q = S.search.trim().toLowerCase();
  let list = S.projects.filter(p => {
    const f = S.filter;
    if (f.kind === "fav" && !p.favorite) return false;
    if (f.kind === "none" && p.category_id !== null && cat(p.category_id)) return false;
    if (f.kind === "cat") {
      const c = cat(p.category_id);
      if (!c || (c.id !== f.value && c.parent_id !== f.value)) return false;
    }
    if (f.kind === "tag" && !p.tags.includes(f.value)) return false;
    if (f.kind === "running" && !isActive(rt(p.id).state)) return false;
    if (f.kind === "crashed" && !PROBLEM_STATES.includes(rt(p.id).state)) return false;
    if (f.kind === "external" && !(rt(p.id).external || []).length) return false;
    if (!q) return true;
    const r = rt(p.id);
    const hay = [p.name, p.description, p.path, ...p.tags, cat(p.category_id)?.name || "",
      activeProfile(p).launcher, String(activeProfile(p).port || ""), ...(r.ports || []).map(String)].join(" ").toLowerCase();
    return hay.includes(q);
  });
  const order = { crash_loop: 0, orphaned: 0, crashed: 0, unhealthy: 0, external: 1, backoff: 1, running: 2,
    starting: 2, stopping: 2, adopted: 2, stopped: 3 };
  return list.sort((a, b) => (b.favorite - a.favorite) || (order[rt(a.id).state] - order[rt(b.id).state]) ||
    a.name.localeCompare(b.name, "tr"));
}

function countFor(fn) { return S.projects.filter(fn).length; }

function navItem(label, filter, count, extra = {}) {
  const active = S.filter.kind === filter.kind && S.filter.value === filter.value;
  const el = h("div", { class: `nav-item ${active ? "active" : ""} ${extra.child ? "child" : ""}`, role: "button", tabindex: "0",
    onclick: () => { S.filter = filter; renderAll(); },
    onkeydown: e => { if (e.key === "Enter") { S.filter = filter; renderAll(); } } },
    extra.swatch ? h("span", { class: "sw", vars: { "--c": extra.swatch } }) : null,
    h("span", { text: label }), h("span", { class: "count", text: count }), extra.actions || null);
  if (extra.dropCategory !== undefined) {
    el.addEventListener("dragover", e => { e.preventDefault(); el.classList.add("drop"); });
    el.addEventListener("dragleave", () => el.classList.remove("drop"));
    el.addEventListener("drop", async e => {
      e.preventDefault(); el.classList.remove("drop");
      const ids = JSON.parse(e.dataTransfer.getData("text/kovan-ids") || "[]");
      if (!ids.length) return;
      await api("POST", "/api/projects/bulk-update", { ids, set_category: true, category_id: extra.dropCategory });
      toast(`${ids.length} proje "${label}" kategorisine taşındı`, "ok");
      await loadState();
    });
  }
  return el;
}

function categoryActions(c) {
  const ids = () => S.projects.filter(p => { const pc = cat(p.category_id); return pc && (pc.id === c.id || pc.parent_id === c.id); }).map(p => p.id);
  return h("span", { class: "cat-actions" },
    h("button", { class: "icon-btn start", title: `${c.name}: tümünü başlat`, "aria-label": "Kategoriyi başlat",
      onclick: e => { e.stopPropagation(); bulkAction("start", ids()); } }, icon("play")),
    h("button", { class: "icon-btn stop", title: `${c.name}: tümünü durdur`, "aria-label": "Kategoriyi durdur",
      onclick: e => { e.stopPropagation(); bulkAction("stop", ids()); } }, icon("stop")));
}

function renderNav() {
  const nav = $("#nav"); put(nav, );
  add(nav, 
    navItem("Tüm projeler", { kind: "all", value: null }, S.projects.length),
    navItem("★ Favoriler", { kind: "fav", value: null }, countFor(p => p.favorite)),
    navItem("● Çalışanlar", { kind: "running", value: null }, countFor(p => isActive(rt(p.id).state))),
    navItem("✖ Sorunlu", { kind: "crashed", value: null }, countFor(p => PROBLEM_STATES.includes(rt(p.id).state))),
    navItem("⇄ Harici çalışan", { kind: "external", value: null }, countFor(p => (rt(p.id).external || []).length)),
  );
  add(nav, h("div", { class: "nav-section" }, "Kategoriler",
    h("button", { class: "icon-btn", title: "Kategorileri düzenle", "aria-label": "Kategorileri düzenle", onclick: openCategoryManager }, icon("edit"))));
  const roots = S.categories.filter(c => !c.parent_id || !cat(c.parent_id));
  for (const c of roots) {
    const n = countFor(p => { const pc = cat(p.category_id); return pc && (pc.id === c.id || pc.parent_id === c.id); });
    add(nav, navItem(c.name, { kind: "cat", value: c.id }, n, { swatch: c.color, dropCategory: c.id, actions: categoryActions(c) }));
    for (const ch of S.categories.filter(x => x.parent_id === c.id)) {
      add(nav, navItem(ch.name, { kind: "cat", value: ch.id }, countFor(p => p.category_id === ch.id),
        { swatch: ch.color, child: true, dropCategory: ch.id, actions: categoryActions(ch) }));
    }
  }
  add(nav, navItem("Kategorisiz", { kind: "none", value: null }, countFor(p => !cat(p.category_id)), { dropCategory: null }));
  const tags = [...new Set(S.projects.flatMap(p => p.tags))].sort();
  if (tags.length) {
    add(nav, h("div", { class: "nav-section", text: "Etiketler" }));
    add(nav, h("div", { class: "tag-list" }, tags.map(t => h("span", {
      class: `chip tag ${S.filter.kind === "tag" && S.filter.value === t ? "active" : ""}`, text: `#${t}`,
      onclick: () => { S.filter = S.filter.value === t ? { kind: "all", value: null } : { kind: "tag", value: t }; renderAll(); } }))));
  }
}

function renderSummary() {
  const c = { running: 0, crashed: 0, stopped: 0, unhealthy: 0, external: 0 };
  for (const p of S.projects) {
    const st = rt(p.id).state;
    if (st === "unhealthy") c.unhealthy++;
    else if (isActive(st)) c.running++; else if (PROBLEM_STATES.includes(st)) c.crashed++; else c.stopped++;
    if ((rt(p.id).external || []).length) c.external++;
  }
  put($("#summary"),
    h("span", {}, h("span", { class: "dot running" }), h("b", { text: c.running }), " çalışıyor"),
    c.unhealthy ? h("span", {}, h("span", { class: "dot starting" }), h("b", { text: c.unhealthy }), " yanıt vermiyor") : null,
    h("span", {}, h("span", { class: "dot crashed" }), h("b", { text: c.crashed }), " sorunlu"),
    c.external ? h("span", {}, h("span", { class: "dot ext" }), h("b", { text: c.external }), " harici") : null,
    h("span", {}, h("span", { class: "dot" }), h("b", { text: c.stopped }), " durdu"),
    h("span", { class: "grow" }),
    S.selected.size ? h("span", { text: `${S.selected.size} seçili` }) : null);
}

// ---------------------------------------------------------------- proje listesi
function metricsOn(p) { return S.settings.metrics_enabled || p.metrics_enabled; }

function renderList() {
  const content = $("#content");
  if (!S.projects.length) { put(content, renderOnboarding()); return; }
  const list = filteredProjects();
  const showMetrics = S.projects.some(metricsOn);
  const allSel = list.length && list.every(p => S.selected.has(p.id));
  const table = h("table", { class: "projects" },
    h("thead", {}, h("tr", {},
      h("th", { class: "cb" }, h("input", { type: "checkbox", "aria-label": "Tümünü seç", checked: allSel ? true : null,
        onchange: e => { list.forEach(p => e.target.checked ? S.selected.add(p.id) : S.selected.delete(p.id)); renderAll(); } })),
      h("th", { text: "Durum" }), h("th", { text: "Proje" }), h("th", { class: "hide-sm", text: "Kategori" }),
      h("th", { class: "hide-sm", text: "Tür" }), h("th", { text: "Port" }), h("th", { class: "hide-sm", text: "Süre" }),
      showMetrics ? h("th", { text: "CPU / RAM" }) : null, h("th", {}))),
    h("tbody", {}, list.map(p => renderRow(p, showMetrics))));
  put(content, list.length ? table : h("div", { class: "empty muted", text: "Filtreyle eşleşen proje yok." }));
}

function renderRow(p, showMetrics) {
  const r = rt(p.id), profile = activeProfile(p), st = r.state;
  const lastErr = st === "crashed" && r.end_reason === "crashed" ? S.errors[p.id]?.[0] : null;
  const showReason = r.reason_text && !["running", "starting", "stopped"].includes(st) || (st === "stopped" && r.reason_text && r.end_reason && r.end_reason !== "user_stop" && r.end_reason !== "panel_shutdown");
  const tr = h("tr", { "data-id": p.id, draggable: "true",
    class: `${S.selected.has(p.id) ? "selected" : ""} ${S.detail?.id === p.id ? "open" : ""}`,
    onclick: e => { if (e.target.closest("button,input,a,select")) return; openDetail(p.id); } },
    h("td", { class: "cb" }, h("input", { type: "checkbox", "aria-label": `${p.name} seç`, checked: S.selected.has(p.id) ? true : null,
      onchange: e => { e.target.checked ? S.selected.add(p.id) : S.selected.delete(p.id); renderSummary(); renderBulkbar(); tr.classList.toggle("selected", e.target.checked); } })),
    h("td", {}, stateBadge(st, r), ["crashed", "external"].includes(st) && r.exit_code !== null && r.exit_code !== undefined
      ? h("div", { class: "muted mono", text: `çıkış ${r.exit_code}${r.signal ? " · " + r.signal : ""}` }) : null),
    h("td", {},
      h("div", { class: "pname" },
        h("button", { class: `star ${p.favorite ? "on" : ""}`, title: "Favori", "aria-label": "Favori", text: p.favorite ? "★" : "☆",
          onclick: async e => { e.stopPropagation(); await api("POST", "/api/projects/bulk-update", { ids: [p.id], favorite: !p.favorite }); loadState(); } }),
        p.name, p.profiles.length > 1 ? h("span", { class: "chip", text: profile.name }) : null,
        p.tags.slice(0, 3).map(t => h("span", { class: "chip", text: `#${t}` }))),
      p.description ? h("div", { class: "pdesc", title: p.description, text: p.description }) : null,
      lastErr ? h("div", { class: "err-preview", text: `${lastErr.exc_type}: ${lastErr.message}` }) : null,
      showReason && !lastErr ? h("div", { class: `reason-line r-${st}`, text: `${fmtTime(r.changed_at)} · ${r.reason_text}` }) : null,
      externalLine(p)),
    h("td", { class: "hide-sm" }, catChip(p.category_id)),
    h("td", { class: "hide-sm" }, h("span", { class: "chip launcher", text: LAUNCHER_LABEL[profile.launcher] })),
    h("td", {}, portLinks(p)),
    h("td", { class: "hide-sm" }, h("span", { class: "uptime", "data-started": isActive(st) && r.started_at ? r.started_at : "",
      text: isActive(st) && r.started_at ? fmtDuration(Date.now() / 1000 - r.started_at) : "—" })),
    showMetrics ? h("td", {}, metricsCell(p)) : null,
    h("td", { class: "actions" }, actionButtons(p)));
  tr.addEventListener("dragstart", e => {
    const ids = S.selected.has(p.id) ? [...S.selected] : [p.id];
    e.dataTransfer.setData("text/kovan-ids", JSON.stringify(ids)); e.dataTransfer.effectAllowed = "move"; tr.classList.add("dragging");
  });
  tr.addEventListener("dragend", () => tr.classList.remove("dragging"));
  return tr;
}

function metricsCell(p) {
  const m = rt(p.id).metrics;
  if (!metricsOn(p)) return h("span", { class: "muted", text: "kapalı" });
  if (!m) return h("span", { class: "muted", text: isActive(rt(p.id).state) ? "ölçülüyor…" : "—" });
  return h("span", { class: "metric" }, h("b", { text: `${m.cpu}%` }), " · ", h("b", { text: `${m.rss_mb} MB` }));
}

function externalLine(p) {
  const ext = rt(p.id).external || [];
  if (!ext.length) return null;
  return ext.map(e => h("div", { class: "ext-line" },
    h("span", { class: "chip ext", text: "⇄ Panel dışında çalışıyor" }),
    h("span", { class: "mono", text: `pid ${e.pid}${e.ports.length ? " · :" + e.ports.join(", :") : ""}` }),
    h("button", { class: "btn sm", text: "Durdur…", disabled: e.same_user ? null : true,
      title: e.same_user ? "" : "Başka kullanıcıya ait süreç", onclick: ev => { ev.stopPropagation(); confirmStopExternal(p, e); } })));
}

function confirmStopExternal(p, e) {
  openModal("Harici süreci durdur", h("div", { class: "stack" },
    h("p", {}, "Bu süreç Kovan tarafından başlatılmadı. Durdurulursa ", h("b", { text: p.name }), " projesinin panel dışındaki örneği kapanır."),
    h("div", { class: "conflict" }, h("div", { class: "mono", text: `PID ${e.pid} · ${e.name}` }),
      h("div", { class: "mono muted", text: e.cmdline }), e.cwd ? h("div", { class: "mono muted", text: `dizin: ${e.cwd}` }) : null),
    h("p", { class: "muted", text: "İşlem denetim kaydına yazılır." })),
  [h("button", { class: "btn", text: "Vazgeç", onclick: closeModal }),
   h("button", { class: "btn", text: "Durdur ve panelden başlat", onclick: () => stopExternal(p, e, true) }),
   h("button", { class: "btn primary", text: "Durdur", onclick: () => stopExternal(p, e, false) })]);
}

async function stopExternal(p, e, thenStart) {
  try {
    await api("POST", `/api/projects/${p.id}/external/stop`, { pid: e.pid, create_time: e.create_time, confirm: true });
    closeModal(); toast("Harici süreç durduruldu", "ok");
    S.runtimes[p.id] = { ...rt(p.id), external: (rt(p.id).external || []).filter(x => x.pid !== e.pid) };
    renderAll();
    if (thenStart) setTimeout(() => doAction(p.id, "start"), 800);
  } catch (err) { toast(err.message, "err"); }
}

function actionButtons(p) {
  const st = rt(p.id).state;
  const active = isActive(st);
  if (st === "orphaned") return [
    h("button", { class: "btn sm danger", text: "Yetimleri kapat", title: `Yetim alt süreçleri sonlandır — ${rt(p.id).reason_text}`,
      onclick: () => api("POST", `/api/projects/${p.id}/orphans/kill`).then(loadState).catch(e => toast(e.message, "err")) })];
  if (st === "backoff" || st === "crash_loop") return [
    h("button", { class: "icon-btn stop", title: "Otomatik yeniden başlatmayı iptal et", "aria-label": "İptal", onclick: () => doAction(p.id, "stop") }, icon("x")),
    h("button", { class: "icon-btn start", title: "Şimdi başlat", "aria-label": "Şimdi başlat", onclick: () => doAction(p.id, "start") }, icon("play"))];
  return [
    active
      ? h("button", { class: "icon-btn stop", title: "Durdur", "aria-label": "Durdur", disabled: st === "stopping" ? true : null, onclick: () => doAction(p.id, "stop") }, icon("stop"))
      : h("button", { class: "icon-btn start", title: "Başlat", "aria-label": "Başlat", onclick: () => doAction(p.id, "start") }, icon("play")),
    h("button", { class: "icon-btn", title: "Yeniden başlat", "aria-label": "Yeniden başlat", disabled: active ? null : true, onclick: () => doAction(p.id, "restart") }, icon("restart")),
  ];
}

function renderOnboarding() {
  const hasRoot = (S.settings.roots || []).length > 0;
  return h("div", { class: "empty" },
    h("h2", { text: "Projelerinizi tek panelden yönetin" }),
    h("p", { class: "muted", text: "CLI pencerelerine gerek kalmadan başlatın, logları izleyin, portları görün." }),
    h("div", { class: "steps" },
      h("div", { class: `step ${hasRoot ? "done" : ""}` }, h("span", { class: "n", text: hasRoot ? "✓" : "1" }),
        h("div", {}, h("b", { text: "Kök klasörü belirleyin" }),
          h("div", { class: "muted", text: hasRoot ? S.settings.roots.join(", ") : "Örn. D:\\code — panel yalnızca bu klasörlerin içindeki projelere erişir." }))),
      h("div", { class: "step" }, h("span", { class: "n", text: "2" }),
        h("div", {}, h("b", { text: "Projeleri keşfedin" }), h("div", { class: "muted", text: "Panel dosyaları okuyarak çalıştırma biçimini (uvicorn, flask, script…) ve kategoriyi önerir; kod çalıştırmaz." }))),
      h("div", { class: "step" }, h("span", { class: "n", text: "3" }),
        h("div", {}, h("b", { text: "Başlatın ve izleyin" }), h("div", { class: "muted", text: "Loglar, hatalar, portlar ve isteğe bağlı CPU/RAM tek ekranda." })))),
    h("div", { class: "row", style: { justifyContent: "center" } },
      hasRoot ? h("button", { class: "btn primary", onclick: openDiscover, text: "⟳ Klasörleri tara" })
        : h("button", { class: "btn primary", onclick: openSettings, text: "Kök klasör ekle" }),
      h("button", { class: "btn", onclick: openAddProject, text: "Elle proje ekle" })));
}

function renderBulkbar() {
  const bar = $("#bulkbar");
  if (!S.selected.size) { bar.classList.add("hidden"); return; }
  bar.classList.remove("hidden");
  const ids = () => [...S.selected];
  const catSel = h("select", { "aria-label": "Kategori ata",
    onchange: async e => {
      if (e.target.value === "") return;
      const cid = e.target.value === "none" ? null : Number(e.target.value);
      await api("POST", "/api/projects/bulk-update", { ids: ids(), set_category: true, category_id: cid });
      toast("Kategori atandı", "ok"); loadState();
    } }, h("option", { value: "", text: "Kategori ata…" }), h("option", { value: "none", text: "Kategorisiz" }),
    S.categories.map(c => h("option", { value: c.id, text: c.parent_id ? `  ↳ ${c.name}` : c.name })));
  put(bar, 
    h("b", { text: `${S.selected.size} seçili` }),
    h("button", { class: "btn sm", onclick: () => bulkAction("start", ids()) }, icon("play"), "Başlat"),
    h("button", { class: "btn sm", onclick: () => bulkAction("stop", ids()) }, icon("stop"), "Durdur"),
    h("button", { class: "btn sm", onclick: () => bulkAction("restart", ids()) }, icon("restart"), "Yeniden"),
    catSel,
    h("button", { class: "btn sm", onclick: async () => {
      const t = prompt("Eklenecek etiket:"); if (!t) return;
      await api("POST", "/api/projects/bulk-update", { ids: ids(), add_tags: [t] }); loadState(); } }, "+ Etiket"),
    h("button", { class: "btn sm", onclick: async () => { await api("POST", "/api/projects/bulk-update", { ids: ids(), metrics_enabled: true }); loadState(); } }, "Metrik aç"),
    h("button", { class: "icon-btn", title: "Seçimi temizle", "aria-label": "Seçimi temizle", onclick: () => { S.selected.clear(); renderAll(); } }, icon("x")));
}

// ---------------------------------------------------------------- işlemler
async function doAction(id, action) {
  try {
    const r = await api("POST", `/api/projects/${id}/${action}`, {});
    S.runtimes[id] = { ...rt(id), ...r };
    renderAll();
  } catch (e) {
    if (e.data?.code === "port_conflict") return showPortConflict(id, e.data.detail);
    toast(`${proj(id)?.name}: ${e.message}`, "err");
  }
}

async function bulkAction(action, ids) {
  if (!ids.length) return toast("Seçili proje yok");
  if (action === "stop" && ids.length > 1 && !confirm(`${ids.length} proje durdurulacak. Emin misiniz?`)) return;
  const res = await api("POST", `/api/bulk/${action}`, { ids });
  const failed = Object.entries(res.results).filter(([, v]) => !v.ok);
  if (failed.length) failed.forEach(([id, v]) => toast(`${proj(Number(id))?.name}: ${v.error}`, "err"));
  else toast(`${ids.length} proje için işlem tamamlandı`, "ok");
  loadState();
}

function showPortConflict(id, c) {
  const p = proj(id);
  openModal(`Port ${c.port} kullanımda`, h("div", { class: "stack" },
    h("div", { class: "conflict" },
      h("div", {}, h("b", { text: `${p.name} ` }), `başlatılamadı: :${c.port} portu başka bir süreç tarafından dinleniyor.`),
      c.pid ? h("div", { class: "mono", text: `PID ${c.pid} · ${c.name}` }) : null,
      c.cmdline ? h("div", { class: "mono muted", text: c.cmdline }) : null,
      c.managed_project ? h("div", { text: `Bu süreç Kovan'te yönetilen "${proj(c.managed_project)?.name}" projesine ait.` }) : null),
    h("p", { class: "muted", text: "Yabancı süreçler Kovan tarafından sonlandırılmaz. Boş bir port seçebilirsiniz." })),
  [
    c.managed_project ? h("button", { class: "btn", text: `"${proj(c.managed_project)?.name}" projesini durdur`,
      onclick: async () => { closeModal(); await doAction(c.managed_project, "stop"); } }) : null,
    c.suggestion ? h("button", { class: "btn primary", text: `:${c.suggestion} portunu kullan ve başlat`, onclick: async () => {
      const prof = activeProfile(p); prof.port = c.suggestion;
      await api("PUT", `/api/projects/${id}`, p); closeModal(); await loadState(); doAction(id, "start"); } }) : null,
    h("button", { class: "btn", text: "Kapat", onclick: closeModal }),
  ]);
}

// ---------------------------------------------------------------- modal
function openModal(title, body, footer = [], wide = false) {
  const m = $("#modal");
  m.className = wide ? "wide" : "";
  put(m, 
    h("div", { class: "modal-head" }, h("h2", { text: title }),
      h("button", { class: "icon-btn", "aria-label": "Kapat", onclick: closeModal }, icon("x"))),
    h("div", { class: "modal-body" }, body),
    footer.length ? h("div", { class: "modal-foot" }, footer) : null);
  if (!m.open) m.showModal();
}
function closeModal() { const m = $("#modal"); if (m.open) m.close(); }

// ---------------------------------------------------------------- ayarlar
const POLICY_LABEL = { never: "Asla", "on-failure": "Hata olursa", always: "Her zaman" };

function fmtSize(n) { return n > 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`; }

function downloadText(name, text, type = "application/json") {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = h("a", { href: url, download: name }); document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

function openSettings(tab = "general") {
  if (typeof tab !== "string") tab = "general";  // olay dinleyicisinden çağrıldığında Event gelir
  const TABS_S = [["general", "Genel"], ["backups", "Yedekler"], ["transfer", "Taşı / Aktar"], ["diag", "Tanılama"], ["audit", "Denetim kaydı"]];
  const body = h("div");
  const tabsEl = h("div", { class: "tabs settings-tabs" });
  const show = k => {
    put(tabsEl, TABS_S.map(([key, label]) => h("button", { class: `tab ${key === k ? "active" : ""}`, text: label, onclick: () => show(key) })));
    put(body);
    ({ general: settingsGeneral, backups: settingsBackups, transfer: settingsTransfer, diag: settingsDiag, audit: settingsAudit })[k](body);
  };
  openModal("Ayarlar", h("div", {}, tabsEl, h("div", { class: "settings-body" }, body)), [h("button", { class: "btn", text: "Kapat", onclick: closeModal })], true);
  show(tab);
}

function settingsGeneral(body) {
  const roots = h("textarea", { rows: 4, placeholder: "D:\\code", "aria-label": "Kök klasörler" });
  roots.value = (S.settings.roots || []).join("\n");
  const [a, b] = S.settings.port_range || [8000, 8999];
  const pa = h("input", { type: "number", value: a, min: 1, max: 65535 }), pb = h("input", { type: "number", value: b, min: 1, max: 65535 });
  const pol = h("select", {}, Object.entries(POLICY_LABEL).map(([v, l]) => h("option", { value: v, text: l, selected: S.settings.restart_policy === v ? true : null })));
  const notify = h("input", { type: "checkbox", checked: S.settings.notify_default ? true : null });
  const perm = "Notification" in window ? Notification.permission : "unsupported";
  add(body, h("div", { class: "stack" },
    h("label", { class: "field" }, h("span", { text: "İzinli kök klasörler (her satıra bir tane)" }), roots,
      h("small", { text: "Panel yalnızca bu klasörlerin içindeki projeleri çalıştırır ve dosyalarını okur." })),
    h("div", { class: "grid2" },
      h("label", { class: "field" }, h("span", { text: "Port aralığı başlangıcı" }), pa),
      h("label", { class: "field" }, h("span", { text: "Port aralığı sonu" }), pb),
      h("label", { class: "field" }, h("span", { text: "Varsayılan otomatik yeniden başlatma" }), pol,
        h("small", { text: "Proje veya kategori ayarı bunu geçersiz kılar." })),
      h("div", { class: "field" }, h("span", { text: "Bildirimler" }),
        h("label", { class: "check" }, notify, "Çökme, dışarıdan kapatma ve yanıt vermeme bildirimleri"),
        perm === "default" ? h("button", { class: "btn sm", text: "Masaüstü bildirimlerine izin ver", onclick: requestDesktopNotifications }) :
          h("small", { text: perm === "granted" ? "Masaüstü bildirimleri açık" : perm === "denied" ? "Masaüstü bildirimleri tarayıcıda engellenmiş" : "" }))),
    h("div", { class: "row" }, h("div", { class: "grow" }), h("button", { class: "btn primary", text: "Kaydet", onclick: async () => {
      try {
        await api("PUT", "/api/settings", { roots: roots.value.split("\n").map(x => x.trim()).filter(Boolean),
          port_range: [Number(pa.value), Number(pb.value)], restart_policy: pol.value, notify_default: notify.checked });
        toast("Ayarlar kaydedildi", "ok"); loadState();
      } catch (e) { toast(e.message, "err"); }
    } }))));
}

function askPassword(title, text) {
  return new Promise(resolve => {
    const pw = h("input", { type: "password", autocomplete: "current-password", placeholder: "Panel parolası" });
    const done = v => { closeModal(); resolve(v); };
    pw.addEventListener("keydown", e => { if (e.key === "Enter") done(pw.value); });
    openModal(title, h("div", { class: "stack" }, h("p", { text }), pw),
      [h("button", { class: "btn", text: "Vazgeç", onclick: () => done(null) }), h("button", { class: "btn primary", text: "Onayla", onclick: () => done(pw.value) })]);
    setTimeout(() => pw.focus(), 50);
  });
}

async function settingsBackups(body) {
  const d = await api("GET", "/api/backups");
  const KIND = { auto: "Otomatik (günlük)", manual: "Elle", "pre-import": "İçe aktarma öncesi", "pre-restore": "Geri yükleme öncesi", "pre-migrate": "Güncelleme öncesi" };
  add(body, h("div", { class: "stack" },
    h("p", { class: "muted", text: "Yapılandırmanız (projeler, kategoriler, etiketler, notlar, profiller) her gün, her güncellemeden ve her içe aktarmadan önce otomatik yedeklenir. Ayrıca her değişiklikte okunabilir bir JSON kopyası tutulur." }),
    h("div", { class: "row" }, h("span", { class: "mono muted", text: d.dir }), h("div", { class: "grow" }),
      h("button", { class: "btn primary", text: "Şimdi yedekle", onclick: async () => { await api("POST", "/api/backups"); toast("Yedek alındı", "ok"); put(body); settingsBackups(body); } })),
    d.backups.length ? h("table", { class: "simple" },
      h("thead", {}, h("tr", {}, ["Tarih", "Tür", "Boyut", "Şema", ""].map(t => h("th", { text: t })))),
      h("tbody", {}, d.backups.map(b => h("tr", {},
        h("td", { class: "mono", text: fmtDate(b.ts) }), h("td", { text: KIND[b.kind] || b.kind }),
        h("td", { class: "mono", text: fmtSize(b.size) }), h("td", { class: "mono", text: b.schema ? `v${b.schema}` : "—" }),
        h("td", {}, h("button", { class: "btn sm", text: "Geri yükle…", onclick: async () => {
          const pw = await askPassword("Yedekten geri yükle", `${fmtDate(b.ts)} tarihli yedek geri yüklenecek. Mevcut durum önce ayrıca yedeklenir. Onaylamak için panel parolanızı girin.`);
          if (pw === null) return openSettings("backups");
          try { const r = await api("POST", "/api/backups/restore", { name: b.name, password: pw });
            toast(`Geri yüklendi. Önceki durumun yedeği: ${r.safety_backup}`, "ok"); await loadState(); openSettings("backups");
          } catch (e) { toast(e.message, "err"); openSettings("backups"); }
        } })))))) : h("p", { class: "muted", text: "Henüz yedek yok." })));
}

function settingsTransfer(body) {
  // ---- dışa aktarma
  const inc = h("input", { type: "checkbox" });
  const pass = h("input", { type: "password", placeholder: "Şifreleme parolası (en az 8 karakter)", autocomplete: "new-password", disabled: true });
  inc.addEventListener("change", () => { pass.disabled = !inc.checked; });
  const exportCard = h("div", { class: "card" }, h("h3", { text: "Dışa aktar" }),
    h("p", { class: "muted", text: "Tüm projeler, profiller, kategoriler, etiketler, notlar ve ayarlar tek bir JSON dosyasına yazılır. Proje yolları kök klasöre göre saklanır; böylece farklı bir klasör yapısına taşınabilir." }),
    h("label", { class: "check" }, inc, "Gizli ortam değişkenlerini de dahil et (parolayla şifrelenir)"), pass,
    h("div", { class: "row", style: { marginTop: "10px" } }, h("div", { class: "grow" }), h("button", { class: "btn primary", text: "⬇ Dışa aktar", onclick: async () => {
      try {
        const res = await fetch("/api/config/export", { method: "POST", headers: { "X-CSRF-Token": CSRF, "Content-Type": "application/json" },
          body: JSON.stringify({ include_secrets: inc.checked, passphrase: pass.value }) });
        const text = await res.text();
        if (!res.ok) throw new Error(JSON.parse(text).error);
        const name = (res.headers.get("content-disposition") || "").match(/filename="([^"]+)"/)?.[1] || "kovan-config.json";
        downloadText(name, text);
        toast(JSON.parse(text).secrets_omitted ? "Dışa aktarıldı (gizli değerler hariç)" : "Dışa aktarıldı", "ok");
      } catch (e) { toast(e.message, "err"); }
    } })));
  // ---- içe aktarma
  const file = h("input", { type: "file", accept: ".json,application/json" });
  const result = h("div");
  const state = { content: null, root_map: {}, passphrase: "" };
  const preview = async () => {
    try {
      const plan = await api("POST", "/api/config/import/preview", state);
      renderImportPlan(result, plan, state, preview);
    } catch (e) { put(result, h("div", { class: "alert", text: e.message })); }
  };
  file.addEventListener("change", async () => {
    const f = file.files[0]; if (!f) return;
    if (f.size > 5 * 1024 * 1024) return toast("Dosya çok büyük (en fazla 5 MB)", "err");
    state.content = await f.text(); state.root_map = {}; preview();
  });
  const importCard = h("div", { class: "card" }, h("h3", { text: "İçe aktar" }),
    h("p", { class: "muted", text: "Başka bir bilgisayardan veya eski bir sürümden alınan dosyayı yükleyin. Uygulamadan önce ne değişeceği ve çalıştırılacak komutlar gösterilir; hiçbir proje otomatik başlatılmaz." }),
    file, result);
  add(body, h("div", { class: "stack" }, exportCard, importCard));
}

function renderImportPlan(el, plan, state, rerun) {
  const ACT = { add: ["+ Eklenecek", "ok"], update: ["~ Güncellenecek", "warn"], unchanged: ["= Değişmeyecek", "muted"], skip: ["! Atlanacak", "err"] };
  const rootRows = plan.roots.map(r => {
    const inp = h("input", { type: "text", value: state.root_map[r.id] || "", placeholder: `aynı kalsın (${r.path})` });
    inp.addEventListener("change", () => { state.root_map[r.id] = inp.value.trim(); rerun(); });
    return h("tr", {}, h("td", { class: "mono", text: r.path }), h("td", { text: "→" }), h("td", {}, inp));
  });
  const passIn = h("input", { type: "password", placeholder: "Dosyanın şifreleme parolası", value: state.passphrase });
  passIn.addEventListener("change", () => { state.passphrase = passIn.value; rerun(); });
  const mode = h("select", {}, h("option", { value: "merge", text: "Birleştir — eşleşenleri güncelle, yenileri ekle (önerilir)" }),
    h("option", { value: "replace", text: "Değiştir — mevcut yapılandırmanın tamamını bu dosyayla değiştir" }));
  const keepWatch = h("input", { type: "checkbox", checked: state.keep_watch ? true : null });
  keepWatch.addEventListener("change", () => { state.keep_watch = keepWatch.checked; rerun(); });
  const s = plan.summary;
  put(el, h("div", { class: "stack", style: { marginTop: "12px" } },
    h("div", { class: "muted", text: `Kaynak: ${plan.source.host || "?"} (${plan.source.os || "?"}) · Kovan ${plan.app_version || "?"}` }),
    rootRows.length ? h("div", {}, h("b", { text: "Kök klasör eşleme" }),
      h("p", { class: "muted", text: "Projeler bu bilgisayarda farklı bir klasördeyse yeni kök yolunu yazın (ör. D:\\code → C:\\work\\code)." }),
      h("table", { class: "simple" }, h("tbody", {}, rootRows))) : null,
    plan.has_secrets ? h("label", { class: "field" }, h("span", { text: plan.secrets_ok ? "Gizli değerler çözüldü ✓" : "Dosya şifreli gizli değerler içeriyor" }), passIn) : null,
    plan.errors.map(e => h("div", { class: "alert", text: e })),
    h("div", { class: "row wrap" }, ["add", "update", "unchanged", "skip"].map(k => h("span", { class: `chip imp-${k}`, text: `${ACT[k][0]}: ${s[k]}` }))),
    h("table", { class: "simple" }, h("thead", {}, h("tr", {}, ["İşlem", "Proje", "Konum", "Komut"].map(t => h("th", { text: t })))),
      h("tbody", {}, plan.projects.map(it => h("tr", {},
        h("td", {}, h("span", { class: `chip imp-${it.action}`, text: ACT[it.action][0] })),
        h("td", {}, h("b", { text: it.name }), it.reason ? h("div", { class: "muted", text: it.reason }) : null),
        h("td", { class: "mono", text: it.path }),
        h("td", {}, it.commands.map(c => h("div", { class: "cmd-preview" },
          h("div", { class: "mono", text: `[${c.profile}] ${c.argv}` }),
          h("div", { class: "muted", text: `yorumlayıcı: ${c.interpreter} · dizin: ${c.cwd} · yeniden başlatma: ${c.restart}` }),
          c.env.length ? h("div", { class: "mono muted", text: `env: ${c.env.join("  ")}` }) : null,
          c.danger.length ? h("div", { class: "danger-note", text: `⚠ Dikkat: ${c.danger.join(", ")} — yalnızca güvendiğiniz dosyalarda onaylayın` }) : null))))))),
    h("label", { class: "field" }, h("span", { text: "İçe aktarma modu" }), mode),
    h("label", { class: "check" }, keepWatch, "Dosyadaki otomatik yeniden başlatma ayarlarını da al (varsayılan: yerel ayarlar korunur)"),
    h("div", { class: "row" }, h("div", { class: "grow" }), h("button", { class: "btn primary", text: "Uygula…", onclick: async () => {
      const pw = await askPassword("İçe aktarmayı onayla", `${s.add} eklenecek, ${s.update} güncellenecek${mode.value === "replace" ? " (mevcut yapılandırma DEĞİŞTİRİLECEK)" : ""}. Mevcut durum önce yedeklenir. Panel parolanızı girin.`);
      if (pw === null) return openSettings("transfer");
      try {
        const r = await api("POST", "/api/config/import/apply", { ...state, mode: mode.value, password: pw });
        toast(`İçe aktarıldı. Önceki durumun yedeği: ${r.backup}`, "ok"); await loadState();
      } catch (e) { toast(e.message, "err"); openSettings("transfer"); }
    } }))));
}

async function settingsDiag(body) {
  const d = await api("GET", "/api/diagnostics");
  add(body, h("dl", { class: "kv" },
    h("dt", { text: "Sürüm" }), h("dd", { text: d.version }),
    h("dt", { text: "Veri dizini" }), h("dd", { class: "mono", text: d.data_dir + (d.portable ? "  (taşınabilir mod)" : "") }),
    h("dt", { text: "Şema" }), h("dd", { text: `v${d.schema_version} (desteklenen v${d.schema_supported})` }),
    h("dt", { text: "Veritabanı" }), h("dd", { text: `${fmtSize(d.db_size)} · bütünlük ${d.integrity_ok ? "✓ tamam" : "✖ BOZUK — Yedekler sekmesinden geri yükleyin"}` }),
    h("dt", { text: "Yedekler" }), h("dd", { text: `${d.backups} adet` }),
    h("dt", { text: "JSON ayna" }), h("dd", { class: "mono", text: d.mirror }),
    h("dt", { text: "Son güncelleme" }), h("dd", { text: d.migration.from ? `v${d.migration.from} → v${d.migration.to}${d.migration.backup ? " (öncesinde yedek alındı)" : ""}` : "—" }),
    h("dt", { text: "Okunamayan kayıtlar" }), h("dd", {}, d.quarantine.length ? h("div", {},
      h("div", { class: "alert", text: `${d.quarantine.length} kayıt okunamadı ama SİLİNMEDİ. Yedekten geri yükleyebilir veya destek için ham veriyi kopyalayabilirsiniz.` }),
      d.quarantine.map(q => h("details", {}, h("summary", { text: `${q.source} · ${fmtDate(q.ts)} · ${q.error.slice(0, 80)}` }), h("pre", { class: "plain", text: q.raw })))) : "Yok ✓")));
}

async function settingsAudit(body) {
  const d = await api("GET", "/api/audit");
  add(body, h("table", { class: "simple" },
    h("thead", {}, h("tr", {}, h("th", { text: "Zaman" }), h("th", { text: "İşlem" }), h("th", { text: "Ayrıntı" }))),
    h("tbody", {}, d.entries.slice(0, 100).map(e => h("tr", {}, h("td", { class: "mono", text: fmtDate(e.ts) }),
      h("td", { text: e.action }), h("td", { class: "mono", text: e.detail }))))));
}

// ---------------------------------------------------------------- bildirimler
async function requestDesktopNotifications() {
  if (!("Notification" in window)) return toast("Tarayıcınız masaüstü bildirimlerini desteklemiyor", "err");
  const r = await Notification.requestPermission();
  toast(r === "granted" ? "Masaüstü bildirimleri açıldı" : "Masaüstü bildirimlerine izin verilmedi");
}

function renderBell() {
  const b = $("#bell"); if (!b) return;
  put(b, "🔔", S.unread ? h("span", { class: "bell-n", text: S.unread > 99 ? "99+" : S.unread }) : null);
  b.setAttribute("aria-label", `Bildirimler (${S.unread} okunmamış)`);
}

async function openNotifications() {
  const d = await api("GET", "/api/notifications");
  const LV = { error: "✖", warning: "⚠", info: "ℹ" };
  const list = d.notifications.length ? h("div", { class: "notif-list" }, d.notifications.map(n => h("div", {
      class: `notif lv-${n.level} ${n.read ? "" : "unread"}`, role: "button", tabindex: "0",
      onclick: () => { closeModal(); if (n.project_id && proj(n.project_id)) openDetail(n.project_id, n.level === "error" ? "errors" : "history"); } },
    h("span", { class: "lv", text: LV[n.level] || "•" }),
    h("div", { class: "grow" }, h("b", { text: n.title }), n.body ? h("div", { class: "muted", text: n.body }) : null),
    h("span", { class: "mono muted", text: fmtDate(n.ts) })))) : h("p", { class: "muted", text: "Bildirim yok." });
  const perm = "Notification" in window ? Notification.permission : "unsupported";
  openModal("Bildirimler", h("div", { class: "stack" },
    perm === "default" ? h("div", { class: "row notice-row" }, h("span", { class: "grow", text: "Sekme arka plandayken de haberdar olmak için masaüstü bildirimlerini açın." }),
      h("button", { class: "btn sm primary", text: "İzin ver", onclick: requestDesktopNotifications })) : null, list),
  [h("button", { class: "btn", text: "Kapat", onclick: closeModal })]);
  if (S.unread) { await api("POST", "/api/notifications/read"); S.unread = 0; renderBell(); }
}

function onNotification(n) {
  S.unread = (S.unread || 0) + 1; renderBell();
  toast(`${n.title}${n.body ? " — " + n.body : ""}`, n.level === "error" ? "err" : n.level === "info" ? "ok" : "");
  if ("Notification" in window && Notification.permission === "granted" && document.hidden && n.level !== "info") {
    try { const dn = new Notification(n.title, { body: n.body, tag: `kovan-${n.project_id}` });
      dn.onclick = () => { window.focus(); if (n.project_id) openDetail(n.project_id); }; } catch { /* yok say */ }
  }
}

// ---------------------------------------------------------------- kategori yöneticisi
function openCategoryManager() {
  const list = h("div");
  const rows = () => {
    put(list, );
    const ordered = [];
    for (const c of S.categories.filter(c => !c.parent_id)) { ordered.push(c); ordered.push(...S.categories.filter(x => x.parent_id === c.id)); }
    ordered.forEach((c, i) => {
      const name = h("input", { type: "text", value: c.name, "aria-label": "Kategori adı" });
      const color = h("input", { type: "color", value: c.color, "aria-label": "Renk" });
      const iconIn = h("input", { type: "text", value: c.icon, maxlength: 4, placeholder: "ikon", "aria-label": "İkon" });
      const parent = h("select", { "aria-label": "Üst kategori" }, h("option", { value: "", text: "— Üst kategori yok —" }),
        S.categories.filter(x => x.id !== c.id && !x.parent_id).map(x => h("option", { value: x.id, text: x.name, selected: x.id === c.parent_id ? true : null })));
      const defs = c.defaults || {};
      const pol = h("select", { "aria-label": "Varsayılan yeniden başlatma", title: "Bu kategorideki projeler için varsayılan otomatik yeniden başlatma" },
        h("option", { value: "", text: "Yeniden başlatma: genel" }),
        Object.entries(POLICY_LABEL).map(([v, l]) => h("option", { value: v, text: `Yeniden başlatma: ${l}`, selected: defs.restart_policy === v ? true : null })));
      const ntf = h("select", { "aria-label": "Varsayılan bildirim" },
        [["", "Bildirim: genel"], ["true", "Bildirim: açık"], ["false", "Bildirim: sessiz"]].map(([v, l]) =>
          h("option", { value: v, text: l, selected: String(defs.notify ?? "") === v ? true : null })));
      const save = async () => {
        try {
          await api("PUT", `/api/categories/${c.id}`, { ...c, name: name.value, color: color.value, icon: iconIn.value,
            parent_id: parent.value ? Number(parent.value) : null,
            defaults: { restart_policy: pol.value || null, notify: ntf.value === "" ? null : ntf.value === "true" } });
          await loadState(); rows();
        } catch (e) { toast(e.message, "err"); rows(); }
      };
      [name, color, iconIn, parent, pol, ntf].forEach(el => el.addEventListener("change", save));
      const n = countFor(p => p.category_id === c.id);
      add(list, h("div", { class: "cat-row" },
        h("span", { class: "row" },
          h("button", { class: "icon-btn", title: "Yukarı", "aria-label": "Yukarı", disabled: i === 0 ? true : null, onclick: () => move(c, -1) }, icon("up")),
          h("button", { class: "icon-btn", title: "Aşağı", "aria-label": "Aşağı", onclick: () => move(c, 1) }, icon("down"))),
        color, h("div", { class: "row" }, c.parent_id ? h("span", { class: "muted", text: "↳" }) : null, name), iconIn, parent, pol, ntf,
        h("button", { class: "icon-btn stop", title: "Sil", "aria-label": "Sil", onclick: async () => {
          if (!confirm(n ? `"${c.name}" silinecek; ${n} proje Kategorisiz'e taşınacak. Devam edilsin mi?` : `"${c.name}" silinsin mi?`)) return;
          await api("DELETE", `/api/categories/${c.id}`); await loadState(); rows(); } }, icon("trash"))));
    });
  };
  const move = async (c, dir) => {
    const ids = S.categories.map(x => x.id);
    const i = ids.indexOf(c.id), j = i + dir;
    if (j < 0 || j >= ids.length) return;
    [ids[i], ids[j]] = [ids[j], ids[i]];
    await api("PUT", "/api/categories-order", ids); await loadState(); rows();
  };
  const newName = h("input", { type: "text", placeholder: "Yeni kategori adı", "aria-label": "Yeni kategori adı" });
  const newColor = h("input", { type: "color", value: "#0ea5e9", "aria-label": "Yeni kategori rengi" });
  const addCategory = async () => {
    if (!newName.value.trim()) return;
    try { await api("POST", "/api/categories", { name: newName.value, color: newColor.value, sort: S.categories.length });
      newName.value = ""; await loadState(); rows(); } catch (e) { toast(e.message, "err"); }
  };
  newName.addEventListener("keydown", e => { if (e.key === "Enter") addCategory(); });
  rows();
  openModal("Kategoriler", h("div", {},
    h("p", { class: "muted", text: "Değişiklikler anında kaydedilir. Projeleri listeden kenar çubuğundaki kategoriye sürükleyerek de taşıyabilirsiniz." }),
    list, h("div", { class: "row", style: { marginTop: "14px" } }, newColor, newName, h("button", { class: "btn primary", text: "Ekle", onclick: addCategory }))),
  [h("button", { class: "btn", text: "Kapat", onclick: closeModal })], true);
}

// ---------------------------------------------------------------- keşif
async function openDiscover() {
  if (!(S.settings.roots || []).length) { toast("Önce bir kök klasör ekleyin", "err"); return openSettings(); }
  openModal("Projeleri keşfet", h("div", { class: "muted", text: "Klasörler taranıyor… (dosyalar yalnızca okunur)" }), [], true);
  let data;
  try { data = await api("GET", "/api/discover"); } catch (e) { closeModal(); return toast(e.message, "err"); }
  const cands = data.candidates.filter(c => !c.already_added);
  if (!cands.length) {
    return openModal("Projeleri keşfet", h("p", { class: "muted", text: data.candidates.length ? "Bulunan tüm projeler zaten ekli." : "Kök klasörlerde Python projesi bulunamadı." }),
      [h("button", { class: "btn", text: "Kapat", onclick: closeModal })]);
  }
  const catByName = Object.fromEntries(S.categories.map(c => [c.name, c.id]));
  const rows = cands.map(c => {
    const state = { on: true, cat: catByName[c.category] ?? "" };
    const sel = h("select", { "aria-label": "Kategori", onchange: e => state.cat = e.target.value },
      h("option", { value: "", text: "Kategorisiz" }),
      S.categories.map(x => h("option", { value: x.id, text: x.name, selected: x.id === state.cat ? true : null })));
    return { c, state, tr: h("tr", {},
      h("td", {}, h("input", { type: "checkbox", checked: true, "aria-label": `${c.name} içe aktar`, onchange: e => state.on = e.target.checked })),
      h("td", {}, h("b", { text: c.name }), h("div", { class: "muted mono", text: c.path }), c.description ? h("div", { class: "pdesc", text: c.description }) : null),
      h("td", {}, h("span", { class: "chip launcher", text: LAUNCHER_LABEL[c.launcher] }), h("div", { class: "mono muted", text: c.target })),
      h("td", { class: "mono", text: c.port ? `:${c.port}` : "—" }),
      h("td", {}, sel),
      h("td", { class: "muted", title: c.reasons.join(", "), text: `%${Math.round(c.confidence * 100)}` })) };
  });
  openModal(`Projeleri keşfet — ${cands.length} aday`, h("table", { class: "simple" },
    h("thead", {}, h("tr", {}, h("th", {}), h("th", { text: "Proje" }), h("th", { text: "Çalıştırma" }), h("th", { text: "Port" }),
      h("th", { text: "Kategori" }), h("th", { text: "Güven" }))),
    h("tbody", {}, rows.map(r => r.tr))),
  [h("button", { class: "btn", text: "İptal", onclick: closeModal }),
   h("button", { class: "btn primary", text: "Seçilenleri ekle", onclick: async () => {
     const chosen = rows.filter(r => r.state.on).map(r => ({ ...r.c, category_id: r.state.cat === "" ? null : Number(r.state.cat) }));
     const res = await api("POST", "/api/discover/import", { candidates: chosen });
     closeModal(); toast(`${res.added.length} proje eklendi`, "ok");
     res.errors.forEach(e => toast(`${e.path}: ${e.error}`, "err"));
     loadState();
   } })], true);
}

function openAddProject() {
  const path = h("input", { type: "text", placeholder: (S.settings.roots || [])[0] ? `${S.settings.roots[0]}${S.settings.roots[0].includes("\\") ? "\\" : "/"}proje` : "Proje klasörü yolu" });
  const name = h("input", { type: "text", placeholder: "Boş bırakılırsa klasör adı" });
  const launcher = h("select", {}, S.launchers.map(l => h("option", { value: l.type, text: l.label })));
  const target = h("input", { type: "text", placeholder: "ör. main.py veya main:app" });
  const port = h("input", { type: "number", min: 1, max: 65535, placeholder: "opsiyonel" });
  const catSel = h("select", {}, h("option", { value: "", text: "Kategorisiz" }), S.categories.map(c => h("option", { value: c.id, text: c.name })));
  launcher.addEventListener("change", () => { const l = S.launchers.find(x => x.type === launcher.value); target.placeholder = l?.hint || ""; });
  openModal("Proje ekle", h("div", { class: "stack" },
    h("label", { class: "field" }, h("span", { text: "Klasör (kök klasörlerin içinde olmalı)" }), path),
    h("div", { class: "grid2" }, h("label", { class: "field" }, h("span", { text: "Ad" }), name),
      h("label", { class: "field" }, h("span", { text: "Kategori" }), catSel)),
    h("div", { class: "grid3" }, h("label", { class: "field" }, h("span", { text: "Çalıştırma biçimi" }), launcher),
      h("label", { class: "field" }, h("span", { text: "Hedef" }), target),
      h("label", { class: "field" }, h("span", { text: "Port" }), port))),
  [h("button", { class: "btn", text: "İptal", onclick: closeModal }),
   h("button", { class: "btn primary", text: "Ekle", onclick: async () => {
     const p = path.value.trim();
     try {
       const created = await api("POST", "/api/projects", {
         name: name.value.trim() || p.split(/[\\/]/).filter(Boolean).pop() || "proje", path: p,
         category_id: catSel.value ? Number(catSel.value) : null,
         profiles: [{ name: "default", launcher: launcher.value, target: target.value.trim(), port: port.value ? Number(port.value) : null }] });
       closeModal(); await loadState(); openDetail(created.id, "config");
     } catch (e) { toast(e.message, "err"); }
   } })]);
}

// ---------------------------------------------------------------- detay paneli
function openDetail(id, tab = "overview") {
  if (S.detail && S.detail.id !== id) wsSend({ op: "unsub", ids: [S.detail.id] });
  S.detail = { id, tab }; S.draft = null;
  wsSend({ op: "sub", ids: [id] });
  $("#drawer").classList.add("open");
  renderDrawer();
  loadLogs(id);
  loadErrors(id);
  document.querySelectorAll("tr.open").forEach(x => x.classList.remove("open"));
  document.querySelector(`tr[data-id="${id}"]`)?.classList.add("open");
}

function closeDetail() {
  if (S.detail) wsSend({ op: "unsub", ids: [S.detail.id] });
  S.detail = null; $("#drawer").classList.remove("open");
  document.querySelectorAll("tr.open").forEach(x => x.classList.remove("open"));
}

const TABS = [["overview", "Genel"], ["logs", "Loglar"], ["errors", "Hatalar"], ["docs", "Dokümanlar"],
  ["config", "Yapılandırma"], ["history", "Geçmiş"], ["notes", "Notlar"]];

function renderDrawer() {
  const d = $("#drawer");
  put(d, h("div", { class: "drawer-head", id: "drawer-head" }), h("div", { class: "drawer-body", id: "drawer-body" }));
  renderDrawerHead(); renderTab();
}

function renderDrawerHead() {
  const p = proj(S.detail.id); if (!p) return closeDetail();
  const r = rt(p.id), profile = activeProfile(p);
  const head = $("#drawer-head"); if (!head) return;
  put(head, 
    h("div", { class: "drawer-title" }, h("h2", { text: p.name }), stateBadge(r.state, r), h("div", { class: "grow" }),
      ...actionButtons(p),
      h("button", { class: "icon-btn", title: "Kapat (Esc)", "aria-label": "Kapat", onclick: closeDetail }, icon("x"))),
    h("div", { class: "drawer-sub" }, catChip(p.category_id), h("span", { class: "chip launcher", text: LAUNCHER_LABEL[profile.launcher] }),
      portLinks(p), r.pid ? h("span", { class: "mono", text: `PID ${r.pid}` }) : null, h("span", { class: "mono", text: p.path })),
    h("div", { class: "tabs", role: "tablist" }, TABS.map(([k, label]) => h("button", {
      class: `tab ${S.detail.tab === k ? "active" : ""}`, role: "tab", "aria-selected": S.detail.tab === k ? "true" : "false",
      onclick: () => { S.detail.tab = k; renderDrawerHead(); renderTab(); } }, label,
      k === "errors" && S.errorCount[p.id] ? h("span", { class: "n", text: S.errorCount[p.id] }) : null))));
}

function renderTab() {
  const body = $("#drawer-body"); if (!body) return;
  body.className = "drawer-body";
  const p = proj(S.detail.id);
  const fn = { overview: tabOverview, logs: tabLogs, errors: tabErrors, docs: tabDocs, config: tabConfig, history: tabHistory, notes: tabNotes }[S.detail.tab];
  put(body, );
  fn(body, p);
}

function tabOverview(body, p) {
  const r = rt(p.id), profile = activeProfile(p);
  const preview = h("div", { class: "preview", text: "…" });
  api("POST", `/api/projects/${p.id}/preview`, profile).then(res => renderPreview(preview, res));
  const lastErr = (S.errors[p.id] || [])[0];
  add(body, h("div", { class: "stack" },
    p.description ? h("p", { class: "muted", text: p.description }) : null,
    h("div", { class: "grid2" },
      h("div", { class: "card" }, h("h3", { text: "Durum" }), h("dl", { class: "kv" },
        h("dt", { text: "Durum" }), h("dd", {}, stateBadge(r.state, r), r.reason_text ? h("div", { class: "muted", text: r.reason_text }) : null),
        h("dt", { text: "Çalışma süresi" }), h("dd", { class: "mono", text: isActive(r.state) && r.started_at ? fmtDuration(Date.now() / 1000 - r.started_at) : "—" }),
        h("dt", { text: "Son çıkış kodu" }), h("dd", { class: "mono", text: r.exit_code ?? "—" }),
        h("dt", { text: "Portlar" }), h("dd", {}, portLinks(p)),
        h("dt", { text: "CPU / RAM" }), h("dd", {}, metricsCell(p)))),
      h("div", { class: "card" }, h("h3", { text: "Yapılandırma" }), h("dl", { class: "kv" },
        h("dt", { text: "Profil" }), h("dd", { text: profile.name }),
        h("dt", { text: "Çalıştırma" }), h("dd", { text: S.launchers.find(l => l.type === profile.launcher)?.label }),
        h("dt", { text: "Hedef" }), h("dd", { class: "mono", text: profile.target || "—" }),
        h("dt", { text: "Kategori" }), h("dd", {}, catChip(p.category_id)),
        h("dt", { text: "Etiketler" }), h("dd", { text: p.tags.map(t => `#${t}`).join(" ") || "—" }),
        h("dt", { text: "Sağlık" }), h("dd", {}, healthStrip(r)),
        h("dt", { text: "Kaynak izleme" }), h("dd", {}, h("label", { class: "check" },
          h("span", { class: "switch" }, h("input", { type: "checkbox", checked: p.metrics_enabled ? true : null, onchange: async e => {
            await api("POST", "/api/projects/bulk-update", { ids: [p.id], metrics_enabled: e.target.checked }); loadState(); } }), h("span")),
          S.settings.metrics_enabled ? "(global olarak açık)" : "bu proje için"))))),
    h("div", { class: "card" }, h("h3", { text: "Çalıştırılacak komut" }), preview),
    lastErr ? h("div", { class: "card" }, h("h3", { text: "Son hata" }), errorCard(lastErr)) : null));
}

function healthStrip(r) {
  const hs = r.health;
  if (!hs || hs.ok === null || hs.ok === undefined) return h("span", { class: "muted", text: isActive(r.state) ? "bekleniyor…" : "—" });
  return h("span", { class: "health-strip", title: hs.last_error || "" },
    hs.history.map(([ts, ok]) => h("span", { class: `hdot ${ok ? "ok" : "bad"}`, title: `${fmtTime(ts)} ${ok ? "yanıt verdi" : "yanıt yok"}` })),
    h("span", { class: "muted", text: ` ${hs.ok ? "yanıt veriyor" : hs.last_error}` }));
}

function renderPreview(el, res) {
  el.replaceChildren();
  if (!res.ok) { el.append(h("span", { class: "err", text: `⚠ ${res.error}` })); return; }
  el.append(h("div", {}, res.argv.map(a => h("span", { class: "tok", text: /\s/.test(a) ? `"${a}"` : a }))),
    h("div", { class: "muted", style: { marginTop: "6px" }, text: `dizin: ${res.cwd} · yorumlayıcı: ${res.interpreter}` }));
}

// ---------- loglar
async function loadLogs(id) {
  const d = await api("GET", `/api/projects/${id}/logs`);
  S.logs[id] = d.lines; S.logSeq[id] = d.lines.length ? d.lines[d.lines.length - 1].seq : 0;
  if (S.detail?.id === id && S.detail.tab === "logs") renderTab();
}

const ANSI_RE = /\x1b\[([0-9;]*)m|\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07/g;
function ansiSpans(text, q) {
  const out = []; let cls = new Set(); let last = 0; let m;
  const push = s => { if (!s) return; out.push(...highlight(s, q, cls.size ? [...cls].join(" ") : "")); };
  ANSI_RE.lastIndex = 0;
  while ((m = ANSI_RE.exec(text))) {
    push(text.slice(last, m.index)); last = ANSI_RE.lastIndex;
    if (m[1] === undefined) continue;
    for (const code of (m[1] || "0").split(";").map(Number)) {
      if (code === 0) cls = new Set();
      else if (code === 1) cls.add("a-bold"); else if (code === 2) cls.add("a-dim");
      else if (code === 3) cls.add("a-italic"); else if (code === 4) cls.add("a-underline");
      else if (code === 22) { cls.delete("a-bold"); cls.delete("a-dim"); }
      else if ((code >= 30 && code <= 37) || (code >= 90 && code <= 97)) { [...cls].filter(c => c.startsWith("a-fg")).forEach(c => cls.delete(c)); cls.add(`a-fg${code}`); }
      else if (code === 39) [...cls].filter(c => c.startsWith("a-fg")).forEach(c => cls.delete(c));
    }
  }
  push(text.slice(last));
  return out;
}
function highlight(s, q, cls) {
  if (!q) return [cls ? h("span", { class: cls, text: s }) : document.createTextNode(s)];
  const parts = []; const low = s.toLowerCase(); let i = 0, j;
  while ((j = low.indexOf(q, i)) !== -1) {
    if (j > i) parts.push(h("span", { class: cls, text: s.slice(i, j) }));
    parts.push(h("mark", { text: s.slice(j, j + q.length) })); i = j + q.length;
  }
  if (i < s.length) parts.push(h("span", { class: cls, text: s.slice(i) }));
  return parts;
}
const stripAnsi = s => s.replace(ANSI_RE, "");

function logLineEl(e) {
  const q = S.logUI.q.toLowerCase();
  return h("div", { class: `log-line ${e.stream}` }, h("span", { class: "ts", text: fmtTime(e.ts) }),
    h("span", {}, ansiSpans(e.text, q)));
}
function logVisible(e) {
  const u = S.logUI;
  if (u.stream !== "all" && e.stream !== u.stream && !(u.stream === "err" && e.stream === "sys")) return false;
  return !u.q || stripAnsi(e.text).toLowerCase().includes(u.q.toLowerCase());
}

function tabLogs(body, p) {
  body.className = "drawer-body flush";
  const view = h("div", { class: `log-view ${S.logUI.ts ? "" : "no-ts"}`, id: "log-view", role: "log", "aria-live": "off" });
  const status = h("div", { class: "log-status", id: "log-status" });
  const fill = () => {
    const lines = (S.logs[p.id] || []).filter(logVisible).slice(-5000);
    put(view, ...lines.map(logLineEl));
    view.scrollTop = view.scrollHeight;
    status.textContent = `${lines.length} satır gösteriliyor · tamponda ${(S.logs[p.id] || []).length}`;
  };
  const search = h("input", { type: "search", placeholder: "Loglarda ara / filtrele", value: S.logUI.q, "aria-label": "Loglarda ara" });
  let t; search.addEventListener("input", () => { clearTimeout(t); t = setTimeout(() => { S.logUI.q = search.value; fill(); }, 150); });
  const stream = h("select", { "aria-label": "Akış", onchange: e => { S.logUI.stream = e.target.value; fill(); } },
    [["all", "Tüm akışlar"], ["out", "stdout"], ["err", "stderr"]].map(([v, l]) => h("option", { value: v, text: l, selected: S.logUI.stream === v ? true : null })));
  const pause = h("button", { class: "btn sm", text: S.logUI.paused ? "▶ Devam" : "⏸ Duraklat", onclick: () => {
    S.logUI.paused = !S.logUI.paused; pause.textContent = S.logUI.paused ? "▶ Devam" : "⏸ Duraklat"; if (!S.logUI.paused) fill(); } });
  const runs = h("a", { class: "btn sm", text: "⬇ İndir", href: "#", onclick: async e => {
    e.preventDefault(); const d = await api("GET", `/api/projects/${p.id}/runs`);
    if (!d.runs.length) return toast("İndirilecek log yok");
    location.href = `/api/projects/${p.id}/runs/${d.runs[0].id}/log`; } });
  add(body, h("div", { class: "log-toolbar" }, search, stream,
    h("label", { class: "check muted" }, h("input", { type: "checkbox", checked: S.logUI.ts ? true : null,
      onchange: e => { S.logUI.ts = e.target.checked; view.classList.toggle("no-ts", !e.target.checked); } }), "Zaman"),
    h("div", { class: "grow" }), pause,
    h("button", { class: "btn sm", text: "Temizle", title: "Yalnızca görünümü temizler", onclick: () => { S.logs[p.id] = []; fill(); } }), runs),
    view, status);
  fill();
}

function appendLog(id, entry) {
  const arr = S.logs[id] || (S.logs[id] = []);
  arr.push(entry); if (arr.length > 5000) arr.splice(0, arr.length - 5000);
  if (S.detail?.id !== id || S.detail.tab !== "logs" || S.logUI.paused || !logVisible(entry)) return;
  const view = $("#log-view"); if (!view) return;
  const atBottom = view.scrollHeight - view.scrollTop - view.clientHeight < 40;
  add(view, logLineEl(entry));
  while (view.childElementCount > 5000) view.firstElementChild.remove();
  if (atBottom) view.scrollTop = view.scrollHeight;
  const st = $("#log-status"); if (st) st.textContent = `${view.childElementCount} satır gösteriliyor · tamponda ${arr.length}`;
}

// ---------- hatalar
async function loadErrors(id) {
  const d = await api("GET", `/api/projects/${id}/errors`);
  S.errors[id] = d.errors; S.errorCount[id] = d.errors.length;
  if (S.detail?.id === id) { renderDrawerHead(); if (S.detail.tab === "errors") renderTab(); }
}

function errorCard(e) {
  const last = e.frames[e.frames.length - 1];
  return h("div", { class: "err-card" },
    h("div", { class: "title", text: e.exc_type }),
    e.message ? h("div", { class: "msg", text: e.message }) : null,
    h("div", { class: "meta" }, h("span", { text: `${e.count} kez` }), h("span", { text: `son: ${fmtDate(e.last_seen)}` }),
      last ? h("span", { class: "mono", text: `${last[0]}:${last[1]}` }) : null,
      h("button", { class: "btn sm", text: "Kopyala", onclick: () => navigator.clipboard?.writeText(e.text).then(() => toast("Traceback kopyalandı", "ok")) })),
    h("pre", { text: e.text }));
}

function tabErrors(body, p) {
  const errs = S.errors[p.id] || [];
  if (!errs.length) { add(body, h("div", { class: "empty muted", text: "Yakalanmış hata yok. Python traceback'leri burada gruplanır." })); return; }
  add(body, h("div", { class: "row", style: { marginBottom: "12px" } }, h("span", { class: "muted", text: `${errs.length} farklı hata` }), h("div", { class: "grow" }),
    h("button", { class: "btn sm", text: "Temizle", onclick: async () => { await api("DELETE", `/api/projects/${p.id}/errors`); loadErrors(p.id); } })),
  errs.map(errorCard));
}

// ---------- dokümanlar
async function tabDocs(body, p) {
  body.className = "drawer-body flush";
  const listEl = h("div", { class: "doc-list" }), docEl = h("div", { class: "doc-body" });
  add(body, h("div", { class: "docs" }, listEl, docEl));
  let docs;
  try { docs = (await api("GET", `/api/projects/${p.id}/docs`)).docs; } catch (e) { add(docEl, h("div", { class: "muted", text: e.message })); return; }
  if (!docs.length) { add(docEl, h("div", { class: "empty muted", text: "Bu projede README veya doküman dosyası bulunamadı." })); return; }
  const open = async (path, push = true) => {
    listEl.querySelectorAll(".nav-item").forEach(x => x.classList.toggle("active", x.dataset.path === path));
    try {
      const d = await api("GET", `/api/projects/${p.id}/docs/view?path=${encodeURIComponent(path)}`);
      // Sunucuda nh3 ile temizlendi. <template> içinde ayrıştırılır: görseller işlenmeden önce hiçbir istek yapılmaz.
      const tpl = document.createElement("template"); tpl.innerHTML = d.html;
      const md = h("div", { class: "md" });
      let blocked = 0;
      const base = path.includes("/") ? path.slice(0, path.lastIndexOf("/") + 1) : "";
      tpl.content.querySelectorAll("img").forEach(img => {
        const src = img.getAttribute("src") || "";
        if (/^https?:/i.test(src)) { blocked++; img.replaceWith(h("span", { class: "blocked-img", text: `🖼 uzak görsel engellendi: ${img.alt || src}` })); }
        else img.src = `/api/projects/${p.id}/docs/image?path=${encodeURIComponent(normPath(base + src))}`;
      });
      tpl.content.querySelectorAll("a[href]").forEach(a => {
        const href = a.getAttribute("href");
        if (/^(https?:|mailto:)/i.test(href)) { a.target = "_blank"; a.rel = "noopener noreferrer nofollow"; return; }
        if (href.startsWith("#")) return;
        a.addEventListener("click", ev => { ev.preventDefault(); open(normPath(base + href.split("#")[0]), true); });
      });
      md.append(tpl.content);
      put(docEl, 
        blocked ? h("div", { class: "notice", text: `Gizlilik için ${blocked} uzak görsel yüklenmedi.` }) : null,
        d.env_keys.length ? h("div", { class: "notice", text: `Bu şablondaki değişkenler: ${d.env_keys.join(", ")} — Yapılandırma > Ortam değişkenleri bölümünden ekleyebilirsiniz.` }) : null,
        md);
      docEl.scrollTop = 0;
    } catch (e) { put(docEl, h("div", { class: "muted", text: e.message })); }
  };
  add(listEl, ...docs.map(d => h("div", { class: "nav-item", "data-path": d.path, role: "button", tabindex: "0", onclick: () => open(d.path) },
    h("span", { text: d.path.includes("/") ? `↳ ${d.path}` : d.path }))));
  open(docs[0].path);
}
function normPath(p) {
  const out = [];
  for (const part of p.split("/")) { if (part === "..") out.pop(); else if (part && part !== ".") out.push(part); }
  return out.join("/");
}

// ---------- yapılandırma
function tabConfig(body, p) {
  if (!S.draft || S.draft.id !== p.id) S.draft = JSON.parse(JSON.stringify(p));
  const d = S.draft;
  let prof = d.profiles.find(x => x.name === (S.draft._editing || d.active_profile)) || d.profiles[0];
  const preview = h("div", { class: "preview", text: "…" });
  let pt; const refresh = () => { clearTimeout(pt); pt = setTimeout(() => api("POST", `/api/projects/${p.id}/preview`, prof).then(r => renderPreview(preview, r)).catch(e => renderPreview(preview, { ok: false, error: e.message })), 250); };
  const bind = (el, obj, key, conv = v => v) => { el.addEventListener("input", () => { obj[key] = conv(el.type === "checkbox" ? el.checked : el.value); refresh(); }); el.addEventListener("change", () => { obj[key] = conv(el.type === "checkbox" ? el.checked : el.value); refresh(); }); return el; };
  const inp = (obj, key, attrs = {}, conv) => bind(h("input", { type: "text", value: obj[key] ?? "", ...attrs }), obj, key, conv);

  const catSel = h("select", {}, h("option", { value: "", text: "Kategorisiz" }),
    S.categories.map(c => h("option", { value: c.id, text: c.parent_id ? `↳ ${c.name}` : c.name, selected: c.id === d.category_id ? true : null })));
  bind(catSel, d, "category_id", v => v === "" ? null : Number(v));
  const tags = h("input", { type: "text", value: d.tags.join(", "), placeholder: "backend, shop" });
  tags.addEventListener("input", () => d.tags = tags.value.split(",").map(s => s.trim()).filter(Boolean));

  const launcherSel = h("select", {}, S.launchers.map(l => h("option", { value: l.type, text: l.label, selected: l.type === prof.launcher ? true : null })));
  const targetIn = inp(prof, "target", { placeholder: S.launchers.find(l => l.type === prof.launcher)?.hint || "" });
  launcherSel.addEventListener("change", () => { prof.launcher = launcherSel.value; targetIn.placeholder = S.launchers.find(l => l.type === prof.launcher)?.hint || ""; renderReloadVis(); refresh(); });
  const args = h("textarea", { rows: 3, placeholder: "Her satıra bir argüman. {port} ve {host} yer tutucuları desteklenir.\nÖzel komutta {python} seçili yorumlayıcıya genişler." });
  args.value = prof.args.join("\n");
  args.addEventListener("input", () => { prof.args = args.value.split("\n").filter(s => s.length); refresh(); });
  const portIn = inp(prof, "port", { type: "number", min: 1, max: 65535, placeholder: "yok" }, v => v === "" ? null : Number(v));
  const findPort = h("button", { class: "btn", text: "Boş port bul", onclick: async () => {
    const [a, b] = S.settings.port_range || [8000, 8999];
    for (let port = prof.port || a; port <= b; port++) {
      const r = await api("GET", `/api/ports/check?port=${port}`);
      const takenByOther = S.projects.some(x => x.id !== p.id && x.profiles.some(pr => pr.port === port));
      if (r.free && !takenByOther) { prof.port = port; portIn.value = port; refresh(); return toast(`:${port} boş`, "ok"); }
    }
    toast("Aralıkta boş port bulunamadı", "err"); } });
  const reloadWrap = h("label", { class: "check" }, bind(h("input", { type: "checkbox", checked: prof.reload ? true : null }), prof, "reload"), "Otomatik yeniden yükleme (--reload / --debug)");
  const renderReloadVis = () => reloadWrap.classList.toggle("hidden", !["uvicorn", "flask", "django"].includes(prof.launcher));
  renderReloadVis();
  const interpSel = h("select", {}, [["auto", "Otomatik (venv → uv → sistem)"], ["venv", "Proje sanal ortamı (.venv/venv/env)"], ["uv", "uv run"], ["system", "Sistem Python (PATH)"], ["custom", "Özel yol"]]
    .map(([v, l]) => h("option", { value: v, text: l, selected: prof.interpreter.kind === v ? true : null })));
  const interpPath = inp(prof.interpreter, "path", { placeholder: "C:\\Python312\\python.exe" });
  const interpPathWrap = h("label", { class: "field" }, h("span", { text: "Yorumlayıcı yolu" }), interpPath);
  const interpVis = () => interpPathWrap.classList.toggle("hidden", prof.interpreter.kind !== "custom");
  interpSel.addEventListener("change", () => { prof.interpreter.kind = interpSel.value; interpVis(); refresh(); }); interpVis();

  const envBox = h("div");
  const renderEnv = () => {
    put(envBox, ...prof.env.map((e, i) => {
      const val = h("input", { type: e.secret ? "password" : "text", value: e.value, placeholder: "değer", autocomplete: "off" });
      val.addEventListener("input", () => { e.value = val.value; });
      return h("div", { class: "env-row" },
        h("input", { type: "text", value: e.key, placeholder: "ANAHTAR", class: "mono", oninput: ev => { e.key = ev.target.value.trim(); } }), val,
        h("label", { class: "check muted", title: "Gizli değerler arayüzde maskelenir ve loglarda *** ile değiştirilir" },
          h("input", { type: "checkbox", checked: e.secret ? true : null, onchange: ev => { e.secret = ev.target.checked; renderEnv(); } }), "gizli"),
        h("button", { class: "icon-btn stop", "aria-label": "Sil", onclick: () => { prof.env.splice(i, 1); renderEnv(); } }, icon("trash")));
    }), h("button", { class: "btn sm", text: "+ Değişken ekle", onclick: () => { prof.env.push({ key: "", value: "", secret: false }); renderEnv(); } }));
  };
  renderEnv();

  const profileTabs = h("div", { class: "profile-tabs" },
    d.profiles.map(x => h("button", { class: `btn sm ${x === prof ? "primary" : ""}`, text: x.name + (x.name === d.active_profile ? " ★" : ""),
      onclick: () => { S.draft._editing = x.name; renderTab(); } })),
    h("button", { class: "btn sm", text: "+ Profil", onclick: () => {
      const name = prompt("Yeni profil adı (ör. prod):"); if (!name) return;
      if (d.profiles.some(x => x.name === name)) return toast("Bu adda profil var", "err");
      d.profiles.push({ ...JSON.parse(JSON.stringify(prof)), name }); S.draft._editing = name; renderTab(); } }),
    d.profiles.length > 1 ? h("button", { class: "btn sm danger", text: "Profili sil", onclick: () => {
      if (!confirm(`"${prof.name}" profili silinsin mi?`)) return;
      d.profiles = d.profiles.filter(x => x !== prof); if (d.active_profile === prof.name) d.active_profile = d.profiles[0].name;
      S.draft._editing = null; renderTab(); } }) : null,
    prof.name !== d.active_profile ? h("button", { class: "btn sm", text: "Varsayılan yap", onclick: () => { d.active_profile = prof.name; renderTab(); } }) : null);

  add(body, 
    h("div", { class: "cfg-section" }, h("h3", { text: "Proje" }),
      h("div", { class: "grid2" },
        h("label", { class: "field" }, h("span", { text: "Ad" }), inp(d, "name")),
        h("label", { class: "field" }, h("span", { text: "Kategori" }), catSel),
        h("label", { class: "field" }, h("span", { text: "Etiketler (virgülle)" }), tags),
        h("label", { class: "field" }, h("span", { text: "Klasör" }), inp(d, "path"))),
      h("label", { class: "field", style: { marginTop: "12px" } }, h("span", { text: "Açıklama" }), inp(d, "description", { placeholder: "README'den otomatik alınır" }))),
    h("div", { class: "cfg-section" }, h("h3", { text: "Çalıştırma profili" }), profileTabs,
      h("div", { class: "grid2" },
        h("label", { class: "field" }, h("span", { text: "Çalıştırma biçimi" }), launcherSel),
        h("label", { class: "field" }, h("span", { text: "Hedef" }), targetIn),
        h("label", { class: "field" }, h("span", { text: "Port (çakışma önlenir)" }), h("div", { class: "row" }, portIn, findPort)),
        h("label", { class: "field" }, h("span", { text: "Host" }), inp(prof, "host")),
        h("label", { class: "field" }, h("span", { text: "Çalışma dizini (proje köküne göre)" }), inp(prof, "cwd", { placeholder: "boş = proje kökü" })),
        h("label", { class: "field" }, h("span", { text: "Durdurma zaman aşımı (sn)" }), inp(prof, "stop_timeout", { type: "number", min: 1, max: 120 }, Number))),
      h("div", { style: { marginTop: "10px" } }, reloadWrap),
      h("label", { class: "field", style: { marginTop: "12px" } }, h("span", { text: "Ek argümanlar" }), args)),
    h("div", { class: "cfg-section" }, h("h3", { text: "Yorumlayıcı" }), h("div", { class: "grid2" },
      h("label", { class: "field" }, h("span", { text: "Python" }), interpSel), interpPathWrap)),
    watchSection(d, prof, refresh),
    h("div", { class: "cfg-section" }, h("h3", { text: "Ortam değişkenleri" }), envBox,
      h("small", { class: "muted", text: "PYTHONUNBUFFERED, PYTHONIOENCODING=utf-8 ve PORT otomatik eklenir; böylece CLI'da görünen tüm çıktı burada da görünür." })),
    h("div", { class: "cfg-section" }, h("h3", { text: "Komut önizlemesi" }), preview),
    h("div", { class: "savebar" },
      h("button", { class: "btn danger", text: "Projeyi kaldır", onclick: async () => {
        if (!confirm(`"${p.name}" panelden kaldırılsın mı? (Dosyalar silinmez)`)) return;
        await api("DELETE", `/api/projects/${p.id}`); closeDetail(); loadState(); } }),
      h("div", { class: "grow" }),
      h("button", { class: "btn", text: "Vazgeç", onclick: () => { S.draft = null; renderTab(); } }),
      h("button", { class: "btn primary", text: "Kaydet", onclick: async () => {
        const payload = JSON.parse(JSON.stringify(d)); delete payload._editing;
        payload.profiles.forEach(x => { x.env = x.env.filter(e => e.key); });
        try { await api("PUT", `/api/projects/${p.id}`, payload); S.draft = null; toast("Kaydedildi", "ok"); await loadState(); renderTab(); }
        catch (e) { toast(e.message, "err"); } } })));
  refresh();
}

function watchSection(d, prof, refresh) {
  prof.health ||= { type: "auto", path: "/", interval: 5, timeout: 3, start_grace: 20, failures: 3 };
  prof.restart ||= { policy: null, max_retries: 5, window_sec: 300, on_unhealthy: false };
  const sel = (obj, key, opts, conv = v => v) => {
    const el = h("select", {}, opts.map(([v, l]) => h("option", { value: v ?? "", text: l, selected: (obj[key] ?? "") === (v ?? "") ? true : null })));
    el.addEventListener("change", () => { obj[key] = conv(el.value); refresh(); vis(); });
    return el;
  };
  const num = (obj, key, attrs) => { const el = h("input", { type: "number", value: obj[key], ...attrs });
    el.addEventListener("input", () => { obj[key] = Number(el.value); }); return el; };
  const c = cat(d.category_id);
  const inherited = prof.restart.policy ? null : (c?.defaults?.restart_policy ? `kategoriden: ${POLICY_LABEL[c.defaults.restart_policy]}` : `genel ayardan: ${POLICY_LABEL[S.settings.restart_policy] || "Asla"}`);
  const pathIn = h("input", { type: "text", value: prof.health.path, placeholder: "/" });
  pathIn.addEventListener("input", () => { prof.health.path = pathIn.value || "/"; });
  const pathWrap = h("label", { class: "field" }, h("span", { text: "HTTP yolu" }), pathIn);
  const vis = () => pathWrap.classList.toggle("hidden", prof.health.type !== "http");
  const onUnh = h("input", { type: "checkbox", checked: prof.restart.on_unhealthy ? true : null });
  onUnh.addEventListener("change", () => { prof.restart.on_unhealthy = onUnh.checked; });
  const expect = h("input", { type: "checkbox", checked: prof.expect_exit ? true : null });
  expect.addEventListener("change", () => { prof.expect_exit = expect.checked; });
  const notifySel = sel(d, "notify", [[null, "Varsayılan (kategori / genel)"], ["true", "Açık"], ["false", "Sessiz"]],
    v => v === "" ? null : v === "true");
  if (d.notify !== null && d.notify !== undefined) notifySel.value = String(d.notify);
  const el = h("div", { class: "cfg-section" }, h("h3", { text: "Gözetim" }),
    h("p", { class: "muted", text: "Uygulama çökerse, dışarıdan kapatılırsa veya yanıt vermezse panel bunu algılar, bildirir ve isterseniz otomatik yeniden başlatır." }),
    h("div", { class: "grid3" },
      h("label", { class: "field" }, h("span", { text: "Sağlık kontrolü" }), sel(prof.health, "type", [["auto", "Otomatik (web: HTTP, diğer: TCP)"], ["http", "HTTP isteği"], ["tcp", "TCP bağlantısı"], ["none", "Kapalı"]])),
      pathWrap,
      h("label", { class: "field" }, h("span", { text: "Aralık (sn)" }), num(prof.health, "interval", { min: 1, max: 600 })),
      h("label", { class: "field" }, h("span", { text: "Ardışık hata eşiği" }), num(prof.health, "failures", { min: 1, max: 20 })),
      h("label", { class: "field" }, h("span", { text: "Başlangıç toleransı (sn)" }), num(prof.health, "start_grace", { min: 1, max: 600 }))),
    h("div", { class: "grid3", style: { marginTop: "12px" } },
      h("label", { class: "field" }, h("span", { text: "Otomatik yeniden başlatma" }),
        sel(prof.restart, "policy", [[null, "Varsayılanı kullan"], ["never", "Asla"], ["on-failure", "Çökerse / öldürülürse"], ["always", "Her sonlanmada"]], v => v || null),
        inherited ? h("small", { text: inherited }) : null),
      h("label", { class: "field" }, h("span", { text: "Deneme sınırı" }), num(prof.restart, "max_retries", { min: 1, max: 100 })),
      h("label", { class: "field" }, h("span", { text: "Zaman penceresi (sn)" }), num(prof.restart, "window_sec", { min: 10, max: 86400 }))),
    h("div", { class: "row wrap", style: { marginTop: "10px", gap: "18px" } },
      h("label", { class: "check" }, onUnh, "Yanıt vermediğinde de yeniden başlat"),
      h("label", { class: "check" }, expect, "Kısa ömürlü betik (kod 0 ile bitmesi normal)")),
    h("label", { class: "field", style: { marginTop: "12px", maxWidth: "320px" } }, h("span", { text: "Bildirimler (bu proje)" }), notifySel));
  vis();
  return el;
}

// ---------- geçmiş
const EVENT_LABEL = { start: "Başlatıldı", user_stop: "Kullanıcı durdurdu", external_stop: "Dışarıdan kapatıldı",
  killed: "Öldürüldü", crashed: "Çöktü", exited_ok: "Tamamlandı", orphaned: "Yetim alt süreç kaldı",
  orphans_killed: "Yetimler sonlandırıldı", orphans_gone: "Yetimler kendiliğinden bitti", unhealthy: "Yanıt vermiyor",
  recovered: "Düzeldi", worker_restarted: "İşçi süreç yeniden başladı", auto_restart: "Otomatik yeniden başlatıldı",
  crash_loop: "Çökme döngüsü", adopted: "Devralındı", external_killed: "Harici süreç durduruldu",
  panel_shutdown: "Panel kapandı", spawn_failed: "Başlatılamadı" };
const EVENT_LEVEL = { killed: "err", crashed: "err", crash_loop: "err", spawn_failed: "err", external_stop: "warn",
  unhealthy: "warn", orphaned: "warn", recovered: "ok", start: "ok", auto_restart: "ok", worker_restarted: "warn" };

async function tabHistory(body, p) {
  const [d, ev] = await Promise.all([api("GET", `/api/projects/${p.id}/runs`), api("GET", `/api/projects/${p.id}/events`)]);
  if (!d.runs.length && !ev.events.length) { add(body, h("div", { class: "empty muted", text: "Henüz çalıştırma yok." })); return; }
  add(body, h("div", { class: "history" },
    h("div", {}, h("h3", { class: "sec", text: "Çalıştırmalar" }), h("table", { class: "simple" },
      h("thead", {}, h("tr", {}, ["Başlangıç", "Süre", "Profil", "Tetikleyen", "Sonuç", ""].map(t => h("th", { text: t })))),
      h("tbody", {}, d.runs.map(r => h("tr", {},
        h("td", { class: "mono", text: fmtDate(r.started) }),
        h("td", { class: "mono", text: r.ended ? fmtDuration(r.ended - r.started) : "çalışıyor" }),
        h("td", { text: r.profile }), h("td", { text: r.trigger }),
        h("td", {}, r.ended ? h("span", { class: `badge ${["killed", "crashed", "spawn_failed"].includes(r.end_reason) ? "st-crashed" : r.end_reason === "external_stop" ? "st-external" : "st-stopped"}`,
          text: `${END_REASON[r.end_reason] || "Sonlandı"}${r.exit_code !== null && r.exit_code !== undefined ? " · " + r.exit_code : ""}${r.signal ? " · " + r.signal : ""}` }) : stateBadge("running")),
        h("td", {}, r.log_file ? h("a", { href: `/api/projects/${p.id}/runs/${r.id}/log`, text: "log" }) : null)))))),
    h("div", {}, h("h3", { class: "sec", text: "Olaylar" }), h("ol", { class: "timeline" }, ev.events.map(e => h("li", { class: `ev-${EVENT_LEVEL[e.kind] || "muted"}` },
      h("span", { class: "mono muted", text: fmtDate(e.ts) }), h("b", { text: EVENT_LABEL[e.kind] || e.kind }),
      e.detail ? h("span", { class: "muted", text: e.detail }) : null))))));
}

// ---------- notlar
function tabNotes(body, p) {
  const ta = h("textarea", { rows: 14, placeholder: "Bu projeyle ilgili notlar (ör. \"8001 portu frontend'e sabit, değiştirme\")" });
  ta.value = p.notes || "";
  add(body, h("div", { class: "stack" }, ta, h("div", { class: "row" }, h("div", { class: "grow" }),
    h("button", { class: "btn primary", text: "Kaydet", onclick: async () => {
      await api("PUT", `/api/projects/${p.id}`, { ...p, notes: ta.value }); toast("Not kaydedildi", "ok"); loadState(); } }))));
}

// ---------------------------------------------------------------- WebSocket
function wsSend(msg) { if (S.ws && S.ws.readyState === 1) S.ws.send(JSON.stringify(msg)); }

function connectWS() {
  const ws = new WebSocket(`ws://${location.host}/ws`);
  S.ws = ws;
  ws.onopen = () => { $("#ws-banner").classList.add("hidden"); if (S.detail) wsSend({ op: "sub", ids: [S.detail.id] }); loadState(); };
  ws.onclose = () => { $("#ws-banner").classList.remove("hidden"); setTimeout(connectWS, 2000); };
  ws.onmessage = ev => {
    const msg = JSON.parse(ev.data);
    let dirty = false;
    for (const e of msg.events) {
      if (e.type === "log") appendLog(e.project_id, e.entry);
      else if (e.type === "state") {
        S.runtimes[e.project_id] = { ...e }; dirty = true;  // kullanıcı uyarıları "notification" olaylarıyla gelir
      } else if (e.type === "notification") {
        onNotification(e.notification);
      } else if (e.type === "error") {
        const list = S.errors[e.project_id] || (S.errors[e.project_id] = []);
        const i = list.findIndex(x => x.signature === e.error.signature);
        if (i >= 0) list.splice(i, 1);
        list.unshift(e.error); S.errorCount[e.project_id] = list.length; dirty = true;
      } else if (e.type === "stats") {
        for (const [id, s] of Object.entries(e.data)) S.runtimes[id] = { ...rt(Number(id)), ...s };
        dirty = true;
      }
    }
    if (msg.dropped) toast(`Yoğun log akışı: ${msg.dropped} satır atlandı (dosyada mevcut)`);
    if (dirty) { renderNav(); renderSummary(); renderList(); if (S.detail) { renderDrawerHead(); if (S.detail.tab === "overview" || S.detail.tab === "errors") renderTab(); } }
  };
}

// ---------------------------------------------------------------- başlangıç
document.addEventListener("DOMContentLoaded", () => {
  $("#search").addEventListener("input", e => { S.search = e.target.value; renderList(); });
  $("#btn-add").addEventListener("click", openAddProject);
  $("#btn-discover").addEventListener("click", openDiscover);
  $("#btn-settings").addEventListener("click", openSettings);
  $("#metrics-toggle").addEventListener("change", async e => {
    await api("PUT", "/api/settings", { metrics_enabled: e.target.checked });
    S.settings.metrics_enabled = e.target.checked; renderList();
    toast(e.target.checked ? "CPU/RAM izleme açıldı (5 sn aralık)" : "CPU/RAM izleme kapatıldı");
  });
  document.addEventListener("keydown", e => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); $("#search").focus(); $("#search").select(); }
    else if (e.key === "Escape" && !$("#modal").open && S.detail) closeDetail();
  });
  $("#bell").addEventListener("click", openNotifications);
  setInterval(() => document.querySelectorAll(".badge[data-backoff]").forEach(el => {
    const left = Math.max(0, Math.ceil(Number(el.dataset.backoff) - Date.now() / 1000));
    el.lastChild.textContent = `${STATE_LABEL.backoff} · ${left} sn`; }), 1000);
  setInterval(() => document.querySelectorAll(".uptime[data-started]").forEach(el => {
    const s = Number(el.dataset.started); if (s) el.textContent = fmtDuration(Date.now() / 1000 - s); }), 1000);
  loadState().then(connectWS);
});
