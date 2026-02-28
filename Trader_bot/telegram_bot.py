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
_DB_PATH = os.path.join(_BOT_DIR, "output", "trader.sqlite")
_BRIEF_PATH = os.path.join(_BOT_DIR, "output", "brief.txt")
_APPROVED_TRADES_PATH = os.path.join(_BOT_DIR, "output", "approved_trades.json")

import config
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
    """Return (purse, max_pos, active_positions, invested, owned_symbols) using live overrides + IBKR portfolio."""
    apply_overrides_to_config(config)

    purse_cad = getattr(config, "TRADE_PURSE_CAD", 0) or 0
    if purse_cad > 0:
        purse = float(purse_cad)
        use_cad = True
    else:
        purse = float(getattr(config, "TRADE_PURSE_USD", 1500.0) or 1500.0)
        use_cad = False

    max_pos = int(getattr(config, "MAX_POSITIONS", 6) or 6)

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

    return purse, max_pos, active, invested, owned_symbols


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
        take = round(ref * (1.0 + float(config.TAKE_PROFIT_PCT)), 2)
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
        "⚙️ <b>Settings</b>\n"
        "/purse 3000      Set purse (CAD)\n"
        "/maxpos 6        Set max positions\n"
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
        f"- MAX_POSITIONS:   {getattr(config, 'MAX_POSITIONS', None)}\n"
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
    if not context.args:
        await update.message.reply_text("Usage: /maxpos 6")
        return
    await _propose(update, "MAX_POSITIONS", context.args[0])


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
        take   = _pending_trade["take_price"]
        stop   = _pending_trade["stop_price"]

        await update.message.reply_text(f"Connecting to IBKR and placing order for {symbol}…")

        _fut = concurrent.futures.Future()

        def _place():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                ib = IB()
                ib.connect(config.IB_HOST, config.IB_PORT, clientId=17)
                result = place_bracket(ib, symbol, qty, entry, take, stop)
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
                f"Stop: ${stop:.2f}  ·  Take: ${take:.2f}\n"
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

    purse, max_pos, active, invested, _ = await _get_live_stats()
    msg = fmt_buy_message(recs, purse, max_pos, active_positions=active, invested=invested)
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
            f"Stop: ${match['stop_price']:.2f}  ·  Take: ${match['take_price']:.2f}\n\n"
            f"Reply /confirm to proceed or /cancel to abort",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text(
            f"🕒 <b>Pending order — confirm before placing:</b>\n\n"
            f"📈 <b>{symbol}</b>\n"
            f"Entry: <b>${match['ref_price']:.2f}</b>  ·  Qty: <b>{match['suggested_qty']}</b> shares\n"
            f"Stop: ${match['stop_price']:.2f}  ·  Take: ${match['take_price']:.2f}\n\n"
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

    purse, max_pos, active, invested, owned = await _get_live_stats()
    # Strip already-owned symbols — don't surface noise the user can't act on
    recs       = [r for r in recs       if r["symbol"] not in owned]
    watch_recs = [r for r in watch_recs if r["symbol"] not in owned]
    msg = fmt_brief_message(recs, watch_recs, purse, max_pos, active, invested)
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
            ib.reqMarketDataType(3)  # delayed data — free, no extra subscription needed
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
    max_pos = int(getattr(config, "MAX_POSITIONS", 6) or 6)

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
        f"Total: {len(items)}/{max_pos} positions  ·  ${total_invested:,.2f} invested\n"
        f"Net Gain: {net_flag} {net_dollar} ({net_pct_str})\n"
        f"Best today:  {best_str}\n"
        f"Worst today: {worst_str}\n"
        f"In Green: {green}/{len(items)}  ·  In Red: {red}/{len(items)}"
    )

    body = f"\n{DIVIDER}\n".join(position_blocks)
    warnings = ("\n\n" + "\n".join(warning_lines)) if warning_lines else ""
    await update.message.reply_text(body + summary + warnings, parse_mode="HTML")


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
    app.add_handler(CommandHandler("positions", positions_handler))
    app.add_handler(CommandHandler("portfolio", portfolio_handler))
    app.add_handler(CommandHandler("brief", brief_handler))

    print("Telegram bot running. Press Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()
