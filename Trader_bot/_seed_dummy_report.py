"""
Seed dummy trade data, generate the HTML report, then open in browser.
Run: python _seed_dummy_report.py
Run: python _seed_dummy_report.py --cleanup   (remove dummy data)
"""
import os, sys, time, webbrowser
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src_storage import connect_db, ensure_schema
from src_report import generate_report

# Use a timestamp suffix so every run generates unique exec IDs
_TS = str(int(time.time()))

conn = connect_db()
ensure_schema(conn)
conn.rollback()   # clear any uncommitted state from a previous failed run
cur = conn.cursor()

if "--cleanup" in sys.argv:
    cur.execute(f"DELETE FROM closed_trades WHERE ibkr_exec_id_entry LIKE 'DE{_TS}%' OR ibkr_exec_id_exit LIKE 'DX%'")
    # Broader cleanup: remove ALL dummy rows by marker columns
    cur.execute("DELETE FROM closed_trades WHERE ibkr_exec_id_entry LIKE 'DE%'")
    cur.execute("DELETE FROM open_positions WHERE ibkr_exec_id LIKE 'DO_%'")
    cur.execute("DELETE FROM recommendation_snapshots WHERE purse_usd = 2190.0")
    cur.execute("DELETE FROM runs WHERE created_at = '2026-01-02T09:30:00'")
    cur.execute("DELETE FROM learning_snapshots")
    conn.commit()
    for tbl in ["closed_trades", "open_positions", "recommendation_snapshots"]:
        cur.execute(f"SELECT COUNT(*) FROM {tbl}")
        print(f"{tbl}: {cur.fetchone()[0]}")
    conn.close()
    print("Cleaned up.")
    sys.exit(0)

# ── 1. Dummy run ──────────────────────────────────────────────────────────────
cur.execute("INSERT INTO runs (created_at) VALUES ('2026-01-02T09:30:00')")
run_id = cur.lastrowid

# ── 2. Recommendation snapshots ───────────────────────────────────────────────
snapshots = [
    # sym,   horizon,    score, conf, risk,  vol,  ret5,  brk, setup,          gate,     dec
    ("SOXL", "swing",    88.5,  0.82, 0.18,  3.2,  0.08,  1,  "breakout_vol", "APPROVE","BUY"),
    ("NVAX", "momentum", 81.0,  0.65, 0.35,  5.8,  0.12,  0,  "vol_surge",    "APPROVE","BUY"),
    ("CTRM", "swing",    92.1,  0.88, 0.15,  4.1,  0.22,  1,  "breakout_vol", "APPROVE","BUY"),
    ("BNGO", "momentum", 80.5,  0.61, 0.42,  7.2,  0.06,  0,  "vol_surge",    "HOLD",   "BUY"),
    ("RIDE", "swing",    85.3,  0.79, 0.22,  2.9,  0.15,  1,  "breakout_vol", "APPROVE","BUY"),
    ("WKHS", "momentum", 83.7,  0.71, 0.28,  4.6,  0.09,  1,  "breakout_vol", "APPROVE","BUY"),
    ("IDEX", "swing",    80.2,  0.62, 0.38,  6.1,  0.04,  0,  "vol_surge",    "APPROVE","BUY"),
    ("HOFV", "momentum", 87.4,  0.83, 0.19,  3.4,  0.11,  1,  "breakout_vol", "APPROVE","BUY"),
    ("MMAT", "swing",    84.9,  0.76, 0.24,  2.7,  0.18,  1,  "breakout_vol", "APPROVE","BUY"),
    ("NAKD", "momentum", 81.8,  0.68, 0.31,  5.3,  0.07,  0,  "vol_surge",    "APPROVE","BUY"),
    ("EXPR", "swing",    90.3,  0.91, 0.14,  3.8,  0.25,  1,  "breakout_vol", "APPROVE","BUY"),
    ("BBIG", "momentum", 82.6,  0.72, 0.26,  4.2,  0.10,  1,  "breakout_vol", "APPROVE","BUY"),
    ("PHUN", "swing",    86.1,  0.80, 0.21,  3.1,  0.14,  1,  "breakout_vol", "APPROVE","BUY"),
    ("ILUS", "momentum", 80.8,  0.64, 0.37,  6.5,  0.05,  0,  "vol_surge",    "BLOCK",  "BUY"),
    ("CENN", "swing",    88.9,  0.85, 0.17,  3.6,  0.19,  1,  "breakout_vol", "APPROVE","BUY"),
]
snap_ids = {}
for s in snapshots:
    cur.execute("""
        INSERT INTO recommendation_snapshots
        (run_id, symbol, horizon, score_total, confidence, risk_score,
         vol_surge, ret_5, breakout, setup_type, gate_decision, decision,
         snapshot_at, purse_usd, max_positions)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (run_id, s[0], s[1], s[2], s[3], s[4], s[5], s[6], s[7],
          s[8], s[9], s[10], "2026-01-02T09:35:00", 2190.0, 10))
    snap_ids[s[0]] = cur.lastrowid

# ── 3. Closed trades (15 trades: first 7 = 3W 4L, last 8 = 6W 2L) ───────────
trades = [
    ("SOXL","2026-01-03", 2.10, 2.31, 400,  84.0, 1.40,  82.60,  3.93, "WIN",  "take_profit", 3600, 1.00,  0.002, -0.003),
    ("NVAX","2026-01-05", 1.55, 1.41, 300, -42.0, 1.05, -43.05, -2.71, "LOSS", "stop_loss",   1800, 0.50,  0.003,  0.005),
    ("CTRM","2026-01-08", 3.20, 2.89, 200, -62.0, 0.70, -62.70, -1.94, "LOSS", "stop_loss",   2700, 0.75,  0.004,  0.006),
    ("BNGO","2026-01-10", 1.80, 1.98, 500,  90.0, 1.75,  88.25,  2.50, "WIN",  "take_profit", 5400, 1.50,  0.001, -0.002),
    ("RIDE","2026-01-14", 2.50, 2.27, 250, -57.5, 0.88, -58.38, -2.30, "LOSS", "stop_loss",    900, 0.25,  0.003,  0.004),
    ("WKHS","2026-01-17", 1.95, 1.76, 400, -76.0, 1.40, -77.40, -1.95, "LOSS", "manual",      7200, 2.00,  0.002,  0.000),
    ("IDEX","2026-01-20", 1.25, 1.38, 600,  78.0, 2.10,  75.90,  2.60, "WIN",  "take_profit", 4500, 1.25,  0.002, -0.004),
    ("HOFV","2026-01-23", 2.80, 3.08, 300,  84.0, 1.05,  82.95,  3.00, "WIN",  "take_profit", 3600, 1.00,  0.001, -0.002),
    ("MMAT","2026-01-27", 1.60, 1.45, 450, -67.5, 1.58, -69.08, -2.10, "LOSS", "stop_loss",   1200, 0.33,  0.003,  0.005),
    ("NAKD","2026-01-29", 1.10, 1.21, 700,  77.0, 2.45,  74.55,  3.00, "WIN",  "take_profit", 6300, 1.75,  0.001, -0.002),
    ("EXPR","2026-02-03", 3.50, 3.85, 200,  70.0, 0.70,  69.30,  2.00, "WIN",  "take_profit", 4800, 1.33,  0.002, -0.003),
    ("BBIG","2026-02-07", 2.20, 1.98, 350, -77.0, 1.23, -78.23, -2.21, "LOSS", "stop_loss",    600, 0.17,  0.004,  0.006),
    ("PHUN","2026-02-11", 1.75, 1.93, 500,  90.0, 1.75,  88.25,  2.57, "WIN",  "take_profit", 5100, 1.42,  0.001, -0.002),
    ("ILUS","2026-02-18", 1.40, 1.54, 600,  84.0, 2.10,  81.90,  3.00, "WIN",  "take_profit", 3900, 1.08,  0.002, -0.003),
    ("CENN","2026-02-24", 2.60, 2.86, 300,  78.0, 1.05,  76.95,  3.00, "WIN",  "take_profit", 4200, 1.17,  0.001, -0.002),
]

for i, (sym, date, entry, exit_p, qty, gross, fees, net, pct,
         outcome, reason, hold_s, hold_h, slip_e, slip_x) in enumerate(trades):
    h = hold_s // 3600
    m = (hold_s % 3600) // 60
    opened = f"{date}T09:30:00"
    closed = f"{date}T{9+h:02d}:{m:02d}:00"
    # Use timestamp prefix to guarantee uniqueness across runs
    ex_entry = f"DE{_TS}_{i+1:03d}"
    ex_exit  = f"DX{_TS}_{i+1:03d}"
    cur.execute("""
        INSERT INTO closed_trades
        (snapshot_id, symbol, entry_price, exit_price, entry_qty, exit_qty,
         gross_pnl, commission_total, net_pnl, pnl_pct,
         outcome, exit_reason, hold_seconds, hold_duration_hours,
         opened_at, closed_at, ibkr_exec_id_entry, ibkr_exec_id_exit,
         entry_slippage, exit_slippage)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (snap_ids.get(sym), sym, entry, exit_p, qty, qty,
          gross, fees, net, pct, outcome, reason, hold_s, hold_h,
          opened, closed, ex_entry, ex_exit, slip_e, slip_x))

# ── 4. Two open positions ─────────────────────────────────────────────────────
for sym, fp, qty, ref, stop, take, opened in [
    ("SOXL", 2.15, 400, 2.14, 1.97, 2.37, "2026-02-28T10:15:00"),
    ("CTRM", 3.30, 200, 3.28, 3.00, 3.62, "2026-02-28T11:00:00"),
]:
    cur.execute("""
        INSERT INTO open_positions
        (symbol, fill_price, fill_qty, commission, commission_source,
         ref_price, stop_price, take_price, opened_at, status,
         ibkr_exec_id, ibkr_order_id)
        VALUES (?,?,?,?,?,?,?,?,?,'open',?,?)
    """, (sym, fp, qty, round(0.0035 * qty, 2), "estimated",
          ref, stop, take, opened, f"DO_{_TS}_{sym}", f"ORD_{sym}"))

conn.commit()

# ── 5. Generate report ────────────────────────────────────────────────────────
path = generate_report(conn)
conn.close()

print(f"Report: {path}")
webbrowser.open(f"file:///{path.replace(chr(92), '/')}")
print("Opened in browser.")
print()
print(f"To clean up: python _seed_dummy_report.py --cleanup")
