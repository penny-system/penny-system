# Penny System — User Guide

## What You'll Receive Each Day

### 1. Morning Brief (8:00 AM)
An MP3 audio summary is sent first, covering your open positions, today's buy candidates, and watchlist highlights. This is followed by a text brief and, if there are actionable candidates, a formatted BUY message.

### 2. Hourly Scan
Runs throughout the day. A notification is only sent if new actionable BUY candidates appear that haven't already been actioned.

---

## Reading a BUY Candidate Message

```
🚨 2 BUY Candidates — Sat, Feb 22
💰 Purse: $5,000  |  Max: 6 positions

━━━━━━━━━━━━━━
📈 GRCE  ⚠️
Score 84.5  ·  Conf 72%  ·  Risk 0.30
Ref $0.42  ·  Stop $0.37  ·  Take $0.55
Qty: 500 shares
🧠 Gate: HOLD (78%) — Near-term dilution risk
Why: Momentum breakout on volume confirmation

━━━━━━━━━━━━━━
📈 BRLS
Score 82.1  ·  Conf 68%  ·  Risk 0.20
Ref $1.15  ·  Stop $1.00  ·  Take $1.45
Qty: 250 shares

━━━━━━━━━━━━━━
python approve.py
```

| Field | Meaning |
|-------|---------|
| **Score** | Composite signal score (≥ 80 = BUY threshold) |
| **Conf** | Scoring engine's confidence in the BUY signal (higher = stronger setup) |
| **Risk** | Risk score — lower is better (penalises volatility, dilution signals, etc.) |
| **Ref** | Reference entry price at time of scan |
| **Stop** | Stop-loss price — order exits here if trade goes against you |
| **Take** | Take-profit price — target exit |
| **Qty** | Suggested share quantity based on your purse and position sizing rules |
| **⚠️** | News risk detected for this ticker (see Trigger A section below) |
| **Why** | First sentence of the scoring engine's rationale |

---

## Soft Gate Decisions (🧠 Gate)

When a BUY candidate also has active news risk (Trigger A), Claude reviews it and returns one of four decisions:

| Decision | Meaning | What Happens |
|----------|---------|--------------|
| **APPROVE** | Risk flagged but assessed as acceptable — proceed | `approve.py` will place the order |
| **HOLD** | Pause — news situation needs to clarify before acting | `approve.py` skips this ticker until manually overridden |
| **REDUCE** | Proceed but with a smaller position | `approve.py` will halve the suggested qty |
| **BLOCK** | Hard veto — do not place this order | `approve.py` skips this ticker; no override |

The percentage shown (e.g. `78%`) is Claude's self-assessed certainty in its own decision. Think of it as a signal strength, not a trade probability.

---

## Trigger A Alerts (🔴)

Trigger A fires when a single news headline contains **2 or more risk keywords simultaneously** (e.g. "offering" + "warrant" in the same sentence). This is a stronger signal than a single keyword hit and always triggers a Soft Gate review if the ticker is also a BUY candidate.

Risk keywords include: offering, warrant, dilution, reverse split, bankruptcy, going concern, halt, investigation, lawsuit, and others.

---

## Acting on Recommendations

After reviewing the morning message, run the following command on your PC:

```
python approve.py
```

This script reads the latest BUY recommendations from the database and places bracket orders (entry limit + take-profit + stop-loss) via IBKR TWS for any candidate you approve.

> **No orders are placed automatically.** Every trade requires your explicit approval via `approve.py`.

---

## Telegram Commands (Planned)

The following commands are intended for future Telegram bot interaction. They are not yet active but reflect the intended workflow:

| Command | Action |
|---------|--------|
| `/status` | Show current open positions and today's run summary |
| `/approve GRCE` | Approve a specific BUY candidate for order placement |
| `/skip GRCE` | Skip a candidate for this run (won't re-alert today) |
| `/block GRCE` | Permanently skip a ticker until manually cleared |
| `/positions` | List all open IBKR positions with avg cost |
| `/brief` | Re-send today's morning brief text |

---

## Pipeline Failure Alerts

If the IBKR connection or pipeline fails, you will receive:

```
⚠️ IBKR/pipeline failed: ConnectionRefusedError
(Check TWS running + API enabled on port 7497)
```

**Common causes:**
- TWS / IB Gateway is not running
- API is not enabled in TWS settings (Edit → Global Configuration → API → Settings)
- Port mismatch (paper trading uses 7497, live uses 7496)

After fixing, the next scheduled run will retry automatically. Repeated failures within a 24-hour window are silenced to avoid spam.

---

## Key Settings (config.py / .env)

| Variable | Purpose |
|----------|---------|
| `PURSE` | Total capital allocated for this strategy |
| `MAX_POSITIONS` | Maximum concurrent open positions |
| `MIN_SCORE` | Minimum score to trigger a BUY notification (default: 80) |
| `IB_PORT` | TWS API port (7497 = paper, 7496 = live) |
| `MARKETAUX_API_KEY` | News API key for headline analysis |
| `ANTHROPIC_API_KEY` | Claude API key for Soft Gate reviews |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Telegram delivery credentials |
