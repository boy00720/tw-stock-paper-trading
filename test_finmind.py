from FinMind.data import DataLoader
from datetime import datetime, timedelta

dl = DataLoader()

print("Testing TAIEX:")
df = dl.taiwan_stock_institutional_investors(stock_id="TAIEX", start_date=(datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d"))
print(df)

print("\nTesting 2330:")
df2 = dl.taiwan_stock_institutional_investors(stock_id="2330", start_date=(datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d"))
print(df2)
