"""YouTube Video Opportunity Analyzer — local Flask backend.

Pipeline: raw input -> data collection -> mathematical calculation -> AI audit
(GLM-5.3 via Hugging Face OpenAI-compatible router) -> final decision ->
published result -> feedback -> model calibration. All data persists in SQLite.
"""
import io
import json
from datetime import date

from flask import Flask, jsonify, request, send_file

import ai_client
import db
import engine

app = Flask(__name__, static_folder="static")
db.init_db()


def err(msg, code=400):
    return jsonify({"error": msg}), code


# ------------------------------------------------------------------ views ---

@app.get("/")
def index():
    return app.send_static_file("index.html")


# ---------------------------------------------------- INPUT/OUTPUT 1: project

PROJECT_FIELDS = ["name", "topic", "audience", "market", "language", "category",
                  "format", "notes", "differentiation_rating"]


@app.post("/api/projects")
def create_project():
    d = request.get_json(force=True, silent=True) or {}
    if not (d.get("name") or "").strip():
        return err("Project name is required.")
    data = {k: d.get(k) for k in PROJECT_FIELDS}
    pid = db.insert("projects", data)
    return jsonify(get_project(pid))


@app.put("/api/projects/<int:pid>")
def update_project(pid):
    d = request.get_json(force=True, silent=True) or {}
    data = {k: d[k] for k in PROJECT_FIELDS if k in d}
    data["updated_at"] = str(date.today())
    if data:
        db.update("projects", pid, data)
    return jsonify(get_project(pid))


@app.delete("/api/projects/<int:pid>")
def delete_project(pid):
    db.delete("projects", pid)
    return jsonify({"ok": True})


def get_project(pid):
    p = db.query_one("SELECT * FROM projects WHERE id=?", (pid,))
    if not p:
        return None
    out = dict(p)
    out["terms"] = _terms_out(pid)
    out["competitors"] = db.query("SELECT * FROM competitors WHERE project_id=? ORDER BY id", (pid,))
    out["titles"] = [dict(t, metrics=json.loads(t["metrics_json"] or "{}"))
                    for t in db.query("SELECT * FROM titles WHERE project_id=? ORDER BY id", (pid,))]
    out["thumbnail"] = db.query_one("SELECT * FROM thumbnails WHERE project_id=? ORDER BY id DESC LIMIT 1", pid)
    out["structure"] = db.query_one("SELECT * FROM structures WHERE project_id=? ORDER BY id DESC LIMIT 1", pid)
    out["monetization"] = db.query_one("SELECT * FROM monetizations WHERE project_id=? ORDER BY id DESC LIMIT 1", pid)
    calc = db.query_one("SELECT * FROM calculations WHERE project_id=?", (pid,))
    out["calculation"] = json.loads(calc["payload_json"]) if calc else None
    ai = db.query_one("SELECT * FROM ai_reports WHERE project_id=? ORDER BY id DESC LIMIT 1", pid)
    out["ai_report"] = json.loads(ai["response_json"]) if ai and ai["response_json"] else (
        {"error": ai["error"]} if ai and ai["error"] else None)
    dec = db.query_one("SELECT * FROM decisions WHERE project_id=? ORDER BY id DESC LIMIT 1", pid)
    out["decision"] = json.loads(dec["payload_json"]) if dec else None
    pub = db.query_one("SELECT * FROM publications WHERE project_id=? ORDER BY id DESC LIMIT 1", pid)
    out["publication"] = dict(pub, comparison=json.loads(pub["comparison_json"] or "{}")) if pub else None
    return out


@app.get("/api/projects")
def list_projects():
    rows = db.query("""SELECT p.*,
        (SELECT overall_score FROM calculations c WHERE c.project_id=p.id) AS score,
        (SELECT preliminary_decision FROM calculations c WHERE c.project_id=p.id) AS prelim,
        (SELECT decision FROM decisions d WHERE d.project_id=p.id ORDER BY id DESC LIMIT 1) AS final_decision
        FROM projects p ORDER BY p.id DESC""")
    return jsonify(rows)


@app.get("/api/projects/<int:pid>")
def read_project(pid):
    p = get_project(pid)
    if not p:
        return err("Project not found.", 404)
    return jsonify(p)


# ------------------------------------------------------ INPUT/OUTPUT 2: trend

def _terms_out(pid):
    out = []
    for r in db.query("SELECT * FROM trend_terms WHERE project_id=? ORDER BY id", (pid,)):
        d = dict(r)
        d["series"] = json.loads(d.pop("series_json") or "[]")
        d["metrics"] = json.loads(d.pop("metrics_json") or "null")
        out.append(d)
    return out


@app.post("/api/projects/<int:pid>/terms")
def add_term(pid):
    d = request.get_json(force=True, silent=True) or {}
    term = (d.get("term") or "").strip()
    if not term:
        return err("Term text is required.")
    existing = db.query("SELECT term FROM trend_terms WHERE project_id=?", (pid,))
    if len(existing) >= 5:
        return err("Maximum of five trend terms per project.")
    tid = db.insert("trend_terms", {"project_id": pid, "term": term,
                                    "geo": d.get("geo", ""), "timeframe": d.get("timeframe", "now 12-m")})
    return jsonify({"id": tid, "term": term})


@app.delete("/api/terms/<int:tid>")
def delete_term(tid):
    db.delete("trend_terms", tid)
    return jsonify({"ok": True})


@app.get("/api/projects/<int:pid>/trends_url")
def trends_url(pid):
    terms = [r["term"] for r in db.query("SELECT term FROM trend_terms WHERE project_id=? ORDER BY id", (pid,))]
    geo = ""
    row = db.query_one("SELECT geo FROM trend_terms WHERE project_id=? AND geo!='' LIMIT 1", (pid,))
    if row:
        geo = row["geo"]
    if not terms:
        return err("Add at least one trend term first.")
    return jsonify(engine.trends_embed_url(terms, geo))


@app.post("/api/projects/<int:pid>/trends/import")
def import_trends(pid):
    """Accept a Google Trends CSV export (form field 'csv' or JSON {csv: ...})."""
    csv_text = None
    if request.files.get("csv"):
        csv_text = request.files["csv"].read().decode("utf-8-sig", errors="replace")
    elif request.is_json:
        csv_text = (request.get_json(force=True) or {}).get("csv")
    if not csv_text:
        return err("Provide the exported Google Trends CSV (field 'csv').")
    parsed = engine.parse_trends_csv(csv_text)
    if not parsed:
        return err("Could not parse any time series from that CSV.")
    stored_terms = {r["term"].lower(): r["id"] for r in
                    db.query("SELECT id, term FROM trend_terms WHERE project_id=?", (pid,))}
    matched, unmatched = [], []
    for raw_term, series in parsed.items():
        # Google labels series like 'AI electricity: (Worldwide)' -> match on keyword part
        key = raw_term.split(":")[0].strip().lower()
        tid = next((stored_terms[t] for t in stored_terms if t == key or key in t or t in key), None)
        payload = {"series_json": json.dumps(series)}
        m = engine.compute_term_metrics([s["value"] for s in series])
        tm = engine.term_momentum(m) if m else None
        payload["metrics_json"] = json.dumps({"measured": m, "momentum_detail": tm})
        momentum = tm["momentum"] if tm else None
        if tid:
            payload["momentum"] = momentum
            db.update("trend_terms", tid, payload)
            matched.append(raw_term)
        else:
            new_id = db.insert("trend_terms", {"project_id": pid, "term": key[:80],
                                               "momentum": momentum, **payload})
            matched.append(raw_term + " (auto-added)")
    report = engine.trends_report([
        {"term": r["term"], "series": [s["value"] for s in json.loads(r["series_json"] or "[]")]}
        for r in _terms_out(pid)])
    return jsonify({"matched": matched, "report": report})


@app.get("/api/projects/<int:pid>/trends_report")
def trends_report(pid):
    payload = [{"term": r["term"], "series": [s["value"] for s in r["series"]]} for r in _terms_out(pid)]
    return jsonify(engine.trends_report(payload))


# --------------------------------------------------- INPUT/OUTPUT 3: rivals

COMP_FIELDS = ["title", "channel", "subscribers", "views", "upload_date",
               "likes", "comments", "channel_typical_views"]


@app.post("/api/projects/<int:pid>/competitors")
def add_competitor(pid):
    d = request.get_json(force=True, silent=True) or {}
    data = {k: d.get(k) for k in COMP_FIELDS}
    data["project_id"] = pid
    cid = db.insert("competitors", data)
    return jsonify({"id": cid})


@app.delete("/api/competitors/<int:cid>")
def delete_competitor(cid):
    db.delete("competitors", cid)
    return jsonify({"ok": True})


@app.get("/api/projects/<int:pid>/competition_report")
def competition_report(pid):
    recs = db.query("SELECT * FROM competitors WHERE project_id=?", (pid,))
    return jsonify(engine.competition_report(recs))


# ----------------------------------------------------- INPUT/OUTPUT 4: titles

@app.post("/api/projects/<int:pid>/titles")
def add_titles(pid):
    d = request.get_json(force=True, silent=True) or {}
    items = d.get("titles") or []
    if isinstance(items, str):
        items = [x for x in items.splitlines() if x.strip()]
    ratings = d.get("ratings") or {}
    momentum_by_term = {r["term"].lower(): r["momentum"] for r in _terms_out(pid) if r["momentum"]}
    links = d.get("links") or {}  # {text: term}
    analyses = []
    for t in items:
        text = (t if isinstance(t, str) else t.get("text", "")).strip()
        if not text:
            continue
        linked = (links.get(text) or (t.get("linked_term") if isinstance(t, dict) else "") or "").strip()
        mom = momentum_by_term.get(linked.lower())
        if mom is None and linked:
            mom = next((v for k, v in momentum_by_term.items() if linked.lower() in k or k in linked.lower()), None)
        a = engine.analyze_title(text, ratings.get(text) if isinstance(ratings, dict) else None, mom)
        a["linked_term"] = linked
        tid = db.insert("titles", {"project_id": pid, "text": text, "linked_term": linked,
                                   "ratings_json": json.dumps(ratings.get(text) or {}),
                                   "metrics_json": json.dumps(a)})
        a["id"] = tid
        analyses.append(a)
    return jsonify(engine.titles_report(analyses))


@app.delete("/api/titles/<int:tid>")
def delete_title(tid):
    db.delete("titles", tid)
    return jsonify({"ok": True})


@app.get("/api/projects/<int:pid>/titles_report")
def titles_report(pid):
    analyses = [t["metrics"] for t in get_project(pid)["titles"]]
    return jsonify(engine.titles_report(analyses))


# ------------------------------------------------- INPUT/OUTPUT 5: thumbnail

THUMB_FIELDS = ["subject", "thumb_text", "emotion", "contrast", "simplicity",
                "mobile_readability", "text_load", "complementarity", "curiosity",
                "hierarchy", "specificity", "image_path"]


@app.post("/api/projects/<int:pid>/thumbnail")
def save_thumbnail(pid):
    d = request.get_json(force=True, silent=True) or {}
    rep = engine.thumbnail_report(d)
    data = {k: d.get(k) for k in THUMB_FIELDS}
    data["project_id"] = pid
    data["score"] = rep["thumbnail_score"]
    data["weaknesses_json"] = json.dumps(rep["weaknesses"])
    data["recommendations_json"] = json.dumps(rep["recommendations"])
    old = db.query("SELECT id FROM thumbnails WHERE project_id=?", (pid,))
    for r in old:
        db.delete("thumbnails", r["id"])
    tid = db.insert("thumbnails", data)
    return jsonify(dict(rep, id=tid))


# -------------------------------------------------- INPUT/OUTPUT 6: structure

STRUCT_FIELDS = engine.STRUCT_ATTRS


@app.post("/api/projects/<int:pid>/structure")
def save_structure(pid):
    d = request.get_json(force=True, silent=True) or {}
    rep = engine.retention_report(d)
    data = {k: d.get(k) for k in STRUCT_FIELDS}
    data["project_id"] = pid
    data["score"] = rep["retention_score"]
    data["risks_json"] = json.dumps(rep["risk_points"])
    data["recommendations_json"] = json.dumps(rep["recommendations"])
    for r in db.query("SELECT id FROM structures WHERE project_id=?", (pid,)):
        db.delete("structures", r["id"])
    sid = db.insert("structures", data)
    return jsonify(dict(rep, id=sid))


# ------------------------------------------------ INPUT/OUTPUT 7: monetization

MON_FIELDS = ["niche", "rpm", "monthly_views", "affiliate_ctr", "affiliate_conv",
              "commission", "sponsorship", "other_revenue"]


@app.post("/api/projects/<int:pid>/monetization")
def save_monetization(pid):
    d = request.get_json(force=True, silent=True) or {}
    rep = engine.monetization_report(d)
    data = {k: d.get(k) for k in MON_FIELDS}
    data["project_id"] = pid
    data["scenarios_json"] = json.dumps(rep["scenarios"])
    data["score"] = rep["commercial_value_score"]
    for r in db.query("SELECT id FROM monetizations WHERE project_id=?", (pid,)):
        db.delete("monetizations", r["id"])
    mid = db.insert("monetizations", data)
    return jsonify(dict(rep, id=mid))


# ------------------------------------------- INPUT/OUTPUT 8: math engine

def build_snapshot(pid):
    """INPUT 8 — assemble ALL project data into one structured record."""
    p = get_project(pid)
    if not p:
        return None
    terms_payload = [{"term": r["term"], "series": [s["value"] for s in r["series"]]}
                     for r in p["terms"]]
    trend = engine.trends_report(terms_payload)
    comp = engine.competition_report(
        db.query("SELECT * FROM competitors WHERE project_id=?", (pid,)))
    t_report = engine.titles_report([t["metrics"] for t in p["titles"]])
    thumb_row = p["thumbnail"]
    thumb = None
    if thumb_row:
        thumb = {"thumbnail_score": thumb_row["score"],
                 "weaknesses": json.loads(thumb_row["weaknesses_json"] or "[]"),
                 "recommendations": json.loads(thumb_row["recommendations_json"] or "[]")}
    struct_row = p["structure"]
    retention = None
    if struct_row:
        retention = {"retention_score": struct_row["score"],
                     "risk_points": json.loads(struct_row["risks_json"] or "[]"),
                     "recommendations": json.loads(struct_row["recommendations_json"] or "[]")}
    mon_row = p["monetization"]
    monetization = None
    if mon_row:
        rep = engine.monetization_report({k: mon_row[k] for k in MON_FIELDS})
        monetization = rep
    best_combined = t_report["top_score"]
    pack_title = None
    if best_combined is not None:
        pack_title = best_combined
    else:
        scores = [t["metrics"]["title_score"] for t in p["titles"]]
        pack_title = max(scores) if scores else None
    visual_potential = thumb["thumbnail_score"] if thumb else None
    completeness = sum(x is not None for x in [
        trend["overall_momentum"], comp["competitive_opportunity_score"], pack_title,
        thumb and thumb["thumbnail_score"], retention and retention["retention_score"],
        monetization and monetization["commercial_value_score"]]) / 6.0
    math_res = engine.preliminary_engine(
        p, trend, comp, pack_title, thumb["thumbnail_score"] if thumb else None,
        retention or {}, visual_potential, monetization or {}, completeness=completeness)
    snapshot = {
        "pipeline": "raw input -> data collection -> mathematical calculation -> AI audit -> final decision -> published result -> feedback -> calibration",
        "score_disclaimer": ("This is an evidence-based opportunity score. It is NOT a probability of "
                             "virality and does not reproduce YouTube's private recommendation algorithm."),
        "data_separation": {
            "measured": "trend statistics, competitor velocities/ratios, title structural features",
            "heuristics": "user-rated rubric scores (clarity, hook, contrast...) and transparent formulas",
            "ai_judgment": "produced only by the GLM-5.3 audit stage, never mixed into deterministic math",
        },
        "project": {k: p[k] for k in PROJECT_FIELDS},
        "trend": trend,
        "competition": {k: comp[k] for k in comp if k != "ranked"},
        "competition_ranked": comp["ranked"],
        "titles": t_report,
        "packaging": {"best_title_score": pack_title, "thumbnail": thumb},
        "retention": retention,
        "monetization": monetization,
        "mathematical_engine": math_res,
    }
    return snapshot


@app.post("/api/projects/<int:pid>/calculate")
def calculate(pid):
    snap = build_snapshot(pid)
    if not snap:
        return err("Project not found.", 404)
    m = snap["mathematical_engine"]
    existing = db.query_one("SELECT id FROM calculations WHERE project_id=?", (pid,))
    payload = dict(snap, calculated_on=str(date.today()))
    if existing:
        db.update("calculations", existing["id"], {
            "payload_json": json.dumps(payload), "overall_score": m["final_score"],
            "preliminary_decision": m["preliminary_decision"],
            "evidence_quality": m["evidence_quality"],
            "weights_json": json.dumps(m["weights_used"]),
            "calculated_at": str(date.today())})
    else:
        db.insert("calculations", {"project_id": pid, "payload_json": json.dumps(payload),
                                   "overall_score": m["final_score"],
                                   "preliminary_decision": m["preliminary_decision"],
                                   "evidence_quality": m["evidence_quality"],
                                   "weights_json": json.dumps(m["weights_used"])})
    return jsonify({"output_8_preliminary_decision": m, "snapshot": snap})


@app.get("/api/projects/<int:pid>/snapshot")
def snapshot(pid):
    snap = build_snapshot(pid)
    if not snap:
        return err("Project not found.", 404)
    return jsonify(snap)


# -------------------------------------------------- INPUT/OUTPUT 9: AI audit

@app.post("/api/projects/<int:pid>/ai_audit")
def ai_audit(pid):
    snap = build_snapshot(pid)
    if not snap:
        return err("Project not found.", 404)
    body = request.get_json(silent=True) or {}
    token = body.get("token")  # optional one-off override; not stored unless settings say so
    ai_res, error = ai_client.audit(snap, token=token)
    rid = db.insert("ai_reports", {"project_id": pid, "model": ai_client.MODEL,
                                   "request_json": json.dumps(snap),
                                   "response_json": json.dumps(ai_res) if ai_res else None,
                                   "error": error})
    if error:
        return jsonify({"error": error, "report_id": rid}), 502
    return jsonify({"ai_report": ai_res, "report_id": rid})


# ---------------------------------------------- INPUT/OUTPUT 10: synthesis

@app.post("/api/projects/<int:pid>/final_decision")
def final_decision(pid):
    snap = build_snapshot(pid)
    if not snap:
        return err("Project not found.", 404)
    m = snap["mathematical_engine"]
    ai_row = db.query_one("SELECT * FROM ai_reports WHERE project_id=? ORDER BY id DESC LIMIT 1", pid)
    ai_res = None
    if ai_row and ai_row["response_json"]:
        try:
            ai_res = json.loads(ai_row["response_json"])
        except json.JSONDecodeError:
            ai_res = None
    body = request.get_json(silent=True) or {}
    notes = body.get("user_notes", "")
    fd = engine.final_decision(m, ai_res, notes)
    t_report = snap["titles"]
    thumb_row = db.query_one(
        "SELECT subject, thumb_text FROM thumbnails WHERE project_id=? ORDER BY id DESC LIMIT 1", pid)
    thumb_desc = None
    if thumb_row:
        thumb_desc = f"{thumb_row['subject']}" + (f" + text '{thumb_row['thumb_text']}'" if thumb_row["thumb_text"] else "")
    neg_mods = [n for n, mod in zip(m["modifier_notes"], m["modifiers"]) if mod["points"] < 0]
    out = dict(fd,
               recommended_title=(ai_res or {}).get("best_title") or t_report["best_title"],
               recommended_trend_term=snap["trend"]["strongest_term"],
               recommended_thumbnail=thumb_desc,
               recommended_hook=(ai_res or {}).get("hook_recommendation"),
               recommended_angle=(ai_res or {}).get("best_topic_angle"),
               key_evidence=[f"Trend momentum {m['components']['trend_momentum']}",
                             f"Competitive opportunity {m['components']['competitive_opportunity']}",
                             f"Packaging {m['components']['packaging_strength']}",
                             f"Evidence quality: {fd['evidence_quality']}"],
               major_risks=((ai_res or {}).get("contradictions") or []) + neg_mods,
               )
    did = db.insert("decisions", {"project_id": pid, "final_score": fd["final_score"],
                                  "decision": fd["decision"], "payload_json": json.dumps(out)})
    return jsonify(dict(out, id=did))


# --------------------------------------- INPUT/OUTPUT 11: learning loop

PUB_FIELDS = ["days_since_upload", "impressions", "ctr", "views", "avg_view_duration_sec",
              "video_length_sec", "avg_pct_viewed", "retention_milestones",
              "returning_viewers_pct", "traffic_sources", "subscribers_gained", "revenue"]


@app.post("/api/projects/<int:pid>/publication")
def save_publication(pid):
    d = request.get_json(force=True, silent=True) or {}
    data = {k: d.get(k) for k in PUB_FIELDS}
    data["project_id"] = pid
    calc = db.query_one("SELECT payload_json FROM calculations WHERE project_id=?", (pid,))
    snap = json.loads(calc["payload_json"]) if calc else {}
    data["comparison_json"] = None
    pub_id = db.insert("publications", data)
    comparison = engine.publication_comparison(dict(data, project_id=pid), snap)
    db.update("publications", pub_id, {"comparison_json": json.dumps(comparison)})
    weights_now, status = engine.recalibrate()
    return jsonify({"id": pub_id, "comparison": comparison, "calibration": status,
                    "weights": weights_now})


@app.get("/api/model")
def model_state():
    return jsonify({
        "weights": engine.db_weights(),
        "default_weights": engine.DEFAULT_WEIGHTS,
        "calibration_state": db.setting_get("calibration_state"),
        "baselines": engine.channel_baselines(),
        "outcomes": db.query("""SELECT p.name, pu.comparison_json FROM publications pu
                                JOIN projects p ON p.id=pu.project_id ORDER BY pu.id DESC"""),
    })


# ------------------------------------------------------------- exports ----

@app.get("/api/projects/<int:pid>/export.json")
def export_json(pid):
    snap = build_snapshot(pid)
    if not snap:
        return err("Project not found.", 404)
    full = {"snapshot": snap, "project": get_project(pid)}
    buf = io.BytesIO(json.dumps(full, indent=2, ensure_ascii=False).encode("utf-8"))
    return send_file(buf, mimetype="application/json", as_attachment=True,
                     download_name=f"project_{pid}_opportunity.json")


@app.get("/api/projects/<int:pid>/brief.txt")
def export_brief(pid):
    snap = build_snapshot(pid)
    if not snap:
        return err("Project not found.", 404)
    text = render_brief(snap)
    buf = io.BytesIO(text.encode("utf-8"))
    return send_file(buf, mimetype="text/plain", as_attachment=True,
                     download_name=f"project_{pid}_production_brief.txt")


def render_brief(snap):
    p = snap["project"]
    m = snap["mathematical_engine"]
    t = snap["trend"]
    c = snap["competition"]
    lines = [
        "=" * 72,
        "PRODUCTION BRIEF — YouTube Video Opportunity Analyzer",
        "=" * 72,
        f"Project        : {p.get('name')}",
        f"Topic          : {p.get('topic')}",
        f"Audience       : {p.get('audience')}   Market: {p.get('market')}   Language: {p.get('language')}",
        f"Category       : {p.get('category')}   Format: {p.get('format')}",
        "",
        "NOTE: The score below is an EVIDENCE-BASED OPPORTUNITY SCORE. It is not a",
        "probability of going viral and does not model YouTube's private algorithm.",
        "-" * 72,
        "OUTPUT 2 — TREND REPORT",
    ]
    for term in t["terms"]:
        mm = term.get("metrics")
        if mm:
            lines.append(f"  {term['term']}: momentum {term.get('momentum')} | latest {mm['latest_interest']} "
                         f"| peak {mm['peak_interest']} | recent growth {mm['recent_growth_pct']}% "
                         f"| slope {mm['slope_simple']}/step | volatility {mm['volatility']}")
    lines += [f"  Direction: {t['direction']} | Strongest: {t['strongest_term']} | Weakest: {t['weakest_term']}",
              "", "OUTPUT 3 — COMPETITION REPORT"]
    for e in snap["competition_ranked"]:
        em = e["metrics"]
        lines.append(f"  {e.get('title') or e.get('channel')}: {em['views_per_day']} views/day, "
                     f"idx {em.get('velocity_index')}, outlier {em.get('outlier_ratio')}x, "
                     f"engagement {em.get('engagement_rate_pct')}%")
    lines += [f"  Median velocity: {c['median_velocity']} views/day | Saturation: {c['saturation_level']}",
              f"  Recent outliers: {', '.join(c['recent_high_velocity_outliers']) or 'none'} | "
              f"Opportunity: {c['competitive_opportunity_score']}", ""]
    lines.append("OUTPUT 4 — TITLE REPORT")
    for a in snap["titles"]["ranked"]:
        lines.append(f"  {a.get('combined_score') or a['title_score']:>6}  {a['text']}")
    lines += [f"  Recommended: {snap['titles']['best_title']}", ""]
    th = snap["packaging"].get("thumbnail")
    if th:
        lines += ["OUTPUT 5 — THUMBNAIL REPORT", f"  Thumbnail Score: {th['thumbnail_score']}/100"]
        for w in th["weaknesses"]:
            lines.append(f"  Weakness: {w['attribute']} = {w['score']}")
        lines += [""]
    rt = snap.get("retention")
    if rt:
        lines += ["OUTPUT 6 — RETENTION REPORT", f"  Retention Design Score: {rt['retention_score']}/100"]
        for r in rt["risk_points"]:
            lines.append(f"  Risk: {r}")
        lines += [""]
    mo = snap.get("monetization")
    if mo:
        lines += ["OUTPUT 7 — MONETIZATION SCENARIOS (assumptions, not promises)"]
        for s in mo["scenarios"]:
            lines.append(f"  {s['name']}: AdSense ${s['adsense_estimate']} + affiliate ${s['affiliate_estimate']} "
                         f"+ sponsorship ${s['sponsorship_estimate']} = ${s['total_potential']}")
        lines += [""]
    lines += ["OUTPUT 8 — PRELIMINARY DECISION (mathematical engine)",
              f"  Overall opportunity score: {m['final_score']}/100  -> {m['preliminary_decision']}",
              f"  Evidence quality: {m['evidence_quality']}",
              f"  Components: " + ", ".join(f"{k}={v}" for k, v in m["components"].items() if v is not None),
              f"  Modifiers: " + "; ".join(m["modifier_notes"]),
              f"  Strongest factor: {m['strongest_factor']} | Weakest: {m['weakest_factor']}", ""]
    ai = db.query_one("SELECT response_json FROM ai_reports WHERE project_id=? ORDER BY id DESC LIMIT 1",
                      (p.get("id") or 0,))
    if ai and ai["response_json"]:
        try:
            a = json.loads(ai["response_json"])
            lines += ["OUTPUT 9 — AI AUDIT (GLM-5.3, independent judgment)",
                      f"  AI score: {a.get('ai_score')} | Decision: {a.get('decision')}",
                      f"  Strongest evidence: {a.get('strongest_evidence')}",
                      f"  Weakest evidence: {a.get('weakest_evidence')}",
                      f"  Best title: {a.get('best_title')}",
                      f"  Hook: {a.get('hook_recommendation')}",
                      f"  Highest-priority improvement: {a.get('highest_priority_improvement')}"]
            for cc in a.get("contradictions") or []:
                lines.append(f"  Contradiction: {cc}")
            lines.append("")
        except json.JSONDecodeError:
            pass
    dec = db.query_one("SELECT payload_json FROM decisions WHERE project_id=? ORDER BY id DESC LIMIT 1",
                       (p.get("id") or 0,))
    if dec:
        d = json.loads(dec["payload_json"])
        lines += ["OUTPUT 10 — PRODUCTION DECISION",
                  f"  Final score: {d['final_score']}/100  -> {d['decision']}",
                  f"  Mix: {d['mix']}",
                  f"  Recommended title: {d.get('recommended_title')}",
                  f"  Recommended trend term: {d.get('recommended_trend_term')}",
                  f"  Recommended hook: {d.get('recommended_hook')}",
                  f"  Required changes before production: {'; '.join(d['required_changes_before_production']) or 'none'}"]
        if d.get("disagreement"):
            lines.append(f"  !! {d['disagreement']}")
        lines.append("")
    pub = db.query_one("SELECT comparison_json FROM publications WHERE project_id=? ORDER BY id DESC LIMIT 1",
                       (p.get("id") or 0,))
    if pub and pub["comparison_json"]:
        cmpd = json.loads(pub["comparison_json"])
        lines += ["OUTPUT 11 — PUBLISHED RESULT VS PREDICTION",
                  f"  Actual: {cmpd['actual']}",
                  f"  Baseline deltas: {cmpd['prediction_error_deltas']}",
                  f"  Verdict: {cmpd['verdict']}"]
    lines += ["", "Generated locally. All inputs, calculations and outcomes are stored in SQLite."]
    return "\n".join(lines)


# ---------------------------------------------------------------- settings --

@app.get("/api/settings")
def get_settings():
    tok = ai_client.get_token()
    return jsonify({"hf_token_set": bool(tok), "model": ai_client.MODEL,
                    "weights": engine.db_weights(),
                    "default_weights": engine.DEFAULT_WEIGHTS})


@app.post("/api/settings/token")
def set_token():
    d = request.get_json(silent=True) or {}
    tok = (d.get("token") or "").strip()
    if tok:
        db.setting_set("hf_token", tok)
    else:
        conn = db.get_conn(); conn.execute("DELETE FROM settings WHERE key='hf_token'"); conn.commit(); conn.close()
    return jsonify({"hf_token_set": bool(tok)})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
