from ib_insync import IB

ib = IB()

# Paper TWS uses port 7497 by default
ib.connect("127.0.0.1", 7497, clientId=1)

print("✅ Connected:", ib.isConnected())
print("Managed accounts:", ib.managedAccounts())

# Request account summary (basic health check)
summary = ib.accountSummary()
print("Account summary items:", len(summary))
for item in summary[:10]:
    print(item.tag, item.value, item.currency)

ib.disconnect()
print("✅ Disconnected.")
