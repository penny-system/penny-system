def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def score_candidate(feat: dict, horizon: str):
    """
    Uses keys produced by src_features.py:
      ref_price, ret_1, ret_5, vol_surge, dollar_vol, breakout

    Adds a critical concept for pennies:
      - "confirmation": breakout + volume >= threshold OR very strong liquidity
      - if not confirmed, we cap score (prevents constant BUYs)
    """

    ref_price = float(feat.get("ref_price", 0) or 0)

    # Hard filter: only score penny-stock price range [$1.00, $8.00]
    if not (1.00 <= ref_price <= 8.00):
        print(f"[FILTER] {feat.get('symbol', '?')} ref_price ${ref_price:.2f} outside $1.00–$8.00 — skipped")
        return None

    ret_1 = float(feat.get("ret_1", 0) or 0)
    ret_5 = float(feat.get("ret_5", 0) or 0)
    vol_surge = float(feat.get("vol_surge", 1.0) or 1.0)
    dollar_vol = float(feat.get("dollar_vol", 0) or 0)
    breakout = int(feat.get("breakout", 0) or 0)

    # ----- Return component depends on horizon -----
    if horizon == "momentum":
        ret_component = _clamp(ret_1 * 350, -20, 35)   # a bit less aggressive
        ret_used = ret_1
        ret_label = "ret_1"
    else:
        ret_component = _clamp(ret_5 * 180, -20, 35)
        ret_used = ret_5
        ret_label = "ret_5"

    # ----- Volume surge component -----
    # 1x => 0, 3x => 12, 6x => 30 (capped)
    vol_component = _clamp((vol_surge - 1.0) * 6.0, 0, 30)

    # ----- Breakout bonus (only meaningful with confirmation) -----
    breakout_component = 0.0
    if breakout == 1:
        breakout_component = 8.0  # base breakout bonus

    # ----- Liquidity component from dollar volume -----
    if dollar_vol >= 50_000_000:
        liq_component = 12.0
    elif dollar_vol >= 10_000_000:
        liq_component = 9.0
    elif dollar_vol >= 2_000_000:
        liq_component = 6.0
    elif dollar_vol >= 500_000:
        liq_component = 2.0
    else:
        liq_component = 0.0

    # ----- Price penalty for very low-priced stocks -----
    price_penalty = 0.0
    if ref_price < 0.75:
        price_penalty = -10.0
    elif ref_price < 1.00:
        price_penalty = -5.0

    # ----- Confirmation logic (prevents false BUY spam) -----
    # Confirmed if breakout + volume is above average, OR liquidity is extremely strong.
    confirmed = False
    if breakout == 1 and vol_surge >= 1.20:
        confirmed = True
    if dollar_vol >= 25_000_000:
        confirmed = True

    # ----- Score assembly -----
    score = 35.0 + ret_component + vol_component + breakout_component + liq_component + price_penalty

    # If not confirmed, cap the score to avoid BUY.
    # (still allows WATCH/SETUP depending on thresholds)
    if not confirmed:
        score = min(score, 74.9)

    score = _clamp(score, 0.0, 100.0)

    # ----- Confidence -----
    conf = 0.55
    if confirmed:
        conf += 0.10
    if breakout == 1:
        conf += 0.05
    if vol_surge >= 2.0:
        conf += 0.05
    if dollar_vol >= 10_000_000:
        conf += 0.05
    conf = _clamp(conf, 0.50, 0.95)

    # Setup type labeling
    if breakout == 1 and vol_surge >= 2.0:
        setup_type = "breakout_volume_surge"
    elif breakout == 1:
        setup_type = "breakout_unconfirmed"
    else:
        setup_type = "watch"

    _parts = []
    _label = "Short-term" if horizon == "momentum" else "5-day"
    _sign = "+" if ret_used > 0 else ""
    _parts.append(f"{_label} move {_sign}{ret_used*100:.1f}%")
    if vol_surge >= 2.0:
        _parts.append(f"volume surge {vol_surge:.1f}x normal")
    elif vol_surge >= 1.2:
        _parts.append(f"above-average volume ({vol_surge:.1f}x)")
    if breakout == 1:
        _parts.append("price breakout confirmed" if confirmed else "price breakout unconfirmed")
    if dollar_vol >= 50_000_000:
        _parts.append("high liquidity ($50M+ daily)")
    elif dollar_vol >= 10_000_000:
        _parts.append("strong liquidity ($10M+ daily)")
    elif dollar_vol >= 2_000_000:
        _parts.append(f"${dollar_vol/1_000_000:.0f}M daily volume")
    rationale = ". ".join(_parts) + "." if _parts else "Scan criteria met."

    return float(score), float(conf), setup_type, rationale
