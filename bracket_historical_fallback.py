from ib_insync import IB, Stock, LimitOrder, StopOrder
import math

# -----------------------------
# IBKR / TWS Connection Settings
# -----------------------------
IB_HOST = "127.0.0.1"
IB_PORT = 7497          # Paper trading port (usually 7497)
IB_CLIENT_ID = 12       # Use any unused integer

# -----------------------------
# Trade Settings
# -----------------------------
SYMBOL = "AAPL"
EXCHANGE = "SMART"
CURRENCY = "USD"

QTY = 1
STOP_LOSS_PCT = 0.15    # 15% stop loss
TAKE_PROFIT_PCT = 0.35  # 35% take profit


def get_reference_price(ib: IB, contract) -> float:
    """
    Tries to get a streaming (delayed) market price first.
    If streaming is unavailable (e.g., Error 10089), falls back to historical data.

    Tomorrow, once your market data entitlement is active, you can change:
        ib.reqMarketDataType(3) -> ib.reqMarketDataType(1)
    """

    # 3 = Delayed. Use 1 = Live when entitlements are active.
    ib.reqMarketDataType(3)

    # ---- Attempt streaming/delayed market data ----
    try:
        ticker = ib.reqMktData(contract, "", False, False)
        ib.sleep(1.0)

        px = ticker.marketPrice()
        ib.cancelMktData(contract)

        if px is not None and not math.isnan(px) and px > 0:
            print("Using streaming/delayed price")
            return float(px)

    except Exception as e:
        print("Streaming price failed:", e)

    # ---- Fallback to historical data ----
    print("Falling back to historical price")

    bars = ib.reqHistoricalData(
        contract,
        endDateTime="",
        durationStr="1 D",
        barSizeSetting="1 min",
        whatToShow="TRADES",
        useRTH=True,
        formatDate=1
    )

    if not bars:
        raise RuntimeError("No historical bars returned. Try useRTH=False or a different symbol.")

    return float(bars[-1].close)


def main():
    ib = IB()
    ib.connect(IB_HOST, IB_PORT, clientId=IB_CLIENT_ID)

    # Define contract and qualify it
    contract = Stock(SYMBOL, EXCHANGE, CURRENCY)
    ib.qualifyContracts(contract)

    # Get reference price (streaming if possible, otherwise historical)
    ref_price = get_reference_price(ib, contract)

    # Calculate bracket prices
    entry_limit = round(ref_price, 2)
    take_profit = round(ref_price * (1 + TAKE_PROFIT_PCT), 2)
    stop_loss = round(ref_price * (1 - STOP_LOSS_PCT), 2)

    # -----------------------------
    # Build and place bracket order
    # -----------------------------
    # Parent (entry) order
    parent = LimitOrder("BUY", QTY, entry_limit, transmit=False)

    # Children (exit) orders - parentId will be set after parent gets an orderId
    take = LimitOrder("SELL", QTY, take_profit, parentId=0, transmit=False)
    stop = StopOrder("SELL", QTY, stop_loss, parentId=0, transmit=True)  # last child transmits the whole bracket

    # Place parent first to obtain orderId
    parent_trade = ib.placeOrder(contract, parent)
    ib.sleep(0.5)

    parent_id = parent.orderId
    if not parent_id:
        raise RuntimeError("Parent orderId not assigned. Check TWS order permissions/paper account status.")

    # Attach children to parent
    take.parentId = parent_id
    stop.parentId = parent_id

    take_trade = ib.placeOrder(contract, take)
    stop_trade = ib.placeOrder(contract, stop)

    # Output
    print(f"Reference price:     {ref_price:.2f}")
    print(f"Entry LMT:           {entry_limit:.2f}")
    print(f"Take profit (35%):   {take_profit:.2f}")
    print(f"Stop loss (15%):     {stop_loss:.2f}")
    print(f"OrderIds: parent={parent_id}, take={take.orderId}, stop={stop.orderId}")

    # Brief status watch
    for _ in range(10):
        ib.sleep(1)
        print("Parent:", parent_trade.orderStatus.status,
              "| Take:", take_trade.orderStatus.status,
              "| Stop:", stop_trade.orderStatus.status)

    ib.disconnect()


if __name__ == "__main__":
    main()
