/* Hoard Files: a two-pane file manager, the fifth view.
   ui.html loads this script the first time Files opens. It relies on ui.html's globals:
   api, $, esc, fmt, when, toast, copyText, stage, setMode, closeMenu, menu, S, C, TOKEN, CATN, ICON, SPIN,
   ghost, fillGhost, startDragUI, endDragUI, stageBtn. Server contract: review/fm-api.md. */
(() => {
"use strict";

/* ------------------------------------------------------------------ small helpers */
const ce = (tag, cls, html) => { const e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; };
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const plural = (n, w, ws = w + "s") => `${(+n || 0).toLocaleString()} ${n === 1 ? w : ws}`;
const items = n => plural(n, "item");
const isDir = r => r.k === "d";
const extOf = n => { const b = baseName(n), i = b.lastIndexOf("."); return i > 0 ? b.slice(i + 1).toLowerCase() : ""; };
function baseName(p) {
  const s = p.length > 1 ? p.replace(/[\\/]+$/, "") : p;
  const i = Math.max(s.lastIndexOf("\\"), s.lastIndexOf("/"));
  return i >= 0 ? s.slice(i + 1) || s : s;
}
function dirName(p) {
  const s = p.length > 1 ? p.replace(/[\\/]+$/, "") : p;
  const i = Math.max(s.lastIndexOf("\\"), s.lastIndexOf("/"));
  if (i < 0) return "";
  const d = s.slice(0, i);
  if (/^[A-Za-z]:$/.test(d)) return d + "\\";
  return d || s[0];
}
const join = (d, n) => /[\\/]$/.test(d) ? d + n : d + FM.sep + n;
const norm = p => FM.ci ? p.toLowerCase() : p;
const sameP = (a, b) => a != null && b != null && norm(a) === norm(b);
const escRe = s => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const sizeWords = b => fmt(b).replace(/ TB$/, " terabytes").replace(/ GB$/, " gigabytes").replace(/ MB$/, " megabytes").replace(/ KB$/, " kilobytes").replace(/ B$/, " bytes");
function dur(s) {
  if (s == null || !isFinite(s)) return "";
  s = Math.max(1, Math.round(s));
  if (s < 60) return s + "s";
  if (s < 3600) return Math.floor(s / 60) + "m " + String(s % 60).padStart(2, "0") + "s";
  return Math.floor(s / 3600) + "h " + String(Math.floor(s % 3600 / 60)).padStart(2, "0") + "m";
}
function typeName(r) {
  if (r.k === "d") return "Folder";
  if (r.k === "l") return "Link";
  const c = r.c ?? 8;
  if (c !== 8) return CATN[c];
  const e = extOf(r.n);
  return e && e.length <= 6 ? e.toUpperCase() + " file" : "File";
}
function b64(s) { const bin = atob(s || ""), u = new Uint8Array(bin.length); for (let i = 0; i < bin.length; i++) u[i] = bin.charCodeAt(i); return u; }
const fmURL = p => `/fmfile?t=${encodeURIComponent(TOKEN)}&p=${encodeURIComponent(p)}`;
const binName = () => FM.trashName || S.trashName || "Recycle Bin";
const isMac = () => S.os === "macos";

const I = {
  dir: '<svg class="fi" viewBox="0 0 16 16" aria-hidden="true"><path d="M1.5 4.5v8h13v-7h-6l-1.5-2h-5z" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/></svg>',
  file: '<svg class="fi" viewBox="0 0 16 16" aria-hidden="true"><path d="M4 1.5h5.5L13 5v9.5H4zM9.5 1.5V5H13" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/></svg>',
  hfile: '<svg class="fi" viewBox="0 0 16 16" aria-hidden="true"><path d="M4 1.5h5.5L13 5v9.5H4zM9.5 1.5V5H13" fill="none" stroke="currentColor" stroke-width="1.3" stroke-dasharray="2 1.6" stroke-linejoin="round"/></svg>',
  hdir: '<svg class="fi" viewBox="0 0 16 16" aria-hidden="true"><path d="M1.5 4.5v8h13v-7h-6l-1.5-2h-5z" fill="none" stroke="currentColor" stroke-width="1.3" stroke-dasharray="2 1.6" stroke-linejoin="round"/></svg>',
  link: '<svg class="fi" viewBox="0 0 16 16" aria-hidden="true"><path d="M4 1.5h5.5L13 5v9.5H4z" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/><path d="M6.5 11.5c0-2.5 1.5-3.5 4-3.5M9 6l1.8 2L9 10" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  lock: '<svg viewBox="0 0 16 16" aria-hidden="true"><rect x="3" y="7" width="10" height="7.5" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.4"/><path d="M5.5 7V5a2.5 2.5 0 0 1 5 0v2" fill="none" stroke="currentColor" stroke-width="1.4"/></svg>',
  up: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 13V3M3.5 7.5 8 3l4.5 4.5"/></svg>',
  star: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round" aria-hidden="true"><path d="m8 1.8 1.9 3.9 4.3.6-3.1 3 .7 4.3L8 11.6l-3.8 2 .7-4.3-3.1-3 4.3-.6z"/></svg>',
  starOn: '<svg viewBox="0 0 16 16" fill="currentColor" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round" aria-hidden="true"><path d="m8 1.8 1.9 3.9 4.3.6-3.1 3 .7 4.3L8 11.6l-3.8 2 .7-4.3-3.1-3 4.3-.6z"/></svg>',
  search: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" aria-hidden="true"><circle cx="7" cy="7" r="4.5"/><path d="m10.5 10.5 3.5 3.5"/></svg>',
  warn: '<svg viewBox="0 0 16 16" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" aria-hidden="true"><circle cx="8" cy="8" r="6.5"/><path d="M8 4.5v4M8 11v.5"/></svg>',
  move: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 8h10M9 4.5 12.5 8 9 11.5"/></svg>',
  rename: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 12.5h3l7.5-7.5-3-3L2 9.5zM8 4l3 3"/></svg>',
  size: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" aria-hidden="true"><path d="M2 13.5h12M4 13.5V9M8 13.5V4M12 13.5V7"/></svg>',
};
const MEDIA = { img: "jpg jpeg png gif webp bmp svg ico avif", video: "mp4 webm m4v mov ogv", audio: "mp3 wav ogg oga m4a flac aac opus", pdf: "pdf" };
Object.keys(MEDIA).forEach(k => MEDIA[k] = new Set(MEDIA[k].split(" ")));
const mediaKind = n => { const e = extOf(n); for (const k in MEDIA) if (MEDIA[k].has(e)) return k; return null; };

/* ------------------------------------------------------------------ state */
const FM = {
  panes: [], active: 0, roots: [], home: "", sep: S.sep || "\\", ci: (S.sep || "\\") === "\\", trashName: S.trashName,
  hotlist: [], density: "compact", hidden: false, split: .5, cols: {},
  jobs: [], hist: [], pending: new Map(), seen: new Map(), built: false, ready: false, rh: 28,
  escAt: 0, chordAt: 0, jump: "", jumpPane: null, jumpT: 0, quick: null, drag: null, lastAnnounce: 0, renameEl: null,
};
const ACTIVE = new Set(["queued", "running", "paused", "waiting"]);
const MUT = op => op !== "sizes" && op !== "find";
const OVERSCAN = 10;
const coll = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });

let tabSeq = 0;
function newTab(path, o = {}) {
  return {
    id: ++tabSeq, path: path || "", kind: o.kind || "dir", label: o.label || "", sort: o.sort || "name", desc: !!o.desc, biggest: !!o.biggest,
    listing: o.listing === "brief" ? "brief" : "full", branch: false,
    rows: [], sorted: [], view: [], cur: 0, anchor: -1, curName: o.curName || null, scrollTop: 0,
    filter: { text: "", mode: "fuzzy", open: false, bad: false }, crumbs: [], parent: null, scanned: null, truncated: false, error: null,
    loading: false, loaded: false, tok: 0, back: [], fwd: [], ntag: 0, tagBytes: 0, tagUnknown: 0, maxS: 0, stale: false,
  };
}
const tabOf = P => P.tabs[P.ti];
const AP = () => FM.panes[FM.active];
const OP = () => FM.panes[1 - FM.active];
const AT = () => tabOf(AP());
const rowPath = (t, r) => r.path || join(t.path, r.n);
const curRow = (t = AT()) => t.view[t.cur];
const visible = (P, t) => tabOf(P) === t && FM.built && !FM.root.hidden;

/* ------------------------------------------------------------------ announcements */
let live, alertEl;
function announce(msg) {
  if (!live) return;
  live.textContent = "";
  setTimeout(() => { live.textContent = msg; }, 40);
}

/* ------------------------------------------------------------------ sorting and filtering */
function sortRows(t) {
  const rows = t.rows.slice(), big = rows.length > 20000, dir = t.desc ? -1 : 1;
  const name = big ? (a, b) => { const x = a._l ??= a.n.toLowerCase(), y = b._l ??= b.n.toLowerCase(); return x < y ? -1 : x > y ? 1 : 0; }
                   : (a, b) => coll.compare(a.n, b.n);
  let cmp;
  if (t.biggest) cmp = (a, b) => (a.s == null) - (b.s == null) || (b.s ?? 0) - (a.s ?? 0) || name(a, b);
  else if (t.sort === "size") cmp = (a, b) => (a.s == null) - (b.s == null) || dir * ((a.s ?? 0) - (b.s ?? 0)) || name(a, b);
  else if (t.sort === "mtime") cmp = (a, b) => dir * ((a.m || 0) - (b.m || 0)) || name(a, b);
  else if (t.sort === "ext") cmp = (a, b) => dir * (coll.compare(typeName(a), typeName(b)) || coll.compare(extOf(a.n), extOf(b.n))) || name(a, b);
  else cmp = (a, b) => dir * name(a, b);
  const ff = !t.biggest;
  rows.sort(ff ? (a, b) => (isDir(b) - isDir(a)) || cmp(a, b) : cmp);
  t.sorted = rows;
  let m = 0;
  for (const r of rows) if (r.s != null && r.s > m) m = r.s;
  t.maxS = m;
}
function globRes(pat, cs) {
  return pat.split(/[;,]/).map(s => s.trim()).filter(Boolean)
    .map(p => new RegExp("^" + p.replace(/[.+^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*").replace(/\?/g, ".") + "$", cs ? "" : "i"));
}
function matcher(text, mode, cs = false) {
  if (!text) return null;
  if (mode === "regex") { const re = new RegExp(text, cs ? "" : "i"); return n => re.test(n); }
  if (mode === "pattern") { const res = globRes(text, cs); return res.length ? n => res.some(re => re.test(n)) : null; }
  const q = text.toLowerCase();
  return n => { const s = n.toLowerCase(); let j = 0; for (let i = 0; i < s.length && j < q.length; i++) if (s[i] === q[j]) j++; return j === q.length; };
}
function applyFilter(t, quiet) {
  const f = t.filter;
  let v = t.sorted;
  f.bad = false;
  if (f.text) {
    let m = null;
    try { m = matcher(f.text, f.mode); } catch { f.bad = true; }
    if (m) v = v.filter(r => m(baseName(r.n)));
    else if (f.bad) v = t.view.length || !t.sorted.length ? t.view : t.sorted;   // invalid regex: keep what was showing
  }
  t.view = v;
  if (t.ntag && f.text && !f.bad) {
    const inView = new Set(v);
    let cleared = 0;
    for (const r of t.rows) if (r._t && !inView.has(r)) { r._t = false; cleared++; }
    if (cleared && !quiet) announce(`${plural(cleared, "tag")} cleared: those items are outside the filter.`);
  }
  recount(t);
}
function recount(t) {
  let n = 0, b = 0, u = 0;
  for (const r of t.rows) if (r._t) { n++; if (r.s == null) u++; else b += r.s; }
  t.ntag = n; t.tagBytes = b; t.tagUnknown = u;
}
function setTag(t, r, on) { r._t = !!on; }
function placeCursor(t, name, fallback = 0) {
  let i = name != null ? t.view.findIndex(r => r.n === name) : -1;
  if (i < 0 && name != null && FM.ci) { const l = name.toLowerCase(); i = t.view.findIndex(r => r.n.toLowerCase() === l); }
  t.cur = clamp(i >= 0 ? i : fallback, 0, Math.max(0, t.view.length - 1));
  t.curName = t.view[t.cur]?.n ?? null;
}
function resort(t, keepName = true) {
  const name = keepName ? t.view[t.cur]?.n ?? t.curName : null;
  sortRows(t); applyFilter(t, true);
  placeCursor(t, name, t.cur);
}
/* the rows an operation acts on: explicit rows, else tagged, else the cursor row */
function sel(t, rows) {
  if (rows) return rows;
  if (t.ntag) return t.rows.filter(r => r._t);
  const c = t.view[t.cur];
  return c ? [c] : [];
}

/* ------------------------------------------------------------------ DOM */
function build() {
  const root = FM.root = $("#files");
  root.innerHTML = "";
  root.setAttribute("aria-label", "Files");
  const panes = FM.panesEl = ce("div", "fm-panes");
  FM.panes = [0, 1].map(side => makePane(side));
  const div = FM.divider = ce("div", "fm-divider");
  Object.assign(div, { tabIndex: 0, title: "Drag to resize the panes. Double-click to make them equal." });
  div.setAttribute("role", "separator"); div.setAttribute("aria-orientation", "vertical"); div.setAttribute("aria-label", "Resize panes");
  div.setAttribute("aria-valuemin", "20"); div.setAttribute("aria-valuemax", "80");
  panes.append(FM.panes[0].el.root, div, FM.panes[1].el.root);
  root.appendChild(panes);
  // job strip
  const js = FM.jobsEl = ce("div", "fm-jobs");
  js.setAttribute("role", "region"); js.setAttribute("aria-label", "File operations");
  js.innerHTML = `<span class="txt" id="fmJobTxt"></span>
    <button class="btn" data-j="answer" hidden>Answer</button>
    <button class="btn" data-j="pause" hidden>Pause</button>
    <button class="btn" data-j="cancel" hidden>Cancel</button>
    <button class="btn ghost" data-j="list">Jobs</button>
    <i class="bar" role="progressbar" aria-label="Operation progress" aria-valuemin="0" aria-valuemax="100" hidden></i>`;
  root.appendChild(js);
  // F-key bar
  const fk = FM.fkeysEl = ce("div", "fm-fkeys");
  fk.setAttribute("role", "toolbar"); fk.setAttribute("aria-label", "Function keys");
  root.appendChild(fk);
  live = ce("div", "sr"); live.setAttribute("aria-live", "polite"); root.appendChild(live);
  alertEl = ce("div", "sr"); alertEl.setAttribute("role", "alert"); root.appendChild(alertEl);
  FM.tip = ce("div", "fm-dragtip"); FM.tip.hidden = true; document.body.appendChild(FM.tip);
  FM.chordEl = ce("div", "fm-chord"); FM.chordEl.hidden = true; document.body.appendChild(FM.chordEl);
  // the header keeps an indicator while jobs run in other views
  const hj = FM.hjob = ce("button", "scanchip fm-hjob");
  hj.hidden = true; hj.title = "File operations are running. Open Files.";
  hj.onclick = () => setMode("files");
  const hs = document.querySelector(".hstatus");
  if (hs) hs.insertBefore(hj, hs.querySelector(".simpletoggle"));
  wireDivider(); wireJobs(); wireStageDrop();
  FM.built = true;
}

function makePane(side) {
  const P = { side, name: side ? "right" : "left", tabs: [], ti: 0, hist: [], pool: new Map(), free: [], el: {} };
  const r = ce("div", "fm-pane");
  r.dataset.side = side;
  r.setAttribute("role", "region");
  r.setAttribute("aria-label", side ? "Right pane" : "Left pane");
  r.innerHTML = `
    <div class="fm-tabs" role="tablist" aria-label="${side ? "Right" : "Left"} pane tabs"></div>
    <div class="fm-nav">
      <button class="fm-ib fm-up" title="Parent folder (Backspace)" aria-label="Parent folder">${I.up}</button>
      <div class="fm-crumbs" title="Click empty space to type a path (Alt+C)"></div>
      <input class="fm-pathin" type="text" spellcheck="false" autocomplete="off" aria-label="Folder path. Enter goes there, Escape cancels." hidden>
      <button class="fm-ib fm-star" aria-pressed="false"></button>
      <button class="fm-ib fm-more" title="Pane options" aria-label="Pane options" aria-haspopup="menu">${ICON.more}</button>
    </div>
    <div class="fm-filter" hidden>${I.search}
      <input type="search" aria-label="Quick filter. Escape clears it." placeholder="Filter names" spellcheck="false" autocomplete="off">
      <div class="fm-seg" role="group" aria-label="Filter mode"><button data-fm="fuzzy" title="Letters in order, anywhere in the name">Fuzzy</button><button data-fm="pattern" title="Wildcards, like *.jpg;*.png">Pattern</button><button data-fm="regex" title="Regular expression">Regex</button></div>
      <button class="fm-ib fm-fclose" aria-label="Clear filter" title="Clear filter (Escape)"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" aria-hidden="true"><path d="M4 4l8 8M12 4l-8 8"/></svg></button>
    </div>
    <div class="fm-body">
      <div class="fm-grid" role="grid" tabindex="0" aria-multiselectable="true" aria-colcount="5">
        <div class="fm-head" role="row" aria-rowindex="1"></div>
        <div class="fm-scroll" role="rowgroup"><div class="fm-spacer" aria-hidden="true"></div></div>
      </div>
      <div class="fm-msg" hidden></div>
    </div>
    <div class="fm-status"><span class="fm-sel"></span><span class="fm-jump" hidden></span><span class="sp"></span><span class="fm-free"></span></div>
    <div class="fm-quick" hidden></div>`;
  const q = s => r.querySelector(s);
  P.el = { root: r, tabs: q(".fm-tabs"), nav: q(".fm-nav"), up: q(".fm-up"), crumbs: q(".fm-crumbs"), pathin: q(".fm-pathin"), star: q(".fm-star"),
           more: q(".fm-more"), filter: q(".fm-filter"), fin: q(".fm-filter input"), fseg: q(".fm-seg"), body: q(".fm-body"), grid: q(".fm-grid"),
           head: q(".fm-head"), scroll: q(".fm-scroll"), spacer: q(".fm-spacer"), msg: q(".fm-msg"), status: q(".fm-status"), sel: q(".fm-sel"),
           jump: q(".fm-jump"), free: q(".fm-free"), quick: q(".fm-quick") };
  wirePane(P);
  return P;
}

/* ------------------------------------------------------------------ rendering */
function renderAllPanes() { FM.panes.forEach(renderPane); }
function renderPane(P) {
  P.el.root.classList.toggle("on", P === AP());
  renderTabs(P); renderNav(P); renderHead(P); renderFilter(P); renderBody(P); renderStatus(P);
}
function tabLabel(t) {
  if (t.kind === "find") return "Find: " + (t.label || "results");
  const b = baseName(t.path) || t.path || "…";
  return t.branch ? "Branch: " + b : b;
}
function renderTabs(P) {
  const one = P.tabs.length === 1;
  P.el.tabs.innerHTML = P.tabs.map((t, i) => `<div class="fm-tab" role="tab" id="fm${P.side}t${t.id}" aria-selected="${i === P.ti}" tabindex="${i === P.ti ? 0 : -1}"
      data-i="${i}" title="${esc(t.kind === "find" ? `Find results in ${t.path}` : t.path)}"><span>${esc(tabLabel(t))}</span>${one ? "" : `<span class="fm-tabx" data-x="${i}" title="Close tab (Alt+W)" aria-hidden="true">×</span>`}</div>`).join("")
    + `<button class="fm-tabadd" title="New tab (Alt+T)" aria-label="New tab">+</button>`;
}
function renderNav(P) {
  const t = tabOf(P), cr = P.el.crumbs;
  P.el.up.disabled = t.kind === "find" || (!t.parent && !t.branch);
  const crumbs = t.crumbs.length ? t.crumbs : t.path ? [{ n: t.path, path: t.path }] : [];
  const btn = (c, last) => `<button data-p="${esc(c.path)}" class="${last ? "last" : ""}" title="${esc(c.path)}">${esc(c.n)}</button>`;
  let parts;
  if (crumbs.length > 4) {
    parts = [btn(crumbs[0]), `<button class="fm-ell" aria-label="More folders" aria-haspopup="menu" title="More folders">…</button>`,
             btn(crumbs[crumbs.length - 2]), btn(crumbs[crumbs.length - 1], true)];
  } else parts = crumbs.map((c, i) => btn(c, i === crumbs.length - 1));
  const lead = t.kind === "find" ? `<span class="note">Results in</span>` : t.branch ? `<span class="note">All files in</span>` : "";
  cr.innerHTML = lead + parts.join('<span class="sep" aria-hidden="true">›</span>');
  cr._middle = crumbs.slice(1, -2);
  const saved = FM.hotlist.some(h => sameP(h.path, t.path));
  P.el.star.innerHTML = saved ? I.starOn : I.star;
  P.el.star.classList.toggle("on", saved);
  P.el.star.setAttribute("aria-pressed", String(saved));
  P.el.star.title = saved ? "Remove from saved locations" : "Save this location (Ctrl+\\ shows saved locations)";
  P.el.star.setAttribute("aria-label", P.el.star.title);
  P.el.star.disabled = t.kind === "find";
}
const COLS = [
  { k: "tag", cls: "c-tag" }, { k: "name", cls: "c-name", label: "Name", sort: "name" }, { k: "size", cls: "c-size", label: "Size", sort: "size", rz: "size" },
  { k: "mod", cls: "c-mod", label: "Modified", sort: "mtime", rz: "mod" }, { k: "type", cls: "c-type", label: "Type", sort: "ext", rz: "type" },
];
function renderHead(P) {
  const t = tabOf(P), allTagged = t.view.length > 0 && t.ntag >= t.view.length;
  P.el.grid.classList.toggle("brief", t.listing === "brief");
  P.el.grid.setAttribute("aria-colcount", t.listing === "brief" ? "3" : "5");
  P.el.head.innerHTML = COLS.map(c => {
    if (c.k === "tag") return `<div role="columnheader" class="c-tag" aria-label="Tag"><button data-tagall title="${allTagged ? "Untag all (Ctrl+A)" : "Tag all shown items (Ctrl+A)"}" aria-label="${allTagged ? "Untag all" : "Tag all shown items"}"><span class="fm-check" style="${allTagged ? "background:var(--accent-solid);border-color:var(--accent-solid)" : ""}"></span></button></div>`;
    const on = t.biggest ? c.sort === "size" : t.sort === c.sort;
    const desc = t.biggest || t.desc;
    const aria = on ? (desc ? "descending" : "ascending") : "none";
    const label = t.biggest && c.sort === "size" ? "Biggest first" : c.label;
    return `<div role="columnheader" class="${c.cls}" aria-sort="${aria}"><button data-sort="${c.sort}" title="Sort by ${c.label.toLowerCase()}">${c.k === "size" && on ? `<span class="arr" aria-hidden="true">${desc ? "↓" : "↑"}</span>` : ""}${esc(label)}${c.k !== "size" && on ? `<span class="arr" aria-hidden="true">${desc ? "↓" : "↑"}</span>` : ""}</button>${c.rz ? `<span class="fm-rz" data-rz="${c.rz}" aria-hidden="true"></span>` : ""}</div>`;
  }).join("");
}
function renderFilter(P) {
  const t = tabOf(P), f = t.filter;
  P.el.filter.hidden = !(f.open || f.text);
  if (P.el.fin.value !== f.text && document.activeElement !== P.el.fin) P.el.fin.value = f.text;
  P.el.fin.setAttribute("aria-invalid", String(!!f.bad));
  P.el.fseg.querySelectorAll("button").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.fm === f.mode)));
}
function renderBody(P) {
  const t = tabOf(P), g = P.el.grid;
  g.setAttribute("aria-rowcount", String(t.view.length + 1));
  g.setAttribute("aria-label", `${P.side ? "Right" : "Left"} pane: ${t.kind === "find" ? `find results in ${t.path}` : t.path}`);
  renderMsg(P);
  renderRows(P, true);
}
function renderMsg(P) {
  const t = tabOf(P), m = P.el.msg;
  let html = "", err = false;
  if (t.loading && t.showLoading) html = `<div class="loading">${SPIN}Reading folder…</div>`;
  else if (t.error && !t.rows.length) {
    err = true;
    html = `<div><b>${esc(t.error)}</b><div class="row-actions"><button class="btn" data-m="retry">Retry</button>${t.parent || dirName(t.path) ? `<button class="btn" data-m="up">Go up</button>` : ""}</div></div>`;
  } else if (!t.loading && t.loaded && !t.view.length) {
    if (t.filter.text && t.rows.length) html = `<div><b>No matching items.</b><button class="btn" data-m="clear">Clear filter</button></div>`;
    else html = `<div><b>${t.kind === "find" ? "Nothing was found." : t.branch ? "There are no files in this folder or below it." : "This folder is empty."}</b></div>`;
  }
  m.hidden = !html;
  m.classList.toggle("err", err);
  if (m._html !== html) { m.innerHTML = html; m._html = html; }
}
function rowHTML(t, r) {
  const dir = r.k === "d";
  const icon = dir ? (r.h ? I.hdir : I.dir) : r.k === "l" ? I.link : r.h ? I.hfile : I.file;
  let nm = r.n, rel = "";
  if (t.kind === "find" || t.branch) { const b = baseName(nm); rel = nm.slice(0, nm.length - b.length); nm = b; }
  let base = nm, ext = "";
  if (!dir) { const j = nm.lastIndexOf("."); if (j > 0 && nm.length - j <= 12) { base = nm.slice(0, j); ext = nm.slice(j); } }
  const size = r._calc ? `<span class="calc">Calculating…</span>`
    : r.s == null ? `<span class="unk" title="Not in the last scan. Press Space to calculate.">—</span>`
    : `${fmt(r.s)}${t.maxS && r.s > 0 ? `<i style="--p:${Math.max(.02, r.s / t.maxS).toFixed(3)}"></i>` : ""}`;
  return `<div role="gridcell" class="c-tag"><span class="fm-check" aria-hidden="true"></span><i class="fm-dot" style="background:var(--c${dir ? 8 : r.c ?? 8})" aria-hidden="true"></i></div>`
    + `<div role="gridcell" class="c-name">${icon}<span class="nm">${rel ? `<span class="rel nb" title="${esc(rel)}">${esc(rel)}</span>` : ""}<span class="nb">${esc(base)}</span>${ext ? `<span class="ne">${esc(ext)}</span>` : ""}</span>`
    + `${r.h ? `<span class="meta">Hidden</span>` : ""}${r.p ? `<span class="fm-lock" title="${esc(r.p)}">${I.lock}<span class="sr">Protected</span></span>` : ""}</div>`
    + `<div role="gridcell" class="c-size">${size}</div>`
    + `<div role="gridcell" class="c-mod">${r.m ? esc(when(r.m)) : ""}</div>`
    + `<div role="gridcell" class="c-type" title="${esc(typeName(r))}">${esc(typeName(r))}</div>`;
}
function rowLabel(r) {
  return `${r.n}, ${typeName(r)}, ${r._calc ? "size being calculated" : r.s == null ? "size unknown" : sizeWords(r.s)}${r.h ? ", hidden" : ""}${r.ro ? ", read-only" : ""}${r.p ? ", " + r.p : ""}`;
}
function renderRows(P, force) {
  const t = tabOf(P), sc = P.el.scroll, rh = FM.rh, n = t.view.length;
  const h = n * rh;
  if (P.el.spacer._h !== h) { P.el.spacer.style.height = h + "px"; P.el.spacer._h = h; }
  const top = sc.scrollTop, vh = sc.clientHeight || 600;
  let first = Math.max(0, Math.floor(top / rh) - OVERSCAN), last = Math.min(n, Math.ceil((top + vh) / rh) + OVERSCAN);
  const want = new Set();
  for (let i = first; i < last; i++) want.add(i);
  if (n && t.cur >= 0 && t.cur < n) want.add(t.cur);            // the cursor row always exists, for aria-activedescendant
  for (const [i, el] of P.pool) if (!want.has(i)) { P.pool.delete(i); el.remove(); P.free.push(el); }
  const on = P === AP(), idp = `fm${P.side}r`;
  for (const i of want) {
    const r = t.view[i];
    let el = P.pool.get(i);
    if (!el) {
      el = P.free.pop() || ce("div", "fm-row");
      el.setAttribute("role", "row");
      el.draggable = true;
      P.pool.set(i, el);
      sc.appendChild(el);
      el._sig = null;
    }
    const content = `${t.id}|${t.kind}|${t.branch}|${r.s}|${r._calc ? 1 : 0}|${t.maxS}|${r.m}`;
    const state = `${i}|${r._t ? 1 : 0}|${i === t.cur ? 1 : 0}|${rh}|${on ? 1 : 0}|${r._drop ? 1 : 0}`;
    if (force || el._row !== r || el._content !== content) { el.innerHTML = rowHTML(t, r); el._content = content; el._state = null; }
    if (el._state === state && el._row === r) continue;
    el._state = state; el._row = r;
    el.id = idp + i;
    el.dataset.i = i;
    el.className = "fm-row" + (isDir(r) ? " dir" : "") + (r._t ? " tag" : "") + (i === t.cur ? " cur" : "") + (r._drop ? " drop" : "");
    el.style.transform = `translateY(${i * rh}px)`;
    el.setAttribute("aria-rowindex", String(i + 2));
    el.setAttribute("aria-selected", String(!!r._t));
    el.setAttribute("aria-label", rowLabel(r));
  }
  if (n && P.pool.has(t.cur)) P.el.grid.setAttribute("aria-activedescendant", idp + t.cur);
  else P.el.grid.removeAttribute("aria-activedescendant");
}
function rootFor(path) {
  let best = null;
  for (const r of FM.roots) if (path && norm(path).startsWith(norm(r.path)) && (!best || r.path.length > best.path.length)) best = r;
  return best;
}
function renderStatus(P) {
  const t = tabOf(P);
  const calc = t.rows.some(r => r._calc);
  let s;
  if (t.ntag) {
    s = `<b>${t.ntag.toLocaleString()} selected</b> · ${fmt(t.tagBytes)}`;
    if (t.tagUnknown) s += ` <span class="muted">+ ${plural(t.tagUnknown, "folder")} not sized</span>`
      + (calc ? "" : ` <button class="link" data-s="calc" title="Calculate the unknown folder sizes">Calculate</button>`);
  } else s = `0 selected · 0 B`;
  const cnt = t.filter.text ? `${t.view.length.toLocaleString()} of ${t.rows.length.toLocaleString()} shown` : items(t.rows.length);
  s += ` <span class="muted">· ${cnt}${t.truncated ? ` · first ${t.rows.length.toLocaleString()} only` : ""}</span>`;
  if (calc) s += ` <span class="spinner sm" aria-hidden="true"></span><span class="muted">Calculating sizes…</span> <button class="link" data-s="stopcalc">Stop</button>`;
  P.el.sel.innerHTML = s;
  const root = rootFor(t.path);
  P.el.free.textContent = root && root.free != null ? `${fmt(root.free)} free` : "";
  P.el.status.title = t.scanned ? `Folder sizes from scan: ${new Date(t.scanned * 1000).toLocaleString()}`
                                : "Folder sizes aren't in a scan yet. Press Space to calculate.";
  const j = P.el.jump;
  j.hidden = !(FM.jump && FM.jumpPane === P);
  if (!j.hidden) j.textContent = "Jump: " + FM.jump;
}

/* scroll so the cursor row is in view */
function ensureVisible(P) {
  const t = tabOf(P), sc = P.el.scroll, rh = FM.rh, y = t.cur * rh;
  if (y < sc.scrollTop) sc.scrollTop = y;
  else if (y + rh > sc.scrollTop + sc.clientHeight) sc.scrollTop = y + rh - sc.clientHeight;
}
function moveCursor(P, i, extend) {
  const t = tabOf(P);
  if (!t.view.length) return;
  i = clamp(i, 0, t.view.length - 1);
  if (extend) {
    if (t.anchor < 0 || t.anchor >= t.view.length) t.anchor = t.cur;
    const a = Math.min(t.anchor, i), b = Math.max(t.anchor, i);
    for (let j = a; j <= b; j++) setTag(t, t.view[j], true);
    recount(t);
  } else t.anchor = i;
  t.cur = i; t.curName = t.view[i].n;
  ensureVisible(P);
  renderRows(P);
  if (extend) { renderStatus(P); renderHead(P); }
  quickSoon();
}
function refreshTags(P) { const t = tabOf(P); recount(t); renderRows(P); renderStatus(P); renderHead(P); }

/* ------------------------------------------------------------------ loading and navigation */
async function load(P, t, path, o = {}) {
  const my = ++t.tok;
  t.loading = true; t.showLoading = false;
  const slow = setTimeout(() => { if (t.tok === my && t.loading) { t.showLoading = true; if (visible(P, t)) renderMsg(P); } }, 150);
  let r;
  try {
    r = await api("fm_list", { path, hidden: FM.hidden, sort: t.biggest ? "size" : t.sort, desc: t.biggest ? true : t.desc,
                               foldersFirst: !t.biggest, branch: !!t.branch });
  } catch (e) { r = { error: e.message }; }
  clearTimeout(slow);
  if (my !== t.tok) return false;
  t.loading = false; t.showLoading = false;
  if (r.error && !r.rows?.length) {
    if (o.soft) { toast(r.error, "error"); if (visible(P, t)) renderMsg(P); return false; }
    Object.assign(t, { error: r.error, path: r.path || path, rows: [], crumbs: r.crumbs || [], parent: r.parent ?? (dirName(path) || null),
                       scanned: null, truncated: false, loaded: true });
    sortRows(t); applyFilter(t, true); t.cur = 0;
  } else {
    const keepTags = o.keep ? new Set(t.rows.filter(x => x._t).map(x => x.n)) : null;
    const keepCalc = o.keep ? new Map(t.rows.filter(x => x.s != null && x._sz === "calc").map(x => [x.n, x.s])) : null;
    Object.assign(t, { path: r.path || path, parent: r.parent ?? null, crumbs: r.crumbs || [], rows: r.rows || [], scanned: r.scanned ?? null,
                       truncated: !!r.truncated, error: r.error || null, loaded: true, stale: false });
    for (const x of t.rows) {
      if (keepTags?.has(x.n)) x._t = true;
      if (x.s == null && keepCalc?.has(x.n)) { x.s = keepCalc.get(x.n); x._sz = "calc"; }
    }
    if (!o.keep) { t.filter.text = ""; t.filter.open = false; t.anchor = -1; }
    sortRows(t); applyFilter(t, true);
    placeCursor(t, o.focus ?? (o.keep ? t.curName : null), o.keep ? t.cur : 0);
  }
  if (visible(P, t)) {
    if (!o.keep) P.el.scroll.scrollTop = 0;
    renderPane(P);
    ensureVisible(P); renderRows(P);
    if (P === AP()) quickSoon();
  }
  if (t.kind === "dir" && !t.error) addPaneHist(P, t.path);
  savePrefs();
  return true;
}
async function go(P, path, o = {}) {
  const t = tabOf(P);
  if (!path) return false;
  if (FM.quick && FM.quick.P === P) closeQuick();
  const prev = t.loaded && t.kind === "dir" ? { path: t.path, cur: t.view[t.cur]?.n, branch: t.branch } : null;
  if (o.branch === undefined) t.branch = false; else t.branch = o.branch;
  const ok = await load(P, t, path, o);
  if (ok && prev && o.hist !== false && !(sameP(prev.path, t.path) && prev.branch === t.branch)) {
    t.back.push(prev); if (t.back.length > 60) t.back.shift(); t.fwd = [];
  }
  return ok;
}
function goUp(P = AP()) {
  const t = tabOf(P);
  if (t.kind === "find") return;
  if (t.branch) return go(P, t.path, { branch: false });
  const up = t.parent || (t.error ? dirName(t.path) : null);
  if (up && !sameP(up, t.path)) go(P, up, { focus: baseName(t.path) });
}
function histMove(P, dir) {
  const t = tabOf(P), from = dir < 0 ? t.back : t.fwd, to = dir < 0 ? t.fwd : t.back;
  if (t.kind !== "dir" || !from.length) return announce(dir < 0 ? "Nothing to go back to." : "Nothing to go forward to.");
  const h = from.pop();
  to.push({ path: t.path, cur: t.view[t.cur]?.n, branch: t.branch });
  t.branch = !!h.branch;
  load(P, t, h.path, { focus: h.cur });
}
function addPaneHist(P, path) {
  P.hist = [path, ...P.hist.filter(p => !sameP(p, path))].slice(0, 30);
}
async function refresh(P, t = tabOf(P)) {
  if (t.kind === "find") { if (visible(P, t)) renderPane(P); return; }   // results are a snapshot; finished jobs prune them
  return load(P, t, t.path, { keep: true });
}
function refreshAll() {
  for (const P of FM.panes) for (const t of P.tabs) {
    if (t.kind === "find") continue;
    if (tabOf(P) === t) refresh(P, t); else t.stale = true;
  }
}
function activate(P, focus) {
  if (FM.quick && FM.quick.P === P) closeQuick();
  if (AP() !== P) {
    FM.active = P.side;
    FM.panes.forEach(x => { x.el.root.classList.toggle("on", x === P); renderRows(x, true); });
    savePrefs();
    quickSoon();
  }
  if (focus) focusGrid(P);
}
function focusGrid(P = AP()) { if (FM.built && !FM.root.hidden) P.el.grid.focus({ preventScroll: true }); }
function switchTab(P, i) {
  if (i === P.ti || i < 0 || i >= P.tabs.length) return;
  if (P.tabs[P.ti]) P.tabs[P.ti].scrollTop = P.el.scroll.scrollTop;
  P.ti = i;
  const t = tabOf(P);
  for (const [, el] of P.pool) { el.remove(); P.free.push(el); }
  P.pool.clear();
  renderPane(P);
  P.el.scroll.scrollTop = t.scrollTop || 0;
  renderRows(P, true);
  if (!t.loaded || t.stale) load(P, t, t.path, { keep: t.loaded });
  savePrefs();
}
function addTab(P, path, o = {}) {
  const src = tabOf(P);
  const t = o.tab || newTab(path || src.path, { sort: src.sort, desc: src.desc, biggest: src.biggest, listing: src.listing });
  P.tabs.splice(P.ti + 1, 0, t);
  switchTab(P, P.ti + 1);
  if (!o.tab) announce(`New tab: ${tabLabel(t)}`);
  return t;
}
function closeTab(P, i = P.ti) {
  if (P.tabs.length <= 1) return announce("The last tab stays open.");
  P.tabs.splice(i, 1);
  const ni = i < P.ti ? P.ti - 1 : Math.min(P.ti, P.tabs.length - 1);
  P.ti = -1;
  switchTab(P, ni);
  announce(`Tab closed. Now showing ${tabLabel(tabOf(P))}.`);
}

/* ------------------------------------------------------------------ pane events */
function wirePane(P) {
  const e = P.el;
  e.root.addEventListener("mousedown", ev => { if (!e.quick.contains(ev.target)) activate(P); }, true);
  // tabs
  e.tabs.addEventListener("click", ev => {
    const x = ev.target.closest(".fm-tabx");
    if (x) { ev.stopPropagation(); return closeTab(P, +x.dataset.x); }
    if (ev.target.closest(".fm-tabadd")) { addTab(P); return focusGrid(P); }
    const tb = ev.target.closest(".fm-tab");
    if (tb) { switchTab(P, +tb.dataset.i); focusGrid(P); }
  });
  e.tabs.addEventListener("auxclick", ev => { const tb = ev.target.closest(".fm-tab"); if (ev.button === 1 && tb) closeTab(P, +tb.dataset.i); });
  e.tabs.addEventListener("keydown", ev => {
    const tb = ev.target.closest(".fm-tab");
    if (!tb) return;
    const i = +tb.dataset.i;
    if (ev.key === "ArrowRight" || ev.key === "ArrowLeft") {
      ev.preventDefault();
      const ni = (i + (ev.key === "ArrowRight" ? 1 : -1) + P.tabs.length) % P.tabs.length;
      switchTab(P, ni); e.tabs.querySelectorAll(".fm-tab")[ni]?.focus();
    } else if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); switchTab(P, i); focusGrid(P); }
    else if (ev.key === "Delete") { ev.preventDefault(); closeTab(P, i); }
  });
  // navigation bar
  e.up.onclick = () => { goUp(P); focusGrid(P); };
  e.crumbs.addEventListener("click", ev => {
    const b = ev.target.closest("button");
    if (b?.classList.contains("fm-ell")) {
      const r = b.getBoundingClientRect();
      return fmMenu(r.left, r.bottom + 4, e.crumbs._middle.map(c => ({ label: c.n, run: () => go(P, c.path).then(() => focusGrid(P)) })));
    }
    if (b?.dataset.p) return go(P, b.dataset.p).then(() => focusGrid(P));
    editPath(P);
  });
  e.pathin.addEventListener("keydown", ev => {
    if (ev.key === "Enter") {
      ev.preventDefault(); ev.stopPropagation();
      let v = e.pathin.value.trim().replace(/^"(.*)"$/, "$1");
      if (v === "~" || v.startsWith("~/") || v.startsWith("~\\")) v = FM.home + v.slice(1);
      if (!v) return endEdit(P, true);
      e.pathin.disabled = true;
      go(P, v, { soft: true }).then(ok => {
        e.pathin.disabled = false;
        if (ok) endEdit(P, true); else { e.pathin.focus(); e.pathin.select(); }
      });
    } else if (ev.key === "Escape") { ev.preventDefault(); ev.stopPropagation(); endEdit(P, true); }
  });
  e.pathin.addEventListener("blur", () => { if (!e.pathin.disabled) endEdit(P, false); });
  e.star.onclick = () => toggleHot(P);
  e.more.onclick = () => { const r = e.more.getBoundingClientRect(); paneMenu(P, r.right - 260, r.bottom + 4); };
  // quick filter
  e.fin.addEventListener("input", () => {
    const t = tabOf(P);
    t.filter.text = e.fin.value;
    clearTimeout(P.ft);
    P.ft = setTimeout(() => filterNow(P), t.rows.length > 20000 ? 150 : 40);
  });
  e.fin.addEventListener("keydown", ev => {
    if (ev.key === "Escape") { ev.preventDefault(); ev.stopPropagation(); clearFilter(P); focusGrid(P); }
    else if (["Enter", "ArrowDown", "ArrowUp", "PageDown", "PageUp"].includes(ev.key)) {
      ev.preventDefault(); ev.stopPropagation();
      clearTimeout(P.ft); filterNow(P);
      if (ev.key === "ArrowDown" && document.activeElement === e.fin) moveCursor(P, tabOf(P).cur);
      focusGrid(P);
    }
  });
  e.fseg.addEventListener("click", ev => { const b = ev.target.closest("button"); if (!b) return; tabOf(P).filter.mode = b.dataset.fm; filterNow(P); e.fin.focus(); });
  e.filter.querySelector(".fm-fclose").onclick = () => { clearFilter(P); focusGrid(P); };
  // messages and status
  e.msg.addEventListener("click", ev => {
    const b = ev.target.closest("[data-m]"), t = tabOf(P);
    if (!b) return;
    if (b.dataset.m === "retry") load(P, t, t.path, { keep: true });
    else if (b.dataset.m === "up") goUp(P);
    else if (b.dataset.m === "clear") clearFilter(P);
    focusGrid(P);
  });
  e.status.addEventListener("click", ev => {
    const b = ev.target.closest("[data-s]");
    if (!b) return;
    if (b.dataset.s === "calc") calcSelection(P); else stopSizes();
  });
  // column headings
  e.head.addEventListener("click", ev => {
    if (ev.target.closest(".fm-rz")) return;
    const b = ev.target.closest("button");
    if (!b) return;
    if (b.hasAttribute("data-tagall")) tagAll(P); else setSort(P, b.dataset.sort);
    focusGrid(P);
  });
  e.head.addEventListener("pointerdown", ev => { const h = ev.target.closest(".fm-rz"); if (h) colResize(ev, h.dataset.rz); });
  // rows
  e.scroll.addEventListener("scroll", () => { if (!P.raf) P.raf = requestAnimationFrame(() => { P.raf = 0; renderRows(P); }); }, { passive: true });
  new ResizeObserver(() => renderRows(P)).observe(e.scroll);
  e.scroll.addEventListener("click", ev => {
    const el = ev.target.closest(".fm-row");
    if (!el) return;
    const t = tabOf(P), i = +el.dataset.i, r = t.view[i];
    if (!r) return;
    if (ev.target.closest(".fm-check") || ev.ctrlKey || ev.metaKey) {
      setTag(t, r, !r._t); t.anchor = i; t.cur = i; t.curName = r.n;
      refreshTags(P); quickSoon();
      return;
    }
    moveCursor(P, i, ev.shiftKey);
  });
  e.scroll.addEventListener("dblclick", ev => {
    const el = ev.target.closest(".fm-row");
    if (!el || ev.target.closest(".fm-check")) return;
    moveCursor(P, +el.dataset.i);
    enter(P);
  });
  e.scroll.addEventListener("contextmenu", ev => {
    ev.preventDefault();
    const el = ev.target.closest(".fm-row"), t = tabOf(P);
    if (!el) return paneMenu(P, ev.clientX, ev.clientY);
    const i = +el.dataset.i, r = t.view[i];
    const rows = r._t && t.ntag ? t.rows.filter(x => x._t) : [r];
    t.cur = i; t.curName = r.n; renderRows(P);
    contextMenu(P, rows, ev.clientX, ev.clientY);
  });
  e.scroll.addEventListener("dragstart", ev => rowDragStart(P, ev));
  e.scroll.addEventListener("dragend", dragEnd);
  e.body.addEventListener("dragover", ev => dragOver(P, ev));
  e.body.addEventListener("dragleave", ev => { if (!e.body.contains(ev.relatedTarget)) dragClear(); });
  e.body.addEventListener("drop", ev => dropOn(P, ev));
  e.quick.addEventListener("click", ev => { if (ev.target.closest("[data-q=close]")) { closeQuick(); focusGrid(); } });
}
function editPath(P) {
  const t = tabOf(P), e = P.el;
  e.crumbs.hidden = true; e.pathin.hidden = false;
  e.pathin.value = t.path;
  e.pathin.focus(); e.pathin.select();
}
function endEdit(P, focus) {
  const e = P.el;
  if (e.pathin.hidden) return;
  e.pathin.hidden = true; e.crumbs.hidden = false;
  if (focus) focusGrid(P);
}
function filterNow(P) {
  const t = tabOf(P), name = t.view[t.cur]?.n ?? t.curName;
  applyFilter(t);
  placeCursor(t, name, 0);
  if (!t.view.length || t.cur === 0) P.el.scroll.scrollTop = 0;
  renderFilter(P); renderHead(P); renderBody(P); renderStatus(P); ensureVisible(P); renderRows(P);
  if (t.filter.text && !t.filter.bad) announce(t.view.length ? `${items(t.view.length)} match` : "No matching items.");
  quickSoon();
}
function openFilter(P = AP()) {
  const t = tabOf(P);
  t.filter.open = true;
  renderFilter(P);
  P.el.fin.focus(); P.el.fin.select();
}
function clearFilter(P) {
  const t = tabOf(P);
  t.filter.text = ""; t.filter.open = false; t.filter.bad = false;
  P.el.fin.value = "";
  filterNow(P);
}
function setSort(P, key, biggest) {
  const t = tabOf(P);
  if (biggest !== undefined) t.biggest = biggest;
  else if (t.biggest) { t.biggest = false; t.sort = key; t.desc = key === "size" || key === "mtime"; }
  else if (t.sort === key) t.desc = !t.desc;
  else { t.sort = key; t.desc = key === "size" || key === "mtime"; }
  resort(t);
  renderHead(P); renderRows(P, true); ensureVisible(P); renderRows(P);
  savePrefs();
  const names = { name: "name", size: "size", mtime: "modified date", ext: "type" };
  announce(t.biggest ? "Biggest first" : `Sorted by ${names[t.sort]}, ${t.desc ? "descending" : "ascending"}`);
}
function tagAll(P) {
  const t = tabOf(P), all = t.view.length && t.view.every(r => r._t);
  for (const r of t.view) setTag(t, r, !all);
  refreshTags(P);
  announce(all ? "All tags cleared." : `${items(t.view.length)} tagged, ${fmt(t.tagBytes)}${t.tagUnknown ? ` plus ${plural(t.tagUnknown, "folder")} not sized` : ""}.`);
}
function invertTags(P) {
  const t = tabOf(P);
  for (const r of t.view) setTag(t, r, !r._t);
  refreshTags(P);
  announce(`${items(t.ntag)} tagged.`);
}

/* ------------------------------------------------------------------ divider and column widths */
function setSplit(x) {
  FM.split = clamp(x, .2, .8);
  FM.panesEl.style.setProperty("--fm-l", FM.split + "fr");
  FM.panesEl.style.setProperty("--fm-r", (1 - FM.split) + "fr");
  FM.divider.setAttribute("aria-valuenow", String(Math.round(FM.split * 100)));
}
function wireDivider() {
  const d = FM.divider;
  d.addEventListener("pointerdown", e => {
    d.setPointerCapture(e.pointerId); d.classList.add("drag");
    const box = FM.panesEl.getBoundingClientRect();
    const mv = ev => setSplit((ev.clientX - box.left) / box.width);
    const up = () => { d.classList.remove("drag"); d.removeEventListener("pointermove", mv); d.removeEventListener("pointerup", up); savePrefs(); };
    d.addEventListener("pointermove", mv); d.addEventListener("pointerup", up);
  });
  d.addEventListener("dblclick", () => { setSplit(.5); savePrefs(); });
  d.addEventListener("keydown", e => {
    if (e.key === "ArrowLeft" || e.key === "ArrowRight") { e.preventDefault(); setSplit(FM.split + (e.key === "ArrowLeft" ? -.02 : .02)); savePrefs(); }
    else if (e.key === "Home" || e.key === "End") { e.preventDefault(); setSplit(e.key === "Home" ? .2 : .8); savePrefs(); }
  });
}
const COLW = { size: [104, 64, 220], mod: [136, 80, 260], type: [80, 50, 220] };
function applyCols() { for (const k in COLW) FM.root.style.setProperty("--w-" + k, (FM.cols[k] || COLW[k][0]) + "px"); }
function colResize(e, k) {
  e.preventDefault(); e.stopPropagation();
  const h = e.target, x0 = e.clientX, w0 = FM.cols[k] || COLW[k][0];
  h.setPointerCapture(e.pointerId);
  const mv = ev => { FM.cols[k] = clamp(Math.round(w0 - (ev.clientX - x0)), COLW[k][1], COLW[k][2]); applyCols(); };
  const up = () => { h.removeEventListener("pointermove", mv); h.removeEventListener("pointerup", up); FM.panes.forEach(P => renderRows(P, true)); savePrefs(); };
  h.addEventListener("pointermove", mv); h.addEventListener("pointerup", up);
}

/* ------------------------------------------------------------------ menus */
function fmMenu(x, y, entries) {
  entries = entries.filter(Boolean);
  const anyCheck = entries.some(e => e.checked !== undefined);
  FM.menuBack = document.activeElement;
  menu.innerHTML = entries.map((e, i) => {
    if (e === "-") return "<hr>";
    if (e.header) return `<div class="fm-mh" role="presentation">${esc(e.header)}</div>`;
    const check = e.checked !== undefined;
    return `<button data-i="${i}" role="${check ? "menuitemcheckbox" : "menuitem"}" ${check ? `aria-checked="${!!e.checked}"` : ""}
      class="${e.danger ? "danger" : ""}" ${e.disabled ? `aria-disabled="true" title="${esc(e.disabled === true ? "" : e.disabled)}"` : e.title ? `title="${esc(e.title)}"` : ""}>${check ? `<span class="fm-ck" aria-hidden="true">${e.checked ? "✓" : ""}</span>` : e.icon || (anyCheck ? '<span class="fm-ck" aria-hidden="true"></span>' : "")}${esc(e.label)}${e.keys ? `<kbd>${esc(e.keys)}</kbd>` : ""}</button>`;
  }).join("").replace(/^(<hr>)+|(<hr>)+$/g, "").replace(/(<hr>){2,}/g, "<hr>");
  menu.querySelectorAll("button").forEach(b => b.onclick = () => {
    const e = entries[+b.dataset.i];
    if (e.disabled) { if (typeof e.disabled === "string") { toast(e.disabled); announce(e.disabled); } return; }
    closeMenu();
    e.run();
  });
  menu.style.display = "block";
  menu.style.left = Math.max(8, Math.min(x, innerWidth - menu.offsetWidth - 8)) + "px";
  menu.style.top = Math.max(8, Math.min(y, innerHeight - menu.offsetHeight - 8)) + "px";
  (menu.querySelector("button:not([aria-disabled])") || menu.querySelector("button"))?.focus();
}
function menuKey(e) {
  const btns = [...menu.querySelectorAll("button")], i = btns.indexOf(document.activeElement);
  if (e.key === "ArrowDown") { btns[(i + 1) % btns.length]?.focus(); return true; }
  if (e.key === "ArrowUp") { btns[(i - 1 + btns.length) % btns.length]?.focus(); return true; }
  if (e.key === "Home") { btns[0]?.focus(); return true; }
  if (e.key === "End") { btns[btns.length - 1]?.focus(); return true; }
  if (e.key === "Escape" || e.key === "Tab" || e.key === "F10") {
    closeMenu();
    if (FM.menuBack?.isConnected && FM.root.contains(FM.menuBack)) FM.menuBack.focus(); else focusGrid();
    return true;
  }
  return false;
}
function paneMenu(P, x, y) {
  const t = tabOf(P), dir = t.kind === "dir" && !t.branch;
  fmMenu(x, y, [
    { header: "Listing" },
    { label: "Full listing", checked: t.listing === "full", run: () => setListing(P, "full") },
    { label: "Brief listing", checked: t.listing === "brief", run: () => setListing(P, "brief") },
    { label: "Biggest first", checked: t.biggest, run: () => setSort(P, "size", !t.biggest) },
    { label: "Show hidden files", checked: FM.hidden, run: toggleHidden },
    { label: "Comfortable rows", checked: FM.density === "comfortable", run: toggleDensity },
    "-",
    { label: "Refresh this folder", keys: "Ctrl+R", run: () => refresh(P) },
    { label: "Branch view: all files below", keys: "Ctrl+B", checked: t.branch, disabled: t.kind === "find" ? "Search results can't be shown as a branch." : false, run: () => { activate(P); toggleBranch(); } },
    dir ? { label: "New folder…", keys: "F7", run: () => { activate(P); newItem("dir"); } } : null,
    dir ? { label: "New file…", keys: "Shift+F7", run: () => { activate(P); newItem("file"); } } : null,
    "-",
    { label: "New tab", keys: "Alt+T", run: () => addTab(P) },
    { label: "Close tab", keys: "Alt+W", disabled: P.tabs.length < 2 ? "The last tab stays open." : false, run: () => closeTab(P) },
    { label: "Saved locations…", keys: "Ctrl+\\", run: () => { activate(P); hotlistDlg(); } },
    { label: "Folder history…", keys: "Alt+H", run: () => { activate(P); histMenu(P); } },
    "-",
    { label: t.scanned ? `Folder sizes from scan: ${new Date(t.scanned * 1000).toLocaleString()}` : "Folder sizes: not in a scan yet", disabled: true },
  ]);
}
function setListing(P, l) { tabOf(P).listing = l; renderHead(P); renderRows(P, true); savePrefs(); announce(l === "brief" ? "Brief listing: name and size." : "Full listing."); }
function toggleHidden() {
  FM.hidden = !FM.hidden;
  announce(FM.hidden ? "Showing hidden files." : "Hiding hidden files.");
  FM.panes.forEach(P => P.tabs.forEach(t => { if (t.kind === "find") return; if (tabOf(P) === t) load(P, t, t.path, { keep: true }); else t.stale = true; }));
  savePrefs();
}
function toggleDensity() {
  FM.density = FM.density === "comfortable" ? "compact" : "comfortable";
  FM.rh = FM.density === "comfortable" ? 34 : 28;
  FM.root.classList.toggle("comfy", FM.density === "comfortable");
  FM.panes.forEach(P => { renderRows(P, true); ensureVisible(P); renderRows(P); });
  savePrefs();
}
function contextMenu(P, rows, x, y) {
  const t = tabOf(P), one = rows.length === 1 ? rows[0] : null;
  const prot = rows.filter(r => r.p), lockWhy = prot.length && prot.length === rows.length ? prot[0].p : false;
  fmMenu(x, y, [
    one && isDir(one) ? { label: "Open", keys: "Enter", icon: ICON.open, run: () => enter(P, one) } : null,
    one && !isDir(one) ? { label: "View", keys: "F3", icon: ICON.eye, run: () => openViewer(false) } : null,
    one && !isDir(one) ? { label: "Open with default app", keys: "Enter", icon: ICON.launch, run: () => openDefault(rowPath(t, one)) } : null,
    one ? { label: isDir(one) ? `Open in ${S.revealName}` : `Show in ${S.revealName}`, icon: ICON.open, run: () => reveal(rowPath(t, one)) } : null,
    "-",
    { label: "Copy to the other pane…", keys: "F5", icon: ICON.copy, run: () => copyMove("copy", { rows, P }) },
    { label: "Move to the other pane…", keys: "F6", icon: I.move, disabled: lockWhy, run: () => copyMove("move", { rows, P }) },
    one ? { label: "Rename", keys: "Shift+F6", icon: I.rename, disabled: one.p || false, run: () => inlineRename(P) }
        : { label: "Rename these…", keys: "Ctrl+M", icon: I.rename, disabled: lockWhy, run: () => multiRename(rows, P) },
    "-",
    { label: one ? "Copy full path" : "Copy full paths", keys: "Ctrl+X P", icon: ICON.copy, run: () => copyPaths(rows, P) },
    { label: one ? "Copy name" : "Copy names", keys: "Ctrl+X C", run: () => copyNames(rows) },
    rows.some(isDir) ? { label: "Calculate folder size", keys: "Ctrl+Space", icon: I.size, run: () => calcSizes(P, rows.filter(isDir)) } : null,
    { label: "Mark for removal", icon: ICON.stage, disabled: lockWhy, run: () => stageRows(P, rows) },
    "-",
    { label: `Move to ${binName()}…`, keys: "F8", icon: ICON.trash, danger: true, disabled: lockWhy, run: () => removeDialog(false, rows, P) },
    { label: "Delete for good…", keys: "Shift+F8", danger: true, disabled: lockWhy, run: () => removeDialog(true, rows, P) },
  ]);
}
function menuAtCursor(P, entriesFn) {
  const t = tabOf(P), el = P.pool.get(t.cur);
  const r = (el || P.el.scroll).getBoundingClientRect();
  return entriesFn(r.left + 60, el ? r.bottom : r.top + 30);
}
function locationMenu(P) {
  activate(P);
  const r = P.el.nav.getBoundingClientRect();
  fmMenu(r.left + 8, r.bottom + 2, [
    { header: `${P.side ? "Right" : "Left"} pane: go to` },
    ...FM.roots.map(x => ({ label: `${x.name}${x.label ? " " + x.label : ""}${x.free != null ? ` · ${fmt(x.free)} free` : ""}`, run: () => go(P, x.path).then(() => focusGrid(P)) })),
    FM.home ? { label: "Home folder", title: FM.home, run: () => go(P, FM.home).then(() => focusGrid(P)) } : null,
    FM.hotlist.length ? "-" : null,
    FM.hotlist.length ? { header: "Saved locations" } : null,
    ...FM.hotlist.map(h => ({ label: h.name, title: h.path, run: () => go(P, h.path).then(() => focusGrid(P)) })),
  ]);
}
function histMenu(P) {
  const t = tabOf(P), list = P.hist.filter(p => !sameP(p, t.path));
  if (!list.length) return announce("No folder history yet.");
  const r = P.el.nav.getBoundingClientRect();
  fmMenu(r.left + 8, r.bottom + 2, [{ header: "Recent folders in this pane" }, ...list.slice(0, 20).map(p => ({ label: p, run: () => go(P, p).then(() => focusGrid(P)) }))]);
}

/* ------------------------------------------------------------------ keyboard */
function combo(e) {
  let k = e.key;
  if (k === " " || k === "Spacebar") k = "Space";
  else if (k === "Esc") k = "Escape";
  else if (k === "Apps") k = "ContextMenu";
  else if (k.length === 1) k = k.toUpperCase();
  if (e.code === "NumpadAdd") k = "+"; else if (e.code === "NumpadSubtract") k = "-"; else if (e.code === "NumpadMultiply") k = "*";
  const ctl = e.ctrlKey || e.metaKey;
  return (ctl ? "Ctrl+" : "") + (e.altKey ? "Alt+" : "") + (e.shiftKey && (k.length > 1 || ctl || e.altKey) ? "Shift+" : "") + k;
}
const GLOBAL_KEYS = {
  "F1": "help", "F2": "actions", "F3": "view", "Shift+F3": "hex", "F4": "edit", "Shift+F4": "edit", "F5": "copy", "Shift+F5": "copyas",
  "F6": "move", "Shift+F6": "rename", "F7": "mkdir", "Shift+F7": "mkfile", "F8": "trash", "Shift+F8": "delete", "F9": "menu", "F10": "close",
  "Shift+F10": "context", "ContextMenu": "context", "Alt+F1": "leftloc", "Alt+F2": "rightloc", "Alt+F7": "find", "Alt+Shift+?": "find", "Alt+?": "find",
  "Ctrl+S": "filter", "Ctrl+F": "filter", "Alt+C": "goto", "Ctrl+\\": "hotlist", "Ctrl+U": "swap", "Alt+O": "otherCursor", "Alt+I": "otherHere",
  "Alt+ArrowLeft": "back", "Alt+ArrowRight": "fwd", "Alt+Y": "back", "Alt+U": "fwd", "Alt+H": "history", "Ctrl+R": "refresh", "Ctrl+Q": "quick",
  "Ctrl+B": "branch", "Ctrl+M": "mrename", "Ctrl+Z": "undo", "Alt+T": "newtab", "Alt+W": "closetab", "Ctrl+Shift+P": "palette",
  "Ctrl+F6": "regionNext", "Ctrl+Shift+F6": "regionPrev",
};
function onKey(e) {
  if (S.mode !== "files" || !FM.built || FM.root.hidden) return;
  if ($("#dlg").open) return;                      // dialogs and the viewer own their keys
  const ae = document.activeElement;
  if (menu.style.display === "block" && menu.contains(ae)) { if (menuKey(e)) stop(e); return; }
  const inFiles = !ae || ae === document.body || FM.root.contains(ae) || (ae.closest?.("header") && !ae.matches?.("input, select"));
  const inHeader = ae?.closest?.("header");
  if (!inFiles) return;
  const k = combo(e);
  if (ae?.matches?.("input, textarea, select")) {
    if (k === "Ctrl+F" || k === "Ctrl+S") { stop(e); openFilter(); }
    else if (/^(Shift\+)?F\d+$/.test(k) || k === "Ctrl+R") { stop(e); if (ae.classList.contains("fm-rename")) endRename(); run(GLOBAL_KEYS[k]); }
    return;
  }
  if (inHeader && !GLOBAL_KEYS[k]) return;                // from the header, only the Files commands
  if (!ae || ae === document.body) focusGrid();
  // Escape then a digit within a second stands in for F1–F10
  if (FM.escAt && Date.now() - FM.escAt < 1000 && /^[0-9]$/.test(e.key) && !e.ctrlKey && !e.altKey && !e.metaKey) {
    FM.escAt = 0; stop(e);
    return fkey(e.key === "0" ? 10 : +e.key);
  }
  if (FM.chordAt) {
    const fresh = Date.now() - FM.chordAt < 1000 || !FM.chordEl.hidden;
    FM.chordAt = 0; FM.chordEl.hidden = true;
    if (fresh && !["Control", "Meta", "Shift", "Alt"].includes(e.key)) {
      stop(e);
      const c = e.key.toLowerCase();
      if (c === "p") copyPaths(); else if (c === "c") copyNames();
      return;
    }
  }
  if (k === "Ctrl+X") { stop(e); return startChord(); }
  if (k !== "Escape") FM.escAt = 0;
  const id = GLOBAL_KEYS[k];
  if (id) { stop(e); return run(id); }
  const grid = document.activeElement?.closest?.(".fm-grid");
  if (grid && gridKey(k, e, FM.panes.find(P => P.el.grid === grid))) stop(e);
}
function stop(e) { e.preventDefault(); e.stopImmediatePropagation(); }
function gridKey(k, e, P) {
  if (!P) return false;
  if (P !== AP()) activate(P);
  const t = tabOf(P), n = t.view.length;
  const page = Math.max(1, Math.floor(P.el.scroll.clientHeight / FM.rh) - 1);
  const firstVis = () => clamp(Math.ceil(P.el.scroll.scrollTop / FM.rh), 0, n - 1);
  const lastVis = () => clamp(Math.floor((P.el.scroll.scrollTop + P.el.scroll.clientHeight) / FM.rh) - 1, 0, n - 1);
  const shift = e.shiftKey;
  switch (k.replace(/^Shift\+/, "")) {
    case "ArrowDown": moveCursor(P, t.cur + 1, shift); return true;
    case "ArrowUp": moveCursor(P, t.cur - 1, shift); return true;
    case "PageDown": moveCursor(P, t.cur + page, shift); return true;
    case "PageUp": moveCursor(P, t.cur - page, shift); return true;
    case "Home": moveCursor(P, 0, shift); return true;
    case "End": moveCursor(P, n - 1, shift); return true;
  }
  switch (k) {
    case "Enter": enter(P); return true;
    case "Ctrl+PageDown": if (curRow(t) && isDir(curRow(t))) enter(P); return true;
    case "Ctrl+PageUp": goUp(P); return true;
    case "Backspace":
      if (FM.jump && FM.jumpPane === P) { FM.jump = FM.jump.slice(0, -1); jumpTo(P); } else goUp(P);
      return true;
    case "Alt+G": moveCursor(P, firstVis()); return true;
    case "Alt+R": moveCursor(P, Math.round((firstVis() + lastVis()) / 2)); return true;
    case "Alt+J": moveCursor(P, lastVis()); return true;
    case "Insert": case "Ctrl+T": {
      const r = t.view[t.cur]; if (!r) return true;
      setTag(t, r, !r._t); recount(t);
      t.anchor = t.cur;
      if (t.cur < n - 1) moveCursor(P, t.cur + 1); else renderRows(P);
      renderStatus(P); renderHead(P);
      return true;
    }
    case "Space": space(P); return true;
    case "Ctrl+Space": { const r = curRow(t); if (r && isDir(r)) calcSizes(P, [r]); else announce("Ctrl+Space calculates a folder's size."); return true; }
    case "+": maskDialog(true); return true;
    case "-": maskDialog(false); return true;
    case "*": invertTags(P); return true;
    case "Ctrl+A": tagAll(P); return true;
    case "/": openFilter(P); return true;
    case "Tab": case "Shift+Tab": switchPane(); return true;
    case "Delete": run("trash"); return true;
    case "Shift+Delete": run("delete"); return true;
    case "Escape":
      if (FM.jump) endJump();
      else if (t.filter.text || t.filter.open) clearFilter(P);
      else if (FM.quick) closeQuick();
      FM.escAt = Date.now();
      return true;
  }
  if (e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey && e.key !== " ") { prefixJump(P, e.key); return true; }
  return false;
}
function prefixJump(P, ch) {
  if (FM.jumpPane !== P) FM.jump = "";
  FM.jumpPane = P;
  FM.jump += ch;
  jumpTo(P);
}
function jumpTo(P) {
  clearTimeout(FM.jumpT);
  const t = tabOf(P), q = FM.jump.toLowerCase();
  if (!q) return endJump();
  let i = t.view.findIndex((r, j) => j >= t.cur && baseName(r.n).toLowerCase().startsWith(q));
  if (i < 0) i = t.view.findIndex(r => baseName(r.n).toLowerCase().startsWith(q));
  P.el.jump.classList.toggle("miss", i < 0);
  if (i >= 0) moveCursor(P, i);
  else { FM.jump = FM.jump.slice(0, -1); announce(`No name starts with ${q}`); }
  renderStatus(P);
  FM.jumpT = setTimeout(endJump, 1500);
}
function endJump() {
  clearTimeout(FM.jumpT);
  const P = FM.jumpPane;
  FM.jump = ""; FM.jumpPane = null;
  if (P) { P.el.jump.hidden = true; P.el.jump.classList.remove("miss"); }
}
function startChord() {
  FM.chordAt = Date.now();
  FM.chordEl.textContent = "Ctrl+X, then P copies the full path, or C copies the name. Escape cancels.";
  FM.chordEl.hidden = false;
  clearTimeout(FM.chordT);
  FM.chordT = setTimeout(() => { FM.chordEl.hidden = true; FM.chordAt = 0; }, 1000);
}
function switchPane() {
  const to = OP();
  if (FM.quick) closeQuick();
  activate(to, true);
  announce(`${to.side ? "Right" : "Left"} pane: ${tabOf(to).path}`);
}
function fkey(n) { const id = GLOBAL_KEYS["F" + n]; if (id) run(id); }
function regionCycle(dir) {
  const A = AP(), O = OP();
  const regions = [document.querySelector('.views button[aria-pressed="true"]'), A.el.up.disabled ? A.el.crumbs.querySelector("button") : A.el.up, A.el.grid,
                   O.el.grid, FM.jobsEl.querySelector("button:not([hidden])"), FM.fkeysEl.querySelector("button")].filter(Boolean);
  const i = regions.findIndex(r => r === document.activeElement || r.contains(document.activeElement));
  const nx = regions[(i + dir + regions.length) % regions.length];
  if (nx === O.el.grid) activate(O);
  nx.focus();
}

/* ------------------------------------------------------------------ commands (keys, F-key bar, palette, F9 menu, help) */
const CMD = {};
function def(id, label, keys, fn, o = {}) { CMD[id] = { id, label, keys, fn, ...o }; }
const need = {
  sel: () => { const t = AT(); return t.loading ? "Wait until the folder has loaded." : !t.view.length && !t.ntag ? "There's nothing here to act on." : ""; },
  file: () => { const t = AT(), r = curRow(t); return t.loading ? "Wait until the folder has loaded." : !r ? "There's nothing here to view." : ""; },
  dir: () => { const t = AT(); return t.kind === "find" ? "This tab shows search results. Open a folder first." : t.branch ? "Leave branch view first (Ctrl+B)." : t.error ? "This folder couldn't be read." : t.loading ? "Wait until the folder has loaded." : ""; },
  undo: () => FM.hist.some(h => h.undo) ? "" : "There's nothing to undo.",
};
function run(id, ...args) {
  const c = CMD[id];
  if (!c) return;
  const why = c.why?.();
  if (why) { toast(why); announce(why); return; }
  closeMenu();
  c.fn(...args);
}
function defineCommands() {
  def("help", "Keyboard help", "F1", helpDlg, { group: "General" });
  def("actions", "Actions menu", "F2", actionsMenu, { group: "General", why: need.sel });
  def("view", "View file", "F3", () => openViewer(false), { group: "Files", why: need.file });
  def("hex", "View as hex", "Shift+F3", () => openViewer(true), { group: "Files", why: need.file });
  def("edit", "Edit file", "F4", () => {}, { group: "Files", why: () => "Editing isn't available yet. View the file with F3, or open it with its usual app (Enter)." });
  def("copy", "Copy to the other pane", "F5", () => copyMove("copy"), { group: "Files", why: need.sel, tip: "Copy to the other pane" });
  def("copyas", "Copy with a new name", "Shift+F5", () => copyMove("copy", { rename: true, rows: curRow() ? [curRow()] : [] }), { group: "Files", why: need.file });
  def("move", "Move to the other pane", "F6", () => copyMove("move"), { group: "Files", why: need.sel, tip: "Move to the other pane" });
  def("rename", "Rename", "Shift+F6", () => inlineRename(AP()), { group: "Files", why: need.file });
  def("mkdir", "New folder", "F7", () => newItem("dir"), { group: "Files", why: need.dir });
  def("mkfile", "New file", "Shift+F7", () => newItem("file"), { group: "Files", why: need.dir });
  def("trash", `Move to the ${binName()}`, "F8", () => removeDialog(false), { group: "Files", why: need.sel, tip: `Move to the ${binName()}` });
  def("delete", "Delete for good", "Shift+F8", () => removeDialog(true), { group: "Files", why: need.sel });
  def("menu", "Files menu", "F9", filesMenu, { group: "General" });
  def("close", "Close Files", "F10", closeFiles, { group: "General", tip: "Return to the previous view" });
  def("context", "Item menu", "Shift+F10", () => { const P = AP(), t = tabOf(P); const rows = sel(t); if (rows.length) menuAtCursor(P, (x, y) => contextMenu(P, rows, x, y)); }, { group: "General", why: need.sel });
  def("leftloc", "Choose the left pane's location", "Alt+F1", () => locationMenu(FM.panes[0]), { group: "Navigate" });
  def("rightloc", "Choose the right pane's location", "Alt+F2", () => locationMenu(FM.panes[1]), { group: "Navigate" });
  def("find", "Find files", "Alt+F7", findDlg, { group: "Files" });
  def("filter", "Quick filter", "Ctrl+S", () => openFilter(), { group: "Select" });
  def("goto", "Go to path", "Alt+C", () => editPath(AP()), { group: "Navigate" });
  def("hotlist", "Saved locations", "Ctrl+\\", hotlistDlg, { group: "Navigate" });
  def("savehere", "Save this location", "", () => toggleHot(AP(), true), { group: "Navigate", why: () => AT().kind === "find" ? "Search results can't be saved as a location." : "" });
  def("swap", "Swap panes", "Ctrl+U", swapPanes, { group: "Navigate" });
  def("otherCursor", "Open the cursor folder in the other pane", "Alt+O", () => {
    const t = AT(), r = curRow(t);
    go(OP(), r && isDir(r) ? rowPath(t, r) : t.path);
  }, { group: "Navigate" });
  def("otherHere", "Show this folder in the other pane", "Alt+I", () => go(OP(), AT().path), { group: "Navigate", why: () => AT().kind === "find" ? "This tab shows search results." : "" });
  def("back", "Back", "Alt+Left", () => histMove(AP(), -1), { group: "Navigate" });
  def("fwd", "Forward", "Alt+Right", () => histMove(AP(), 1), { group: "Navigate" });
  def("history", "Folder history", "Alt+H", () => histMenu(AP()), { group: "Navigate" });
  def("refresh", "Refresh", "Ctrl+R", () => { refresh(AP()); announce("Refreshed."); }, { group: "Navigate", tip: "Refresh this folder" });
  def("quick", "Quick view", "Ctrl+Q", toggleQuick, { group: "Files" });
  def("branch", "Branch view: all files below", "Ctrl+B", toggleBranch, { group: "Files", why: () => AT().kind === "find" ? "Search results can't be shown as a branch." : "" });
  def("mrename", "Rename several items", "Ctrl+M", () => multiRename(), { group: "Files", why: need.sel });
  def("undo", "Undo the last operation", "Ctrl+Z", undoLatest, { group: "Files", why: need.undo });
  def("newtab", "New tab", "Alt+T", () => addTab(AP()), { group: "Navigate" });
  def("closetab", "Close tab", "Alt+W", () => closeTab(AP()), { group: "Navigate" });
  def("palette", "Command palette", "Ctrl+Shift+P", palette, { group: "General" });
  def("jobs", "Jobs and history", "", jobsDlg, { group: "General" });
  def("copypath", "Copy full path", "Ctrl+X P", () => copyPaths(), { group: "Files", why: need.sel });
  def("copyname", "Copy name", "Ctrl+X C", () => copyNames(), { group: "Files", why: need.sel });
  def("open", "Open", "Enter", () => enter(AP()), { group: "Files", why: need.file });
  def("opendefault", "Open with default app", "", () => openDefault(rowPath(AT(), curRow())), { group: "Files", why: need.file });
  def("reveal", `Show in ${S.revealName || "the file manager"}`, "", () => reveal(rowPath(AT(), curRow())), { group: "Files", why: need.file });
  def("stage", "Mark for removal", "", () => stageRows(AP(), sel(AT())), { group: "Files", why: need.sel });
  def("selsize", "Tag and show the selection size", "Space", () => space(AP()), { group: "Select", why: need.file });
  def("calc", "Calculate folder size", "Ctrl+Space", () => { const r = curRow(); if (r && isDir(r)) calcSizes(AP(), [r]); }, { group: "Select", why: () => { const r = curRow(); return r && isDir(r) ? "" : "Put the cursor on a folder first."; } });
  def("selmask", "Select matching items", "+", () => maskDialog(true), { group: "Select" });
  def("deselmask", "Deselect matching items", "−", () => maskDialog(false), { group: "Select" });
  def("invert", "Invert tags", "*", () => invertTags(AP()), { group: "Select" });
  def("tagall", "Tag all shown items", "Ctrl+A", () => tagAll(AP()), { group: "Select" });
  def("full", "Full listing", "", () => setListing(AP(), "full"), { group: "Listing" });
  def("brief", "Brief listing", "", () => setListing(AP(), "brief"), { group: "Listing" });
  def("biggest", "Biggest first", "", () => setSort(AP(), "size", !AT().biggest), { group: "Listing" });
  def("hidden", "Show hidden files", "", toggleHidden, { group: "Listing" });
  def("density", "Comfortable rows", "", toggleDensity, { group: "Listing" });
  def("sortname", "Sort by name", "", () => setSort(AP(), "name"), { group: "Listing" });
  def("sortsize", "Sort by size", "", () => setSort(AP(), "size"), { group: "Listing" });
  def("sortmod", "Sort by modified date", "", () => setSort(AP(), "mtime"), { group: "Listing" });
  def("sorttype", "Sort by type", "", () => setSort(AP(), "ext"), { group: "Listing" });
  def("regionNext", "Next region", "Ctrl+F6", () => regionCycle(1), { group: "General" });
  def("regionPrev", "Previous region", "Ctrl+Shift+F6", () => regionCycle(-1), { group: "General" });
}
function renderFkeys() {
  const F = [[1, "Help", "help"], [2, "Actions", "actions"], [3, "View", "view"], [4, "Edit", "edit"], [5, "Copy", "copy"],
             [6, "Move", "move"], [7, "Folder", "mkdir"], [8, "Trash", "trash"], [9, "Menu", "menu"], [10, "Close", "close"]];
  FM.fkeysEl.innerHTML = F.map(([n, l, id]) => `<button data-cmd="${id}" title="${esc(id === "edit" ? CMD.edit.why() : (CMD[id]?.tip || CMD[id]?.label || l) + ` (F${n})`)}"
      ${id === "edit" ? 'aria-disabled="true"' : ""} aria-label="${esc(`F${n} ${CMD[id]?.label || l}`)}"><kbd>F${n}</kbd><span>${l}</span></button>`).join("");
  FM.fkeysEl.addEventListener("mousedown", e => { if (e.target.closest("button")) e.preventDefault(); });   // keep focus on the grid
  FM.fkeysEl.onclick = ev => { const b = ev.target.closest("button"); if (b) run(b.dataset.cmd); };
}
function actionsMenu() {
  const P = AP(), t = tabOf(P), rows = sel(t), one = rows.length === 1 ? rows[0] : null;
  menuAtCursor(P, (x, y) => fmMenu(x, y, [
    { header: "Actions" },
    one && !isDir(one) ? { label: "View", keys: "F3", icon: ICON.eye, run: () => openViewer(false) } : null,
    one ? { label: isDir(one) ? "Open" : "Open with default app", keys: "Enter", icon: ICON.launch, run: () => isDir(one) ? enter(P, one) : openDefault(rowPath(t, one)) } : null,
    one ? { label: `Show in ${S.revealName}`, icon: ICON.open, run: () => reveal(rowPath(t, one)) } : null,
    { label: one ? "Copy full path" : "Copy full paths", keys: "Ctrl+X P", icon: ICON.copy, run: () => copyPaths(rows, P) },
    { label: one ? "Copy name" : "Copy names", keys: "Ctrl+X C", run: () => copyNames(rows) },
    rows.some(isDir) ? { label: "Calculate folder size", keys: "Ctrl+Space", icon: I.size, run: () => calcSizes(P, rows.filter(isDir)) } : null,
    { label: "Show selection size", keys: "Space", run: () => calcSelection(P, true) },
    { label: "Mark for removal", icon: ICON.stage, run: () => stageRows(P, rows) },
    "-",
    { label: "Select matching items…", keys: "+", run: () => maskDialog(true) },
    { label: "Find files…", keys: "Alt+F7", icon: ICON.zoom, run: findDlg },
    { label: "Quick view", keys: "Ctrl+Q", checked: !!FM.quick, run: toggleQuick },
  ]));
}
function filesMenu() {
  const t = AT(), b = FM.fkeysEl.querySelector('[data-cmd="menu"]').getBoundingClientRect();
  fmMenu(b.left, b.top - 8, [
    { header: "Locations" },
    { label: "Left pane location…", keys: "Alt+F1", run: () => run("leftloc") },
    { label: "Right pane location…", keys: "Alt+F2", run: () => run("rightloc") },
    { label: "Saved locations…", keys: "Ctrl+\\", run: () => run("hotlist") },
    { label: "Go to path…", keys: "Alt+C", run: () => run("goto") },
    { label: "Swap panes", keys: "Ctrl+U", run: () => run("swap") },
    "-",
    { header: "Listing" },
    { label: "Full listing", checked: t.listing === "full", run: () => run("full") },
    { label: "Brief listing", checked: t.listing === "brief", run: () => run("brief") },
    { label: "Biggest first", checked: t.biggest, run: () => run("biggest") },
    { label: "Show hidden files", checked: FM.hidden, run: () => run("hidden") },
    { label: "Comfortable rows", checked: FM.density === "comfortable", run: () => run("density") },
    "-",
    { header: "Commands" },
    { label: "Find files…", keys: "Alt+F7", run: () => run("find") },
    { label: "Branch view", keys: "Ctrl+B", checked: t.branch, run: () => run("branch") },
    { label: "Rename several items…", keys: "Ctrl+M", run: () => run("mrename") },
    { label: "Quick view", keys: "Ctrl+Q", checked: !!FM.quick, run: () => run("quick") },
    { label: "Undo", keys: "Ctrl+Z", disabled: need.undo() || false, run: () => run("undo") },
    { label: `Jobs (${FM.jobs.filter(j => ACTIVE.has(j.state)).length})`, run: () => run("jobs") },
    "-",
    { label: "Command palette…", keys: "Ctrl+Shift+P", run: () => run("palette") },
    { label: "Keyboard help", keys: "F1", run: () => run("help") },
  ]);
}
function closeFiles() {
  const back = S.fmPrev && S.fmPrev !== "files" && S.fmPrev !== "simple" ? S.fmPrev : "map";
  setMode(back);
}
function swapPanes() {
  if (FM.quick) closeQuick();
  const [a, b] = FM.panes;
  [a.tabs, b.tabs] = [b.tabs, a.tabs];
  [a.ti, b.ti] = [b.ti, a.ti];
  [a.hist, b.hist] = [b.hist, a.hist];
  for (const P of FM.panes) { for (const [, el] of P.pool) { el.remove(); P.free.push(el); } P.pool.clear(); renderPane(P); P.el.scroll.scrollTop = tabOf(P).scrollTop || 0; ensureVisible(P); renderRows(P, true); }
  savePrefs();
  focusGrid();
  announce("Swapped the panes.");
}
function toggleBranch() {
  const P = AP(), t = tabOf(P);
  if (t.kind === "find") return;
  go(P, t.path, { branch: !t.branch }).then(ok => { if (ok) announce(t.branch ? `Branch view: ${items(t.rows.length)}${t.truncated ? ", first ones only" : ""}.` : "Branch view off."); });
}

/* ------------------------------------------------------------------ dialogs (the shared #dlg) */
function dlg(cls, html, { onClose } = {}) {
  const d = $("#dlg");
  closeMenu();
  d.className = "fm-dlg " + (cls || "");
  d.removeAttribute("role");
  d.onclick = null;
  d.innerHTML = html;
  d.onclose = () => {
    if (d.open) return;                        // a late close event from the dialog this one replaced
    d.onclose = null; d.onkeydown = null; d.onclick = null; d.className = ""; d.removeAttribute("role");
    try { onClose?.(); } finally { if (S.mode === "files") requestAnimationFrame(() => { if (!$("#dlg").open) focusGrid(); }); }
  };
  d.onkeydown = e => {
    if (e.key === "Enter" && !e.shiftKey && !e.ctrlKey && !e.altKey && e.target.matches?.("input:not([type=checkbox]):not([type=radio])")) {
      const b = d.querySelector("[data-primary]");
      e.preventDefault();
      if (b && !b.disabled) b.click();
    }
  };
  d.querySelectorAll("[data-cancel]").forEach(b => b.onclick = () => d.close());
  d.showModal();
  if (!d.contains(document.activeElement)) d.querySelector("[autofocus], input:not([type=checkbox]), [data-primary], button")?.focus();
  return d;
}
function listHTML(arr, fn, max = 200) {
  const more = arr.length > max ? `<li><span class="why">…and ${items(arr.length - max)} more</span></li>` : "";
  return `<ul class="fm-ul">${arr.slice(0, max).map(fn).join("")}${more}</ul>`;
}
const blockedHTML = (blocked, allowed, verb) => !blocked.length ? "" : `<div class="fm-blocked">
    <b>${I.lock.replace("<svg", '<svg width="14" height="14"')} ${blocked.length === 1 ? "1 item can't be" : `${blocked.length.toLocaleString()} items can't be`} ${verb}</b>
    ${listHTML(blocked, b => `<li><span class="p">${esc(b.path)}</span><span class="why">${esc(b.reason)}</span></li>`)}
    ${allowed ? `<label class="fm-cl"><input type="checkbox" id="fmCont"> Continue with ${items(allowed)} allowed</label>` : `<span>Nothing in this selection can be ${verb}.</span>`}</div>`;

/* ------------------------------------------------------------------ simple actions */
async function openDefault(p) {
  try {
    const l = await api("locate", { abs: p });
    if (l.error || !l.drive) return toast(l.error || "Hoard can't open this item with another app.", "error");
    const r = await api("open", { drive: l.drive, path: l.parts });
    if (r.error) toast(r.error, "error"); else announce(`Opened ${baseName(p)} with its usual app.`);
  } catch (e) { toast(e.message, "error"); }
}
async function reveal(p) {
  try { const r = await api("reveal", { abs: p }); if (r.error) toast(r.error, "error"); } catch (e) { toast(e.message, "error"); }
}
async function enter(P, r) {
  const t = tabOf(P);
  r ||= t.view[t.cur];
  if (!r || t.loading) return;
  const p = rowPath(t, r);
  if (isDir(r)) {
    if (t.kind === "find" || t.branch) return addTab(P, p);
    return go(P, p);
  }
  if (r.k === "l") {      // a link to a folder opens like a folder; a link to a file opens with its app
    let res;
    try { res = await api("fm_list", { path: p, hidden: FM.hidden, sort: "name", desc: false, foldersFirst: true, branch: false }); } catch (e) { res = { error: e.message }; }
    if (!res.error && res.rows) return t.kind === "dir" && !t.branch ? go(P, p) : addTab(P, p);
  }
  openDefault(p);
}
async function copyPaths(rows, P = AP()) {
  const t = tabOf(P);
  rows ||= sel(t);
  if (!rows.length) return;
  const ok = await copyText(rows.map(r => rowPath(t, r)).join("\n"));
  toast(ok ? (rows.length === 1 ? "Copied the full path" : `Copied ${rows.length} paths`) : "Couldn't copy", ok ? "ok" : "error");
}
async function copyNames(rows) {
  const t = AT();
  rows ||= sel(t);
  if (!rows.length) return;
  const ok = await copyText(rows.map(r => baseName(r.n)).join("\n"));
  toast(ok ? (rows.length === 1 ? "Copied the name" : `Copied ${rows.length} names`) : "Couldn't copy", ok ? "ok" : "error");
}
function stageRows(P, rows) {
  const t = tabOf(P);
  const ok = rows.filter(r => !r.p), prot = rows.length - ok.length;
  if (prot) toast(prot === 1 ? "1 protected item wasn't added" : `${prot} protected items weren't added`, "error", rows.find(r => r.p).p);
  if (!ok.length) return;
  stage(null, ok.map(r => ({ path: rowPath(t, r), name: baseName(r.n), size: r.s ?? 0, kind: isDir(r) ? "d" : "f", c: r.c ?? 8 })));
}

/* ------------------------------------------------------------------ selection size and folder sizes */
function space(P) {
  const t = tabOf(P), r = t.view[t.cur];
  if (!r) return;
  setTag(t, r, !r._t);
  t.anchor = t.cur;
  refreshTags(P);
  calcSelection(P, true);
}
function calcSelection(P, speak) {
  const t = tabOf(P), rows = t.rows.filter(r => r._t);
  const unk = rows.filter(r => r.s == null && isDir(r) && !r._calc);
  if (unk.length) return calcSizes(P, unk);
  if (speak) announce(rows.length ? `${items(rows.length)} selected, ${fmt(t.tagBytes)}${rows.some(r => r._calc) ? ", still calculating" : ""}.` : "Nothing selected.");
}
async function calcSizes(P, rows) {
  const t = tabOf(P);
  rows = rows.filter(r => !r._calc);
  if (!rows.length) return;
  const paths = rows.map(r => rowPath(t, r));
  rows.forEach(r => { r._calc = true; });
  renderRows(P, true); renderStatus(P);
  announce(`Calculating ${plural(paths.length, "folder size")}…`);
  try {
    const r = await api("fm_sizes", { paths });
    if (r.error) throw new Error(r.error);
    FM.pending.set(r.job, { op: "sizes", paths });
    pollJobs();
  } catch (e) {
    rows.forEach(x => { x._calc = false; });
    renderRows(P, true); renderStatus(P);
    toast(e.message, "error");
  }
}
function applySizes(sizes, paths, quiet) {
  const m = new Map(Object.entries(sizes || {}).map(([k, v]) => [norm(k), v]));
  const asked = new Set((paths || []).map(norm));
  for (const P of FM.panes) {
    let touched = false;
    for (const t of P.tabs) {
      let hit = false;
      for (const r of t.rows) {
        const k = norm(rowPath(t, r));
        if (m.has(k)) { r.s = m.get(k); r._sz = "calc"; r._calc = false; hit = true; }
        else if (r._calc && asked.has(k)) { r._calc = false; hit = true; }
      }
      if (hit) { resort(t); if (tabOf(P) === t) touched = true; }
    }
    if (touched) { renderRows(P, true); ensureVisible(P); renderRows(P); renderStatus(P); }
  }
  if (quiet) return;
  const t = AT();
  if (t.ntag) announce(`${items(t.ntag)} selected, ${fmt(t.tagBytes)}${t.tagUnknown ? `, ${plural(t.tagUnknown, "folder")} still unknown` : ""}.`);
  else announce("Folder sizes calculated.");
}
function stopSizes() {
  for (const j of FM.jobs) if (j.op === "sizes" && ACTIVE.has(j.state)) api("fm_control", { job: j.id, action: "cancel" }).catch(() => {});
  for (const P of FM.panes) for (const t of P.tabs) for (const r of t.rows) r._calc = false;
  FM.panes.forEach(P => { renderRows(P, true); renderStatus(P); });
}

/* ------------------------------------------------------------------ masks */
function maskDialog(select) {
  const P = AP(), t = tabOf(P);
  const verb = select ? "Select" : "Deselect";
  const d = dlg("", `<h3>${verb} matching items</h3>
    <label class="fm-field"><span>Pattern</span><input type="text" id="fmMask" value="${esc(FM.lastMask || "*")}" spellcheck="false" autocomplete="off"><small>Wildcards, separated by semicolons: *.jpg;*.png</small></label>
    <div class="fm-checks"><label><input type="checkbox" id="fmMRe"> Regex</label><label><input type="checkbox" id="fmMCase"> Match case</label><label><input type="checkbox" id="fmMDir"> Include folders</label></div>
    <p class="form-error" id="fmMErr" hidden></p>
    <div class="row-actions"><button class="btn" data-cancel>Cancel</button><button class="btn primary" data-primary>${verb}</button></div>`);
  const inp = $("#fmMask"); inp.select();
  d.querySelector("[data-primary]").onclick = () => {
    const pat = inp.value.trim(), re = $("#fmMRe").checked, cs = $("#fmMCase").checked, dirs = $("#fmMDir").checked;
    let m;
    try { m = matcher(pat, re ? "regex" : "pattern", cs); } catch { m = null; }
    if (!m) { const er = $("#fmMErr"); er.textContent = re ? "That regex isn't valid." : "Type a pattern, like *.jpg."; er.hidden = false; inp.setAttribute("aria-invalid", "true"); return; }
    FM.lastMask = pat;
    let n = 0;
    for (const r of t.view) if ((dirs || !isDir(r)) && m(baseName(r.n)) && !!r._t !== select) { setTag(t, r, select); n++; }
    d.close();
    refreshTags(P);
    announce(`${select ? "Tagged" : "Untagged"} ${items(n)}. ${items(t.ntag)} selected, ${fmt(t.tagBytes)}.`);
  };
}

/* ------------------------------------------------------------------ copy and move */
function copyMove(op, o = {}) {
  const P = o.P || AP(), t = tabOf(P);
  if (t.loading) return;
  const rows = sel(t, o.rows);
  if (!rows.length) return announce("Nothing to " + op + ".");
  const srcs = rows.map(r => rowPath(t, r)), n = rows.length, one = n === 1 ? rows[0] : null;
  const other = tabOf(P === FM.panes[0] ? FM.panes[1] : FM.panes[0]);
  const dest = o.dest || (other.kind === "dir" && !other.error ? other.path : "");
  const Verb = op === "copy" ? "Copy" : "Move", verbed = op === "copy" ? "copied" : "moved";
  const busy = FM.jobs.some(j => ACTIVE.has(j.state) && MUT(j.op));
  const d = dlg("fm-op", `<h3>${Verb} ${items(n)}</h3>
    <div class="dpath">${esc(one ? srcs[0] : `${items(n)} from ${t.path}`)}</div>
    <label class="fm-field"><span>To</span><input type="text" id="fmTo" class="mono" value="${esc(dest)}" spellcheck="false" autocomplete="off"></label>
    <label class="fm-field"><span>New name / pattern</span><input type="text" id="fmPat" value="${esc(o.rename && one ? baseName(one.n) : "*")}" spellcheck="false" autocomplete="off">
      <small>* keeps each name. Text around one * is added to each name, like copy of *.</small></label>
    <div class="fm-checks"><label><input type="checkbox" id="fmKeepT" checked> Preserve modified times</label></div>
    <p class="fm-note">Links are ${verbed} as links; Hoard never follows them.</p>
    <div class="fm-plan" id="fmPlan" aria-live="polite"><div class="loading">${SPIN}Checking…</div></div>
    <p class="form-error" id="fmErr" hidden></p>
    <div class="row-actions"><button class="btn" data-cancel>Cancel</button><button class="btn primary" data-primary disabled>${busy ? "Add to queue" : Verb + " now"}</button></div>`);
  const to = $("#fmTo"), pat = $("#fmPat"), btn = d.querySelector("[data-primary]"), planEl = $("#fmPlan"), errEl = $("#fmErr");
  if (o.rename && one) { pat.focus(); const j = baseName(one.n).lastIndexOf("."); pat.setSelectionRange(0, isDir(one) || j <= 0 ? pat.value.length : j); }
  else to.focus();
  let plan = null, tok = 0, timer = 0, at = 0;
  const bad = msg => { plan = null; planEl.innerHTML = ""; errEl.textContent = msg; errEl.hidden = false; btn.disabled = true; };
  const ready = () => { btn.disabled = !plan || !plan.items.length || (plan.blocked.length > 0 && !$("#fmCont")?.checked); };
  async function doPlan() {
    const my = ++tok, dst = to.value.trim(), p = pat.value.trim() || "*", stars = (p.match(/\*/g) || []).length;
    errEl.hidden = true; plan = null; btn.disabled = true;
    if (!dst) return bad(`Choose where to ${op} to.`);
    if (stars > 1) return bad("Use at most one * in the pattern.");
    if (!stars && n > 1) return bad(`To ${op} several items, include * to stand for each name.`);
    if (/[\\/]/.test(p) || (FM.ci && /[:?"<>|]/.test(p))) return bad("A name can't contain \\ / : ? \" < > or |.");
    planEl.innerHTML = `<div class="loading">${SPIN}Checking…</div>`;
    const body = { op, sources: srcs, dest: dst, options: { preserveTimes: $("#fmKeepT").checked } };
    if (!stars) body.name = p; else if (p !== "*") body.pattern = p;
    let r;
    try { r = await api("fm_plan", body); } catch (e) { r = { error: e.message }; }
    if (my !== tok || !d.open) return;
    if (r.error) return bad(r.error);
    plan = r; at = Date.now();
    plan.items ||= []; plan.blocked ||= []; plan.conflicts ||= [];
    const tot = plan.total || { n: plan.items.length, bytes: 0 };
    planEl.innerHTML = (!plan.items.length ? "" : `<details><summary>Will ${op} ${items(plan.items.length)}${tot.bytes ? ` · ${fmt(tot.bytes)}` : ""}${plan.undoable === false ? " · can't be undone" : ""}</summary>
        ${listHTML(plan.items, it => `<li><span class="p">${esc(baseName(it.src))} → ${esc(it.dst || "")}</span>${it.size != null ? `<span class="z">${fmt(it.size)}</span>` : ""}</li>`)}</details>`)
      + `
      ${plan.conflicts.length ? `<div class="fm-conf"><b>${plan.conflicts.length === 1 ? "1 item already exists" : `${plan.conflicts.length.toLocaleString()} items already exist`} in the destination</b>
        ${listHTML(plan.conflicts, c => `<li><span class="p">${esc(c.dst)}</span><span class="z">${c.srcSize != null ? fmt(c.srcSize) : "—"} → replaces ${c.dstSize != null ? fmt(c.dstSize) : "—"}</span></li>`, 50)}
        <label class="fm-field" style="margin:0"><span>When a name is taken</span><select id="fmConf">
          <option value="ask">Ask each time</option><option value="replace">Replace all</option><option value="skip">Skip all</option><option value="keep">Keep both</option>
          <option value="newer">Replace if the source is newer</option><option value="larger">Replace if the source is larger</option><option value="differs">Replace if the size differs</option></select></label></div>` : ""}
      ${blockedHTML(plan.blocked, plan.items.length, verbed)}`;
    const cont = $("#fmCont");
    if (cont) cont.onchange = ready;
    ready();
  }
  const replan = () => { clearTimeout(timer); btn.disabled = true; timer = setTimeout(doPlan, 300); };
  to.oninput = replan; pat.oninput = replan; $("#fmKeepT").onchange = replan;
  doPlan();
  btn.onclick = async () => {
    if (!plan) return;
    if (Date.now() - at > 9 * 60000) { await doPlan(); if (!plan) return; ready(); if (btn.disabled) return; }
    btn.disabled = true;
    const conflict = $("#fmConf")?.value || "ask", count = plan.items.length;
    let r;
    try { r = await api("fm_start", { plan: plan.plan, exclude: plan.blocked.map(b => b.path), conflict, confirm: false }); } catch (e) { r = { error: e.message }; }
    if (r.error) { errEl.textContent = r.error; errEl.hidden = false; btn.disabled = false; plan = null; replan(); return; }
    FM.pending.set(r.job, { op, sources: srcs, dest: to.value.trim(), P, t });
    d.close();
    announce(`${op === "copy" ? "Copying" : "Moving"} ${items(count)}.`);
    pollJobs();
  };
}

/* ------------------------------------------------------------------ trash and delete */
function removeDialog(permanent, rowsIn, PIn) {
  const P = PIn || AP(), t = tabOf(P);
  if (t.loading) return;
  const rows = sel(t, rowsIn);
  if (!rows.length) return;
  const srcs = rows.map(r => rowPath(t, r)), n = rows.length, bin = binName();
  const name = n === 1 ? `“${baseName(rows[0].n)}”` : items(n);
  const known = rows.reduce((a, r) => a + (r.s || 0), 0), unknown = rows.some(r => r.s == null);
  const d = dlg("fm-op", `<h3>${permanent ? `Delete ${name} for good?` : `Move ${name} to the ${esc(bin)}?`}</h3>
    <p>${permanent ? `This bypasses the ${esc(bin)} and cannot be undone.` : "You can restore them until the bin is emptied. Disk space is freed when you empty it."}</p>
    ${listHTML(rows, r => `<li><span class="p">${esc(rowPath(t, r))}</span><span class="z">${r.s != null ? fmt(r.s) : "—"}</span></li>`, 8)}
    <p class="fm-note" style="margin-top:8px">${known ? `${fmt(known)}${unknown ? " plus folders not sized yet" : ""}` : unknown ? "Folder sizes not known yet" : ""}</p>
    <div class="fm-plan" id="fmPlan" aria-live="polite"><div class="loading">${SPIN}Checking…</div></div>
    ${permanent ? `<label class="fm-cl" style="margin-top:12px"><input type="checkbox" id="fmSure"> I understand these items cannot be restored</label>` : ""}
    <p class="form-error" id="fmErr" hidden></p>
    <div class="row-actions"><button class="btn" data-cancel autofocus>Cancel</button>
      <button class="btn ${permanent ? "danger solid" : "primary"}" data-primary disabled>${permanent ? "Delete for good" : `Move to ${esc(bin)}`}</button></div>`);
  const btn = d.querySelector("[data-primary]"), planEl = $("#fmPlan"), errEl = $("#fmErr");
  d.querySelector("[data-cancel]").focus();
  let plan = null;
  const ready = () => { btn.disabled = !plan || !plan.items.length || (plan.blocked.length > 0 && !$("#fmCont")?.checked) || (permanent && !$("#fmSure").checked); };
  if (permanent) $("#fmSure").onchange = ready;
  (async () => {
    let r;
    try { r = await api("fm_plan", { op: permanent ? "delete" : "trash", sources: srcs }); } catch (e) { r = { error: e.message }; }
    if (!d.open) return;
    if (r.error) { planEl.innerHTML = ""; errEl.textContent = r.error; errEl.hidden = false; return; }
    plan = r; plan.items ||= []; plan.blocked ||= [];
    planEl.innerHTML = blockedHTML(plan.blocked, plan.items.length, permanent ? "deleted" : "moved to the " + bin)
      + (permanent ? "" : plan.undoable === false ? `<p class="fm-note">Hoard can't undo this one; restore from the ${esc(bin)} instead.</p>` : "");
    const cont = $("#fmCont");
    if (cont) cont.onchange = ready;
    ready();
  })();
  btn.onclick = async () => {
    if (!plan) return;
    btn.disabled = true;
    let r;
    try { r = await api("fm_start", { plan: plan.plan, exclude: plan.blocked.map(b => b.path), conflict: "ask", confirm: permanent }); } catch (e) { r = { error: e.message }; }
    if (r.error) { errEl.textContent = r.error; errEl.hidden = false; return; }
    FM.pending.set(r.job, { op: permanent ? "delete" : "trash", sources: srcs, P, t, next: nextName(t, rows) });
    d.close();
    announce(permanent ? `Deleting ${items(plan.items.length)}.` : `Moving ${items(plan.items.length)} to the ${bin}.`);
    pollJobs();
  };
}
/* where the cursor lands after the rows go away: the next row that stays */
function nextName(t, rows) {
  const gone = new Set(rows);
  for (let i = t.cur; i < t.view.length; i++) if (!gone.has(t.view[i])) return t.view[i].n;
  for (let i = t.cur; i >= 0; i--) if (!gone.has(t.view[i])) return t.view[i].n;
  return null;
}

/* ------------------------------------------------------------------ rename in place, new items */
function inlineRename(P) {
  const t = tabOf(P), r = t.view[t.cur];
  if (!r || t.loading) return;
  if (r.p) return toast(r.p, "error");
  endRename();
  ensureVisible(P); renderRows(P);
  const old = baseName(r.n), inp = ce("input", "fm-rename");
  inp.type = "text"; inp.value = old; inp.spellcheck = false;
  inp.setAttribute("aria-label", `New name for ${old}. Enter renames, Escape cancels.`);
  const nameCell = P.pool.get(t.cur)?.querySelector(".c-name");
  const sc = P.el.scroll.getBoundingClientRect(), nc = nameCell?.getBoundingClientRect();
  inp.style.top = (t.cur * FM.rh + 2) + "px";
  inp.style.left = (nc ? nc.left - sc.left + 22 : 60) + "px";
  inp.style.width = Math.max(160, (nc ? nc.width - 24 : 240)) + "px";
  P.el.scroll.appendChild(inp);
  FM.renameEl = inp;
  inp.focus();
  const dot = old.lastIndexOf(".");
  inp.setSelectionRange(0, isDir(r) || dot <= 0 ? old.length : dot);
  let busy = false;
  inp.addEventListener("keydown", async e => {
    e.stopPropagation();
    if (e.key === "Escape") { e.preventDefault(); endRename(); focusGrid(P); }
    else if (e.key === "Enter") {
      e.preventDefault();
      const nn = inp.value.trim();
      if (!nn || nn === old) { endRename(); return focusGrid(P); }
      if (/[\\/]/.test(nn) || (FM.ci && /[:*?"<>|]/.test(nn))) return toast("A name can't contain \\ / : * ? \" < > or |.", "error");
      busy = true; inp.disabled = true;
      let pl;
      try { pl = await api("fm_plan", { op: "rename", sources: [rowPath(t, r)], name: nn }); } catch (er) { pl = { error: er.message }; }
      const fail = msg => { toast(msg, "error"); busy = false; inp.disabled = false; inp.focus(); };
      if (pl.error) return fail(pl.error);
      if (pl.blocked?.length) return fail(pl.blocked[0].reason);
      if (pl.conflicts?.length) return fail(`“${nn}” already exists here.`);
      let st;
      try { st = await api("fm_start", { plan: pl.plan, exclude: [], conflict: "skip", confirm: false }); } catch (er) { st = { error: er.message }; }
      if (st.error) return fail(st.error);
      FM.pending.set(st.job, { op: "rename", sources: [rowPath(t, r)], P, t, focus: t.kind === "dir" && !t.branch ? nn : null });
      endRename(); focusGrid(P);
      pollJobs();
    }
  });
  inp.addEventListener("blur", () => { if (!busy) setTimeout(endRename, 0); });
}
function endRename() { const r = FM.renameEl; FM.renameEl = null; if (r?.isConnected) r.remove(); }
function newItem(kind) {
  const P = AP(), t = tabOf(P), what = kind === "dir" ? "folder" : "file";
  const d = dlg("", `<h3>New ${what}</h3><div class="dpath">${esc(t.path)}</div>
    <label class="fm-field"><span>Name</span><input type="text" id="fmNew" value="${kind === "dir" ? "New folder" : "New file.txt"}" spellcheck="false" autocomplete="off"></label>
    <p class="form-error" id="fmErr" hidden></p>
    <div class="row-actions"><button class="btn" data-cancel>Cancel</button><button class="btn primary" data-primary>Create</button></div>`);
  const inp = $("#fmNew"), btn = d.querySelector("[data-primary]"), errEl = $("#fmErr");
  inp.focus();
  const dot = inp.value.lastIndexOf(".");
  inp.setSelectionRange(0, kind === "file" && dot > 0 ? dot : inp.value.length);
  const fail = m => { errEl.textContent = m; errEl.hidden = false; btn.disabled = false; inp.setAttribute("aria-invalid", "true"); inp.focus(); };
  btn.onclick = async () => {
    const nm = inp.value.trim();
    if (!nm) return fail("Type a name.");
    if (/[\\/]/.test(nm) || (FM.ci && /[:*?"<>|]/.test(nm))) return fail("A name can't contain \\ / : * ? \" < > or |.");
    btn.disabled = true;
    let pl;
    try { pl = await api("fm_plan", { op: kind === "dir" ? "mkdir" : "mkfile", sources: [], dest: t.path, name: nm }); } catch (e) { pl = { error: e.message }; }
    if (pl.error) return fail(pl.error);
    if (pl.blocked?.length) return fail(pl.blocked[0].reason);
    if (pl.conflicts?.length) return fail(`Something called “${nm}” is already here.`);
    let st;
    try { st = await api("fm_start", { plan: pl.plan, exclude: [], conflict: "skip", confirm: false }); } catch (e) { st = { error: e.message }; }
    if (st.error) return fail(st.error);
    FM.pending.set(st.job, { op: kind === "dir" ? "mkdir" : "mkfile", P, t, focus: nm });
    d.close();
    pollJobs();
  };
}

/* ------------------------------------------------------------------ jobs */
async function pollJobs() {
  clearTimeout(FM.pollT);
  let r = null;
  try { r = await api("fm_jobs"); } catch { /* try again later */ }
  if (r && !r.error) {
    FM.jobs = r.jobs || [];
    FM.hist = (r.history || []).slice().sort((a, b) => (b.time || 0) - (a.time || 0));
    for (const j of FM.jobs) track(j);
    // what startup recovery did after an interrupted job (shown once per window)
    FM.noticed = FM.noticed || new Set();
    for (const n of r.notices || []) if (!FM.noticed.has(n)) { FM.noticed.add(n); toast(n, "ok", "See Jobs → History for details."); }
    for (const [id, m] of FM.pending) if (!FM.jobs.some(j => j.id === id) && (m.seenAt || (m.seenAt = Date.now())) && Date.now() - m.seenAt > 3000) {
      FM.pending.delete(id);                       // finished and already gone from the list
      finish({ id, op: m.op, state: "done", done: {}, total: {}, errors: [] }, m);
    }
    renderJobs();
    maybeAsk();
    FM.jobsHook?.();
    FM.findHook?.();
  }
  const busy = FM.jobs.some(j => ACTIVE.has(j.state)) || FM.pending.size > 0;
  if (busy || S.mode === "files") FM.pollT = setTimeout(pollJobs, busy ? 450 : 5000);
}
function track(j) {
  const m = FM.pending.get(j.id);
  if (ACTIVE.has(j.state)) {
    if (m) m.live = true;
    if (!FM.seen.has(j.id)) FM.seen.set(j.id, j.state);
    if (m?.op === "sizes" && j.result?.sizes) { const n = Object.keys(j.result.sizes).length; if (n !== m.got) { m.got = n; applySizes(j.result.sizes, [], true); } }
    return;
  }
  if (m) { FM.pending.delete(j.id); finish(j, m); }
  else if (FM.seen.has(j.id) && FM.seen.get(j.id) !== "finished") finish(j, { op: j.op });   // started elsewhere (undo, another window)
  FM.seen.set(j.id, "finished");
}
function finish(j, m) {
  FM.seen.set(j.id, "finished");
  if (j.op === "sizes" || m.op === "sizes") return applySizes(j.result?.sizes, m.paths);
  if (j.op === "find" || m.op === "find") return m.onDone?.(j);
  const errs = j.errors || [];
  if (j.state === "done") {
    toast(j.summary || "Done.", "ok", errs.length ? `${plural(errs.length, "item")} had a problem. Open Jobs for details.` : "",
          j.journal ? { label: "Undo", run: () => undo(j.journal) } : errs.length ? { label: "Jobs", run: jobsDlg } : null);
    announce(j.summary || "Done.");
  } else if (j.state === "cancelled") {
    const done = j.done?.n || 0, left = Math.max(0, (j.total?.n || 0) - done);
    const msg = j.summary || `Stopped. ${items(done)} completed; ${left.toLocaleString()} unchanged.`;
    toast(msg, "ok", "", j.journal ? { label: "Undo", run: () => undo(j.journal) } : null);
    announce(msg);
  } else if (j.state === "failed") {
    toast(j.summary || "The operation didn't finish.", "error", errs[0]?.message || "", { label: "Jobs", run: jobsDlg });
    alertEl.textContent = j.summary || "The operation didn't finish.";
  }
  // operations untag what they processed, like other two-pane managers
  if (j.state === "done" && m.t && m.sources) {
    const done = new Set(m.sources.map(norm));
    for (const r of m.t.rows) if (r._t && done.has(norm(rowPath(m.t, r)))) r._t = false;
    recount(m.t);
  }
  // keep the panes honest: refresh listings, prune search results, land the cursor sensibly
  if (m.P && m.t && m.focus) m.t.curName = m.focus;
  if (m.P && m.t && m.next !== undefined) m.t.curName = m.next;
  if (m.sources && ["move", "trash", "delete", "rename"].includes(m.op)) {
    const gone = new Set(m.sources.map(norm));
    for (const P of FM.panes) for (const t of P.tabs) if (t.kind === "find") {
      const before = t.rows.length;
      t.rows = t.rows.filter(r => !gone.has(norm(rowPath(t, r))));
      if (t.rows.length !== before) { resort(t); if (tabOf(P) === t) renderPane(P); }
    }
  }
  refreshAll();
  refreshRoots();
}
async function refreshRoots() {
  try { const r = await api("fm_roots"); if (r.roots) { FM.roots = r.roots; FM.panes.forEach(renderStatus); } } catch { /* keep the old numbers */ }
}
async function control(id, action) {
  try { const r = await api("fm_control", { job: id, action }); if (r.error) toast(r.error, "error"); } catch (e) { toast(e.message, "error"); }
  pollJobs();
}
async function undo(journal) {
  try {
    const r = await api("fm_undo", { journal });
    if (r.error) return toast(r.error, "error");
    FM.pending.set(r.job, { op: "undo" });
    announce("Undoing…");
    pollJobs();
  } catch (e) { toast(e.message, "error"); }
}
function undoLatest() { const h = FM.hist.find(x => x.undo); if (h) undo(h.undo); }
function primaryJob() {
  const act = FM.jobs.filter(j => ACTIVE.has(j.state));
  return act.find(j => j.state === "waiting") || act.find(j => j.state === "running" && MUT(j.op)) || act.find(j => j.state === "paused")
    || act.find(j => j.state === "running") || act[0] || null;
}
function jobLine(j) {
  const tot = j.total || {}, dn = j.done || {};
  const bits = [];
  if (tot.n) bits.push(`${(dn.n || 0).toLocaleString()} of ${tot.n.toLocaleString()}`);
  if (tot.bytes) bits.push(`${fmt(dn.bytes || 0)} / ${fmt(tot.bytes)}`);
  if (j.state === "running" && j.speed) bits.push(`${fmt(j.speed)}/s`);
  if (j.state === "running" && j.eta != null) bits.push(`~${dur(j.eta)}`);
  return bits.join(" · ");
}
const jobPct = j => j.total?.bytes ? (j.done?.bytes || 0) / j.total.bytes : j.total?.n ? (j.done?.n || 0) / j.total.n : 0;
function wireJobs() {
  FM.jobsEl.addEventListener("click", e => {
    const b = e.target.closest("[data-j]"), j = primaryJob();
    if (!b) return;
    const a = b.dataset.j;
    if (a === "list") return jobsDlg();
    if (!j) return;
    if (a === "answer") { FM.askDismissed = null; maybeAsk(true); }
    else if (a === "pause") control(j.id, j.state === "paused" ? "resume" : "pause");
    else if (a === "cancel") control(j.id, "cancel");
  });
}
function renderJobs() {
  if (!FM.built) return;
  const el = FM.jobsEl, j = primaryJob(), act = FM.jobs.filter(x => ACTIVE.has(x.state)).length;
  const txt = el.querySelector(".txt"), bar = el.querySelector(".bar");
  const B = k => el.querySelector(`[data-j="${k}"]`);
  el.classList.toggle("wait", j?.state === "waiting");
  if (!j) {
    txt.innerHTML = FM.hist.length ? `No file operations running. Last: ${esc(FM.hist[0].summary || "")}` : "No file operations running.";
    B("answer").hidden = B("pause").hidden = B("cancel").hidden = true; bar.hidden = true;
  } else {
    const head = j.state === "waiting" ? "Waiting for your answer" : j.state === "paused" ? "Paused" : j.state === "queued" ? "Queued" : "";
    txt.innerHTML = `<b>${esc(head ? head + " · " : "")}${esc(j.title || j.op)}</b>${jobLine(j) ? " · " + esc(jobLine(j)) : ""}`;
    txt.title = j.current || "";
    B("answer").hidden = j.state !== "waiting";
    B("pause").hidden = !(j.state === "running" || j.state === "paused");
    B("pause").textContent = j.state === "paused" ? "Resume" : "Pause";
    B("cancel").hidden = false;
    const pct = Math.round(jobPct(j) * 100);
    bar.hidden = false; bar.style.width = pct + "%"; bar.setAttribute("aria-valuenow", String(pct));
    if (Date.now() - FM.lastAnnounce > 10000 && j.state === "running") { FM.lastAnnounce = Date.now(); announce(`${j.title || j.op}: ${pct}%`); }
  }
  B("list").textContent = act ? `Jobs (${act})` : "Jobs";
  // header indicator for other views
  const hj = FM.hjob;
  if (hj) {
    hj.hidden = !j || S.mode === "files";
    if (j) hj.innerHTML = j.state === "waiting" ? `${I.warn}<span>Files needs an answer</span>` : `<span class="spinner sm"></span><span class="num">${esc(j.title || "Working")} · ${Math.round(jobPct(j) * 100)}%</span>`;
  }
}
function maybeAsk(force) {
  const j = FM.jobs.find(x => x.state === "waiting" && x.question);
  if (!j || $("#dlg").open || S.mode !== "files") return;
  const q = j.question, key = `${j.id}|${q.kind}|${q.src || ""}|${q.dst || ""}|${q.message || ""}`;
  if (!force && (FM.askDismissed === key || (FM.askKey === key && Date.now() - FM.askAt < 4000))) return;
  FM.askKey = key;
  showAsk(j, key);
}
function showAsk(j, key) {
  const q = j.question, choices = q.choices || [];
  const LABEL = { replace: "Replace", skip: "Skip", keep: "Keep both", abort: "Abort job", retry: "Retry", skipall: "Skip all" };
  let answered = false;
  const conflict = q.kind === "conflict";
  const html = conflict
    ? `<h3>‘${esc(baseName(q.dst || q.src))}’ already exists in ${esc(dirName(q.dst || q.src))}.</h3>
       <div class="fm-cmp"><span></span><b>Size</b><b>Modified</b>
         <b>Copying</b><span>${q.srcSize != null ? fmt(q.srcSize) : "—"}</span><span>${q.srcTime ? esc(new Date(q.srcTime * 1000).toLocaleString()) : "—"}</span>
         <b>Existing</b><span>${q.dstSize != null ? fmt(q.dstSize) : "—"}</span><span>${q.dstTime ? esc(new Date(q.dstTime * 1000).toLocaleString()) : "—"}</span></div>
       ${!choices.includes("replace") ? `<p class="fm-note">Replacing isn't offered because Hoard can't keep a recoverable copy of the existing item.</p>` : ""}
       <label class="fm-cl" style="margin-top:8px"><input type="checkbox" id="fmAll"> Apply to remaining conflicts</label>`
    : `<h3>${esc(j.title || "The operation")} stopped on an item</h3><div class="dpath">${esc(q.src || "")}</div><p>${esc(q.message || "")}</p>
       ${choices.includes("retry") ? "" : `<p class="fm-note">Retry isn't offered: this item is protected or can't be changed.</p>`}`;
  const safe = ["keep", "skip", "retry"].find(c => choices.includes(c)) || choices[0];
  const d = dlg("", `${html}<div class="row-actions">${choices.map(c => `<button class="btn ${c === "abort" ? "danger" : c === safe ? "primary" : ""}" data-a="${c}">${esc(LABEL[c] || c)}</button>`).join("")}</div>`,
    { onClose: () => { if (!answered) { FM.askDismissed = key; announce("The job is waiting. Press Answer in the job strip when you're ready."); } } });
  d.setAttribute("role", "alertdialog");
  alertEl.textContent = conflict ? `${baseName(q.dst || q.src)} already exists. Choose what to do.` : (q.message || "The operation needs a decision.");
  d.querySelectorAll("[data-a]").forEach(b => b.onclick = async () => {
    answered = true;
    const all = !!$("#fmAll")?.checked;
    d.close(); d.removeAttribute("role");
    FM.askAt = Date.now();
    try { const r = await api("fm_decide", { job: j.id, answer: b.dataset.a, all }); if (r.error) toast(r.error, "error"); } catch (e) { toast(e.message, "error"); }
    pollJobs();
  });
  d.querySelector(`[data-a="${safe}"]`)?.focus();
}
function jobsDlg() {
  const draw = () => {
    const d = $("#dlg");
    if (!d.open || !d.classList.contains("fm-jobsdlg")) { FM.jobsHook = null; return; }
    const act = FM.jobs.filter(j => ACTIVE.has(j.state)), fin = FM.jobs.filter(j => !ACTIVE.has(j.state) && j.errors?.length);
    const focusKey = document.activeElement?.dataset?.k;
    $("#fmJA").innerHTML = act.length ? act.map(j => `<div class="j"><b>${esc(j.title || j.op)}</b><small>${esc(j.state === "running" ? jobLine(j) || "Working…" : j.state)}${j.current ? ` · ${esc(baseName(j.current))}` : ""}</small>
        <span class="a">${j.state === "waiting" ? `<button class="btn" data-k="ans${j.id}" data-ans="${j.id}">Answer</button>` : ""}
          ${j.state === "running" || j.state === "paused" ? `<button class="btn" data-k="p${j.id}" data-ctl="${j.id}" data-act="${j.state === "paused" ? "resume" : "pause"}">${j.state === "paused" ? "Resume" : "Pause"}</button>` : ""}
          <button class="btn" data-k="c${j.id}" data-ctl="${j.id}" data-act="cancel">Cancel</button></span></div>`).join("")
      : `<div class="tnote">Nothing is running.</div>`;
    $("#fmJE").innerHTML = fin.map(j => `<div class="j"><b>${esc(j.summary || j.title || j.op)}</b><span class="errs">${j.errors.slice(0, 5).map(x => `${esc(baseName(x.path || ""))}: ${esc(x.message)}`).join("<br>")}${j.errors.length > 5 ? `<br>…and ${j.errors.length - 5} more` : ""}</span></div>`).join("");
    $("#fmJH").innerHTML = FM.hist.length ? FM.hist.slice(0, 50).map(h => `<div class="j"><b>${esc(h.summary || h.op)}</b><small>${h.time ? esc(when(h.time)) : ""}</small>
        <span class="a">${h.undo ? `<button class="btn" data-k="u${h.id}" data-undo="${esc(h.undo)}">Undo</button>` : h.op === "delete" ? `<span class="cant">Cannot undo</span>` : ""}</span></div>`).join("")
      : `<div class="tnote">No file operations yet.</div>`;
    if (focusKey) d.querySelector(`[data-k="${focusKey}"]`)?.focus();
  };
  const d = dlg("fm-wide fm-jobsdlg", `<h3>Jobs</h3><div class="fm-h">Running and queued</div><div class="fm-jobl" id="fmJA"></div>
    <div class="fm-jobl" id="fmJE"></div><div class="fm-h">History</div><div class="fm-jobl" id="fmJH"></div>
    <div class="row-actions"><button class="btn primary" data-cancel>Close</button></div>`, { onClose: () => { FM.jobsHook = null; } });
  d.onclick = e => {
    const c = e.target.closest("[data-ctl]"), u = e.target.closest("[data-undo]"), a = e.target.closest("[data-ans]");
    if (c) control(c.dataset.ctl, c.dataset.act);
    if (u) { u.disabled = true; undo(u.dataset.undo); }
    if (a) { d.close(); FM.askDismissed = null; setTimeout(() => maybeAsk(true), 50); }
  };
  FM.jobsHook = draw;
  draw();
  d.querySelector("[data-cancel]").focus();
  pollJobs();
}

/* ------------------------------------------------------------------ drag and drop */
function rowDragStart(P, e) {
  const el = e.target.closest(".fm-row");
  if (!el) return;
  const t = tabOf(P), r = t.view[+el.dataset.i];
  if (!r) return;
  const rows = r._t ? t.rows.filter(x => x._t) : [r];
  FM.drag = { P, t, rows, paths: rows.map(x => rowPath(t, x)) };
  e.dataTransfer.effectAllowed = "copyMove";
  e.dataTransfer.setData("text/plain", FM.drag.paths.join("\n"));
  try {
    const tot = rows.reduce((a, x) => a + (x.s || 0), 0);
    fillGhost({ n: rows.length > 1 ? items(rows.length) : r.n, s: tot, k: isDir(r) ? "d" : "f", c: r.c ?? 8 }, C.cats?.[r.c ?? 8] || "#888", 150, 64);
    ghost.style.left = "-1000px"; ghost.style.top = "-1000px"; ghost.style.display = "flex";
    e.dataTransfer.setDragImage(ghost, 20, 20);
    setTimeout(() => { ghost.style.display = "none"; }, 0);
  } catch { /* the browser's own drag image is fine */ }
  startDragUI();
}
function dropTarget(P, e) {
  const dr = FM.drag, t = tabOf(P);
  if (!dr) return null;
  const el = e.target.closest?.(".fm-row"), r = el ? t.view[+el.dataset.i] : null;
  if (r && isDir(r) && !dr.rows.includes(r)) return { path: rowPath(t, r), row: r };
  if (P !== dr.P && t.kind === "dir" && !t.branch && !t.error && t.loaded) return { path: t.path, row: null };
  return null;
}
function dragOver(P, e) {
  const tg = dropTarget(P, e);
  if (!tg) { dragClear(); return; }
  e.preventDefault();
  const move = isMac() ? (e.metaKey && !e.altKey) : (e.shiftKey && !e.ctrlKey);
  e.dataTransfer.dropEffect = move ? "move" : "copy";
  FM.tip.textContent = `${move ? "Move" : "Copy"} to ${tg.path}`;
  FM.tip.hidden = false;
  FM.tip.style.left = (e.clientX + 16) + "px"; FM.tip.style.top = (e.clientY + 18) + "px";
  if (FM.dropRow !== tg.row) {
    if (FM.dropRow) FM.dropRow._drop = false;
    FM.dropRow = tg.row; if (tg.row) tg.row._drop = true;
    FM.panes.forEach(x => renderRows(x));
  }
}
function dragClear() {
  FM.tip.hidden = true;
  if (FM.dropRow) { FM.dropRow._drop = false; FM.dropRow = null; FM.panes.forEach(x => renderRows(x)); }
}
function dropOn(P, e) {
  const tg = dropTarget(P, e), dr = FM.drag;
  dragClear();
  if (!tg || !dr) return;
  e.preventDefault();
  const move = isMac() ? (e.metaKey && !e.altKey) : (e.shiftKey && !e.ctrlKey);
  FM.drag = null;
  endDragUI();
  copyMove(move ? "move" : "copy", { rows: dr.rows, P: dr.P, dest: tg.path });
}
function dragEnd() { dragClear(); FM.drag = null; }
function wireStageDrop() {
  if (FM.stageWired) return;
  FM.stageWired = true;
  stageBtn.addEventListener("dragover", e => { if (FM.drag) { e.preventDefault(); e.dataTransfer.dropEffect = "move"; stageBtn.classList.add("drop"); } });
  stageBtn.addEventListener("drop", e => { if (!FM.drag) return; e.preventDefault(); const dr = FM.drag; FM.drag = null; stageRows(dr.P, dr.rows); });
}

/* ------------------------------------------------------------------ viewer (F3) */
const VCHUNK = 262144, VCAP = 8 * 1048576, SCHUNK = 1048576, HEXL = 18, HBLK = 65536;
let V = null;
async function readRange(path, offset, length) {
  const r = await api("fm_read", { path, offset, length });
  if (r.error) throw new Error(r.error);
  return { bytes: b64(r.data), size: r.size, eof: !!r.eof };
}
function openViewer(hex, rowIn) {
  const P = AP(), t = tabOf(P), r = rowIn || curRow(t);
  if (!r) return;
  if (isDir(r)) return enter(P, r);
  const d = dlg("fm-viewer", `<div class="fm-vhead"><h3 id="fmVT"></h3><div class="fm-vpath" id="fmVP"></div></div>
    <div class="fm-vtools">
      <div class="seg" role="group" aria-label="Show as" id="fmVMode"><button data-m="media">Preview</button><button data-m="text">Text</button><button data-m="hex">Hex</button></div>
      <label class="fm-cl" id="fmVWrapL"><input type="checkbox" id="fmVWrap"> Wrap</label>
      <button class="btn" id="fmVFit" hidden>Actual size</button>
      <button class="btn" id="fmVGoB">Go to…</button>
      <span class="fm-vgo" id="fmVGo" hidden><input type="text" id="fmVGoIn" placeholder="50% or 0x1F00" aria-label="Go to a percentage or byte offset, like 50% or 0x1F00"> <button class="btn" id="fmVGoOk">Go</button></span>
      <span id="fmVS"><input type="search" id="fmVQ" placeholder="Search" aria-label="Search in this file. F3 finds the next match, Shift+F3 the previous."> <select id="fmVQm" aria-label="Search mode"><option value="lit">Text</option><option value="re">Regex</option></select>
        <button class="btn icon" id="fmVPrevM" title="Previous match (Shift+F3)" aria-label="Previous match">↑</button><button class="btn icon" id="fmVNextM" title="Next match (F3)" aria-label="Next match">↓</button></span>
      <span class="fm-vstat" id="fmVStat" aria-live="polite"></span><span class="sp"></span>
      <button class="btn" id="fmVPrev" title="Previous file (Ctrl+PgUp)">‹ Previous file</button><button class="btn" id="fmVNext" title="Next file (Ctrl+PgDn)">Next file ›</button>
    </div>
    <div class="fm-vbody" id="fmVB" tabindex="0" aria-label="File contents"></div>
    <div class="row-actions"><button class="btn" id="fmVOpen">${ICON.launch}Open with default app</button><button class="btn primary" data-cancel>Close</button></div>`,
    { onClose: () => { if (V) V.tok++; d.querySelectorAll("video,audio").forEach(m => m.pause()); V = null; } });
  V = { P, t, r, tok: 0, hex };
  $("#fmVB").focus();
  $("#fmVMode").addEventListener("click", e => { const b = e.target.closest("button"); if (b) vShow(b.dataset.m); });
  $("#fmVWrap").onchange = e => $("#fmVB").classList.toggle("wrap", e.target.checked);
  $("#fmVFit").onclick = () => { const b = $("#fmVB"), a = b.classList.toggle("actual"); $("#fmVFit").textContent = a ? "Fit" : "Actual size"; };
  $("#fmVGoB").onclick = () => { $("#fmVGo").hidden = false; $("#fmVGoIn").focus(); };
  $("#fmVGoOk").onclick = vGoto;
  $("#fmVGoIn").addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); e.stopPropagation(); vGoto(); } else if (e.key === "Escape") { e.preventDefault(); $("#fmVGo").hidden = true; $("#fmVB").focus(); } });
  $("#fmVQ").addEventListener("keydown", e => {
    if (e.key === "Enter") { e.preventDefault(); e.stopPropagation(); vSearch(e.shiftKey); }
    else if (e.key === "Escape" && e.target.value) { e.preventDefault(); e.target.value = ""; }
  });
  $("#fmVNextM").onclick = () => vSearch(false);
  $("#fmVPrevM").onclick = () => vSearch(true);
  $("#fmVPrev").onclick = () => vStep(-1);
  $("#fmVNext").onclick = () => vStep(1);
  $("#fmVOpen").onclick = () => openDefault(V.path);
  $("#fmVB").addEventListener("scroll", () => { if (!V) return; if (V.mode === "text") vMaybeMore(); else if (V.mode === "hex" && !V.raf) V.raf = requestAnimationFrame(() => { V.raf = 0; hexRender(); }); }, { passive: true });
  const baseKey = d.onkeydown;
  d.onkeydown = e => {
    baseKey?.(e);
    if (!V) return;
    const k = combo(e);
    if (k === "F3" || k === "Shift+F3") { e.preventDefault(); vSearch(k !== "F3"); }
    else if (k === "Ctrl+F" || (k === "/" && !e.target.matches("input"))) { e.preventDefault(); $("#fmVQ").focus(); $("#fmVQ").select(); }
    else if (k === "Ctrl+PageDown" || k === "Ctrl+PageUp") { e.preventDefault(); vStep(k === "Ctrl+PageDown" ? 1 : -1); }
    else if (k === "F10") { e.preventDefault(); d.close(); }
    else if (k === "Shift+F4" || k === "F4") { e.preventDefault(); vShow(V.mode === "hex" ? (V.kind ? "media" : "text") : "hex"); }
  };
  vLoad();
}
function vLoad() {
  const r = V.r;
  V.path = rowPath(V.t, r);
  V.kind = mediaKind(r.n);
  V.size = r.s;
  V.match = -1; V.hm = -1; V.hlen = 0;
  $("#fmVT").textContent = baseName(r.n);
  $("#fmVP").textContent = V.path;
  $("#fmVMode [data-m=media]").hidden = !V.kind;
  vShow(V.hex ? "hex" : V.kind ? "media" : "text");
  V.hex = false;
}
function vStep(dir) {
  const P = V.P, t = V.t;
  if (tabOf(P) !== t) return;
  let i = t.view.indexOf(V.r);
  do i += dir; while (i >= 0 && i < t.view.length && isDir(t.view[i]));
  if (i < 0 || i >= t.view.length) return vStat(dir > 0 ? "This is the last file." : "This is the first file.");
  moveCursor(P, i);
  V.r = t.view[i];
  vLoad();
}
const vStat = s => { const el = $("#fmVStat"); if (el) el.textContent = s; };
function vShow(mode) {
  if (!V) return;
  V.tok++;
  V.mode = mode;
  const body = $("#fmVB");
  body.className = "fm-vbody" + ($("#fmVWrap").checked ? " wrap" : "");
  body.innerHTML = ""; body.scrollTop = 0;
  $("#fmVMode").querySelectorAll("button").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.m === mode)));
  $("#fmVWrapL").hidden = mode !== "text";
  $("#fmVS").hidden = mode === "media";
  $("#fmVQm").hidden = mode === "hex";
  $("#fmVQ").placeholder = mode === "hex" ? "Bytes, like 4D 5A" : "Search";
  $("#fmVGoB").hidden = mode === "media";
  $("#fmVGo").hidden = true;
  $("#fmVFit").hidden = !(mode === "media" && V.kind === "img");
  $("#fmVFit").textContent = "Actual size";
  vStat("");
  if (mode === "media") return vMedia(body);
  if (mode === "hex") return vHex(0);
  return vText(0);
}
function vMedia(body) {
  const url = fmURL(V.path), name = esc(baseName(V.r.n));
  body.classList.add("media");
  const none = `<div class="empty"><b>No preview available.</b>Open this file with its usual app.</div>`;
  if (V.kind === "img") body.innerHTML = `<img src="${url}" alt="${name}">`;
  else if (V.kind === "video") body.innerHTML = `<video src="${url}" controls preload="metadata"></video>`;
  else if (V.kind === "audio") body.innerHTML = `<audio src="${url}" controls preload="metadata" style="width:min(640px,90%)"></audio>`;
  else if (V.kind === "pdf") { body.innerHTML = `<iframe src="${url}" title="${name}"></iframe>`; vStat("If the PDF doesn't show, use Open with default app."); }
  const m = body.querySelector("img,video,audio");
  if (m) m.addEventListener("error", () => { body.innerHTML = none; vStat("This format can't be shown here."); });
}
/* text: ranged reads appended as you scroll */
async function vText(start) {
  const my = V.tok;
  Object.assign(V, { tStart: start, tEnd: start, eof: false, dec: new TextDecoder("utf-8"), txt: "", busy: false, match: -1 });
  const body = $("#fmVB");
  body.innerHTML = `<pre></pre>`;
  V.pre = body.firstChild;
  await vMore(true);
  if (my === V?.tok && V.binary) { vStat("This looks like a binary file, so it's shown as hex."); V.mode = "hex"; $("#fmVMode").querySelectorAll("button").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.m === "hex"))); $("#fmVWrapL").hidden = true; vHex(start); }
}
async function vMore(first) {
  if (!V || V.busy || V.eof || V.tEnd - V.tStart >= VCAP) return;
  const my = V.tok;
  V.busy = true;
  let res;
  try { res = await readRange(V.path, V.tEnd, VCHUNK); }
  catch (e) { if (V && my === V.tok) { V.busy = false; vStat(e.message); } return; }
  if (!V || my !== V.tok) return;
  V.busy = false; V.binary = false;
  if (first && res.bytes.subarray(0, 8192).includes(0)) { V.binary = true; V.size = res.size; return; }
  V.size = res.size; V.tEnd += res.bytes.length; V.eof = res.eof || !res.bytes.length;
  const s = V.dec.decode(res.bytes, { stream: !V.eof });
  V.txt += s; V.pre.append(s);
  vStat(`${fmt(V.tEnd - V.tStart)} of ${fmt(V.size)}${V.tStart ? ` from ${fmt(V.tStart)}` : ""}${!V.eof && V.tEnd - V.tStart >= VCAP ? " shown. Use Go to for later parts." : ""}`);
  vMaybeMore();
}
function vMaybeMore() {
  const b = $("#fmVB");
  if (b && V && !V.eof && b.scrollHeight - b.scrollTop - b.clientHeight < 1200) vMore();
}
function vGoto() {
  const v = $("#fmVGoIn").value.trim();
  let off = null;
  if (/^\d+(\.\d+)?%$/.test(v)) off = Math.floor((V.size || 0) * parseFloat(v) / 100);
  else if (/^0x[0-9a-f]+$/i.test(v)) off = parseInt(v, 16);
  else if (/^\d+$/.test(v)) off = +v;
  if (off == null) return vStat("Type a percentage or a byte offset, like 50% or 0x1F00.");
  off = clamp(off, 0, Math.max(0, (V.size || 0) - 1));
  $("#fmVGo").hidden = true;
  if (V.mode === "hex") { hexScrollTo(Math.floor(off / 16)); $("#fmVB").focus(); }
  else { V.tok++; vText(off).then(() => $("#fmVB").focus()); }
}
function markText(i, len) {
  V.match = i;
  V.pre.textContent = "";
  const mk = document.createElement("mark");
  mk.textContent = V.txt.slice(i, i + len);
  V.pre.append(V.txt.slice(0, i), mk, V.txt.slice(i + len));
  mk.scrollIntoView({ block: "center" });
}
function searchRe() {
  const q = $("#fmVQ").value;
  if (!q) return null;
  try { return new RegExp($("#fmVQm").value === "re" ? q : escRe(q), "gi"); } catch { vStat("That regex isn't valid."); return false; }
}
async function vSearch(back) {
  if (!V) return;
  if (V.mode === "hex") return hexSearch(back);
  if (V.mode !== "text") return;
  const re = searchRe();
  if (re === null) { $("#fmVQ").focus(); return; }
  if (!re) return;
  if (back) {
    let last = null, m;
    re.lastIndex = 0;
    while ((m = re.exec(V.txt)) && m.index < V.match) { last = m; if (!m[0]) re.lastIndex++; }
    if (last) return markText(last.index, last[0].length);
    return vStat(V.tStart ? "No earlier match in this part. Use Go to to look earlier." : "No earlier match.");
  }
  re.lastIndex = V.match + 1;
  const m = re.exec(V.txt);
  if (m) return markText(m.index, m[0].length);
  if (V.eof && V.tEnd >= (V.size || 0)) return vStat("No more matches.");
  // read ahead through the rest of the file, a chunk at a time
  const my = ++V.tok;
  let off = V.tEnd;
  const dec = new TextDecoder("utf-8");
  while (off < V.size) {
    vStat(`Searching… ${Math.round(off / V.size * 100)}%`);
    let res;
    try { res = await readRange(V.path, off, SCHUNK); } catch (e) { if (V && my === V.tok) vStat(e.message); return; }
    if (!V || my !== V.tok) return;
    re.lastIndex = 0;
    if (re.test(dec.decode(res.bytes))) {
      const start = Math.max(0, off - 4096);
      await vText(start);
      if (!V) return;
      while (V && !V.eof && V.tEnd - V.tStart < VCAP) {
        re.lastIndex = 0;
        const mm = re.exec(V.txt);
        if (mm) return markText(mm.index, mm[0].length);
        await vMore();
      }
      re.lastIndex = 0;
      const mm = V && re.exec(V.txt);
      if (mm) return markText(mm.index, mm[0].length);
      return;
    }
    if (res.eof || !res.bytes.length) break;
    off += res.bytes.length - Math.min(4096, res.bytes.length - 1);
  }
  if (V && my === V.tok) vStat("No more matches.");
}
/* hex: a virtual list over the whole file, read in 64 KB blocks */
async function vHex(startOff) {
  const my = V.tok, body = $("#fmVB");
  V.blocks = new Map(); V.pend = new Set(); V.mode = "hex";
  if (V.size == null) { try { V.size = (await readRange(V.path, 0, 1)).size; } catch (e) { return vStat(e.message); } }
  if (!V || my !== V.tok) return;
  V.lines = Math.max(1, Math.ceil(V.size / 16));
  const full = V.lines * HEXL;
  V.vh = Math.min(full, 4000000);
  body.innerHTML = `<div class="fm-hexsp" style="height:${V.vh}px"></div><div class="fm-hexl"></div>`;
  vStat(`${fmt(V.size)} · ${V.size.toLocaleString()} bytes`);
  if (startOff) hexScrollTo(Math.floor(startOff / 16)); else hexRender();
}
function hexTop() {
  const b = $("#fmVB"), vis = Math.max(1, Math.floor(b.clientHeight / HEXL));
  if (V.vh >= V.lines * HEXL) return Math.floor(b.scrollTop / HEXL);
  const max = Math.max(1, b.scrollHeight - b.clientHeight);
  return Math.round(b.scrollTop / max * Math.max(0, V.lines - vis));
}
function hexScrollTo(line) {
  const b = $("#fmVB"), vis = Math.max(1, Math.floor(b.clientHeight / HEXL));
  line = clamp(line - 2, 0, Math.max(0, V.lines - 1));
  if (V.vh >= V.lines * HEXL) b.scrollTop = line * HEXL;
  else b.scrollTop = line / Math.max(1, V.lines - vis) * Math.max(1, b.scrollHeight - b.clientHeight);
  hexRender();
}
function hexRender() {
  if (!V || V.mode !== "hex") return;
  const b = $("#fmVB"), box = b.querySelector(".fm-hexl");
  if (!box) return;
  const top = hexTop(), count = Math.ceil(b.clientHeight / HEXL) + 1;
  box.style.transform = `translateY(${b.scrollTop}px)`;
  const out = [];
  for (let ln = top; ln < Math.min(V.lines, top + count); ln++) {
    const off = ln * 16, blk = Math.floor(off / HBLK), data = V.blocks.get(blk);
    if (!data) { hexFetch(blk); out.push(`<div><span class="o">${off.toString(16).padStart(8, "0")}</span>··</div>`); continue; }
    let hx = "", asc = "";
    for (let i = 0; i < 16; i++) {
      const p = off + i;
      if (p >= V.size) { hx += "   "; continue; }
      const v = data[p - blk * HBLK];
      const on = V.hm >= 0 && p >= V.hm && p < V.hm + V.hlen;
      const h = v.toString(16).padStart(2, "0"), c = v >= 32 && v < 127 ? esc(String.fromCharCode(v)) : ".";
      hx += (on ? `<mark>${h}</mark>` : h) + (i === 7 ? "  " : " ");
      asc += on ? `<mark>${c}</mark>` : c;
    }
    out.push(`<div><span class="o">${off.toString(16).padStart(8, "0")}</span>${hx}<span class="a">${asc}</span></div>`);
  }
  box.innerHTML = out.join("");
}
async function hexFetch(blk) {
  const blocks = V.blocks, pend = V.pend, path = V.path;
  if (pend.has(blk)) return;
  pend.add(blk);
  try {
    const r = await readRange(path, blk * HBLK, HBLK);
    blocks.set(blk, r.bytes);
    if (blocks.size > 64) blocks.delete(blocks.keys().next().value);
  } catch (e) { if (V && V.blocks === blocks) vStat(e.message); }
  pend.delete(blk);
  if (V && V.blocks === blocks) hexRender();
}
function parseHex(q) {
  const s = q.replace(/\s+/g, "");
  if (!s || s.length % 2 || /[^0-9a-f]/i.test(s)) return null;
  const out = new Uint8Array(s.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(s.substr(i * 2, 2), 16);
  return out;
}
function findBytes(hay, nd, from, back) {
  const last = hay.length - nd.length;
  const at = i => { for (let k = 0; k < nd.length; k++) if (hay[i + k] !== nd[k]) return false; return true; };
  if (!back) { for (let i = Math.max(0, from); i <= last; i++) if (at(i)) return i; }
  else for (let i = Math.min(from, last); i >= 0; i--) if (at(i)) return i;
  return -1;
}
async function hexSearch(back) {
  const q = $("#fmVQ").value, nd = parseHex(q);
  if (!q) return $("#fmVQ").focus();
  if (!nd) return vStat("Type byte pairs, like 4D 5A.");
  const my = ++V.tok;
  let pos = V.hm >= 0 ? V.hm + (back ? -1 : 1) : hexTop() * 16;
  while (back ? pos >= 0 : pos < V.size) {
    const start = back ? Math.max(0, pos - SCHUNK + nd.length) : pos;
    vStat(`Searching… ${Math.round(start / Math.max(1, V.size) * 100)}%`);
    let r;
    try { r = await readRange(V.path, start, SCHUNK); } catch (e) { if (V && my === V.tok) vStat(e.message); return; }
    if (!V || my !== V.tok) return;
    const i = findBytes(r.bytes, nd, back ? pos - start : 0, back);
    if (i >= 0) {
      V.hm = start + i; V.hlen = nd.length;
      vStat(`Found at 0x${V.hm.toString(16)}`);
      return hexScrollTo(Math.floor(V.hm / 16));
    }
    if (back) { if (start === 0) break; pos = start + nd.length - 2; }
    else { if (r.eof || !r.bytes.length) break; pos = start + r.bytes.length - nd.length + 1; }
  }
  if (V && my === V.tok) { V.tok++; vStat(back ? "No earlier match." : "No more matches."); hexRender(); }
}

/* ------------------------------------------------------------------ quick view (Ctrl+Q): the inactive pane shows the cursor item */
function toggleQuick() { if (FM.quick) closeQuick(); else openQuick(); }
function openQuick() {
  const Q = OP();
  FM.quick = { P: Q, tok: 0 };
  Q.el.quick.hidden = false;
  quickNow();
  announce("Quick view on. Ctrl+Q or Tab closes it.");
}
function closeQuick() {
  if (!FM.quick) return;
  const Q = FM.quick.P;
  Q.el.quick.querySelectorAll("video,audio").forEach(m => m.pause());
  Q.el.quick.hidden = true; Q.el.quick.innerHTML = "";
  FM.quick = null;
}
let quickT = 0;
function quickSoon() { if (FM.quick) { clearTimeout(quickT); quickT = setTimeout(quickNow, 140); } }
async function quickNow() {
  const q = FM.quick;
  if (!q) return;
  if (q.P === AP()) return closeQuick();
  const t = AT(), r = curRow(t), my = ++q.tok, el = q.P.el.quick;
  const dest = tabOf(q.P).path;
  const head = `<div class="fm-qhead"><div class="t"><h3>Quick view · ${esc(r ? baseName(r.n) : "nothing selected")}</h3><button class="btn" data-q="close">Close quick view</button></div>
    <div class="dest" title="${esc(dest)}">Underneath: ${esc(dest)}</div></div>`;
  if (!r) { el.innerHTML = head + `<div class="fm-qbody"><div class="empty">Nothing to show.</div></div>`; return; }
  const p = rowPath(t, r), kind = isDir(r) ? null : mediaKind(r.n);
  const info = `<dl class="fm-qinfo"><dt>Type</dt><dd>${esc(typeName(r))}</dd><dt>Size</dt><dd>${r.s != null ? `${fmt(r.s)}${r.a != null && r.a !== r.s ? ` (${fmt(r.a)} on disk)` : ""}` : "Not in the last scan. Press Space to calculate."}</dd>
    <dt>Modified</dt><dd>${r.m ? esc(new Date(r.m * 1000).toLocaleString()) : "—"}</dd><dt>Where</dt><dd class="mono">${esc(p)}</dd>${r.p ? `<dt>Protected</dt><dd>${esc(r.p)}</dd>` : ""}${r.h ? `<dt>Hidden</dt><dd>Yes</dd>` : ""}</dl>`;
  let body;
  if (isDir(r) || r.k === "l") body = info;
  else if (kind === "img") body = `<img src="${fmURL(p)}" alt="${esc(baseName(r.n))}">`;
  else if (kind === "video") body = `<video src="${fmURL(p)}" controls preload="metadata"></video>`;
  else if (kind === "audio") body = `<div style="display:grid;gap:12px;justify-items:center;width:100%">${info}<audio src="${fmURL(p)}" controls preload="metadata"></audio></div>`;
  else if (kind === "pdf") body = `<iframe src="${fmURL(p)}" title="${esc(baseName(r.n))}"></iframe>`;
  else body = `<div class="loading">${SPIN}Loading…</div>`;
  el.innerHTML = head + `<div class="fm-qbody">${body}</div>`;
  el.querySelector("img,video,audio")?.addEventListener("error", () => { const b = el.querySelector(".fm-qbody"); if (b) b.innerHTML = `<div class="empty"><b>No preview available.</b>Open this file with its usual app.</div>${info}`; });
  if (isDir(r) || r.k === "l" || kind) return;
  try {
    const res = await readRange(p, 0, 65536);
    if (!FM.quick || my !== FM.quick.tok) return;
    const b = el.querySelector(".fm-qbody");
    if (res.bytes.includes(0)) { b.innerHTML = `<div style="display:grid;gap:12px;width:100%"><div class="empty"><b>No preview available.</b>Open this file with its usual app.</div>${info}</div>`; return; }
    b.innerHTML = "<pre></pre>";
    b.firstChild.textContent = new TextDecoder("utf-8").decode(res.bytes) + (res.size > 65536 ? "\n\n… (the first 64 KB; F3 shows it all)" : "");
  } catch (e) {
    if (!FM.quick || my !== FM.quick.tok) return;
    const b = el.querySelector(".fm-qbody");
    if (b) b.innerHTML = `<div class="empty">${esc(e.message)}</div>`;
  }
}

/* ------------------------------------------------------------------ find files (Alt+F7) */
function findDlg() {
  const P = AP(), t = tabOf(P), last = FM.lastFind || {};
  const d = dlg("", `<h3>Find files</h3>
    <label class="fm-field"><span>Start in</span><input type="text" id="fmFS" class="mono" value="${esc(t.kind === "dir" ? t.path : t.path)}" spellcheck="false" autocomplete="off"></label>
    <label class="fm-field"><span>Name pattern</span><input type="text" id="fmFN" value="${esc(last.name ?? "*")}" spellcheck="false" autocomplete="off"><small>Wildcards, separated by semicolons: *.mp4;*.mkv</small></label>
    <label class="fm-field"><span>Containing text</span><input type="text" id="fmFT" value="${esc(last.text || "")}" spellcheck="false" autocomplete="off"><small>Leave empty to match names only.</small></label>
    <div class="fm-checks"><label><input type="checkbox" id="fmFR" ${last.regex ? "checked" : ""}> Regex</label><label><input type="checkbox" id="fmFC" ${last.case ? "checked" : ""}> Match case</label>
      <label><input type="checkbox" id="fmFH" ${last.hidden ? "checked" : ""}> Include hidden</label></div>
    <p class="fm-note">Links are never followed.</p>
    <div class="fm-plan" id="fmFP" aria-live="polite"></div>
    <p class="form-error" id="fmErr" hidden></p>
    <div class="row-actions"><button class="btn" data-cancel>Cancel</button><button class="btn" id="fmFStop" hidden>Stop</button><button class="btn" id="fmFShow" hidden>Show in pane</button><button class="btn primary" data-primary>Find</button></div>`,
    { onClose: () => { FM.findHook = null; } });
  const btn = d.querySelector("[data-primary]"), prog = $("#fmFP"), errEl = $("#fmErr"), stopB = $("#fmFStop"), showB = $("#fmFShow");
  $("#fmFN").focus(); $("#fmFN").select();
  let job = null, rows = null, q = null;
  const show = () => {
    if (!rows) return;
    d.close();
    const nt = newTab(q.start, { kind: "find", label: q.name && q.name !== "*" ? q.name : q.text ? `“${q.text}”` : "all files", sort: "name" });
    nt.rows = rows.map(r => ({ ...r }));
    nt.loaded = true; nt.path = q.start;
    nt.crumbs = [{ n: baseName(q.start) || q.start, path: q.start }];
    sortRows(nt); applyFilter(nt, true); nt.cur = 0;
    addTab(P, null, { tab: nt });
    activate(P, true);
    announce(`${items(rows.length)} found. They're in a new tab; operations act on their real locations.`);
  };
  showB.onclick = show;
  stopB.onclick = () => { if (job) control(job, "cancel"); };
  btn.onclick = async () => {
    q = { start: $("#fmFS").value.trim(), name: $("#fmFN").value.trim(), text: $("#fmFT").value, regex: $("#fmFR").checked, case: $("#fmFC").checked, hidden: $("#fmFH").checked };
    FM.lastFind = q;
    errEl.hidden = true;
    if (!q.start) { errEl.textContent = "Choose a folder to start in."; errEl.hidden = false; return; }
    btn.disabled = true; showB.hidden = true; rows = null;
    prog.innerHTML = `<div class="loading">${SPIN}<span id="fmFPT">Searching…</span></div>`;
    let r;
    try { r = await api("fm_find", { start: q.start, name: q.name === "*" ? "" : q.name, text: q.text, regex: q.regex, hidden: q.hidden, case: q.case }); } catch (e) { r = { error: e.message }; }
    if (r.error) { prog.innerHTML = ""; errEl.textContent = r.error; errEl.hidden = false; btn.disabled = false; return; }
    job = r.job; stopB.hidden = false;
    FM.findHook = () => {
      const j = FM.jobs.find(x => x.id === job);
      const el = $("#fmFPT");
      if (j && el) el.textContent = `Searching… ${(j.result?.rows?.length ?? j.done?.n ?? 0).toLocaleString()} found${j.current ? ` · ${baseName(j.current)}` : ""}`;
    };
    FM.pending.set(job, { op: "find", onDone: j => {
      FM.findHook = null;
      rows = j.result?.rows || [];
      if (!d.open) { if (rows.length) toast(`Find finished: ${items(rows.length)}`, "ok", "", { label: "Show", run: () => { const P2 = AP(); const nt = newTab(q.start, { kind: "find", label: q.name || q.text }); nt.rows = rows; nt.loaded = true; nt.crumbs = [{ n: baseName(q.start) || q.start, path: q.start }]; sortRows(nt); applyFilter(nt, true); addTab(P2, null, { tab: nt }); } }); return; }
      stopB.hidden = true; btn.disabled = false;
      prog.innerHTML = `<b>${j.state === "cancelled" ? "Stopped. " : ""}${rows.length ? `${items(rows.length)} found` : "Nothing was found."}${rows.length >= 20000 ? " (the first 20,000)" : ""}</b>`;
      showB.hidden = !rows.length;
      if (rows.length) showB.focus();
    } });
    pollJobs();
  };
}

/* ------------------------------------------------------------------ multi-rename (Ctrl+M) */
function multiRename(rowsIn, PIn) {
  const P = PIn || AP(), t = tabOf(P), rows = sel(t, rowsIn);
  if (!rows.length) return;
  const prot = rows.filter(r => r.p);
  const d = dlg("fm-wide", `<h3>Rename ${items(rows.length)}</h3>
    <div class="fm-row2">
      <label class="fm-field"><span>Name</span><input type="text" id="fmRN" value="[N]" spellcheck="false" autocomplete="off"></label>
      <label class="fm-field"><span>Extension</span><input type="text" id="fmRE" value="[E]" spellcheck="false" autocomplete="off"></label>
    </div>
    <p class="fm-note">[N] is the old name, [E] the extension, [C] a counter, [P] the parent folder's name.</p>
    <div class="fm-row2">
      <label class="fm-field"><span>Find</span><input type="text" id="fmRF" spellcheck="false" autocomplete="off"></label>
      <label class="fm-field"><span>Replace with</span><input type="text" id="fmRR" spellcheck="false" autocomplete="off"></label>
    </div>
    <div class="fm-checks"><label><input type="checkbox" id="fmRX"> Regex</label><label><input type="checkbox" id="fmRCs"> Match case</label></div>
    <div class="fm-row2">
      <label class="fm-field"><span>Letter case</span><select id="fmRK"><option value="">Unchanged</option><option value="lower">lower case</option><option value="upper">UPPER CASE</option><option value="title">Title Case</option></select></label>
      <label class="fm-field"><span>Counter starts at</span><input type="number" id="fmRC1" value="1" min="0"></label>
      <label class="fm-field"><span>Step</span><input type="number" id="fmRC2" value="1" min="1"></label>
      <label class="fm-field"><span>Digits</span><input type="number" id="fmRC3" value="1" min="1" max="9"></label>
    </div>
    <div class="fm-mr" id="fmRT"></div>
    <p class="fm-note" id="fmRS"></p>
    ${prot.length ? `<div class="fm-blocked"><b>${items(prot.length)} protected: they keep their names.</b></div>` : ""}
    <p class="form-error" id="fmErr" hidden></p>
    <div class="row-actions"><button class="btn" data-cancel>Cancel</button><button class="btn primary" data-primary disabled>Rename</button></div>`);
  const btn = d.querySelector("[data-primary]"), errEl = $("#fmErr");
  const val = id => $(id).value;
  const work = rows.filter(r => !r.p);
  const existing = new Set(t.rows.map(r => norm(rowPath(t, r))));
  let plan = [];
  const bad = nm => !nm ? "Empty name" : /[\\/]/.test(nm) || (FM.ci && /[:*?"<>|]/.test(nm)) ? "Can't contain \\ / : * ? \" < > |"
    : nm === "." || nm === ".." ? "Not a valid name" : FM.ci && /[. ]$/.test(nm) ? "Can't end with a dot or space" : "";
  function compute() {
    errEl.hidden = true;
    let re = null;
    const f = val("#fmRF");
    if (f) { try { re = new RegExp($("#fmRX").checked ? f : escRe(f), $("#fmRCs").checked ? "g" : "gi"); } catch { errEl.textContent = "That regex isn't valid."; errEl.hidden = false; } }
    const c1 = +val("#fmRC1") || 0, c2 = +val("#fmRC2") || 1, c3 = clamp(+val("#fmRC3") || 1, 1, 9), kc = val("#fmRK");
    const seen = new Map();
    plan = work.map((r, i) => {
      const old = baseName(r.n), dot = isDir(r) ? -1 : old.lastIndexOf(".");
      const N = dot > 0 ? old.slice(0, dot) : old, E = dot > 0 ? old.slice(dot + 1) : "";
      const src = rowPath(t, r), par = baseName(dirName(src));
      const sub = s => s.replace(/\[N\]/g, N).replace(/\[E\]/g, E).replace(/\[P\]/g, par).replace(/\[C\]/g, String(c1 + i * c2).padStart(c3, "0"));
      let base = sub(val("#fmRN")), ext = sub(val("#fmRE"));
      let nm = ext ? `${base}.${ext}` : base;
      if (re) nm = nm.replace(re, val("#fmRR"));
      if (kc === "lower") nm = nm.toLowerCase(); else if (kc === "upper") nm = nm.toUpperCase();
      else if (kc === "title") nm = nm.toLowerCase().replace(/(^|[\s_\-.(])(\p{L})/gu, (m, a, b) => a + b.toUpperCase());
      nm = nm.trim();
      const target = join(dirName(src), nm);
      return { r, src, old, nm, target, why: bad(nm) };
    });
    for (const p of plan) {
      if (p.why || p.nm === p.old) continue;
      const k = norm(p.target);
      if (seen.has(k)) { p.why = "Two items would get this name"; seen.get(k).why ||= "Two items would get this name"; }
      else seen.set(k, p);
      if (!p.why && existing.has(k) && !(FM.ci && norm(p.nm) === norm(p.old))) p.why = "Something with this name is already here";
    }
    const changed = plan.filter(p => p.nm !== p.old), bads = plan.filter(p => p.why);
    $("#fmRT").innerHTML = `<table><thead><tr><th scope="col">Before</th><th scope="col">After</th></tr></thead><tbody>${plan.slice(0, 1000).map(p =>
      `<tr class="${p.why ? "bad" : p.nm === p.old ? "same" : ""}"><td>${esc(p.old)}</td><td>${esc(p.nm)}${p.why ? `<small>${esc(p.why)}</small>` : ""}</td></tr>`).join("")}</tbody></table>`;
    $("#fmRS").textContent = `${items(changed.length)} will be renamed${plan.length > 1000 ? " (the table shows the first 1,000)" : ""}.${bads.length ? ` ${plural(bads.length, "problem")} to fix first.` : ""}`;
    btn.disabled = !!bads.length || !changed.length || !errEl.hidden;
    btn.textContent = changed.length ? `Rename ${items(changed.length)}` : "Rename";
  }
  d.querySelectorAll("input, select").forEach(x => x.addEventListener("input", compute));
  $("#fmRN").focus(); $("#fmRN").select();
  compute();
  btn.onclick = async () => {
    const ch = plan.filter(p => p.nm !== p.old);
    if (!ch.length) return;
    btn.disabled = true;
    let pl;
    try { pl = await api("fm_plan", { op: "rename_many", sources: ch.map(p => p.src), renames: ch.map(p => [p.src, p.nm]) }); } catch (e) { pl = { error: e.message }; }
    const fail = m => { errEl.textContent = m; errEl.hidden = false; btn.disabled = false; };
    if (pl.error) return fail(pl.error);
    if (pl.conflicts?.length) return fail(`${items(pl.conflicts.length)} would replace something that's already there. Change the pattern.`);
    if (pl.blocked?.length) return fail(`${items(pl.blocked.length)} can't be renamed: ${pl.blocked[0].reason}`);
    let st;
    try { st = await api("fm_start", { plan: pl.plan, exclude: [], conflict: "skip", confirm: false }); } catch (e) { st = { error: e.message }; }
    if (st.error) return fail(st.error);
    FM.pending.set(st.job, { op: "rename", sources: ch.map(p => p.src), P, t });
    d.close();
    announce(`Renaming ${items(ch.length)}.`);
    pollJobs();
  };
}

/* ------------------------------------------------------------------ palette, help, saved locations */
function palette() {
  const list = Object.values(CMD).filter(c => c.id !== "palette");
  const d = dlg("", `<h3 class="sr" id="fmPalT">Command palette</h3>
    <input class="fm-pal-in" id="fmPalIn" role="combobox" aria-expanded="true" aria-controls="fmPalL" aria-autocomplete="list" aria-labelledby="fmPalT" placeholder="Type a command" autocomplete="off" spellcheck="false">
    <ul class="fm-pal" id="fmPalL" role="listbox" aria-label="Commands"></ul>`, { onClose: () => { const f = next; next = null; f?.(); } });
  let next = null, shown = [], idx = 0;
  const inp = $("#fmPalIn"), ul = $("#fmPalL");
  const draw = () => {
    const q = inp.value.trim().toLowerCase();
    const m = q ? matcher(q, "fuzzy") : null;
    shown = list.filter(c => !m || m(c.label) || (c.keys && c.keys.toLowerCase().includes(q)));
    if (q) shown.sort((a, b) => (b.label.toLowerCase().startsWith(q) - a.label.toLowerCase().startsWith(q)) || (b.label.toLowerCase().includes(q) - a.label.toLowerCase().includes(q)));
    idx = clamp(idx, 0, Math.max(0, shown.length - 1));
    ul.innerHTML = shown.map((c, i) => { const why = c.why?.(); return `<li role="option" id="fmPo${i}" aria-selected="${i === idx}" ${why ? 'aria-disabled="true"' : ""} data-i="${i}">
      <span class="lb"><span>${esc(c.label)}</span>${why ? `<small>${esc(why)}</small>` : ""}</span>${c.keys ? `<kbd>${esc(c.keys)}</kbd>` : ""}</li>`; }).join("") || `<li aria-disabled="true">No command matches.</li>`;
    if (shown.length) inp.setAttribute("aria-activedescendant", "fmPo" + idx); else inp.removeAttribute("aria-activedescendant");
    ul.querySelector(`[data-i="${idx}"]`)?.scrollIntoView({ block: "nearest" });
  };
  const pick = i => { const c = shown[i]; if (!c) return; const why = c.why?.(); if (why) { toast(why); return; } next = () => run(c.id); d.close(); };
  inp.addEventListener("input", () => { idx = 0; draw(); });
  inp.addEventListener("keydown", e => {
    if (e.key === "ArrowDown") { e.preventDefault(); idx = Math.min(shown.length - 1, idx + 1); draw(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); idx = Math.max(0, idx - 1); draw(); }
    else if (e.key === "Enter") { e.preventDefault(); e.stopPropagation(); pick(idx); }
  });
  ul.addEventListener("click", e => { const li = e.target.closest("[data-i]"); if (li) pick(+li.dataset.i); });
  draw(); inp.focus();
}
function helpDlg() {
  const groups = {};
  for (const c of Object.values(CMD)) if (c.keys) (groups[c.group || "General"] ||= []).push(c);
  const nav = [["Tab", "Switch panes"], ["Arrows, PgUp/PgDn, Home/End", "Move the cursor (with Shift: tag as you go)"], ["Enter", "Open a folder, or a file with its usual app"],
    ["Backspace", "Parent folder"], ["Ctrl+PgUp / Ctrl+PgDn", "Parent / open"], ["Alt+G / R / J", "First / middle / last visible row"],
    ["Insert or Ctrl+T", "Tag and move down"], ["Typing letters", "Jump to the first name that starts with them"], ["Escape, then 1–9 or 0", "Same as F1–F10"]];
  const row = (k, l) => `<div><span>${esc(l)}</span><kbd>${esc(k)}</kbd></div>`;
  dlg("fm-wide", `<h3>Keyboard</h3><p>Files puts its keys first while it's open. Typing in a box, a dialog or the viewer uses their own keys.</p>
    <div class="fm-keys"><h4>Moving around</h4>${nav.map(([k, l]) => row(k, l)).join("")}
    ${Object.entries(groups).map(([g, cs]) => `<h4>${esc(g)}</h4>${cs.map(c => row(c.keys, c.label)).join("")}`).join("")}
    <h4>Viewer</h4>${row("F3 / Shift+F3", "Next / previous match")}${row("F4", "Switch between text and hex")}${row("Ctrl+PgUp / Ctrl+PgDn", "Previous / next file")}${row("Escape or F10", "Close")}</div>
    <p class="fm-note" style="margin-top:12px">F4 Edit isn't available yet: view with F3, or open the file with its usual app.</p>
    <div class="row-actions"><button class="btn primary" data-cancel>Close</button></div>`);
  $("#dlg [data-cancel]").focus({ preventScroll: true });
  $("#dlg").scrollTop = 0;
}
function toggleHot(P, onlyAdd) {
  const t = tabOf(P);
  if (t.kind === "find") return;
  const i = FM.hotlist.findIndex(h => sameP(h.path, t.path));
  if (i >= 0 && !onlyAdd) { FM.hotlist.splice(i, 1); announce("Removed from saved locations."); }
  else if (i < 0) { FM.hotlist.push({ name: baseName(t.path) || t.path, path: t.path }); announce(`Saved ${t.path}.`); }
  FM.panes.forEach(renderNav);
  savePrefs();
}
function hotlistDlg() {
  const P = AP();
  let next = null;
  const d = dlg("", `<h3>Saved locations</h3><ul class="fm-hot" id="fmHot"></ul>
    <div class="row-actions"><button class="btn" id="fmHAdd">Add current folder</button><span class="sp"></span><button class="btn primary" data-cancel>Close</button></div>`,
    { onClose: () => { const f = next; next = null; f?.(); } });
  const draw = () => {
    $("#fmHot").innerHTML = FM.hotlist.length ? FM.hotlist.map((h, i) => `<li><button data-go="${i}"><span>${esc(h.name)}</span><small>${esc(h.path)}</small></button>
      <button class="btn" data-ren="${i}" aria-label="Rename ${esc(h.name)}">Rename</button><button class="btn" data-rm="${i}" aria-label="Remove ${esc(h.name)}">Remove</button></li>`).join("")
      : `<li class="tnote">Nothing saved yet. Add folders you visit often.</li>`;
    $("#fmHAdd").disabled = AT().kind === "find" || FM.hotlist.some(h => sameP(h.path, AT().path));
  };
  d.onclick = e => {
    const g = e.target.closest("[data-go]"), rn = e.target.closest("[data-ren]"), rm = e.target.closest("[data-rm]");
    if (g) { const h = FM.hotlist[+g.dataset.go]; next = () => go(P, h.path).then(() => focusGrid(P)); d.close(); }
    else if (rm) { FM.hotlist.splice(+rm.dataset.rm, 1); draw(); FM.panes.forEach(renderNav); savePrefs(); $("#fmHAdd").focus(); }
    else if (rn) {
      const i = +rn.dataset.ren, li = rn.closest("li"), h = FM.hotlist[i];
      li.innerHTML = `<input type="text" class="fm-pal-in" style="height:32px;font-size:13px" value="${esc(h.name)}" aria-label="New name for ${esc(h.name)}"><button class="btn primary" data-ok>Save</button><button class="btn" data-no>Cancel</button>`;
      const inp = li.querySelector("input");
      inp.focus(); inp.select();
      const save = () => { h.name = inp.value.trim() || h.name; draw(); savePrefs(); };
      li.querySelector("[data-ok]").onclick = save;
      li.querySelector("[data-no]").onclick = draw;
      inp.addEventListener("keydown", ev => { if (ev.key === "Enter") { ev.preventDefault(); ev.stopPropagation(); save(); } else if (ev.key === "Escape") { ev.preventDefault(); draw(); } });
    }
  };
  $("#fmHAdd").onclick = () => { toggleHot(P, true); draw(); };
  draw();
  (d.querySelector("[data-go]") || $("#fmHAdd")).focus();
}

/* ------------------------------------------------------------------ preferences */
let saveT = 0;
function savePrefs() {
  if (!FM.ready) return;
  clearTimeout(saveT);
  saveT = setTimeout(() => {
    const pane = P => {
      const tabs = P.tabs.filter(t => t.kind === "dir" && t.path);
      const act = tabs.indexOf(tabOf(P));
      return { tabs: tabs.map(t => ({ path: t.path, sort: t.sort, desc: t.desc, biggest: t.biggest, listing: t.listing })), active: Math.max(0, act), on: P === AP() };
    };
    const prefs = { left: { ...pane(FM.panes[0]), split: FM.split, cols: FM.cols }, right: pane(FM.panes[1]), hotlist: FM.hotlist,
                    history: { left: FM.panes[0].hist, right: FM.panes[1].hist }, density: FM.density, hidden: FM.hidden };
    api("fm_prefs", { set: prefs }).catch(() => {});
  }, 700);
}

/* ------------------------------------------------------------------ boot */
async function init() {
  build();
  defineCommands();
  renderFkeys();
  FM.root.querySelector(".fm-panes").hidden = true;
  const boot = ce("div", "fm-boot", `<div class="loading">${SPIN}Opening Files…</div>`);
  FM.root.prepend(boot);
  let roots, prefs = {};
  try {
    const [a, b] = await Promise.all([api("fm_roots"), api("fm_prefs").catch(() => ({}))]);
    if (a.error) throw new Error(a.error);
    roots = a; prefs = b.prefs || {};
  } catch (e) {
    boot.innerHTML = `<div class="card"><h2>Files couldn't open</h2><p>${esc(e.message)}</p><button class="btn primary" id="fmRetry">Try again</button></div>`;
    $("#fmRetry").onclick = () => { FM.built = false; FM.hjob?.remove(); init(); };
    return;
  }
  boot.remove();
  FM.root.querySelector(".fm-panes").hidden = false;
  Object.assign(FM, { roots: roots.roots || [], home: roots.home || "", sep: roots.sep || FM.sep, trashName: roots.trashName || FM.trashName, scanTime: roots.scanTime || {} });
  FM.ci = FM.sep === "\\";
  CMD.trash.label = `Move to the ${binName()}`; CMD.trash.tip = CMD.trash.label;
  FM.hotlist = Array.isArray(prefs.hotlist) ? prefs.hotlist.filter(h => h && h.path) : [];
  FM.density = prefs.density === "comfortable" ? "comfortable" : "compact";
  FM.rh = FM.density === "comfortable" ? 34 : 28;
  FM.root.classList.toggle("comfy", FM.density === "comfortable");
  FM.hidden = !!prefs.hidden;
  const L = prefs.left || {};
  FM.cols = L.cols && typeof L.cols === "object" ? L.cols : {};
  applyCols();
  setSplit(typeof L.split === "number" ? L.split : .5);
  const homeRoot = rootFor(FM.home);
  const other = FM.roots.find(r => r !== homeRoot)?.path || FM.home || FM.roots[0]?.path || "";
  [0, 1].forEach(side => {
    const P = FM.panes[side], pp = prefs[side ? "right" : "left"] || {};
    const tabs = Array.isArray(pp.tabs) ? pp.tabs.filter(x => x && x.path) : [];
    P.tabs = tabs.length ? tabs.map(x => newTab(x.path, x)) : [newTab(side ? other : FM.home || FM.roots[0]?.path || "")];
    P.ti = clamp(+pp.active || 0, 0, P.tabs.length - 1);
    P.hist = Array.isArray(prefs.history?.[side ? "right" : "left"]) ? prefs.history[side ? "right" : "left"].slice(0, 30) : [];
  });
  FM.active = prefs.right?.on ? 1 : 0;
  renderAllPanes();
  FM.ready = true;
  await Promise.all(FM.panes.map(P => load(P, tabOf(P), tabOf(P).path, { focus: null })));
  focusGrid();
  pollJobs();
}
addEventListener("keydown", onKey, true);
addEventListener("focus", () => {
  if (S.mode !== "files" || !FM.ready) return;
  for (const P of FM.panes) { const t = tabOf(P); if (t.kind === "dir" && t.loaded && !t.loading && t.rows.length < 20000) load(P, t, t.path, { keep: true }); }
});

window.HoardFM = {
  show() {
    if (!FM.built) return init();
    renderJobs();
    for (const P of FM.panes) { const t = tabOf(P); renderPane(P); if (t.stale) load(P, t, t.path, { keep: true }); }
    requestAnimationFrame(() => focusGrid());
    pollJobs();
  },
  hide() { endRename(); closeQuick(); FM.tip && (FM.tip.hidden = true); renderJobs(); },
  openRoot(path) {
    if (!FM.ready) return;
    const P = AP();
    go(P, path).then(() => focusGrid(P));
  },
};
})();
