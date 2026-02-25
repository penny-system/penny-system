from pathlib import Path
import config


def _pct(a: float, b: float) -> float:
    if b == 0:
        return 0.0
    return (a - b) / b


def write_brief(run_id: int, portfolio: list[dict], recs: list[dict], filename: str = "brief.txt") -> str:
    out_dir = Path(getattr(config, "OUTPUT_DIR", "output"))
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / filename

    move_thr = float(getattr(config, "HOLDING_MOVE_ALERT_PCT", 0.05))
    lookback_hours = int(getattr(config, "NEWS_LOOKBACK_HOURS", 72))

    lines = []
    lines.append(f"RUN {run_id} — Morning Brief")

    regimes = [r.get("regime") for r in recs if r.get("regime")]
    regime = regimes[0] if regimes else "neutral"
    lines.append(f"REGIME: {regime}")
    lines.append(f"NEWS LOOKBACK: {lookback_hours}h")
    lines.append("")

    lines.append("HOLDINGS UPDATE (only meaningful changes)")
    if not portfolio:
        lines.append("- (none)")
    else:
        any_meaningful = False
        for p in portfolio:
            sym = (p.get("symbol") or "").upper()
            pos = float(p.get("position", 0))
            avg = float(p.get("avg_cost", 0) or 0)
            last = float(p.get("market_price", 0) or 0)
            upnl = float(p.get("unrealized_pnl", 0) or 0)

            if avg > 0 and last > 0:
                pct_from_avg = _pct(last, avg)
                if abs(pct_from_avg) >= move_thr:
                    any_meaningful = True
                    lines.append(
                        f"- {sym}: pos={pos:.2f} avg={avg:.2f} last={last:.2f} "
                        f"uPnL={upnl:.2f} move_vs_avg={pct_from_avg*100:.1f}%"
                    )
        if not any_meaningful:
            lines.append(f"- No holdings moved more than ±{move_thr*100:.0f}% vs avg cost.")
    lines.append("")

    buys = [r for r in recs if r.get("decision") == "BUY"]
    setups = [r for r in recs if r.get("decision") == "SETUP"]
    watches = [r for r in recs if r.get("decision") == "WATCH"]
    no_trades = [r for r in recs if r.get("decision") == "NO_TRADE"]

    lines.append("NEW OPPORTUNITIES (deduped by symbol)")

    if not buys:
        lines.append("BUY CANDIDATES: none today.")
    else:
        lines.append("BUY CANDIDATES (need your approval)")
        for r in buys[:10]:
            qty = int(r.get("suggested_qty", 0) or 0)
            alloc = float(r.get("suggested_alloc_usd", 0) or 0)
            lines.append(
                f"- {r['symbol']} [{r.get('horizon')}] score={r.get('score_total'):.1f} conf={r.get('confidence'):.2f} "
                f"risk={r.get('risk_score'):.1f} ref={r.get('ref_price'):.2f} stop={r.get('stop_price'):.2f} take={r.get('take_price'):.2f}"
            )
            lines.append(f"  sizing: suggested_qty={qty} suggested_alloc_usd≈{alloc:.2f}")
            lines.append(f"  trigger: {r.get('entry_trigger')}")
            lines.append(f"  why: {r.get('rationale')}")
            lines.append(f"  flags: {r.get('risk_flags', 'none')}")
            lines.append(
                f"  news(72h): count={int(r.get('news_count',0))} risk_hits={int(r.get('news_risk_hits',0))} pos_hits={int(r.get('news_pos_hits',0))}"
            )
            top = (r.get("news_top") or "").strip()
            if top:
                lines.append(f"  top_headline: {top}")

    lines.append("")
    lines.append("SETUPS (almost buy; wait for confirmation)")
    if not setups:
        lines.append("- (none)")
    else:
        for r in setups[:10]:
            lines.append(
                f"- {r['symbol']} [{r.get('horizon')}] score={r.get('score_total'):.1f} conf={r.get('confidence'):.2f} "
                f"risk={r.get('risk_score'):.1f} ref={r.get('ref_price'):.2f}"
            )
            lines.append(f"  why: {r.get('rationale')}")
            lines.append(f"  flags: {r.get('risk_flags', 'none')}")
            lines.append(
                f"  news(72h): count={int(r.get('news_count',0))} risk_hits={int(r.get('news_risk_hits',0))} pos_hits={int(r.get('news_pos_hits',0))}"
            )
            top = (r.get("news_top") or "").strip()
            if top:
                lines.append(f"  top_headline: {top}")

    lines.append("")
    lines.append("WATCHLIST (monitor, no action)")
    if not watches:
        lines.append("- (none)")
    else:
        for r in watches[:10]:
            lines.append(
                f"- {r['symbol']} [{r.get('horizon')}] score={r.get('score_total'):.1f} conf={r.get('confidence'):.2f} "
                f"risk={r.get('risk_score'):.1f} ref={r.get('ref_price'):.2f}"
            )
            lines.append(f"  why: {r.get('rationale')}")

    lines.append("")
    lines.append("LOW QUALITY / NO-TRADE (top 5 shown)")
    if not no_trades:
        lines.append("- (none)")
    else:
        for r in no_trades[:5]:
            lines.append(f"- {r['symbol']} score={r.get('score_total'):.1f} risk={r.get('risk_score'):.1f}")

    path.write_text("\n".join(lines), encoding="utf-8")
    return str(path)
