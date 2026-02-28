# src_trade_monitor.py
#
# Safety monitor: detects open positions that have breached the stop-loss
# threshold but whose stop order may not have triggered (e.g. expired DAY
# orders, TWS disconnect during session, or bracket order placement failure).
#
# This module does NOT place orders automatically.  It alerts only.
#
# Usage:
#   Imported and called from main_daily_run.py after each scan.
#   Can also be run standalone:  python src_trade_monitor.py
#
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, time as dt_time, timezone, timedelta

_BOT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _BOT_DIR)

import config

LOG_PATH = os.path.join(_BOT_DIR, "output", "stop_loss_alerts.log")

# Default drawdown threshold matching config.STOP_LOSS_PCT
DEFAULT_THRESHOLD = float(getattr(config, "STOP_LOSS_PCT", 0.15))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _send_telegram(msg: str):
    """Send a Telegram message.  Fail-silent."""
    token = getattr(config, "TELEGRAM_BOT_TOKEN", None) or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = getattr(config, "TELEGRAM_CHAT_ID", None) or os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": str(chat_id), "text": msg}).encode("utf-8")
        urllib.request.urlopen(
            urllib.request.Request(url, data=data, method="POST"), timeout=10
        )
    except Exception as e:
        print(f"[MONITOR] Telegram send failed: {e}")


def _log_alert(sym: str, avg_cost: float, current_price: float, drawdown: float):
    """Append one line to the stop-loss alert log."""
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = (
        f"{ts}  {sym:<6}  entry=${avg_cost:.4f}  "
        f"current=${current_price:.4f}  drawdown={drawdown*100:.1f}%\n"
    )
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception as e:
        print(f"[MONITOR] Log write failed: {e}")


def _best_price(ticker) -> float:
    """Return the most reliable price available from a Ticker object."""
    for attr in ("last", "close", "bid"):
        val = getattr(ticker, attr, None)
        try:
            v = float(val)
            if v == v and v > 0:   # not NaN, not zero
                return v
        except (TypeError, ValueError):
            continue
    return 0.0


def is_market_hours() -> bool:
    """
    Return True if the current time is within US equity market hours
    (Mon–Fri, 09:30–16:00 US Eastern).

    Uses zoneinfo (stdlib in Python 3.9+) with a UTC-4 fallback.
    """
    try:
        from zoneinfo import ZoneInfo
        now_et = datetime.now(tz=ZoneInfo("America/New_York"))
    except Exception:
        # Fallback: approximate EDT (UTC-4); safe for most of the trading year
        now_et = datetime.now(tz=timezone(timedelta(hours=-4)))

    if now_et.weekday() > 4:          # Saturday=5, Sunday=6
        return False

    t = now_et.time()
    return dt_time(9, 30) <= t <= dt_time(16, 0)


# ---------------------------------------------------------------------------
# Main monitor function
# ---------------------------------------------------------------------------

def monitor_stop_loss(ib, conn=None, drawdown_threshold: float = None) -> list:
    """
    Check all open IBKR positions for breached stop-loss levels.

    For each position where:
        drawdown = (current_price - avg_cost) / avg_cost <= -drawdown_threshold

    the function:
      1. Logs the event to output/stop_loss_alerts.log
      2. Sends an urgent Telegram alert asking for immediate review

    Parameters
    ----------
    ib                  : connected ib_insync IB instance
    conn                : sqlite3 connection (optional, not currently used)
    drawdown_threshold  : fractional threshold (default: config.STOP_LOSS_PCT = 0.15)

    Returns
    -------
    List of dicts describing each alert that was fired.
    """
    if drawdown_threshold is None:
        drawdown_threshold = DEFAULT_THRESHOLD

    alerts = []

    # --- Fetch positions ---
    try:
        positions = ib.reqPositions()
        ib.sleep(1)
    except Exception as e:
        print(f"[MONITOR] reqPositions failed: {e}")
        return alerts

    active = [p for p in positions if int(p.position or 0) != 0]
    if not active:
        print("[MONITOR] No open positions to check.")
        return alerts

    print(f"[MONITOR] Checking {len(active)} position(s) for stop-loss breach ...")

    # --- Fetch current prices for all contracts in one batch ---
    contracts = [p.contract for p in active]
    try:
        tickers = ib.reqTickers(*contracts)
        ib.sleep(2)
    except Exception as e:
        print(f"[MONITOR] reqTickers failed: {e}")
        return alerts

    # --- Evaluate each position ---
    for pos, ticker in zip(active, tickers):
        sym = pos.contract.symbol.upper()
        avg_cost = float(pos.avgCost or 0)

        if avg_cost <= 0:
            print(f"[MONITOR] {sym}: avgCost unavailable — skipping.")
            continue

        current_price = _best_price(ticker)
        if current_price <= 0:
            print(f"[MONITOR] {sym}: current price unavailable — skipping.")
            continue

        drawdown = (current_price - avg_cost) / avg_cost
        print(
            f"[MONITOR] {sym}: entry=${avg_cost:.4f}  "
            f"current=${current_price:.4f}  drawdown={drawdown*100:.1f}%"
        )

        if drawdown <= -drawdown_threshold:
            pct = abs(drawdown * 100)
            msg = (
                f"🚨 STOP ALERT: {sym} is down {pct:.1f}% "
                f"(entry ${avg_cost:.2f} → current ${current_price:.2f}). "
                f"Stop-loss may not have triggered. Immediate review required."
            )
            print(f"[MONITOR] *** {msg}")
            _log_alert(sym, avg_cost, current_price, drawdown)
            _send_telegram(msg)
            alerts.append({
                "symbol": sym,
                "avg_cost": avg_cost,
                "current_price": current_price,
                "drawdown": drawdown,
            })

    if not alerts:
        print("[MONITOR] All positions within stop-loss threshold.")

    return alerts


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from src_ibkr_client import connect_ib

    if not is_market_hours():
        print("[MONITOR] Outside market hours. Run with --force to override.")
        if "--force" not in sys.argv:
            sys.exit(0)

    print("[MONITOR] Connecting to IBKR ...")
    ib = connect_ib()
    try:
        monitor_stop_loss(ib)
    finally:
        ib.disconnect()
        print("[MONITOR] Done.")
