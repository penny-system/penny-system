# src_sizing.py
from __future__ import annotations

import config
from typing import Any, Dict, List, Optional

from src_settings import apply_overrides_to_config, load_overrides
from src_ibkr_client import get_effective_purse_usd, get_live_fx_rate


def _get_max_positions() -> int:
    return int(getattr(config, "MAX_POSITIONS", 6))


def _get_min_position_usd() -> float:
    return float(getattr(config, "MIN_POSITION_USD", 0.0))


def _get_max_position_usd() -> float:
    # optional cap
    return float(getattr(config, "MAX_POSITION_USD", 10**12))


def size_recommendations(
    recs: List[Dict[str, Any]],
    ib=None,
) -> List[Dict[str, Any]]:
    """
    Adds suggested_qty to BUY recs, using:
      effective_purse_usd = min(configured_purse, IBKR available funds)

    - Applies Telegram/runtime overrides first
    - Caps to MAX_POSITIONS
    - Risk-weighted allocation: lower risk => higher weight
    - Enforces MIN_POSITION_USD
    - Optional MAX_POSITION_USD cap

    Works even if ib is None:
      - then it uses configured purse only (no IBKR cap)
    """
    apply_overrides_to_config(config)

    # ── FX rate resolution ───────────────────────────────────────────────────
    # Priority: manual override in runtime_overrides.json > live IBKR/API rate
    # > config/hardcoded fallback.  Manual override wins when explicitly set.
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

    # Determine effective purse
    if ib is not None:
        purse_info = get_effective_purse_usd(ib)
        purse_usd = float(purse_info["effective_purse_usd"])
        # attach for reporting (optional)
        for r in recs:
            r["_purse_note"] = purse_info.get("note", "")
            r["_purse_configured"] = purse_info.get("configured_purse_usd", 0.0)
            r["_purse_available"] = purse_info.get("available_funds_usd", 0.0)
            r["_purse_effective"] = purse_usd
    else:
        # Configured only (no IBKR connection)
        # CAD purse takes priority when set (matches get_effective_purse_usd logic)
        cad = float(getattr(config, "TRADE_PURSE_CAD", 0.0) or 0.0)
        if cad > 0:
            fx = float(getattr(config, "USD_PER_CAD", 0.73) or 0.73)
            purse_usd = cad * fx
        else:
            purse_usd = float(getattr(config, "TRADE_PURSE_USD", 0.0) or 0.0)

    purse_usd = max(purse_usd, 0.0)
    if purse_usd <= 0:
        # no capital => qty 0
        for r in recs:
            if r.get("decision") == "BUY":
                r["suggested_qty"] = 0
        return recs

    max_pos = _get_max_positions()
    min_pos_usd = _get_min_position_usd()
    max_pos_usd = _get_max_position_usd()

    # Keep only BUY recommendations
    buy_recs = [r for r in recs if r.get("decision") == "BUY"]
    if not buy_recs:
        return recs

    # Take top N by score_total (fallback to score)
    def _score_key(x):
        return float(x.get("score_total", x.get("score", 0.0)) or 0.0)

    buy_recs = sorted(buy_recs, key=_score_key, reverse=True)[:max_pos]

    # Risk weights: lower risk -> higher allocation
    weights = []
    for r in buy_recs:
        risk = float(r.get("risk_score", r.get("risk", 25.0)) or 25.0)
        risk = max(risk, 1.0)
        weights.append(1.0 / risk)

    total_w = sum(weights) if weights else 0.0
    if total_w <= 0:
        for r in buy_recs:
            r["suggested_qty"] = 0
        return recs

    # Allocate
    for r, w in zip(buy_recs, weights):
        alloc_usd = purse_usd * (w / total_w)

        # apply max position cap
        alloc_usd = min(alloc_usd, max_pos_usd)

        price = float(r.get("ref_price", r.get("ref", 0.0)) or 0.0)
        if price <= 0:
            r["suggested_qty"] = 0
            continue

        if alloc_usd < min_pos_usd:
            r["suggested_qty"] = 0
            continue

        qty = int(alloc_usd / price)
        r["suggested_qty"] = max(qty, 0)

    # Ensure BUY recs outside top N still have suggested_qty field
    top_symbols = {r.get("symbol") for r in buy_recs}
    for r in recs:
        if r.get("decision") == "BUY" and r.get("symbol") not in top_symbols:
            r["suggested_qty"] = 0

    return recs