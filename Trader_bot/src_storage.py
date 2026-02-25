# src_storage.py
import os
import sqlite3
import config


def ensure_dir(path: str):
    d = os.path.dirname(path)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)


def connect_db():
    db_path = getattr(config, "DB_PATH", "output/trader.sqlite")
    ensure_dir(db_path)
    return sqlite3.connect(db_path)


def ensure_schema(conn: sqlite3.Connection):
    cur = conn.cursor()

    # runs table
    cur.execute("""
    CREATE TABLE IF NOT EXISTS runs (
        run_id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT DEFAULT (datetime('now'))
    )
    """)

    # recommendations table
    cur.execute("""
    CREATE TABLE IF NOT EXISTS recommendations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER,
        symbol TEXT,
        decision TEXT,
        horizon TEXT,
        score_total REAL,
        confidence REAL,
        risk_score REAL,
        ref_price REAL,
        stop_price REAL,
        take_price REAL,
        rationale TEXT
    )
    """)

    # Add columns if missing
    cur.execute("PRAGMA table_info(recommendations)")
    cols = [r[1] for r in cur.fetchall()]
    if "suggested_qty" not in cols:
        cur.execute("ALTER TABLE recommendations ADD COLUMN suggested_qty INTEGER DEFAULT 0")
    if "gate_decision" not in cols:
        cur.execute("ALTER TABLE recommendations ADD COLUMN gate_decision TEXT")
    if "gate_confidence" not in cols:
        cur.execute("ALTER TABLE recommendations ADD COLUMN gate_confidence REAL")
    if "gate_reason" not in cols:
        cur.execute("ALTER TABLE recommendations ADD COLUMN gate_reason TEXT")

    conn.commit()


def create_run(conn: sqlite3.Connection) -> int:
    cur = conn.cursor()
    cur.execute("INSERT INTO runs DEFAULT VALUES")
    conn.commit()
    return int(cur.lastrowid)


def update_gate_result(conn: sqlite3.Connection, run_id: int, symbol: str,
                       decision: str, confidence: float, reason: str):
    """Write Claude soft gate result back to all recommendation rows for this run+symbol."""
    cur = conn.cursor()
    cur.execute("""
        UPDATE recommendations
        SET gate_decision = ?, gate_confidence = ?, gate_reason = ?
        WHERE run_id = ? AND UPPER(TRIM(symbol)) = UPPER(TRIM(?))
    """, (decision, float(confidence), reason, run_id, symbol))
    conn.commit()


def insert_recommendations(conn: sqlite3.Connection, run_id: int, recs: list[dict]):
    cur = conn.cursor()
    for r in recs:
        cur.execute("""
        INSERT INTO recommendations
        (run_id, symbol, decision, horizon, score_total, confidence, risk_score,
         ref_price, stop_price, take_price, rationale, suggested_qty)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            run_id,
            r.get("symbol"),
            r.get("decision"),
            r.get("horizon"),
            float(r.get("score_total", 0.0)),
            float(r.get("confidence", 0.0)),
            float(r.get("risk_score", 0.0)),
            float(r.get("ref_price", 0.0)),
            float(r.get("stop_price", 0.0)),
            float(r.get("take_price", 0.0)),
            r.get("rationale", ""),
            int(r.get("suggested_qty", 0)),
        ))

    conn.commit()
