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


def _classify_tier(score: float, confidence: float, risk_score: float) -> str:
    """Classify a recommendation into HIGH / MED / LOW allocation tier."""
    high_score = float(getattr(config, "ALLOC_TIER_HIGH_MIN_SCORE", 87.0))
    high_conf  = float(getattr(config, "ALLOC_TIER_HIGH_MIN_CONF",  0.75))
    high_risk  = float(getattr(config, "ALLOC_TIER_HIGH_MAX_RISK",  0.35))

    med_score  = float(getattr(config, "ALLOC_TIER_MED_MIN_SCORE", 83.0))
    med_conf   = float(getattr(config, "ALLOC_TIER_MED_MIN_CONF",  0.60))
    med_risk   = float(getattr(config, "ALLOC_TIER_MED_MAX_RISK",  0.50))

    if score >= high_score and confidence >= high_conf and risk_score <= high_risk:
        return "HIGH"
    if score >= med_score and confidence >= med_conf and risk_score <= med_risk:
        return "MED"
    return "LOW"


def _get_tier_pct(tier: str) -> float:
    pcts = {
        "HIGH": float(getattr(config, "ALLOC_TIER_HIGH_PCT", 0.25)),
        "MED":  float(getattr(config, "ALLOC_TIER_MED_PCT",  0.18)),
        "LOW":  float(getattr(config, "ALLOC_TIER_LOW_PCT",  0.12)),
    }
    return pcts.get(tier, pcts["LOW"])


def calc_suggested_qty(ref_price: float, purse: float,
                       score: float, confidence: float, risk_score: float) -> int:
    """
    Tier-based qty: each position gets its tier % of the configured purse.
    This is a backfill approximation — does not subtract currently deployed capital.
    """
    if ref_price <= 0 or purse <= 0:
        return 0
    tier = _classify_tier(score, confidence, risk_score)
    tier_pct = _get_tier_pct(tier)
    position_budget = tier_pct * purse
    min_pos = float(getattr(config, "MIN_POSITION_USD", 0.0))
    max_pos = float(getattr(config, "MAX_POSITION_USD", 10**12))
    position_budget = max(min_pos, min(position_budget, max_pos))
    return max(0, int(math.floor(position_budget / ref_price)))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-path", default=None, help="Override DB path (e.g. output\\trader.sqlite)")
    parser.add_argument("--run-id", type=int, default=None, help="Run ID to update (default: latest)")
    parser.add_argument("--only-buy", action="store_true", help="Only fill qty for BUY rows (recommended)")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite even if suggested_qty already > 0")
    args = parser.parse_args()

    purse_cad = float(getattr(config, "TRADE_PURSE_CAD", 0) or 0)
    if purse_cad > 0:
        fx = float(getattr(config, "USD_PER_CAD", 0.73) or 0.73)
        purse = purse_cad * fx
    else:
        purse = float(getattr(config, "PURSE", 0) or getattr(config, "TRADE_PURSE_USD", 0) or 0)

    db_path = db_path_resolve(args.db_path)
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    # Determine run_id
    if args.run_id is None:
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
        where.append("UPPER(TRIM(decision)) = 'BUY'")

    if not args.overwrite:
        where.append("COALESCE(suggested_qty, 0) <= 0")

    where_sql = " AND ".join(where)

    select_sql = f"""
        SELECT rowid AS rid, symbol, ref_price, suggested_qty,
               COALESCE(score_total, 80.0) AS score_total,
               COALESCE(confidence, 0.5)   AS confidence,
               COALESCE(risk_score, 0.5)   AS risk_score
        FROM recommendations
        WHERE {where_sql}
    """

    rows = cur.execute(select_sql, params).fetchall()

    print(f"Using DB: {db_path}")
    print(f"Run ID: {run_id}")
    print(f"PURSE={purse:.2f} (tier-based allocation: HIGH={getattr(config,'ALLOC_TIER_HIGH_PCT',0.25)*100:.0f}% "
          f"MED={getattr(config,'ALLOC_TIER_MED_PCT',0.18)*100:.0f}% "
          f"LOW={getattr(config,'ALLOC_TIER_LOW_PCT',0.12)*100:.0f}%)")
    print(f"Rows matched for update: {len(rows)}")

    if not rows:
        print("No rows need updating (already filled, or filter didn't match).")
        con.close()
        return

    updated = 0
    for r in rows:
        rid        = int(r["rid"])
        ref_price  = float(r["ref_price"] or 0)
        score      = float(r["score_total"] or 80.0)
        confidence = float(r["confidence"] or 0.5)
        risk_score = float(r["risk_score"] or 0.5)
        qty = calc_suggested_qty(ref_price, purse, score, confidence, risk_score)

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
