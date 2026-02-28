# src_report.py
#
# HTML performance report generator.
# Saves reports to output/reports/. Email delivery is stubbed (TODO).
#
# Usage:
#   from src_report import generate_report
#   path = generate_report(conn)   # returns file path
#
import os
import sys
import sqlite3
from datetime import datetime

_BOT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _BOT_DIR)

import config
from src_learning import run_learning_analysis


_REPORT_DIR = os.path.join(_BOT_DIR, getattr(config, "REPORT_OUTPUT_DIR", "output/reports"))

_CSS = """
body { font-family: 'Segoe UI', Arial, sans-serif; background:#0d1117; color:#c9d1d9;
       margin:0; padding:20px; }
h1   { color:#58a6ff; border-bottom:1px solid #30363d; padding-bottom:8px; }
h2   { color:#79c0ff; margin-top:32px; border-left:3px solid #58a6ff; padding-left:10px; }
h3   { color:#a5d6ff; }
table { border-collapse:collapse; width:100%; margin-top:12px; font-size:0.9em; }
th    { background:#161b22; color:#8b949e; border:1px solid #30363d;
        padding:6px 10px; text-align:left; }
td    { border:1px solid #30363d; padding:6px 10px; }
tr:nth-child(even) { background:#161b22; }
.win   { color:#3fb950; }
.loss  { color:#f85149; }
.even  { color:#d29922; }
.stat-grid { display:grid; grid-template-columns: repeat(auto-fill, minmax(160px,1fr));
             gap:12px; margin-top:16px; }
.stat-box  { background:#161b22; border:1px solid #30363d; border-radius:6px;
             padding:12px 16px; }
.stat-val  { font-size:1.4em; font-weight:bold; color:#58a6ff; }
.stat-lbl  { font-size:0.8em; color:#8b949e; margin-top:4px; }
.obs-list  { list-style:none; padding:0; }
.obs-list li { background:#161b22; border-left:3px solid #58a6ff;
               margin:6px 0; padding:8px 12px; border-radius:0 4px 4px 0; }
.tag-preliminary { color:#d29922; font-size:0.75em; }
.tag-emerging    { color:#79c0ff; font-size:0.75em; }
.tag-reliable    { color:#3fb950; font-size:0.75em; }
.footer { margin-top:40px; border-top:1px solid #30363d; padding-top:12px;
          color:#484f58; font-size:0.8em; }
"""


def _fmt_pnl(v: float) -> str:
    sign = "+" if v >= 0 else ""
    return f"{sign}${v:.2f}"


def _fmt_pct(v: float) -> str:
    sign = "+" if v >= 0 else ""
    return f"{sign}{v*100:.1f}%"


def _outcome_cls(outcome: str) -> str:
    return {"WIN": "win", "LOSS": "loss", "BREAKEVEN": "even"}.get(outcome, "")


def _tier_tag(tier: str) -> str:
    cls = {"preliminary": "tag-preliminary", "emerging": "tag-emerging",
           "reliable": "tag-reliable"}.get(tier, "")
    return f' <span class="{cls}">[{tier}]</span>' if cls else ""


def _hold_str(seconds: int) -> str:
    if not seconds:
        return "—"
    if seconds >= 3600:
        h, m = divmod(seconds, 3600)
        return f"{h}h {m//60}m"
    return f"{seconds // 60}m"


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------

def _section_overview(ov: dict) -> str:
    n        = ov.get("total_trades", 0)
    wr       = ov.get("win_rate", 0.0)
    tot_pnl  = ov.get("total_net_pnl", 0.0)
    avg_pnl  = ov.get("avg_net_pnl", 0.0)
    avg_hold = ov.get("avg_hold_hours", 0.0)
    wins     = ov.get("wins", 0)
    losses   = ov.get("losses", 0)
    tier     = ov.get("confidence_tier", "")

    tier_note = {
        "insufficient": "(insufficient data — keep trading!)",
        "preliminary":  "(early data — patterns not yet reliable)",
        "emerging":     "(emerging patterns — treat as directional)",
        "reliable":     "(statistically reliable)",
    }.get(tier, "")

    pf      = ov.get("profit_factor")
    exp     = ov.get("expectancy", 0.0)
    pf_str  = f"{pf:.2f}" if pf is not None else "∞"
    exp_str = _fmt_pnl(exp)

    stats = [
        ("Total Trades", str(n)),
        ("Win Rate", f"{wr*100:.1f}%"),
        ("Wins / Losses", f"{wins} / {losses}"),
        ("Total Net P&L", _fmt_pnl(tot_pnl)),
        ("Avg Net P&L", _fmt_pnl(avg_pnl)),
        ("Profit Factor", pf_str),
        ("Expectancy", exp_str),
        ("Avg Hold Time", f"{avg_hold:.1f}h"),
    ]
    boxes = "".join(
        f'<div class="stat-box"><div class="stat-val">{v}</div>'
        f'<div class="stat-lbl">{l}</div></div>'
        for l, v in stats
    )

    exit_reasons = ov.get("exit_reasons", {})
    er_rows = "".join(
        f"<tr><td>{r}</td><td>{c}</td></tr>"
        for r, c in sorted(exit_reasons.items(), key=lambda x: -x[1])
    )
    er_table = (
        f"<table><tr><th>Exit Reason</th><th>Count</th></tr>{er_rows}</table>"
        if er_rows else ""
    )

    return f"""
<h2>Overview {tier_note}</h2>
<div class="stat-grid">{boxes}</div>
{er_table}
"""


def _section_observations(obs: list[str]) -> str:
    items = "".join(f"<li>{o}</li>" for o in obs)
    return f"""
<h2>Key Observations</h2>
<ul class="obs-list">{items}</ul>
"""


def _section_temporal(te: dict) -> str:
    if te.get("total", 0) == 0:
        return ""

    fh = te.get("first_half", {})
    lh = te.get("last_half", {})
    l10 = te.get("last_10", {})
    trend = te.get("trend", "STABLE")
    trend_cls = {"IMPROVING": "win", "DEGRADING": "loss", "STABLE": "even"}.get(trend, "even")
    dd = te.get("max_drawdown", 0.0)
    cum_max = te.get("cum_max_pnl", 0.0)

    return f"""
<h2>Temporal Analysis</h2>
<table>
<tr><th></th><th>N</th><th>Win Rate</th><th>Net P&L</th></tr>
<tr><td>First half</td><td>{fh.get('n',0)}</td>
    <td>{fh.get('win_rate',0)*100:.1f}%</td>
    <td>{_fmt_pnl(fh.get('net_pnl',0))}</td></tr>
<tr><td>Last half</td><td>{lh.get('n',0)}</td>
    <td>{lh.get('win_rate',0)*100:.1f}%</td>
    <td>{_fmt_pnl(lh.get('net_pnl',0))}</td></tr>
<tr><td>Last 10 trades</td><td>{l10.get('n',0)}</td>
    <td>{l10.get('win_rate',0)*100:.1f}%</td>
    <td>{_fmt_pnl(l10.get('net_pnl',0))}</td></tr>
</table>
<p>Trend: <span class="{trend_cls}"><strong>{trend}</strong></span> &nbsp;|&nbsp;
   Max drawdown: <span class="loss">{_fmt_pnl(dd)}</span> ({te.get('dd_trades','')}) &nbsp;|&nbsp;
   Cumulative peak: {_fmt_pnl(cum_max)}</p>
"""


def _section_signals(sig: dict) -> str:
    def _feature_table(feature_name: str, buckets: list[dict]) -> str:
        if not buckets:
            return ""
        rows = "".join(
            f"<tr><td>{b['label']}{_tier_tag(b['tier'])}</td>"
            f"<td>{b['n']}</td>"
            f"<td>{b['win_rate']*100:.1f}%</td>"
            f"<td>{_fmt_pnl(b['avg_pnl'])}</td></tr>"
            for b in buckets
        )
        return f"""
<h3>{feature_name}</h3>
<table>
<tr><th>Range</th><th>N</th><th>Win Rate</th><th>Avg Net P&L</th></tr>
{rows}
</table>"""

    label_map = {
        "vol_surge":    "Volume Surge",
        "score_total":  "Score Total",
        "confidence":   "Confidence",
        "risk_score":   "Risk Score",
        "ret_5":        "5-Day Return at Entry",
        "breakout":     "Breakout Flag",
        "setup_type":   "Setup Type",
    }

    parts = [_feature_table(label_map.get(k, k), v) for k, v in sig.items() if v]
    if not any(parts):
        return "<h2>Signal Analysis</h2><p>Insufficient data for signal-level analysis yet.</p>"

    return "<h2>Signal Analysis</h2>" + "".join(p for p in parts if p)


def _section_combinations(cm: dict) -> str:
    all_combos = cm.get("all", [])
    if not all_combos:
        return "<h2>Combination Analysis</h2><p>Insufficient data for combination analysis yet.</p>"

    rows = "".join(
        f"<tr><td>{c['label']}{_tier_tag(c['tier'])}</td>"
        f"<td>{c['n']}</td>"
        f"<td>{c['win_rate']*100:.1f}%</td>"
        f"<td>{_fmt_pnl(c['avg_pnl'])}</td></tr>"
        for c in all_combos
    )
    return f"""
<h2>Combination Analysis</h2>
<table>
<tr><th>Combination</th><th>N</th><th>Win Rate</th><th>Avg Net P&L</th></tr>
{rows}
</table>"""


def _section_slippage(sl: dict) -> str:
    n = sl.get("n", 0)
    if n == 0:
        return ""

    ent    = sl.get("avg_entry_slip", 0.0)
    ext    = sl.get("avg_exit_slip", 0.0)
    paper  = sl.get("paper_net", 0.0)
    lo     = sl.get("live_est_low", 0.0)
    hi     = sl.get("live_est_high", 0.0)
    opt    = sl.get("pct_optimism", 0.0)
    tp_sl  = sl.get("avg_tp_slip")
    sl_sl  = sl.get("avg_sl_slip")

    tp_row = (f"<tr><td>Avg take-profit exit slippage</td>"
              f"<td>${tp_sl:+.4f}/share</td></tr>") if tp_sl is not None else ""
    sl_row = (f"<tr><td>Avg stop-loss exit slippage</td>"
              f"<td>${sl_sl:+.4f}/share</td></tr>") if sl_sl is not None else ""

    return f"""
<h2>Slippage Analysis (Paper → Live Readiness)</h2>
<table>
<tr><th>Metric</th><th>Value</th></tr>
<tr><td>Avg entry slippage (fill vs ref_price)</td><td>${ent:+.4f}/share</td></tr>
<tr><td>Avg exit slippage</td><td>${ext:+.4f}/share</td></tr>
{tp_row}
{sl_row}
<tr><td>Paper net P&L</td><td>{_fmt_pnl(paper)}</td></tr>
<tr><td>Estimated live P&L range</td><td>{_fmt_pnl(lo)} to {_fmt_pnl(hi)}</td></tr>
<tr><td>Paper result optimism vs live</td><td>~{opt*100:.0f}% (normal for paper trading)</td></tr>
</table>
"""


def _section_open_positions(conn: sqlite3.Connection) -> str:
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT symbol, entry_price, entry_qty, stop_price, take_price, entry_time
            FROM open_positions
            WHERE status = 'open'
            ORDER BY entry_time DESC
        """)
        rows = cur.fetchall()
    except Exception:
        return ""

    if not rows:
        return ""

    now = datetime.now()
    tr_rows = ""
    for sym, entry_p, qty, stop_p, take_p, entry_time in rows:
        try:
            et       = datetime.fromisoformat(entry_time) if entry_time else None
            hold_str = _hold_str(int((now - et).total_seconds())) if et else "—"
        except Exception:
            hold_str = "—"
        stop_str = f"${stop_p:.4f}" if stop_p else "—"
        take_str = f"${take_p:.4f}" if take_p else "—"
        tr_rows += (
            f"<tr>"
            f"<td><strong>{sym or '—'}</strong></td>"
            f"<td>${entry_p:.4f}</td>"
            f"<td>{qty}</td>"
            f"<td>{stop_str}</td>"
            f"<td>{take_str}</td>"
            f"<td>{hold_str}</td>"
            f"</tr>"
        )

    return f"""
<h2>Open Positions ({len(rows)})</h2>
<table>
<tr><th>Symbol</th><th>Entry</th><th>Qty</th><th>Stop</th><th>Take</th><th>Hold</th></tr>
{tr_rows}
</table>"""


def _section_trade_history(conn: sqlite3.Connection) -> str:
    cur = conn.cursor()
    cur.execute("""
        SELECT ct.symbol, ct.closed_at, ct.entry_price, ct.exit_price,
               ct.exit_qty, ct.net_pnl, ct.outcome, ct.exit_reason, ct.hold_seconds,
               rs.breakout, rs.vol_surge, rs.confidence, rs.gate_decision
        FROM closed_trades ct
        LEFT JOIN recommendation_snapshots rs
            ON rs.snapshot_id = (
                SELECT snapshot_id FROM recommendation_snapshots
                WHERE symbol = ct.symbol
                ORDER BY snapshot_id DESC LIMIT 1
            )
        ORDER BY ct.trade_id DESC
        LIMIT 100
    """)
    rows = cur.fetchall()
    if not rows:
        return ""

    tr_rows = ""
    for (sym, closed_at, entry, exit_p, qty, net_pnl, outcome, reason,
         hold_s, breakout, vol_surge, confidence, gate) in rows:
        cls = _outcome_cls(outcome or "")
        dt  = closed_at[:10] if closed_at else "—"

        # Key signals summary
        sig_parts = []
        if breakout == 1:
            sig_parts.append("brkout")
        if vol_surge is not None:
            sig_parts.append(f"vol {vol_surge:.1f}x")
        if confidence is not None:
            sig_parts.append(f"conf {confidence:.2f}")
        if gate and gate not in ("", "APPROVE"):
            sig_parts.append(f"gate={gate}")
        sig_str = ", ".join(sig_parts) if sig_parts else "—"

        tr_rows += (
            f"<tr>"
            f"<td><strong>{sym or '—'}</strong></td>"
            f"<td>{dt}</td>"
            f"<td>${entry:.4f}</td>"
            f"<td>${exit_p:.4f}</td>"
            f"<td>{qty}</td>"
            f"<td class='{cls}'>{_fmt_pnl(net_pnl or 0)}</td>"
            f"<td class='{cls}'>{outcome or '—'}</td>"
            f"<td>{reason or '—'}</td>"
            f"<td>{_hold_str(hold_s or 0)}</td>"
            f"<td style='font-size:0.8em;color:#8b949e;'>{sig_str}</td>"
            f"</tr>"
        )

    return f"""
<h2>Trade History (last 100)</h2>
<table>
<tr><th>Symbol</th><th>Date</th><th>Entry</th><th>Exit</th>
    <th>Qty</th><th>Net P&L</th><th>Outcome</th><th>Reason</th><th>Hold</th><th>Signals</th></tr>
{tr_rows}
</table>"""


# ---------------------------------------------------------------------------
# Main builder
# ---------------------------------------------------------------------------

def _build_html(analysis: dict, conn: sqlite3.Connection) -> str:
    ov   = analysis.get("overview", {})
    obs  = analysis.get("observations", [])
    te   = analysis.get("temporal", {})
    sig  = analysis.get("signals", {})
    cm   = analysis.get("combinations", {})
    sl   = analysis.get("slippage", {})
    gen  = analysis.get("generated_at", "")

    body = (
        _section_overview(ov)
        + _section_observations(obs)
        + _section_open_positions(conn)
        + _section_temporal(te)
        + _section_signals(sig)
        + _section_combinations(cm)
        + _section_slippage(sl)
        + _section_trade_history(conn)
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Penny System — Performance Report</title>
<style>{_CSS}</style>
</head>
<body>
<h1>Penny System — Performance Report</h1>
<p style="color:#484f58; font-size:0.85em;">Generated: {gen[:19].replace('T',' ')} UTC</p>
{body}
<div class="footer">
  Penny System &nbsp;|&nbsp; Paper trading only &nbsp;|&nbsp;
  Data: {ov.get('total_trades',0)} closed trade(s) as of {gen[:10]}
</div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_report(conn: sqlite3.Connection) -> str:
    """
    Run full learning analysis, build an HTML report, save to output/reports/.
    Returns the absolute file path.
    """
    os.makedirs(_REPORT_DIR, exist_ok=True)
    analysis = run_learning_analysis(conn)
    html     = _build_html(analysis, conn)

    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"report_{ts}.html"
    path     = os.path.join(_REPORT_DIR, filename)

    with open(path, "w", encoding="utf-8") as f:
        f.write(html)

    return path


def email_report(html_path: str, recipients: list[str]):
    """
    TODO: Email report to recipients.
    SMTP delivery not yet configured — save report locally and notify via Telegram instead.
    """
    print(f"[REPORT] TODO: email delivery not configured. Report saved at: {html_path}")
    print(f"[REPORT] Intended recipients: {recipients}")
