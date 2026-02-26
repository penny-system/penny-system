# src_format.py
# Shared Telegram HTML formatting helpers used by notify_scan.py and telegram_bot.py.
import re
from datetime import datetime

DIVIDER = "━━━━━━━━━━━━━━"


def _fmt_date() -> str:
    now = datetime.now()
    return now.strftime("%a, %b ") + str(now.day)


def _clean_rationale(raw: str) -> str:
    """
    Convert a rationale string to clean, readable text.
    Handles both the new human-readable format and legacy key=value format.
    """
    if not raw or raw in ("normalized_from_string", "Scan criteria met."):
        return ""

    # New format — already readable (no key=value pairs)
    if "=" not in raw:
        return raw[:120].rstrip(".")

    # Legacy format: "setup_type | horizon: ret_1=X%, vol_surge=Xx, ..."
    parts = []
    horizon = "momentum" if "momentum" in raw else "swing"

    ret_m   = re.search(r"ret_\d=(-?[\d.]+)%", raw)
    vol_m   = re.search(r"vol_surge=([\d.]+)x", raw)
    brk_m   = re.search(r"breakout=(\d)", raw)
    dvol_m  = re.search(r"dollar_vol=([\d,]+)", raw)
    conf_m  = re.search(r"confirmed=(\d)", raw)

    ret       = float(ret_m.group(1))   if ret_m   else 0.0
    vol       = float(vol_m.group(1))   if vol_m   else 1.0
    brk       = int(brk_m.group(1))     if brk_m   else 0
    dvol      = int(dvol_m.group(1).replace(",", "")) if dvol_m else 0
    confirmed = int(conf_m.group(1))    if conf_m  else 0

    label = "Short-term" if horizon == "momentum" else "5-day"
    sign  = "+" if ret > 0 else ""
    parts.append(f"{label} move {sign}{ret:.1f}%")

    if vol >= 2.0:
        parts.append(f"volume surge {vol:.1f}x normal")
    elif vol >= 1.2:
        parts.append(f"above-average volume ({vol:.1f}x)")

    if brk:
        parts.append("price breakout confirmed" if confirmed else "price breakout unconfirmed")

    if dvol >= 50_000_000:
        parts.append("high liquidity ($50M+ daily)")
    elif dvol >= 10_000_000:
        parts.append("strong liquidity ($10M+ daily)")
    elif dvol >= 2_000_000:
        parts.append(f"${dvol/1_000_000:.0f}M daily volume")

    return ". ".join(parts) if parts else ""


def fmt_candidate(r: dict, gate_note: str = None, has_news_risk: bool = False, emoji: str = "📈") -> str:
    """
    Format a single BUY candidate as an HTML block.

    r keys: symbol, score_total, confidence, risk_score, ref_price, stop_price,
            take_price, suggested_qty, rationale (opt), gate_decision (opt),
            gate_confidence (opt), gate_reason (opt)

    gate_note: pre-formatted gate string (overrides gate_* fields in r).
    has_news_risk: show ⚠️ flag next to symbol.
    """
    sym   = str(r.get("symbol", "")).upper()
    score = float(r.get("score_total", 0) or 0)
    conf  = float(r.get("confidence",  0) or 0)
    risk  = float(r.get("risk_score",  0) or 0)
    refp  = float(r.get("ref_price",   0) or 0)
    stop  = float(r.get("stop_price",  0) or 0)
    take  = float(r.get("take_price",  0) or 0)
    qty   = int(r.get("suggested_qty", 0) or 0)
    raw_rationale = (r.get("rationale") or "").strip()

    # Gate from pre-computed note OR from DB columns
    if gate_note:
        _gate_line = gate_note
    else:
        gd  = (r.get("gate_decision")   or "").strip()
        gc  = float(r.get("gate_confidence") or 0)
        gr  = (r.get("gate_reason")     or "").strip()
        if gd:
            pct = f" ({gc*100:.0f}%)" if gc else ""
            reason = f" — {gr[:80]}" if gr else ""
            _gate_line = f"🛡 Gate: <b>{gd}</b>{pct}{reason}"
        else:
            _gate_line = ""

    risk_flag = "  ⚠️" if (has_news_risk or _gate_line) else ""

    lines = [
        f"{emoji} <b>{sym}</b>{risk_flag}",
        f"Score <b>{score:.1f}</b>  ·  Conf {conf*100:.0f}%  ·  Risk {risk:.2f}",
        f"Ref <b>${refp:.2f}</b>  ·  Stop ${stop:.2f}  ·  Take ${take:.2f}",
        f"Qty: {qty} shares",
    ]

    if _gate_line:
        lines.append(_gate_line)

    why = _clean_rationale(raw_rationale)
    if why:
        lines.append(f"<i>Why: {why}</i>")

    return "\n".join(lines)


def fmt_watch_candidate(r: dict) -> str:
    """
    Format a single WATCH candidate as an HTML block (📊, no stop/take/qty).
    """
    sym   = str(r.get("symbol", "")).upper()
    score = float(r.get("score_total", 0) or 0)
    conf  = float(r.get("confidence",  0) or 0)
    risk  = float(r.get("risk_score",  0) or 0)
    refp  = float(r.get("ref_price",   0) or 0)
    raw_rationale = (r.get("rationale") or "").strip()

    lines = [
        f"📊 <b>{sym}</b>",
        f"Score <b>{score:.1f}</b>  ·  Conf {conf*100:.0f}%  ·  Risk {risk:.2f}",
        f"Ref <b>${refp:.2f}</b>",
    ]

    why = _clean_rationale(raw_rationale)
    if why:
        lines.append(f"<i>Why: {why}</i>")

    return "\n".join(lines)


def fmt_brief_message(buy_recs: list[dict], watch_recs: list[dict],
                      purse: float, max_positions: int,
                      active_positions: int = 0, invested: float = 0) -> str:
    """
    Build the complete /brief HTML message: BUY candidates + WATCHLIST.
    """
    lines = [
        f"📋 <b>Brief — {_fmt_date()}</b>",
        f"💰 Purse: ${purse:,.0f} | Invested: ${invested:,.0f} | Available: ${max(0, purse - invested):,.0f}",
        f"📊 Positions: {active_positions}/{max_positions} active",
    ]

    if buy_recs:
        lines.append("")
        lines.append(f"🎆 <b>{len(buy_recs)} BUY Candidate{'s' if len(buy_recs) != 1 else ''}</b>")
        for r in buy_recs:
            lines.append("")
            lines.append(DIVIDER)
            lines.append(fmt_candidate(r))
    else:
        lines.append("\nNo BUY candidates in this run.")

    if watch_recs:
        lines.append("")
        lines.append(f"👀 <b>{len(watch_recs)} Watchlist</b>")
        for r in watch_recs:
            lines.append("")
            lines.append(DIVIDER)
            lines.append(fmt_watch_candidate(r))

    lines.append("")
    lines.append(DIVIDER)
    lines.append("/approve SYMBOL — place a bracket order")
    lines.append("/skip SYMBOL — dismiss for this run")

    return "\n".join(lines)


def fmt_buy_message(recs: list[dict], purse: float, max_positions: int,
                    gate_notes: dict = None, news_risk_syms: set = None,
                    active_positions: int = 0, invested: float = 0) -> str:
    """
    Build the complete HTML BUY candidate message.

    gate_notes:     {symbol: gate_note_string} — live gate results from notify_scan loop
    news_risk_syms: set of symbols with news risk (⚠️ flag)
    """
    gate_notes     = gate_notes     or {}
    news_risk_syms = news_risk_syms or set()
    count = len(recs)

    lines = [
        f"🎆 <b>{count} BUY Candidate{'s' if count != 1 else ''} — {_fmt_date()}</b>",
        f"💰 Purse: ${purse:,.0f} | Invested: ${invested:,.0f} | Available: ${max(0, purse - invested):,.0f}",
        f"📊 Positions: {active_positions}/{max_positions} active",
    ]

    for r in recs:
        sym = str(r.get("symbol", "")).upper()
        lines.append("")
        lines.append(DIVIDER)
        lines.append(fmt_candidate(
            r,
            gate_note=gate_notes.get(sym),
            has_news_risk=(sym in news_risk_syms),
        ))

    lines.append("")
    lines.append(DIVIDER)
    lines.append("/approve SYMBOL — place a bracket order")
    lines.append("/skip SYMBOL — dismiss for this run")

    return "\n".join(lines)
