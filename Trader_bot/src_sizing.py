# src_sizing.py
from __future__ import annotations

import os
import sqlite3
import config
from typing import Any, Dict, List, Optional

from src_settings import apply_overrides_to_config, load_overrides
from src_ibkr_client import get_live_fx_rate


def _get_min_position_usd() -> float:
    return float(getattr(config, "MIN_POSITION_USD", 0.0))


def _get_max_position_usd() -> float:
    return float(getattr(config, "MAX_POSITION_USD", 10**12))


def _get_deployed_capital_usd() -> float:
    """
    Query open_positions DB for total capital currently deployed.
    Returns sum(fill_price * fill_qty) for all status='open' rows.
    Returns 0.0 on any error (sizing degrades gracefully if DB unavailable).
    """
    try:
        db_path = getattr(config, "DB_PATH", "output/trader.sqlite")
        bot_dir = os.path.dirname(os.path.abspath(__file__))
        full_path = os.path.join(bot_dir, db_path)
        conn = sqlite3.connect(full_path)
        cur = conn.cursor()
        cur.execute(
            "SELECT SUM(fill_price * fill_qty) FROM open_positions WHERE status='open'"
        )
        row = cur.fetchone()
        conn.close()
        if row and row[0] is not None:
            return float(row[0])
    except Exception:
        pass
    return 0.0


def _classify_tier(r: Dict[str, Any]) -> str:
    """
    Classify a BUY candidate into HIGH / MED / LOW allocation tier.
    All three criteria must be met (AND logic) to qualify for HIGH or MED.
    Falls through to LOW otherwise.
    """
    score = float(r.get("score_total", r.get("score", 0.0)) or 0.0)
    conf  = float(r.get("confidence", 0.0) or 0.0)
    risk  = float(r.get("risk_score", r.get("risk", 1.0)) or 1.0)

    high_score = float(getattr(config, "ALLOC_TIER_HIGH_MIN_SCORE", 87.0))
    high_conf  = float(getattr(config, "ALLOC_TIER_HIGH_MIN_CONF",  0.75))
    high_risk  = float(getattr(config, "ALLOC_TIER_HIGH_MAX_RISK",  0.35))

    med_score  = float(getattr(config, "ALLOC_TIER_MED_MIN_SCORE", 83.0))
    med_conf   = float(getattr(config, "ALLOC_TIER_MED_MIN_CONF",  0.60))
    med_risk   = float(getattr(config, "ALLOC_TIER_MED_MAX_RISK",  0.50))

    if score >= high_score and conf >= high_conf and risk <= high_risk:
        return "HIGH"
    if score >= med_score and conf >= med_conf and risk <= med_risk:
        return "MED"
    return "LOW"


def _get_tier_pct(tier: str) -> float:
    pcts = {
        "HIGH": float(getattr(config, "ALLOC_TIER_HIGH_PCT", 0.25)),
        "MED":  float(getattr(config, "ALLOC_TIER_MED_PCT",  0.18)),
        "LOW":  float(getattr(config, "ALLOC_TIER_LOW_PCT",  0.12)),
    }
    return pcts.get(tier, pcts["LOW"])


def size_recommendations(
    recs: List[Dict[str, Any]],
    ib=None,
) -> List[Dict[str, Any]]:
    """
    Adds suggested_qty to BUY recs using tier-based allocation:

    - Configured purse (CAD or USD) minus already-deployed capital from the
      open_positions DB gives the remaining available purse.
    - Each BUY candidate is classified HIGH / MED / LOW based on score_total,
      confidence, and risk_score (strict AND logic per tier).
    - Candidates are processed highest-score-first. Each gets its tier %
      of the remaining purse at time of allocation (first-come, first-served).
    - No cap on number of positions — purse exhaustion is the only limit.
    - MIN_POSITION_USD and MAX_POSITION_USD floor/ceiling still apply.

    ib is optional and used only for live FX rate resolution.
    """
    apply_overrides_to_config(config)

    # ── FX rate resolution ───────────────────────────────────────────────────
    _manual_fx = float(load_overrides().get("USD_PER_CAD", 0.0) or 0.0)
    if _manual_fx > 0:
        print(f"[FX] FX rate: 1 CAD = {_manual_fx:.4f} USD (source: manual override)")
    elif ib is not None:
        _live_fx, _fx_source = get_live_fx_rate(ib)
        if _live_fx > 0:
            config.USD_PER_CAD = _live_fx
            print(f"[FX] FX rate: 1 CAD = {_live_fx:.4f} USD (source: {_fx_source})")
        else:
            _fb = float(getattr(config, "USD_PER_CAD", 0.73) or 0.73)
            print(f"[FX] FX rate: 1 CAD = {_fb:.4f} USD (source: config fallback — live fetch failed)")
    else:
        _fb = float(getattr(config, "USD_PER_CAD", 0.73) or 0.73)
        print(f"[FX] FX rate: 1 CAD = {_fb:.4f} USD (source: config fallback — no IBKR connection)")

    if not recs:
        return recs

    # ── Configured purse (no IBKR cap) ───────────────────────────────────────
    cad = float(getattr(config, "TRADE_PURSE_CAD", 0.0) or 0.0)
    if cad > 0:
        fx = float(getattr(config, "USD_PER_CAD", 0.73) or 0.73)
        configured_purse = cad * fx
    else:
        configured_purse = float(getattr(config, "TRADE_PURSE_USD", 0.0) or 0.0)

    configured_purse = max(configured_purse, 0.0)

    if configured_purse <= 0:
        for r in recs:
            if r.get("decision") == "BUY":
                r["suggested_qty"] = 0
        return recs

    # ── Deployed capital from open positions ─────────────────────────────────
    deployed_usd = _get_deployed_capital_usd()
    remaining_purse = max(0.0, configured_purse - deployed_usd)

    print(f"[Sizing] Configured purse: ${configured_purse:,.2f} | Deployed: ${deployed_usd:,.2f} | Remaining: ${remaining_purse:,.2f}")

    if remaining_purse <= 0:
        print("[Sizing] No remaining purse — all BUY candidates get qty=0")
        for r in recs:
            if r.get("decision") == "BUY":
                r["suggested_qty"] = 0
        return recs

    min_pos_usd = _get_min_position_usd()
    max_pos_usd = _get_max_position_usd()

    # ── Tier-based allocation (highest score first) ───────────────────────────
    buy_recs = [r for r in recs if r.get("decision") == "BUY"]
    if not buy_recs:
        return recs

    def _score_key(x):
        return float(x.get("score_total", x.get("score", 0.0)) or 0.0)

    buy_recs_sorted = sorted(buy_recs, key=_score_key, reverse=True)

    budget = remaining_purse
    for r in buy_recs_sorted:
        if budget <= 0:
            r["suggested_qty"] = 0
            continue

        tier = _classify_tier(r)
        tier_pct = _get_tier_pct(tier)
        alloc_usd = tier_pct * remaining_purse  # always % of original remaining, not shrinking budget

        # Can't allocate more than what's left
        alloc_usd = min(alloc_usd, budget)

        # Apply per-position caps
        alloc_usd = min(alloc_usd, max_pos_usd)

        if alloc_usd < min_pos_usd:
            r["suggested_qty"] = 0
            r["_tier"] = tier
            budget -= alloc_usd
            continue

        price = float(r.get("ref_price", r.get("ref", 0.0)) or 0.0)
        if price <= 0:
            r["suggested_qty"] = 0
            continue

        qty = int(alloc_usd / price)
        r["suggested_qty"] = max(qty, 0)
        r["_tier"] = tier
        budget -= alloc_usd

        print(f"[Sizing] {r.get('symbol','?')} tier={tier} ({tier_pct*100:.0f}%) alloc=${alloc_usd:,.2f} qty={qty}")

    return recs
