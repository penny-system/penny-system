from ib_insync import IB, Stock, LimitOrder, StopOrder

# --- Config ---
SYMBOL = "AAPL"
EXCHANGE = "SMART"
CURRENCY = "USD"

QTY = 1
ENTRY = 255.38          # parent buy limit
TAKE_PROFIT = 345.00     # +35%
STOP_LOSS = 217.00       # -15%

if __name__ == "__main__":
    confirm = input(
        "⚠️  This will place a REAL bracket order on the paper account.\n"
        "Type YES to continue: "
    )
    if confirm.strip().upper() != "YES":
        print("Aborted.")
    else:
        ib = IB()
        ib.connect("127.0.0.1", 7497, clientId=3)   # paper port

        # Contract
        contract = Stock(SYMBOL, EXCHANGE, CURRENCY)

        # Parent BUY (not transmitted yet)
        parent = LimitOrder("BUY", QTY, ENTRY)
        parent.transmit = False

        # Take Profit SELL (child)
        tp = LimitOrder("SELL", QTY, TAKE_PROFIT)
        tp.parentId = 0  # set after parent is placed
        tp.transmit = False

        # Stop Loss SELL (child) - last child transmits entire bracket
        sl = StopOrder("SELL", QTY, STOP_LOSS)
        sl.parentId = 0  # set after parent is placed
        sl.transmit = True

        # Place parent first to get an orderId
        trade_parent = ib.placeOrder(contract, parent)
        ib.sleep(1)  # allow orderId assignment

        parent_id = trade_parent.order.orderId
        tp.parentId = parent_id
        sl.parentId = parent_id

        # Place children
        trade_tp = ib.placeOrder(contract, tp)
        trade_sl = ib.placeOrder(contract, sl)

        print("✅ Bracket submitted")
        print("Parent ID:", parent_id)
        print("Parent:", trade_parent.orderStatus.status)
        print("TP:", trade_tp.orderStatus.status, " @", TAKE_PROFIT)
        print("SL:", trade_sl.orderStatus.status, " @", STOP_LOSS)

        ib.disconnect()
