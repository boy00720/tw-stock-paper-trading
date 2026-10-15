from FinMind.data import DataLoader
from datetime import datetime, timedelta

dl = DataLoader()
df = dl.taiwan_stock_institutional_investors_total(start_date=(datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d"))
print(df)
