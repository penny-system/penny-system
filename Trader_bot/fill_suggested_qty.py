import argparse
import math
import os
import sqlite3
import config


def db_path_resolve(db_path: str | None) -> str:
    path = (
        db_path
        or getattr(config, "DB_PATH", None)
        or os.environ.get("TRADER_DB_PATH")
        or r"output\trader.sqlite"
    )
    path = os.path.normpath(path)
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"DB not found at: {path}\n"
            f"Tip: run from Trader_bot or pass --db-path output\\trader.sqlite"
        )
    return path


def calc_suggested_qty(ref_price: float, purse: float, max_positions: int) -> int:
    # Simple stable sizing to unblock workflow:
    # qty = floor((purse / max_positions) / ref_price)
    if ref_price <= 0 or purse <= 0 or max_positions <= 0:
        return 0
    position_budget = purse / max_positions
    return max(0, int(math.floor(position_budget / ref_price)))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-path", default=None, help="Override DB path (e.g. output\\trader.sqlite)")
    parser.add_argument("--run-id", type=int, default=None, help="Run ID to update (default: latest)")
    parser.add_argument("--only-buy", action="store_true", help="Only fill qty for BUY rows (recommended)")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite even if suggested_qty already > 0")
    args = parser.parse_args()

    purse = float(getattr(config, "PURSE", 0) or 0)
    max_pos = int(getattr(config, "MAX_POSITIONS", 0) or 0)

    db_path = db_path_resolve(args.db_path)
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    # Determine run_id
    if args.run_id is None:
        # Prefer runs.id if it exists; otherwise max(rowid)
        try:
            cur.execute("SELECT MAX(id) FROM runs")
            run_id = int(cur.fetchone()[0] or 0)
        except Exception:
            cur.execute("SELECT MAX(rowid) FROM runs")
            run_id = int(cur.fetchone()[0] or 0)
    else:
        run_id = int(args.run_id)

    if run_id <= 0:
        print("No valid run_id found.")
        con.close()
        return

    # Build WHERE clause
    where = ["run_id = ?"]
    params = [run_id]

    if args.only_buy:
        # robust match: BUY, Buy, BUY␠ etc.
        where.append("UPPER(TRIM(decision)) = 'BUY'")

    if not args.overwrite:
        where.append("COALESCE(suggested_qty, 0) <= 0")

    where_sql = " AND ".join(where)

    # IMPORTANT: alias rowid so sqlite3.Row has a named key
    select_sql = f"""
        SELECT rowid AS rid, symbol, ref_price, suggested_qty
        FROM recommendations
        WHERE {where_sql}
    """

    rows = cur.execute(select_sql, params).fetchall()

    print(f"Using DB: {db_path}")
    print(f"Run ID: {run_id}")
    print(f"PURSE={purse} | MAX_POSITIONS={max_pos}")
    print(f"Rows matched for update: {len(rows)}")

    if not rows:
        print("No rows need updating (already filled, or filter didn’t match).")
        con.close()
        return

    updated = 0
    for r in rows:
        rid = int(r["rid"])
        ref_price = float(r["ref_price"] or 0)
        qty = calc_suggested_qty(ref_price, purse, max_pos)

        cur.execute(
            "UPDATE recommendations SET suggested_qty = ? WHERE rowid = ?",
            (qty, rid),
        )
        updated += 1

    con.commit()
    print(f"Updated {updated} rows.")

    # Preview BUY rows for this run
    preview_sql = """
        SELECT symbol, ref_price, suggested_qty, decision
        FROM recommendations
        WHERE run_id = ?
          AND UPPER(TRIM(decision)) = 'BUY'
        ORDER BY suggested_qty DESC
        LIMIT 10
    """
    preview = cur.execute(preview_sql, (run_id,)).fetchall()
    print("\nPreview BUY rows:")
    for p in preview:
        print(f"{p['symbol']} | ref={float(p['ref_price'] or 0):.2f} | qty={int(p['suggested_qty'] or 0)} | decision={p['decision']}")

    con.close()


if __name__ == "__main__":
    main()