"""Deterministic calculation engine for the YouTube Video Opportunity Analyzer.

Every function here is pure math over user-supplied data. The system separates:
  * MEASURED values (directly computed from entered/imported numbers),
  * HEURISTIC scores (transparent rubric ratings supplied by the user),
  * AI JUDGMENT (produced separately by the GLM-5.3 audit, never mixed in here).

The final score is an EVIDENCE-BASED OPPORTUNITY SCORE. It is explicitly NOT a
probability that a video will go viral and it does not model YouTube's private
recommendation algorithm.
"""
import csv
import io
import json
import math
import re
from datetime import date, datetime
from urllib.parse import quote

# ---------------------------------------------------------------- defaults ---

DEFAULT_WEIGHTS = {
    "trend_momentum": 0.25,
    "audience_demand": 0.20,
    "competitive_opportunity": 0.15,
    "packaging_strength": 0.15,
    "retention_design": 0.10,
    "visual_potential": 0.10,
    "monetization_potential": 0.05,
}

TITLE_FORMULA = {
    "clarity": 0.20, "curiosity": 0.20, "stakes": 0.15,
    "novelty": 0.15, "relevance": 0.15, "readability": 0.15,
}

COMBINED_TITLE_MIX = {"title": 0.70, "momentum": 0.30}
PACKAGING_MIX = {"title": 0.60, "thumbnail": 0.40}
FINAL_MIX = {"math": 0.60, "ai": 0.40}

TREND_MOMENTUM_MIX = {"growth": 0.35, "latest": 0.25, "slope": 0.20, "freshness": 0.20}

DECISION_BANDS = [(80, "GO"), (65, "PROMISING"), (50, "REWORK")]  # below 50 -> STOP


def clamp(v, lo=0.0, hi=100.0):
    return max(lo, min(hi, v))


def _num(v, default=None):
    try:
        if v is None or v == "":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _median(vals):
    s = sorted(vals)
    n = len(s)
    if n == 0:
        return None
    m = n // 2
    return s[m] if n % 2 else (s[m - 1] + s[m]) / 2.0


def _mean(vals):
    return sum(vals) / len(vals) if vals else None


# ------------------------------------------------------------ INPUT 2 ------
# Trend parsing + momentum metrics

def parse_trends_csv(text):
    """Parse a Google Trends interest-over-time CSV export.

    Handles both the multi-term format (header row of quoted term names under a
    'Interest over time' banner) and simple date,value formats. Returns
    {term: [{date, value}, ...]} with values coerced to floats.
    """
    text = (text or "").strip()
    if not text:
        return {}
    rows = list(csv.reader(io.StringIO(text)))
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        return {}

    def looks_like_date(cell):
        cell = cell.strip().strip('"')
        return bool(re.match(r"^\d{1,2}/\d{1,2}/\d{2,4}$|^\d{4}-\d{2}(-\d{2})?$", cell))

    # Locate header row: first row whose second+ cells are non-numeric labels
    header_idx = None
    terms = None
    for i, r in enumerate(rows[:5]):
        if len(r) < 2:
            continue
        rest = [c.strip().strip('"') for c in r[1:]]
        numeric = [c for c in rest if re.match(r"^[\d.]+$", c)]
        if len(numeric) < len(rest) and any(rest):
            # candidate header; but skip the literal banner row
            joined = " ".join(rest).lower()
            if "interest over time" in joined and all(not c or "interest" in c.lower() for c in rest):
                continue
            header_idx = i
            terms = [c if c else f"Term {j}" for j, c in enumerate(rest)]
            break
    if header_idx is None:
        # fallback: single series, columns date,value
        data_rows = rows[1:] if rows and not looks_like_date(rows[0][0]) else rows
        series = []
        for r in data_rows:
            if len(r) >= 2 and looks_like_date(r[0]):
                v = _num(r[1])
                if v is not None:
                    series.append({"date": r[0].strip(), "value": v})
        return {"Term 1": series} if series else {}

    out = {t: [] for t in terms}
    for r in rows[header_idx + 1:]:
        if not r or not looks_like_date(r[0]):
            continue
        d = r[0].strip()
        for j, t in enumerate(terms):
            cell = r[j + 1].strip() if j + 1 < len(r) else ""
            v = _num(cell.replace("<", ""))
            if v is not None:
                out[t].append({"date": d, "value": v})
    return {t: s for t, s in out.items() if s}


def compute_term_metrics(values):
    """MEASURED trend statistics for one normalized-interest series."""
    vals = [v for v in values if v is not None]
    n = len(vals)
    if n < 2:
        return None

    def window(k, from_end=True):
        seg = vals[-k:] if from_end else vals[:k]
        return _mean(seg)

    half = max(1, n // 2)
    previous_avg = _mean(vals[:half])
    recent_avg = _mean(vals[n - half:])
    growth_rate = ((recent_avg - previous_avg) / previous_avg * 100.0) if previous_avg else None

    q = max(1, min(3, n // 4))
    prev3 = _mean(vals[max(0, n - 2 * q):n - q])
    recent3 = _mean(vals[n - q:])
    recent_growth_pct = ((recent3 - prev3) / prev3 * 100.0) if prev3 else None

    mean_v = _mean(vals)
    var_v = sum((x - mean_v) ** 2 for x in vals) / n
    volatility = math.sqrt(var_v)

    # least-squares slope (interest points per observation step)
    xs = list(range(n))
    xm, ym = _mean(xs), mean_v
    denom = sum((x - xm) ** 2 for x in xs) or 1.0
    slope = sum((xs[i] - xm) * (vals[i] - ym) for i in range(n)) / denom
    slope_simple = (vals[-1] - vals[0]) / (n - 1)
    norm_slope = slope / mean_v * 100.0 if mean_v else 0.0

    return {
        "observations": n,
        "average_interest": round(mean_v, 1),
        "latest_interest": vals[-1],
        "peak_interest": max(vals),
        "recent_period_average": round(recent_avg, 1),
        "previous_period_average": round(previous_avg, 1),
        "growth_rate_pct": round(growth_rate, 1) if growth_rate is not None else None,
        "recent_3p_avg": round(recent3, 1),
        "previous_3p_avg": round(prev3, 1),
        "recent_growth_pct": round(recent_growth_pct, 1) if recent_growth_pct is not None else None,
        "volatility": round(volatility, 1),
        "slope_per_step": round(slope, 2),
        "slope_simple": round(slope_simple, 2),
        "normalized_slope": round(norm_slope, 1),
    }


def term_momentum(metrics, freshness=None, slope_scale=15.0):
    """HEURISTIC-MIXED 0-100 momentum score built ONLY from measured stats.

    Mirrors the worked example: 0.35*growth + 0.25*latest + 0.20*slope +
    0.20*freshness. Sub-scores are mapped transparently:
      growth_score   = clamp(50 + recent_growth_pct * 1.2)   (flat market -> 50)
      latest_score   = latest normalized interest (already 0-100)
      slope_score    = clamp(50 + slope_simple * slope_scale) (points/step -> 0-100)
      freshness      = user-rated if given, else latest/peak ratio
    """
    if not metrics:
        return None
    growth_score = clamp(50.0 + metrics["recent_growth_pct"] * 1.2) if metrics.get("recent_growth_pct") is not None else 50.0
    latest_score = clamp(float(metrics["latest_interest"]))
    slope_score = clamp(50.0 + float(metrics["slope_simple"]) * slope_scale)
    if freshness is None:
        peak = metrics["peak_interest"] or 1
        freshness = clamp(float(metrics["latest_interest"]) / float(peak) * 100.0)
    momentum = (TREND_MOMENTUM_MIX["growth"] * growth_score
                + TREND_MOMENTUM_MIX["latest"] * latest_score
                + TREND_MOMENTUM_MIX["slope"] * slope_score
                + TREND_MOMENTUM_MIX["freshness"] * float(freshness))
    return {
        "momentum": round(clamp(momentum), 1),
        "components": {
            "growth_score": round(growth_score, 1),
            "latest_score": round(latest_score, 1),
            "slope_score": round(slope_score, 1),
            "freshness": round(float(freshness), 1),
        },
        "formula": "0.35*growth + 0.25*latest + 0.20*slope + 0.20*freshness",
    }


def trends_report(terms_payload):
    """OUTPUT 2 — aggregate report across up to five terms.

    terms_payload: list of {term, series:[values], freshness?}
    """
    per_term = []
    for item in terms_payload[:5]:
        m = compute_term_metrics(item.get("series") or [])
        entry = {"term": item.get("term", ""), "metrics": m}
        if m:
            tm = term_momentum(m, item.get("freshness"))
            entry.update(tm or {})
        per_term.append(entry)
    scored = [t for t in per_term if t.get("momentum") is not None]
    strongest = max(scored, key=lambda t: t["momentum"])["term"] if scored else None
    weakest = min(scored, key=lambda t: t["momentum"])["term"] if scored else None
    overall = round(_mean([t["momentum"] for t in scored]), 1) if scored else None
    direction = "insufficient data"
    if scored:
        g = _mean([t["metrics"]["recent_growth_pct"] for t in scored if t["metrics"]])
        if g is not None:
            direction = "rising" if g > 5 else ("declining" if g < -5 else "flat")
    return {"terms": per_term, "overall_momentum": overall, "direction": direction,
            "observations": sum(t["metrics"]["observations"] for t in scored if t.get("metrics")),
            "strongest_term": strongest, "weakest_term": weakest}


def trends_embed_url(terms, geo="", timeframe="now 12-m"):
    """Generate a Google Trends comparison/explore URL for up to 5 terms.

    Uses the standard comma-separated comparison query which opens the native
    Trends comparison chart (no API key required). Also returns an embed-frame
    style URL that can be iframed locally.
    """
    clean = [t.strip() for t in terms if t and t.strip()][:5]
    kw = quote(",".join(clean), safe=",")
    hl = "en"
    base = f"https://trends.google.com/trends/explore?hl={hl}&geo={quote(geo)}&q={kw}"
    embed = base + "&date=now%2012-m"
    return {"explore_url": base, "embed_url": embed}


# ------------------------------------------------------------ INPUT 3 ------
# Competitor metrics

def compute_competitor_metrics(rec, today=None):
    """MEASURED per-video competition metrics."""
    views = _num(rec.get("views"))
    subs = _num(rec.get("subscribers"))
    likes = _num(rec.get("likes"), 0)
    comments = _num(rec.get("comments"), 0)
    typical = _num(rec.get("channel_typical_views"))
    upload = rec.get("upload_date")
    age_days = None
    if upload:
        try:
            ud = datetime.strptime(str(upload)[:10], "%Y-%m-%d").date()
            ref = today or date.today()
            age_days = max(1, (ref - ud).days)
        except ValueError:
            age_days = None
    vpd = views / age_days if views and age_days else None
    vps = views / subs if views and subs else None
    eng = (likes + comments) / views * 100.0 if views else None
    outlier_ratio = views / typical if views and typical else None
    return {
        "age_days": age_days,
        "views_per_day": round(vpd, 1) if vpd else None,
        "views_per_subscriber": round(vps, 2) if vps else None,
        "engagement_rate_pct": round(eng, 2) if eng is not None else None,
        "outlier_ratio": round(outlier_ratio, 2) if outlier_ratio else None,
    }


def competition_report(records, today=None):
    """OUTPUT 3 — ranking, median velocity, outliers, saturation, opportunity."""
    enriched = []
    for r in records:
        m = compute_competitor_metrics(r, today)
        enriched.append({**{k: r.get(k) for k in ("id", "title", "channel", "views",
                                                   "subscribers", "upload_date",
                                                   "channel_typical_views")}, "metrics": m})
    velocities = [e["metrics"]["views_per_day"] for e in enriched if e["metrics"]["views_per_day"]]
    median_v = _median(velocities)
    avg_v = _round(_mean(velocities), 1) if velocities else None
    for e in enriched:
        m = e["metrics"]
        m["velocity_index"] = (round(m["views_per_day"] / median_v, 2)
                               if m["views_per_day"] and median_v else None)
    ranked = sorted(enriched, key=lambda e: (e["metrics"]["views_per_day"] or -1), reverse=True)

    recent_outliers = [e for e in enriched
                       if e["metrics"]["outlier_ratio"] and e["metrics"]["outlier_ratio"] >= 4
                       and e["metrics"]["age_days"] and e["metrics"]["age_days"] <= 30]
    strong_recent = [e for e in enriched
                     if e["metrics"]["velocity_index"] and e["metrics"]["velocity_index"] >= 1.5
                     and e["metrics"]["age_days"] and e["metrics"]["age_days"] <= 30]

    n = len(enriched)
    active_recent = len([e for e in enriched if e["metrics"]["age_days"] and e["metrics"]["age_days"] <= 30])
    if n == 0:
        saturation = "unknown"
    elif active_recent >= 4 or len(recent_outliers) >= 3:
        saturation = "high"
    elif active_recent >= 2 or len(recent_outliers) >= 2:
        saturation = "medium-high"
    elif active_recent >= 1:
        saturation = "medium"
    else:
        saturation = "low"

    # Competitive opportunity score (heuristic mapping, fully transparent):
    # demand signal (outliers/velocity spread) minus saturation penalty.
    if n == 0:
        opp = None
    else:
        base = 50.0
        base += min(20.0, len(recent_outliers) * 8.0)          # proven fresh demand
        base += min(10.0, len(strong_recent) * 4.0)            # above-median performers
        avg_e = _mean([e["metrics"]["engagement_rate_pct"] for e in enriched
                       if e["metrics"]["engagement_rate_pct"] is not None])
        if avg_e is not None:
            base += clamp((avg_e - 2.0) * 4.0, -8.0, 10.0)     # audience engagement
        penalty = {"low": 0, "medium": 6, "medium-high": 12, "high": 22}.get(saturation, 8)
        if median_v and avg_v and avg_v > median_v * 1.8:
            penalty -= 4  # heavy right tail => breakout possible even if crowded
        opp = round(clamp(base - penalty), 1)

    evidence = evidence_quality(n, has_series=False)
    if opp is not None:
        # demand-presence bonus: recent above-median performers indicate an
        # actively watched topic even when the sample is small (capped +10)
        opp = round(clamp(opp + min(10.0, len(strong_recent) * 5.0)), 1)
    return {
        "ranked": ranked,
        "median_velocity": round(median_v, 1) if median_v else None,
        "average_velocity": avg_v,
        "sample_size": n,
        "recent_high_velocity_outliers": [e["title"] or e["channel"] for e in recent_outliers],
        "saturation_level": saturation,
        "competitive_opportunity_score": opp,
        "evidence_quality": evidence,
    }


def _round(v, nd):
    return None if v is None else round(v, nd)


# ------------------------------------------------------------ INPUT 4 ------
# Title scoring

STOPWORDS = set("the a an and or of to in for on with is are was be it this that".split())

def analyze_title(text, ratings=None, linked_momentum=None):
    """PROCESS 4 — deterministic title analysis + optional combined score.

    Objective (measured) features are always computed; subjective qualities
    (clarity, curiosity, stakes, novelty, relevance) come from user/AI ratings
    when provided, otherwise they fall back to structural heuristics which are
    clearly labelled as estimates.
    """
    text = (text or "").strip()
    words = re.findall(r"[A-Za-z0-9'’]+", text)
    n_words = len(words)
    n_chars = len(text)
    front_loaded_core = n_words and len(" ".join(words[: min(6, n_words)])) <= 40
    has_number = bool(re.search(r"\d", text))
    has_question = "?" in text
    has_negation = bool(re.search(r"\b(no|not|never|don'?t|doesn'?t|without|why)\b", text, re.I))
    has_contrast = bool(re.search(r"\b(but|versus|vs\.?|instead|however|yet)\b", text, re.I)) \
        or (text.count(".") >= 2 and "." in text[:-1])
    has_strong_claim = bool(re.search(r"\b(worst|best|truth|secret|really|actually|only|never|finally)\b", text, re.I))
    stopword_ratio = len([w for w in words if w.lower() in STOPWORDS]) / max(1, n_words)
    length_penalty = 0.0
    if n_chars > 70:
        length_penalty = min(15.0, (n_chars - 70) * 0.5)
    elif n_chars < 25:
        length_penalty = min(10.0, (25 - n_chars) * 0.4)

    measured = {
        "words": n_words, "characters": n_chars,
        "front_load_ok": bool(front_loaded_core),
        "has_number": has_number, "has_question": has_question,
        "has_negation_or_why": has_negation, "contrast_structure": has_contrast,
        "strong_claim_words": has_strong_claim,
        "stopword_ratio": round(stopword_ratio, 2),
        "length_penalty": round(length_penalty, 1),
    }

    est = {
        "clarity": clamp(100 - abs(n_chars - 55) * 0.6 - stopword_ratio * 40),
        "curiosity": clamp(55 + 15 * has_negation + 15 * has_question + 12 * has_contrast + 10 * has_strong_claim),
        "specificity": clamp(50 + 20 * has_number + 15 * has_strong_claim + 15 * front_loaded_core),
        "stakes": clamp(50 + 15 * has_strong_claim + 12 * has_negation + 10 * has_contrast),
        "novelty": clamp(55 + 15 * has_contrast + 12 * has_strong_claim + 8 * has_question),
        "relevance": None,  # cannot be estimated without project context
        "readability": clamp(92 - length_penalty - max(0, n_words - 12) * 2),
    }
    ratings_src = "estimated (structural heuristics)"
    given = {k: _num(v) for k, v in (ratings or {}).items() if _num(v) is not None}
    if given:
        merged = {k: (given.get(k, est[k])) for k in est}
        if len(given) >= 3:
            ratings_src = "user/AI rated"
        est = merged

    score = sum(TITLE_FORMULA[k] * clamp(est[k] if est[k] is not None else 60.0) for k in TITLE_FORMULA)
    score = round(clamp(score - measured["length_penalty"] * 0.3), 2)

    combined = None
    if linked_momentum is not None:
        combined = round(COMBINED_TITLE_MIX["title"] * score + COMBINED_TITLE_MIX["momentum"] * linked_momentum, 2)

    reasons = []
    if measured["contrast_structure"]:
        reasons.append("contrast/twist structure present")
    if measured["has_negation_or_why"]:
        reasons.append("negation or question framing supports curiosity gap")
    if measured["characters"] > 70:
        reasons.append(f"title too long ({measured['characters']} chars); mobile truncates near 60")
    if not measured["front_load_ok"]:
        reasons.append("core idea not front-loaded within first ~6 words")
    if measured["stopword_ratio"] > 0.45:
        reasons.append("high stopword ratio; wording could be tighter")
    improvements = []
    if measured["characters"] > 60:
        improvements.append("Trim to <=60 characters so nothing is cut off on mobile.")
    if not measured["has_number"] and est["specificity"] < 75:
        improvements.append("Add one concrete detail (number, timeframe, named entity) to raise specificity.")
    if not measured["contrast_structure"] and est["curiosity"] < 80:
        improvements.append("Introduce a contradiction or unexpected pivot ('X doesn't need Y. It needs Z.').")
    if est["stakes"] is not None and est["stakes"] < 75:
        improvements.append("State what is at risk for the viewer (cost, deadline, consequence).")

    return {
        "text": text, "measured": measured, "qualities": {k: round(v, 1) if v is not None else None for k, v in est.items()},
        "ratings_source": ratings_src,
        "title_score": score,
        "score_formula": "0.20*clarity+0.20*curiosity+0.15*stakes+0.15*novelty+0.15*relevance+0.15*readability",
        "linked_momentum": linked_momentum,
        "combined_score": combined,
        "reasons": reasons, "improvements": improvements,
    }


def _syl_per_word(words):
    total = 0
    for w in words:
        v = len(re.findall(r"[aeiouy]+", w.lower()))
        total += max(1, v)
    return total / max(1, len(words))


def titles_report(analyses):
    ranked = sorted([a for a in analyses if a.get("combined_score") is not None],
                    key=lambda a: a["combined_score"], reverse=True)
    if not ranked:
        ranked = sorted(analyses, key=lambda a: a["title_score"], reverse=True)
    best = ranked[0]["text"] if ranked else None
    worst = ranked[-1]["text"] if ranked else None
    top_score = ranked[0].get("combined_score", ranked[0]["title_score"]) if ranked else None
    return {"ranked": ranked, "best_title": best, "worst_title": worst,
            "top_score": top_score,
            "note": "Combined score = 0.70*Title Score + 0.30*Trend Momentum where a term is linked."}


# ------------------------------------------------------------ INPUT 5 ------

THUMB_ATTRS = ["simplicity", "contrast", "emotion", "specificity", "mobile_readability",
               "text_load", "complementarity", "curiosity", "hierarchy"]

def thumbnail_report(data):
    scores = {}
    for a in THUMB_ATTRS:
        v = _num(data.get(a))
        if v is not None:
            scores[a] = clamp(v)
    # 'text_load' input means LOW burden quality (higher = less text burden)
    core = ["simplicity", "contrast", "emotion", "specificity", "mobile_readability",
            "text_load", "complementarity"]
    used = [scores[a] for a in core if a in scores]
    extra = [scores[a] for a in ("curiosity", "hierarchy") if a in scores]
    all_used = used + extra
    score = round(sum(all_used) / len(all_used), 2) if all_used else None
    weaknesses = [{"attribute": a, "score": s} for a, s in scores.items() if s < 70]
    recs = []
    if scores.get("text_load", 100) < 70:
        recs.append("Reduce thumbnail text to 1-3 words; let the title carry detail.")
    if scores.get("contrast", 100) < 70:
        recs.append("Increase subject/background contrast (bright rim light or complementary background).")
    if scores.get("mobile_readability", 100) < 70:
        recs.append("Test at 120px width; enlarge the main subject until readable on a phone feed.")
    if scores.get("complementarity", 100) < 70:
        recs.append("Thumbnail should add information the title omits instead of repeating it.")
    if scores.get("specificity", 100) < 70:
        recs.append("Show one identifiable object (GPU, power line, chart) rather than abstract tech art.")
    return {"thumbnail_score": score, "attributes": scores, "weaknesses": weaknesses,
            "recommendations": recs,
            "formula": "mean of rated attributes (7 core attributes; extra rated attributes included)",
            "image_path": data.get("image_path") or None}


# ------------------------------------------------------------ INPUT 6 ------

STRUCT_ATTRS = ["hook", "promise", "curiosity_setup", "progression", "open_loops",
                "payoff_density", "pacing", "visual_variety", "escalation", "ending"]

def retention_report(data):
    scores = {a: clamp(_num(data.get(a))) for a in STRUCT_ATTRS if _num(data.get(a)) is not None}
    score = round(sum(scores.values()) / len(scores), 2) if scores else None
    risks, recs = [], []
    def chk(key, thr, risk, rec):
        if key in scores and scores[key] < thr:
            risks.append(risk); recs.append(rec)
    chk("hook", 70, "WEAK OPENING: hook below 70 risks first-30-second drop-off.",
        "Open with the sharpest concrete fact or visual inside the first 15 seconds.")
    chk("promise", 70, "UNCLEAR PROMISE: viewers may not know what payoff they are waiting for.",
        "State the specific promise/question the video will answer by minute one.")
    chk("curiosity_setup", 70, "DELAYED VALUE: curiosity not opened early enough.",
        "Raise the central question before the first explanation block.")
    chk("progression", 70, "FLAT MIDDLE: story progression rated low; mid-video drift risk.",
        "Restructure the middle as escalating reveals instead of parallel facts.")
    chk("open_loops", 70, "LOW RETENTION TENSION: too few open loops holding viewers forward.",
        "Plant 2-3 unanswered questions early and resolve them late.")
    chk("payoff_density", 70, "LOW PAYOFF DENSITY: long exposition relative to delivered value.",
        "Cut exposition; guarantee one new insight every 60-90 seconds.")
    chk("pacing", 70, "PACING RISK: pacing rated low.",
        "Tighten edits, remove dead air, vary shot rhythm every 5-8 seconds.")
    chk("visual_variety", 70, "VISUAL MONOTONY risk reducing watch time.",
        "Alternate footage types (data viz, archival, animation, map) per segment.")
    chk("escalation", 70, "NO ESCALATION: later segments not stronger than earlier ones.",
        "Order beats so stakes/insight escalate toward the finale.")
    chk("ending", 70, "WEAK ENDING/PAYOFF: resolution rated low.",
        "Deliver the promised answer explicitly and end with a forward-looking beat.")
    return {"retention_score": score, "components": scores, "risk_points": risks,
            "recommendations": recs, "formula": "mean of rated structural components"}


# ------------------------------------------------------------ INPUT 7 ------

NICHE_RPM_DEFAULTS = {
    "technology_software": (4, 12), "finance_investing": (8, 25), "business": (6, 18),
    "ai_datacenter": (5, 14), "education_explainer": (3, 9), "entertainment": (1, 5),
    "gaming": (1.5, 6), "health": (4, 12), "autos": (3, 10), "general": (2, 8),
}

def monetization_report(data):
    niche = (data.get("niche") or "general").strip().lower().replace(" ", "_")
    rpm_lo, rpm_hi = NICHE_RPM_DEFAULTS.get(niche, NICHE_RPM_DEFAULTS["general"])
    rpm = _num(data.get("rpm"))
    views = _num(data.get("monthly_views"), 0) or 0
    aff_ctr = (_num(data.get("affiliate_ctr"), 0) or 0) / 100.0 if _num(data.get("affiliate_ctr")) is not None else 0
    aff_conv = (_num(data.get("affiliate_conv"), 0) or 0) / 100.0 if _num(data.get("affiliate_conv")) is not None else 0
    commission = _num(data.get("commission"), 0) or 0
    sponsorship = _num(data.get("sponsorship"), 0) or 0
    other = _num(data.get("other_revenue"), 0) or 0

    def adsense(r):
        return views / 1000.0 * r

    clicks = views * aff_ctr
    conversions = clicks * aff_conv
    affiliate = conversions * commission
    scenarios = []
    for label, r in (("conservative (niche low RPM)", rpm_lo),
                     ("base (entered/estimated RPM)", rpm if rpm else (rpm_lo + rpm_hi) / 2),
                     ("optimistic (niche high RPM)", rpm_hi)):
        scenarios.append({
            "name": label, "rpm": round(r, 2),
            "adsense_estimate": round(adsense(r), 2),
            "affiliate_estimate": round(affiliate, 2),
            "sponsorship_estimate": round(sponsorship, 2),
            "other_estimate": round(other, 2),
            "total_potential": round(adsense(r) + affiliate + sponsorship + other, 2),
        })
    totals = [s["total_potential"] for s in scenarios] if views or sponsorship or other else []
    # commercial-value score: log-scaled expected upside from the *base*
    # scenario, capped at 100 (transparent heuristic, scenario-based)
    base_total = scenarios[1]["total_potential"] if (views or sponsorship or other) else 0.0
    score = round(clamp(20 * math.log10(max(base_total, 1) + 1) + (10 if sponsorship else 0)
                        + (10 if affiliate > 0 else 0)), 1)
    return {
        "niche": niche, "niche_rpm_range_usd": [rpm_lo, rpm_hi],
        "inputs": {"rpm": rpm, "monthly_views": views, "affiliate_clicks": round(clicks, 1),
                   "affiliate_conversions": round(conversions, 2), "commission": commission,
                   "sponsorship": sponsorship, "other": other},
        "scenarios": scenarios,
        "expected_total_usd": round(_mean(totals), 2) if totals else 0.0,
        "base_scenario_total_usd": base_total,
        "commercial_value_score": score,
        "disclaimer": "Scenario estimates only — these are assumptions, not guaranteed income.",
    }


# ------------------------------------------------------------- INPUT 8 -----
# Mathematical opportunity engine

def evidence_quality(n_records, has_series=False, completeness=None):
    """Heuristic label of how much real data backs the score."""
    pts = 0
    if has_series:
        pts += 2
    pts += min(3, (n_records or 0) // 2)
    if completeness is not None:
        pts += 3 if completeness >= 0.85 else (2 if completeness >= 0.6 else (1 if completeness >= 0.35 else 0))
    if pts >= 6:
        return "High"
    if pts >= 4:
        return "Medium"
    if pts >= 2:
        return "Low"
    return "Very low"


def preliminary_engine(project, trend, comp, pack_title_score, thumb_score,
                       retention, visual_potential, monetization, completeness=1.0):
    """PROCESS 8 — weighted score + bounded evidence modifiers, clamped 0-100."""
    weights = db_weights()
    comps = {
        "trend_momentum": trend.get("overall_momentum"),
        "audience_demand": _num(trend.get("audience_demand")),
        "competitive_opportunity": comp.get("competitive_opportunity_score"),
        "packaging_strength": (round(PACKAGING_MIX["title"] * pack_title_score
                                     + PACKAGING_MIX["thumbnail"] * thumb_score, 2)
                               if pack_title_score is not None and thumb_score is not None
                               else pack_title_score if pack_title_score is not None else thumb_score),
        "retention_design": retention.get("retention_score"),
        "visual_potential": _num(visual_potential),
        "monetization_potential": monetization.get("commercial_value_score"),
    }
    # renormalize weights over available components so missing data lowers
    # evidence quality rather than silently zeroing the score
    avail = {k: v for k, v in comps.items() if v is not None}
    wsum = sum(weights[k] for k in avail) or 1.0
    wn = {k: weights[k] / wsum for k in avail}
    core = round(sum(wn[k] * avail[k] for k in avail), 2)

    eq = evidence_quality(comp.get("sample_size"), has_series=bool(trend.get("observations")),
                           completeness=completeness)
    modifiers, notes = [], []
    outliers = comp.get("recent_high_velocity_outliers") or []
    if len(outliers) >= 2:
        modifiers.append(("recent competitor outliers", +2)); notes.append("+2: >=2 strong recent outlier videos confirm live demand")
    g = trend.get("direction")
    if g == "rising" and (trend.get("overall_momentum") or 0) >= 75:
        modifiers.append(("strong positive trend growth", +2)); notes.append("+2: rising trend with momentum >= 75")
    if g == "declining":
        modifiers.append(("declining trend momentum", -3)); notes.append("-3: measured trend direction is declining")
    sat = comp.get("saturation_level")
    if sat == "high":
        modifiers.append(("severe saturation", -8)); notes.append("-8: topic saturated with many active recent uploads")
    elif sat == "medium-high":
        modifiers.append(("medium-high saturation", -4)); notes.append("-4: multiple channels actively covering the subject")
    diff = _num(project.get("differentiation_rating"))
    if diff is not None and diff < 50:
        modifiers.append(("weak differentiation", -5)); notes.append("-5: proposed angle barely differs from existing videos")
    if (comp.get("sample_size") or 0) < 3:
        modifiers.append(("insufficient sample size", -3)); notes.append("-3: fewer than 3 competitor records")
    if eq == "Very low":
        modifiers.append(("poor data quality", -5)); notes.append("-5: very little measured data behind inputs")
    mod_total = sum(m[1] for m in modifiers)
    final = clamp(core + mod_total)

    decision = "STOP"
    for thr, label in DECISION_BANDS:
        if final >= thr:
            decision = label
            break
    if eq in ("Low", "Very low") and decision == "GO":
        decision = "PROMISING"
        notes.append("Decision downgraded GO->PROMISING because evidence quality is low.")

    named = {k: v for k, v in comps.items() if v is not None}
    strongest = max(named, key=named.get) if named else None
    weakest = min(named, key=named.get) if named else None
    return {
        "score_type": "evidence-based opportunity score (NOT a virality probability)",
        "weights_used": {k: round(wn[k], 4) for k in avail},
        "weights_missing_note": "Weights renormalized over components with data; missing data reduces evidence quality.",
        "components": comps,
        "core_weighted_score": core,
        "modifiers": [{"factor": f, "points": p} for f, p in modifiers],
        "modifier_notes": notes,
        "modifier_total": mod_total,
        "final_score": round(final, 2),
        "preliminary_decision": decision,
        "evidence_quality": eq,
        "strongest_factor": strongest,
        "weakest_factor": weakest,
        "calibrated_weights": weights is not DEFAULT_WEIGHTS,
    }


def db_weights():
    import db as _db
    stored = _db.setting_get("weights")
    if stored and isinstance(stored, dict):
        merged = dict(DEFAULT_WEIGHTS)
        merged.update({k: v for k, v in stored.items() if k in merged})
        return merged
    return DEFAULT_WEIGHTS


# ------------------------------------------------------------- INPUT 10 ----

def final_decision(math_res, ai_res, user_notes=""):
    """PROCESS 10 — synthesis of deterministic score + AI audit (+notes)."""
    ms = math_res["final_score"]
    as_ = _num(ai_res.get("ai_score")) if ai_res else None
    if as_ is None:
        final = ms
        mix_note = "AI audit unavailable — final score equals deterministic score."
    else:
        final = round(FINAL_MIX["math"] * ms + FINAL_MIX["ai"] * as_, 2)
        mix_note = f"final = {FINAL_MIX['math']}*math({ms}) + {FINAL_MIX['ai']}*AI({as_})"
    disagreement = None
    if as_ is not None:
        delta = as_ - ms
        if abs(delta) >= 10 or (ai_res.get("decision") and ai_res["decision"] != math_res["preliminary_decision"]):
            disagreement = (f"DISAGREEMENT FLAGGED: math={ms} ({math_res['preliminary_decision']}) vs "
                            f"AI={as_} ({ai_res.get('decision')}); delta={round(delta,1)}. "
                            "Review weak evidence before trusting either side.")
    eq = math_res["evidence_quality"]
    if final >= 80 and eq in ("High", "Medium"):
        decision = "GO"
    elif final >= 80:
        decision = "PROMISING"
    elif final >= 65:
        decision = "PROMISING"
    elif final >= 50:
        decision = "REWORK"
    else:
        decision = "STOP"
    required = []
    if eq in ("Low", "Very low"):
        required.append("Collect more measured evidence (trend observations, >=3 competitor records) before production.")
    if disagreement:
        required.append("Resolve the math-vs-AI disagreement by auditing its underlying numbers.")
    claims = (ai_res or {}).get("required_checks") or []
    required.extend(claims)
    return {
        "final_score": final,
        "decision": decision,
        "mix": mix_note,
        "disagreement": disagreement,
        "math_score": ms,
        "ai_score": as_,
        "evidence_quality": eq,
        "required_changes_before_production": required,
        "user_notes": user_notes,
        "disclaimer": "This is an evidence-based opportunity score, not a prediction of YouTube's algorithm or a virality probability.",
    }


# ------------------------------------------------------------- INPUT 11 ----

def channel_baselines(exclude_project_id=None):
    import db as _db
    rows = _db.query(
        "SELECT p.id, pu.* FROM publications pu JOIN projects p ON p.id=pu.project_id"
        + (" WHERE pu.project_id!=?" if exclude_project_id else ""),
        ((exclude_project_id,) if exclude_project_id else ()),
    )
    if not rows:
        return None
    ctrs = [_num(r["ctr"]) for r in rows if _num(r["ctr"]) is not None]
    pcts = [_num(r["avg_pct_viewed"]) for r in rows if _num(r["avg_pct_viewed"]) is not None]
    subs = [_num(r["subscribers_gained"]) for r in rows if _num(r["subscribers_gained"]) is not None]
    revs = [_num(r["revenue"]) for r in rows if _num(r["revenue"]) is not None]
    views = [_num(r["views"]) for r in rows if _num(r["views"]) is not None]
    if not ctrs and not pcts:
        return None
    return {
        "videos_observed": len(rows),
        "median_ctr_pct": _round(_median(ctrs), 2),
        "median_avg_pct_viewed": _round(_median(pcts), 2),
        "median_subscribers_gained": _round(_median(subs), 1),
        "median_revenue_usd": _round(_median(revs), 2),
        "median_views": _round(_median(views), 1),
    }


def publication_comparison(pub, project_snapshot):
    """PROCESS 11 — compare predicted packaging/retention against reality.

    Predicted proxies: the packaging component score (title+thumbnail) and the
    retention-design score from the stored calculation snapshot; actuals: CTR
    (packaging outcome), average percentage viewed (retention outcome) and view
    count (demand outcome), each expressed as a % delta versus the channel's
    own historical median baseline.
    """
    out = {}
    actual_ctr = _num(pub.get("ctr"))
    actual_pct = _num(pub.get("avg_pct_viewed"))
    avd = _num(pub.get("avg_view_duration_sec"))
    vlen = _num(pub.get("video_length_sec"))
    if actual_pct is None and avd and vlen:
        actual_pct = round(avd / vlen * 100.0, 1)
    baseline = channel_baselines(exclude_project_id=pub.get("project_id"))
    comps = (project_snapshot.get("components") or {})
    pack_pred = comps.get("packaging_strength")
    ret_pred = comps.get("retention_design")
    views = _num(pub.get("views"))
    out["actual"] = {"ctr_pct": actual_ctr, "avg_pct_viewed": actual_pct,
                     "views": views, "subscribers_gained": _num(pub.get("subscribers_gained")),
                     "revenue_usd": _num(pub.get("revenue"))}
    out["predicted_proxy"] = {"packaging_score": pack_pred, "retention_score": ret_pred,
                              "overall_opportunity_score": project_snapshot.get("final_score")}
    deltas = {}
    if baseline:
        out["channel_baseline"] = baseline
        if actual_ctr is not None and baseline.get("median_ctr_pct"):
            deltas["packaging_vs_baseline_pct"] = round((actual_ctr - baseline["median_ctr_pct"]) / baseline["median_ctr_pct"] * 100.0, 1)
        if actual_pct is not None and baseline.get("median_avg_pct_viewed"):
            deltas["retention_vs_baseline_pct"] = round((actual_pct - baseline["median_avg_pct_viewed"]) / baseline["median_avg_pct_viewed"] * 100.0, 1)
        if views and baseline.get("median_views"):
            deltas["views_vs_baseline_pct"] = round((views - baseline["median_views"]) / baseline["median_views"] * 100.0, 1)
    # per-component prediction error: signed difference between normalized
    # component expectation and normalized realized outcome (transparent proxy)
    errors = {}
    if pack_pred is not None and actual_ctr is not None:
        errors["packaging_error"] = round(clamp(actual_ctr * 12, 0, 100) - float(pack_pred), 1)
    if ret_pred is not None and actual_pct is not None:
        errors["retention_error"] = round(clamp(actual_pct * 1.6, 0, 100) - float(ret_pred), 1)
    out["component_prediction_errors"] = errors
    out["prediction_error_deltas"] = deltas
    verdict = "insufficient history"
    if deltas:
        perf = _mean([d for d in deltas.values() if d is not None])
        if perf is not None:
            verdict = "successful prediction (materially above channel baseline)" if perf > 15 else (
                "underperformed baseline — inspect which component mis-predicted" if perf < -15 else "near baseline")
    out["verdict"] = verdict
    return out


def recalibrate(min_videos=5):
    """PROCESS 11 — gradient-free weight update from historical outcome deltas.

    Guardrails: max +/-4 percentage points per component per calibration run,
    weights stay normalized and never drop below 5%; requires >=min_videos
    published outcomes. Returns (weights, status_message).
    """
    import db as _db
    pubs = _db.query("""SELECT c.payload_json, pu.comparison_json FROM publications pu
                        JOIN calculations c ON c.project_id=pu.project_id""")
    samples = []
    for r in pubs:
        try:
            snap = json.loads(r["payload_json"])
            cmp_ = json.loads(r["comparison_json"]) if r["comparison_json"] else None
            if cmp_ and cmp_.get("prediction_error_deltas"):
                samples.append((snap, cmp_))
        except Exception:
            continue
    if len(samples) < min_videos:
        return db_weights(), (f"Calibration needs at least {min_videos} published videos with results "
                              f"(have {len(samples)}); generic weights retained.")
    weights = dict(db_weights())
    comp_key = {"packaging_vs_baseline_pct": "packaging_strength",
                "retention_vs_baseline_pct": "retention_design",
                "views_vs_baseline_pct": "trend_momentum"}
    adj = {k: 0.0 for k in weights}
    n = len(samples)
    for snap, cmp_ in samples:
        comps = snap.get("components") or {}
        deltas = cmp_["prediction_error_deltas"]
        for dk, ck in comp_key.items():
            d = deltas.get(dk)
            if d is None:
                continue
            # if outcome exceeded baseline, credit the component that was high;
            # if underperformed, penalize it. Scale small, clip hard.
            strength = (comps.get(ck) or 50.0) / 100.0
            sign = 1.0 if d > 0 else -1.0
            adj[ck] += sign * strength * min(abs(d), 50.0) / 50.0 * 0.04  # up to 4pp influence per sample
    changed = []
    for k, a in adj.items():
        step = clamp(a / n, -0.04, 0.04)
        if abs(step) >= 0.002:
            old = weights[k]
            weights[k] = max(0.05, min(0.40, old + step))
            changed.append(f"{k}: {old:.2f}->{weights[k]:.2f}")
    tot = sum(weights.values())
    weights = {k: round(v / tot, 4) for k, v in weights.items()}
    _db.setting_set("weights", weights)
    progress = min(100, int(round(n / min_videos * 100)))
    _db.setting_set("calibration_state", {"samples": n, "updated": str(datetime.now()),
                                          "changes": changed, "progress_pct": progress})
    return weights, (f"Weights updated from {n} historical outcomes "
                     f"(calibration maturity {progress}% of the {min_videos}-video minimum).")


def preliminary_decision_label(score):
    for thr, label in DECISION_BANDS:
        if score >= thr:
            return label
    return "STOP"
