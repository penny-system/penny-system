# src_ibkr_client.py
from __future__ import annotations

import time
from ib_insync import IB
import config


def connect_ib() -> IB:
    """
    Connects to IBKR TWS / Gateway using config settings.
    Also sets market data type (live/delayed/etc).
    """
    ib = IB()
    ib.connect(config.IB_HOST, config.IB_PORT, clientId=config.IB_CLIENT_ID)

    # Market data mode (1 live, 3 delayed, etc.)
    mdt = getattr(config, "MARKET_DATA_TYPE", 1)
    try:
        ib.reqMarketDataType(int(mdt))
    except Exception:
        # Not fatal; continue
        pass

    return ib


def connect_ib_with_retry(retries: int = 3, delay: int = 10) -> IB:
    """
    Connect to IBKR TWS with up to `retries` attempts, `delay` seconds apart.
    Raises ConnectionError if all attempts fail.
    """
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            return connect_ib()
        except Exception as e:
            last_exc = e
            if attempt < retries:
                print(f"[IBKR] Connection attempt {attempt}/{retries} failed: {e}. Retrying in {delay}s...")
                time.sleep(delay)
    raise ConnectionError(f"IBKR connection failed after {retries} attempts: {last_exc}")


def _pick_first_float(values: list[str]) -> float | None:
    for v in values:
        try:
            x = float(v)
            if x == x:  # not NaN
                return x
        except Exception:
            continue
    return None


def get_available_funds_usd(ib: IB) -> float:
    """
    Returns a best-effort USD 'available funds' number from IBKR.
    This is what we use to cap your configured purse so the bot doesn't
    try to allocate more than the account can actually deploy.

    We try (in order):
      - AvailableFunds (USD)
      - BuyingPower (USD)
      - TotalCashValue (USD)
      - NetLiquidation (USD)
    If nothing is found, returns 0.0.
    """
    try:
        summary = ib.accountSummary()
        ib.sleep(0.5)
    except Exception:
        return 0.0

    # Normalize into tag -> list of values for USD
    tags: dict[str, list[str]] = {}
    for av in summary:
        try:
            if getattr(av, "currency", None) != "USD":
                continue
            tag = getattr(av, "tag", "")
            val = getattr(av, "value", "")
            if not tag:
                continue
            tags.setdefault(tag, []).append(val)
        except Exception:
            continue

    for key in ["AvailableFunds", "BuyingPower", "TotalCashValue", "NetLiquidation"]:
        if key in tags:
            x = _pick_first_float(tags[key])
            if x is not None and x > 0:
                return float(x)

    return 0.0


def get_effective_purse_usd(ib: IB) -> dict:
    """
    Returns a dict with:
      configured_purse_usd
      available_funds_usd
      effective_purse_usd (min(configured, available) when available>0)
      note

    Works with either:
      - config.TRADE_PURSE_CAD + config.USD_PER_CAD  (takes priority when CAD > 0)
    or
      - config.TRADE_PURSE_USD                        (fallback)
    """
    # CAD purse takes priority when set (e.g. via Telegram /purse + /fx overrides)
    cad = float(getattr(config, "TRADE_PURSE_CAD", 0.0) or 0.0)
    if cad > 0:
        fx = float(getattr(config, "USD_PER_CAD", 0.73) or 0.73)
        configured = cad * fx
    else:
        configured = float(getattr(config, "TRADE_PURSE_USD", 0.0) or 0.0)

    configured = max(configured, 0.0)

    available = get_available_funds_usd(ib)

    # If IBKR didn't give us anything (paper sometimes does), fall back to configured
    if available <= 0:
        return {
            "configured_purse_usd": configured,
            "available_funds_usd": available,
            "effective_purse_usd": configured,
            "note": "IBKR available funds not found; using configured purse.",
        }

    effective = min(configured, available)

    note = "Using min(configured, IBKR available)."
    if effective < configured:
        note = "Configured purse exceeds IBKR available; capped to available."

    return {
        "configured_purse_usd": configured,
        "available_funds_usd": available,
        "effective_purse_usd": effective,
        "note": note,
    }