"use strict";
/* Stock Journal — thesis journal, financial health grades, portfolio, news.
   Market data comes from data/market.json (built daily by a GitHub Action).
   Your journal lives in this browser and, if you add a token, syncs to a private repo. */

const CFG_KEY = "sj.cfg", J_KEY = "sj.journal";
const DEFAULT_WEIGHTS = { growth: 20, profit: 20, cash: 20, balance: 25, dilution: 15 };
const PART_LABELS = { growth: "Growth", profit: "Profitability", cash: "Cash generation", balance: "Balance sheet", dilution: "Share count" };
const DEFAULT_CFG = { owner: "max-stax", appRepo: "Stock-Journal", dataRepo: "Stock-Journal-data", token: "", learn: true, weights: DEFAULT_WEIGHTS };

let cfg = Object.assign({}, DEFAULT_CFG, readLS(CFG_KEY) || {});
cfg.weights = Object.assign({}, DEFAULT_WEIGHTS, cfg.weights || {});
let J = readLS(J_KEY) || newJournal();
let M = { generated: null, tickers: {} };   // market.json
let LIST = [];                               // tickers.json entries
const ui = { tab: "portfolio", sym: null, sub: "thesis", filter: "all", syncState: "" };

function newJournal() { return { version: 1, updated: null, holdings: {}, stocks: {}, extraTickers: [] }; }
function readLS(k) { try { return JSON.parse(localStorage.getItem(k)); } catch { return null; } }
function writeLS(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} }

// ------------------------------------------------------------ formatting
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
function money(n, dp) {
  if (n == null || isNaN(n)) return "—";
  const a = Math.abs(n), s = n < 0 ? "−" : "";
  if (a >= 1e12) return `${s}$${(a / 1e12).toFixed(2)}T`;
  if (a >= 1e9) return `${s}$${(a / 1e9).toFixed(a >= 1e11 ? 0 : 1)}B`;
  if (a >= 1e6) return `${s}$${(a / 1e6).toFixed(a >= 1e8 ? 0 : 1)}M`;
  if (a >= 1e4 && dp == null) return `${s}$${Math.round(a).toLocaleString("en-US")}`;
  return `${s}$${a.toLocaleString("en-US", { minimumFractionDigits: dp ?? 2, maximumFractionDigits: dp ?? 2 })}`;
}
const pct = (n, dp = 1, sign = true) => n == null || isNaN(n) ? "—" : `${sign && n > 0 ? "+" : n < 0 ? "−" : ""}${Math.abs(n).toFixed(dp)}%`;
const cls = n => n == null ? "" : n > 0 ? "pos" : n < 0 ? "neg" : "";
const today = () => new Date().toISOString().slice(0, 10);
function fmtDate(iso) { if (!iso) return "—"; const d = new Date(iso.length === 10 ? iso + "T12:00:00" : iso); return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" }); }
function rel(iso) {
  if (!iso) return ""; const h = (Date.now() - new Date(iso)) / 36e5;
  if (h < 1) return "just now"; if (h < 24) return `${Math.round(h)}h ago`; const d = Math.round(h / 24);
  return d < 30 ? `${d}d ago` : fmtDate(iso);
}
function toast(msg) { const t = document.createElement("div"); t.className = "toast"; t.textContent = msg; document.body.appendChild(t); setTimeout(() => t.remove(), 2600); }
const gClass = l => l ? "g" + l : "gN";

// ------------------------------------------------------------ data access
function symbols() {
  const s = new Set(LIST.map(t => t.symbol));
  (J.extraTickers || []).forEach(x => s.add(x));
  Object.keys(J.holdings).forEach(x => s.add(x));
  return [...s];
}
function rec(sym) { return M.tickers[sym] || { symbol: sym }; }
function stock(sym) {
  if (!J.stocks[sym]) J.stocks[sym] = { status: "watching", thesis: {}, entries: [] };
  return J.stocks[sym];
}
function callout(sym) { return (LIST.find(t => t.symbol === sym) || {}).callout; }
function priceOf(sym) { return rec(sym).price?.last ?? null; }
function reviewDue(st) { return st?.thesis?.review && st.thesis.review <= today(); }

function ctxFor(sym) {
  const r = rec(sym), f = r.fundamentals || {}, t = f.ttm || {}, b = f.balance || {}, v = f.valuation || {};
  const last = (f.fy || [])[(f.fy || []).length - 1] || {};
  return {
    name: shortName(r.name) || sym, rev: t.revenue, growth: t.revenueGrowth, gm: t.grossMargin, om: t.operatingMargin,
    ni: t.netIncome, fcf: t.fcf, fm: t.fcfMargin, sbc: t.sbc, nc: b.netCash, cr: b.currentRatio, de: b.debtToEquity,
    sc: last.shareChange, mc: v.marketCap, ps: v.priceToSales, evs: v.evToSales, pfcf: v.priceToFcf, pe: v.pe,
    gradeScore: f.grade?.score, gradeLetter: f.grade?.letter,
    ta: b.totalAssets, tl: b.totalLiabilities, eq: b.equity, la: b.liabToAssets, ish: b.intangibleShare,
    bvg: b.bvpsGrowth, roe: last.roe, pb: v.priceToBook, eps: t.eps,
  };
}
function shortName(n) { return (n || "").replace(/[,.]?\s+(inc|corp|corporation|holdings|group|ltd|plc|n\.?v|co|company)\.?$/i, "").replace(/\b\w+/g, w => w.length > 3 && w === w.toUpperCase() ? w[0] + w.slice(1).toLowerCase() : w); }

// Grades arrive with each part's 0–1 ratio; the score is re-weighted here with your weights.
const letterOf = s => s == null ? null : s >= 80 ? "A" : s >= 65 ? "B" : s >= 50 ? "C" : s >= 35 ? "D" : "F";
function regrade(g) {
  if (!g?.parts) return;
  const w = cfg.weights, total = Object.values(w).reduce((a, b) => a + b, 0);
  g.parts.forEach(p => { p.max = w[p.key] ?? 0; p.score = (p.ratio ?? 0) * p.max; });
  const mx = g.parts.reduce((a, p) => a + p.max, 0);
  g.coverage = mx; g.coveragePct = total ? mx / total : 0;
  g.score = mx >= total * 0.4 && mx > 0 ? Math.round(g.parts.reduce((a, p) => a + p.score, 0) / mx * 100) : null;
  g.letter = letterOf(g.score);
}
function applyWeights() {
  Object.values(M.tickers || {}).forEach(r => { const f = r.fundamentals; if (!f) return; regrade(f.grade); (f.fy || []).forEach(x => regrade(x.grade)); });
}

// ------------------------------------------------------------ persistence + sync
let pushTimer = null;
function save() {
  J.updated = new Date().toISOString();
  writeLS(J_KEY, J);
  if (cfg.token) { clearTimeout(pushTimer); pushTimer = setTimeout(push, 1500); setSync("saving…"); }
}
function setSync(s) { ui.syncState = s; const el = document.getElementById("sync"); if (el) el.textContent = s; }

const b64e = s => btoa(unescape(encodeURIComponent(s)));
const b64d = s => decodeURIComponent(escape(atob(s.replace(/\n/g, ""))));
async function gh(method, repo, path, body) {
  const r = await fetch(`https://api.github.com/repos/${cfg.owner}/${repo}/contents/${path}`, {
    method, headers: { Authorization: `Bearer ${cfg.token}`, Accept: "application/vnd.github+json", "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined, cache: "no-store",
  });
  if (r.status === 404 && method === "GET") return null;
  if (!r.ok) throw new Error(`GitHub ${r.status}: ${(await r.text()).slice(0, 120)}`);
  return r.json();
}
let remoteSha = null;
async function pull() {
  if (!cfg.token) return;
  try {
    setSync("syncing…");
    const f = await gh("GET", cfg.dataRepo, "journal.json");
    if (!f) { remoteSha = null; await push(); return; }
    remoteSha = f.sha;
    const remote = JSON.parse(b64d(f.content));
    if (!J.updated || (remote.updated && remote.updated > J.updated)) { J = remote; writeLS(J_KEY, J); render(); }
    else if (remote.updated !== J.updated) await push();
    setSync("synced");
  } catch (e) { setSync("sync error"); console.warn(e); }
}
async function push() {
  if (!cfg.token) return;
  try {
    const body = { message: `journal ${new Date().toISOString()}`, content: b64e(JSON.stringify(J, null, 1)) };
    if (remoteSha) body.sha = remoteSha;
    const res = await gh("PUT", cfg.dataRepo, "journal.json", body);
    remoteSha = res.content.sha; setSync("synced");
  } catch (e) {
    if (/409|422/.test(e.message)) { remoteSha = null; const f = await gh("GET", cfg.dataRepo, "journal.json").catch(() => null); remoteSha = f?.sha || null; if (remoteSha) return push(); }
    setSync("sync error"); console.warn(e);
  }
}
async function addTickerRemote(sym) {
  // Commits the ticker to tickers.json in the app repo so the daily job starts fetching it.
  const f = await gh("GET", cfg.appRepo, "data/tickers.json");
  const data = JSON.parse(b64d(f.content));
  if (data.tickers.some(t => t.symbol === sym)) return false;
  data.tickers.push({ symbol: sym });
  await gh("PUT", cfg.appRepo, "data/tickers.json", { message: `Add ${sym}`, content: b64e(JSON.stringify(data, null, 2) + "\n"), sha: f.sha });
  LIST = data.tickers; return true;
}

// ------------------------------------------------------------ charts
function spark(hist, w = 320, h = 56) {
  if (!hist || hist.length < 2) return "";
  const ys = hist.map(p => p[1]), lo = Math.min(...ys), hi = Math.max(...ys), up = ys[ys.length - 1] >= ys[0];
  const pts = ys.map((y, i) => `${(i / (ys.length - 1) * w).toFixed(1)},${(h - 4 - (y - lo) / (hi - lo || 1) * (h - 8)).toFixed(1)}`).join(" ");
  return `<svg viewBox="0 0 ${w} ${h}" width="100%" height="${h}" preserveAspectRatio="none" aria-hidden="true"><polyline points="${pts}" fill="none" stroke="var(--${up ? "pos" : "neg"})" stroke-width="1.8" vector-effect="non-scaling-stroke" stroke-linejoin="round"/></svg>`;
}
const gradeColor = l => `var(--g${l || "C"})`;
function priceAt(monthly, iso) {
  let best = null; for (const [d, c] of monthly) { if (d <= iso) best = c; else break; } return best;
}
function addDays(iso, n) { const d = new Date(iso + "T12:00:00"); d.setDate(d.getDate() + n); return d.toISOString().slice(0, 10); }
function gradeVsPrice(sym) {
  const r = rec(sym), mo = r.price?.monthly || [], fy = (r.fundamentals?.fy || []).filter(x => x.grade?.score != null);
  return fy.map(x => {
    const known = addDays(x.end, 60), later = addDays(known, 365);
    const p0 = priceAt(mo, known), p1 = later <= today() ? priceAt(mo, later) : priceOf(sym);
    return { end: x.end, known, grade: x.grade, p0, p1, partial: later > today(), ret: p0 && p1 ? (p1 / p0 - 1) * 100 : null };
  });
}
function gradeChart(sym) {
  const mo = rec(sym).price?.monthly || []; if (mo.length < 3) return `<p class="mute">Price history not loaded yet.</p>`;
  const rows = gradeVsPrice(sym), W = 340, H = 170, L = 38, R = 8, T = 22, B = 20;
  const t0 = new Date(mo[0][0]).getTime(), t1 = new Date(mo[mo.length - 1][0]).getTime();
  const ys = mo.map(p => p[1]), lo = Math.min(...ys), hi = Math.max(...ys);
  const x = d => L + (new Date(d).getTime() - t0) / (t1 - t0 || 1) * (W - L - R);
  const y = v => T + (hi - v) / (hi - lo || 1) * (H - T - B);
  let s = `<svg viewBox="0 0 ${W} ${H}" width="100%" role="img" aria-label="Monthly price over five years with yearly grades marked">`;
  [hi, (hi + lo) / 2, lo].forEach(v => { s += `<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" stroke="var(--line)"/><text x="${L - 4}" y="${y(v) + 3}" font-size="9" text-anchor="end" fill="var(--mute)">${v >= 1000 ? Math.round(v) : v.toFixed(v < 10 ? 2 : 0)}</text>`; });
  for (let yr = new Date(mo[0][0]).getFullYear() + 1; yr <= new Date(mo[mo.length - 1][0]).getFullYear(); yr++) {
    const xx = x(`${yr}-01-01`); s += `<text x="${xx}" y="${H - 5}" font-size="9" text-anchor="middle" fill="var(--mute)">${yr}</text>`;
  }
  s += `<polyline points="${mo.map(p => `${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join(" ")}" fill="none" stroke="var(--text-2)" stroke-width="1.6" stroke-linejoin="round"/>`;
  rows.forEach(r => {
    if (r.known < mo[0][0]) return;
    const xx = x(r.known), yy = r.p0 != null ? y(r.p0) : T;
    s += `<line x1="${xx}" x2="${xx}" y1="${T - 2}" y2="${H - B}" stroke="${gradeColor(r.grade.letter)}" stroke-dasharray="2 3" opacity=".7"/>`;
    s += `<circle cx="${xx}" cy="${yy}" r="3.5" fill="${gradeColor(r.grade.letter)}"/>`;
    s += `<rect x="${xx - 9}" y="2" width="18" height="16" rx="4" fill="${gradeColor(r.grade.letter)}"/><text x="${xx}" y="14" font-size="11" font-weight="700" text-anchor="middle" fill="#0b0d0f">${r.grade.letter}</text>`;
  });
  return s + `</svg>`;
}

// ------------------------------------------------------------ views
function render() {
  document.querySelectorAll("nav.tabs button").forEach(b => b.classList.toggle("on", b.dataset.tab === ui.tab && !ui.sym));
  const main = document.querySelector("main"), title = document.getElementById("title"), back = document.getElementById("back");
  back.hidden = !ui.sym;
  if (ui.sym) { title.textContent = ui.sym; main.innerHTML = viewDetail(ui.sym); }
  else if (ui.tab === "portfolio") { title.textContent = "Portfolio"; main.innerHTML = viewPortfolio(); }
  else if (ui.tab === "stocks") { title.textContent = "Stocks"; main.innerHTML = viewStocks(); }
  else if (ui.tab === "learn") { title.textContent = "Learn"; main.innerHTML = viewLearn(); }
  else { title.textContent = "Settings"; main.innerHTML = viewSettings(); }
  setSync(ui.syncState);
}

function viewPortfolio() {
  const hs = Object.entries(J.holdings).filter(([, h]) => h.shares > 0);
  if (!hs.length) return `<div class="card empty">No holdings yet.<br><br>
    <button class="btn" data-act="tab" data-v="settings">Import Fidelity positions</button><br><br>
    <span class="t2">or open a stock and tap “Edit position”.</span></div>${dataStatus()}`;
  let value = 0, cost = 0, day = 0;
  const rows = hs.map(([sym, h]) => {
    const p = priceOf(sym), r = rec(sym).price;
    const v = p != null ? p * h.shares : h.cost * h.shares; value += v; cost += h.cost * h.shares;
    if (r?.change != null) day += r.change * h.shares;
    return { sym, h, p, v, pl: p != null && h.cost ? (p / h.cost - 1) * 100 : null, dayPct: r?.changePct };
  }).sort((a, b) => b.v - a.v);
  const pl = value - cost;
  return `<div class="card"><div class="totals">
      <div><div class="lab">Value</div><div class="big num">${money(value)}</div></div>
      <div><div class="lab">Total gain</div><div class="big num ${cls(pl)}">${money(pl)}</div><div class="num ${cls(pl)}">${pct(cost ? pl / cost * 100 : null)}</div></div>
      <div><div class="lab">Today</div><div class="num ${cls(day)}">${money(day)}</div></div>
      <div><div class="lab">Cost basis</div><div class="num">${money(cost)}</div></div>
    </div></div>
    <div class="card"><h2>Holdings<span class="sp"></span><span class="mute" style="text-transform:none;letter-spacing:0">weight · gain</span></h2><div class="rows">
    ${rows.map(r => {
      const g = rec(r.sym).fundamentals?.grade;
      return `<div class="row" data-act="open" data-v="${r.sym}">
        <div><div class="sym">${r.sym} <span class="grade ${gClass(g?.letter)}">${g?.letter || "–"}</span></div>
        <div class="sub num">${r.h.shares} sh @ ${money(r.h.cost)} · ${r.p != null ? money(r.p) : "no price"} <span class="${cls(r.dayPct)}">${pct(r.dayPct)}</span></div></div>
        <div class="right"><div class="num">${money(r.v)}</div><div class="sub num ${cls(r.pl)}">${(r.v / value * 100).toFixed(1)}% · ${pct(r.pl)}</div></div>
        <div class="bar"><i style="width:${(r.v / value * 100).toFixed(1)}%"></i></div></div>`;
    }).join("")}</div></div>${dataStatus()}`;
}

function dataStatus() {
  return `<p class="mute" style="font-size:12px;text-align:center">${M.generated ? `Market data updated ${rel(M.generated)}` : "Market data not loaded yet — the daily job hasn't run."}</p>`;
}

function viewStocks() {
  const f = ui.filter;
  const syms = symbols().filter(s => f === "all" || (f === "due" ? reviewDue(J.stocks[s]) : (J.stocks[s]?.status || "watching") === f));
  const counts = { owned: 0, watching: 0, passed: 0, due: 0 };
  symbols().forEach(s => { counts[J.stocks[s]?.status || "watching"]++; if (reviewDue(J.stocks[s])) counts.due++; });
  const mk = M.meta?.market;
  const banner = mk ? `<div class="flag ${mk.state === "uptrend" ? "good" : "bad"}" style="margin-bottom:12px">Market: ${esc(mk.why)}</div>` : "";
  return `${banner}<div class="filters">${[["all", "All"], ["owned", "Owned"], ["watching", "Watching"], ["due", "Review due"], ["passed", "Passed"]]
      .map(([k, l]) => `<button class="${f === k ? "on" : ""}" data-act="filter" data-v="${k}">${l}${k !== "all" ? ` ${counts[k]}` : ""}</button>`).join("")}</div>
    <div class="card"><div class="rows">${syms.map(s => {
      const r = rec(s), st = J.stocks[s], g = r.fundamentals?.grade, hasThesis = st?.thesis?.why;
      return `<div class="row" data-act="open" data-v="${s}">
        <div><div class="sym">${s} <span class="grade ${gClass(g?.letter)}" title="Health grade">${g?.letter || "–"}</span>
          ${st?.status === "owned" ? `<span class="chip owned">owned</span>` : ""}${reviewDue(st) ? `<span class="chip due">review</span>` : ""}</div>
          <div class="sub">${esc(shortName(r.name) || "")}${r.rules ? ` · rules ${ruleCount(r)[0]}/${ruleCount(r)[1]}` : ""}${r.canslim ? ` · CAN SLIM ${["C","A","N","S","L","I","M"].filter(k => r.canslim[k]?.pass === true).length}/7` : ""}${hasThesis ? "" : ` · <span class="t2">no thesis yet</span>`}</div></div>
        <div class="right"><div class="num">${r.price ? money(r.price.last) : "—"}</div><div class="sub num ${cls(r.price?.changePct)}">${pct(r.price?.changePct)}</div></div></div>`;
    }).join("") || `<div class="empty">Nothing here.</div>`}</div></div>
    <button class="btn ghost block" data-act="addTicker">+ Add a stock</button>`;
}

function learnQ(key) { return `<button class="q" data-act="learn" data-v="${key}" aria-label="What is this?">?</button>`; }
function kvRow(label, val, key, sym, valCls = "") {
  const c = sym ? ctxFor(sym) : null, line = cfg.learn && key && c && LEARN[key]?.say(c);
  return `<div class="k">${label}${key ? learnQ(key) : ""}</div><div class="v ${valCls}">${val}</div>${line ? `<div class="explain">${esc(line)}</div>` : ""}`;
}

function viewDetail(sym) {
  const r = rec(sym), st = stock(sym), p = r.price, h = J.holdings[sym], co = callout(sym), g = r.fundamentals?.grade;
  const subs = [["thesis", "Thesis"], ["health", "Health"], ["canslim", "Rules"], ["numbers", "Numbers"], ["journal", "Journal"], ["news", "News"]];
  let body = "";
  if (ui.sub === "thesis") body = subThesis(sym, st, co);
  else if (ui.sub === "health") body = subHealth(sym);
  else if (ui.sub === "numbers") body = subNumbers(sym);
  else if (ui.sub === "canslim") body = subCanslim(sym);
  else if (ui.sub === "journal") body = subJournal(sym, st);
  else body = subNews(r);
  return `<div class="dhead"><div><div class="t2">${esc(r.name || "")}</div>
      <div class="price num">${p ? money(p.last) : "—"}</div>
      <div class="num ${cls(p?.change)}">${p ? `${p.change >= 0 ? "+" : "−"}${money(Math.abs(p.change))} (${pct(p.changePct)})` : "No price yet"} <span class="mute">${p ? fmtDate(p.asOf) : ""}</span></div></div>
      ${g ? `<div style="text-align:center" data-act="sub" data-v="health"><span class="grade lg ${gClass(g.letter)}">${g.letter}</span><div class="mute" style="font-size:11px">health ${g.score}</div></div>` : ""}</div>
    ${spark(p?.history)}
    <div class="stack" style="margin-top:6px;font-size:12.5px"><span class="mute">52w</span><span class="num">${p ? `${money(p.lo52)} – ${money(p.hi52)}` : "—"}</span>
      <span class="mute" style="margin-left:auto">${h?.shares ? `${h.shares} sh @ ${money(h.cost)}` : "No position"}</span>
      <button class="btn ghost sm" data-act="editPos" data-v="${sym}">Edit position</button></div>
    <div class="seg">${subs.map(([k, l]) => `<button class="${ui.sub === k ? "on" : ""}" data-act="sub" data-v="${k}">${l}</button>`).join("")}</div>${body}`;
}

function subThesis(sym, st, co) {
  const t = st.thesis || {};
  const f = (k, label, hint) => `<label class="f"><span>${label}</span><textarea data-th="${k}" placeholder="${esc(hint)}">${esc(t[k] || "")}</textarea></label>`;
  return `${co ? `<div class="card"><h2>Source</h2><div class="kv">
      <div class="k">${esc(co.source)}</div><div class="v"></div>
      <div class="k">Their entry targets</div><div class="v">${esc(co.entries || "—")}</div>
      ${co.theirAvg ? `<div class="k">Their average cost</div><div class="v">${money(co.theirAvg)}</div>` : ""}</div></div>` : ""}
    <div class="card"><h2>Status</h2><div class="seg" style="margin:0">${["watching", "owned", "passed"].map(s => `<button class="${st.status === s ? "on" : ""}" data-act="status" data-v="${s}">${s[0].toUpperCase() + s.slice(1)}</button>`).join("")}</div></div>
    <div class="card"><h2>My thesis</h2>
      ${f("why", "Why I own it / want it", "What does this company do that will make it worth more in 3–5 years?")}
      ${f("catalysts", "What has to go right", "Specific things you expect: product launches, margin expansion, new markets…")}
      ${f("risks", "What would prove me wrong", "Concrete signs the thesis is broken, e.g. growth below 15% for two quarters.")}
      ${f("rules", "Buy / trim / sell rules", "e.g. Add below $X. Trim above $Y. Sell if the thesis breaks, not on price alone.")}
      <label class="f"><span>My fair value per share</span><input type="number" step="0.01" data-th="fair" value="${esc(t.fair || "")}" placeholder="Optional"></label>
      <label class="f"><span>Conviction</span><div class="conv">${[1, 2, 3, 4, 5].map(n => `<button class="${+t.conviction === n ? "on" : ""}" data-act="conv" data-v="${n}">${n}</button>`).join("")}</div></label>
      <label class="f"><span>Next review</span><input type="date" data-th="review" value="${esc(t.review || "")}"><small>Shows a “review” tag when the date arrives. After earnings is a good default.</small></label>
      <div class="stack"><button class="btn" data-act="saveThesis">Save</button><button class="btn ghost" data-act="saveThesisLog">Save + log change</button>
      <span class="mute" style="font-size:12px">${t.updated ? `Updated ${fmtDate(t.updated)}` : ""}</span></div></div>`;
}

function subHealth(sym) {
  const f = rec(sym).fundamentals;
  if (!f) return `<div class="card empty">${esc(rec(sym).errors?.fundamentals || "No filings data yet. It appears after the next daily update.")}</div>`;
  const g = f.grade, rows = gradeVsPrice(sym);
  return `<button class="btn block" data-act="walk" data-v="0" style="margin-bottom:12px">Walk me through ${sym}'s numbers</button>
    <div class="card"><h2>Health grade ${learnQ("grade")}<span class="sp"></span><span class="mute" style="text-transform:none;letter-spacing:0">latest 12 mo</span></h2>
      <div class="stack" style="margin-bottom:6px"><span class="grade lg ${gClass(g.letter)}">${g.letter || "–"}</span>
      <div><div class="big num">${g.score ?? "—"}<span class="mute" style="font-size:14px"> / 100</span></div>
      <div class="mute" style="font-size:12px">${g.parts.length < 5 ? `Based on ${g.parts.length} of 5 parts` : "All 5 parts"}</div></div></div>
      ${g.parts.map(p => `<div class="part"><div>${p.label}</div><div class="num">${p.score.toFixed(0)}<span class="mute">/${p.max}</span></div>
        <div class="pb"><i style="width:${p.score / p.max * 100}%;background:${p.score / p.max >= .7 ? "var(--pos)" : p.score / p.max >= .4 ? "var(--warn)" : "var(--neg)"}"></i></div>
        <div class="why">${esc(p.why)}</div>${p.checks ? balanceChecks(p.checks) : ""}</div>`).join("")}
      <p class="mute" style="font-size:12px;margin:8px 0 0">Weights: ${Object.entries(cfg.weights).map(([k, v]) => `${PART_LABELS[k]} ${v}`).join(" · ")}. <a href="#" data-act="tab" data-v="settings">Change</a></p></div>
    <div class="card"><h2>What stands out</h2>
      ${f.flags.map(x => `<div class="flag bad">${esc(x)}</div>`).join("")}${f.strengths.map(x => `<div class="flag good">${esc(x)}</div>`).join("")}
      ${!f.flags.length && !f.strengths.length ? `<p class="mute">Nothing notable either way.</p>` : ""}</div>
    <div class="card"><h2>Grade vs. price ${learnQ("gradeVsPrice")}</h2>${gradeChart(sym)}
      <div class="tbl"><table><thead><tr><th>Fiscal year</th><th>Grade</th><th>Price then</th><th>12 mo later</th><th>Change</th></tr></thead><tbody>
      ${rows.map(r => `<tr><td>${r.end.slice(0, 4)}</td><td><span class="grade ${gClass(r.grade.letter)}">${r.grade.letter}</span> ${r.grade.score}</td>
        <td>${r.p0 != null ? money(r.p0) : "—"}</td><td>${r.p1 != null ? money(r.p1) : "—"}${r.partial ? "*" : ""}</td><td class="${cls(r.ret)}">${pct(r.ret, 0)}</td></tr>`).join("")}
      </tbody></table></div>
      <p class="mute" style="font-size:12px;margin:8px 0 0">${gradeTakeaway(rows)} Prices are split-adjusted. * = less than 12 months so far.</p></div>`;
}
function gradeTakeaway(rows) {
  const done = rows.filter(r => r.ret != null);
  if (done.length < 2) return "Too few graded years to compare yet.";
  const up = done.filter((r, i) => i && r.grade.score > done[i - 1].grade.score);
  const dn = done.filter((r, i) => i && r.grade.score < done[i - 1].grade.score);
  const avg = a => a.length ? a.reduce((s, r) => s + r.ret, 0) / a.length : null;
  const a = avg(up), b = avg(dn);
  if (a == null || b == null) return "Not enough ups and downs in the grade to compare.";
  return `After the grade improved, the stock averaged ${pct(a, 0)} over the next year; after it worsened, ${pct(b, 0)}. With ${done.length} years, treat this as a hint, not a rule.`;
}

function subNumbers(sym) {
  const f = rec(sym).fundamentals;
  if (!f) return `<div class="card empty">${esc(rec(sym).errors?.fundamentals || "No filings data yet.")}</div>`;
  const t = f.ttm, b = f.balance || {}, v = f.valuation || {};
  const row = (label, key, fn) => `<tr><td>${label}${key ? " " + learnQ(key) : ""}</td>${f.fy.map(x => `<td class="${/Growth|Change|Margin/.test(label) ? "" : ""}">${fn(x)}</td>`).join("")}</tr>`;
  return `<div class="card"><h2>Valuation</h2><div class="kv">
      ${kvRow("Market cap", money(v.marketCap), "marketCap", sym)}
      ${kvRow("Price / sales", v.priceToSales ?? "—", "priceToSales", sym)}
      ${kvRow("EV / sales", v.evToSales ?? "—", "evToSales", sym)}
      ${kvRow("Price / free cash flow", v.priceToFcf ?? "n/a", "priceToFcf", sym)}
      ${kvRow("P/E", v.pe ?? "n/a", "pe", sym)}
      ${kvRow("Price / book", v.priceToBook ?? "n/a", "priceToBook", sym)}</div></div>
    <div class="card"><h2>Last 12 months ${learnQ("ttm")}<span class="sp"></span><span class="mute" style="text-transform:none;letter-spacing:0">through ${fmtDate(t.asOf)}</span></h2><div class="kv">
      ${kvRow("Revenue", money(t.revenue), "revenue", sym)}
      ${kvRow("Revenue growth", pct(t.revenueGrowth), "growth", sym, cls(t.revenueGrowth))}
      ${kvRow("Gross margin", pct(t.grossMargin, 1, false), "grossMargin", sym)}
      ${kvRow("Operating margin", pct(t.operatingMargin, 1, false), "operatingMargin", sym, cls(t.operatingMargin))}
      ${kvRow("Net income", money(t.netIncome), "netIncome", sym, cls(t.netIncome))}
      ${kvRow("Free cash flow", money(t.fcf), "fcf", sym, cls(t.fcf))}
      ${kvRow("FCF margin", pct(t.fcfMargin, 1, false), "fcfMargin", sym)}
      ${kvRow("Stock comp", money(t.sbc), "sbc", sym)}</div></div>
    <div class="card"><h2>Balance sheet ${learnQ("balanceSheet")}<span class="sp"></span><span class="mute" style="text-transform:none;letter-spacing:0">${fmtDate(b.asOf)}</span></h2>
      ${aleBar(b)}
      <div class="kv">
      ${kvRow("Total assets", money(b.totalAssets), "totalAssets", sym)}
      ${kvRow("Total liabilities", money(b.totalLiabilities), "totalLiabilities", sym)}
      ${kvRow("Shareholders' equity", money(b.equity), "equity", sym, cls(b.equity))}
      ${kvRow("Liabilities ÷ assets", pct(b.liabToAssets, 0, false), "liabToAssets", sym)}
      ${kvRow("Goodwill + intangibles", pct(b.intangibleShare, 0, false) + " of assets", "intangibleShare", sym)}
      ${kvRow("Equity per share, 1 yr", pct(b.bvpsGrowth, 0), "bvpsGrowth", sym, cls(b.bvpsGrowth))}
      ${kvRow("Cash + investments", money(b.cashAndInvestments))}
      ${kvRow("Total debt", money(b.totalDebt))}
      ${kvRow("Net cash", money(b.netCash), "netCash", sym, cls(b.netCash))}
      ${kvRow("Current ratio", b.currentRatio ?? "—", "currentRatio", sym)}
      ${kvRow("Debt / equity", b.debtToEquity ?? "—", "debtToEquity", sym)}</div></div>
    <div class="card"><h2>By fiscal year</h2><div class="tbl"><table>
      <thead><tr><th></th>${f.fy.map(x => `<th>FY${x.end.slice(2, 4)}</th>`).join("")}</tr></thead><tbody>
      ${row("Grade", "grade", x => x.grade?.letter ? `<span class="grade ${gClass(x.grade.letter)}">${x.grade.letter}</span>` : "–")}
      ${row("Revenue", "revenue", x => money(x.revenue))}
      ${row("Growth", "growth", x => `<span class="${cls(x.revenueGrowth)}">${pct(x.revenueGrowth, 0)}</span>`)}
      ${row("Gross margin", "grossMargin", x => pct(x.grossMargin, 0, false))}
      ${row("Op. margin", "operatingMargin", x => `<span class="${cls(x.operatingMargin)}">${pct(x.operatingMargin, 0, false)}</span>`)}
      ${row("Net income", "netIncome", x => money(x.netIncome))}
      ${row("Free cash flow", "fcf", x => `<span class="${cls(x.fcf)}">${money(x.fcf)}</span>`)}
      ${row("Share change", "shareChange", x => pct(x.shareChange))}
      ${row("Diluted EPS", "eps", x => x.eps != null ? (x.eps < 0 ? "−$" : "$") + Math.abs(x.eps).toFixed(2) : "—")}
      ${row("ROE", "roe", x => pct(x.roe, 0, false))}
      ${row("Net cash", "netCash", x => money(x.netCash))}
      ${row("Liab. ÷ assets", "liabToAssets", x => pct(x.liabToAssets, 0, false))}
      ${row("Equity/share", "bvpsGrowth", x => pct(x.bvpsGrowth, 0))}
      </tbody></table></div><p class="mute" style="font-size:12px;margin:8px 0 0">Source: ${esc(f.source || "SEC filings")}${f.splitNote ? ". " + esc(f.splitNote) : ""}${f.currency && f.currency !== "USD" ? ` (in ${f.currency})` : ""}.</p></div>`;
}

function aleBar(b) {
  // Assets = Liabilities + Equity, drawn to scale
  if (!b.totalAssets || b.totalLiabilities == null) return "";
  const L = Math.max(0, Math.min(100, b.totalLiabilities / b.totalAssets * 100)), E = 100 - L;
  return `<div style="margin:2px 0 12px"><div class="mute" style="font-size:12px;margin-bottom:4px">Assets = Liabilities + Equity</div>
    <div style="display:flex;height:22px;border-radius:6px;overflow:hidden;font-size:11px;font-family:var(--mono)">
      <div style="width:${L}%;background:color-mix(in srgb,var(--neg) 55%,transparent);display:grid;place-items:center">${L >= 18 ? `Owes ${L.toFixed(0)}%` : ""}</div>
      <div style="width:${E}%;background:color-mix(in srgb,var(--pos) 55%,transparent);display:grid;place-items:center">${E >= 18 ? `Owns ${E.toFixed(0)}%` : ""}</div></div>
    <div class="mute" style="font-size:11.5px;margin-top:4px">Of ${money(b.totalAssets)} in assets, ${money(b.totalLiabilities)} is owed to others and ${money(b.equity)} belongs to shareholders.</div></div>`;
}

function balanceChecks(checks) {
  return `<div style="grid-column:1/-1;margin-top:6px">${checks.map(c => `<div class="stack" style="font-size:12.5px;padding:3px 0;flex-wrap:nowrap">
    <span style="width:8px;height:8px;border-radius:50%;flex:none;background:${c.score >= .7 ? "var(--pos)" : c.score >= .4 ? "var(--warn)" : "var(--neg)"}"></span>
    <span style="flex:1">${esc(c.label)} ${learnQ(c.key)}</span><span class="mute">${esc(c.text)}</span></div>`).join("")}</div>`;
}

const RULE_ORDER = ["sales", "cash", "revenue", "earnings", "chart"];
const RULE_LEARN = { sales: "ruleSales", cash: "netCash", revenue: "growth", earnings: "eps", chart: "ruleChart" };
function ruleCount(r) { const x = r.rules || {}; return [RULE_ORDER.filter(k => x[k]?.pass === true).length, RULE_ORDER.filter(k => x[k]?.pass != null).length]; }
function myRulesCard(sym) {
  const r = rec(sym), R = r.rules; if (!R) return "";
  const [p, n] = ruleCount(r);
  return `<div class="card"><h2>My rules<span class="sp"></span><span class="num" style="text-transform:none;letter-spacing:0;color:var(--text)">${p} / ${n} pass</span></h2>
    ${RULE_ORDER.map(k => { const x = R[k] || {}; const st = x.pass === true ? "pos" : x.pass === false ? "neg" : "mute";
      return `<div class="part" style="grid-template-columns:1fr auto"><div>${esc(x.name || k)} ${learnQ(RULE_LEARN[k])}</div>
        <div class="${st}" style="font-weight:600">${x.pass === true ? "Pass" : x.pass === false ? "Fail" : "—"}</div><div class="why">${esc(x.why || "")}</div></div>`; }).join("")}
    ${R.chart ? `<div style="margin-top:8px">${spark((r.price?.history || []).slice(-27), 320, 44)}<div class="mute" style="font-size:11.5px">Last 6 months${r.price?.approxHistory ? " · averages estimated from weekly data until daily history builds up" : ""}</div></div>` : ""}
    <p class="mute" style="font-size:12px;margin:8px 0 0">Exceptions happen. If you buy one that fails a rule, write why in the thesis.</p></div>`;
}

function subCanslim(sym) {
  const r = rec(sym), cs = r.canslim, mk = M.meta?.market;
  if (!cs) return `<div class="card empty">${r.quoteType === "ETF" ? "These checklists apply to individual companies, not ETFs." : "No checklist data yet. It appears after the next daily update."}</div>`;
  const order = ["C", "A", "N", "S", "L", "I", "M"], passed = order.filter(k => cs[k]?.pass === true).length;
  const known = order.filter(k => cs[k]?.pass != null).length;
  return `${myRulesCard(sym)}<div class="card"><h2>CAN SLIM ${learnQ("canslim")}<span class="sp"></span><span class="num" style="text-transform:none;letter-spacing:0;color:var(--text)">${passed} / ${known} pass</span></h2>
    ${order.map(k => { const x = cs[k] || {}; const st = x.pass === true ? "pos" : x.pass === false ? "neg" : "mute";
      return `<div class="part" style="grid-template-columns:auto 1fr auto"><span class="grade" style="background:var(--${st === "mute" ? "panel-2" : st});color:${st === "mute" ? "var(--mute)" : "#0b0d0f"}">${k}</span>
        <div>${esc(x.name || k)} ${learnQ("cs" + k)}</div><div class="${st}" style="font-weight:600">${x.pass === true ? "Pass" : x.pass === false ? "Fail" : "—"}</div>
        <div class="why">${esc(x.why || "")}</div></div>`; }).join("")}
    <p class="mute" style="font-size:12px;margin:8px 0 0">L is an approximation of IBD's Relative Strength rating (compares to the S&P 500, not every stock). I's change builds up over time from daily snapshots.</p></div>
    ${mk ? `<div class="card"><h2>Market (M)</h2><div class="kv">${kvRow("S&P 500 (SPY)", money(mk.spy))}${kvRow("50-day average", money(mk.ma50))}${kvRow("200-day average", money(mk.ma200))}</div></div>` : ""}
    <div class="card"><h2>How CAN SLIM and the health grade differ</h2><p class="t2" style="margin:0">The health grade asks “is this a sound business?” CAN SLIM asks “is this the right stock to buy now?” A stock can score A on health and fail CAN SLIM (great business, weak price action), or the reverse. O'Neil also cuts any loss at 7–8% — write in your thesis which rulebook governs this position.</p></div>`;
}

function subJournal(sym, st) {
  const es = [...(st.entries || [])].sort((a, b) => b.date.localeCompare(a.date));
  return `<button class="btn block" data-act="newEntry" style="margin-bottom:12px">+ New entry</button>
    <div class="card">${es.length ? es.map(e => {
      const now = priceOf(sym), since = e.price && now ? (now / e.price - 1) * 100 : null;
      return `<div class="entry"><div class="meta"><span class="chip">${esc(e.type)}</span>${fmtDate(e.date)}
        ${e.price ? `<span class="num">@ ${money(e.price)}</span><span class="num ${cls(since)}">${pct(since, 0)} since</span>` : ""}
        <span style="margin-left:auto"><button class="iconbtn" data-act="delEntry" data-v="${e.id}" aria-label="Delete entry">✕</button></span></div>
        <div class="txt">${esc(e.text)}</div></div>`;
    }).join("") : `<div class="empty">Log every decision here: why you bought, added, trimmed, or changed your mind. The price is recorded automatically so you can see later how each call played out.</div>`}</div>`;
}

function subNews(r) {
  const n = r.news || [];
  return `<div class="card news">${n.length ? n.map(a => `<a href="${esc(a.link)}" target="_blank" rel="noopener">${esc(a.title)}<div class="meta">${esc(a.source || "")} · ${rel(a.date)}</div></a>`).join("") : `<div class="empty">No recent headlines.</div>`}</div>`;
}

function viewLearn() {
  return `<div class="card"><p style="margin-top:0">Every number in the app has a <span class="q" style="display:inline-grid">?</span> next to it. Tap it for what the number means, what good and bad look like, and the common traps.</p>
    <p>On any stock's <b>Health</b> tab, “Walk me through” goes step by step through that company's actual numbers, in the order an analyst would read them.</p>
    <label class="stack" style="margin:0"><input type="checkbox" data-act="toggleLearn" ${cfg.learn ? "checked" : ""}> Show one-line explanations under each number</label></div>
    ${[["Reading the business", ["revenue", "growth", "grossMargin", "operatingMargin", "netIncome", "eps"]], ["Cash", ["fcf", "fcfMargin", "sbc"]],
      ["Balance sheet", ["balanceSheet", "totalAssets", "totalLiabilities", "equity", "liabToAssets", "netCash", "currentRatio", "intangibleShare", "bvpsGrowth", "debtToEquity", "shareChange"]],
      ["My rules", ["ruleSales", "netCash", "growth", "eps", "ruleChart"]], ["Valuation", ["marketCap", "priceToSales", "evToSales", "priceToFcf", "pe", "priceToBook"]], ["CAN SLIM", ["canslim", "csC", "csA", "csN", "csS", "csL", "csI", "csM", "eps", "roe"]],
      ["This app", ["grade", "gradeVsPrice", "ttm"]]]
      .map(([h, keys]) => `<div class="card"><h2>${h}</h2><div class="rows">${keys.map(k => `<div class="row" data-act="learn" data-v="${k}"><div><div>${LEARN[k].title}</div><div class="sub">${esc(LEARN[k].what)}</div></div><div class="mute">›</div></div>`).join("")}</div></div>`).join("")}`;
}

function viewSettings() {
  return `<div class="card"><h2>Sync</h2>
    <p class="t2" style="margin-top:0">Your journal is saved on this phone. Add a GitHub token to back it up to your private <b>${esc(cfg.dataRepo)}</b> repo and use it on other devices.</p>
    <label class="f"><span>GitHub token</span><input type="password" id="tok" value="${esc(cfg.token)}" placeholder="github_pat_…" autocomplete="off">
      <small>Fine-grained token with “Contents: read and write” on ${esc(cfg.appRepo)} and ${esc(cfg.dataRepo)} only. Stored only on this device.</small></label>
    <div class="stack"><button class="btn" data-act="saveTok">Save &amp; sync</button><span class="sync" id="sync2">${esc(ui.syncState)}</span></div></div>
    <div class="card"><h2>Grade weights</h2>
      <p class="t2" style="margin-top:0">How much each part counts toward the health grade. Changes apply to every stock and every year, so you can see how the grade-vs-price results shift.</p>
      ${Object.entries(cfg.weights).map(([k, v]) => `<label class="f"><span>${PART_LABELS[k]} <span class="num" id="wv-${k}">${v}</span></span>
        <input type="range" min="0" max="40" step="5" value="${v}" data-act="weight" data-k="${k}" style="width:100%"></label>`).join("")}
      <div class="stack"><span class="mute" id="wtotal">Total ${Object.values(cfg.weights).reduce((a, b) => a + b, 0)} (scores are scaled to 100 either way)</span>
      <button class="btn ghost sm" data-act="resetWeights" style="margin-left:auto">Reset</button></div></div>
    <div class="card"><h2>Import from Fidelity</h2>
      <p class="t2" style="margin-top:0">Fidelity → Positions → Download (the ↓ icon). Pick the CSV here. You'll choose which account to import.</p>
      <input type="file" accept=".csv,text/csv" data-act="csv"></div>
    <div class="card"><h2>Backup</h2><div class="stack">
      <button class="btn ghost" data-act="export">Export journal</button>
      <label class="btn ghost" style="cursor:pointer">Import backup<input type="file" accept=".json" data-act="importJson" hidden></label></div></div>
    <div class="card"><h2>About the data</h2><p class="t2" style="margin:0">Prices: Yahoo Finance (Stooq fallback). Financials: SEC EDGAR when reachable, otherwise Yahoo Finance's fundamentals feed (built from the same filings). Ownership: Yahoo Finance. News: Yahoo Finance and Google News. Updated each weekday after the close. ${M.generated ? `Last update ${rel(M.generated)}.` : ""}</p></div>`;
}

// ------------------------------------------------------------ sheets
function sheet(html) {
  closeSheet();
  const bg = document.createElement("div"); bg.className = "sheet-bg"; bg.id = "sheet";
  bg.innerHTML = `<div class="sheet" role="dialog" aria-modal="true">${html}</div>`;
  bg.addEventListener("click", e => { if (e.target === bg) closeSheet(); });
  document.body.appendChild(bg); bg.querySelector("input,textarea")?.focus();
}
function closeSheet() { document.getElementById("sheet")?.remove(); }

function learnSheet(key, sym) {
  const L = LEARN[key], line = sym && L.say(ctxFor(sym));
  sheet(`<h3>${L.title}</h3><p>${esc(L.what)}</p>${line ? `<div class="callout">${esc(line)}</div>` : ""}
    <h4>How to read it</h4><p>${esc(L.read)}</p><h4>Watch out for</h4><p>${esc(L.watch)}</p>
    <button class="btn ghost block" data-act="close" style="margin-top:14px">Got it</button>`);
}

function walkSheet(sym, i) {
  const step = WALKTHROUGH[i], L = LEARN[step.key], c = ctxFor(sym), f = rec(sym).fundamentals;
  const lines = [step.key, ...(step.also || [])].map(k => LEARN[k].say(c)).filter(Boolean);
  let extra = "";
  if (step.key === "grade" && f?.grade) {
    extra = `<div class="callout">${f.flags.length ? `Main concerns: ${esc(f.flags.join("; "))}.` : "No red flags in the numbers."}
      ${f.strengths.length ? `<br>Strengths: ${esc(f.strengths.join("; "))}.` : ""}</div>
      <p class="t2">Now write your own one-paragraph take in the journal. Putting it in your words is what makes it stick, and it gives you something to check against later.</p>`;
  }
  sheet(`<div class="steps">${WALKTHROUGH.map((_, j) => `<i class="${j <= i ? "on" : ""}"></i>`).join("")}</div>
    <div class="mute" style="font-size:12px">Step ${i + 1} of ${WALKTHROUGH.length} · ${sym}</div>
    <h3>${step.q}</h3>
    ${lines.length ? `<div class="callout">${lines.map(esc).join("<br>")}</div>` : `<div class="callout">No data for this step.</div>`}
    <h4>${L.title}</h4><p>${esc(L.what)}</p><p class="t2">${esc(L.read)}</p><p class="mute" style="font-size:13px">${esc(L.watch)}</p>${extra}
    <div class="stack" style="margin-top:14px">${i ? `<button class="btn ghost" data-act="walk" data-v="${i - 1}">Back</button>` : ""}
      ${i < WALKTHROUGH.length - 1 ? `<button class="btn" data-act="walk" data-v="${i + 1}" style="margin-left:auto">Next</button>`
        : `<button class="btn" data-act="walkDone" style="margin-left:auto">Write my take</button>`}</div>`);
}

function entrySheet(sym, preset = {}) {
  const types = ["Note", "Buy", "Add", "Trim", "Sell", "Thesis change", "Earnings review"];
  sheet(`<h3>New entry · ${sym}</h3>
    <label class="f"><span>Type</span><select id="eType">${types.map(t => `<option ${preset.type === t ? "selected" : ""}>${t}</option>`).join("")}</select></label>
    <label class="f"><span>What happened, and what are you thinking?</span><textarea id="eText" style="min-height:120px">${esc(preset.text || "")}</textarea></label>
    <div class="stack"><label class="f" style="flex:1;margin:0"><span>Date</span><input type="date" id="eDate" value="${today()}"></label>
      <label class="f" style="flex:1;margin:0"><span>Price</span><input type="number" step="0.01" id="ePrice" value="${priceOf(sym) ?? ""}"></label></div>
    <div class="stack" style="margin-top:14px"><button class="btn ghost" data-act="close">Cancel</button><button class="btn" data-act="saveEntry" style="margin-left:auto">Save</button></div>`);
}

function posSheet(sym) {
  const h = J.holdings[sym] || {};
  sheet(`<h3>Position · ${sym}</h3>
    <label class="f"><span>Shares</span><input type="number" step="any" id="pShares" value="${h.shares ?? ""}"></label>
    <label class="f"><span>Average cost per share</span><input type="number" step="0.01" id="pCost" value="${h.cost ?? ""}"></label>
    <div class="stack"><button class="btn ghost" data-act="close">Cancel</button><button class="btn" data-act="savePos" style="margin-left:auto">Save</button></div>`);
}

// ------------------------------------------------------------ CSV import (Fidelity positions)
function parseCSV(text) {
  const rows = []; let row = [], cur = "", q = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (q) { if (c === '"') { if (text[i + 1] === '"') { cur += '"'; i++; } else q = false; } else cur += c; }
    else if (c === '"') q = true; else if (c === ",") { row.push(cur); cur = ""; }
    else if (c === "\n" || c === "\r") { if (c === "\r" && text[i + 1] === "\n") i++; row.push(cur); rows.push(row); row = []; cur = ""; }
    else cur += c;
  }
  if (cur || row.length) { row.push(cur); rows.push(row); }
  return rows;
}
const numv = s => { const n = parseFloat(String(s || "").replace(/[$,+%\s]/g, "")); return isNaN(n) ? null : n; };
let pendingImport = null;
function importFidelity(text) {
  const rows = parseCSV(text.replace(/^﻿/, ""));
  const hi = rows.findIndex(r => r.some(c => c.trim() === "Symbol"));
  if (hi < 0) return toast("Couldn't find a Symbol column — is this the Positions download?");
  const H = rows[hi].map(c => c.trim()), col = n => H.findIndex(h => h.toLowerCase() === n.toLowerCase());
  const iSym = col("Symbol"), iQty = col("Quantity"), iAvg = col("Average Cost Basis"), iTot = col("Cost Basis Total"),
    iName = col("Account Name"), iNum = col("Account Number"), iType = col("Type");
  const acctOf = r => { const n = iName >= 0 ? r[iName].trim() : "", d = iNum >= 0 ? r[iNum].trim() : "";
    return n && d ? `${n} (…${d.slice(-4)})` : n || d || "Account"; };
  const pos = [];
  for (const r of rows.slice(hi + 1)) {
    const sym = (r[iSym] || "").trim().toUpperCase();
    if (!sym || /\*\*|PENDING|^[0-9]|CASH/.test(sym) || r.length < 4) continue;
    const qty = numv(r[iQty]); if (!qty) continue;
    let avg = iAvg >= 0 ? numv(r[iAvg]) : null; if (avg == null && iTot >= 0 && numv(r[iTot]) != null) avg = numv(r[iTot]) / qty;
    pos.push({ acct: acctOf(r), sym, qty, avg: avg ?? 0, type: iType >= 0 ? r[iType] : "" });
  }
  if (!pos.length) return toast("No stock positions found in that file.");
  pendingImport = pos;
  const accts = [...new Set(pos.map(p => p.acct))];
  sheet(`<h3>Import positions</h3><p class="t2">Pick the account(s) to import. This replaces the positions for those stocks.</p>
    ${accts.map(a => `<label class="stack" style="padding:6px 0"><input type="checkbox" value="${esc(a)}" class="acct" ${accts.length === 1 ? "checked" : ""}>
      <span>${esc(a)} <span class="mute">· ${pos.filter(p => p.acct === a).map(p => p.sym).join(", ")}</span></span></label>`).join("")}
    <div class="stack" style="margin-top:12px"><button class="btn ghost" data-act="close">Cancel</button><button class="btn" data-act="doImport" style="margin-left:auto">Import</button></div>`);
}
async function doImport() {
  const chosen = [...document.querySelectorAll(".acct:checked")].map(x => x.value);
  const pos = pendingImport.filter(p => chosen.includes(p.acct)); if (!pos.length) return toast("Pick at least one account.");
  const agg = {};
  pos.forEach(p => { const a = agg[p.sym] || (agg[p.sym] = { shares: 0, costTotal: 0 }); a.shares += p.qty; a.costTotal += p.qty * p.avg; });
  const newSyms = [];
  Object.entries(agg).forEach(([s, a]) => {
    J.holdings[s] = { shares: +a.shares.toFixed(6), cost: +(a.costTotal / a.shares).toFixed(4) };
    stock(s).status = "owned";
    if (!symbols().includes(s) || !LIST.some(t => t.symbol === s)) newSyms.push(s);
  });
  newSyms.forEach(s => { if (!J.extraTickers.includes(s)) J.extraTickers.push(s); });
  save(); closeSheet(); ui.tab = "portfolio"; ui.sym = null; render();
  toast(`Imported ${Object.keys(agg).length} positions`);
  if (cfg.token) for (const s of newSyms) { try { await addTickerRemote(s); } catch (e) { console.warn(e); } }
}

// ------------------------------------------------------------ events
document.addEventListener("click", async e => {
  const el = e.target.closest("[data-act]"); if (!el || el.tagName === "INPUT") return;
  const a = el.dataset.act, v = el.dataset.v;
  if (a === "tab") { ui.tab = v; ui.sym = null; render(); scrollTo(0, 0); }
  else if (a === "open") { ui.sym = v; ui.sub = "thesis"; render(); scrollTo(0, 0); }
  else if (a === "back") { ui.sym = null; render(); }
  else if (a === "sub") { ui.sub = v; render(); }
  else if (a === "filter") { ui.filter = v; render(); }
  else if (a === "learn") learnSheet(v, ui.sym);
  else if (a === "close") closeSheet();
  else if (a === "walk") walkSheet(ui.sym, +v);
  else if (a === "walkDone") entrySheet(ui.sym, { type: "Earnings review", text: "My take on the numbers:\n" });
  else if (a === "status") { stock(ui.sym).status = v; save(); render(); }
  else if (a === "conv") { el.parentElement.querySelectorAll("button").forEach(b => b.classList.toggle("on", b === el)); }
  else if (a === "saveThesis" || a === "saveThesisLog") {
    const st = stock(ui.sym), before = JSON.stringify(st.thesis || {}), t = Object.assign({}, st.thesis);
    document.querySelectorAll("[data-th]").forEach(i => t[i.dataset.th] = i.value.trim());
    const on = document.querySelector(".conv button.on"); t.conviction = on ? +on.textContent : t.conviction;
    t.updated = today(); st.thesis = t; save();
    if (a === "saveThesisLog" && before !== JSON.stringify(t)) {
      entrySheet(ui.sym, { type: "Thesis change", text: "What changed and why:\n" });
    } else { toast("Thesis saved"); render(); }
  }
  else if (a === "newEntry") entrySheet(ui.sym);
  else if (a === "saveEntry") {
    const text = document.getElementById("eText").value.trim(); if (!text) return toast("Write something first.");
    stock(ui.sym).entries.push({ id: Date.now().toString(36), type: document.getElementById("eType").value, date: document.getElementById("eDate").value || today(),
      price: numv(document.getElementById("ePrice").value), text });
    save(); closeSheet(); ui.sub = "journal"; render();
  }
  else if (a === "delEntry") { if (!confirm("Delete this entry?")) return; const st = stock(ui.sym); st.entries = st.entries.filter(x => x.id !== v); save(); render(); }
  else if (a === "editPos") posSheet(v);
  else if (a === "savePos") {
    const sh = numv(document.getElementById("pShares").value), c = numv(document.getElementById("pCost").value);
    if (!sh) delete J.holdings[ui.sym]; else { J.holdings[ui.sym] = { shares: sh, cost: c || 0 }; stock(ui.sym).status = "owned"; }
    save(); closeSheet(); render();
  }
  else if (a === "addTicker") {
    const s = (prompt("Ticker symbol (e.g. INTU)") || "").trim().toUpperCase(); if (!/^[A-Z.\-]{1,6}$/.test(s)) return;
    if (!J.extraTickers.includes(s)) J.extraTickers.push(s); stock(s); save();
    if (cfg.token) { try { await addTickerRemote(s); toast(`${s} added — data arrives in a few minutes`); } catch (err) { toast("Saved here, but couldn't update the data list: " + err.message); } }
    else toast(`${s} added. Add a token in Settings so the daily job fetches its data.`);
    ui.sym = s; ui.sub = "thesis"; render();
  }
  else if (a === "saveTok") { cfg.token = document.getElementById("tok").value.trim(); writeLS(CFG_KEY, cfg); await pull(); const s2 = document.getElementById("sync2"); if (s2) s2.textContent = ui.syncState; }
  else if (a === "export") {
    const blob = new Blob([JSON.stringify(J, null, 1)], { type: "application/json" }), u = URL.createObjectURL(blob);
    const l = document.createElement("a"); l.href = u; l.download = `stock-journal-${today()}.json`; l.click(); URL.revokeObjectURL(u);
  }
  else if (a === "doImport") doImport();
  else if (a === "resetWeights") { cfg.weights = { ...DEFAULT_WEIGHTS }; writeLS(CFG_KEY, cfg); applyWeights(); render(); toast("Weights reset"); }
});
document.addEventListener("change", async e => {
  const a = e.target.dataset.act;
  if (a === "csv" && e.target.files[0]) importFidelity(await e.target.files[0].text());
  else if (a === "importJson" && e.target.files[0]) {
    try { const d = JSON.parse(await e.target.files[0].text()); if (!d.stocks) throw 0; J = d; save(); render(); toast("Backup restored"); } catch { toast("That isn't a journal backup."); }
  }
  else if (a === "toggleLearn") { cfg.learn = e.target.checked; writeLS(CFG_KEY, cfg); }
  else if (a === "weight") { cfg.weights[e.target.dataset.k] = +e.target.value; writeLS(CFG_KEY, cfg); applyWeights(); toast("Grades updated"); }
});
document.addEventListener("input", e => {
  if (e.target.dataset.act !== "weight") return;
  const k = e.target.dataset.k, el = document.getElementById("wv-" + k); if (el) el.textContent = e.target.value;
  const tot = document.getElementById("wtotal"); if (tot) tot.textContent = `Total ${[...document.querySelectorAll("[data-act=weight]")].reduce((a, i) => a + +i.value, 0)} (scores are scaled to 100 either way)`;
});
document.addEventListener("visibilitychange", () => { if (!document.hidden) pull(); });

// ------------------------------------------------------------ boot
async function boot() {
  render();
  try { LIST = (await (await fetch(`data/tickers.json?${Date.now()}`, { cache: "no-store" })).json()).tickers; } catch {}
  try { const r = await fetch(`data/market.json?${Date.now()}`, { cache: "no-store" }); if (r.ok) { M = await r.json(); applyWeights(); } } catch {}
  symbols().forEach(s => { if (J.holdings[s]?.shares && !J.stocks[s]) stock(s).status = "owned"; });
  render(); pull();
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js").catch(() => {});
}
boot();
