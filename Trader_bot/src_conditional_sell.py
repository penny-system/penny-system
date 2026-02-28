# src_conditional_sell.py
#
# Stock health evaluation engine for the conditional sell system.
#
# Called when a position reaches a price threshold (+50% first trigger,
# ±30% subsequent triggers). Evaluates 5 factors and returns a SELL/HOLD
# recommendation with a plain-English rationale.
#
# Usage:
#   from src_conditional_sell import evaluate_stock_health
#
import math
import os
import sys

_BOT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _BOT_DIR)

import config
from src_news import fetch_and_analyze_news, fetch_sec_risk, fetch_stocktwits_score


# ---------------------------------------------------------------------------
# IBKR bar fetcher
# ---------------------------------------------------------------------------

def _get_bars(ib, symbol: str) -> list:
    """Fetch today's 1-min bars for a symbol via the shared IBKR connection."""
    from ib_insync import Stock
    try:
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
        return bars or []
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Factor evaluators
# ---------------------------------------------------------------------------

def _eval_momentum(bars: list, current_price: float) -> dict:
    """Evaluate short-term price momentum from recent 1-min bars."""
    if len(bars) < 20:
        return {"signal": "neutral", "detail": "Insufficient bar data"}

    closes = [float(b.close) for b in bars if b.close and float(b.close) > 0]
    if len(closes) < 20:
        return {"signal": "neutral", "detail": "No close data available"}

    day_high = max(closes)
    day_low  = min(closes)

    # ret_1: last 60 mins vs prior 60 mins
    recent = closes[-60:] if len(closes) >= 60 else closes
    prior  = closes[-120:-60] if len(closes) >= 120 else closes[:max(1, len(closes) // 2)]
    ret_1  = (recent[-1] - prior[-1]) / prior[-1] if prior and prior[-1] > 0 else 0.0

    # Position in day range
    day_range = day_high - day_low
    pos_in_range = (current_price - day_low) / day_range if day_range > 0 else 0.5
    near_high = pos_in_range >= 0.70

    # Intraday trend: first third vs last third
    n = len(closes)
    first_third = closes[:n // 3] if n >= 3 else closes
    last_third  = closes[2 * n // 3:] if n >= 3 else closes
    trend_up = last_third[-1] > first_third[0] if first_third and last_third else False

    if ret_1 > 0.02 or (near_high and trend_up):
        detail = f"+{ret_1*100:.1f}% last hour" + (", near day high" if near_high else "")
        return {"signal": "bullish", "detail": detail}
    elif ret_1 < -0.02 or (not trend_up and ret_1 < 0):
        detail = f"{ret_1*100:.1f}% last hour, pulling back from high"
        return {"signal": "bearish", "detail": detail}
    else:
        return {"signal": "neutral", "detail": f"{ret_1*100:+.1f}% last hour, flat momentum"}


def _eval_volume(bars: list) -> dict:
    """Evaluate current volume profile vs average 60-min window."""
    if len(bars) < 60:
        return {"signal": "normal", "detail": "Insufficient bar data for volume analysis"}

    vols = [float(b.volume) for b in bars
            if hasattr(b, "volume") and b.volume is not None and float(b.volume) >= 0]
    if not vols or sum(vols) == 0:
        return {"signal": "normal", "detail": "Volume data unavailable"}

    recent_vols    = vols[-60:]
    recent_total   = sum(recent_vols)
    # average 60-min window across the full day
    avg_window_vol = sum(vols) / max(1, len(vols) / 60)
    vol_ratio      = recent_total / avg_window_vol if avg_window_vol > 0 else 1.0

    if vol_ratio >= 2.0:
        return {"signal": "strong", "detail": f"{vol_ratio:.1f}x volume surge — strong buyer interest"}
    elif vol_ratio <= 0.5:
        return {"signal": "fading", "detail": f"{vol_ratio:.2f}x normal — volume drying out"}
    else:
        return {"signal": "normal", "detail": f"{vol_ratio:.1f}x normal volume"}


def _eval_risk(symbol: str, bars: list) -> dict:
    """Re-evaluate risk from live data: SEC, StockTwits, realized volatility."""
    risk_signals = []
    signal = "low_risk"

    # SEC EDGAR — new filings since entry?
    try:
        sec = fetch_sec_risk(symbol)
        if sec.get("sec_trigger_a"):
            risk_signals.append("SEC Trigger A filing detected (dilution risk)")
            signal = "high_risk"
        elif sec.get("sec_hits", 0) >= 2:
            risk_signals.append("Multiple SEC filings detected")
            if signal == "low_risk":
                signal = "elevated"
    except Exception:
        pass

    # StockTwits — pump signal increasing?
    try:
        st_score = fetch_stocktwits_score(symbol)
        if st_score >= 75:
            risk_signals.append(f"High pump signal ({st_score:.0f}/100 on StockTwits)")
            if signal == "low_risk":
                signal = "elevated"
    except Exception:
        pass

    # Realized volatility from current bars
    if bars and len(bars) >= 30:
        try:
            closes = [float(b.close) for b in bars if b.close and float(b.close) > 0]
            if len(closes) >= 30:
                log_returns = [
                    math.log(closes[i] / closes[i - 1])
                    for i in range(1, len(closes))
                    if closes[i - 1] > 0
                ]
                if log_returns:
                    variance     = sum(x ** 2 for x in log_returns) / len(log_returns)
                    realized_vol = math.sqrt(variance) * math.sqrt(390 * 252)
                    if realized_vol > 3.0:  # > 300% annualised
                        risk_signals.append(f"Extreme realized vol ({realized_vol * 100:.0f}% ann.)")
                        if signal == "low_risk":
                            signal = "elevated"
        except Exception:
            pass

    if not risk_signals:
        return {"signal": "low_risk", "detail": "No new risk flags detected"}
    return {"signal": signal, "detail": "; ".join(risk_signals)}


def _eval_news(symbol: str) -> dict:
    """Fetch fresh news and determine sentiment signal."""
    try:
        result     = fetch_and_analyze_news(symbol)
        trigger_a  = result.get("trigger_a_hits", [])
        risk_hits  = result.get("risk_hits", [])
        pos_hits   = result.get("positive_hits", [])

        if trigger_a:
            return {"signal": "negative", "detail": f"Risk headline: {str(trigger_a[0])[:80]}"}
        if risk_hits and not pos_hits:
            return {"signal": "negative", "detail": f"Negative news: {str(risk_hits[0])[:80]}"}
        if pos_hits and not risk_hits:
            return {"signal": "positive", "detail": f"Positive catalyst: {str(pos_hits[0])[:80]}"}
        return {"signal": "no_news", "detail": "No material news"}
    except Exception:
        return {"signal": "no_news", "detail": "News data unavailable"}


def _eval_technical(bars: list, current_price: float, direction: str) -> dict:
    """Evaluate technical position within the day's range."""
    if not bars or len(bars) < 20:
        return {"signal": "weakening", "detail": "Insufficient bar data"}

    closes = [float(b.close) for b in bars if b.close and float(b.close) > 0]
    if not closes:
        return {"signal": "weakening", "detail": "No close data"}

    day_high  = max(closes)
    day_low   = min(closes)
    day_range = day_high - day_low

    if day_range == 0:
        return {"signal": "neutral", "detail": "No intraday range"}

    pos_in_range  = (current_price - day_low) / day_range
    breakout_active = pos_in_range >= 0.80

    # Trend: first quarter vs last quarter
    n = len(closes)
    first_q = closes[:n // 4] if n >= 4 else closes
    last_q  = closes[3 * n // 4:] if n >= 4 else closes
    trend_up = last_q[-1] > first_q[0] if first_q and last_q else None

    if direction == "DOWN":
        # Assess whether this is a pullback in an uptrend or a breakdown
        if pos_in_range >= 0.40 and trend_up is not False:
            return {"signal": "weakening",
                    "detail": f"Price at {pos_in_range*100:.0f}% of day range — potential pullback"}
        else:
            return {"signal": "broken",
                    "detail": f"Price near day lows ({pos_in_range*100:.0f}% of range) — breakdown signal"}
    else:
        if breakout_active:
            return {"signal": "strong",
                    "detail": f"Price in top {(1 - pos_in_range)*100:.0f}% of day range — breakout intact"}
        elif pos_in_range >= 0.50:
            return {"signal": "weakening",
                    "detail": f"Price at {pos_in_range*100:.0f}% of range — pulling back from high"}
        else:
            return {"signal": "broken",
                    "detail": f"Price near day lows ({pos_in_range*100:.0f}% of range)"}


# ---------------------------------------------------------------------------
# Scoring and decision
# ---------------------------------------------------------------------------

def _compute_hold_score(factors: dict) -> int:
    """
    Synthesize 5 factor signals into a numeric hold_score.

    Range: -12 to +8
      Momentum: BULLISH +2 | NEUTRAL 0 | BEARISH -2
      Volume:   STRONG  +2 | NORMAL  0 | FADING  -2
      Risk:     LOW_RISK+1 | ELEVATED-1 | HIGH_RISK -3
      News:     POSITIVE+1 | NO_NEWS  0 | NEGATIVE  -2
      Technical:STRONG  +2 | WEAKENING-1 | BROKEN   -3
    """
    momentum_map = {"bullish": +2, "neutral": 0, "bearish": -2}
    volume_map   = {"strong":  +2, "normal":  0, "fading":  -2}
    risk_map     = {"low_risk":+1, "elevated":-1, "high_risk":-3}
    news_map     = {"positive":+1, "no_news":  0, "negative": -2}
    tech_map     = {"strong":  +2, "weakening":-1, "broken":  -3, "neutral": 0}

    score  = momentum_map.get(factors["momentum"]["signal"], 0)
    score += volume_map.get(factors["volume"]["signal"],     0)
    score += risk_map.get(factors["risk"]["signal"],         0)
    score += news_map.get(factors["news"]["signal"],         0)
    score += tech_map.get(factors["technical"]["signal"],    0)
    return score


def _hold_score_to_recommendation(hold_score: int) -> tuple:
    """Convert hold_score to (recommendation, confidence)."""
    abs_score = abs(hold_score)

    if abs_score >= 6:
        confidence = 0.85 + min(0.10, (abs_score - 6) * 0.02)
    elif abs_score >= 3:
        confidence = 0.65 + (abs_score - 3) * 0.065
    else:
        confidence = 0.50 + abs_score * 0.05

    confidence = round(min(0.95, confidence), 2)

    if hold_score >= 2:
        return "HOLD", confidence
    else:
        # Includes -1 to +1 neutral zone — when in doubt, take profits
        return "SELL", confidence


def _build_rationale(recommendation: str, factors: dict,
                     gain_pct: float, direction: str) -> str:
    """Assemble a plain-English rationale string from factor signals."""
    gain_str = f"+{gain_pct*100:.1f}%" if gain_pct >= 0 else f"{gain_pct*100:.1f}%"

    positives = []
    negatives = []

    if factors["momentum"]["signal"] == "bullish":
        positives.append(factors["momentum"]["detail"])
    elif factors["momentum"]["signal"] == "bearish":
        negatives.append(factors["momentum"]["detail"])

    if factors["volume"]["signal"] == "strong":
        positives.append(factors["volume"]["detail"])
    elif factors["volume"]["signal"] == "fading":
        negatives.append(factors["volume"]["detail"])

    if factors["risk"]["signal"] in ("elevated", "high_risk"):
        negatives.append(factors["risk"]["detail"])

    if factors["news"]["signal"] == "positive":
        positives.append(factors["news"]["detail"])
    elif factors["news"]["signal"] == "negative":
        negatives.append(factors["news"]["detail"])

    if factors["technical"]["signal"] == "strong":
        positives.append(factors["technical"]["detail"])
    elif factors["technical"]["signal"] in ("weakening", "broken"):
        negatives.append(factors["technical"]["detail"])

    if recommendation == "HOLD":
        parts = positives if positives else ["Conditions remain supportive"]
        if negatives:
            parts.append(f"Note: {negatives[0]}")
        parts.append(f"Conditions support further upside at {gain_str}.")
        return " ".join(parts)
    else:
        if negatives:
            reason = ". ".join(negatives[:2])
        else:
            reason = "Marginal conditions — insufficient conviction to hold"
        return f"{reason}. Recommend locking in your {gain_str} gain."


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def evaluate_stock_health(ib, conn, symbol: str, entry_price: float,
                          current_price: float, direction: str) -> dict:
    """
    Comprehensive stock health evaluation at the time of a conditional sell trigger.

    Parameters
    ----------
    ib            : connected ib_insync IB instance (shared FillMonitor connection)
    conn          : sqlite3 connection (may be used for historical context)
    symbol        : ticker symbol
    entry_price   : original fill price
    current_price : current market price at trigger time
    direction     : 'UP' or 'DOWN'

    Returns
    -------
    {
        "recommendation": "SELL" or "HOLD",
        "confidence":     float 0.0-1.0,
        "rationale":      str,
        "hold_score":     int,
        "factors": {
            "momentum":  {"signal": ..., "detail": ...},
            "volume":    {"signal": ..., "detail": ...},
            "risk":      {"signal": ..., "detail": ...},
            "news":      {"signal": ..., "detail": ...},
            "technical": {"signal": ..., "detail": ...},
        }
    }
    """
    gain_pct = (current_price - entry_price) / entry_price if entry_price else 0.0

    # Fetch bars once — reused by momentum, volume, risk, and technical
    bars = _get_bars(ib, symbol)

    factors = {
        "momentum":  _eval_momentum(bars, current_price),
        "volume":    _eval_volume(bars),
        "risk":      _eval_risk(symbol, bars),
        "news":      _eval_news(symbol),
        "technical": _eval_technical(bars, current_price, direction),
    }

    hold_score     = _compute_hold_score(factors)
    recommendation, confidence = _hold_score_to_recommendation(hold_score)
    rationale      = _build_rationale(recommendation, factors, gain_pct, direction)

    return {
        "recommendation": recommendation,
        "confidence":     confidence,
        "rationale":      rationale,
        "hold_score":     hold_score,
        "factors":        factors,
    }
