"""SQLite persistence layer for the YouTube Video Opportunity Analyzer.

Stores every stage of the pipeline: projects, trend terms/series, competitors,
titles, thumbnails, structure, monetization, calculations, AI reports, final
decisions, publication results and calibration state.
"""
import json
import os
import sqlite3

DB_PATH = os.environ.get("YTAI_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "analyzer.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    topic TEXT,
    audience TEXT,
    market TEXT,
    language TEXT,
    category TEXT,
    format TEXT,
    notes TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS trend_terms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    term TEXT NOT NULL,
    geo TEXT DEFAULT '',
    timeframe TEXT DEFAULT '12m',
    series_json TEXT,            -- list of {date, value}
    metrics_json TEXT,           -- computed per-term metrics
    momentum REAL,               -- 0-100
    imported_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS competitors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title TEXT,
    channel TEXT,
    subscribers INTEGER,
    views INTEGER,
    upload_date TEXT,
    likes INTEGER,
    comments INTEGER,
    channel_typical_views INTEGER,
    metrics_json TEXT,           -- computed per-video metrics
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS titles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    linked_term TEXT,
    ratings_json TEXT,           -- user/AI supplied quality ratings
    metrics_json TEXT            -- computed title score + combined score
);

CREATE TABLE IF NOT EXISTS thumbnails (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    subject TEXT,
    thumb_text TEXT,
    emotion TEXT,
    contrast INTEGER,
    simplicity INTEGER,
    mobile_readability INTEGER,
    text_load INTEGER,
    complementarity INTEGER,
    curiosity INTEGER,
    hierarchy INTEGER,
    specificity INTEGER,
    image_path TEXT,
    score REAL,
    weaknesses_json TEXT,
    recommendations_json TEXT
);

CREATE TABLE IF NOT EXISTS structures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    hook INTEGER, promise INTEGER, curiosity_setup INTEGER, progression INTEGER,
    open_loops INTEGER, payoff_density INTEGER, pacing INTEGER, visual_variety INTEGER,
    escalation INTEGER, ending INTEGER,
    score REAL, risks_json TEXT, recommendations_json TEXT
);

CREATE TABLE IF NOT EXISTS monetizations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    niche TEXT, rpm REAL, monthly_views REAL, affiliate_ctr REAL, affiliate_conv REAL,
    commission REAL, sponsorship REAL, other_revenue REAL,
    scenarios_json TEXT, score REAL
);

CREATE TABLE IF NOT EXISTS calculations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL UNIQUE REFERENCES projects(id) ON DELETE CASCADE,
    payload_json TEXT,           -- full deterministic engine output
    overall_score REAL,
    preliminary_decision TEXT,
    evidence_quality TEXT,
    weights_json TEXT,           -- weights actually used (may be calibrated)
    calculated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS ai_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    model TEXT,
    request_json TEXT,
    response_json TEXT,
    error TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    final_score REAL,
    decision TEXT,
    payload_json TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS publications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    days_since_upload INTEGER,
    impressions INTEGER, ctr REAL, views INTEGER, avg_view_duration_sec INTEGER,
    video_length_sec INTEGER, avg_pct_viewed REAL, retention_milestones TEXT,
    returning_viewers_pct REAL, traffic_sources TEXT, subscribers_gained INTEGER,
    revenue REAL,
    comparison_json TEXT,        -- prediction vs actual deltas
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_conn()
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def setting_get(key, default=None):
    conn = get_conn()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except Exception:
        return row["value"]


def setting_set(key, value):
    conn = get_conn()
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value)),
    )
    conn.commit()
    conn.close()


# ---------- generic helpers ----------

def insert(table, data: dict):
    cols = ", ".join(data.keys())
    marks = ", ".join(["?"] * len(data))
    conn = get_conn()
    cur = conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", list(data.values()))
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid


def update(table, rid, data: dict):
    sets = ", ".join(f"{k}=?" for k in data)
    conn = get_conn()
    conn.execute(f"UPDATE {table} SET {sets} WHERE id=?", list(data.values()) + [rid])
    conn.commit()
    conn.close()


def delete(table, rid):
    conn = get_conn()
    conn.execute(f"DELETE FROM {table} WHERE id=?", (rid,))
    conn.commit()
    conn.close()


def query(sql, args=()):
    conn = get_conn()
    rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
    conn.close()
    return rows


def query_one(sql, args=()):
    rows = query(sql, args)
    return rows[0] if rows else None
