from ib_insync import LimitOrder, StopOrder, Stock
import config
import math

def place_bracket_from_ref(ib, symbol: str, qty: int, ref_price: float, stop_pct: float, tp_pct: float):
    contract = Stock(symbol, "SMART", "USD")
    ib.qualifyContracts(contract)

    entry_limit = round(ref_price, 2)
    take_profit = round(ref_price * (1 + tp_pct), 2)
    stop_loss = round(ref_price * (1 - stop_pct), 2)

    parent = LimitOrder("BUY", qty, entry_limit, transmit=False)
    take = LimitOrder("SELL", qty, take_profit, parentId=0, transmit=False)
    stop = StopOrder("SELL", qty, stop_loss, parentId=0, transmit=True)

    parent_trade = ib.placeOrder(contract, parent)
    ib.sleep(0.5)

    parent_id = parent.orderId
    if not parent_id:
        raise RuntimeError("Parent orderId not assigned.")

    take.parentId = parent_id
    stop.parentId = parent_id

    take_trade = ib.placeOrder(contract, take)
    stop_trade = ib.placeOrder(contract, stop)

    return {
        "parentId": parent_id,
        "entry": entry_limit,
        "take": take_profit,
        "stop": stop_loss
    }
