// Trade Board — Discord Activity front end.
// Runs inside Discord's iframe: signs the viewer in with the Embedded App SDK,
// then polls the bot's API for the server's open trades.
import { DiscordSDK } from "./vendor/discord-sdk.js";

const REFRESH_MS = 15000;
const params = new URLSearchParams(location.search);
const inDiscord = params.has("frame_id");
const demo = params.has("demo");

const $ = (id) => document.getElementById(id);
let sdk = null;
let accessToken = null;
let guildId = null;
let board = null;
let filter = "All";
let openTradeId = null;

// ------------------------------------------------------------------ formatting
const fmtR = (r) => (r == null ? "—" : `${r >= 0 ? "+" : "−"}${Math.abs(r).toFixed(1)}R`);
const tone = (r) => (r == null || Math.abs(r) < 0.05 ? "flat" : r > 0 ? "pos" : "neg");
function fmtPrice(p) {
  if (p == null) return "—";
  let s;
  if (p >= 1000) s = p.toLocaleString("en-US", { maximumFractionDigits: 2 });
  else if (p >= 1) s = p.toFixed(4);
  else s = p.toFixed(Math.min(12, Math.max(4, -Math.floor(Math.log10(p)) + 3)));
  if (s.includes(".")) s = s.replace(/0+$/, "").replace(/\.$/, "");
  return "$" + s;
}
function fmtAge(sec) {
  sec = Math.max(0, sec);
  const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60);
  if (d) return d < 3 ? `${d}d ${h}h` : `${d}d`;
  return h ? `${h}h ${m}m` : `${m}m`;
}
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const now = () => Date.now() / 1000;

// ------------------------------------------------------------------ API
async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: { ...(opts.headers || {}), ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}) },
  });
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res;
}

async function signIn() {
  const { client_id } = await (await api("api/config")).json();
  sdk = new DiscordSDK(client_id);
  await sdk.ready();
  guildId = sdk.guildId;
  if (!guildId) throw new Error("Open the Trade Board from inside your server.");
  const { code } = await sdk.commands.authorize({
    client_id, response_type: "code", state: "", prompt: "none", scope: ["identify", "guilds"],
  });
  const tok = await (await api("api/token", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code }),
  })).json();
  accessToken = tok.access_token;
  await sdk.commands.authenticate({ access_token: accessToken });
}

async function loadBoard() {
  board = demo ? demoBoard() : await (await api(`api/board?guild_id=${guildId}`)).json();
  render();
  if (openTradeId != null) refreshDetail();
}

// ------------------------------------------------------------------ the ladder
// Left edge = original stop, right edge = last target, so "right" always means profit.
function ladder(t, { labels = false } = {}) {
  const lo = t.orig_stop;
  const hi = t.targets[t.targets.length - 1].price;
  const pos = (v) => Math.min(1, Math.max(0, (v - lo) / (hi - lo)));
  const pct = (v) => `${(pos(v) * 100).toFixed(2)}%`;
  const entryX = pos(t.entry);
  let html = `<div class="ladder" aria-hidden="true"><div class="track">`;
  html += `<div class="loss-zone" style="width:${(entryX * 100).toFixed(2)}%"></div>`;
  if (t.status === "open" && t.last_price != null) {
    const nowX = pos(t.last_price);
    const a = Math.min(entryX, nowX), b = Math.max(entryX, nowX);
    html += `<div class="run ${nowX >= entryX ? "pos" : "neg"}" style="left:${a * 100}%;width:${(b - a) * 100}%"></div>`;
  }
  html += `</div>`;
  html += `<span class="tick stop" style="left:0"></span>`;
  if (Math.abs(t.stop - t.orig_stop) > 1e-12) html += `<span class="tick stop" style="left:${pct(t.stop)}"></span>`;
  html += `<span class="tick entry" style="left:${pct(t.entry)}"></span>`;
  t.targets.forEach((tg, i) => {
    html += `<span class="tick target${tg.hit ? " hit" : ""}" style="left:${pct(tg.price)}"></span>`;
    if (labels) html += `<span class="tick-label${tg.hit ? " hit" : ""}" style="left:${pct(tg.price)}">T${i + 1}</span>`;
  });
  if (labels) html += `<span class="tick-label" style="left:${pct(t.entry)}">Entry</span>`;
  if (t.last_price != null) {
    const x = pos(t.last_price);
    const edge = x < 0.08 ? " edge-l" : x > 0.92 ? " edge-r" : "";
    const over = t.last_price > Math.max(lo, hi) || t.last_price < Math.min(lo, hi);
    html += `<span class="now${edge}" style="left:${(x * 100).toFixed(2)}%">${over ? "▸ " : ""}${fmtPrice(t.last_price)}</span>`;
  }
  return html + `</div>`;
}

function describeLadder(t) {
  const hit = t.targets.filter((x) => x.hit).length;
  return `${hit} of ${t.targets.length} targets hit, price ${fmtPrice(t.last_price)}`;
}

// ------------------------------------------------------------------ list view
function row(t) {
  const side = t.direction === "long" ? "Long" : "Short";
  const pending = t.status === "pending";
  const held = fmtAge(now() - (t.opened_at || t.created_at));
  const meta = [t.analyst];
  if (!pending) {
    if (t.remaining < 99.99) meta.push(`${+t.remaining.toFixed(1)}% left`);
    if (Math.abs(t.stop - t.entry) < 1e-12) meta.push("stop at entry");
    meta.push(held);
  } else meta.push(`waiting ${fmtAge(now() - t.created_at)}`);
  const right = pending
    ? `<div class="r">${fmtPrice(t.entry)}<small>limit</small></div>`
    : `<div class="r ${tone(t.live_r)}">${fmtR(t.live_r)}<small>banked ${fmtR(t.banked)}</small></div>`;
  return `<button class="row${pending ? " pending" : ""}" data-id="${t.id}"
      aria-label="${esc(`${t.base} ${side} by ${t.analyst}, ${pending ? "pending" : fmtR(t.live_r)}, ${describeLadder(t)}`)}">
    <div class="who">
      <div class="pair">${esc(t.base)}<span class="quote">/${esc(t.quote)}</span><span class="side ${t.direction}">${side}</span></div>
      <div class="meta">${esc(meta.join(", "))}</div>
    </div>
    ${ladder(t)}
    ${right}
  </button>`;
}

function render() {
  $("hub").textContent = `${board.hub_name} trade board`;
  document.title = `${board.hub_name} trade board`;
  const all = board.trades;
  const analysts = [...new Set(all.map((t) => t.analyst))].sort();
  if (filter !== "All" && !analysts.includes(filter)) filter = "All";

  $("filters").innerHTML = analysts.length > 1
    ? ["All", ...analysts].map((a) => `<button class="chip" aria-pressed="${a === filter}" data-a="${esc(a)}">${esc(a)}</button>`).join("")
    : "";

  const shown = all.filter((t) => filter === "All" || t.analyst === filter);
  const open = shown.filter((t) => t.status === "open").sort((a, b) => b.live_r - a.live_r);
  const pending = shown.filter((t) => t.status === "pending");
  const total = open.reduce((s, t) => s + t.live_r, 0);

  if (open.length) {
    const up = open.filter((t) => t.live_r > 0.05).length;
    const down = open.filter((t) => t.live_r < -0.05).length;
    $("summary").innerHTML = `<strong>${open.length} open</strong> at <strong class="${tone(total)}">${fmtR(total)}</strong> combined, ${up} up and ${down} down`
      + (pending.length ? `, ${pending.length} waiting for entry` : "");
  } else {
    $("summary").textContent = pending.length ? `${pending.length} waiting for entry` : "No open trades";
  }

  $("open-list").innerHTML = open.map(row).join("");
  $("pending-list").innerHTML = pending.map(row).join("");
  $("pending-wrap").hidden = !pending.length;
  $("empty").hidden = !!shown.length;
  $("empty").innerHTML = `<strong>No open trades right now</strong>New calls from your analysts show up here as soon as they post them.`;

  $("live").hidden = false;
  $("updated").textContent = `Live, updated ${new Date(board.generated_at * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
}

// ------------------------------------------------------------------ detail view
async function openDetail(id) {
  openTradeId = id;
  $("detail").hidden = false;
  $("detail-body").innerHTML = `<p class="d-r-sub">Loading…</p>`;
  await refreshDetail(true);
  $("close").focus();
}

async function refreshDetail(first = false) {
  const id = openTradeId;
  let t;
  try {
    t = demo ? demoTrade(id) : await (await api(`api/trades/${id}`)).json();
  } catch (e) {
    $("detail-body").innerHTML = `<p class="d-r-sub">Couldn't load this trade. It may have been cancelled.</p>`;
    return;
  }
  if (id !== openTradeId) return;
  const side = t.direction === "long" ? "Long" : "Short";
  const pending = t.status === "pending";
  const levels = [
    `<tr><td>Stop</td><td>${Math.abs(t.stop - t.orig_stop) > 1e-12 ? `<span class="struck">${fmtPrice(t.orig_stop)}</span>` : ""}${fmtPrice(t.stop)}</td><td>${Math.abs(t.stop - t.orig_stop) > 1e-12 ? "moved" : "−1.0R"}</td></tr>`,
    `<tr><td>Entry</td><td>${fmtPrice(t.entry)}</td><td>${pending ? "limit" : ""}</td></tr>`,
    ...t.targets.map((tg, i) => `<tr class="${tg.hit ? "hit" : ""}"><td>T${i + 1}${tg.hit ? " hit" : ""}</td><td>${fmtPrice(tg.price)}</td><td>${fmtR(tg.r)}, ${+tg.pct.toFixed(1)}%</td></tr>`),
  ];
  const runner = 100 - t.targets.reduce((s, x) => s + x.pct, 0);
  if (runner > 0.01) levels.push(`<tr><td>Runner</td><td>${+runner.toFixed(1)}%</td><td>until stop</td></tr>`);

  const chartSlot = t.has_chart ? `<img class="chart" id="d-chart" alt="Chart for ${esc(t.base)} ${side}">` : "";
  $("detail-body").innerHTML = `
    <div class="d-head">
      <h2 id="d-title">${esc(t.base)}<span class="quote">/${esc(t.quote)}</span><span class="side ${t.direction}">${side}</span></h2>
      <p>${esc(t.analyst)}, opened ${new Date((t.opened_at || t.created_at) * 1000).toLocaleDateString([], { day: "numeric", month: "short" })}</p>
    </div>
    ${pending
      ? `<div class="d-r" style="color:var(--wait)">Waiting</div><div class="d-r-sub">Limit at ${fmtPrice(t.entry)}, price now ${fmtPrice(t.last_price)}</div>`
      : `<div class="d-r ${tone(t.live_r)}">${fmtR(t.live_r)}</div>
         <div class="d-r-sub">${fmtR(t.banked)} banked, ${+t.remaining.toFixed(1)}% still running${t.stop_dist_r != null ? `, ${t.stop_dist_r.toFixed(1)}R above the stop` : ""}</div>`}
    <div class="d-ladder">${ladder(t, { labels: true })}</div>
    ${chartSlot}
    <table class="levels"><tbody>${levels.join("")}</tbody></table>
    ${t.notes ? `<div class="notes">${esc(t.notes)}</div>` : ""}
    <h3 class="d-sub">History</h3>
    <ol class="timeline">${t.events.slice().reverse().map((e) =>
      `<li><time>${new Date(e.at * 1000).toLocaleDateString([], { day: "numeric", month: "short" })}</time><span>${esc(e.text)}</span></li>`).join("")}</ol>
    ${t.src_url ? `<button class="btn" id="d-src">Open ${esc(t.analyst)}'s post</button>` : ""}`;

  if (t.has_chart) loadChart(t);
  const src = $("d-src");
  if (src) src.onclick = () => (sdk ? sdk.commands.openExternalLink({ url: t.src_url }) : window.open(t.src_url, "_blank"));
}

const chartCache = new Map();
async function loadChart(t) {
  const img = $("d-chart");
  if (!img) return;
  try {
    let url = chartCache.get(t.id);
    if (!url) {
      url = demo ? demoChart() : URL.createObjectURL(await (await api(`api/trades/${t.id}/chart`)).blob());
      chartCache.set(t.id, url);
    }
    img.src = url;
    img.onclick = () => img.classList.toggle("full");
  } catch {
    img.remove();
  }
}

function closeDetail() {
  const id = openTradeId;
  openTradeId = null;
  $("detail").hidden = true;
  document.querySelector(`.row[data-id="${id}"]`)?.focus();
}

// ------------------------------------------------------------------ events
document.addEventListener("click", (e) => {
  const chip = e.target.closest(".chip");
  if (chip) { filter = chip.dataset.a; render(); return; }
  const r = e.target.closest(".row");
  if (r) { openDetail(Number(r.dataset.id)); return; }
  if (e.target.id === "close" || e.target.id === "detail") closeDetail();
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && openTradeId != null) closeDetail(); });

// ------------------------------------------------------------------ start
async function start() {
  try {
    if (!demo) {
      if (!inDiscord) {
        $("summary").textContent = "Open this from the Trade Board button in your Discord server.";
        return;
      }
      await signIn();
    }
    await loadBoard();
    setInterval(() => loadBoard().catch((e) => console.warn(e)), REFRESH_MS);
  } catch (e) {
    console.error(e);
    $("summary").textContent = String(e.message || e).includes("403")
      ? "You need to be a member of this server to see its trades."
      : `Couldn't load the board: ${e.message || e}`;
  }
}
start();

// ------------------------------------------------------------------ demo data (?demo)
function demoBoard() {
  const t0 = now();
  const mk = (id, analyst, base, direction, entry, stop, targets, last, o = {}) => ({
    id, analyst, base, quote: "USDT", direction, status: "open", entry, stop, orig_stop: stop,
    targets: targets.map(([price, pct, hit]) => ({ price, pct, hit: !!hit, r: +(((price - entry) / Math.abs(entry - stop)) * (direction === "long" ? 1 : -1)).toFixed(2) })),
    remaining: 100, banked: 0, last_price: last, created_at: t0 - 86400 * (o.age || 3), opened_at: t0 - 86400 * (o.age || 3),
    has_chart: true, src_url: "https://discord.com", ...o,
  });
  const trades = [
    mk(1, "CBS", "ZEC", "long", 30, 25, [[60, 25, 1], [100, 25, 1], [200, 25, 1], [400, 25]], 360, { age: 32, stop: 30, remaining: 25, banked: 23.5 }),
    mk(2, "CBS", "DOGE", "long", 0.2, 0.18, [[0.25, 33, 1], [0.3, 33], [0.35, 34]], 0.22, { age: 17, stop: 0.2, remaining: 67, banked: 0.83 }),
    mk(3, "CBS", "SPX", "long", 1.0, 0.9, [[1.2, 33], [1.4, 33], [1.6, 34]], 1.04, { age: 6 }),
    mk(4, "Krillin", "TAO", "long", 300, 250, [[400, 30, 1], [500, 30, 1], [600, 40]], 560, { age: 54, stop: 300, remaining: 40, banked: 1.8 }),
    mk(5, "Pierre", "VVV", "long", 14.19, 12, [[17, 50, 1], [19.5, 25]], 18.1, { age: 9, stop: 14.19, remaining: 50, banked: 0.64 }),
    mk(6, "Alex", "NOK", "short", 0.05, 0.055, [[0.04, 50], [0.03, 50]], 0.0515, { age: 26 }),
    mk(7, "Alex", "NEAR", "short", 2.41, 2.6, [[2.2, 50], [2.0, 50]], 2.3, { status: "pending", age: 0.1 }),
  ];
  for (const t of trades) {
    const R = Math.abs(t.entry - t.orig_stop), s = t.direction === "long" ? 1 : -1;
    t.live_r = +(t.banked + (t.remaining / 100) * ((t.last_price - t.entry) / R) * s).toFixed(2);
    t.stop_dist_r = +(((t.last_price - t.stop) * s) / R).toFixed(2);
    if (t.status === "pending") t.live_r = 0;
  }
  return { hub_name: "Haven", trades, generated_at: t0 };
}
function demoTrade(id) {
  const t = demoBoard().trades.find((x) => x.id === id);
  return { ...t, notes: "D1 trend and the 100 MA must hold as support. Bail if we close back below the range.",
    events: [{ kind: "new", text: `New market ${t.direction} @ ${fmtPrice(t.entry)}`, at: t.created_at },
      ...t.targets.filter((x) => x.hit).map((x, i) => ({ kind: "tp", text: `T${i + 1} hit @ ${fmtPrice(x.price)}`, at: t.created_at + 86400 * (i + 1) }))] };
}
function demoChart() {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 560 300"><rect width="560" height="300" fill="#2a2f3d"/><polyline fill="none" stroke="#3dd68c" stroke-width="3" points="0,240 60,220 110,232 170,180 230,196 290,140 350,150 410,100 470,112 560,60"/><text x="20" y="34" fill="#9097a8" font-family="system-ui" font-size="16">Chart preview</text></svg>`;
  return "data:image/svg+xml;charset=utf-8," + encodeURIComponent(svg);
}
