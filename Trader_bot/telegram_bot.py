# telegram_bot.py
import asyncio
import concurrent.futures
import json
import math
import os
import sqlite3
import sys
import threading
from datetime import datetime

_BOT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _BOT_DIR)
_BRIEF_PATH = os.path.join(_BOT_DIR, "output", "brief.txt")
_APPROVED_TRADES_PATH = os.path.join(_BOT_DIR, "output", "approved_trades.json")

import config
_DB_PATH = os.path.join(_BOT_DIR, getattr(config, "DB_PATH", "output/trader.sqlite"))
from approve import place_bracket, load_buy_recs, latest_run_id as _latest_run_id
from ib_insync import IB
from src_format import fmt_buy_message, fmt_brief_message, fmt_watch_candidate, fmt_candidate, DIVIDER, _fmt_date
from src_settings import (
    apply_overrides_to_config,
    propose_change,
    confirm_change,
    cancel_change,
    clear_overrides,
    load_overrides,
    load_pending,
    get_allowed_chat_id,
    register_allowed_chat_id,
    parse_value,
)

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes


_MISSING_GRACE_MINUTES = 30


def _load_approved_data() -> dict:
    """Load approved_trades.json, normalising old string-value format to new dict format."""
    try:
        with open(_APPROVED_TRADES_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        data = {}
        for sym, val in raw.items():
            if isinstance(val, str):
                # old format: {"SYM": "iso_timestamp"}
                data[sym.upper()] = {"approved_at": val, "first_missing_at": None}
            elif isinstance(val, dict):
                data[sym.upper()] = val
        return data
    except Exception:
        return {}


def _save_approved_data(data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_APPROVED_TRADES_PATH), exist_ok=True)
        with open(_APPROVED_TRADES_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def _record_approved_trade(symbol: str) -> None:
    """Persist a successfully placed order to approved_trades.json."""
    data = _load_approved_data()
    data[symbol.upper()] = {"approved_at": datetime.now().isoformat(), "first_missing_at": None}
    _save_approved_data(data)


def _process_missing_approved(ibkr_symbols: set) -> list:
    """
    Cross-reference approved_trades.json against current IBKR positions.

    - First time a symbol is missing: record first_missing_at timestamp.
    - If missing for < GRACE_MINUTES: show warning with countdown.
    - If missing for >= GRACE_MINUTES: auto-remove from file silently.
    - If symbol is present in IBKR: clear first_missing_at.

    Returns list of warning line strings.
    """
    data = _load_approved_data()
    if not data:
        return []

    now = datetime.now()
    warning_lines = []
    to_remove = []
    changed = False

    for sym, entry in data.items():
        if sym in ibkr_symbols:
            # Position found in IBKR — clear any stale missing timestamp
            if entry.get("first_missing_at"):
                entry["first_missing_at"] = None
                changed = True
            continue

        # Symbol is missing from IBKR
        first_missing = entry.get("first_missing_at")

        if first_missing is None:
            # First time detected missing — start the grace timer
            entry["first_missing_at"] = now.isoformat()
            changed = True
            elapsed_min = 0.0
        else:
            try:
                elapsed_min = (now - datetime.fromisoformat(first_missing)).total_seconds() / 60
            except Exception:
                elapsed_min = 0.0

        if elapsed_min >= _MISSING_GRACE_MINUTES:
            to_remove.append(sym)
            changed = True
        else:
            remaining = int(_MISSING_GRACE_MINUTES - elapsed_min)
            warning_lines.append(
                f"⚠️ <b>{sym}</b> — approved but not found in IBKR positions "
                f"(may have been filled and closed · auto-clears in {remaining}m)"
            )

    for sym in to_remove:
        del data[sym]

    if changed:
        _save_approved_data(data)

    return warning_lines


# In-memory pending trade (cleared after confirm/cancel)
_pending_trade: dict = {}


async def _get_live_stats() -> tuple:
    """Return (purse, active_positions, invested, owned_symbols) using live overrides + IBKR portfolio."""
    apply_overrides_to_config(config)

    purse_cad = getattr(config, "TRADE_PURSE_CAD", 0) or 0
    if purse_cad > 0:
        purse = float(purse_cad)
        use_cad = True
    else:
        purse = float(getattr(config, "TRADE_PURSE_USD", 1500.0) or 1500.0)
        use_cad = False

    _pos_fut = concurrent.futures.Future()

    def _fetch():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            ib = IB()
            ib.connect(config.IB_HOST, config.IB_PORT, clientId=16)
            pos = ib.reqPositions()  # explicit fresh request; awaits positionEnd from TWS
            ib.disconnect()
            _pos_fut.set_result(pos)
        except Exception as _e:
            _pos_fut.set_exception(_e)
        finally:
            loop.close()

    threading.Thread(target=_fetch, daemon=True).start()

    try:
        pos_list = await asyncio.wait_for(asyncio.wrap_future(_pos_fut), timeout=15)
        active = sum(1 for p in pos_list if p.position > 0)
        invested_usd = sum(p.position * p.avgCost for p in pos_list if p.position > 0)
        owned_symbols = {p.contract.symbol.upper() for p in pos_list if p.position > 0}
        if use_cad:
            fx = max(float(getattr(config, "USD_PER_CAD", 0.73) or 0.73), 0.01)
            invested = invested_usd / fx
        else:
            invested = invested_usd
    except Exception:
        active = 0
        invested = 0.0
        owned_symbols = set()

    return purse, active, invested, owned_symbols


def _fetch_bot_recs(run_id: int) -> list[dict]:
    """Query the DB for BUY recs including gate results, deduped by symbol."""
    conn = sqlite3.connect(_DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT symbol, score_total, confidence, risk_score, ref_price, stop_price, take_price,
               suggested_qty, rationale, gate_decision, gate_confidence, gate_reason
        FROM recommendations
        WHERE run_id = ? AND UPPER(TRIM(decision)) = 'BUY'
        ORDER BY score_total DESC, confidence DESC
        LIMIT 20
    """, (run_id,))
    rows = cur.fetchall()
    conn.close()
    seen, recs = set(), []
    for r in rows:
        sym = str(r[0]).upper()
        if sym in seen:
            continue
        seen.add(sym)
        recs.append({
            "symbol":           sym,
            "score_total":      float(r[1] or 0),
            "confidence":       float(r[2] or 0),
            "risk_score":       float(r[3] or 0),
            "ref_price":        float(r[4] or 0),
            "stop_price":       float(r[5] or 0),
            "take_price":       float(r[6] or 0),
            "suggested_qty":    int(r[7] or 0),
            "rationale":        (r[8] or "").strip(),
            "gate_decision":    (r[9] or "").strip(),
            "gate_confidence":  float(r[10] or 0),
            "gate_reason":      (r[11] or "").strip(),
        })
    return recs


def _fetch_bot_watch_recs(run_id: int) -> list[dict]:
    """Query the DB for WATCH recs, deduped by symbol."""
    conn = sqlite3.connect(_DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT symbol, score_total, confidence, risk_score, ref_price, rationale
        FROM recommendations
        WHERE run_id = ? AND UPPER(TRIM(decision)) = 'WATCH'
        ORDER BY score_total DESC
        LIMIT 10
    """, (run_id,))
    rows = cur.fetchall()
    conn.close()
    seen, recs = set(), []
    for r in rows:
        sym = str(r[0]).upper()
        if sym in seen:
            continue
        seen.add(sym)
        recs.append({
            "symbol":      sym,
            "score_total": float(r[1] or 0),
            "confidence":  float(r[2] or 0),
            "risk_score":  float(r[3] or 0),
            "ref_price":   float(r[4] or 0),
            "rationale":   (r[5] or "").strip(),
        })
    return recs


def _fetch_watch_rec_by_symbol(run_id: int, symbol: str) -> dict | None:
    """Query DB for a single WATCH rec by symbol, with stop/take derived if missing."""
    conn = sqlite3.connect(_DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT symbol, score_total, confidence, risk_score, ref_price, stop_price, take_price, rationale
        FROM recommendations
        WHERE run_id = ? AND UPPER(TRIM(decision)) = 'WATCH' AND UPPER(TRIM(symbol)) = ?
        ORDER BY score_total DESC
        LIMIT 1
    """, (run_id, symbol.upper()))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    ref  = float(row[4] or 0)
    stop = float(row[5] or 0)
    take = float(row[6] or 0)
    # Derive stop/take from config if not stored
    if stop <= 0 and ref > 0:
        stop = round(ref * (1.0 - float(config.STOP_LOSS_PCT)), 2)
    if take <= 0 and ref > 0:
        take = round(ref * (1.0 + float(getattr(config, "CONDITIONAL_SELL_INITIAL_PCT", 0.50))), 2)
    return {
        "symbol":        str(row[0]).upper(),
        "score_total":   float(row[1] or 0),
        "confidence":    float(row[2] or 0),
        "risk_score":    float(row[3] or 0),
        "ref_price":     ref,
        "stop_price":    stop,
        "take_price":    take,
        "suggested_qty": 0,
        "rationale":     (row[7] or "").strip(),
    }


def _is_allowed(update: Update) -> bool:
    chat_id = update.effective_chat.id if update.effective_chat else None
    allowed = get_allowed_chat_id(config)
    if allowed is None:
        # First-time bootstrap: allow /start to register
        return True
    return chat_id == allowed


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    allowed = get_allowed_chat_id(config)

    if allowed is None:
        register_allowed_chat_id(chat_id)
        await update.message.reply_text(
            f"✅ Registered this chat as authorized.\n"
            f"Chat ID: {chat_id}\n\n"
            f"Now you can use:\n"
            f"/showsettings\n"
            f"/purse 3000\n"
            f"/maxpos 6\n"
            f"/fx 0.74\n"
            f"/minpos 150\n"
            f"Then reply /confirm to apply."
        )
        return

    if not _is_allowed(update):
        await update.message.reply_text("⛔ Not authorized for this bot.")
        return

    await update.message.reply_text(
        "✅ Bot is running.\nTry /showsettings or /help"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        await update.message.reply_text("⛔ Not authorized for this bot.")
        return

    await update.message.reply_text(
        "📋 <b>PENNY SYSTEM COMMANDS</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "\n"
        "🔍 <b>Market Intel</b>\n"
        "/brief           Morning briefing\n"
        "/candidates      Current BUY candidates\n"
        "/status          Latest scan summary\n"
        "\n"
        "💼 <b>Trading</b>\n"
        "/approve SYM     Stage a bracket order\n"
        "/skip SYM        Dismiss a candidate\n"
        "/confirm         Confirm pending order\n"
        "/cancel          Cancel pending order\n"
        "/positions       View open IBKR positions\n"
        "/portfolio       Holdings with P&amp;L and daily change\n"
        "\n"
        "📊 <b>Position Management</b>\n"
        "/sell SYMBOL     Sell position (on conditional trigger)\n"
        "/hold SYMBOL     Hold position, set new anchor\n"
        "\n"
        "📈 <b>Analytics</b>\n"
        "/history [N]     Last N closed trades (default 20)\n"
        "/performance     Win rate, P&amp;L, key observations\n"
        "/report          Generate full HTML performance report\n"
        "\n"
        "⚙️ <b>Settings</b>\n"
        "/purse 3000      Set purse (CAD)\n"
        "/minpos 150      Set min position (USD)\n"
        "/showsettings    View current settings\n"
        "/resetsettings   Reset all overrides\n"
        "\n"
        "<i>FX rate is fetched automatically · view with /status</i>",
        parse_mode="HTML",
    )


async def showsettings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        await update.message.reply_text("⛔ Not authorized for this bot.")
        return

    apply_overrides_to_config(config)
    overrides = load_overrides()
    pending = load_pending()

    msg = (
        "📌 Current settings (defaults overridden by runtime_overrides.json):\n"
        f"- TRADE_PURSE_CAD: {getattr(config, 'TRADE_PURSE_CAD', None)}\n"
        f"- USD_PER_CAD:     {getattr(config, 'USD_PER_CAD', None)}\n"
        f"- MIN_POSITION_USD:{getattr(config, 'MIN_POSITION_USD', None)}\n\n"
        f"Overrides file has: {overrides if overrides else 'none'}\n"
    )

    if pending:
        msg += f"\n🕒 Pending change: {pending}\nReply /confirm or /cancel"
    else:
        msg += "\nNo pending change."

    await update.message.reply_text(msg)


async def _propose(update: Update, key: str, raw_value: str):
    if not _is_allowed(update):
        await update.message.reply_text("⛔ Not authorized for this bot.")
        return

    try:
        value = parse_value(key, raw_value)
        pending = propose_change(key, value)
        await update.message.reply_text(
            f"🕒 Proposed change:\n"
            f"{key} → {value}\n\n"
            f"Reply /confirm to apply or /cancel to discard."
        )
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")


async def purse(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /purse 3000")
        return
    await _propose(update, "TRADE_PURSE_CAD", context.args[0])


async def maxpos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ Max positions cap has been removed. Sizing is now tier-based "
        "(HIGH/MED/LOW) with no fixed position limit — the purse is the only constraint."
    )


async def fx(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /fx 0.74")
        return
    await _propose(update, "USD_PER_CAD", context.args[0])


async def minpos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /minpos 150")
        return
    await _propose(update, "MIN_POSITION_USD", context.args[0])


async def confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        await update.message.reply_text("⛔ Not authorized for this bot.")
        return

    # Trade confirmation takes priority over settings
    if _pending_trade:
        symbol = _pending_trade["symbol"]
        qty    = _pending_trade["suggested_qty"]
        entry  = _pending_trade["ref_price"]
        stop   = _pending_trade["stop_price"]

        await update.message.reply_text(f"Connecting to IBKR and placing order for {symbol}…")

        _fut = concurrent.futures.Future()

        def _place():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                ib = IB()
                ib.connect(config.IB_HOST, config.IB_PORT, clientId=17)
                result = place_bracket(ib, symbol, qty, entry, stop)
                ib.disconnect()
                _fut.set_result(result)
            except Exception as _e:
                _fut.set_exception(_e)
            finally:
                loop.close()

        threading.Thread(target=_place, daemon=True).start()

        try:
            result = await asyncio.wrap_future(_fut)
            _pending_trade.clear()
            _record_approved_trade(symbol)
            await update.message.reply_text(
                f"✅ Bracket order placed for <b>{symbol}</b>\n"
                f"Entry: <b>${entry:.2f}</b>  ·  Qty: <b>{qty}</b> shares\n"
                f"Stop: ${stop:.2f}  (take-profit via conditional sell monitor)\n"
                f"Order ID: {result.get('parentId', 'n/a')}",
                parse_mode="HTML",
            )
        except Exception as e:
            await update.message.reply_text(
                f"❌ Order failed: {e}\n\nIs TWS running with API enabled on port {config.IB_PORT}?"
            )
        return

    # Fall back to settings confirmation
    try:
        confirm_change()
        apply_overrides_to_config(config)
        await update.message.reply_text("✅ Applied pending change. Use /showsettings to verify.")
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        await update.message.reply_text("⛔ Not authorized for this bot.")
        return
    if _pending_trade:
        sym = _pending_trade.get("symbol", "")
        _pending_trade.clear()
        await update.message.reply_text(f"✅ Trade for {sym} cancelled.")
        return
    cancel_change()
    await update.message.reply_text("✅ Pending change cancelled.")


async def resetsettings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        await update.message.reply_text("⛔ Not authorized for this bot.")
        return
    clear_overrides()
    cancel_change()
    await update.message.reply_text("✅ Cleared all overrides. Back to config.py defaults.")


def _get_fx_display() -> str:
    """Return a display string for the current FX rate.
    Priority: manual override → open.er-api.com → config fallback."""
    import requests as _req
    manual = float(load_overrides().get("USD_PER_CAD", 0.0) or 0.0)
    if manual > 0:
        return f"1 CAD = {manual:.4f} USD <i>(manual override)</i>"
    try:
        resp = _req.get("https://open.er-api.com/v6/latest/CAD", timeout=4)
        rate = float(resp.json()["rates"]["USD"])
        if rate > 0:
            return f"1 CAD = {rate:.4f} USD"
    except Exception:
        pass
    fb = float(getattr(config, "USD_PER_CAD", 0.73) or 0.73)
    return f"1 CAD = {fb:.4f} USD <i>(cached)</i>"


async def status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/status — latest scan summary."""
    if not _is_allowed(update):
        await update.message.reply_text("⛔ Not authorized for this bot.")
        return
    try:
        conn = sqlite3.connect(_DB_PATH)
        _, run_id = _latest_run_id(conn)
        cur = conn.cursor()
        cur.execute("SELECT created_at FROM runs WHERE run_id = ?", (run_id,))
        row = cur.fetchone()
        run_time = row[0] if row else "unknown"
        cur.execute("SELECT COUNT(*) FROM recommendations WHERE run_id=? AND UPPER(TRIM(decision))='BUY'", (run_id,))
        buy_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM recommendations WHERE run_id=? AND UPPER(TRIM(decision))='WATCH'", (run_id,))
        watch_count = cur.fetchone()[0]
        conn.close()
        fx_line = _get_fx_display()
        await update.message.reply_text(
            f"📊 <b>Latest Run: {run_id}</b>\n"
            f"Time: {run_time}\n"
            f"BUY candidates: {buy_count}\n"
            f"Watchlist: {watch_count}\n"
            f"FX: {fx_line}\n\n"
            f"/candidates — view BUY details",
            parse_mode="HTML",
        )
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")


async def candidates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/candidates — show current BUY recs from latest run."""
    if not _is_allowed(update):
        await update.message.reply_text("⛔ Not authorized for this bot.")
        return
    try:
        conn = sqlite3.connect(_DB_PATH)
        _, run_id = _latest_run_id(conn)
        conn.close()
        recs = _fetch_bot_recs(run_id)
    except Exception as e:
        await update.message.reply_text(f"❌ DB error: {e}")
        return

    if not recs:
        await update.message.reply_text("No BUY candidates in the latest run.")
        return

    purse, active, invested, _ = await _get_live_stats()
    msg = fmt_buy_message(recs, purse, active_positions=active, invested=invested)
    await update.message.reply_text(msg, parse_mode="HTML")


async def approve_trade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/approve SYMBOL [qty] — stage a bracket order for confirmation."""
    if not _is_allowed(update):
        await update.message.reply_text("⛔ Not authorized for this bot.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /approve SYMBOL\nExample: /approve GRCE")
        return

    symbol = context.args[0].upper()
    qty_override = int(context.args[1]) if len(context.args) > 1 and context.args[1].isdigit() else None

    try:
        conn = sqlite3.connect(_DB_PATH)
        _, run_id = _latest_run_id(conn)
        recs, _, _ = load_buy_recs(conn, run_id)
        conn.close()
    except Exception as e:
        await update.message.reply_text(f"❌ DB error: {e}")
        return

    match = next((r for r in recs if r["symbol"] == symbol), None)
    from_watchlist = False

    if not match:
        # Fall back to watchlist
        watch_match = _fetch_watch_rec_by_symbol(run_id, symbol)
        if not watch_match:
            await update.message.reply_text(
                f"❌ {symbol} not found in BUY candidates or watchlist.\n"
                f"Use /candidates to see what's available."
            )
            return
        match = watch_match
        from_watchlist = True

    if qty_override:
        match = {**match, "suggested_qty": qty_override}

    if match["suggested_qty"] <= 0:
        await update.message.reply_text(
            f"❌ Qty for {symbol} is 0. Specify manually:\n/approve {symbol} 500"
        )
        return

    _pending_trade.clear()
    _pending_trade.update(match)

    if from_watchlist:
        await update.message.reply_text(
            f"⚠️ <b>{symbol}</b> is on the WATCHLIST (not a formal BUY).\n"
            f"Score: {match['score_total']:.1f}  ·  Conf: {match['confidence']*100:.0f}%  ·  Risk: {match['risk_score']:.2f}\n\n"
            f"Entry: <b>${match['ref_price']:.2f}</b>  ·  Qty: <b>{match['suggested_qty']}</b> shares\n"
            f"Stop: ${match['stop_price']:.2f}  ·  Target: ${match['take_price']:.2f} (cond. +50%)\n\n"
            f"Reply /confirm to proceed or /cancel to abort",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text(
            f"🕒 <b>Pending order — confirm before placing:</b>\n\n"
            f"📈 <b>{symbol}</b>\n"
            f"Entry: <b>${match['ref_price']:.2f}</b>  ·  Qty: <b>{match['suggested_qty']}</b> shares\n"
            f"Stop: ${match['stop_price']:.2f}  ·  Target: ${match['take_price']:.2f} (cond. +50%)\n\n"
            f"Reply /confirm to place bracket order\n"
            f"Reply /cancel to discard",
            parse_mode="HTML",
        )


async def skip_trade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/skip SYMBOL — dismiss a candidate for this run."""
    if not _is_allowed(update):
        await update.message.reply_text("⛔ Not authorized for this bot.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /skip SYMBOL\nExample: /skip GRCE")
        return
    symbol = context.args[0].upper()
    await update.message.reply_text(
        f"✅ {symbol} skipped. It won't trigger further alerts for this run."
    )


async def positions_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/positions — list open IBKR positions."""
    if not _is_allowed(update):
        await update.message.reply_text("⛔ Not authorized for this bot.")
        return

    await update.message.reply_text("Fetching positions from IBKR…")

    _pos_fut = concurrent.futures.Future()

    def _fetch():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            ib = IB()
            ib.connect(config.IB_HOST, config.IB_PORT, clientId=16)
            pos = ib.reqPositions()  # explicit fresh request; awaits positionEnd from TWS
            ib.disconnect()
            _pos_fut.set_result(pos)
        except Exception as _e:
            _pos_fut.set_exception(_e)
        finally:
            loop.close()

    threading.Thread(target=_fetch, daemon=True).start()

    try:
        pos_list = await asyncio.wait_for(asyncio.wrap_future(_pos_fut), timeout=15)
        active = [p for p in pos_list if int(p.position) != 0]
        if not active:
            await update.message.reply_text("No open positions.")
            return
        lines = ["📊 <b>Open Positions</b>"]
        for p in active:
            sym = p.contract.symbol
            qty = int(p.position)
            avg = float(p.avgCost)
            lines.append(f"  <b>{sym}</b>: {qty} shares @ ${avg:.2f}")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(
            f"❌ IBKR error: {e}\n\nIs TWS running with API enabled on port {config.IB_PORT}?"
        )


async def brief_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/brief — re-send today's brief including BUY candidates and watchlist."""
    if not _is_allowed(update):
        await update.message.reply_text("⛔ Not authorized for this bot.")
        return
    try:
        conn = sqlite3.connect(_DB_PATH)
        _, run_id = _latest_run_id(conn)
        conn.close()
        recs = _fetch_bot_recs(run_id)
        watch_recs = _fetch_bot_watch_recs(run_id)
    except Exception as e:
        await update.message.reply_text(f"❌ DB error: {e}")
        return

    if not recs and not watch_recs:
        await update.message.reply_text("No candidates or watchlist entries in the latest run.")
        return

    purse, active, invested, owned = await _get_live_stats()
    # Strip already-owned symbols — don't surface noise the user can't act on
    recs       = [r for r in recs       if r["symbol"] not in owned]
    watch_recs = [r for r in watch_recs if r["symbol"] not in owned]
    msg = fmt_brief_message(recs, watch_recs, purse, active, invested)
    await update.message.reply_text(msg, parse_mode="HTML")


async def portfolio_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/portfolio — live positions with P&L and daily change."""
    if not _is_allowed(update):
        await update.message.reply_text("⛔ Not authorized for this bot.")
        return

    await update.message.reply_text("Fetching portfolio from IBKR…")

    _fut = concurrent.futures.Future()

    def _fetch():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            ib = IB()
            ib.connect(config.IB_HOST, config.IB_PORT, clientId=18)
            ib.reqMarketDataType(getattr(config, "MARKET_DATA_TYPE", 1))
            positions = ib.reqPositions()  # explicit fresh request; awaits positionEnd
            active = [p for p in positions if p.position > 0]

            def safe_price(val):
                if val is None:
                    return None
                try:
                    f = float(val)
                    return None if math.isnan(f) else f
                except Exception:
                    return None

            items = []

            for pos in active:
                contract = pos.contract
                avg_cost = float(pos.avgCost)
                qty = int(pos.position)

                curr_price   = None  # current trading price
                ticker_last  = None  # last traded price  (daily change numerator)
                ticker_close = None  # prev session close (daily change denominator)

                try:
                    ib.qualifyContracts(contract)
                    ticker = ib.reqMktData(contract, "", False, False)
                    ib.sleep(3)  # allow IBKR time to populate ticker fields

                    ticker_last  = safe_price(ticker.last)
                    ticker_close = safe_price(ticker.close)

                    # Current price: last → marketPrice() → close
                    curr_price = ticker_last
                    if curr_price is None:
                        curr_price = safe_price(ticker.marketPrice())
                    if curr_price is None:
                        curr_price = ticker_close

                    ib.cancelMktData(contract)
                except Exception:
                    pass

                # Historical bar fallback when live data is entirely unavailable
                if curr_price is None:
                    try:
                        bars = ib.reqHistoricalData(
                            contract, endDateTime="",
                            durationStr="3 D", barSizeSetting="1 day",
                            whatToShow="TRADES", useRTH=True, formatDate=1,
                        )
                        if len(bars) >= 1:
                            curr_price = float(bars[-1].close)
                        if len(bars) >= 2 and ticker_close is None:
                            ticker_close = float(bars[-2].close)
                    except Exception:
                        pass

                # P&L from entry price
                pnl_usd = (curr_price - avg_cost) * qty if curr_price is not None else None

                # Daily change: requires a last-traded price AND a prev-session close
                if ticker_last is not None and ticker_close is not None and ticker_close > 0:
                    day_chng_usd = (ticker_last - ticker_close) * qty
                    day_chng_pct = (ticker_last - ticker_close) / ticker_close * 100
                else:
                    day_chng_usd = None
                    day_chng_pct = None

                items.append({
                    "symbol":        contract.symbol.upper(),
                    "qty":           qty,
                    "avgCost":       avg_cost,
                    "marketPrice":   curr_price,
                    "unrealizedPNL": pnl_usd,
                    "dayChngUsd":    day_chng_usd,
                    "dayChngPct":    day_chng_pct,
                })

            ib.disconnect()
            _fut.set_result(items)
        except Exception as _e:
            _fut.set_exception(_e)
        finally:
            loop.close()

    threading.Thread(target=_fetch, daemon=True).start()

    try:
        items = await asyncio.wait_for(
            asyncio.wrap_future(_fut), timeout=30
        )
    except Exception as e:
        await update.message.reply_text(
            f"❌ IBKR error: {e}\n\nIs TWS running on port {config.IB_PORT}?"
        )
        return

    ibkr_symbols = {item["symbol"] for item in items}
    warning_lines = _process_missing_approved(ibkr_symbols)

    if not items:
        warning_lines = _process_missing_approved(set())
        if warning_lines:
            await update.message.reply_text("\n".join(warning_lines), parse_mode="HTML")
        else:
            await update.message.reply_text("No open positions.")
        return

    apply_overrides_to_config(config)
    purse_cad = getattr(config, "TRADE_PURSE_CAD", 0) or 0
    if purse_cad > 0:
        use_cad = True
        fx = max(float(getattr(config, "USD_PER_CAD", 0.73) or 0.73), 0.01)
    else:
        use_cad = False
        fx = 1.0
    position_blocks: list[str] = []
    total_cost_usd = 0.0
    total_pnl_usd  = 0.0
    best_sym,  best_day_usd,  best_day_pct  = "", 0.0, -9999.0
    worst_sym, worst_day_usd, worst_day_pct = "", 0.0,  9999.0
    green = red = 0

    for item in items:
        sym          = item["symbol"]
        qty          = item["qty"]
        entry        = item["avgCost"]
        curr         = item["marketPrice"]   # float or None
        pnl_usd      = item["unrealizedPNL"] # float or None
        day_chng_usd = item["dayChngUsd"]    # float or None
        day_chng_pct = item["dayChngPct"]    # float or None
        cost_basis   = entry * qty

        total_cost_usd += cost_basis
        if pnl_usd is not None:
            total_pnl_usd += pnl_usd

        # P&L display
        if pnl_usd is not None:
            pnl_pct     = (pnl_usd / cost_basis * 100) if cost_basis > 0 else 0.0
            pnl_flag    = "🟢" if pnl_usd >= 0 else "🔴"
            pnl_dollar  = f"+${abs(pnl_usd):.2f}" if pnl_usd >= 0 else f"-${abs(pnl_usd):.2f}"
            pnl_pct_str = f"+{abs(pnl_pct):.2f}%" if pnl_pct >= 0 else f"-{abs(pnl_pct):.2f}%"
            pnl_line    = f"P&L: {pnl_flag} {pnl_dollar} ({pnl_pct_str})"
            if pnl_usd >= 0:
                green += 1
            else:
                red += 1
        else:
            pnl_line = "P&L: N/A"

        # Best/worst by today's CHNG (not P&L from entry)
        if day_chng_pct is not None:
            if day_chng_pct > best_day_pct:
                best_day_pct, best_day_usd, best_sym  = day_chng_pct, day_chng_usd, sym
            if day_chng_pct < worst_day_pct:
                worst_day_pct, worst_day_usd, worst_sym = day_chng_pct, day_chng_usd, sym

        # Daily change — show both $ and %
        if day_chng_usd is not None and day_chng_pct is not None:
            d_dollar = f"+${abs(day_chng_usd):.2f}" if day_chng_usd >= 0 else f"-${abs(day_chng_usd):.2f}"
            d_pct    = f"+{abs(day_chng_pct):.2f}%" if day_chng_pct >= 0 else f"-{abs(day_chng_pct):.2f}%"
            arrow    = "▲" if day_chng_pct >= 0 else "▼"
            day_str  = f"Today: {arrow} {d_dollar} ({d_pct})"
        else:
            day_str = "Today: N/A"

        entry_str = f"${entry:.2f}" if entry > 0 else "N/A"
        curr_str  = f"${curr:.2f}"  if curr is not None else "N/A"

        position_blocks.append(
            f"💠 <b>{sym}</b> | {qty} shares\n"
            f"Entry: {entry_str}  ·  Current: {curr_str}\n"
            f"{pnl_line}\n"
            f"{day_str}"
        )

    # Convert totals to display currency
    def _conv(usd: float) -> float:
        return usd / fx if use_cad else usd

    total_invested = _conv(total_cost_usd)
    total_pnl      = _conv(total_pnl_usd)
    overall_pct    = (total_pnl_usd / total_cost_usd * 100) if total_cost_usd > 0 else 0.0
    net_flag    = "🟢" if total_pnl >= 0 else "🔴"
    net_dollar  = f"+${abs(total_pnl):.2f}" if total_pnl >= 0 else f"-${abs(total_pnl):.2f}"
    net_pct_str = f"+{abs(overall_pct):.2f}%" if total_pnl >= 0 else f"-{abs(overall_pct):.2f}%"

    # Best/worst summary strings (based on today's CHNG)
    if best_sym:
        b_dollar  = f"+${abs(best_day_usd):.2f}" if best_day_usd >= 0 else f"-${abs(best_day_usd):.2f}"
        b_pct_str = f"+{abs(best_day_pct):.2f}%" if best_day_usd >= 0 else f"-{abs(best_day_pct):.2f}%"
        best_str  = f"<b>{best_sym}</b> {b_dollar} ({b_pct_str})"
    else:
        best_str = "N/A"
    if worst_sym:
        w_dollar  = f"+${abs(worst_day_usd):.2f}" if worst_day_usd >= 0 else f"-${abs(worst_day_usd):.2f}"
        w_pct_str = f"+{abs(worst_day_pct):.2f}%" if worst_day_usd >= 0 else f"-{abs(worst_day_pct):.2f}%"
        worst_str = f"<b>{worst_sym}</b> {w_dollar} ({w_pct_str})"
    else:
        worst_str = "N/A"

    summary = (
        f"\n\n\n📊 <b>PORTFOLIO SUMMARY</b>\n"
        f"{DIVIDER}\n"
        f"Total: {len(items)} positions  ·  ${total_invested:,.2f} invested\n"
        f"Net Gain: {net_flag} {net_dollar} ({net_pct_str})\n"
        f"Best today:  {best_str}\n"
        f"Worst today: {worst_str}\n"
        f"In Green: {green}/{len(items)}  ·  In Red: {red}/{len(items)}"
    )

    body = f"\n{DIVIDER}\n".join(position_blocks)
    warnings = ("\n\n" + "\n".join(warning_lines)) if warning_lines else ""
    await update.message.reply_text(body + summary + warnings, parse_mode="HTML")


# ---------------------------------------------------------------------------
# Analytics commands: /history, /performance, /report
# ---------------------------------------------------------------------------

_MONTH_ABBREVS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

def _parse_history_args(args: list[str]):
    """
    Parse /history args into (days_back, month_num, symbol_filter, label).
    Priority: month abbrev > numeric days > symbol ticker.
    Supports 0, 1, or 2 args. Default: last 30 days.
    """
    days_back, month_num, sym_filter = None, None, None

    for arg in args:
        a = arg.strip().lower()
        if a in _MONTH_ABBREVS:
            month_num = _MONTH_ABBREVS[a]
        elif a.isdigit():
            days_back = max(1, min(int(a), 365))
        else:
            sym_filter = arg.upper()

    if month_num is None and days_back is None and sym_filter is None:
        days_back = 30   # default

    if month_num is not None:
        label = f"{arg.capitalize()} trades"
    elif days_back is not None:
        label = f"Last {days_back} day(s)"
    elif sym_filter:
        label = f"{sym_filter} trades"
    else:
        label = "Last 30 days"

    return days_back, month_num, sym_filter, label


async def history_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show recent trade history with flexible filtering."""
    if not _is_allowed(update):
        await update.message.reply_text("⛔ Not authorized.")
        return

    days_back, month_num, sym_filter, label = _parse_history_args(context.args or [])

    try:
        from datetime import timedelta
        conn = sqlite3.connect(_DB_PATH)
        cur  = conn.cursor()

        conditions, params = [], []
        if month_num is not None:
            year = datetime.now().year
            conditions.append("strftime('%Y-%m', closed_at) = ?")
            params.append(f"{year}-{month_num:02d}")
        elif days_back is not None:
            since = (datetime.utcnow() - timedelta(days=days_back)).isoformat()
            conditions.append("closed_at >= ?")
            params.append(since)
        if sym_filter:
            conditions.append("UPPER(TRIM(symbol)) = ?")
            params.append(sym_filter)

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        cur.execute(f"""
            SELECT symbol, closed_at, entry_price, exit_price, entry_qty,
                   net_pnl, gross_pnl, outcome, exit_reason, hold_seconds, pnl_pct
            FROM closed_trades {where}
            ORDER BY trade_id DESC LIMIT 50
        """, params)
        rows = cur.fetchall()

        # Summary stats
        cur.execute(f"""
            SELECT COUNT(*), SUM(net_pnl), SUM(gross_pnl),
                   SUM(commission_total), AVG(hold_duration_hours)
            FROM closed_trades {where}
        """, params)
        s = cur.fetchone()
        conn.close()
    except Exception as e:
        await update.message.reply_text(f"DB error: {e}")
        return

    if not rows:
        await update.message.reply_text(f"No closed trades in this period ({label}).")
        return

    total_n, total_net, total_gross, total_fees, avg_hold = s
    total_n    = total_n or 0
    total_net  = float(total_net or 0)
    total_gross = float(total_gross or 0)
    total_fees  = float(total_fees or 0)
    avg_hold    = float(avg_hold or 0)
    wins   = sum(1 for r in rows if r[7] == "WIN")
    losses = sum(1 for r in rows if r[7] == "LOSS")
    wr     = int(wins / total_n * 100) if total_n else 0

    # Profit factor
    win_sum  = sum(float(r[5] or 0) for r in rows if r[7] == "WIN")
    loss_sum = abs(sum(float(r[5] or 0) for r in rows if r[7] == "LOSS"))
    pf_str   = f"{win_sum/loss_sum:.2f}" if loss_sum else "∞"

    net_sign = "+" if total_net >= 0 else ""
    best  = max(rows, key=lambda r: float(r[5] or 0))
    worst = min(rows, key=lambda r: float(r[5] or 0))

    def _pnl_s(r):
        p = float(r[5] or 0)
        return ("+" if p >= 0 else "") + f"${p:.2f}"

    def _hold_s(secs):
        s = int(secs or 0)
        if s >= 3600: return f"{s//3600}h {(s%3600)//60}m"
        return f"{s//60}m"

    header = (
        f"<b>📊 TRADE HISTORY — {label}</b>\n\n"
        f"Total: {total_n} trades  ·  {wins}W {losses}L ({wr}%)\n"
        f"Gross P&L: {net_sign}${total_gross:.2f}\n"
        f"Total Fees: -${abs(total_fees):.2f}\n"
        f"Net P&L: {net_sign}${total_net:.2f}\n"
        f"Profit Factor: {pf_str}  ·  Avg Hold: {avg_hold:.1f}h\n\n"
        f"Best:  <b>{best[0]}</b> {_pnl_s(best)}\n"
        f"Worst: <b>{worst[0]}</b> {_pnl_s(worst)}\n\n"
        f"<b>Recent:</b>"
    )

    outcome_icon = {"WIN": "✅", "LOSS": "🔴", "BREAKEVEN": "⚪"}
    trade_lines = []
    for sym, closed_at, entry, exit_p, qty, net_pnl, gross_pnl, outcome, reason, hold_s, pnl_pct in rows[:20]:
        ico = outcome_icon.get(outcome or "", "•")
        dt  = (closed_at or "")[:10]
        pnl = float(net_pnl or 0)
        pct = float(pnl_pct or 0)
        sgn = "+" if pnl >= 0 else ""
        rsn = (reason or "?").replace("_", "-")
        trade_lines.append(
            f"  {ico} <b>{sym}</b>  {sgn}${pnl:.2f} ({sgn}{pct:.1f}%)  {rsn}  {_hold_s(hold_s)}  <i>{dt}</i>"
        )

    await update.message.reply_text(
        header + "\n" + "\n".join(trade_lines),
        parse_mode="HTML"
    )


async def performance_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Run the learning engine and show performance analysis with signal quality and trend."""
    if not _is_allowed(update):
        await update.message.reply_text("⛔ Not authorized.")
        return

    await update.message.reply_text("Analysing trade history...")

    def _run():
        from src_learning import run_learning_analysis
        from src_storage import ensure_schema, connect_db
        conn = connect_db()
        ensure_schema(conn)
        try:
            return run_learning_analysis(conn)
        finally:
            conn.close()

    try:
        _fut     = concurrent.futures.Future()
        t        = threading.Thread(target=lambda: _fut.set_result(_run()), daemon=True)
        t.start()
        analysis = await asyncio.wrap_future(_fut)
    except Exception as e:
        await update.message.reply_text(f"Analysis error: {e}")
        return

    ov   = analysis.get("overview", {})
    obs  = analysis.get("observations", [])
    te   = analysis.get("temporal", {})
    sig  = analysis.get("signals", {})
    cm   = analysis.get("combinations", {})

    n        = ov.get("total_trades", 0)
    wins     = ov.get("wins", 0)
    losses   = ov.get("losses", 0)
    wr       = ov.get("win_rate", 0.0)
    pnl      = ov.get("total_net_pnl", 0.0)
    avg_hold = ov.get("avg_hold_hours", 0.0)
    tier     = ov.get("confidence_tier", "insufficient")
    pf       = ov.get("profit_factor")
    exp      = ov.get("expectancy", 0.0)

    pnl_s  = ("+" if pnl >= 0 else "") + f"${pnl:.2f}"
    pf_s   = f"{pf:.2f}" if pf is not None else "inf"
    exp_s  = ("+" if exp >= 0 else "") + f"${exp:.2f}"

    tier_label = {
        "insufficient": f"(need {max(0, 5 - n)} more trades for signal analysis)",
        "preliminary":  "(preliminary — patterns not yet reliable)",
        "emerging":     "(emerging — treat as directional)",
        "reliable":     "(statistically reliable)",
    }.get(tier, "")

    # Trend line
    trend       = te.get("trend", "STABLE")
    first_wr    = te.get("first_half", {}).get("win_rate", 0.0)
    last10_wr   = te.get("last_10", {}).get("win_rate", 0.0)
    trend_emoji = {"IMPROVING": "📈", "DEGRADING": "📉", "STABLE": "→"}.get(trend, "→")

    # Signal quality bullets — pick best bucket per key signal
    sig_lines = []

    for b in (sig.get("breakout") or []):
        if b.get("label") == "Breakout" and b.get("n", 0) >= 5:
            sig_lines.append(f"• Breakout=1: {b['win_rate']*100:.0f}% WR  (N={b['n']})")
            break

    vs_buckets = [b for b in (sig.get("vol_surge") or []) if b.get("n", 0) >= 5]
    if vs_buckets:
        best_vs = max(vs_buckets, key=lambda b: b["win_rate"])
        sig_lines.append(
            f"• Vol surge {best_vs['label']}: {best_vs['win_rate']*100:.0f}% WR  (N={best_vs['n']})"
        )

    conf_buckets = [b for b in (sig.get("confidence") or []) if b.get("n", 0) >= 5]
    if conf_buckets:
        best_conf = max(conf_buckets, key=lambda b: b["win_rate"])
        sig_lines.append(
            f"• Confidence {best_conf['label']}: {best_conf['win_rate']*100:.0f}% WR  (N={best_conf['n']})"
        )

    best_combos = cm.get("best", [])
    if best_combos:
        bc = best_combos[0]
        sig_lines.append(
            f"• {bc['label']}: {bc['win_rate']*100:.0f}% WR  (N={bc['n']})"
        )

    # Assemble message
    lines = [
        "<b>📊 PERFORMANCE ANALYSIS</b>",
        "",
        f"<b>{n} trades</b>  {tier_label}",
        f"{wins}W / {losses}L  ·  Win Rate: <b>{wr*100:.1f}%</b>",
        f"Net P&L: <b>{pnl_s}</b>  ·  Avg Hold: {avg_hold:.1f}h",
        f"Profit Factor: <b>{pf_s}</b>  ·  Expectancy: <b>{exp_s}</b>",
    ]

    if n >= 10:
        lines += [
            "",
            f"{trend_emoji} Trend: Last 10: {last10_wr*100:.0f}% WR  vs  "
            f"First half: {first_wr*100:.0f}% WR  →  {trend}",
        ]

    if sig_lines:
        lines += ["", "<b>🔬 Signal Quality</b>"] + sig_lines

    if obs:
        lines += ["", "<b>Observations:</b>"]
        lines += [f"• {o}" for o in obs[:5]]
    else:
        lines += ["", "No observations yet — need more closed trades."]

    lines += ["", "<i>Use /report for the full HTML analysis.</i>"]

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def report_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generate the full HTML performance report and send file path."""
    if not _is_allowed(update):
        await update.message.reply_text("⛔ Not authorized.")
        return

    await update.message.reply_text("Generating report...")

    def _run():
        from src_report import generate_report
        from src_storage import ensure_schema, connect_db
        conn = connect_db()
        ensure_schema(conn)
        try:
            return generate_report(conn)
        finally:
            conn.close()

    try:
        _fut  = concurrent.futures.Future()
        t     = threading.Thread(target=lambda: _fut.set_result(_run()), daemon=True)
        t.start()
        path  = await asyncio.wrap_future(_fut)
    except Exception as e:
        await update.message.reply_text(f"Report error: {e}")
        return

    await update.message.reply_text(
        f"Report saved:\n<code>{path}</code>\n\n"
        f"Open it in any browser for the full analysis.",
        parse_mode="HTML",
    )


async def sell_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/sell SYMBOL — Exit position via conditional sell (cancels stop, places market sell)."""
    if not _is_allowed(update):
        await update.message.reply_text("⛔ Not authorized for this bot.")
        return

    args = context.args
    if not args:
        await update.message.reply_text("Usage: /sell SYMBOL")
        return

    symbol = args[0].upper()

    # Verify symbol is in PENDING_DECISION state
    try:
        conn = sqlite3.connect(_DB_PATH)
        cur  = conn.cursor()
        cur.execute(
            "SELECT status FROM conditional_sell_state WHERE symbol=?", (symbol,)
        )
        row = cur.fetchone()
        conn.close()
    except Exception as e:
        await update.message.reply_text(f"❌ DB error: {e}")
        return

    if not row or row[0] != "PENDING_DECISION":
        await update.message.reply_text(
            f"❌ No pending sell decision for {symbol}. Use /portfolio to check positions."
        )
        return

    # Get qty from open_positions
    try:
        conn = sqlite3.connect(_DB_PATH)
        cur  = conn.cursor()
        cur.execute(
            "SELECT fill_qty FROM open_positions"
            " WHERE UPPER(TRIM(symbol))=? AND status='open'"
            " ORDER BY position_id DESC LIMIT 1",
            (symbol,)
        )
        qty_row = cur.fetchone()
        conn.close()
    except Exception as e:
        await update.message.reply_text(f"❌ DB error fetching qty: {e}")
        return

    if not qty_row:
        await update.message.reply_text(f"❌ No open position found for {symbol}.")
        return

    qty = int(qty_row[0])
    await update.message.reply_text(
        f"Connecting to IBKR — selling {qty} shares of {symbol}…"
    )

    _fut = concurrent.futures.Future()

    def _sell():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            from src_trade_tracker import execute_conditional_sell
            ib = IB()
            ib.connect(config.IB_HOST, config.IB_PORT, clientId=17)
            result = execute_conditional_sell(ib, symbol, qty)
            ib.disconnect()
            # Mark state as SOLD in DB
            conn2 = sqlite3.connect(_DB_PATH)
            cur2  = conn2.cursor()
            cur2.execute(
                "UPDATE conditional_sell_state SET status='SOLD' WHERE symbol=?",
                (symbol,)
            )
            conn2.commit()
            conn2.close()
            _fut.set_result(result)
        except Exception as _e:
            _fut.set_exception(_e)
        finally:
            loop.close()

    threading.Thread(target=_sell, daemon=True).start()

    try:
        result = await asyncio.wrap_future(_fut)
        await update.message.reply_text(
            f"✅ Sell order placed for <b>{symbol}</b>\n"
            f"Qty: {qty} shares  ·  Order ID: {result.get('orderId', 'n/a')}\n"
            f"Fill notification will follow.",
            parse_mode="HTML",
        )
    except Exception as e:
        await update.message.reply_text(
            f"❌ Sell order failed for {symbol}: {e}\n\n"
            f"Is TWS running with API enabled on port {config.IB_PORT}?"
        )


async def hold_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/hold SYMBOL — Keep position, reset anchor to trigger price."""
    if not _is_allowed(update):
        await update.message.reply_text("⛔ Not authorized for this bot.")
        return

    args = context.args
    if not args:
        await update.message.reply_text("Usage: /hold SYMBOL")
        return

    symbol = args[0].upper()

    conn = None
    try:
        conn = sqlite3.connect(_DB_PATH)
        cur  = conn.cursor()
        cur.execute(
            "SELECT status, last_trigger_price, entry_price"
            " FROM conditional_sell_state WHERE symbol=?",
            (symbol,)
        )
        row = cur.fetchone()
    except Exception as e:
        if conn:
            conn.close()
        await update.message.reply_text(f"❌ DB error: {e}")
        return

    if not row or row[0] != "PENDING_DECISION":
        conn.close()
        await update.message.reply_text(
            f"❌ No pending sell decision for {symbol}."
        )
        return

    # Use last_trigger_price (actual price at trigger time); fall back to entry
    current_price = float(row[1] if row[1] is not None else row[2])

    try:
        from src_trade_tracker import handle_hold
        result = handle_hold(conn, symbol, current_price)
        conn.close()
    except Exception as e:
        conn.close()
        await update.message.reply_text(f"❌ Hold update failed: {e}")
        return

    subsequent_pct = float(getattr(config, "CONDITIONAL_SELL_SUBSEQUENT_PCT", 0.30))

    await update.message.reply_text(
        f"✅ Holding <b>{symbol}</b>. New anchor: <b>${result['anchor']:.2f}</b>\n"
        f"Next triggers:\n"
        f"  📈 +{subsequent_pct*100:.0f}%: <b>${result['trigger_high']:.2f}</b>\n"
        f"  📉 -{subsequent_pct*100:.0f}%: <b>${result['trigger_low']:.2f}</b>",
        parse_mode="HTML",
    )


def main():
    token = getattr(config, "TELEGRAM_BOT_TOKEN", None)
    if not token or "PASTE_YOUR_BOT_TOKEN_HERE" in token:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN in config.py first.")

    app = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("showsettings", showsettings))

    app.add_handler(CommandHandler("purse", purse))
    app.add_handler(CommandHandler("maxpos", maxpos))
    app.add_handler(CommandHandler("fx", fx))
    app.add_handler(CommandHandler("minpos", minpos))

    app.add_handler(CommandHandler("confirm", confirm))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("resetsettings", resetsettings))

    # Trading commands
    app.add_handler(CommandHandler("status", status_handler))
    app.add_handler(CommandHandler("candidates", candidates))
    app.add_handler(CommandHandler("approve", approve_trade))
    app.add_handler(CommandHandler("skip", skip_trade))
    app.add_handler(CommandHandler("sell", sell_handler))
    app.add_handler(CommandHandler("hold", hold_handler))
    app.add_handler(CommandHandler("positions", positions_handler))
    app.add_handler(CommandHandler("portfolio", portfolio_handler))
    app.add_handler(CommandHandler("brief", brief_handler))

    # Analytics commands
    app.add_handler(CommandHandler("history", history_handler))
    app.add_handler(CommandHandler("performance", performance_handler))
    app.add_handler(CommandHandler("report", report_handler))

    # Start fill monitor in background thread (daemon — won't block shutdown)
    try:
        from src_trade_tracker import start_fill_monitor
        _fill_monitor = start_fill_monitor()
    except Exception as e:
        print(f"[BOT] Fill monitor failed to start (non-fatal): {e}")

    print("Telegram bot running. Press Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()
