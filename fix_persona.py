import re

filepath = "/Users/w/Cursor/automation/api_server.py"
with open(filepath, 'r') as f:
    content = f.read()

old_profile = """【溝通風格】喜歡簡短直接、重點優先、附關鍵數字；不喜歡冗長前言、重複說明、無謂鼓勵詞；特別喜歡具體建議、可執行的下一步、毒雞湯。"""
new_profile = """【溝通風格】
1. 當 W 只是閒聊或打招呼（如「你好」、「早安」）時，請以同樣簡短自然的口吻回覆，切勿強行塞入長篇大論的工作進度或毒雞湯。
2. 當 W 詢問工作或進度時，才使用：簡短直接、重點優先、附關鍵數字；不喜歡冗長前言；並提供可執行的下一步及毒雞湯。"""

if old_profile in content:
    content = content.replace(old_profile, new_profile)
    with open(filepath, 'w') as f:
        f.write(content)
    print("Fixed PROFILE_SUMMARY")
