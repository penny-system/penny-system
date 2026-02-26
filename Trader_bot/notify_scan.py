import os
import json
import sqlite3
import subprocess
import sys
import urllib.parse
import urllib.request

# Absolute paths derived from this file's location — works regardless of CWD or how the
# script is invoked (directly, via .bat, or as a module from the project root).
_BOT_DIR = os.path.dirname(os.path.abspath(__file__))   # .../Trader_bot
_ROOT_DIR = os.path.dirname(_BOT_DIR)                    # .../penny-system
sys.path.insert(0, _BOT_DIR)
sys.path.insert(0, _ROOT_DIR)

import config
import anthropic
from src_format import DIVIDER, _fmt_date, fmt_candidate, fmt_watch_candidate, fmt_buy_message
from src_news import fetch_and_analyze_news
from src_storage import ensure_schema, update_gate_result
from src_settings import apply_overrides_to_config

DB_PATH = getattr(config, "DB_PATH", None) or os.path.join(_BOT_DIR, "output", "trader.sqlite")
DB_PATH = os.path.normpath(DB_PATH)
STATE_PATH = os.path.join(_BOT_DIR, "output", "notify_state.json")
BRIEF_PATH = os.path.join(_BOT_DIR, "output", "brief.txt")

# Notify rules (tweak later)
MIN_BUY_COUNT = 1
MIN_SCORE = 80.0
MAX_ITEMS_IN_MESSAGE = 8

FAILURE_SILENCE_HOURS = 24

PURSE = float(getattr(config, "PURSE", None) or getattr(config, "TRADE_PURSE_USD", 0) or 0)
MAX_POSITIONS = int(getattr(config, "MAX_POSITIONS", 6) or 6)

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")



def soft_gate_review(payload: dict) -> dict:
    """
    Trigger C soft-gate: review BUY + risky headlines using Claude.
    Returns:
      decision: APPROVE | HOLD | REDUCE | BLOCK
      confidence: 0..1
      reason: short text
    """
    if not ANTHROPIC_API_KEY:
        return {"decision": "HOLD", "confidence": 0.5, "reason": "ANTHROPIC_API_KEY missing."}

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    tools = [
        {
            "name": "submit_review",
            "description": "Submit the trading risk review decision.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "decision": {"type": "string", "enum": ["APPROVE", "HOLD", "REDUCE", "BLOCK"]},
                    "confidence": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": ["decision", "confidence", "reason"],
            },
        }
    ]

    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=256,
        system="You are a trading risk reviewer. A BUY candidate has triggered news risk alerts. Assess whether to proceed.",
        messages=[{"role": "user", "content": json.dumps(payload)}],
        tools=tools,
        tool_choice={"type": "tool", "name": "submit_review"},
    )

    tool_input = message.content[0].input
    return {
        "decision": tool_input["decision"],
        "confidence": float(tool_input["confidence"]),
        "reason": tool_input["reason"],
    }




def die(msg: str, code: int = 1):
    print(f"[ERROR] {msg}")
    sys.exit(code)


def send_telegram(text: str, parse_mode: str = None):
    if not TOKEN:
        die("TELEGRAM_BOT_TOKEN is not set in config.py")
    if not CHAT_ID:
        die("TELEGRAM_CHAT_ID is not set in config.py (set it to your numeric chat id)")

    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {
        "chat_id": str(CHAT_ID),
        "text": text,
        "disable_web_page_preview": "true",
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    data = urllib.parse.urlencode(payload).encode("utf-8")

    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        resp.read()


def load_state():
    if not os.path.exists(STATE_PATH):
        return {"last_action_run_id": 0, "last_action_signature": "", "sent_trigger_a": {}}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            s = json.load(f)
        # Ensure new field exists
        if "sent_trigger_a" not in s or not isinstance(s["sent_trigger_a"], dict):
            s["sent_trigger_a"] = {}
        return s
    except Exception:
        return {"last_action_run_id": 0, "last_action_signature": "", "sent_trigger_a": {}}


def save_state(state: dict):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f)


def run_pipeline():
    import time

    # 1) scan — run from _BOT_DIR so scan_*.csv and output/ resolve correctly
    r1 = subprocess.run(
        [sys.executable, os.path.join(_BOT_DIR, "main_daily_run.py")],
        capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=_BOT_DIR,
    )
    if r1.returncode != 0:
        err = (r1.stderr or "").strip()
        short = ""
        for key in ["ConnectionRefusedError", "TimeoutError", "open orders request timed out", "Make sure API port", "API connection failed"]:
            if key in err:
                short = key
                break
        if not short:
            short = err.splitlines()[-1] if err else "Unknown error"

        state = load_state()
        now = int(time.time())
        if now - state.get("last_pipeline_fail_ts", 0) >= FAILURE_SILENCE_HOURS * 3600:
            send_telegram(f"⚠️ IBKR/pipeline failed: {short}\n(Check TWS running + API enabled on port 7497)")
            state["last_pipeline_fail_ts"] = now
            save_state(state)
        else:
            print("Pipeline failed, but within silence window. No Telegram sent.")
        return False

    # 2) fill qty
    r2 = subprocess.run(
        [sys.executable, os.path.join(_BOT_DIR, "fill_suggested_qty.py"), "--only-buy", "--overwrite"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=_BOT_DIR,
    )
    if r2.returncode != 0:
        print(r2.stdout)
        print(r2.stderr)
        die("fill_suggested_qty.py failed")

    return True


def get_latest_run_id(con: sqlite3.Connection) -> int:
    cur = con.cursor()
    try:
        cur.execute("SELECT MAX(id) FROM runs")
        return int(cur.fetchone()[0] or 0)
    except Exception:
        cur.execute("SELECT MAX(rowid) FROM runs")
        return int(cur.fetchone()[0] or 0)


def get_buy_recs(con: sqlite3.Connection, run_id: int):
    cur = con.cursor()
    cur.execute("""
        SELECT symbol, score_total, confidence, risk_score, ref_price, stop_price, take_price, suggested_qty, rationale
        FROM recommendations
        WHERE run_id = ?
          AND UPPER(TRIM(decision)) = 'BUY'
        ORDER BY score_total DESC, confidence DESC
        LIMIT 50
    """, (run_id,))
    rows = cur.fetchall()

    # Deduplicate by symbol (keep best row)
    best = {}
    for r in rows:
        sym = str(r[0]).upper()
        if sym not in best:
            best[sym] = r
    return list(best.values())


def get_watch_recs(con: sqlite3.Connection, run_id: int):
    cur = con.cursor()
    cur.execute("""
        SELECT symbol, score_total, confidence, ref_price, risk_score, rationale
        FROM recommendations
        WHERE run_id = ?
          AND UPPER(TRIM(decision)) = 'WATCH'
        ORDER BY score_total DESC
        LIMIT 10
    """, (run_id,))
    return cur.fetchall()



def get_owned_symbols() -> set:
    """Return set of uppercase ticker symbols with a non-zero open IBKR position.
    Returns empty set (fail-open) if TWS is unreachable so notifications still send.
    """
    try:
        from ib_insync import IB
        ib = IB()
        ib.connect(config.IB_HOST, config.IB_PORT, clientId=19)
        positions = ib.reqPositions()
        ib.disconnect()
        return {p.contract.symbol.upper() for p in positions if p.position != 0}
    except Exception as e:
        print(f"[WARN] Could not fetch IBKR positions for ownership check: {e}")
        return set()


def _fmt_add_candidate(r: tuple) -> str:
    """Format a BUY rec for an already-held symbol as an add-to-position card."""
    sym   = str(r[0]).upper()
    score = float(r[1] or 0)
    conf  = float(r[2] or 0)
    risk  = float(r[3] or 0) / 100
    refp  = float(r[4] or 0)
    stop  = float(r[5] or 0)
    take  = float(r[6] or 0)
    qty   = int(r[7] or 0)
    return "\n".join([
        f"📈 <b>ADD: {sym}</b>",
        f"Score <b>{score:.1f}</b>  ·  Conf {conf*100:.0f}%  ·  Risk {risk:.2f}",
        f"Ref <b>${refp:.2f}</b>  ·  Stop ${stop:.2f}  ·  Take ${take:.2f}  ·  Qty: {qty}",
        f"<i>Engine suggests this position has room to grow</i>",
    ])


def read_brief_text(max_chars: int = 3200) -> str:
    """
    Telegram has message limits; keep it trimmed.
    """
    if not os.path.exists(BRIEF_PATH):
        return "(brief.txt not found)"
    try:
        with open(BRIEF_PATH, "r", encoding="utf-8", errors="ignore") as f:
            txt = f.read().strip()
        if len(txt) > max_chars:
            txt = txt[:max_chars] + "\n...\n(brief truncated)"
        return txt if txt else "(brief is empty)"
    except Exception as e:
        return f"(failed to read brief: {e})"


def _format_trigger_a_block(trigger_a_new: list[str]) -> list[str]:
    if not trigger_a_new:
        return []
    return ["", "🔴 <b>Trigger A Alerts</b>"] + trigger_a_new[:MAX_ITEMS_IN_MESSAGE]


def main():
    mode = "hourly"
    if "--mode" in sys.argv:
        try:
            mode = sys.argv[sys.argv.index("--mode") + 1].strip().lower()
        except Exception:
            mode = "hourly"

    # Run scan + fill — abort if pipeline failed (Telegram warning already sent inside)
    if not run_pipeline():
        return

    if not os.path.exists(DB_PATH):
        die(f"DB not found at {DB_PATH}")

    con = sqlite3.connect(DB_PATH)
    ensure_schema(con)
    run_id = get_latest_run_id(con)
    recs = get_buy_recs(con, run_id) if run_id > 0 else []
    watch_recs = get_watch_recs(con, run_id) if run_id > 0 else []
    # con stays open so gate results can be written back; closed at end of each mode

    # --- MODE: morning (always send one structured message) ---
    if mode == "morning":
        # Apply runtime overrides so purse/max_positions reflect actual settings
        apply_overrides_to_config(config)
        purse_cad = float(getattr(config, "TRADE_PURSE_CAD", 0) or 0)
        if purse_cad > 0:
            purse_val = purse_cad
        else:
            purse_val = float(getattr(config, "TRADE_PURSE_USD", 1500.0) or 1500.0)
        max_pos = int(getattr(config, "MAX_POSITIONS", 6) or 6)

        # Split candidates: already-owned symbols go to add-to-position section
        owned = get_owned_symbols()
        new_recs = [r for r in recs if str(r[0]).upper() not in owned]
        add_recs  = [r for r in recs if str(r[0]).upper() in owned]

        lines = []
        lines.append(f"🟦 <b>{len(new_recs)} BUY Candidate{'s' if len(new_recs) != 1 else ''} — {_fmt_date()}</b>")
        lines.append(f"💰 Purse: ${purse_val:,.0f}  |  Max: {max_pos}")

        state = load_state()
        trigger_a_new = []

        for r in new_recs[:MAX_ITEMS_IN_MESSAGE]:
            sym   = str(r[0]).upper()
            score = float(r[1] or 0)
            conf  = float(r[2] or 0)
            refp  = float(r[4] or 0)
            qty   = int(r[7] or 0)

            news = fetch_and_analyze_news(sym, limit=3)
            has_news_risk = bool(news.get("risk_hits"))
            gate_note = None

            # Trigger A (2+ keywords in same headline), deduped
            for h in news.get("trigger_a_hits", []):
                key = f"{sym}|{h}".lower().strip()
                if key in state["sent_trigger_a"]:
                    continue
                state["sent_trigger_a"][key] = True
                trigger_a_new.append(f"• {sym}: {h[:80]}")

            # Trigger C: BUY candidate + Trigger A conflict → Claude soft gate
            if news.get("trigger_a_hits"):
                gate_key = f"GATE|{run_id}|{sym}".lower().strip()
                if gate_key not in state["sent_trigger_a"]:
                    payload = {
                        "symbol": sym,
                        "run_id": run_id,
                        "score_total": score,
                        "confidence": conf,
                        "risk_score": float(r[3] or 0),
                        "ref_price": refp,
                        "qty": qty,
                        "rationale": "",
                        "trigger_a_headlines": news.get("trigger_a_hits", [])[:3],
                    }
                    gate = soft_gate_review(payload)
                    update_gate_result(con, run_id, sym,
                                       gate["decision"], gate["confidence"], gate["reason"])
                    gate_note = (
                        f"🛡 Gate: <b>{gate['decision']}</b> "
                        f"({gate['confidence']*100:.0f}%) — {gate['reason'][:80]}"
                    )
                    state["sent_trigger_a"][gate_key] = True

            lines.append("")
            lines.append(DIVIDER)
            lines.append(fmt_candidate({
                "symbol":        sym,
                "score_total":   float(r[1] or 0),
                "confidence":    float(r[2] or 0),
                "risk_score":    float(r[3] or 0),
                "ref_price":     float(r[4] or 0),
                "stop_price":    float(r[5] or 0),
                "take_price":    float(r[6] or 0),
                "suggested_qty": int(r[7] or 0),
                "rationale":     r[8] if len(r) > 8 else "",
            }, gate_note=gate_note, has_news_risk=has_news_risk, emoji="🟩"))

        if not new_recs:
            lines.append("\nNo new BUY candidates in this scan.")

        if add_recs:
            lines.append("")
            lines.append(f"📈 <b>Add Opportunities ({len(add_recs)})</b>")
            for r in add_recs:
                lines.append("")
                lines.append(DIVIDER)
                lines.append(_fmt_add_candidate(r))

        if watch_recs:
            lines.append("")
            lines.append(f"👀 <b>Watchlist ({len(watch_recs)})</b>")
            for r in watch_recs[:5]:
                lines.append("")
                lines.append(DIVIDER)
                lines.append(fmt_watch_candidate({
                    "symbol":      str(r[0]).upper(),
                    "score_total": float(r[1] or 0),
                    "confidence":  float(r[2] or 0),
                    "ref_price":   float(r[3] or 0),
                    "risk_score":  float(r[4] or 0) if len(r) > 4 else 0,
                    "rationale":   str(r[5] or "") if len(r) > 5 else "",
                }))

        lines.extend(_format_trigger_a_block(trigger_a_new))

        lines.append("")
        lines.append(DIVIDER)
        lines.append("/approve SYMBOL — place a bracket order")
        lines.append("/skip SYMBOL — dismiss for this run")

        send_telegram("\n".join(lines), parse_mode="HTML")
        save_state(state)
        con.close()
        print("[OK] Morning brief sent.")
        return

    # --- MODE: hourly (notify only if actionable + avoid spamming same run/signature) ---
    # Split owned vs new before any dedup check
    owned = get_owned_symbols()
    new_recs = [r for r in recs if str(r[0]).upper() not in owned]
    add_recs  = [r for r in recs if str(r[0]).upper() in owned]

    if not new_recs or len(new_recs) < MIN_BUY_COUNT:
        print(f"No new (unowned) BUY candidates for run {run_id}. No notification sent.")
        return

    top_score = float(new_recs[0][1] or 0)
    if top_score < MIN_SCORE:
        print(f"Top new BUY score {top_score:.1f} < MIN_SCORE {MIN_SCORE}. No notification sent.")
        return

    # Dedup on candidate signature alone — run_id changes every scan so cannot be used.
    # Signature covers new_recs only: owned symbols are suppressed, not deduplicated.
    signature = "|".join([f"{r[0]}:{int(r[7] or 0)}" for r in new_recs[:MAX_ITEMS_IN_MESSAGE]])
    state = load_state()

    if state.get("last_action_signature", "") == signature:
        print("Candidate set unchanged since last notification. No notification sent.")
        return

    apply_overrides_to_config(config)
    purse_cad = float(getattr(config, "TRADE_PURSE_CAD", 0) or 0)
    purse_val = purse_cad if purse_cad > 0 else float(getattr(config, "TRADE_PURSE_USD", 1500.0) or 1500.0)
    max_pos = int(getattr(config, "MAX_POSITIONS", 6) or 6)

    lines = []
    lines.append(f"🎆 <b>{len(new_recs)} BUY Candidate{'s' if len(new_recs) != 1 else ''} — {_fmt_date()}</b>")
    lines.append(f"💰 Purse: ${purse_val:,.0f}  |  Max: {max_pos}")
    lines.append("")

    trigger_a_new = []

    for r in new_recs[:MAX_ITEMS_IN_MESSAGE]:
        sym = str(r[0]).upper()
        score = float(r[1] or 0)
        conf = float(r[2] or 0)
        refp = float(r[4] or 0)
        qty = int(r[7] or 0)

        news = fetch_and_analyze_news(sym, limit=3)
        has_news_risk = bool(news.get("risk_hits"))
        gate_note = None

        # Trigger A (dedup)
        for h in news.get("trigger_a_hits", []):
            key = f"{sym}|{h}".lower().strip()
            if key in state["sent_trigger_a"]:
                continue
            state["sent_trigger_a"][key] = True
            trigger_a_new.append(f"• {sym}: {h[:80]}")

        # Soft gate only when conflict exists (Trigger C): BUY candidate + Trigger A headlines
        if news.get("trigger_a_hits"):
            gate_key = f"GATE|{run_id}|{sym}".lower().strip()
            if gate_key not in state["sent_trigger_a"]:
                payload = {
                    "symbol": sym,
                    "run_id": run_id,
                    "score_total": score,
                    "confidence": conf,
                    "risk_score": float(r[3] or 0),
                    "ref_price": refp,
                    "qty": qty,
                    "rationale": "",
                    "trigger_a_headlines": news.get("trigger_a_hits", [])[:3],
                }
                gate = soft_gate_review(payload)
                update_gate_result(con, run_id, sym,
                                   gate["decision"], gate["confidence"], gate["reason"])
                gate_note = (
                    f"🛡 Gate: <b>{gate['decision']}</b> "
                    f"({gate['confidence']*100:.0f}%) — {gate['reason'][:80]}"
                )
                state["sent_trigger_a"][gate_key] = True

        lines.append(DIVIDER)
        lines.append(fmt_candidate({
            "symbol":       str(r[0]).upper(),
            "score_total":  float(r[1] or 0),
            "confidence":   float(r[2] or 0),
            "risk_score":   float(r[3] or 0),
            "ref_price":    float(r[4] or 0),
            "stop_price":   float(r[5] or 0),
            "take_price":   float(r[6] or 0),
            "suggested_qty": int(r[7] or 0),
            "rationale":    r[8] if len(r) > 8 else "",
        }, gate_note=gate_note, has_news_risk=has_news_risk))

    # After loop: Trigger A block
    lines.extend(_format_trigger_a_block(trigger_a_new))

    if add_recs:
        lines.append("")
        lines.append(f"📈 <b>Add Opportunities ({len(add_recs)})</b>")
        for r in add_recs:
            lines.append("")
            lines.append(DIVIDER)
            lines.append(_fmt_add_candidate(r))

    lines.append("")
    lines.append(DIVIDER)
    lines.append("/approve SYMBOL — place a bracket order")
    lines.append("/skip SYMBOL — dismiss for this run")

    send_telegram("\n".join(lines), parse_mode="HTML")

    state["last_action_run_id"] = run_id
    state["last_action_signature"] = signature
    save_state(state)
    con.close()

    print("[OK] Hourly actionable notification sent.")





if __name__ == "__main__":
    main()


