# main_daily_run.py
import os
from datetime import datetime
import inspect

import config
from ib_insync import Stock

from src_ibkr_client import connect_ib_with_retry
from src_storage import connect_db, ensure_schema, create_run, insert_recommendations

import src_universe
import src_features
import src_scoring
import src_sizing
import src_risk


# ----------------------------
# Helpers
# ----------------------------

def call_first_existing(module, fn_names, *args, **kwargs):
    for name in fn_names:
        fn = getattr(module, name, None)
        if callable(fn):
            return fn(*args, **kwargs)

    available = [x for x in dir(module) if not x.startswith("_")]
    raise ImportError(
        f"None of these functions exist in {module.__name__}: {fn_names}\n"
        f"Available: {available}"
    )


def normalize_recs(recs):
    """
    Ensure recs is a list[dict]. If a stray string appears, convert to dict.
    """
    out = []
    for r in recs or []:
        if isinstance(r, dict):
            rr = dict(r)
            rr.setdefault("symbol", "")
            rr.setdefault("horizon", "")
            rr.setdefault("decision", "WATCH")
            rr.setdefault("score_total", 0.0)
            rr.setdefault("confidence", 0.0)
            rr.setdefault("risk_score", 0.0)
            rr.setdefault("ref_price", 0.0)
            rr.setdefault("stop_price", 0.0)
            rr.setdefault("take_price", 0.0)
            rr.setdefault("rationale", "")
            rr.setdefault("suggested_qty", 0)
            out.append(rr)
        elif isinstance(r, str):
            out.append({
                "symbol": r.upper(),
                "horizon": "",
                "decision": "WATCH",
                "score_total": 0.0,
                "confidence": 0.0,
                "risk_score": 0.0,
                "ref_price": 0.0,
                "stop_price": 0.0,
                "take_price": 0.0,
                "rationale": "normalized_from_string",
                "suggested_qty": 0
            })
        else:
            continue
    return out


# ----------------------------
# Universe
# ----------------------------

def build_universe(ib):
    static_syms = call_first_existing(
        src_universe,
        [
            "load_static_universe",
            "load_universe_static",
            "read_static_universe",
            "get_static_universe",
            "load_static_universe_list",
        ],
    )

    dyn_syms = []
    try:
        dyn_syms = call_first_existing(
            src_universe,
            ["dynamic_scan_symbols", "scan_symbols", "run_scanners", "get_dynamic_universe"],
            ib,
        )
    except Exception:
        dyn_syms = []

    universe = list(dict.fromkeys([s.upper() for s in (static_syms + dyn_syms) if s]))
    return universe


# ----------------------------
# Features
# ----------------------------

def fetch_bars_for_symbol(ib, symbol: str):
    contract = Stock(symbol, "SMART", "USD")
    ib.qualifyContracts(contract)

    bars = ib.reqHistoricalData(
        contract,
        endDateTime="",
        durationStr="1 D",
        barSizeSetting="1 min",
        whatToShow="TRADES",
        useRTH=True,
        formatDate=1,
    )
    return bars


def build_features(ib, symbols):
    feat_fn = getattr(src_features, "compute_features_from_bars", None)
    if not callable(feat_fn):
        raise ImportError("Expected src_features.compute_features_from_bars(bars) but it was not found.")

    feats_by_symbol = {}
    for sym in symbols:
        try:
            bars = fetch_bars_for_symbol(ib, sym)
            if not bars:
                continue
            feats = feat_fn(bars)
            if isinstance(feats, dict):
                feats["symbol"] = sym
                feats_by_symbol[sym] = feats
        except Exception:
            continue

    return feats_by_symbol


# ----------------------------
# Scoring
# ----------------------------

def score_all_candidates(feats_by_symbol):
    score_fn = getattr(src_scoring, "score_candidate", None)
    if not callable(score_fn):
        raise ImportError("Expected src_scoring.score_candidate but it was not found.")

    recs = []
    for sym, feat in feats_by_symbol.items():
        for horizon in ["swing", "momentum"]:
            try:
                out = score_fn(feat, horizon)
            except TypeError:
                out = score_fn(feat)

            if out is None:
                continue

            if isinstance(out, dict):
                r = dict(out)
                r.setdefault("symbol", sym)
                r.setdefault("horizon", horizon)
                r.setdefault("decision", "WATCH")
                r.setdefault("score_total", 0.0)
                r.setdefault("confidence", 0.0)
                recs.append(r)

            elif isinstance(out, (list, tuple)):
                score_total = float(out[0]) if len(out) > 0 else 0.0
                confidence = float(out[1]) if len(out) > 1 else 0.0
                setup_type = str(out[2]) if len(out) > 2 else "unknown"
                rationale = str(out[3]) if len(out) > 3 else ""

                ref = float(feat.get("ref_price", feat.get("close", 0.0)) or 0.0)

                recs.append({
                    "symbol": sym,
                    "horizon": horizon,
                    "decision": "BUY" if score_total >= 80 else "WATCH",
                    "score_total": score_total,
                    "confidence": confidence,
                    "risk_score": src_risk.risk_score_and_flags(sym, feat)[0],
                    "ref_price": ref,
                    "stop_price": round(ref * (1.0 - float(config.STOP_LOSS_PCT)), 2) if ref else 0.0,
                    "take_price": round(ref * (1.0 + float(config.TAKE_PROFIT_PCT)), 2) if ref else 0.0,
                    "rationale": f"{setup_type} | {rationale}".strip(),
                })

    return normalize_recs(recs)


# ----------------------------
# Sizing (optional)
# ----------------------------

def apply_sizing(recs):
    recs = normalize_recs(recs)

    for r in recs:
        r["suggested_qty"] = 0

    buy_recs = [r for r in recs if r.get("decision") == "BUY"]
    if not buy_recs:
        return recs

    size_fn = getattr(src_sizing, "size_recommendations", None)
    if not callable(size_fn):
        return recs

    try:
        sized = size_fn(buy_recs)
    except Exception:
        return recs

    sized = normalize_recs(sized)
    sized_map = {(r["symbol"].upper(), r.get("horizon", "")): int(r.get("suggested_qty", 0)) for r in sized}

    for r in recs:
        key = (r["symbol"].upper(), r.get("horizon", ""))
        r["suggested_qty"] = int(sized_map.get(key, 0)) if r.get("decision") == "BUY" else 0

    return recs


# ----------------------------
# Write brief (we own this now)
# ----------------------------

def write_brief_file(run_id, recs):
    os.makedirs("output", exist_ok=True)
    path = os.path.join("output", "brief.txt")

    recs = normalize_recs(recs)

    # Rank for display: BUY first, then WATCH; higher score first
    def sort_key(r):
        decision_rank = 0 if r.get("decision") == "BUY" else 1
        return (decision_rank, -float(r.get("score_total", 0.0)))

    recs_sorted = sorted(recs, key=sort_key)

    buy = [r for r in recs_sorted if r.get("decision") == "BUY"]
    watch = [r for r in recs_sorted if r.get("decision") != "BUY"]

    lines = []
    lines.append(f"RUN {run_id} — Morning Brief")
    lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")
    lines.append("BUY CANDIDATES (need your approval)")
    if not buy:
        lines.append("- none today.")
    else:
        for r in buy[:10]:
            sym = (r.get("symbol") or "").upper()
            hor = r.get("horizon", "")
            score = r.get("score_total", 0.0)
            conf = r.get("confidence", 0.0)
            risk = r.get("risk_score", 0.0)
            ref = r.get("ref_price", 0.0)
            stop = r.get("stop_price", 0.0)
            take = r.get("take_price", 0.0)
            qty = r.get("suggested_qty", 0)
            why = r.get("rationale", "")
            lines.append(f"- {sym} [{hor}] score={score:.1f} conf={conf:.2f} risk={risk:.1f} ref={ref:.2f} stop={stop:.2f} take={take:.2f} qty={qty}")
            if why:
                lines.append(f"  why: {why}")

    lines.append("")
    lines.append("WATCHLIST (monitor, no action)")
    if not watch:
        lines.append("- none.")
    else:
        for r in watch[:15]:
            sym = (r.get("symbol") or "").upper()
            hor = r.get("horizon", "")
            score = r.get("score_total", 0.0)
            conf = r.get("confidence", 0.0)
            risk = r.get("risk_score", 0.0)
            ref = r.get("ref_price", 0.0)
            why = r.get("rationale", "")
            lines.append(f"- {sym} [{hor}] score={score:.1f} conf={conf:.2f} risk={risk:.1f} ref={ref:.2f}")
            if why:
                lines.append(f"  why: {why}")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return path


# ----------------------------
# Main
# ----------------------------

def main():
    ib = connect_ib_with_retry()

    symbols = build_universe(ib)
    print(f"Universe size (symbols): {len(symbols)}")

    feats_by_symbol = build_features(ib, symbols)
    recs = score_all_candidates(feats_by_symbol)
    recs = apply_sizing(recs)

    conn = connect_db()
    ensure_schema(conn)
    run_id = create_run(conn)
    insert_recommendations(conn, run_id, recs)
    conn.close()

    brief_path = write_brief_file(run_id, recs)

    print(f"Run created: {run_id} ({datetime.now().date()})")
    print(f"Saved brief: {brief_path}")
    print("Next: run approve.py to approve BUY recs and place bracket orders.")

    ib.disconnect()


if __name__ == "__main__":
    main()
