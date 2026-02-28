"""
src_universe_refresh.py
-----------------------
Weekly dynamic universe refresh.

Entry point: run_weekly_universe_refresh(ib)

Sources:
  1. IBKR scanner (TOP_PERC_GAIN + HIGH_VS_13W_HL)
  2. yfinance validation + optional yf.screen() extras
  3. Static CSV fallback if combined results < MIN_DYNAMIC_SYMBOLS

Output:
  - Trader_bot/universe_dynamic.txt   (one symbol per line)
  - Trader_bot/output/universe_refresh_log.txt  (append-only log)
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

from ib_insync import IB, ScannerSubscription

import config

# ─── Paths (all anchored to this file's directory) ────────────────────────────
BASE = Path(__file__).resolve().parent
DYNAMIC_TXT = BASE / "universe_dynamic.txt"
REFRESH_LOG = BASE / "output" / "universe_refresh_log.txt"
SCAN_ACTIVE = BASE / "scan_active.csv"
SCAN_GAINERS = BASE / "scan_gainers.csv"

# ─── Filter constants ─────────────────────────────────────────────────────────
MIN_PRICE = 1.00
MAX_PRICE = 8.00
MIN_AVG_VOL = 500_000
MIN_DYNAMIC_SYMBOLS = 20  # supplement from static CSVs below this threshold


# ─── IBKR Scanner ─────────────────────────────────────────────────────────────

def _run_ibkr_scanner(ib: IB, scan_code: str) -> list[str]:
    """Run a single IBKR scanner and return a list of symbols."""
    sub = ScannerSubscription(
        instrument="STK",
        locationCode="STK.US.MAJOR",   # NYSE, NASDAQ, AMEX
        scanCode=scan_code,
        numberOfRows=50,
        abovePrice=MIN_PRICE,
        belowPrice=MAX_PRICE,
        aboveVolume=int(MIN_AVG_VOL),
    )
    try:
        results = ib.reqScannerData(sub, [], [])
        ib.sleep(1)
        symbols = []
        for item in results:
            try:
                sym = item.contractDetails.contract.symbol
                # Accept tickers that are purely alphabetic (1–5 chars)
                if sym and sym.isalpha() and 1 <= len(sym) <= 5:
                    symbols.append(sym.upper())
            except Exception:
                continue
        return symbols
    except Exception as e:
        print(f"[REFRESH] IBKR scanner {scan_code} error: {e}")
        return []


def _ibkr_scan(ib: IB) -> list[str]:
    """Run both scanner types and merge/deduplicate results."""
    syms_gain = _run_ibkr_scanner(ib, "TOP_PERC_GAIN")
    syms_hl = _run_ibkr_scanner(ib, "HIGH_VS_13W_HL")
    combined = list(dict.fromkeys(syms_gain + syms_hl))
    print(
        f"[REFRESH] IBKR scan: {len(syms_gain)} TOP_PERC_GAIN, "
        f"{len(syms_hl)} HIGH_VS_13W_HL -> {len(combined)} unique"
    )
    return combined


# ─── yfinance Validation ──────────────────────────────────────────────────────

def _validate_yfinance(symbols: list[str]) -> tuple[list[str], list[str]]:
    """
    Validate each symbol with yfinance.
    Uses fast_info first; falls back to full .info on failure.
    Returns (valid_symbols, failed_symbols).
    """
    if not symbols:
        return [], []

    try:
        import yfinance as yf
    except ImportError:
        print("[REFRESH] yfinance not installed -- skipping validation, keeping all IBKR symbols")
        return symbols, []

    valid, failed = [], []
    for sym in symbols:
        try:
            t = yf.Ticker(sym)
            fi = t.fast_info
            price = float(getattr(fi, "last_price", None) or 0)
            vol = int(getattr(fi, "three_month_average_volume", None) or 0)

            # fast_info sometimes returns 0 for thinly-traded names; try full info
            if price == 0:
                info = t.info
                price = float(
                    info.get("currentPrice")
                    or info.get("regularMarketPrice")
                    or 0
                )
                vol = int(info.get("averageVolume") or info.get("averageVolume10days") or 0)

            if MIN_PRICE <= price <= MAX_PRICE and vol >= MIN_AVG_VOL:
                valid.append(sym)
            else:
                failed.append(sym)
        except Exception:
            failed.append(sym)

    print(f"[REFRESH] yfinance validation: {len(valid)} passed, {len(failed)} dropped")
    return valid, failed


def _yfinance_screen_extra() -> list[str]:
    """
    Optionally pull additional symbols from yf.screen() (day_gainers).
    Returns an empty list if yf.screen() is not available or fails.
    """
    try:
        import yfinance as yf
        result = yf.screen("day_gainers", count=100)
        quotes = result.get("quotes", [])
        symbols = []
        for q in quotes:
            sym = q.get("symbol", "")
            # Strip exchange suffix if present (e.g. "AAPL" from "AAPL.NMS")
            sym = sym.split(".")[0].strip().upper()
            price = float(q.get("regularMarketPrice") or 0)
            vol = int(q.get("averageVolume") or 0)
            if sym and sym.isalpha() and MIN_PRICE <= price <= MAX_PRICE and vol >= MIN_AVG_VOL:
                symbols.append(sym)
        print(f"[REFRESH] yf.screen() extra: {len(symbols)} symbols")
        return symbols
    except Exception as e:
        print(f"[REFRESH] yf.screen() not available or failed: {e}")
        return []


# ─── Static CSV Fallback ──────────────────────────────────────────────────────

def _load_csv_symbols(filepath: Path) -> list[str]:
    """Load first-column symbols from a CSV file (skips header)."""
    if not filepath.exists():
        return []
    syms = []
    try:
        with filepath.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f)
            next(reader, None)  # skip header
            for row in reader:
                if not row:
                    continue
                sym = row[0].strip().upper()
                if sym and sym.isalnum():
                    syms.append(sym)
    except Exception:
        pass
    return syms


def _static_fallback_validated() -> list[str]:
    """
    Load scan_active.csv + scan_gainers.csv, validate with yfinance,
    and return only symbols that pass price/volume checks.
    """
    raw = _load_csv_symbols(SCAN_ACTIVE) + _load_csv_symbols(SCAN_GAINERS)
    raw = list(dict.fromkeys(raw))
    if not raw:
        return []
    valid, _ = _validate_yfinance(raw)
    print(f"[REFRESH] Static fallback: {len(valid)} validated symbols from CSVs")
    return valid


# ─── Main Entry Point ─────────────────────────────────────────────────────────

def run_weekly_universe_refresh(ib: IB) -> list[str]:
    """
    Orchestrates the full weekly refresh:
      1. IBKR scanner (two scan types merged)
      2. yfinance validation + optional screen extras
      3. Static CSV fallback if combined results < MIN_DYNAMIC_SYMBOLS
      4. Deduplicate, sort, cap at MAX_UNIVERSE_SIZE
      5. Write universe_dynamic.txt and append to refresh log

    Returns the final list of symbols.
    """
    max_size = getattr(config, "MAX_UNIVERSE_SIZE", 150)

    # SOURCE 1 — IBKR scanner
    ibkr_symbols = _ibkr_scan(ib)
    n_ibkr = len(ibkr_symbols)

    # SOURCE 2 — yfinance validation of IBKR results
    validated, _ = _validate_yfinance(ibkr_symbols)

    # SOURCE 2b — yfinance screen extras (best-effort)
    yf_extra = _yfinance_screen_extra()
    n_yf = len(yf_extra)

    combined = list(dict.fromkeys(validated + yf_extra))

    # FALLBACK — supplement from static CSVs if below threshold
    n_static = 0
    if len(combined) < MIN_DYNAMIC_SYMBOLS:
        fallback = _static_fallback_validated()
        before = len(combined)
        combined = list(dict.fromkeys(combined + fallback))
        n_static = len(combined) - before
        print(
            f"[REFRESH] Below {MIN_DYNAMIC_SYMBOLS} symbols after live sources "
            f"-- added {n_static} from static fallback"
        )

    # Sort alphabetically, deduplicate, cap
    final = sorted(set(combined))[:max_size]

    # Write universe_dynamic.txt
    DYNAMIC_TXT.write_text("\n".join(final) + "\n", encoding="utf-8")
    print(f"[REFRESH] Wrote {len(final)} symbols to {DYNAMIC_TXT.name}")

    # Append to refresh log
    REFRESH_LOG.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_line = (
        f"[{ts}] Refresh complete -- "
        f"{n_ibkr} from IBKR, {n_yf} from yfinance, "
        f"{n_static} from static fallback, "
        f"{len(final)} TOTAL unique symbols\n"
    )
    with REFRESH_LOG.open("a", encoding="utf-8") as f:
        f.write(log_line)
    print(f"[REFRESH] {log_line.strip()}")

    return final


# ─── Standalone Entry Point ───────────────────────────────────────────────────

if __name__ == "__main__":
    # Dedicated clientId for standalone refresh (avoids conflicts with pipeline)
    _REFRESH_CLIENT_ID = 20

    print("[REFRESH] Connecting to IBKR TWS...")
    ib = IB()
    ib.connect(config.IB_HOST, config.IB_PORT, clientId=_REFRESH_CLIENT_ID)

    try:
        ib.reqMarketDataType(getattr(config, "MARKET_DATA_TYPE", 1))
    except Exception:
        pass  # not fatal

    try:
        symbols = run_weekly_universe_refresh(ib)
        print(f"\n[REFRESH] Done. {len(symbols)} symbols written to universe_dynamic.txt")
        if symbols:
            sample = ", ".join(symbols[:15])
            print(f"[REFRESH] Sample: {sample}")
    finally:
        ib.disconnect()
        print("[REFRESH] Disconnected from IBKR.")
