from FinMind.data import DataLoader
from datetime import datetime, timedelta
import time

dl = DataLoader()

# Try one stock for the whole month
start_date = datetime.now().replace(day=1).strftime("%Y-%m-%d")
print(f"Fetching from {start_date}")

df = dl.taiwan_stock_institutional_investors(stock_id="2330", start_date=start_date)
print("2330 data:")
print(df.tail(10))

# Can we pass multiple stocks?
try:
    df_multi = dl.taiwan_stock_institutional_investors(stock_id="2330,2454", start_date=start_date)
    print("Multi stock query worked?")
except Exception as e:
    print("Multi failed:", e)

