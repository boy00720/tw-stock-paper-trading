import re
import sys

with open("/Users/w/Cursor/automation/scripts/feishu_bot.py", "r") as f:
    code = f.read()

STOCK_QUERY_FUNC = """
def query_stock_inst(stock_target: str, chat_id: str):
    import yfinance as yf
    from FinMind.data import DataLoader
    from datetime import datetime
    import os
    import anthropic
    
    try:
        # 1. 抓取股價
        ticker = yf.Ticker(f"{stock_target}.TW")
        hist = ticker.history(period="2d")
        if len(hist) >= 2:
            prev = hist['Close'].iloc[-2]
            curr = hist['Close'].iloc[-1]
            change_pct = (curr - prev) / prev * 100
            price_info = f"收盤價: {curr:.1f} ({'+' if change_pct>0 else ''}{change_pct:.2f}%)"
        elif len(hist) == 1:
            curr = hist['Close'].iloc[-1]
            price_info = f"收盤價: {curr:.1f}"
            change_pct = 0
        else:
            price_info = "無法取得最新股價"
            curr = 0
            change_pct = 0
            
        # 2. 抓取法人籌碼
        dl = DataLoader()
        first_day = datetime.now().replace(day=1).strftime("%Y-%m-%d")
        df = dl.taiwan_stock_institutional_investors(stock_id=stock_target, start_date=first_day)
        
        inst_info = ""
        if not df is None and not df.empty:
            recent_date = df['date'].max()
            stats = {"外資": {"today": 0, "month": 0}, "投信": {"today": 0, "month": 0}, "自營商": {"today": 0, "month": 0}}
            
            for _, row in df.iterrows():
                net_lots = (row['buy'] - row['sell']) / 1000.0
                inv = row['name']
                if "Foreign" in inv: k = "外資"
                elif "Investment" in inv: k = "投信"
                elif "Dealer" in inv: k = "自營商"
                else: continue
                
                stats[k]["month"] += net_lots
                if row['date'] == recent_date:
                    stats[k]["today"] += net_lots
                    
            def f_num(val): return f"+{val:,.0f}" if val > 0 else f"{val:,.0f}"
            s = stats
            inst_info = f"[今日籌碼(張)] 外: {f_num(s['外資']['today'])} / 投: {f_num(s['投信']['today'])} / 自: {f_num(s['自營商']['today'])}\n"
            inst_info += f"[本月累計(張)] 外: {f_num(s['外資']['month'])} / 投: {f_num(s['投信']['month'])} / 自: {f_num(s['自營商']['month'])}"
        else:
            inst_info = "近期無三大法人進出數據或該代號非台股。"
            
        raw_data = f"股票代號: {stock_target}\n{price_info}\n{inst_info}"
        
        # 3. 呼叫 Claude 寫短評
        client = anthropic.Anthropic(
            api_key=os.getenv("ANTHROPIC_AUTH_TOKEN"),
            base_url=os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
        )
        system_prompt = "你是專業的股市分析師。根據用戶提供的個股最新價位與法人籌碼動向，給出100字以內的極簡精闢短評（以 Markdown 輸出，勿寒暄）。"
        resp = client.messages.create(
            model=os.getenv("ANTHROPIC_MODEL", "sonnet"),
            max_tokens=300,
            system=system_prompt,
            messages=[{"role": "user", "content": raw_data}]
        )
        analysis = resp.content[0].text.strip()
        
        final_msg = f"📊 **{stock_target} 即時籌碼查詢**\n\n{price_info}\n{inst_info}\n\n💡 **AI短評**\n{analysis}"
        send_message(chat_id, final_msg)
        
    except Exception as e:
        logger.error(f"查詢個股失敗: {e}")
        send_message(chat_id, f"❌ 查詢失敗: {e}")

# ── 訊息處理 ──────────────────────────────────────────
"""

PROCESS_MSG_MOD = """# ── 訊息處理 ──────────────────────────────────────────
def process_message(text: str, chat_id: str):
    logger.info(f"收到訊息: {text[:80]}")
    
    # 攔截查籌碼指令
    if text.startswith("查籌碼"):
        stock_id = text.replace("查籌碼", "").strip()
        if stock_id:
            send_message(chat_id, f"🔍 正在為你查詢 {stock_id} 的最新報價與三大法人籌碼，請稍候...")
            threading.Thread(target=query_stock_inst, args=(stock_id, chat_id), daemon=True).start()
        else:
            send_message(chat_id, "請提供股票代號，例如「查籌碼 2330」")
        return
"""

code = code.replace("# ── 訊息處理 ──────────────────────────────────────────\n", STOCK_QUERY_FUNC)
code = code.replace("def process_message(text: str, chat_id: str):\n    logger.info(f\"收到訊息: {text[:80]}\")\n", PROCESS_MSG_MOD.replace("# ── 訊息處理 ──────────────────────────────────────────\n", ""))

with open("/Users/w/Cursor/automation/scripts/feishu_bot.py", "w") as f:
    f.write(code)

print("feishu_bot.py patched successfully")
