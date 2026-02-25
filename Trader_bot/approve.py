import argparse
import os
import sqlite3
import config
from src_ibkr_client import connect_ib
from ib_insync import Stock, LimitOrder, StopOrder


def db_connect(db_path: str | None = None):
    """
    DB path resolution priority:
      1) --db-path argument (if provided)
      2) config.DB_PATH (if defined)
      3) env var TRADER_DB_PATH (if defined)
      4) default "output/trader.sqlite" (when running from Trader_bot)
    """
    if db_path:
        path = db_path
    else:
        path = getattr(config, "DB_PATH", None) or os.environ.get("TRADER_DB_PATH") or r"output\trader.sqlite"

    # Normalize for Windows
    path = os.path.normpath(path)

    if not os.path.exists(path):
        raise FileNotFoundError(
            f"DB not found at: {path}\n"
            f"Tip: run from Trader_bot OR pass --db-path output\\trader.sqlite"
        )

    return sqlite3.connect(path)


def get_columns(conn, table):
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    return [r[1] for r in cur.fetchall()]


def pick_col(cols, options):
    for o in options:
        if o in cols:
            return o
    return None


def latest_run_id(conn):
    cols = get_columns(conn, "runs")
    run_key = pick_col(cols, ["run_id", "id", "runId", "run", "rid"])
    if not run_key:
        cur = conn.cursor()
        cur.execute("SELECT MAX(rowid) FROM runs")
        return "rowid", int(cur.fetchone()[0] or 0)

    cur = conn.cursor()
    cur.execute(f"SELECT MAX({run_key}) FROM runs")
    return run_key, int(cur.fetchone()[0] or 0)


def load_buy_recs(conn, run_id):
    table = "recommendations"
    cols = get_columns(conn, table)

    # Required columns
    symbol_col = pick_col(cols, ["symbol", "ticker"])
    decision_col = pick_col(cols, ["decision", "action"])
    run_col = pick_col(cols, ["run_id", "runId", "run", "rid"])

    ref_col = pick_col(cols, ["ref_price", "ref", "price"])
    stop_col = pick_col(cols, ["stop_price", "stop"])
    take_col = pick_col(cols, ["take_price", "take"])

    score_col = pick_col(cols, ["score_total", "score"])
    conf_col = pick_col(cols, ["confidence", "conf"])
    risk_col = pick_col(cols, ["risk_score", "risk"])

    # Qty column can vary; auto-detect
    qty_col = pick_col(cols, ["suggested_qty", "suggestedQuantity", "qty", "quantity", "order_qty"])

    missing = []
    for name, col in [
        ("symbol", symbol_col),
        ("decision", decision_col),
        ("ref_price", ref_col),
        ("stop_price", stop_col),
        ("take_price", take_col),
    ]:
        if col is None:
            missing.append(name)

    if missing:
        raise RuntimeError(
            f"recommendations table missing required columns: {missing}\n"
            f"Available columns: {cols}"
        )

    # Build SELECT list safely
    select_cols = [
        f"{symbol_col} AS symbol",
        f"{ref_col} AS ref_price",
        f"{stop_col} AS stop_price",
        f"{take_col} AS take_price",
        f"COALESCE({score_col}, 0) AS score_total" if score_col else "0 AS score_total",
        f"COALESCE({conf_col}, 0) AS confidence" if conf_col else "0 AS confidence",
        f"COALESCE({risk_col}, 0) AS risk_score" if risk_col else "0 AS risk_score",
        f"COALESCE({qty_col}, 0) AS suggested_qty" if qty_col else "0 AS suggested_qty",
    ]

    sql = f"SELECT {', '.join(select_cols)} FROM {table} WHERE {decision_col}='BUY'"
    params = []

    if run_col:
        sql += f" AND {run_col}=?"
        params.append(run_id)

    sql += " ORDER BY score_total DESC, confidence DESC LIMIT 25"

    cur = conn.cursor()
    cur.execute(sql, params)
    rows = cur.fetchall()

    recs = []
    for r in rows:
        recs.append({
            "symbol": str(r[0]).upper(),
            "ref_price": float(r[1]),
            "stop_price": float(r[2]),
            "take_price": float(r[3]),
            "score_total": float(r[4]),
            "confidence": float(r[5]),
            "risk_score": float(r[6]),
            "suggested_qty": int(r[7]),
        })

    return recs, cols, qty_col


def place_bracket(ib, symbol, qty, entry, take, stop, dry_run: bool = False):
    """
    Places a bracket order:
      Parent: BUY Limit
      Child 1: SELL Limit (take profit)
      Child 2: SELL Stop  (stop loss) [transmit=True]
    """
    if dry_run:
        return {"dry_run": True, "symbol": symbol, "qty": qty, "entry": entry, "take": take, "stop": stop}

    contract = Stock(symbol, "SMART", "USD")
    ib.qualifyContracts(contract)

    parent = LimitOrder("BUY", qty, round(entry, 2), transmit=False)
    take_o = LimitOrder("SELL", qty, round(take, 2), parentId=0, transmit=False)
    stop_o = StopOrder("SELL", qty, round(stop, 2), parentId=0, transmit=True)

    ib.placeOrder(contract, parent)
    ib.sleep(0.5)

    parent_id = parent.orderId
    if not parent_id:
        raise RuntimeError("Parent orderId not assigned. Check TWS permissions/paper account.")

    take_o.parentId = parent_id
    stop_o.parentId = parent_id

    ib.placeOrder(contract, take_o)
    ib.placeOrder(contract, stop_o)
    ib.sleep(1)  # flush child orders to TWS before caller disconnects

    return {"parentId": parent_id, "qty": qty, "entry": entry, "take": take, "stop": stop}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-path", default=None, help="Override DB path (e.g. output\\trader.sqlite)")
    parser.add_argument("--dry-run", action="store_true", help="Do not place orders; only print what would happen")
    args = parser.parse_args()

    conn = db_connect(args.db_path)
    print(f"Using DB: {os.path.normpath(args.db_path or getattr(config, 'DB_PATH', None) or os.environ.get('TRADER_DB_PATH') or r'output\\trader.sqlite')}")

    _, run_id = latest_run_id(conn)
    if run_id <= 0:
        print("No runs found.")
        conn.close()
        return

    recs, cols, qty_col = load_buy_recs(conn, run_id)

    print(f"\nLatest run: {run_id}")
    print("BUY RECOMMENDATIONS:\n")
    for r in recs:
        print(
            f"{r['symbol']} | score={r['score_total']:.1f} "
            f"conf={r['confidence']:.2f} risk={r['risk_score']:.1f} "
            f"ref={r['ref_price']:.2f} stop={r['stop_price']:.2f} take={r['take_price']:.2f} "
            f"qty={r['suggested_qty']}"
        )

    if qty_col is None:
        print("\nNOTE: No qty column found in DB, so you'll be prompted for qty on approve.")
    else:
        print(f"\nNOTE: Using qty column: {qty_col}")

    if args.dry_run:
        print("\nNOTE: Running in DRY-RUN mode (no orders will be placed).")

    print("\nCommands:")
    print("  APPROVE <SYMBOL>")
    print("  REJECT <SYMBOL>")
    print("  EXIT\n")

    ib = None

    while True:
        cmd = input("> ").strip().upper()
        if cmd == "EXIT":
            break

        parts = cmd.split()
        if len(parts) != 2:
            print("Use: APPROVE <SYMBOL>  |  REJECT <SYMBOL>  |  EXIT")
            continue

        action, symbol = parts
        match = next((r for r in recs if r["symbol"] == symbol), None)

        if not match:
            print("Symbol not in BUY list.")
            continue

        if action == "REJECT":
            print(f"Rejected {symbol}")
            continue

        if action != "APPROVE":
            print("Unknown command.")
            continue

        qty = int(match["suggested_qty"] or 0)
        if qty <= 0:
            try:
                qty = int(input(f"Enter qty for {symbol} (or 0 to cancel): ").strip())
            except ValueError:
                print("Invalid qty.")
                continue

        if qty <= 0:
            print("Cancelled.")
            continue

        if ib is None and not args.dry_run:
            ib = connect_ib()

        result = place_bracket(
            ib,
            symbol,
            qty,
            match["ref_price"],
            match["take_price"],
            match["stop_price"],
            dry_run=args.dry_run
        )
        print(f"Placed bracket for {symbol}: {result}")

    if ib:
        ib.disconnect()

    conn.close()


if __name__ == "__main__":
    main()