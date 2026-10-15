#!/usr/bin/env python3
"""環境變數檢查工具"""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

required_vars = {
    "FEISHU_APP_ID": "飛書應用 ID",
    "FEISHU_APP_SECRET": "飛書應用密鑰",
    "FEISHU_DEFAULT_CHAT_ID": "飛書預設群組 ID",
    "WECHAT_PUSH_TO_USER": "微信推送目標用戶",
    "ANTHROPIC_API_KEY": "Claude API 金鑰",
    "FINMIND_TOKEN": "FinMind 資料源 Token"
}

optional_vars = {
    "WECHAT_PUSH_CONTEXT_TOKEN": "微信上下文 Token（可選）",
    "FEISHU_USER_OPEN_ID": "飛書用戶 Open ID（可選）"
}

print("=" * 60)
print("環境變數檢查")
print("=" * 60)
print()

missing = []
present = []

for var, desc in required_vars.items():
    val = os.getenv(var)
    if val:
        present.append(f"✓ {var}: {desc}")
    else:
        missing.append(f"✗ {var}: {desc}")

print("必需變數：")
for line in present:
    print(f"  {line}")
for line in missing:
    print(f"  {line}")

print()
print("可選變數：")
for var, desc in optional_vars.items():
    val = os.getenv(var)
    status = "✓" if val else "○"
    print(f"  {status} {var}: {desc}")

print()
if missing:
    print(f"⚠ 缺少 {len(missing)} 個必需變數")
else:
    print("✓ 所有必需變數已設定")
print("=" * 60)
