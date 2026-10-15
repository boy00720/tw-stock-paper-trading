import re

filepath = "/Users/w/Cursor/automation/scripts/feishu_bot.py"
with open(filepath, 'r') as f:
    content = f.read()

# 1. 關閉內建重試
old_client = """def call_claude(user_message: str) -> str:
    _load_env()
    try:
        client = anthropic.Anthropic(
            api_key=os.getenv("ANTHROPIC_AUTH_TOKEN"),
            base_url=os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
        )"""

new_client = """def call_claude(user_message: str) -> str:
    _load_env()
    try:
        client = anthropic.Anthropic(
            api_key=os.getenv("ANTHROPIC_AUTH_TOKEN"),
            base_url=os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
            max_retries=0, # 關閉官方預設的 2 次慢速重試，遇到 503 瞬間切換
        )"""

content = content.replace(old_client, new_client)

old_client_2 = """        # AI 短評
        try:
            client = anthropic.Anthropic(
                api_key=os.getenv("ANTHROPIC_AUTH_TOKEN"),
                base_url=os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
            )"""

new_client_2 = """        # AI 短評
        try:
            client = anthropic.Anthropic(
                api_key=os.getenv("ANTHROPIC_AUTH_TOKEN"),
                base_url=os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
                max_retries=0,
            )"""

content = content.replace(old_client_2, new_client_2)

with open(filepath, 'w') as f:
    f.write(content)
print("Feishu bot optimized")
