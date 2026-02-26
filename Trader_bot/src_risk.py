import config


def risk_score_and_flags(
    symbol: str,
    feat: dict,
    sec_hits: int = 0,
    sec_trigger_a: bool = False,
    stocktwits_score: float = 0.0,
) -> tuple:
    """
    Weighted risk model. Each sub-factor is scored 0-100, multiplied by its
    weight, and summed. Weights total 100%. Final score is normalized to 0–1
    (floor 0.10, hard clamp 1.0).

    Pillars:
      Market data (65%):
        Liquidity / dollar vol    25%
        Volume surge              20%
        Intraday move (ret_1)     12%
        Price level               8%

      External signals (35%):
        SEC EDGAR filings         15%
        Realized volatility       12%
        StockTwits pump signal    8%

    Returns: (risk_score: float, flags: str)
    """
    flags = []

    dollar_vol   = float(feat.get("dollar_vol",   0)   or 0)
    vol_surge    = float(feat.get("vol_surge",     1.0) or 1.0)
    ret_1        = float(feat.get("ret_1",         0)   or 0)
    ref_price    = float(feat.get("ref_price",     0)   or 0)
    realized_vol = float(feat.get("realized_vol",  0)   or 0)

    # ------------------------------------------------------------------
    # Market data sub-scores (0-100 each)
    # ------------------------------------------------------------------

    # Liquidity (25%) — low turnover = harder to exit, pump risk
    if dollar_vol < 250_000:
        liq_score = 100
        flags.append("low_dollar_volume")
    elif dollar_vol < 1_000_000:
        liq_score = 50
        flags.append("moderate_dollar_volume")
    else:
        liq_score = 0

    # Volume surge (20%) — extreme spikes often signal rug-pulls
    if vol_surge >= 15:
        vol_score = 100
        flags.append("extreme_volume_spike")
    elif vol_surge >= 7:
        vol_score = 75
        flags.append("high_volume_spike")
    elif vol_surge >= 3:
        vol_score = 25
    else:
        vol_score = 0

    # Intraday move (12%) — gap/shock proxy
    abs_ret = abs(ret_1)
    if abs_ret > 0.12:
        move_score = 100
        flags.append("high_intraday_move")
    elif abs_ret > 0.05:
        move_score = 50
    else:
        move_score = 0

    # Price level (8%) — very cheap stocks are mechanically riskier
    if ref_price < 0.75:
        price_score = 100
        flags.append("very_low_price")
    elif ref_price < 1.00:
        price_score = 50
    else:
        price_score = 0

    # ------------------------------------------------------------------
    # External signal sub-scores (0-100 each)
    # ------------------------------------------------------------------

    # SEC EDGAR (15%) — dilution / toxic financing filings
    if sec_trigger_a or sec_hits >= 2:
        sec_score = 100
        flags.append("sec_dilution_risk")
    elif sec_hits == 1:
        sec_score = 50
        flags.append("sec_filing_flagged")
    else:
        sec_score = 0

    # Realized volatility (12%) — annualized from 1-min log returns
    if realized_vol > 2.0:       # > 200% annualized
        realvol_score = 100
        flags.append("extreme_realized_vol")
    elif realized_vol > 1.0:     # 100-200% annualized
        realvol_score = 50
        flags.append("high_realized_vol")
    else:
        realvol_score = 0

    # StockTwits (8%) — pump-and-dump social signal
    twits_score = float(stocktwits_score)
    if twits_score >= 100:
        flags.append("pump_signal")
    elif twits_score >= 50:
        flags.append("elevated_social_activity")

    # ------------------------------------------------------------------
    # Weighted sum
    # ------------------------------------------------------------------
    risk = (
        0.25 * liq_score +
        0.20 * vol_score +
        0.12 * move_score +
        0.08 * price_score +
        0.15 * sec_score +
        0.12 * realvol_score +
        0.08 * twits_score
    )

    # Normalize to 0–1 (sub-scores were 0–100, weights sum to 1.0)
    risk = risk / 100.0

    # Floor at 0.10, hard clamp at 1.0
    risk = max(risk, 0.10)
    risk = min(risk, 1.0)

    return float(risk), ("none" if not flags else ",".join(flags))
