from ib_insync import IB, Stock, util
import math
import time

IB_HOST = "127.0.0.1"
IB_PORT = 7497
IB_CLIENT_ID = 12  # choose any unused int

SYMBOL = "AAPL"
EXCHANGE = "SMART"
CURRENCY = "USD"

STOP_LOSS_PCT = 0.15   # 15%
TAKE_PROFIT_PCT = 0.35 # 35%

def round_to_min_tick(price: float, min_tick: float) -> float:
    # Round to nearest tick (simple + safe for most stocks)
    return round(price / min_tick) * min_tick

def get_min_tick(ib: IB, contract) -> float:
    # Pull contract details to discover tick size
    details = ib.reqContractDetails(contract)
    if not details:
        raise RuntimeError("No contract details returned (symbol/exchange wrong?).")
    return details[0].minTick

def wait_for_price(ib: IB, ticker, timeout_s: int = 15) -> float:
    """
    Wait for a usable price. Uses marketPrice() which picks a sensible value from last/bid/ask.
    """
    start = time.time()
    while time.time() - start < timeout_s:
        ib.sleep(0.2)
        px = ticker.marketPrice()
        if px is not None and not math.isnan(px) and px > 0:
            return float(px)
    raise TimeoutError("No valid price received (still no entitlement / wrong data type / market closed).")

def main():
    ib = IB()
    ib.connect(IB_HOST, IB_PORT, clientId=IB_CLIENT_ID)

    # While unfunded/entitlements pending, force delayed data:
    ib.reqMarketDataType(3)

    contract = Stock(SYMBOL, EXCHANGE, CURRENCY)
    ib.qualifyContracts(contract)

    ticker = ib.reqMktData(contract, "", False, False)
    entry_price = wait_for_price(ib, ticker, timeout_s=20)

    min_tick = get_min_tick(ib, contract)

    stop_price = entry_price * (1 - STOP_LOSS_PCT)
    take_price = entry_price * (1 + TAKE_PROFIT_PCT)

    stop_price = round_to_min_tick(stop_price, min_tick)
    take_price = round_to_min_tick(take_price, min_tick)

    print(f"Entry reference price: {entry_price:.4f}")
    print(f"Stop (15%):          {stop_price:.4f}")
    print(f"Take profit (35%):   {take_price:.4f}")

    ib.cancelMktData(contract)
    ib.disconnect()

if __name__ == "__main__":
    main()
