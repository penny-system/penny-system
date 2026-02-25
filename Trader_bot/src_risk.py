import config


def _pct_change(a: float, b: float) -> float:
    if b == 0:
        return 0.0
    return (a - b) / b


def risk_score_and_flags(symbol: str, feat: dict, regime: dict | None = None, corr_to_holdings: float | None = None):
    """
    Returns: (risk_score, flags_string)

    risk_score: 0 (low risk) to 100 (high risk)
    flags: comma-separated tags
    """
    flags = []

    price = float(feat.get("ref_price", 0) or 0)
    vol_surge = float(feat.get("vol_surge", 1.0) or 1.0)
    dollar_vol = float(feat.get("dollar_vol", 0) or 0)
    ret_1 = float(feat.get("ret_1", 0) or 0)
    ret_5 = float(feat.get("ret_5", 0) or 0)

    risk = 25.0

    # --- Liquidity / turnover proxies ---
    if dollar_vol < 250_000:
        risk += 25
        flags.append("low_dollar_volume")
    elif dollar_vol < 1_000_000:
        risk += 10

    # --- Extreme volume spikes (often rug-pull risk) ---
    if vol_surge >= 15:
        risk += 25
        flags.append("extreme_volume_spike")
    elif vol_surge >= 7:
        risk += 12
        flags.append("high_volume_spike")

    # --- Very cheap stocks behave worse mechanically ---
    if price < 0.75:
        risk += 10
        flags.append("very_low_price")

    # --- Gap / shock proxy using 1d return ---
    if abs(ret_1) > 0.12:
        risk += 12
        flags.append("high_intraday_move")

    # --- Regime penalty ---
    if regime:
        if regime.get("regime") == "risk_off":
            risk += 10
            flags.append("risk_off_regime")

    # --- Crowding / correlation vs current holdings ---
    if corr_to_holdings is not None:
        if corr_to_holdings >= float(getattr(config, "CORR_FLAG_THRESHOLD", 0.60)):
            risk += 12
            flags.append(f"correlated_to_holdings_{corr_to_holdings:.2f}")

    # Clamp 0..100
    risk = max(0.0, min(100.0, risk))
    return float(risk), ("none" if not flags else ",".join(flags))
