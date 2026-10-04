"""AI audit client — Hugging Face OpenAI-compatible Router, model zai-org/GLM-5.3:novita.

The API key is read from the environment (HF_TOKEN) or from the locally stored
settings table; it is never written to exports or logs.
"""
import json
import os

import requests

import db

MODEL = "zai-org/GLM-5.3:novita"
ROUTER_URL = "https://router.huggingface.co/v1/chat/completions"

SYSTEM_PROMPT = """You are an independent production-decision auditor for a YouTube video project.
You receive structured JSON containing MEASURED data (Google Trends series and computed statistics,
competitor metrics), TRANSPARENT HEURISTIC scores (title/thumbnail/retention/monetization rubrics),
and a DETERMINISTIC opportunity score with its component breakdown and evidence-quality label.

Your job:
1. Independently re-examine the underlying numbers. Challenge unsupported assumptions and
   arithmetic that the deterministic engine may have distorted with weak or thin data.
2. Identify contradictions between components (e.g., rising trend vs declining related terms,
   high packaging score vs saturated competition).
3. Compare the candidate titles against each other and against the measured trend momentum.
4. Interpret the trend and competitor evidence; judge whether recent outliers show real demand
   or saturation risk.
5. Evaluate the proposed angle, thumbnail concept and retention design.
6. Decide your own verdict. You must NOT simply copy the mathematical score.

Rules:
- Never claim to know or reproduce YouTube's private recommendation algorithm.
- The opportunity score is evidence-based, never a probability of virality.
- If evidence is thin, say so and lower confidence instead of inventing certainty.

Respond with ONLY a valid JSON object (no markdown fences) with exactly these keys:
{
  "ai_score": <0-100 number>,
  "decision": "GO" | "PROMISING" | "REWORK" | "STOP",
  "strongest_evidence": "<string>",
  "weakest_evidence": "<string>",
  "contradictions": ["<string>", ...],
  "unsupported_assumptions": ["<string>", ...],
  "math_distortion_risk": "<how the deterministic result may be distorted by weak data>",
  "best_topic_angle": "<string>",
  "best_title": "<one of the supplied candidate titles, verbatim>",
  "thumbnail_recommendation": "<string>",
  "hook_recommendation": "<a concrete opening line / hook>",
  "major_risk": "<string>",
  "highest_priority_improvement": "<the single change with the biggest expected effect>",
  "required_checks": ["<factual or evidence checks that MUST pass before publishing>", ...]
}"""


def get_token():
    tok = os.environ.get("HF_TOKEN") or os.environ.get("HF_API_TOKEN")
    if not tok:
        tok = db.setting_get("hf_token")
    return (tok or "").strip() or None


def audit(project_json, token=None, model=MODEL):
    """Call the HF router chat-completions endpoint and parse the audit JSON."""
    token = token or get_token()
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(project_json, ensure_ascii=False)},
        ],
        "temperature": 0.2,
        "max_tokens": 1800,
    }
    if not token:
        return None, ("No Hugging Face API token configured. Set the HF_TOKEN environment "
                      "variable or save a token in Settings.")
    try:
        resp = requests.post(
            ROUTER_URL,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
            timeout=120,
        )
    except requests.RequestException as exc:
        return None, f"Network error calling HF router: {exc}"
    if resp.status_code != 200:
        detail = ""
        try:
            detail = resp.json().get("error", {}).get("message", "") or resp.text[:300]
        except Exception:
            detail = resp.text[:300]
        return None, f"HF router returned HTTP {resp.status_code}: {detail}"
    try:
        body = resp.json()
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None, "Unexpected response shape from HF router."
    parsed = _extract_json(content)
    if parsed is None:
        return {"raw_response": content, "ai_score": None,
                "parse_error": "Model did not return valid JSON."}, None
    parsed.setdefault("raw_response", content)
    return parsed, None


def _extract_json(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    # tolerate leading prose: find first { ... last }
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        obj = json.loads(text[start:end + 1])
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None
