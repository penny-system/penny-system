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

    # ---------------------------------------------------------------------------
    # recommendation_snapshots — frozen feature vector captured at BUY signal time
    # Gate fields are NULL at insert; backfilled by notify_scan.py after gate runs
    # ---------------------------------------------------------------------------
    cur.execute("""
    CREATE TABLE IF NOT EXISTS recommendation_snapshots (
        snapshot_id   INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id        INTEGER NOT NULL,
        symbol        TEXT    NOT NULL,
        horizon       TEXT    NOT NULL,
        score_total   REAL,
        confidence    REAL,
        risk_score    REAL,
        risk_flags    TEXT,
        ref_price     REAL,
        stop_price    REAL,
        take_price    REAL,
        suggested_qty INTEGER,
        setup_type    TEXT,
        rationale     TEXT,
        vol_surge     REAL,
        ret_1         REAL,
        ret_5         REAL,
        dollar_vol    REAL,
        breakout      INTEGER,
        realized_vol  REAL,
        sec_hits      INTEGER,
        sec_trigger_a INTEGER,
        stocktwits_score REAL,
        gate_decision    TEXT,
        gate_confidence  REAL,
        gate_reason      TEXT,
        created_at    TEXT DEFAULT (datetime('now')),
        UNIQUE(run_id, symbol, horizon)
    )
    """)

    # ---------------------------------------------------------------------------
    # open_positions — one row per IBKR fill; closed when exit fill detected
    # UNIQUE on ibkr_exec_id so repeat trades in same symbol are allowed
    # ---------------------------------------------------------------------------
    cur.execute("""
    CREATE TABLE IF NOT EXISTS open_positions (
        position_id       INTEGER PRIMARY KEY AUTOINCREMENT,
        snapshot_id       INTEGER,
        symbol            TEXT    NOT NULL,
        ibkr_exec_id      TEXT    UNIQUE,
        ibkr_order_id     INTEGER,
        oca_group         TEXT,
        fill_price        REAL,
        fill_qty          INTEGER,
        commission        REAL    DEFAULT 0.0,
        commission_source TEXT    DEFAULT 'ibkr',
        ref_price         REAL,
        stop_price        REAL,
        take_price        REAL,
        opened_at         TEXT,
        status            TEXT    DEFAULT 'open',
        created_at        TEXT    DEFAULT (datetime('now'))
    )
    """)

    # ---------------------------------------------------------------------------
    # closed_trades — one row per completed round-trip; linked back to open_positions
    # ---------------------------------------------------------------------------
    cur.execute("""
    CREATE TABLE IF NOT EXISTS closed_trades (
        trade_id           INTEGER PRIMARY KEY AUTOINCREMENT,
        position_id        INTEGER,
        snapshot_id        INTEGER,
        symbol             TEXT    NOT NULL,
        entry_price        REAL,
        entry_qty          INTEGER,
        exit_price         REAL,
        exit_qty           INTEGER,
        exit_reason        TEXT,
        gross_pnl          REAL,
        commission_total   REAL    DEFAULT 0.0,
        commission_source  TEXT    DEFAULT 'ibkr',
        net_pnl            REAL,
        outcome            TEXT,
        hold_seconds       INTEGER,
        opened_at          TEXT,
        closed_at          TEXT,
        entry_slippage     REAL    DEFAULT 0.0,
        exit_slippage      REAL    DEFAULT 0.0,
        ibkr_exec_id_entry TEXT,
        ibkr_exec_id_exit  TEXT
    )
    """)

    # ---------------------------------------------------------------------------
    # learning_snapshots — periodic analytics snapshots written by src_learning.py
    # ---------------------------------------------------------------------------
    cur.execute("""
    CREATE TABLE IF NOT EXISTS learning_snapshots (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at    TEXT DEFAULT (datetime('now')),
        total_trades  INTEGER,
        win_rate      REAL,
        avg_net_pnl   REAL,
        total_net_pnl REAL,
        avg_hold_hours REAL,
        analysis_json TEXT
    )
    """)

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


def snapshot_recommendations(conn: sqlite3.Connection, run_id: int,
                              recs: list[dict], intermediate_data: dict):
    """
    Insert a recommendation_snapshot for every BUY rec in this run.

    intermediate_data shape (keyed by uppercase symbol):
        {
          "SYM": {
            "feat":       {vol_surge, ret_1, ret_5, dollar_vol, breakout, realized_vol, ...},
            "sec":        {sec_hits: int, sec_trigger_a: int},
            "twits":      float,
            "risk_flags": str,
            "setup_types": {"swing": str, "momentum": str},
          }
        }
    Gate fields are left NULL here; call backfill_snapshot_gate() later.
    Uses INSERT OR IGNORE so re-running the pipeline won't duplicate rows.
    """
    cur = conn.cursor()
    intermediate_data = intermediate_data or {}

    for r in recs:
        if r.get("decision") != "BUY":
            continue

        sym = (r.get("symbol") or "").upper()
        horizon = r.get("horizon", "")
        inter = intermediate_data.get(sym, {})
        feat = inter.get("feat", {})
        sec = inter.get("sec", {})

        cur.execute("""
        INSERT OR IGNORE INTO recommendation_snapshots
        (run_id, symbol, horizon,
         score_total, confidence, risk_score, risk_flags,
         ref_price, stop_price, take_price, suggested_qty,
         setup_type, rationale,
         vol_surge, ret_1, ret_5, dollar_vol, breakout, realized_vol,
         sec_hits, sec_trigger_a, stocktwits_score,
         gate_decision, gate_confidence, gate_reason)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)
        """, (
            run_id,
            sym,
            horizon,
            float(r.get("score_total", 0.0)),
            float(r.get("confidence", 0.0)),
            float(r.get("risk_score", 0.0)),
            inter.get("risk_flags", ""),
            float(r.get("ref_price", 0.0)),
            float(r.get("stop_price", 0.0)),
            float(r.get("take_price", 0.0)),
            int(r.get("suggested_qty", 0)),
            inter.get("setup_types", {}).get(horizon, ""),
            r.get("rationale", ""),
            float(feat.get("vol_surge", 0.0) or 0.0),
            float(feat.get("ret_1", 0.0) or 0.0),
            float(feat.get("ret_5", 0.0) or 0.0),
            float(feat.get("dollar_vol", 0.0) or 0.0),
            int(feat.get("breakout", 0) or 0),
            float(feat.get("realized_vol", 0.0) or 0.0),
            int(sec.get("sec_hits", 0) or 0),
            int(sec.get("sec_trigger_a", 0) or 0),
            float(inter.get("twits", 0.0) or 0.0),
        ))

    conn.commit()


def backfill_snapshot_gate(conn: sqlite3.Connection, run_id: int, symbol: str,
                            gate_decision: str, gate_confidence: float, gate_reason: str):
    """
    Backfill gate result into recommendation_snapshots for a run+symbol pair.
    Called from notify_scan.py after the soft gate runs, for every symbol that triggered it.
    A NULL gate_decision means "gate was never triggered" — this only writes non-NULL values.
    """
    cur = conn.cursor()
    cur.execute("""
        UPDATE recommendation_snapshots
        SET gate_decision = ?, gate_confidence = ?, gate_reason = ?
        WHERE run_id = ? AND UPPER(TRIM(symbol)) = UPPER(TRIM(?))
    """, (gate_decision, float(gate_confidence), gate_reason, run_id, symbol))
    conn.commit()


def insert_open_position(conn: sqlite3.Connection, data: dict) -> int:
    """
    Insert a new open_positions row. Returns the new position_id.
    data keys: snapshot_id, symbol, ibkr_exec_id, ibkr_order_id, oca_group,
               fill_price, fill_qty, commission, commission_source,
               ref_price, stop_price, take_price, opened_at
    """
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO open_positions
    (snapshot_id, symbol, ibkr_exec_id, ibkr_order_id, oca_group,
     fill_price, fill_qty, commission, commission_source,
     ref_price, stop_price, take_price, opened_at, status)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')
    """, (
        data.get("snapshot_id"),
        (data.get("symbol") or "").upper(),
        data.get("ibkr_exec_id"),
        data.get("ibkr_order_id"),
        data.get("oca_group"),
        float(data.get("fill_price", 0.0) or 0.0),
        int(data.get("fill_qty", 0) or 0),
        float(data.get("commission", 0.0) or 0.0),
        data.get("commission_source", "ibkr"),
        float(data.get("ref_price", 0.0) or 0.0),
        float(data.get("stop_price", 0.0) or 0.0),
        float(data.get("take_price", 0.0) or 0.0),
        data.get("opened_at"),
    ))
    conn.commit()
    return int(cur.lastrowid)


def close_position(conn: sqlite3.Connection, position_id: int, exit_data: dict) -> int:
    """
    Mark an open_positions row as closed and insert a closed_trades row.
    Returns the new trade_id.

    exit_data keys: exit_price, exit_qty, exit_reason, gross_pnl, commission_exit,
                    commission_source, net_pnl, outcome, hold_seconds,
                    closed_at, entry_slippage, exit_slippage, ibkr_exec_id_exit
    """
    cur = conn.cursor()

    # Fetch the open position for linked fields
    cur.execute("""
        SELECT snapshot_id, symbol, fill_price, fill_qty, commission,
               commission_source, ref_price, stop_price, take_price,
               opened_at, ibkr_exec_id
        FROM open_positions WHERE position_id = ?
    """, (position_id,))
    row = cur.fetchone()
    if row is None:
        raise ValueError(f"open_positions row not found: position_id={position_id}")

    (snapshot_id, symbol, entry_price, entry_qty, entry_commission,
     commission_source, ref_price, stop_price, take_price,
     opened_at, ibkr_exec_id_entry) = row

    commission_exit = float(exit_data.get("commission_exit", 0.0) or 0.0)
    commission_total = entry_commission + commission_exit
    commission_source_out = exit_data.get("commission_source", commission_source or "ibkr")

    cur.execute("""
    INSERT INTO closed_trades
    (position_id, snapshot_id, symbol,
     entry_price, entry_qty, exit_price, exit_qty,
     exit_reason, gross_pnl, commission_total, commission_source,
     net_pnl, outcome, hold_seconds,
     opened_at, closed_at,
     entry_slippage, exit_slippage,
     ibkr_exec_id_entry, ibkr_exec_id_exit)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        position_id,
        snapshot_id,
        symbol,
        float(entry_price or 0.0),
        int(entry_qty or 0),
        float(exit_data.get("exit_price", 0.0) or 0.0),
        int(exit_data.get("exit_qty", 0) or 0),
        exit_data.get("exit_reason", "manual"),
        float(exit_data.get("gross_pnl", 0.0) or 0.0),
        commission_total,
        commission_source_out,
        float(exit_data.get("net_pnl", 0.0) or 0.0),
        exit_data.get("outcome", "LOSS"),
        int(exit_data.get("hold_seconds", 0) or 0),
        opened_at,
        exit_data.get("closed_at"),
        float(exit_data.get("entry_slippage", 0.0) or 0.0),
        float(exit_data.get("exit_slippage", 0.0) or 0.0),
        ibkr_exec_id_entry,
        exit_data.get("ibkr_exec_id_exit"),
    ))
    trade_id = int(cur.lastrowid)

    cur.execute(
        "UPDATE open_positions SET status = 'closed' WHERE position_id = ?",
        (position_id,)
    )
    conn.commit()
    return trade_id


def get_open_position_by_exec_id(conn: sqlite3.Connection, ibkr_exec_id: str) -> dict | None:
    """Return the open_positions row as a dict, or None if not found."""
    cur = conn.cursor()
    cur.execute("""
        SELECT position_id, snapshot_id, symbol, ibkr_exec_id, ibkr_order_id,
               oca_group, fill_price, fill_qty, commission, commission_source,
               ref_price, stop_price, take_price, opened_at, status
        FROM open_positions WHERE ibkr_exec_id = ?
    """, (ibkr_exec_id,))
    row = cur.fetchone()
    if row is None:
        return None
    keys = ["position_id", "snapshot_id", "symbol", "ibkr_exec_id", "ibkr_order_id",
            "oca_group", "fill_price", "fill_qty", "commission", "commission_source",
            "ref_price", "stop_price", "take_price", "opened_at", "status"]
    return dict(zip(keys, row))


def get_open_positions_all(conn: sqlite3.Connection) -> list[dict]:
    """Return all rows from open_positions where status = 'open'."""
    cur = conn.cursor()
    cur.execute("""
        SELECT position_id, snapshot_id, symbol, ibkr_exec_id, ibkr_order_id,
               oca_group, fill_price, fill_qty, commission, commission_source,
               ref_price, stop_price, take_price, opened_at, status
        FROM open_positions WHERE status = 'open'
        ORDER BY opened_at
    """)
    keys = ["position_id", "snapshot_id", "symbol", "ibkr_exec_id", "ibkr_order_id",
            "oca_group", "fill_price", "fill_qty", "commission", "commission_source",
            "ref_price", "stop_price", "take_price", "opened_at", "status"]
    return [dict(zip(keys, row)) for row in cur.fetchall()]


def get_snapshot_id(conn: sqlite3.Connection, run_id: int, symbol: str,
                    horizon: str) -> int | None:
    """Look up a snapshot_id by run/symbol/horizon. Returns None if not found."""
    cur = conn.cursor()
    cur.execute("""
        SELECT snapshot_id FROM recommendation_snapshots
        WHERE run_id = ? AND UPPER(TRIM(symbol)) = UPPER(TRIM(?)) AND horizon = ?
    """, (run_id, symbol, horizon))
    row = cur.fetchone()
    return row[0] if row else None


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
