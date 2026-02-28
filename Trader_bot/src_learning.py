# src_learning.py
#
# Learning engine: analyzes closed_trades + recommendation_snapshots to produce
# signal analysis, combination analysis, temporal analysis, and slippage analysis.
#
# Does NOT auto-tune anything. Observes, measures, and reports. You decide.
#
# Usage:
#   from src_learning import run_learning_analysis
#   analysis = run_learning_analysis(conn)   # conn = sqlite3 connection
#   # analysis is a dict — pass to src_report.generate_report() or /performance
#
import os
import sys
import sqlite3
import json
from datetime import datetime

_BOT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _BOT_DIR)

import config


# ---------------------------------------------------------------------------
# Confidence tier helpers
# ---------------------------------------------------------------------------

_MIN_SAMPLE      = int(getattr(config, "LEARNING_MIN_SAMPLE",      5))
_LOW_SAMPLE      = int(getattr(config, "LEARNING_LOW_SAMPLE",     14))
_MODERATE_SAMPLE = int(getattr(config, "LEARNING_MODERATE_SAMPLE", 29))


def _confidence_tier(n: int) -> str:
    if n < _MIN_SAMPLE:
        return "insufficient"
    if n <= _LOW_SAMPLE:
        return "preliminary"
    if n <= _MODERATE_SAMPLE:
        return "emerging"
    return "reliable"


def _confidence_note(n: int) -> str:
    tier = _confidence_tier(n)
    if tier == "insufficient":
        return f"(N={n} — insufficient data, suppressed)"
    if tier == "preliminary":
        return f"(N={n} — early data, treat with caution)"
    if tier == "emerging":
        return f"(N={n} — emerging pattern)"
    return f"(N={n})"


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _safe_div(num, denom, default=0.0):
    try:
        return num / denom if denom else default
    except Exception:
        return default


def _rows_to_dicts(cur) -> list[dict]:
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _load_closed_trades(conn: sqlite3.Connection) -> list[dict]:
    cur = conn.cursor()
    cur.execute("""
        SELECT ct.trade_id, ct.symbol, ct.entry_price, ct.entry_qty,
               ct.exit_price, ct.exit_qty, ct.exit_reason,
               ct.gross_pnl, ct.net_pnl, ct.outcome,
               ct.hold_seconds, ct.opened_at, ct.closed_at,
               ct.entry_slippage, ct.exit_slippage,
               ct.commission_total, ct.commission_source,
               rs.score_total, rs.confidence, rs.risk_score,
               rs.vol_surge, rs.ret_1, rs.ret_5, rs.dollar_vol,
               rs.breakout, rs.realized_vol, rs.sec_hits, rs.sec_trigger_a,
               rs.stocktwits_score, rs.setup_type, rs.gate_decision,
               rs.gate_confidence, rs.ref_price AS snap_ref_price
        FROM closed_trades ct
        LEFT JOIN recommendation_snapshots rs
               ON ct.snapshot_id = rs.snapshot_id
        ORDER BY ct.trade_id ASC
    """)
    return _rows_to_dicts(cur)


# ---------------------------------------------------------------------------
# Layer 1: Signal Analysis (per-feature bucket analysis)
# ---------------------------------------------------------------------------

def _bucket_stats(trades: list[dict], feature: str, buckets: list[tuple]) -> list[dict]:
    """
    buckets = [(label, low, high), ...]  — inclusive on both ends for reals,
    or [(label, value, value), ...] for discrete (pass same value twice).
    Returns a list of bucket dicts with N, win_rate, avg_net_pnl, confidence.
    """
    results = []
    for label, low, high in buckets:
        subset = []
        for t in trades:
            val = t.get(feature)
            if val is None:
                continue
            try:
                v = float(val)
            except (TypeError, ValueError):
                continue
            if low <= v <= high:
                subset.append(t)

        n        = len(subset)
        wins     = sum(1 for t in subset if t.get("outcome") == "WIN")
        win_rate = _safe_div(wins, n)
        avg_pnl  = _safe_div(sum(t.get("net_pnl", 0.0) or 0.0 for t in subset), n)
        tier     = _confidence_tier(n)

        if tier == "insufficient":
            continue   # suppress low-N buckets

        results.append({
            "label":    label,
            "n":        n,
            "win_rate": win_rate,
            "avg_pnl":  avg_pnl,
            "tier":     tier,
        })
    return results


def _signal_analysis(trades: list[dict]) -> dict:
    """Bucket analysis per feature vs win rate / avg P&L."""
    out = {}

    # Volume surge
    out["vol_surge"] = _bucket_stats(trades, "vol_surge", [
        ("1.0–2.0x",  1.0,  2.0),
        ("2.0–3.0x",  2.0,  3.0),
        ("3.0–5.0x",  3.0,  5.0),
        ("5.0x+",     5.0, 9999.0),
    ])

    # Score total
    out["score_total"] = _bucket_stats(trades, "score_total", [
        ("80–84",  80.0,  84.9),
        ("85–89",  85.0,  89.9),
        ("90–94",  90.0,  94.9),
        ("95–100", 95.0, 100.0),
    ])

    # Confidence
    out["confidence"] = _bucket_stats(trades, "confidence", [
        ("<0.60",    0.0,  0.599),
        ("0.60–0.69", 0.60, 0.699),
        ("0.70–0.79", 0.70, 0.799),
        ("0.80+",    0.80, 1.0),
    ])

    # Risk score (0–1 scale)
    out["risk_score"] = _bucket_stats(trades, "risk_score", [
        ("Low (<0.20)",    0.0,  0.199),
        ("Mod (0.20–0.35)", 0.20, 0.35),
        ("High (0.35+)",   0.35, 1.0),
    ])

    # 5-day return at entry (ret_5)
    out["ret_5"] = _bucket_stats(trades, "ret_5", [
        ("0–5%",   0.0,  0.05),
        ("5–15%",  0.05, 0.15),
        ("15–30%", 0.15, 0.30),
        ("30%+",   0.30, 9999.0),
    ])

    # Breakout flag (0 vs 1)
    out["breakout"] = _bucket_stats(trades, "breakout", [
        ("No breakout", 0.0, 0.0),
        ("Breakout",    1.0, 1.0),
    ])

    # Setup type (text) — handle separately
    setup_counts: dict[str, dict] = {}
    for t in trades:
        st = (t.get("setup_type") or "unknown").strip()
        if st not in setup_counts:
            setup_counts[st] = {"wins": 0, "total": 0, "pnl": 0.0}
        setup_counts[st]["total"] += 1
        if t.get("outcome") == "WIN":
            setup_counts[st]["wins"] += 1
        setup_counts[st]["pnl"] += float(t.get("net_pnl", 0.0) or 0.0)

    setup_rows = []
    for st, d in setup_counts.items():
        n = d["total"]
        if _confidence_tier(n) == "insufficient":
            continue
        setup_rows.append({
            "label":    st,
            "n":        n,
            "win_rate": _safe_div(d["wins"], n),
            "avg_pnl":  _safe_div(d["pnl"], n),
            "tier":     _confidence_tier(n),
        })
    out["setup_type"] = sorted(setup_rows, key=lambda r: -r["n"])

    return out


# ---------------------------------------------------------------------------
# Layer 2: Combination Analysis
# ---------------------------------------------------------------------------

def _combo_stats(trades: list[dict], label: str, predicate) -> dict | None:
    """
    predicate(trade_dict) -> bool
    Returns a dict with n, win_rate, avg_pnl, tier, or None if insufficient.
    """
    subset = [t for t in trades if predicate(t)]
    n      = len(subset)
    if _confidence_tier(n) == "insufficient":
        return None
    wins    = sum(1 for t in subset if t.get("outcome") == "WIN")
    avg_pnl = _safe_div(sum(t.get("net_pnl", 0.0) or 0.0 for t in subset), n)
    return {
        "label":    label,
        "n":        n,
        "win_rate": _safe_div(wins, n),
        "avg_pnl":  avg_pnl,
        "tier":     _confidence_tier(n),
    }


def _combination_analysis(trades: list[dict]) -> dict:
    combos = []

    # breakout + gate
    combos.append(_combo_stats(trades, "breakout=1 + gate=APPROVE",
        lambda t: t.get("breakout") == 1 and t.get("gate_decision") == "APPROVE"))

    # breakout + vol_surge 2-3x
    combos.append(_combo_stats(trades, "breakout=1 + vol_surge 2-3x",
        lambda t: t.get("breakout") == 1 and 2.0 <= (t.get("vol_surge") or 0) <= 3.0))

    # high confidence + low risk
    combos.append(_combo_stats(trades, "confidence>=0.70 + risk<0.25",
        lambda t: (t.get("confidence") or 0) >= 0.70 and (t.get("risk_score") or 0) < 0.25))

    # vol_surge extreme + high risk
    combos.append(_combo_stats(trades, "vol_surge>5x + risk>=0.40",
        lambda t: (t.get("vol_surge") or 0) > 5.0 and (t.get("risk_score") or 0) >= 0.40))

    # breakout + sec_trigger_a
    combos.append(_combo_stats(trades, "breakout=1 + sec_trigger_a=1",
        lambda t: t.get("breakout") == 1 and t.get("sec_trigger_a") == 1))

    # low confidence + high risk
    combos.append(_combo_stats(trades, "confidence<0.60 + risk>=0.30",
        lambda t: (t.get("confidence") or 0) < 0.60 and (t.get("risk_score") or 0) >= 0.30))

    # gate_decision = BLOCK or HOLD → did gate protect us?
    combos.append(_combo_stats(trades, "gate=BLOCK or HOLD (would-be)",
        lambda t: t.get("gate_decision") in ("BLOCK", "HOLD")))

    valid = [c for c in combos if c is not None]
    valid.sort(key=lambda c: -c["win_rate"])

    n_valid = len(valid)
    best  = valid[:3]          if n_valid >= 1 else []
    worst = valid[-3:][::-1]   if n_valid >= 3 else []

    return {"all": valid, "best": best, "worst": worst}


# ---------------------------------------------------------------------------
# Layer 3: Temporal Analysis
# ---------------------------------------------------------------------------

def _temporal_analysis(trades: list[dict]) -> dict:
    n = len(trades)
    if n == 0:
        return {"total": 0}

    def _stats(subset):
        if not subset:
            return {"n": 0, "win_rate": 0.0, "net_pnl": 0.0}
        wins = sum(1 for t in subset if t.get("outcome") == "WIN")
        return {
            "n":        len(subset),
            "win_rate": _safe_div(wins, len(subset)),
            "net_pnl":  sum(t.get("net_pnl", 0.0) or 0.0 for t in subset),
        }

    split = max(n // 2, 1)
    first_half = trades[:split]
    last_half  = trades[split:]

    # Recent 10 trades
    last_10 = trades[-10:] if n >= 10 else trades

    # Cumulative P&L and drawdown
    cum_pnl      = 0.0
    cum_max      = 0.0
    max_drawdown = 0.0
    dd_trades    = ""
    peak_at      = 0

    for i, t in enumerate(trades):
        cum_pnl += float(t.get("net_pnl", 0.0) or 0.0)
        if cum_pnl > cum_max:
            cum_max = cum_pnl
            peak_at = i + 1
        dd = cum_pnl - cum_max
        if dd < max_drawdown:
            max_drawdown = dd
            dd_trades = f"trades #{peak_at}–#{i+1}"

    # Trend detection
    first_stats = _stats(first_half)
    last_stats  = _stats(last_half)
    wr_delta    = last_stats["win_rate"] - first_stats["win_rate"]
    if wr_delta >= 0.10:
        trend = "IMPROVING"
    elif wr_delta <= -0.10:
        trend = "DEGRADING"
    else:
        trend = "STABLE"

    return {
        "total":         n,
        "total_net_pnl": sum(t.get("net_pnl", 0.0) or 0.0 for t in trades),
        "first_half":    first_stats,
        "last_half":     last_stats,
        "last_10":       _stats(last_10),
        "trend":         trend,
        "wr_delta":      wr_delta,
        "cum_max_pnl":   cum_max,
        "max_drawdown":  max_drawdown,
        "dd_trades":     dd_trades,
        "all_time_high": cum_pnl >= cum_max and cum_max > 0,
    }


# ---------------------------------------------------------------------------
# Layer 4: Slippage Analysis
# ---------------------------------------------------------------------------

def _slippage_analysis(trades: list[dict]) -> dict:
    n = len(trades)
    if n == 0:
        return {"n": 0}

    entry_slips = [float(t.get("entry_slippage") or 0.0) for t in trades]
    exit_slips  = [float(t.get("exit_slippage")  or 0.0) for t in trades]

    tp_trades   = [t for t in trades if t.get("exit_reason") == "take_profit"]
    sl_trades   = [t for t in trades if t.get("exit_reason") == "stop_loss"]

    tp_slips    = [float(t.get("exit_slippage") or 0.0) for t in tp_trades]
    sl_slips    = [float(t.get("exit_slippage") or 0.0) for t in sl_trades]

    paper_net   = sum(t.get("net_pnl", 0.0) or 0.0 for t in trades)
    # Rough live estimate: entry slip costs (fill worse), exit slip on SL more
    avg_entry_slip = _safe_div(sum(entry_slips), n)
    avg_exit_slip  = _safe_div(sum(exit_slips), n)

    # Conservative live adjustment: avg entry slip per share * total qty
    total_qty  = sum(int(t.get("entry_qty", 0) or 0) for t in trades)
    live_haircut = avg_entry_slip * total_qty  # rough estimate
    live_low    = paper_net - abs(live_haircut) * 1.5
    live_high   = paper_net - abs(live_haircut) * 0.5

    return {
        "n":              n,
        "avg_entry_slip": avg_entry_slip,
        "avg_exit_slip":  avg_exit_slip,
        "avg_tp_slip":    _safe_div(sum(tp_slips), len(tp_slips)) if tp_slips else None,
        "avg_sl_slip":    _safe_div(sum(sl_slips), len(sl_slips)) if sl_slips else None,
        "paper_net":      paper_net,
        "live_est_low":   live_low,
        "live_est_high":  live_high,
        "pct_optimism":   _safe_div(abs(live_haircut), abs(paper_net)) if paper_net else 0.0,
    }


# ---------------------------------------------------------------------------
# Overview stats
# ---------------------------------------------------------------------------

def _overview_stats(trades: list[dict]) -> dict:
    n = len(trades)
    if n == 0:
        return {"total_trades": 0}

    wins      = [t for t in trades if t.get("outcome") == "WIN"]
    losses    = [t for t in trades if t.get("outcome") == "LOSS"]
    total_pnl = sum(t.get("net_pnl", 0.0) or 0.0 for t in trades)
    avg_pnl   = _safe_div(total_pnl, n)

    exit_reasons = {}
    for t in trades:
        r = t.get("exit_reason") or "unknown"
        exit_reasons[r] = exit_reasons.get(r, 0) + 1

    hold_times = [int(t.get("hold_seconds") or 0) for t in trades if t.get("hold_seconds")]
    avg_hold   = _safe_div(sum(hold_times), len(hold_times)) / 3600 if hold_times else 0.0

    win_pnls  = [t.get("net_pnl", 0.0) or 0.0 for t in wins]
    loss_pnls = [t.get("net_pnl", 0.0) or 0.0 for t in losses]
    avg_win_pnl  = _safe_div(sum(win_pnls),  len(win_pnls))  if win_pnls  else 0.0
    avg_loss_pnl = _safe_div(sum(loss_pnls), len(loss_pnls)) if loss_pnls else 0.0
    win_sum  = sum(win_pnls)
    loss_sum = abs(sum(loss_pnls))
    profit_factor = _safe_div(win_sum, loss_sum) if loss_sum else None  # None = no losses yet
    wr_dec    = _safe_div(len(wins), n)
    lr_dec    = _safe_div(len(losses), n)
    expectancy = (wr_dec * avg_win_pnl) + (lr_dec * avg_loss_pnl)

    return {
        "total_trades":   n,
        "wins":           len(wins),
        "losses":         len(losses),
        "win_rate":       wr_dec,
        "total_net_pnl":  total_pnl,
        "avg_net_pnl":    avg_pnl,
        "avg_hold_hours": avg_hold,
        "exit_reasons":   exit_reasons,
        "confidence_tier": _confidence_tier(n),
        "avg_win_pnl":    avg_win_pnl,
        "avg_loss_pnl":   avg_loss_pnl,
        "profit_factor":  profit_factor,
        "expectancy":     expectancy,
    }


# ---------------------------------------------------------------------------
# Observation generator — plain-English summaries
# ---------------------------------------------------------------------------

def _generate_observations(analysis: dict) -> list[str]:
    obs = []
    ov  = analysis.get("overview", {})
    te  = analysis.get("temporal", {})
    sl  = analysis.get("slippage", {})

    n         = ov.get("total_trades", 0)
    win_rate  = ov.get("win_rate", 0.0)
    tier      = ov.get("confidence_tier", "insufficient")

    if tier == "insufficient":
        obs.append(
            f"Only {n} closed trade(s) so far. Most signal-level insights require {_MIN_SAMPLE}+ trades. "
            f"Overall stats and slippage measurement are useful from trade #1."
        )
    else:
        obs.append(
            f"Overall win rate: {win_rate*100:.1f}% across {n} trades {_confidence_note(n)}."
        )

    # Temporal trend
    trend = te.get("trend", "")
    if trend == "IMPROVING" and n >= 20:
        delta = te.get("wr_delta", 0.0)
        obs.append(
            f"Pipeline is IMPROVING: win rate up {delta*100:.1f}pp in second half vs first half."
        )
    elif trend == "DEGRADING" and n >= 20:
        delta = te.get("wr_delta", 0.0)
        obs.append(
            f"Pipeline is DEGRADING: win rate down {abs(delta)*100:.1f}pp in recent trades. Review recent decisions."
        )

    dd = te.get("max_drawdown", 0.0)
    if dd < -50:
        obs.append(f"Maximum drawdown reached ${dd:.2f} ({te.get('dd_trades','')}).")

    # Signal findings — only surfaces noteworthy buckets
    sig = analysis.get("signals", {})

    vc_buckets = sig.get("vol_surge", [])
    best_vc = max(vc_buckets, key=lambda b: b["win_rate"], default=None)
    if best_vc and best_vc["n"] >= _MIN_SAMPLE:
        obs.append(
            f"Best vol_surge range: {best_vc['label']} — "
            f"{best_vc['win_rate']*100:.0f}% win rate "
            f"(avg +${best_vc['avg_pnl']:.2f}) {_confidence_note(best_vc['n'])}."
        )

    extreme_vc = next((b for b in vc_buckets if "5.0x+" in b["label"]), None)
    if extreme_vc and extreme_vc["win_rate"] < 0.35:
        obs.append(
            f"Extreme vol_surge (5x+) is underperforming: "
            f"{extreme_vc['win_rate']*100:.0f}% win rate. Consider a hard cap or extra risk flag."
        )

    conf_buckets = sig.get("confidence", [])
    low_conf = next((b for b in conf_buckets if "<0.60" in b["label"]), None)
    if low_conf and low_conf["win_rate"] < 0.40 and low_conf["n"] >= _MIN_SAMPLE:
        obs.append(
            f"Low-confidence trades (<0.60) are dragging returns: "
            f"{low_conf['win_rate']*100:.0f}% win rate. Consider raising the BUY threshold for these."
        )

    # Best / worst combos
    cm = analysis.get("combinations", {})
    best_c  = cm.get("best", [])
    worst_c = cm.get("worst", [])

    if best_c:
        top = best_c[0]
        obs.append(
            f"Strongest setup: '{top['label']}' — "
            f"{top['win_rate']*100:.0f}% win rate {_confidence_note(top['n'])}."
        )
    if worst_c:
        bot = worst_c[0]
        obs.append(
            f"Weakest setup: '{bot['label']}' — "
            f"{bot['win_rate']*100:.0f}% win rate {_confidence_note(bot['n'])}. Consider blocking."
        )

    # Slippage
    if sl.get("n", 0) >= _MIN_SAMPLE:
        ent = sl.get("avg_entry_slip", 0.0)
        opt = sl.get("pct_optimism", 0.0)
        obs.append(
            f"Average entry slippage: ${ent:+.4f}/share. "
            f"Paper results are estimated {opt*100:.0f}% optimistic vs live trading."
        )

    return obs


# ---------------------------------------------------------------------------
# Layer 5: Per-trade signal cards with auto-commentary
# ---------------------------------------------------------------------------

def _generate_trade_commentary(t: dict) -> tuple[list[str], list[str]]:
    """
    Inspect a single trade's snapshot values and return (positives, warnings).
    All thresholds align with src_risk.py and src_scoring.py logic.
    """
    outcome  = (t.get("outcome") or "").upper()
    score    = float(t.get("score_total") or 0)
    conf     = float(t.get("confidence") or 0)
    risk     = float(t.get("risk_score") or 0)
    vol      = float(t.get("vol_surge") or 0)
    brk      = int(t.get("breakout") or 0)
    gate     = (t.get("gate_decision") or "").upper()
    sec_a    = int(t.get("sec_trigger_a") or 0)
    sec_hits = int(t.get("sec_hits") or 0)

    positives: list[str] = []
    warnings:  list[str] = []

    # Confidence
    if conf >= 0.80:
        positives.append(f"High confidence ({conf:.2f}) — signal well above BUY threshold")
    elif conf >= 0.70:
        positives.append(f"Solid confidence ({conf:.2f})")
    elif 0 < conf < 0.65:
        warnings.append(f"Low confidence ({conf:.2f}) — borderline BUY near decision threshold")

    # Risk score
    if 0 < risk < 0.20:
        positives.append(f"Low risk score ({risk:.2f}) — favourable risk profile")
    elif risk < 0.30:
        positives.append(f"Moderate risk ({risk:.2f})")
    elif risk >= 0.35:
        warnings.append(
            f"Elevated risk score ({risk:.2f}) — position sizing should have been reduced"
        )

    # Breakout
    if brk == 1:
        positives.append("Breakout confirmed — price cleared recent resistance structure")
    else:
        warnings.append("No breakout confirmation — vol surge alone drove the entry signal")

    # Gate decision
    if gate == "APPROVE":
        positives.append("AI gate: APPROVE — no conflicting news risk at entry")
    elif gate in ("HOLD", "REDUCE"):
        warnings.append(
            f"AI gate flagged {gate} — trade was executed against the gate signal"
        )
    elif gate == "BLOCK":
        warnings.append(
            "AI gate flagged BLOCK — strongest caution signal; entry overrode it"
        )

    # Vol surge
    if 2.0 <= vol <= 5.0:
        positives.append(f"Healthy vol surge ({vol:.1f}x) — meaningful but not excessive")
    elif vol > 5.0:
        warnings.append(
            f"Extreme vol surge ({vol:.1f}x) — heightened pump/dump risk at these levels"
        )
    elif 0 < vol < 1.5:
        warnings.append(f"Low vol surge ({vol:.1f}x) — weak conviction signal")

    # Score
    if score >= 90:
        positives.append(f"Strong composite score ({score:.1f}) — multiple pillars aligned")

    # SEC news
    if sec_a == 1 or sec_hits >= 2:
        warnings.append(
            "SEC EDGAR filings flagged at entry — dilution/warrant risk was present"
        )

    # For losses with no detected warnings, add a catch-all note
    if outcome == "LOSS" and not warnings:
        warnings.append(
            f"No clear parameter warnings — score {score:.1f}, conf {conf:.2f}, "
            f"risk {risk:.2f} all within normal range. Likely a market or timing "
            f"factor outside the model's current scope."
        )

    return positives, warnings


def _trade_signal_cards(trades: list[dict]) -> list[dict]:
    """
    Build a per-trade card dict (newest first, max 25) with signal values
    and auto-generated commentary for the HTML report.
    """
    cards = []
    for t in trades:
        positives, warnings = _generate_trade_commentary(t)

        net_pnl   = float(t.get("net_pnl") or 0)
        entry_p   = float(t.get("entry_price") or 0)
        exit_p    = float(t.get("exit_price") or 0)
        qty       = int(t.get("entry_qty") or 0)
        trade_val = entry_p * qty
        pnl_pct   = (net_pnl / trade_val * 100) if trade_val else 0.0
        hold_s    = int(t.get("hold_seconds") or 0)

        cards.append({
            "symbol":      t.get("symbol", "?"),
            "closed_at":   (t.get("closed_at") or "")[:10],
            "outcome":     (t.get("outcome") or "").upper(),
            "net_pnl":     net_pnl,
            "pnl_pct":     pnl_pct,
            "hold_seconds": hold_s,
            "score":       float(t.get("score_total") or 0),
            "confidence":  float(t.get("confidence") or 0),
            "risk":        float(t.get("risk_score") or 0),
            "vol_surge":   float(t.get("vol_surge") or 0),
            "breakout":    int(t.get("breakout") or 0),
            "ret_5":       float(t.get("ret_5") or 0),
            "setup_type":  t.get("setup_type") or "",
            "gate":        (t.get("gate_decision") or "").upper(),
            "entry_price": entry_p,
            "exit_price":  exit_p,
            "entry_qty":   qty,
            "exit_reason": t.get("exit_reason") or "",
            "gross_pnl":   float(t.get("gross_pnl") or 0),
            "commission":  float(t.get("commission_total") or 0),
            "positives":   positives,
            "warnings":    warnings,
        })

    # Newest first, cap at 25
    cards.sort(key=lambda c: c["closed_at"], reverse=True)
    return cards[:25]


# ---------------------------------------------------------------------------
# Layer 6: Pillar correlation analysis
# ---------------------------------------------------------------------------

def _pillar_correlation(trades: list[dict]) -> list[dict]:
    """
    For each key scoring pillar, compute win/loss stats per threshold bucket.
    Returns a flat list of rows: {pillar, label, n, wins, losses, win_rate, avg_pnl}.
    """
    def _stats(subset: list[dict]) -> dict | None:
        n = len(subset)
        if n == 0:
            return None
        wins    = sum(1 for t in subset if (t.get("outcome") or "") == "WIN")
        losses  = sum(1 for t in subset if (t.get("outcome") or "") == "LOSS")
        avg_pnl = _safe_div(sum(float(t.get("net_pnl") or 0) for t in subset), n)
        return {"n": n, "wins": wins, "losses": losses,
                "win_rate": _safe_div(wins, n), "avg_pnl": avg_pnl}

    pillars = [
        ("Confidence", [
            ("≥ 0.80  (high)",      lambda t: (t.get("confidence") or 0) >= 0.80),
            ("0.70 – 0.79  (solid)",lambda t: 0.70 <= (t.get("confidence") or 0) < 0.80),
            ("< 0.70  (low)",       lambda t: 0 < (t.get("confidence") or 0) < 0.70),
        ]),
        ("Risk Score", [
            ("< 0.20  (low)",       lambda t: 0 < (t.get("risk_score") or 0) < 0.20),
            ("0.20 – 0.34  (mod)",  lambda t: 0.20 <= (t.get("risk_score") or 0) < 0.35),
            ("≥ 0.35  (high)",      lambda t: (t.get("risk_score") or 0) >= 0.35),
        ]),
        ("Breakout", [
            ("Yes  (breakout = 1)", lambda t: int(t.get("breakout") or 0) == 1),
            ("No   (breakout = 0)", lambda t: int(t.get("breakout") or 0) == 0),
        ]),
        ("Gate Decision", [
            ("APPROVE",             lambda t: (t.get("gate_decision") or "") == "APPROVE"),
            ("HOLD / BLOCK",        lambda t: (t.get("gate_decision") or "") in ("HOLD","BLOCK","REDUCE")),
        ]),
        ("Vol Surge", [
            ("< 3x",                lambda t: 0 < (t.get("vol_surge") or 0) < 3.0),
            ("3 – 5x",              lambda t: 3.0 <= (t.get("vol_surge") or 0) < 5.0),
            ("> 5x",                lambda t: (t.get("vol_surge") or 0) >= 5.0),
        ]),
        ("Score", [
            ("≥ 90",                lambda t: (t.get("score_total") or 0) >= 90.0),
            ("85 – 89",             lambda t: 85.0 <= (t.get("score_total") or 0) < 90.0),
            ("80 – 84",             lambda t: 80.0 <= (t.get("score_total") or 0) < 85.0),
        ]),
    ]

    rows = []
    for pillar_name, buckets in pillars:
        for label, pred in buckets:
            subset = [t for t in trades if pred(t)]
            st = _stats(subset)
            if st:
                rows.append({"pillar": pillar_name, "label": label, **st})
    return rows


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_learning_analysis(conn: sqlite3.Connection) -> dict:
    """
    Run the full learning analysis against the database.

    Returns a dict with keys: overview, signals, combinations, temporal,
    slippage, observations, generated_at.
    """
    trades   = _load_closed_trades(conn)
    overview = _overview_stats(trades)
    signals  = _signal_analysis(trades)
    combos   = _combination_analysis(trades)
    temporal = _temporal_analysis(trades)
    slippage = _slippage_analysis(trades)
    cards    = _trade_signal_cards(trades)
    pillars  = _pillar_correlation(trades)

    analysis = {
        "overview":           overview,
        "signals":            signals,
        "combinations":       combos,
        "temporal":           temporal,
        "slippage":           slippage,
        "trade_cards":        cards,
        "pillar_correlation": pillars,
        "generated_at":       datetime.utcnow().isoformat() + "+00:00",
    }
    analysis["observations"] = _generate_observations(analysis)
    return analysis


def save_learning_snapshot(conn: sqlite3.Connection, analysis: dict):
    """Persist an analysis snapshot to learning_snapshots table."""
    ov   = analysis.get("overview", {})
    obs  = analysis.get("observations", [])
    sig  = analysis.get("signals", {})
    te   = analysis.get("temporal", {})

    wins   = ov.get("wins", 0)
    losses = ov.get("losses", 0)
    n      = ov.get("total_trades", 0)
    pf     = ov.get("profit_factor")
    exp    = ov.get("expectancy", 0.0)

    # Gross P&L = net P&L + fees (we don't store gross separately in learning, use net as proxy)
    gross_pnl = float(ov.get("total_net_pnl", 0.0))   # best approximation

    # Determine period label from temporal trend
    trend       = te.get("trend", "STABLE")
    period_label = f"all-time ({trend})"

    cur = conn.cursor()
    cur.execute("""
        INSERT INTO learning_snapshots (
            total_trades, win_rate, avg_net_pnl, total_net_pnl, avg_hold_hours,
            period_label, win_count, loss_count, gross_pnl,
            profit_factor, expectancy,
            avg_win_pnl, avg_loss_pnl,
            signal_analysis, observations,
            analysis_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        n,
        float(ov.get("win_rate", 0.0)),
        float(ov.get("avg_net_pnl", 0.0)),
        float(ov.get("total_net_pnl", 0.0)),
        float(ov.get("avg_hold_hours", 0.0)),
        period_label,
        wins,
        losses,
        gross_pnl,
        float(pf) if pf is not None else None,
        float(exp),
        float(ov.get("avg_win_pnl", 0.0)),
        float(ov.get("avg_loss_pnl", 0.0)),
        json.dumps(sig),
        json.dumps(obs),
        json.dumps(analysis),
    ))
    conn.commit()
