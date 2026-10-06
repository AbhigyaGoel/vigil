// The roles board: every role vigil surfaced, pushed or not, newest first.
// Server hands over pre-formatted rows; the page only filters and renders them.
// Job data is external (scraped titles/companies), so the client builds DOM with
// textContent - never innerHTML - and only links http(s) Apply URLs.

const CSS = `
:root{--bg:#f7f7f5;--card:#fff;--ink:#1c1c1a;--mut:#6b6b66;--line:#e4e4df;--acc:#2f6f4f;--acc-ink:#fff;--chip:#eef4ef;--chip-ink:#24543c;--new:#2f6f4f;--big:#efefea}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#121211;--card:#1b1b19;--ink:#ecece6;--mut:#9a9a92;--line:#2c2c29;--acc:#5fae86;--acc-ink:#0d1a13;--chip:#1e2b23;--chip-ink:#9fd3b6;--new:#5fae86;--big:#24241f}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:760px;margin:0 auto;padding:20px 16px 64px}
h1{font-size:20px;margin:0}
.sub{color:var(--mut);font-size:13px;margin:2px 0 16px}
.bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:8px}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:8px;overflow:hidden}
.seg button{border:0;background:var(--card);color:var(--ink);padding:7px 12px;font:inherit;font-size:13px;cursor:pointer}
.seg button+button{border-left:1px solid var(--line)}
.seg button[aria-pressed="true"]{background:var(--acc);color:var(--acc-ink)}
input[type=search]{flex:1;min-width:160px;padding:7px 10px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--ink);font:inherit;font-size:14px}
label.tog{font-size:13px;color:var(--mut);display:flex;gap:5px;align-items:center}
.count{color:var(--mut);font-size:13px;margin:4px 0 12px}
h2{font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:var(--mut);margin:22px 0 8px;font-weight:600}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin-bottom:8px;border-left:3px solid var(--line)}
.card.new{border-left-color:var(--new)}
.card.big{background:var(--big)}
.top{display:flex;justify-content:space-between;gap:10px;align-items:baseline}
.co{font-weight:650}
.tag{font-size:11px;color:var(--mut);border:1px solid var(--line);border-radius:4px;padding:0 5px;margin-left:6px;font-weight:400;white-space:nowrap}
.ti{margin:2px 0 6px}
.meta{display:flex;flex-wrap:wrap;gap:4px 12px;font-size:13px;color:var(--mut)}
.pay{background:var(--chip);color:var(--chip-ink);border-radius:5px;padding:0 6px;font-weight:600}
.acts{display:flex;gap:8px;margin-top:10px}
.acts a,.acts button{font:inherit;font-size:13px;border-radius:7px;padding:5px 12px;cursor:pointer;text-decoration:none}
.acts a{background:var(--acc);color:var(--acc-ink)}
.acts button{background:transparent;color:var(--mut);border:1px solid var(--line)}
.empty{color:var(--mut);padding:24px 0}
`;

const JS = `
const ROWS = window.__ROWS__;
const LS = (k, v) => { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) { return null; } };
const hidden = new Set(JSON.parse(LS("vigil_hidden") || "[]"));
const lastVisit = Number(LS("vigil_last_visit") || 0);
let view = LS("vigil_view") || "startups";
let showHidden = false;
const $ = (s) => document.querySelector(s);
const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };

function keep(r, q) {
  if (!showHidden && hidden.has(r.id)) return false;
  if (view === "startups" && r.bigco) return false;
  if (view === "pushed" && !(r.delivery === "high" || r.delivery === "low")) return false;
  if (q && !(r.company + " " + r.title + " " + r.location).toLowerCase().includes(q)) return false;
  return true;
}

function card(r) {
  const c = el("div", "card" + (r.found > lastVisit && lastVisit ? " new" : "") + (r.bigco ? " big" : ""));
  const top = el("div", "top");
  const co = el("div", "co", r.company);
  if (r.bigco) co.appendChild(el("span", "tag", "Big co"));
  if (r.delivery === "high") co.appendChild(el("span", "tag", "Pushed"));
  top.appendChild(co);
  c.appendChild(top);
  c.appendChild(el("div", "ti", r.title));
  const m = el("div", "meta");
  m.appendChild(el("span", r.pay ? "pay" : "", r.pay || "Pay not listed"));
  if (r.location) m.appendChild(el("span", "", r.location));
  if (r.season) m.appendChild(el("span", "", r.season));
  if (r.postedStr) m.appendChild(el("span", "", "Posted " + r.postedStr));
  m.appendChild(el("span", "", r.foundStr ? "Found " + r.foundStr : "Found before tracking"));
  c.appendChild(m);
  const a = el("div", "acts");
  if (/^https?:\\/\\//.test(r.url)) {
    const ap = el("a", "", "Apply"); ap.href = r.url; ap.target = "_blank"; ap.rel = "noopener noreferrer"; a.appendChild(ap);
  }
  const h = el("button", "", hidden.has(r.id) ? "Unhide" : "Hide");
  h.onclick = () => { hidden.has(r.id) ? hidden.delete(r.id) : hidden.add(r.id); LS("vigil_hidden", JSON.stringify([...hidden])); render(); };
  a.appendChild(h);
  c.appendChild(a);
  return c;
}

function render() {
  const q = $("#q").value.trim().toLowerCase();
  const list = $("#list");
  list.replaceChildren();
  const rows = ROWS.filter((r) => keep(r, q));
  $("#count").textContent = rows.length + " role" + (rows.length === 1 ? "" : "s");
  document.querySelectorAll(".seg button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.v === view)));
  if (!rows.length) { list.appendChild(el("div", "empty", "Nothing here with these filters.")); return; }
  let day = null;
  for (const r of rows) {
    if (r.day !== day) { day = r.day; list.appendChild(el("h2", "", day)); }
    list.appendChild(card(r));
  }
}

document.querySelectorAll(".seg button").forEach((b) => b.onclick = () => { view = b.dataset.v; LS("vigil_view", view); render(); });
$("#q").oninput = render;
$("#sh").onchange = (e) => { showHidden = e.target.checked; render(); };
render();
LS("vigil_last_visit", String(Math.floor(Date.now() / 1000)));
`;

export function renderBoard(rows, updatedStr) {
  const data = JSON.stringify(rows).replace(/</g, "\\u003c").replace(/[\u2028\u2029]/g, " ");
  return `<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>vigil roles</title>
<style>${CSS}</style></head>
<body><div class="wrap">
<h1>vigil</h1>
<div class="sub">Internship roles found by vigil · updated ${updatedStr}</div>
<div class="bar">
  <div class="seg" role="group" aria-label="Which roles">
    <button data-v="startups">Startups</button><button data-v="pushed">Pushed</button><button data-v="all">All</button>
  </div>
  <input id="q" type="search" placeholder="Search company, title, city" aria-label="Search">
  <label class="tog"><input id="sh" type="checkbox"> Show hidden</label>
</div>
<div id="count" class="count"></div>
<div id="list"></div>
</div>
<script>window.__ROWS__=${data};</script>
<script>${JS}</script>
</body></html>`;
}
