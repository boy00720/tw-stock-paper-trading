import requests
from bs4 import BeautifulSoup
import yfinance as yf

# 1. MI_MARGN
print("========== TWSE MI_MARGN ==========")
r = requests.get("https://www.twse.com.tw/exchangeReport/MI_MARGN?response=json")
data = r.json()
for t in data.get("tables", []):
    for row in t.get("data", []):
        if "融資(交易金額)" in str(row[0]):
            print("MARGIN Row Amount (Thousands):", row)
            break

# 2. TAIFEX
print("========== TAIFEX ==========")
r = requests.get("https://www.taifex.com.tw/cht/3/futContractsDate")
r.encoding = 'utf-8'
soup = BeautifulSoup(r.text, 'html.parser')
for tr in soup.find_all('tr'):
    text = tr.get_text()
    if '臺股期貨' in text and '外資' in text:
        tds = tr.find_all(['td', 'th'])
        vals = [td.get_text(strip=True) for td in tds]
        print("FOREIGN TX ROW:", vals)
        # Expected: ['臺股期貨', '外資及陸資', ...] 
        # Multi-index: [0]=商品 [1]=身份 [2]=多方買口 [3]=多方買額 [4]=空方賣口 [5]=空方賣額 [6]=多空淨口 [7]=多空淨額
        # [8]~[13] is 未平倉 (OI). [8]=多方 [10]=空方 [12]=合約淨額口數
        break
