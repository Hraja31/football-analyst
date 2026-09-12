/* Footage → Tactics — coach-facing frontend.
 *
 * Talks to n8n over three webhooks and nothing else:
 *   POST {SUBMIT_URL}               -> { match_id }        (workflow nodes 1/13)
 *   GET  {REPORT_URL}?match_id      -> { status, team, players, cv, degraded }  (node 12)
 *   GET  {REPORT_HTML_URL}?match_id -> a shareable HTML page (node 11b)
 *
 * With no backend configured it runs entirely off sample-data/, so the demo
 * survives a dead network or an unfinished workflow.
 */

const CONFIG = {
  SUBMIT_URL: "https://shaneesilva.app.n8n.cloud/webhook/match-submit",
  REPORT_URL: "https://shaneesilva.app.n8n.cloud/webhook/report",
  REPORT_HTML_URL: "https://shaneesilva.app.n8n.cloud/webhook/report-html",
  POLL_MS: 2500,
  // A healthy live run lands in ~75 s and a slow one has taken 100 s, so 40
  // polls sat exactly on the boundary and would occasionally fall back to the
  // sample report with a perfectly good report seconds away.
  POLL_MAX: 72,     // ~180s
};

const DATA = "../sample-data";
const $ = (s) => document.querySelector(s);
const el = (t, c, h) => { const n = document.createElement(t); if (c) n.className = c; if (h != null) n.innerHTML = h; return n; };
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const num = (v, d = 0) => (v == null || Number.isNaN(v) ? "—" : Number(v).toFixed(d));

let roster = null;      // §6a
let cv = null;          // §6b
let lastMatchId = null; // for the shareable-report link

/* ── startup ──────────────────────────────────────────────── */

async function init() {
  try {
    roster = await (await fetch(`${DATA}/roster.json`)).json();
    renderRosterTable(roster);
  } catch {
    $("#rosterTable").innerHTML = "<p class='hint'>Could not load roster.json — serve this folder over HTTP, not file://</p>";
  }

  const live = Boolean(CONFIG.SUBMIT_URL && CONFIG.REPORT_URL);
  $("#endpointHint").textContent = live
    ? `Connected to ${new URL(CONFIG.SUBMIT_URL).host}`
    : "No n8n endpoints configured — set SUBMIT_URL and REPORT_URL in app.js. 'Load sample report' works regardless.";

  $("#demoBtn").addEventListener("click", loadSample);
  $("#matchForm").addEventListener("submit", onSubmit);

  // ?match_id=m_001 deep-links to a finished report; ?demo=1 is the offline
  // fallback we keep open in a second tab during the pitch.
  const qs = new URLSearchParams(location.search);
  if (qs.get("demo")) loadSample();
  else if (qs.get("match_id") && live) showReportFor(qs.get("match_id"));
}

function renderRosterTable(r) {
  const stats = Object.fromEntries(r.manual_stats.map((s) => [s.player_id, s]));
  const cols = [["goals", "G"], ["assists", "A"], ["shots", "Sh"], ["shots_on_target", "SoT"],
                ["tackles", "Tk"], ["tackles_won", "TkW"], ["yellow", "Y"], ["saves", "Sv"]];
  const rows = r.players.map((p) => {
    const s = stats[p.player_id] || {};
    const cells = cols.map(([k]) =>
      `<td><input data-pid="${p.player_id}" data-k="${k}" value="${s[k] ?? ""}" inputmode="numeric"></td>`).join("");
    return `<tr><td>${p.jersey_number}</td><td>${esc(p.name)}</td><td>${p.position}</td>${cells}</tr>`;
  }).join("");
  $("#rosterTable").innerHTML =
    `<table><thead><tr><th>#</th><th>Player</th><th>Pos</th>${cols.map(([, l]) => `<th>${l}</th>`).join("")}</tr></thead><tbody>${rows}</tbody></table>`;
  $("#rosterCount").textContent = `(${r.players.length} players)`;
}

/* ── submit + poll ────────────────────────────────────────── */

function collectPayload() {
  const f = new FormData($("#matchForm"));
  const stats = JSON.parse(JSON.stringify(roster.manual_stats));
  const byId = Object.fromEntries(stats.map((s) => [s.player_id, s]));
  document.querySelectorAll("#rosterTable input").forEach((i) => {
    const row = byId[i.dataset.pid];
    if (row) row[i.dataset.k] = i.value === "" ? null : Number(i.value);
  });
  return {
    ...roster,
    match_id: `m_${Date.now().toString(36)}`,
    opponent: f.get("opponent"),
    date: f.get("date"),
    footage_url: f.get("footage_url"),
    coach_notes: f.get("coach_notes"),
    playing_style: f.get("playing_style"),
    manual_stats: stats,
  };
}

async function onSubmit(e) {
  e.preventDefault();
  if (!CONFIG.SUBMIT_URL) { loadSample(); return; }

  const payload = collectPayload();
  showView("progress");
  step("submit", "active");
  try {
    // text/plain is deliberate. It is one of the three content types a browser
    // will send without a CORS preflight, and an n8n Cloud webhook does not
    // answer the OPTIONS request that application/json would trigger. The body
    // is still JSON — node 2 parses a string body or an object, whichever it gets.
    const res = await fetch(CONFIG.SUBMIT_URL, {
      method: "POST",
      headers: { "Content-Type": "text/plain;charset=UTF-8" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) throw new Error(`submit returned ${res.status}`);
    const { match_id } = await res.json();
    step("submit", "done");
    await showReportFor(match_id || payload.match_id);
  } catch (err) {
    $("#progressHint").textContent = `${err.message} — falling back to the sample report.`;
    setTimeout(loadSample, 1200);
  }
}

async function showReportFor(matchId) {
  showView("progress");
  lastMatchId = matchId;
  step("submit", "done"); step("cv", "active");
  for (let i = 0; i < CONFIG.POLL_MAX; i++) {
    await new Promise((r) => setTimeout(r, CONFIG.POLL_MS));
    try {
      const res = await fetch(`${CONFIG.REPORT_URL}?match_id=${encodeURIComponent(matchId)}`);
      if (!res.ok) continue;
      const body = await res.json();
      if (body.status === "done" || body.team) {
        ["cv", "merge", "claude", "done"].forEach((s) => step(s, "done"));
        return render(body.team ? body : body.report, body.cv || cv, body.degraded);
      }
      if (body.stage) step(body.stage, "active");
    } catch { /* keep polling — transient network */ }
  }
  $("#progressHint").textContent = "Timed out waiting for the report. Showing the sample instead.";
  setTimeout(loadSample, 1200);
}

async function loadSample() {
  showView("progress");
  const seq = ["submit", "cv", "merge", "claude", "done"];
  for (const s of seq) { step(s, "active"); await new Promise((r) => setTimeout(r, 420)); step(s, "done"); }
  const [report, cvData] = await Promise.all([
    fetch(`${DATA}/report.sample.json`).then((r) => r.json()),
    fetch(`${DATA}/cv_result.json`).then((r) => r.json()),
  ]);
  render(report, cvData, false, true);
}

function showView(v) {
  $("#submitView").hidden = v !== "submit";
  $("#progressView").hidden = v !== "progress";
  $("#reportView").hidden = v !== "report";
}

function step(name, state) {
  const li = document.querySelector(`[data-step="${name}"]`);
  if (li) li.className = state;
}

/* ── render ───────────────────────────────────────────────── */

function render(report, cvData, degraded, isSample) {
  cv = cvData;
  showView("report");

  const r = roster || {};
  $("#matchMeta").innerHTML = `${esc(r.opponent || "")} <span style="opacity:.5">·</span> ${esc(r.date || "")}`;
  $("#formationBadge").textContent = report.team.formation;
  $("#styleNote").textContent = report.team.playing_style_note || "";

  $("#pitch").innerHTML = pitchSvg(report.team.best_xi, report.players);
  $("#teamSwot").innerHTML = ["strengths", "weaknesses", "opportunities", "threats"]
    .map((k) => swotBlock(k, report.team.team_swot[k])).join("");

  $("#posWeak").innerHTML = (report.team.position_weaknesses || [])
    .map((w) => `<div class="weak"><b>${esc(w.area)}</b> — ${esc(w.issue)}</div>`).join("");

  const cards = report.players
    .slice()
    .sort((a, b) => b.rating - a.rating)
    .map((p) => playerCard(p)).join("");
  $("#players").innerHTML = cards;
  $("#playerCount").textContent = `· ranked by rating`;

  const unresolved = (cvData?.players || []).filter((p) => !p.player_id).length;
  const notes = [];
  if (isSample) notes.push("Showing the bundled sample report (no live pipeline run).");
  if (degraded) notes.push("CV unavailable for this match — analysis is based on manual stats and coach notes only.");
  if (unresolved) notes.push(`${unresolved} track${unresolved > 1 ? "s" : ""} could not be matched to a shirt number and ${unresolved > 1 ? "were" : "was"} used for team shape only.`);
  $("#dataNote").textContent = notes.join(" ");

  // The same report as a single self-contained page (workflow node 11b) — the
  // thing a coach actually forwards to a player.
  const share = $("#shareLink");
  if (share) {
    const live = lastMatchId && CONFIG.REPORT_HTML_URL;
    share.hidden = !live;
    if (live) share.href = `${CONFIG.REPORT_HTML_URL}?match_id=${encodeURIComponent(lastMatchId)}`;
  }
  if (degraded) $("#teamPanel").prepend(el("div", "banner", "⚠ Computer vision did not complete for this match. Spatial data is missing; the report below uses manual stats and coach notes."));
}

function swotBlock(kind, items) {
  const cls = kind[0];
  return `<div class="swot ${cls}"><h4>${kind}</h4><ul>${(items || []).map((i) => `<li>${esc(i)}</li>`).join("")}</ul></div>`;
}

function playerCard(p) {
  const info = (roster?.players || []).find((x) => x.player_id === p.player_id) || {};
  const track = (cv?.players || []).find((x) => x.player_id === p.player_id) || {};
  const manual = (roster?.manual_stats || []).find((x) => x.player_id === p.player_id) || {};
  const rc = p.rating >= 7.5 ? "r-hi" : p.rating >= 6.5 ? "r-mid" : "r-lo";
  const conf = track.id_confidence;
  const confBadge = conf == null ? ""
    : `<span class="conf ${conf < 0.75 ? "low" : ""}" title="CV identity confidence">ID ${Math.round(conf * 100)}%</span>`;

  const hm = track.heatmap_url
    ? `<img class="hm" src="${esc(track.heatmap_url.startsWith("http") ? track.heatmap_url : `${DATA}/heatmaps/${p.player_id}.svg`)}" alt="Heatmap for ${esc(info.name)}" loading="lazy">`
    : "";

  const stats = [
    [manual.goals ?? 0, "goals"],
    [manual.assists ?? 0, "assists"],
    [track.distance_m ? `${num(track.distance_m)}m` : "—", "distance"],
    [track.top_speed_kmh ? num(track.top_speed_kmh, 1) : "—", "km/h"],
  ].map(([v, l]) => `<div class="stat"><b>${v}</b><span>${l}</span></div>`).join("");

  const mini = ["strengths", "weaknesses"].map((k) =>
    `<div><b>${k}</b><ul>${(p.swot?.[k] || []).slice(0, 2).map((i) => `<li>${esc(i)}</li>`).join("")}</ul></div>`).join("");

  const plan = (p.training_plan || []).map((d) =>
    `<div class="drill"><b>${esc(d.focus)}</b> — ${esc(d.drill)}<em>${esc(d.why)}</em></div>`).join("");

  return `<article class="card">
    <header>
      <div class="shirt">${info.jersey_number ?? "?"}</div>
      <div class="who"><b>${esc(info.name || p.player_id)}</b><span>${esc(info.position || "")} · ${esc(info.strong_foot || "?")} foot ${confBadge}</span></div>
      <div class="rating ${rc}">${num(p.rating, 1)}</div>
    </header>
    ${hm}
    <div class="stats">${stats}</div>
    <div class="body">
      <p class="summary">${esc(p.summary)}</p>
      <div class="mini-swot">${mini}</div>
      <div class="plan"><h4>Training plan</h4>${plan}</div>
    </div>
  </article>`;
}

/* Best XI plotted on a pitch, positioned from the CV average positions where we
 * have them and from a formation template where we don't. */
function pitchSvg(xi, players) {
  const W = 680, H = 440, PX = 105, PY = 68;
  const ratings = Object.fromEntries((players || []).map((p) => [p.player_id, p.rating]));
  const FALLBACK = { GK: [6, 34], RB: [30, 58], CB: [22, 40], LB: [30, 10], CDM: [45, 34],
                     CM: [58, 46], RW: [80, 56], LW: [80, 12], ST: [88, 34] };
  const seen = {};

  const nodes = xi.map((slot) => {
    const t = (cv?.players || []).find((x) => x.player_id === slot.player_id);
    let px, py;
    if (t?.avg_position) { px = t.avg_position.x; py = t.avg_position.y; }
    else {
      const k = slot.position;
      const base = FALLBACK[k] || [52, 34];
      seen[k] = (seen[k] || 0) + 1;
      px = base[0]; py = base[1] + (seen[k] > 1 ? 16 : 0);   // split duplicate slots
    }
    const info = (roster?.players || []).find((x) => x.player_id === slot.player_id) || {};
    return { x: (px / PX) * W, y: (py / PY) * H, info, rating: ratings[slot.player_id] };
  });

  // Average positions cluster in midfield and the markers collide. Relax them
  // apart just far enough that every shirt number and name stays readable.
  const MIN = 46;
  for (let pass = 0; pass < 80; pass++) {
    let moved = false;
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const a = nodes[i], b = nodes[j];
        let dx = b.x - a.x, dy = b.y - a.y;
        let d = Math.hypot(dx, dy);
        if (d >= MIN) continue;
        if (d < 0.01) { dx = (i % 2 ? 1 : -1); dy = 0.6; d = 1; }
        const push = (MIN - d) / 2;
        const ux = (dx / d) * push, uy = (dy / d) * push;
        a.x -= ux; a.y -= uy; b.x += ux; b.y += uy;
        moved = true;
      }
    }
    nodes.forEach((n) => {
      n.x = Math.max(22, Math.min(W - 22, n.x));
      n.y = Math.max(22, Math.min(H - 34, n.y));
    });
    if (!moved) break;
  }

  const marks = nodes.map(({ x, y, info, rating }) => {
    const fill = rating >= 7.5 ? "#2fbf71" : rating >= 6.5 ? "#e0a33e" : "#d6402a";
    return `<g>
      <circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="15" fill="${fill}" stroke="#0b1014" stroke-width="2"/>
      <text x="${x.toFixed(1)}" y="${(y + 4.5).toFixed(1)}" text-anchor="middle" font-size="13" font-weight="700" fill="#06210f">${info.jersey_number ?? ""}</text>
      <text x="${x.toFixed(1)}" y="${(y + 29).toFixed(1)}" text-anchor="middle" font-size="10.5" fill="#cfe0ea">${esc((info.name || "").split(" ").pop())}</text>
    </g>`;
  }).join("");

  const L = 'fill="none" stroke="rgba(255,255,255,.28)" stroke-width="1.5"';
  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Best XI on the pitch">
    <rect width="${W}" height="${H}" fill="#12301f"/>
    <rect x="8" y="8" width="${W - 16}" height="${H - 16}" ${L}/>
    <line x1="${W / 2}" y1="8" x2="${W / 2}" y2="${H - 8}" ${L}/>
    <circle cx="${W / 2}" cy="${H / 2}" r="52" ${L}/>
    <rect x="8" y="${H / 2 - 88}" width="72" height="176" ${L}/>
    <rect x="${W - 80}" y="${H / 2 - 88}" width="72" height="176" ${L}/>
    ${marks}
  </svg>`;
}

init();
