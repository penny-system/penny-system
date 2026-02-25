from ib_insync import Stock
import config


def _daily_closes(ib, symbol: str, days: int) -> list[float]:
    c = Stock(symbol, "SMART", "USD")
    ib.qualifyContracts(c)
    bars = ib.reqHistoricalData(
        c,
        endDateTime="",
        durationStr=f"{max(days, 5)} D",
        barSizeSetting="1 day",
        whatToShow="TRADES",
        useRTH=True,
        formatDate=1,
    )
    closes = [float(b.close) for b in bars if b.close is not None]
    return closes[-days:]


def _pct_change(a: float, b: float) -> float:
    if b == 0:
        return 0.0
    return (a - b) / b


def compute_regime(ib) -> dict:
    """
    Simple regime detector using SPY trend + VXX level.
    Output: {"regime": "risk_on|neutral|risk_off", "spy_5d":..., "vxx_20d":...}
    """
    lookback = int(getattr(config, "REGIME_LOOKBACK_DAYS", 20))

    spy = getattr(config, "REGIME_SPY_SYMBOL", "SPY")
    vxx = getattr(config, "REGIME_VXX_SYMBOL", "VXX")

    spy_closes = _daily_closes(ib, spy, lookback)
    vxx_closes = _daily_closes(ib, vxx, lookback)

    if len(spy_closes) < 6 or len(vxx_closes) < 6:
        return {"regime": "neutral", "spy_5d": 0.0, "vxx_vs_20d": 0.0}

    spy_5d = _pct_change(spy_closes[-1], spy_closes[-6])

    vxx_avg = sum(vxx_closes) / len(vxx_closes)
    vxx_vs_20d = _pct_change(vxx_closes[-1], vxx_avg)

    # Heuristics:
    # - Risk-off if SPY down AND VXX elevated
    # - Risk-on if SPY up AND VXX subdued
    if spy_5d < -0.01 and vxx_vs_20d > 0.05:
        regime = "risk_off"
    elif spy_5d > 0.01 and vxx_vs_20d < 0.03:
        regime = "risk_on"
    else:
        regime = "neutral"

    return {
        "regime": regime,
        "spy_5d": float(spy_5d),
        "vxx_vs_20d": float(vxx_vs_20d),
    }
