import requests
import yfinance as yf
from bs4 import BeautifulSoup
import pandas as pd
from datetime import datetime
import json

def test_twse_bfi82u():
    url = "https://www.twse.com.tw/fund/BFI82U?response=json"
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"})
    if r.status_code == 200:
        data = r.json()
        print("BFI82U Status:", data.get("stat"))
        if data.get("stat") == "OK":
            for row in data.get("data", []):
                print(row)
    else:
        print("BFI82U Error:", r.status_code)

def test_taifex_oi():
    url = "https://www.taifex.com.tw/cht/3/futContractsDate"
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"})
    r.encoding = 'utf-8'
    if r.status_code == 200:
        # Just grab the outer HTML table with class 'table_f' or similar
        dfs = pd.read_html(r.text)
        for df in dfs:
            if df.size > 20 and '契約' in df.to_string():
                # Locate foreign TX OI
                print("Found TAIFEX Table")
                break
    else:
        print("TAIFEX Error:", r.status_code)

def test_yfinance():
    ticker = yf.Ticker("TWD=X")
    df = ticker.history(period="5d")
    print("TWD=X latest close:", df['Close'].iloc[-1] if not df.empty else "No Data")

print("--- TWSE ---")
test_twse_bfi82u()
print("--- TAIFEX ---")
test_taifex_oi()
print("--- YFinance ---")
test_yfinance()

