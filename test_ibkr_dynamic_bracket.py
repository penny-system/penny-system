from ib_insync import IB, Stock, LimitOrder, StopOrder, util
import math

SYMBOL = "AAPL"
EXCHANGE = "SMART"
CURRENCY = "USD"

QTY = 1
ENTRY_SLIPPAGE_PCT = 0.001  # 0.1% above market for buy limit so it fills in paper
STOP_PCT = 0.15             # 15%
TP_PCT = 0.35               # 35%

if __name__ == "__main__":
    confirm = input(
        "⚠️  This will place a REAL bracket order on the paper account.\n"
        "Type YES to continue: "
    )
    if confirm.strip().upper() != "YES":
        print("Aborted.")
    else:
        ib = IB()
        ib.connect("127.0.0.1", 7497, clientId=5)  # paper

        ib.reqMarketDataType(3)  # 3 = Delayed market data

        contract = Stock(SYMBOL, EXCHANGE, CURRENCY)

        # --- Get a market price from IBKR ---
        ticker = ib.reqMktData(contract, "", False, False)
        ib.sleep(2)  # allow data to populate

        # Prefer last price, else mid, else close
        last = ticker.last
        bid = ticker.bid
        ask = ticker.ask
        close = ticker.close

        def is_valid(x):
            return x is not None and not (isinstance(x, float) and (math.isnan(x) or math.isinf(x)))

        price = None
        if is_valid(last):
            price = float(last)
        elif is_valid(bid) and is_valid(ask) and ask > 0 and bid > 0:
            price = (float(bid) + float(ask)) / 2.0
        elif is_valid(close):
            price = float(close)

        ib.cancelMktData(contract)

        if price is None:
            raise RuntimeError(
                "No market price available from IBKR (last/bid/ask/close all empty). "
                "This usually means no market data. We can switch to delayed or a fallback source."
            )

        # --- Compute dynamic levels ---
        entry = round(price * (1 + ENTRY_SLIPPAGE_PCT), 2)
        stop_price = round(price * (1 - STOP_PCT), 2)
        tp_price = round(price * (1 + TP_PCT), 2)

        print(f"Market price used: {price}")
        print(f"Entry LMT: {entry} | Stop: {stop_price} | Take Profit: {tp_price}")

        # --- Build bracket ---
        parent = LimitOrder("BUY", QTY, entry)
        parent.transmit = False

        tp = LimitOrder("SELL", QTY, tp_price)
        tp.transmit = False

        sl = StopOrder("SELL", QTY, stop_price)
        sl.transmit = True  # last order transmits the whole bracket

        # Place parent to get orderId
        trade_parent = ib.placeOrder(contract, parent)
        ib.sleep(1)
        parent_id = trade_parent.order.orderId

        tp.parentId = parent_id
        sl.parentId = parent_id

        trade_tp = ib.placeOrder(contract, tp)
        trade_sl = ib.placeOrder(contract, sl)

        print("✅ Dynamic bracket submitted. Parent ID:", parent_id)

        ib.disconnect()
