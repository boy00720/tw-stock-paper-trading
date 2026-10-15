import requests
from bs4 import BeautifulSoup
import pandas as pd

def test_taifex():
    url = "https://www.taifex.com.tw/cht/3/futContractsDate"
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"})
    r.encoding = 'utf-8'
    soup = BeautifulSoup(r.text, 'html.parser')
    
    # 找到 臺股期貨 & 外資 的行
    oi_value = None
    for tr in soup.find_all('tr'):
        text = tr.get_text()
        if '臺股期貨' in text and '外資' in text:
            tds = tr.find_all(['td', 'th'])
            # 外資通常是在 td 裡面的最後幾個欄位，淨未平倉口數通常是倒數第 3 個欄位 (索引12左右)
            # 我們印出來看看
            vals = [td.get_text(strip=True) for td in tds]
            print("TX Foreigner Row:", vals)
            if len(vals) > 10:
                oi_value = vals[-3] # Usually 多空淨額口數 in the 未平倉餘额 block
                print("OI Value extracted:", oi_value)
            break

def test_margin():
    url = "https://www.twse.com.tw/exchangeReport/MI_MARGN?response=json"
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"})
    data = r.json()
    for calc in data.get("creditList", []): # Actually, let's just dump keys
        pass
    print("Margin keys:", data.keys())
    if "creditList" in data:
         print(data["creditList"]) # 融資餘額通常在這裡面
    elif "tables" in data:
        for t in data["tables"]:
            print(t["title"])
            if "信用交易統計" in t["title"]:
                for row in t["data"]:
                    if "融資" in row[0]:
                        print("Margin Row:", row)
                        break

test_taifex()
test_margin()
