from ib_insync import IB

ib = IB()
ib.connect("127.0.0.1", 7497, clientId=2)

print("✅ Connected:", ib.isConnected())
print("Accounts:", ib.managedAccounts())

positions = ib.positions()
print(f"Positions: {len(positions)}")
for p in positions[:10]:
    print(p.account, p.contract.symbol, p.position, p.avgCost)

open_orders = ib.openOrders()
print(f"Open Orders: {len(open_orders)}")
for o in open_orders[:10]:
    print(o.action, o.totalQuantity, o.orderType, o.lmtPrice if hasattr(o, "lmtPrice") else "")

ib.disconnect()
print("✅ Done.")
