# src_trade_tracker.py
#
# Trade lifecycle tracker and safety monitor.
#
# Responsibilities:
#   1. Absorbs src_trade_monitor.py (stop-loss safety alerts, is_market_hours)
#   2. FillMonitor — listens for IBKR execDetails events in a background thread,
#      writes entry fills to open_positions and exit fills to closed_trades
#   3. Periodic reconciliation — catches missed events every 5 minutes
#   4. Exit notifications — sends Telegram alert on trade close with P&L summary
#
# Usage:
#   From main_daily_run.py:
#       from src_trade_tracker import monitor_stop_loss, is_market_hours
#   From telegram_bot.py:
#       from src_trade_tracker import start_fill_monitor
#   Standalone:
#       python src_trade_tracker.py [--force]
#
import os
import sys
import queue
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, time as dt_time, timezone, timedelta

_BOT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _BOT_DIR)

import config
from src_storage import (connect_db, ensure_schema,
                         insert_open_position, close_position,
                         get_open_position_by_exec_id, get_open_positions_all,
                         get_snapshot_id)

LOG_PATH     = os.path.join(_BOT_DIR, "output", "stop_loss_alerts.log")
TRACKER_LOG  = os.path.join(_BOT_DIR, "output", "trade_tracker.log")

_DEFAULT_THRESHOLD  = float(getattr(config, "STOP_LOSS_PCT",           0.15))
_WARNING_THRESHOLD  = abs(float(getattr(config, "STOP_WARNING_THRESHOLD_PCT", -0.10)))

# Symbols known to appear as phantom positions on IBKR paper accounts — skip in reconciliation
_PHANTOM_SYMBOLS: set[str] = {"AAPL"}

# Track symbols that have already received a -10% warning this session (resets on restart)
_warned_symbols: set[str] = set()


# ---------------------------------------------------------------------------
# Helpers — shared with monitor functions
# ---------------------------------------------------------------------------

def _send_telegram(msg: str, parse_mode: str = ""):
    """Send a Telegram message. Fail-silent."""
    token   = getattr(config, "TELEGRAM_BOT_TOKEN", None) or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = getattr(config, "TELEGRAM_CHAT_ID",   None) or os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    try:
        url    = f"https://api.telegram.org/bot{token}/sendMessage"
        params = {"chat_id": str(chat_id), "text": msg}
        if parse_mode:
            params["parse_mode"] = parse_mode
        data = urllib.parse.urlencode(params).encode("utf-8")
        urllib.request.urlopen(
            urllib.request.Request(url, data=data, method="POST"), timeout=10
        )
    except Exception as e:
        _tracker_log(f"Telegram send failed: {e}")


def _tracker_log(msg: str):
    """Append a timestamped line to trade_tracker.log."""
    os.makedirs(os.path.dirname(TRACKER_LOG), exist_ok=True)
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts}  {msg}\n"
    try:
        with open(TRACKER_LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass
    print(f"[TRACKER] {msg}")


def _log_alert(sym: str, avg_cost: float, current_price: float, drawdown: float):
    """Append one line to the stop-loss alert log."""
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = (
        f"{ts}  {sym:<6}  entry=${avg_cost:.4f}  "
        f"current=${current_price:.4f}  drawdown={drawdown*100:.1f}%\n"
    )
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception as e:
        _tracker_log(f"Log write failed: {e}")


def _best_price(ticker) -> float:
    """Return the most reliable price from a Ticker object."""
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
    """
    try:
        from zoneinfo import ZoneInfo
        now_et = datetime.now(tz=ZoneInfo("America/New_York"))
    except Exception:
        now_et = datetime.now(tz=timezone(timedelta(hours=-4)))

    if now_et.weekday() > 4:
        return False
    t = now_et.time()
    return dt_time(9, 30) <= t <= dt_time(16, 0)


def _parse_ibkr_time(ibkr_time_str: str) -> str:
    """
    Parse an IBKR execution time string into a UTC ISO 8601 string.
    IBKR format: "20240101 10:30:45 US/Eastern" or "20240101 10:30:45"
    Returns: "2024-01-01T15:30:45+00:00" (UTC)
    """
    if not ibkr_time_str:
        return datetime.utcnow().isoformat() + "+00:00"
    try:
        parts = ibkr_time_str.strip().split()
        date_part = parts[0]   # "20240101"
        time_part = parts[1] if len(parts) > 1 else "00:00:00"
        tz_label  = parts[2] if len(parts) > 2 else ""

        dt_naive = datetime.strptime(f"{date_part} {time_part}", "%Y%m%d %H:%M:%S")

        if "Eastern" in tz_label or "ET" in tz_label:
            # Approximate: use EST (UTC-5). DST not resolved for simplicity.
            offset = timedelta(hours=-5)
        elif "Central" in tz_label or "CT" in tz_label:
            offset = timedelta(hours=-6)
        else:
            offset = timedelta(0)   # assume UTC

        dt_aware = dt_naive.replace(tzinfo=timezone(offset))
        dt_utc   = dt_aware.astimezone(timezone.utc)
        return dt_utc.isoformat()
    except Exception:
        return datetime.utcnow().isoformat() + "+00:00"


def _estimate_commission(fill_price: float, fill_qty: int) -> float:
    """
    Estimate commission when IBKR reports $0 (common in paper trading).
    $0.0035/share, min $0.35, max 1% of trade value.
    """
    per_share = float(getattr(config, "COMMISSION_PER_SHARE", 0.0035)) * fill_qty
    min_comm  = float(getattr(config, "COMMISSION_MIN", 0.35))
    max_pct   = float(getattr(config, "COMMISSION_MAX_PCT", 0.01))
    max_comm  = max_pct * fill_price * fill_qty
    return max(min_comm, min(per_share, max_comm))


def _determine_exit_reason(exit_price: float, stop_price: float, take_price: float) -> str:
    """Infer exit reason from exit price relative to bracket levels."""
    if take_price and exit_price >= take_price * 0.97:
        return "take_profit"
    if stop_price and exit_price <= stop_price * 1.03:
        return "stop_loss"
    return "manual"


def _compute_outcome(net_pnl: float) -> str:
    if net_pnl > 0.005:
        return "WIN"
    if net_pnl < -0.005:
        return "LOSS"
    return "BREAKEVEN"


# ---------------------------------------------------------------------------
# Stop-loss safety monitor (absorbed from src_trade_monitor)
# ---------------------------------------------------------------------------

def monitor_stop_loss(ib, conn=None, drawdown_threshold: float = None) -> list:
    """
    Check all open IBKR positions for breached stop-loss levels.
    Logs and sends Telegram alert for each breach. Does NOT place orders.

    Parameters
    ----------
    ib                  : connected ib_insync IB instance
    conn                : sqlite3 connection (optional)
    drawdown_threshold  : fractional threshold (default: config.STOP_LOSS_PCT)
    """
    if drawdown_threshold is None:
        drawdown_threshold = _DEFAULT_THRESHOLD

    alerts = []

    try:
        positions = ib.reqPositions()
        ib.sleep(1)
    except Exception as e:
        _tracker_log(f"reqPositions failed: {e}")
        return alerts

    active = [p for p in positions if int(p.position or 0) != 0]
    if not active:
        print("[MONITOR] No open positions to check.")
        return alerts

    print(f"[MONITOR] Checking {len(active)} position(s) for stop-loss breach ...")

    contracts = [p.contract for p in active]
    try:
        tickers = ib.reqTickers(*contracts)
        ib.sleep(2)
    except Exception as e:
        _tracker_log(f"reqTickers failed: {e}")
        return alerts

    # Load DB stop prices for cross-reference
    db_stop_prices: dict[str, float] = {}
    if conn is not None:
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT UPPER(TRIM(symbol)), stop_price FROM open_positions WHERE status='open'"
            )
            db_stop_prices = {row[0]: float(row[1] or 0.0) for row in cur.fetchall()}
        except Exception:
            pass

    for pos, ticker in zip(active, tickers):
        sym      = pos.contract.symbol.upper()
        avg_cost = float(pos.avgCost or 0)

        if avg_cost <= 0:
            print(f"[MONITOR] {sym}: avgCost unavailable — skipping.")
            continue

        current_price = _best_price(ticker)
        if current_price <= 0:
            print(f"[MONITOR] {sym}: current price unavailable — skipping.")
            continue

        drawdown  = (current_price - avg_cost) / avg_cost
        stop_price = db_stop_prices.get(sym, 0.0)
        stop_note  = f" Expected stop: ${stop_price:.2f} — NOT TRIGGERED" if stop_price and current_price < stop_price else ""

        print(
            f"[MONITOR] {sym}: entry=${avg_cost:.4f}  "
            f"current=${current_price:.4f}  drawdown={drawdown*100:.1f}%"
        )

        if drawdown <= -drawdown_threshold:
            pct = abs(drawdown * 100)
            msg = (
                f"STOP ALERT: {sym} is down {pct:.1f}% "
                f"(entry ${avg_cost:.2f} -> current ${current_price:.2f}).{stop_note} "
                f"Immediate review required."
            )
            _tracker_log(f"*** {msg}")
            _log_alert(sym, avg_cost, current_price, drawdown)
            _send_telegram(msg)
            _warned_symbols.discard(sym)   # reset warning so it fires again if needed
            alerts.append({
                "symbol": sym, "avg_cost": avg_cost,
                "current_price": current_price, "drawdown": drawdown,
            })

        elif drawdown <= -_WARNING_THRESHOLD and sym not in _warned_symbols:
            pct = abs(drawdown * 100)
            away = f"  Stop at ${stop_price:.2f} (${current_price - stop_price:+.2f} away)" if stop_price else ""
            msg = (
                f"DRAWDOWN WARNING: {sym} is down {pct:.1f}% "
                f"(entry ${avg_cost:.2f} -> current ${current_price:.2f}).{away}"
            )
            _tracker_log(f"WARNING: {msg}")
            _send_telegram(msg)
            _warned_symbols.add(sym)

    if not alerts:
        print("[MONITOR] All positions within stop-loss threshold.")
    return alerts


# ---------------------------------------------------------------------------
# FillMonitor — background thread watching for IBKR fills
# ---------------------------------------------------------------------------

class FillMonitor:
    """
    Background thread that maintains a persistent IBKR connection (clientId=21)
    and listens for execDetailsEvent to record fills in real time.

    Entry fills (BOT) → insert_open_position()
    Exit fills  (SLD) → close_position() + Telegram exit notification
    Periodic reconciliation (every RECONCILIATION_INTERVAL_MINUTES) catches
    any fills that were missed due to disconnects.
    """

    def __init__(self, client_id: int = None):
        self._client_id  = client_id or int(getattr(config, "FILL_MONITOR_CLIENT_ID", 21))
        self._stop_event = threading.Event()
        self._fill_queue = queue.Queue()
        self._ib         = None
        self._thread     = None

    def start(self) -> "FillMonitor":
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="FillMonitor"
        )
        self._thread.start()
        return self

    def stop(self):
        self._stop_event.set()

    # -- internal ----------------------------------------------------------

    def _run_loop(self):
        import asyncio
        from ib_insync import IB

        # ib_insync uses asyncio internally; daemon threads have no event loop by default
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        ib = IB()
        self._ib = ib

        try:
            ib.connect(
                getattr(config, "IB_HOST", "127.0.0.1"),
                int(getattr(config, "IB_PORT", 7497)),
                clientId=self._client_id,
                readonly=True,
            )
            _tracker_log(f"FillMonitor connected (clientId={self._client_id})")
        except Exception as e:
            _tracker_log(f"FillMonitor IBKR connection failed: {e}")
            return

        ib.execDetailsEvent      += self._on_exec_details
        ib.commissionReportEvent += self._on_commission_report

        interval_secs  = int(getattr(config, "RECONCILIATION_INTERVAL_MINUTES", 5)) * 60
        last_reconcile = 0.0

        while not self._stop_event.is_set():
            try:
                ib.sleep(1)   # drives the ib_insync event loop; 1-second tick
            except Exception as e:
                _tracker_log(f"FillMonitor ib.sleep error: {e}")
                break

            # Drain fill queue
            while not self._fill_queue.empty():
                try:
                    fill_data = self._fill_queue.get_nowait()
                    self._process_fill(fill_data)
                except queue.Empty:
                    break
                except Exception as e:
                    _tracker_log(f"Fill processing error: {e}")

            # Periodic reconciliation
            now = time.monotonic()
            if now - last_reconcile >= interval_secs:
                if is_market_hours():
                    try:
                        self._reconcile(ib)
                    except Exception as e:
                        _tracker_log(f"Reconciliation error: {e}")
                last_reconcile = now

        try:
            ib.disconnect()
        except Exception:
            pass
        _tracker_log("FillMonitor stopped.")

    def _on_exec_details(self, trade, fill):
        """ib_insync callback — enqueue fill data for DB processing."""
        try:
            commission = 0.0
            if fill.commissionReport and fill.commissionReport.commission:
                commission = float(fill.commissionReport.commission)

            self._fill_queue.put({
                "exec_id":    fill.execution.execId,
                "order_id":   fill.execution.orderId,
                "symbol":     fill.contract.symbol.upper(),
                "action":     fill.execution.side.upper(),   # "BOT" or "SLD"
                "fill_price": float(fill.execution.price),
                "fill_qty":   int(fill.execution.shares),
                "commission": commission,
                "filled_at":  fill.execution.time,
            })
        except Exception as e:
            _tracker_log(f"_on_exec_details error: {e}")

    def _on_commission_report(self, trade, fill, report):
        """
        ib_insync callback — fires when IBKR sends the async commission report.
        Updates the commission field on the matching open_positions or closed_trades row.
        """
        try:
            if not report or not report.execId:
                return
            commission = float(report.commission or 0.0)
            if commission <= 0:
                return
            exec_id = report.execId
            conn = connect_db()
            try:
                cur = conn.cursor()
                # Try open_positions first (entry fill)
                cur.execute(
                    "UPDATE open_positions SET commission=?, commission_source='ibkr' "
                    "WHERE ibkr_exec_id=?",
                    (commission, exec_id)
                )
                if cur.rowcount == 0:
                    # Try closed_trades (exit fill)
                    cur.execute(
                        "UPDATE closed_trades SET commission_total = commission_total - "
                        "(SELECT COALESCE(commission,0) FROM open_positions WHERE ibkr_exec_id=ibkr_exec_id_entry) "
                        "+ ? + ? WHERE ibkr_exec_id_exit=?",
                        (commission, 0, exec_id)
                    )
                conn.commit()
                _tracker_log(f"Commission updated: exec_id={exec_id} comm=${commission:.4f}")
            finally:
                conn.close()
        except Exception as e:
            _tracker_log(f"_on_commission_report error: {e}")

    def _process_fill(self, d: dict):
        """Write a fill to the database. Idempotent on exec_id for entries."""
        conn = None
        try:
            conn = connect_db()
            ensure_schema(conn)

            exec_id    = d["exec_id"]
            sym        = d["symbol"]
            action     = d["action"]   # "BOT" or "SLD"
            fill_price = d["fill_price"]
            fill_qty   = d["fill_qty"]
            commission = d["commission"]
            filled_at  = _parse_ibkr_time(d.get("filled_at", ""))

            # Estimate commission if IBKR reported $0 (common in paper trading)
            if commission <= 0:
                commission = _estimate_commission(fill_price, fill_qty)
                comm_source = "estimated"
            else:
                comm_source = "ibkr"

            if action in ("BOT", "BUY"):
                # --- Entry fill ---
                existing = get_open_position_by_exec_id(conn, exec_id)
                if existing:
                    _tracker_log(f"Entry fill already recorded: {sym} exec_id={exec_id}")
                    return

                # Try to link to most recent BUY snapshot for this symbol
                snapshot_id = self._find_latest_snapshot_id(conn, sym)

                # Look up bracket levels from snapshot if available
                ref_price  = 0.0
                stop_price = 0.0
                take_price = 0.0
                if snapshot_id:
                    cur = conn.cursor()
                    cur.execute(
                        "SELECT ref_price, stop_price, take_price FROM recommendation_snapshots "
                        "WHERE snapshot_id = ?", (snapshot_id,)
                    )
                    row = cur.fetchone()
                    if row:
                        ref_price, stop_price, take_price = row

                pos_id = insert_open_position(conn, {
                    "snapshot_id":       snapshot_id,
                    "symbol":            sym,
                    "ibkr_exec_id":      exec_id,
                    "ibkr_order_id":     d.get("order_id"),
                    "oca_group":         d.get("oca_group", ""),
                    "fill_price":        fill_price,
                    "fill_qty":          fill_qty,
                    "commission":        commission,
                    "commission_source": comm_source,
                    "ref_price":         ref_price,
                    "stop_price":        stop_price,
                    "take_price":        take_price,
                    "opened_at":         filled_at,
                })
                entry_slippage = fill_price - ref_price if ref_price else 0.0
                _tracker_log(
                    f"ENTRY recorded: {sym} pos_id={pos_id} "
                    f"fill=${fill_price:.4f} qty={fill_qty} "
                    f"slip=${entry_slippage:+.4f} comm=${commission:.2f}({comm_source})"
                )

            elif action in ("SLD", "SELL"):
                # --- Exit fill ---
                open_pos = self._find_open_position_by_symbol(conn, sym)
                if open_pos is None:
                    _tracker_log(f"Exit fill for {sym} but no matching open_position — skipping.")
                    return

                pos_id     = open_pos["position_id"]
                entry_price = open_pos["fill_price"]
                entry_qty   = open_pos["fill_qty"]
                stop_price  = open_pos["stop_price"]
                take_price  = open_pos["take_price"]
                ref_price   = open_pos["ref_price"]
                entry_comm  = open_pos["commission"]
                opened_at   = open_pos["opened_at"]

                gross_pnl   = (fill_price - entry_price) * fill_qty
                comm_total  = entry_comm + commission
                net_pnl     = gross_pnl - comm_total
                outcome     = _compute_outcome(net_pnl)
                exit_reason = _determine_exit_reason(fill_price, stop_price, take_price)

                # Hold time in seconds
                hold_seconds = 0
                try:
                    open_dt  = datetime.fromisoformat(opened_at)
                    close_dt = datetime.fromisoformat(filled_at)
                    hold_seconds = int((close_dt - open_dt).total_seconds())
                except Exception:
                    pass

                entry_slippage = (entry_price - ref_price) if ref_price else 0.0
                if exit_reason == "take_profit":
                    exit_slippage = fill_price - take_price if take_price else 0.0
                elif exit_reason == "stop_loss":
                    exit_slippage = fill_price - stop_price if stop_price else 0.0
                else:
                    exit_slippage = 0.0

                trade_id = close_position(conn, pos_id, {
                    "exit_price":       fill_price,
                    "exit_qty":         fill_qty,
                    "exit_reason":      exit_reason,
                    "gross_pnl":        gross_pnl,
                    "commission_exit":  commission,
                    "commission_source": comm_source,
                    "net_pnl":          net_pnl,
                    "outcome":          outcome,
                    "hold_seconds":     hold_seconds,
                    "closed_at":        filled_at,
                    "entry_slippage":   entry_slippage,
                    "exit_slippage":    exit_slippage,
                    "ibkr_exec_id_exit": exec_id,
                })

                _tracker_log(
                    f"EXIT recorded: {sym} trade_id={trade_id} "
                    f"exit=${fill_price:.4f} gross=${gross_pnl:+.2f} "
                    f"net=${net_pnl:+.2f} ({outcome}) reason={exit_reason}"
                )
                _warned_symbols.discard(sym)   # clear warning memory on close
                self._send_exit_notification(sym, trade_id, {
                    "entry_price": entry_price,
                    "exit_price":  fill_price,
                    "fill_qty":    fill_qty,
                    "gross_pnl":   gross_pnl,
                    "net_pnl":     net_pnl,
                    "outcome":     outcome,
                    "exit_reason": exit_reason,
                    "hold_seconds": hold_seconds,
                })

        except Exception as e:
            _tracker_log(f"_process_fill exception for {d.get('symbol','?')}: {e}")
        finally:
            if conn:
                conn.close()

    def _find_latest_snapshot_id(self, conn, symbol: str):
        """Return snapshot_id of the most recent BUY snapshot for this symbol."""
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT snapshot_id FROM recommendation_snapshots
                WHERE UPPER(TRIM(symbol)) = UPPER(TRIM(?))
                ORDER BY snapshot_id DESC LIMIT 1
            """, (symbol,))
            row = cur.fetchone()
            return row[0] if row else None
        except Exception:
            return None

    def _find_open_position_by_symbol(self, conn, symbol: str):
        """Return the most recent open position for a symbol, or None."""
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT position_id, snapshot_id, symbol, ibkr_exec_id, ibkr_order_id,
                       oca_group, fill_price, fill_qty, commission, commission_source,
                       ref_price, stop_price, take_price, opened_at, status
                FROM open_positions
                WHERE UPPER(TRIM(symbol)) = UPPER(TRIM(?)) AND status = 'open'
                ORDER BY position_id DESC LIMIT 1
            """, (symbol,))
            row = cur.fetchone()
            if row is None:
                return None
            keys = ["position_id", "snapshot_id", "symbol", "ibkr_exec_id", "ibkr_order_id",
                    "oca_group", "fill_price", "fill_qty", "commission", "commission_source",
                    "ref_price", "stop_price", "take_price", "opened_at", "status"]
            return dict(zip(keys, row))
        except Exception:
            return None

    def _reconcile(self, ib):
        """
        Compare today's IBKR executions against our open_positions table.
        Records any exit fills that were missed by the event subscription.
        """
        _tracker_log("Reconciliation starting ...")
        try:
            from ib_insync import ExecutionFilter
            execs = ib.reqExecutions(ExecutionFilter())
            ib.sleep(1)
        except Exception as e:
            _tracker_log(f"reqExecutions failed: {e}")
            return

        for fill in execs:
            try:
                action = fill.execution.side.upper()
                if action not in ("SLD", "SELL"):
                    continue   # only care about exit fills for reconciliation
                exec_id = fill.execution.execId
                sym     = fill.contract.symbol.upper()

                # Skip known phantom positions on paper accounts
                if sym in _PHANTOM_SYMBOLS:
                    _tracker_log(f"Reconciliation: skipping phantom symbol {sym}")
                    continue

                conn = connect_db()
                try:
                    # Check if this exit is already recorded
                    cur = conn.cursor()
                    cur.execute(
                        "SELECT trade_id FROM closed_trades WHERE ibkr_exec_id_exit = ?",
                        (exec_id,)
                    )
                    if cur.fetchone():
                        continue   # already recorded

                    commission = 0.0
                    if fill.commissionReport and fill.commissionReport.commission:
                        commission = float(fill.commissionReport.commission)

                    self._fill_queue.put({
                        "exec_id":    exec_id,
                        "order_id":   fill.execution.orderId,
                        "symbol":     sym,
                        "action":     action,
                        "fill_price": float(fill.execution.price),
                        "fill_qty":   int(fill.execution.shares),
                        "commission": commission,
                        "filled_at":  fill.execution.time,
                    })
                    _tracker_log(f"Reconciliation: queued missed exit fill {sym} exec_id={exec_id}")
                finally:
                    conn.close()
            except Exception as e:
                _tracker_log(f"Reconciliation fill error: {e}")

        _tracker_log("Reconciliation complete.")

    def _send_exit_notification(self, sym: str, trade_id: int, data: dict):
        """Send Telegram notification when a trade closes, followed by session summary."""
        outcome      = data.get("outcome", "?")
        net_pnl      = float(data.get("net_pnl", 0.0))
        gross_pnl    = float(data.get("gross_pnl", 0.0))
        commission   = float(data.get("commission_total", abs(gross_pnl - net_pnl)))
        exit_reason  = data.get("exit_reason", "manual")
        entry_price  = float(data.get("entry_price", 0.0))
        exit_price   = float(data.get("exit_price", 0.0))
        fill_qty     = int(data.get("fill_qty", 0))
        hold_seconds = int(data.get("hold_seconds", 0))
        entry_slip   = float(data.get("entry_slippage", 0.0))
        exit_slip    = float(data.get("exit_slippage", 0.0))

        # Hold time string
        if hold_seconds >= 3600:
            h, rem = divmod(hold_seconds, 3600)
            hold_str = f"{h}h {rem//60}m"
        elif hold_seconds >= 60:
            hold_str = f"{hold_seconds//60}m"
        else:
            hold_str = f"{hold_seconds}s"

        # P&L percentage
        trade_value = entry_price * fill_qty
        pnl_pct     = (net_pnl / trade_value * 100) if trade_value else 0.0
        gross_pct   = (gross_pnl / trade_value * 100) if trade_value else 0.0

        # Emoji and header
        if outcome == "WIN":
            header_emoji = "✅"
            trend_emoji  = "📈"
        elif outcome == "LOSS":
            header_emoji = "🔴"
            trend_emoji  = "📉"
        else:
            header_emoji = "⚪"
            trend_emoji  = "📊"

        g_sign = "+" if gross_pnl >= 0 else ""
        n_sign = "+" if net_pnl   >= 0 else ""
        p_sign = "+" if pnl_pct   >= 0 else ""

        msg = (
            f"{header_emoji} TRADE CLOSED — {outcome}\n\n"
            f"{trend_emoji} {sym}\n"
            f"Entry: ${entry_price:.4f}  ->  Exit: ${exit_price:.4f}\n"
            f"Qty: {fill_qty} shares  •  Hold: {hold_str}\n\n"
            f"Gross P&L: {g_sign}${gross_pnl:.2f} ({g_sign}{gross_pct:.1f}%)\n"
            f"Fees: -${commission:.2f}\n"
            f"Net P&L: {n_sign}${net_pnl:.2f} ({p_sign}{pnl_pct:.1f}%)\n\n"
            f"Exit: {exit_reason}\n"
            f"Slippage: entry {entry_slip:+.4f}  •  exit {exit_slip:+.4f}"
        )

        # Session summary
        try:
            conn = connect_db()
            summary = _get_session_summary(conn)
            conn.close()
            msg += f"\n\n{summary}"
        except Exception:
            pass

        _send_telegram(msg)


def _get_session_summary(conn) -> str:
    """Query current-month closed trades and return a one-line summary string."""
    try:
        month_start = datetime.utcnow().strftime("%Y-%m-01")
        cur = conn.cursor()
        cur.execute("""
            SELECT outcome, net_pnl FROM closed_trades
            WHERE closed_at >= ?
        """, (month_start,))
        rows = cur.fetchall()
        if not rows:
            return "📊 Session: no closed trades this month"
        wins    = sum(1 for r in rows if r[0] == "WIN")
        losses  = sum(1 for r in rows if r[0] == "LOSS")
        net     = sum(float(r[1] or 0) for r in rows)
        total   = wins + losses
        wr      = int(wins / total * 100) if total else 0
        sign    = "+" if net >= 0 else ""
        return f"📊 Session: {wins}W {losses}L ({wr}%)  •  Net: {sign}${net:.2f}"
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------

def start_fill_monitor(client_id: int = None) -> FillMonitor:
    """
    Start the fill monitor in a daemon thread. Returns the FillMonitor instance.
    Call monitor.stop() to shut it down gracefully.
    """
    monitor = FillMonitor(client_id=client_id)
    monitor.start()
    _tracker_log(f"FillMonitor started (clientId={monitor._client_id})")
    return monitor


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
