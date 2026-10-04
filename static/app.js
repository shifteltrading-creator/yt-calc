/* YouTube Video Opportunity Analyzer — vanilla JS frontend */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

let PID = null;           // current project id
let PROJECT = null;       // current full project record
let trendChart = null, radarChart = null;

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}
const postJSON = (path, body) => api(path, {
  method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body || {})
});
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const fmt = v => (v === null || v === undefined || v === "") ? "—" : v;

/* ------------------------------------------------------------ navigation */
function showStage(name) {
  $$(".stage").forEach(s => s.classList.add("hidden"));
  $("#stage-" + name).classList.remove("hidden");
  $$("#stage-nav li").forEach(li => li.classList.toggle("current", li.dataset.stage === name));
}
$$("#stage-nav li").forEach(li => li.onclick = () => {
  if (li.dataset.stage !== "project" && !PID) { alert("Create a project first (stage 1)."); return; }
  showStage(li.dataset.stage);
});

/* ------------------------------------------------------------- projects */
async function loadProjects() {
  const rows = await api("/api/projects");
  const ul = $("#project-list");
  ul.innerHTML = "";
  rows.forEach(p => {
    const li = document.createElement("li");
    li.innerHTML = `<span>${esc(p.name)}</span><span class="score">${p.final_decision ? p.final_decision + " · " : ""}${fmt(p.score)}</span>`;
    li.onclick = () => selectProject(p.id);
    if (p.id === PID) li.classList.add("active");
    ul.appendChild(li);
  });
}

async function selectProject(pid) {
  PID = pid;
  PROJECT = await api(`/api/projects/${pid}`);
  renderProjectForm(); renderTerms(); renderCompetitors(PROJECT.competitors);
  renderTrendReportFromProject(); renderTitleLinks();
  $("#export-json").href = `/api/projects/${pid}/export.json`;
  $("#export-brief").href = `/api/projects/${pid}/brief.txt`;
  if (PROJECT.calculation) renderEngine(PROJECT.calculation.mathematical_engine || PROJECT.calculation);
  if (PROJECT.ai_report) renderAI(PROJECT.ai_report);
  if (PROJECT.decision) renderDecision(PROJECT.decision);
  if (PROJECT.publication && PROJECT.publication.comparison) renderPubCompare(PROJECT.publication.comparison, PROJECT.publication.calibration);
  loadProjects();
  showStage("trends");
}

$("#new-project-btn").onclick = () => { PID = null; PROJECT = null; $("#project-form").reset(); showStage("project"); loadProjects(); };

$("#project-form").onsubmit = async e => {
  e.preventDefault();
  const d = Object.fromEntries(new FormData(e.target));
  let p;
  if (PID) { p = await api(`/api/projects/${PID}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(d) }); }
  else p = await postJSON("/api/projects", d);
  await selectProject(p.id);
  showStage("trends");
};

function renderProjectForm() {
  if (!PROJECT) return;
  const f = $("#project-form");
  for (const [k, v] of Object.entries(PROJECT)) {
    const el = f.elements[k];
    if (el && typeof v !== "object") el.value = v ?? "";
  }
}

/* --------------------------------------------------------------- trends */
$("#term-form").onsubmit = async e => {
  e.preventDefault();
  if (!PID) return alert("Select a project first.");
  const d = Object.fromEntries(new FormData(e.target));
  try { await postJSON(`/api/projects/${PID}/terms`, d); e.target.reset(); await selectProject(PID); }
  catch (err) { alert(err.message); }
};

function renderTerms() {
  const box = $("#terms-chips");
  box.innerHTML = "";
  (PROJECT?.terms || []).forEach(t => {
    const chip = document.createElement("span");
    chip.className = "chip term";
    chip.innerHTML = `${esc(t.term)}${t.momentum != null ? " · <b>" + t.momentum + "</b>" : ""}`;
    const x = document.createElement("button");
    x.textContent = "×"; x.title = "remove";
    x.onclick = async () => { await api(`/api/terms/${t.id}`, { method: "DELETE" }); await selectProject(PID); };
    chip.appendChild(x); box.appendChild(chip);
  });
}

$("#trends-open").onclick = async () => {
  try { const u = await api(`/api/projects/${PID}/trends_url`); window.open(u.explore_url, "_blank"); }
  catch (e) { alert(e.message); }
};
$("#trends-embed").onclick = async () => {
  try {
    const u = await api(`/api/projects/${PID}/trends_url`);
    $("#trends-frame").src = u.embed_url;
  } catch (e) { alert(e.message); }
};

$("#import-csv").onclick = async () => {
  if (!PID) return alert("Select a project first.");
  const file = $("#csv-file").files[0];
  let out;
  try {
    if (file) {
      const fd = new FormData(); fd.append("csv", file);
      out = await api(`/api/projects/${PID}/trends/import`, { method: "POST", body: fd });
    } else {
      const csv = $("#csv-paste").value;
      if (!csv.trim()) return alert("Attach the exported CSV file or paste its contents.");
      out = await postJSON(`/api/projects/${PID}/trends/import`, { csv });
    }
    renderTrendReport(out.report);
    drawTrendChart(out.report);
    await selectProject(PID);
  } catch (e) { alert(e.message); }
};

function renderTrendReportFromProject() {
  const payload = (PROJECT?.terms || []).map(t => ({ term: t.term, series: t.series.map(s => s.value) }));
  if (!payload.some(p => p.series.length)) return;
  // reuse server-side aggregate via GET
  api(`/api/projects/${PID}/trends_report`).then(r => { renderTrendReport(r); drawTrendChart(r); });
}

function renderTrendReport(r) {
  const box = $("#trend-report");
  let html = `<div class="kv">
    <div><span>Momentum (0–100)</span><b>${fmt(r.overall_momentum)}</b></div>
    <div><span>Direction</span><b>${esc(r.direction)}</b></div>
    <div><span>Strongest term</span><b>${esc(fmt(r.strongest_term))}</b></div>
    <div><span>Weakest term</span><b>${esc(fmt(r.weakest_term))}</b></div>
    <div><span>Trend observations</span><b>${fmt(r.observations)}</b></div></div>`;
  html += `<table><tr><th>Term</th><th>Avg</th><th>Latest</th><th>Peak</th><th>Recent avg</th><th>Prev avg</th><th>Growth %</th><th>Slope/step</th><th>Volatility</th><th>Momentum</th></tr>`;
  (r.terms || []).forEach(t => {
    const m = t.metrics || {};
    html += `<tr><td>${esc(t.term)}</td><td>${fmt(m.average_interest)}</td><td>${fmt(m.latest_interest)}</td><td>${fmt(m.peak_interest)}</td>
      <td>${fmt(m.recent_period_average)}</td><td>${fmt(m.previous_period_average)}</td><td>${fmt(m.recent_growth_pct)}</td>
      <td>${fmt(m.slope_simple)}</td><td>${fmt(m.volatility)}</td><td><b>${fmt(t.momentum)}</b></td></tr>`;
  });
  html += "</table>";
  box.innerHTML = html;
}

function drawTrendChart(r) {
  const terms = (r.terms || []).filter(t => t.series_values?.length);
  const colors = ["#4da3ff", "#3ecf8e", "#ffb454", "#ff6b6b", "#b28dff"];
  const datasets = (PROJECT?.terms || []).map((t, i) => ({
    label: t.term, data: t.series.map(s => s.value), borderColor: colors[i % 5], tension: .25
  })).filter(d => d.data.length);
  if (!datasets.length) return;
  const labels = PROJECT.terms[0]?.series.map(s => s.date) || [];
  if (trendChart) trendChart.destroy();
  trendChart = new Chart($("#trend-chart"), {
    type: "line",
    data: { labels, datasets },
    options: { plugins: { title: { display: true, text: "Google Trends imported interest over time (normalized 0–100)", color: "#e8ecf5" } },
      scales: { x: { ticks: { color: "#93a0b8" } }, y: { ticks: { color: "#93a0b8" }, beginAtZero: true } } }
  });
}

/* ---------------------------------------------------------- competitors */
$("#comp-form").onsubmit = async e => {
  e.preventDefault();
  if (!PID) return alert("Select a project first.");
  const d = Object.fromEntries(new FormData(e.target));
  await postJSON(`/api/projects/${PID}/competitors`, d);
  e.target.reset();
  await selectProject(PID);
  refreshCompReport();
};

function renderCompetitors(rows) {
  const box = $("#comp-table");
  if (!rows?.length) { box.innerHTML = "<p class='muted'>No competitor videos yet.</p>"; return; }
  box.innerHTML = `<table><tr><th>Title</th><th>Channel</th><th>Views</th><th>Subs</th><th>Uploaded</th><th>Likes</th><th>Comments</th><th>Typical views</th><th></th></tr>` +
    rows.map(c => `<tr><td>${esc(c.title)}</td><td>${esc(c.channel)}</td><td>${fmt(c.views)}</td><td>${fmt(c.subscribers)}</td>
      <td>${fmt(c.upload_date)}</td><td>${fmt(c.likes)}</td><td>${fmt(c.comments)}</td><td>${fmt(c.channel_typical_views)}</td>
      <td><button class="btn danger tiny" onclick="delCompetitor(${c.id})">×</button></td></tr>`).join("") + "</table>";
}
window.delCompetitor = async id => { await api(`/api/competitors/${id}`, { method: "DELETE" }); await selectProject(PID); };

async function refreshCompReport() {
  const r = await api(`/api/projects/${PID}/competition_report`);
  let html = `<div class="kv">
    <div><span>Median velocity (views/day)</span><b>${fmt(r.median_velocity)}</b></div>
    <div><span>Average velocity</span><b>${fmt(r.average_velocity)}</b></div>
    <div><span>Saturation</span><b>${esc(r.saturation_level)}</b></div>
    <div><span>Competitive opportunity</span><b>${fmt(r.competitive_opportunity_score)}</b></div>
    <div><span>Evidence quality</span><b>${esc(r.evidence_quality)}</b></div></div>`;
  html += `<table><tr><th>Rank</th><th>Video</th><th>Age (d)</th><th>Views/day</th><th>Velocity index</th><th>Views/sub</th><th>Engagement %</th><th>Outlier ×typical</th></tr>`;
  r.ranked.forEach((e, i) => {
    const m = e.metrics;
    html += `<tr><td>${i + 1}</td><td>${esc(e.title || e.channel)}</td><td>${fmt(m.age_days)}</td><td>${fmt(m.views_per_day)}</td>
      <td>${fmt(m.velocity_index)}</td><td>${fmt(m.views_per_subscriber)}</td><td>${fmt(m.engagement_rate_pct)}</td><td>${fmt(m.outlier_ratio)}</td></tr>`;
  });
  html += "</table>";
  if (r.recent_high_velocity_outliers?.length)
    html += `<p class="flag"><b>Recent high-velocity outliers:</b> ${esc(r.recent_high_velocity_outliers.join(", "))} — evidence of live demand.</p>`;
  $("#comp-report").innerHTML = html;
}
$("#refresh-comp").onclick = refreshCompReport;

/* --------------------------------------------------------------- titles */
function renderTitleLinks() {
  const box = $("#title-links");
  const terms = (PROJECT?.terms || []);
  const existing = PROJECT?.titles || [];
  box.innerHTML = existing.map(t => `
    <label>Link “${esc(t.text.slice(0, 40))}…” to trend term
      <select data-title="${esc(t.text)}">
        <option value="">— none —</option>
        ${terms.map(tm => `<option ${tm.term === t.linked_term ? "selected" : ""}>${esc(tm.term)}</option>`).join("")}
      </select></label>`).join("");
}

$("#analyze-titles").onclick = async () => {
  if (!PID) return alert("Select a project first.");
  const lines = $("#titles-input").value.split("\n").map(s => s.trim()).filter(Boolean);
  if (!lines.length) return alert("Enter at least one candidate title.");
  const links = {};
  $$("#title-links select").forEach(s => { if (s.value) links[s.dataset.title] = s.value; });
  const ratings = {};
  $$("#title-ratings .rated").forEach(row => {
    const title = row.dataset.title;
    const g = {};
    $$("input", row).forEach(i => { if (i.value !== "") g[i.name] = Number(i.value); });
    if (Object.keys(g).length) ratings[title] = g;
  });
  const r = await postJSON(`/api/projects/${PID}/titles`, { titles: lines, links, ratings });
  renderTitleReport(r);
  await selectProject(PID);
};

function renderTitleReport(r) {
  let html = `<div class="kv">
    <div><span>Best title score</span><b>${fmt(r.top_score)}</b></div>
    <div><span>Recommended</span><b style="font-size:.9rem">${esc(fmt(r.best_title))}</b></div>
    <div><span>Weakest</span><b style="font-size:.9rem">${esc(fmt(r.worst_title))}</b></div></div>
    <p class="muted">${esc(r.note)}</p>`;
  html += `<table><tr><th>#</th><th>Title</th><th>Title Score</th><th>Linked momentum</th><th>Combined</th><th>Ratings source</th></tr>`;
  r.ranked.forEach((a, i) => {
    html += `<tr><td>${i + 1}</td><td>${esc(a.text)}</td><td><b>${a.title_score}</b></td>
      <td>${fmt(a.linked_momentum)}</td><td><b>${fmt(a.combined_score)}</b></td><td>${esc(a.ratings_source)}</td></tr>`;
  });
  html += "</table>";
  r.ranked.forEach(a => {
    if (a.reasons?.length || a.improvements?.length) {
      html += `<p style="margin-bottom:2px"><b>${esc(a.text)}</b></p><ul class="tight">`;
      (a.reasons || []).forEach(x => html += `<li>Reason: ${esc(x)}</li>`);
      (a.improvements || []).forEach(x => html += `<li>Improvement: ${esc(x)}</li>`);
      html += "</ul>";
    }
  });
  $("#title-report").innerHTML = html;
}

/* ------------------------------------------------------------ thumbnail */
$("#thumb-form").onsubmit = async e => {
  e.preventDefault();
  if (!PID) return alert("Select a project first.");
  const d = Object.fromEntries(new FormData(e.target));
  const r = await postJSON(`/api/projects/${PID}/thumbnail`, d);
  let html = `<div class="kv"><div><span>Thumbnail score</span><b class="score-big">${fmt(r.thumbnail_score)}</b></div></div>
    <p class="muted">${esc(r.formula)}</p>`;
  if (r.image_path) html += `<p class="muted">Manual review image path stored: ${esc(r.image_path)}</p>`;
  if (r.weaknesses?.length) html += `<ul class="tight">${r.weaknesses.map(w => `<li>Weakness: <b>${esc(w.attribute)}</b> = ${w.score}</li>`).join("")}</ul>`;
  if (r.recommendations?.length) html += `<ul class="tight">${r.recommendations.map(x => `<li>→ ${esc(x)}</li>`).join("")}</ul>`;
  $("#thumb-report").innerHTML = html;
};

/* ------------------------------------------------------------ structure */
const STRUCT_LABELS = { hook: "Hook strength", promise: "Opening promise", curiosity_setup: "Curiosity setup",
  progression: "Story progression", open_loops: "Open loops", payoff_density: "Information/payoff density",
  pacing: "Pacing", visual_variety: "Visual variety", escalation: "Escalation", ending: "Ending / payoff quality" };
$("#struct-form").innerHTML = Object.entries(STRUCT_LABELS).map(([k, l]) =>
  `<label>${l} (0–100)<input name="${k}" type="number" min="0" max="100"></label>`).join("") +
  `<div><button class="btn primary" type="submit">Score retention design (PROCESS 6)</button></div>`;

$("#struct-form").onsubmit = async e => {
  e.preventDefault();
  if (!PID) return alert("Select a project first.");
  const d = Object.fromEntries(new FormData(e.target));
  const r = await postJSON(`/api/projects/${PID}/structure`, d);
  let html = `<div class="kv"><div><span>Retention Design Score</span><b class="score-big">${fmt(r.retention_score)}</b></div></div>`;
  if (r.risk_points?.length) html += `<ul class="tight">${r.risk_points.map(x => `<li>⚠ ${esc(x)}</li>`).join("")}</ul>`;
  if (r.recommendations?.length) html += `<ul class="tight">${r.recommendations.map(x => `<li>→ ${esc(x)}</li>`).join("")}</ul>`;
  $("#struct-report").innerHTML = html;
};

/* --------------------------------------------------------- monetization */
$("#mon-form").onsubmit = async e => {
  e.preventDefault();
  if (!PID) return alert("Select a project first.");
  const d = Object.fromEntries(new FormData(e.target));
  const r = await postJSON(`/api/projects/${PID}/monetization`, d);
  let html = `<div class="kv"><div><span>Commercial-value score</span><b class="score-big">${fmt(r.commercial_value_score)}</b></div>
    <div><span>Niche RPM range (assumed)</span><b>$${r.niche_rpm_range_usd[0]} – $${r.niche_rpm_range_usd[1]}</b></div></div>
    <p class="muted">${esc(r.disclaimer)}</p>
    <table><tr><th>Scenario</th><th>RPM</th><th>AdSense est.</th><th>Affiliate est.</th><th>Sponsorship est.</th><th>Total potential</th></tr>` +
    r.scenarios.map(s => `<tr><td>${esc(s.name)}</td><td>$${s.rpm}</td><td>$${s.adsense_estimate.toLocaleString()}</td>
      <td>$${s.affiliate_estimate.toLocaleString()}</td><td>$${s.sponsorship_estimate.toLocaleString()}</td><td><b>$${s.total_potential.toLocaleString()}</b></td></tr>`).join("") +
    "</table>";
  $("#mon-report").innerHTML = html;
};

/* ---------------------------------------------------------------- engine */
$("#run-engine").onclick = async () => {
  if (!PID) return alert("Select a project first.");
  try {
    const r = await postJSON(`/api/projects/${PID}/calculate`);
    renderEngine(r.output_8_preliminary_decision);
    drawRadar(r.output_8_preliminary_decision);
  } catch (e) { alert(e.message); }
};

function renderEngine(m) {
  const badge = `<span class="badge ${m.preliminary_decision}">${m.preliminary_decision}</span>`;
  let html = `<div class="kv">
    <div><span>Opportunity score (evidence-based, not virality probability)</span><b class="score-big">${fmt(m.final_score)}</b></div>
    <div><span>Preliminary decision</span><b>${badge}</b></div>
    <div><span>Evidence quality</span><b>${esc(m.evidence_quality)}</b></div>
    <div><span>Strongest factor</span><b style="font-size:.9rem">${esc(fmt(m.strongest_factor))}</b></div>
    <div><span>Weakest factor</span><b style="font-size:.9rem">${esc(fmt(m.weakest_factor))}</b></div></div>
    <table><tr><th>Component</th><th>Value</th><th>Weight used</th></tr>` +
    Object.entries(m.components).map(([k, v]) =>
      `<tr><td>${esc(k)}</td><td>${fmt(v)}</td><td>${m.weights_used[k] != null ? (m.weights_used[k] * 100).toFixed(1) + "%" : "no data"}</td></tr>`).join("") +
    `</table>
    <p>Core weighted score: <b>${m.core_weighted_score}</b> → modifiers: ${m.modifiers.map(x => `${x.points > 0 ? "+" : ""}${x.points} (${esc(x.factor)})`).join(", ") || "none"} → final <b>${m.final_score}</b>/100</p>
    <ul class="tight">${m.modifier_notes.map(n => `<li>${esc(n)}</li>`).join("")}</ul>
    <p class="muted">${esc(m.weights_missing_note)} ${m.calibrated_weights ? "<b>Channel-calibrated weights in use.</b>" : "Generic default weights in use."}</p>`;
  $("#engine-report").innerHTML = html;
}

function drawRadar(m) {
  const keys = Object.keys(m.components);
  const vals = keys.map(k => m.components[k]);
  const defaults = [88.6, 82, 74, 90.9, 88.3, 92, 78].slice(0, keys.length);
  if (radarChart) radarChart.destroy();
  radarChart = new Chart($("#radar-chart"), {
    type: "radar",
    data: { labels: keys, datasets: [
      { label: "This project", data: vals, borderColor: "#4da3ff", backgroundColor: "rgba(77,163,255,.18)" },
    ]},
    options: { scales: { r: { min: 0, max: 100, ticks: { color: "#93a0b8" }, grid: { color: "#2a3450" }, pointLabels: { color: "#e8ecf5" } } },
      plugins: { title: { display: true, text: "Component breakdown (0–100)", color: "#e8ecf5" } } }
  });
}

/* ------------------------------------------------------------------- AI */
$("#run-ai").onclick = async () => {
  if (!PID) return alert("Select a project first.");
  $("#ai-report").innerHTML = "<p class='muted'>Sending structured project JSON to zai-org/GLM-5.3:novita via Hugging Face router… this can take up to two minutes.</p>";
  try {
    const r = await postJSON(`/api/projects/${PID}/ai_audit`);
    renderAI(r.ai_report);
  } catch (e) { $("#ai-report").innerHTML = `<div class="errorbox">AI audit failed: ${esc(e.message)}</div>`; }
};

function renderAI(a) {
  if (a.error) { $("#ai-report").innerHTML = `<div class="errorbox">${esc(a.error)}</div>`; return; }
  const list = xs => Array.isArray(xs) ? `<ul class="tight">${xs.map(x => `<li>${esc(x)}</li>`).join("")}</ul>` : "";
  const badge = a.decision ? `<span class="badge ${esc(a.decision)}">${esc(a.decision)}</span>` : "";
  $("#ai-report").innerHTML = `
    <div class="kv"><div><span>AI score (independent judgment)</span><b class="score-big">${fmt(a.ai_score)}</b></div>
      <div><span>AI decision</span><b>${badge}</b></div></div>
    <p><b>Strongest evidence:</b> ${esc(fmt(a.strongest_evidence))}</p>
    <p><b>Weakest evidence:</b> ${esc(fmt(a.weakest_evidence))}</p>
    ${a.contradictions ? "<p><b>Contradictions found:</b></p>" + list(a.contradictions) : ""}
    ${a.unsupported_assumptions ? "<p><b>Unsupported assumptions challenged:</b></p>" + list(a.unsupported_assumptions) : ""}
    ${a.math_distortion_risk ? `<p><b>Math distortion risk:</b> ${esc(a.math_distortion_risk)}</p>` : ""}
    <p><b>Best topic angle:</b> ${esc(fmt(a.best_topic_angle))}</p>
    <p><b>Best title:</b> ${esc(fmt(a.best_title))}</p>
    <p><b>Thumbnail recommendation:</b> ${esc(fmt(a.thumbnail_recommendation))}</p>
    <p><b>Hook recommendation:</b> ${esc(fmt(a.hook_recommendation))}</p>
    <p><b>Major risk:</b> ${esc(fmt(a.major_risk))}</p>
    <p><b>Single highest-priority improvement:</b> ${esc(fmt(a.highest_priority_improvement))}</p>
    ${a.required_checks?.length ? "<p><b>Required checks before publishing:</b></p>" + list(a.required_checks) : ""}
    ${a.parse_error ? `<div class="flag">Model response was not valid JSON; raw text stored in SQLite.</div>` : ""}`;
}

/* -------------------------------------------------------------- decision */
$("#run-decision").onclick = async () => {
  if (!PID) return alert("Select a project first.");
  try {
    const r = await postJSON(`/api/projects/${PID}/final_decision`, { user_notes: $("#user-notes").value });
    renderDecision(r);
  } catch (e) { alert(e.message); }
};

function renderDecision(d) {
  const badge = `<span class="badge ${esc(d.decision)}">${esc(d.decision)}</span>`;
  $("#decision-report").innerHTML = `
    <div class="kv"><div><span>Final score</span><b class="score-big">${fmt(d.final_score)}</b></div>
      <div><span>Production decision</span><b>${badge}</b></div>
      <div><span>Math vs AI</span><b>${fmt(d.math_score)} / ${fmt(d.ai_score)}</b></div>
      <div><span>Evidence quality</span><b>${esc(fmt(d.evidence_quality))}</b></div></div>
    <p class="muted">${esc(d.mix)}</p>
    ${d.disagreement ? `<div class="flag">${esc(d.disagreement)}</div>` : ""}
    <p><b>Ranked/recommended title:</b> ${esc(fmt(d.recommended_title))}</p>
    <p><b>Recommended trend term:</b> ${esc(fmt(d.recommended_trend_term))}</p>
    <p><b>Recommended thumbnail concept:</b> ${esc(fmt(d.recommended_thumbnail))}</p>
    <p><b>Recommended hook/angle:</b> ${esc(fmt(d.recommended_hook))} ${d.recommended_angle ? "— " + esc(d.recommended_angle) : ""}</p>
    <p><b>Key evidence:</b></p><ul class="tight">${(d.key_evidence || []).map(x => `<li>${esc(x)}</li>`).join("")}</ul>
    ${(d.major_risks || []).length ? "<p><b>Major risks:</b></p><ul class='tight'>" + d.major_risks.map(x => `<li>⚠ ${esc(x)}</li>`).join("") + "</ul>" : ""}
    ${(d.required_changes_before_production || []).length ? "<p><b>Exact changes required before production:</b></p><ul class='tight'>" + d.required_changes_before_production.map(x => `<li>${esc(x)}</li>`).join("") + "</ul>" : ""}
    <div class="flag">${esc(d.disclaimer)}</div>`;
}

/* ------------------------------------------------------------- learning */
$("#pub-form").onsubmit = async e => {
  e.preventDefault();
  if (!PID) return alert("Select a project first.");
  const d = Object.fromEntries(new FormData(e.target));
  const r = await postJSON(`/api/projects/${PID}/publication`, d);
  renderPubCompare(r.comparison, r.calibration);
 loadModelState();
};

function renderPubCompare(c, calibration) {
  let html = `<h3>OUTPUT 11 — prediction vs reality</h3>
    <div class="kv"><div><span>Verdict</span><b style="font-size:.95rem">${esc(c.verdict)}</b></div>`;
  const a = c.actual || {}, b = c.channel_baseline || {}, dl = c.prediction_error_deltas || {};
  html += `<div><span>Actual CTR %</span><b>${fmt(a.ctr_pct)}</b></div>
    <div><span>Baseline median CTR %</span><b>${fmt(b.median_ctr_pct)}</b></div>
    <div><span>Packaging delta vs baseline</span><b>${fmt(dl.packaging_vs_baseline_pct)}%</b></div>
    <div><span>Actual avg % viewed</span><b>${fmt(a.avg_pct_viewed)}</b></div>
    <div><span>Retention delta vs baseline</span><b>${fmt(dl.retention_vs_baseline_pct)}%</b></div>
    <div><span>Views delta vs baseline</span><b>${fmt(dl.views_vs_baseline_pct)}%</b></div></div>`;
  if (c.component_prediction_errors && Object.keys(c.component_prediction_errors).length)
    html += `<p class="muted">Per-component prediction errors: ${esc(JSON.stringify(c.component_prediction_errors))}</p>`;
  if (calibration) html += `<div class="flag">${esc(calibration)}</div>`;
  html += `<p class="muted">Prediction proxies were: ${esc(JSON.stringify(c.predicted_proxy || {}))}. With enough published outcomes the system replaces generic weights with channel-specific empirical weights (with anti-overfitting guardrails).</p>`;
  $("#pub-report").innerHTML = html;
}

async function loadModelState() {
  const m = await api("/api/model");
  let html = `<table><tr><th>Component</th><th>Current weight</th><th>Default weight</th></tr>` +
    Object.entries(m.weights).map(([k, v]) =>
      `<tr><td>${esc(k)}</td><td>${(v * 100).toFixed(1)}%</td><td>${(m.default_weights[k] * 100).toFixed(1)}%</td></tr>`).join("") + "</table>";
  if (m.baselines) html += `<p class="muted">Channel baselines (${m.baselines.videos_observed} published videos): ${esc(JSON.stringify(m.baselines))}</p>`;
  if (m.calibration_state) html += `<p class="muted">Last calibration: ${esc(JSON.stringify(m.calibration_state))}</p>`;
  $("#model-state").innerHTML = html;
}

/* -------------------------------------------------------------- settings */
async function loadSettings() {
  const s = await api("/api/settings");
  $("#token-status").textContent = s.hf_token_set ? "HF token: configured ✓" : "HF token: not set (needed for stage 9)";
}
$("#save-token").onclick = async () => {
  await postJSON("/api/settings/token", { token: $("#hf-token").value });
  $("#hf-token").value = "";
  loadSettings();
};

/* init */
loadProjects(); loadSettings();
showStage("project");
